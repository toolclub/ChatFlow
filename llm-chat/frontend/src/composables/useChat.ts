import { ref, reactive, computed } from 'vue'
import type { Message, ConversationInfo, SendPayload, AgentStatus } from '../types'
import * as api from '../api'

// ── 每个会话的独立状态 ─────────────────────────────────────────────────────────
interface ConvState {
  messages: Message[]
  loading: boolean
  agentStatus: AgentStatus
  abortController: AbortController | null
}

function makeConvState(): ConvState {
  return {
    messages: [],
    loading: false,
    agentStatus: { state: 'idle', model: '' },
    abortController: null,
  }
}

export function useChat() {
  const conversations = ref<ConversationInfo[]>([])
  const currentConvId = ref<string | null>(null)

  // 所有会话的状态（reactive 保证深层响应性）
  const convStates = reactive<Record<string, ConvState>>({})

  // ── 当前会话的计算属性（自动跟随 currentConvId 切换） ──
  const messages = computed<Message[]>(() =>
    currentConvId.value ? convStates[currentConvId.value]?.messages ?? [] : []
  )
  const loading = computed<boolean>(() =>
    currentConvId.value ? convStates[currentConvId.value]?.loading ?? false : false
  )
  const agentStatus = computed<AgentStatus>(() =>
    currentConvId.value
      ? convStates[currentConvId.value]?.agentStatus ?? { state: 'idle', model: '' }
      : { state: 'idle', model: '' }
  )

  // 正在流式输出的会话 ID 集合（用于侧边栏显示活跃指示器）
  const activeConvIds = computed<Set<string>>(
    () => new Set(Object.keys(convStates).filter(id => convStates[id].loading))
  )

  // ── 工具函数 ───────────────────────────────────────────────────────────────
  function getOrCreate(id: string): ConvState {
    if (!convStates[id]) convStates[id] = makeConvState()
    return convStates[id]
  }

  // ── 停止流（前端+后端双重取消） ────────────────────────────────────────────
  async function stopConversation(convId?: string) {
    const id = convId ?? currentConvId.value
    if (!id) return
    const s = convStates[id]
    if (!s) return

    // 1. 中止前端 SSE 连接
    if (s.abortController) {
      s.abortController.abort()
      s.abortController = null
    }
    // 2. 通知后端停止生成
    await api.stopStream(id)
    // 3. 更新状态
    s.loading = false
    s.agentStatus = { state: 'idle', model: '' }
  }

  // 旧接口兼容（切换会话时不再取消流）
  function cancelStream(convId?: string) {
    const id = convId ?? currentConvId.value
    if (!id) return
    const s = convStates[id]
    if (!s) return
    if (s.abortController) {
      s.abortController.abort()
      s.abortController = null
    }
    s.loading = false
    s.agentStatus = { state: 'idle', model: '' }
  }

  // ── 加载对话列表 ───────────────────────────────────────────────────────────
  async function loadConversations() {
    conversations.value = await api.fetchConversations()
  }

  // ── 切换会话（不取消后台流） ───────────────────────────────────────────────
  async function selectConversation(id: string) {
    currentConvId.value = id
    window.location.hash = id

    // 如果该会话正在流式输出，直接"接回"即可，不重新加载
    if (convStates[id]?.loading) return

    const data = await api.fetchConversation(id)
    const s = getOrCreate(id)
    s.messages = (data.messages || []).map((m: Message) => ({
      role: m.role,
      content: m.content,
      images: m.images,
      timestamp: m.timestamp,
    }))
  }

  // ── 新建会话 ───────────────────────────────────────────────────────────────
  async function newConversation() {
    const data = await api.createConversation()
    currentConvId.value = data.id
    window.location.hash = data.id
    convStates[data.id] = makeConvState()
    await loadConversations()
  }

  // ── 删除会话 ───────────────────────────────────────────────────────────────
  async function removeConversation(id: string) {
    if (convStates[id]?.loading) {
      await stopConversation(id)
    }
    await api.deleteConversation(id)
    delete convStates[id]
    if (currentConvId.value === id) {
      currentConvId.value = null
      window.location.hash = ''
    }
    await loadConversations()
  }

  // ── 从 URL hash 恢复会话 ───────────────────────────────────────────────────
  async function restoreFromHash() {
    const id = window.location.hash.slice(1)
    if (id) await selectConversation(id)
  }

  // ── 发送消息（核心，支持后台多流并发） ────────────────────────────────────
  async function send({ text, images }: SendPayload) {
    if (!text.trim() && images.length === 0) return

    if (!currentConvId.value) {
      const data = await api.createConversation(text.slice(0, 30) || '图片对话')
      currentConvId.value = data.id
    }

    const convId = currentConvId.value!
    const s = getOrCreate(convId)

    if (s.loading) return // 该会话已在流式中，不重复发送

    s.messages.push({ role: 'user', content: text, images: images.length > 0 ? images : undefined })
    s.messages.push({ role: 'assistant', content: '' })
    const assistantIdx = s.messages.length - 1

    s.loading = true
    s.agentStatus = { state: 'routing', model: '' }
    s.abortController = new AbortController()

    try {
      await api.sendMessage(
        convId,
        text,
        '',
        images,
        // onChunk
        (chunk) => {
          s.messages[assistantIdx].content += chunk
        },
        // onToolCall
        (name, input) => {
          if (!s.messages[assistantIdx].toolCalls) s.messages[assistantIdx].toolCalls = []
          if (name === 'fetch_webpage') {
            s.messages[assistantIdx].toolCalls!.push({ name, input, done: false, fetchStatus: 'loading' })
          } else {
            s.messages[assistantIdx].toolCalls!.push({ name, input, done: false })
          }
          s.agentStatus = { ...s.agentStatus, state: 'tool', tool: name }
        },
        // onToolResult
        (name, data) => {
          const tc = s.messages[assistantIdx].toolCalls?.findLast(t => t.name === name && !t.done)
          if (tc) {
            if (name === 'fetch_webpage') {
              tc.fetchStatus = (data.status as 'done' | 'fail') || 'done'
            } else if (data.results) {
              tc.results = data.results as any
            } else if (data.output) {
              tc.output = data.output as string
            }
            tc.done = true
          }
          s.agentStatus = { ...s.agentStatus, state: 'thinking', tool: undefined }
        },
        // onSearchItem
        (item) => {
          const tc = s.messages[assistantIdx].toolCalls?.findLast(t => t.name === 'web_search' && !t.done)
          if (tc) {
            if (!tc.searchItems) tc.searchItems = []
            tc.searchItems.push({
              url: item.url,
              title: item.title,
              status: item.status as 'loading' | 'done' | 'fail',
            })
          }
        },
        // onStatus
        (status, model) => {
          if (status === 'routing') {
            s.agentStatus = { state: 'routing', model: '' }
          } else if (status === 'thinking' && model) {
            s.agentStatus = { ...s.agentStatus, state: 'thinking', model }
          }
        },
        // onRoute
        (model, _intent) => {
          s.agentStatus = { state: 'thinking', model }
        },
        // onDone
        () => {
          s.agentStatus = { ...s.agentStatus, state: 'done' }
          s.loading = false
          s.abortController = null
          setTimeout(() => {
            if (s.agentStatus.state === 'done') {
              s.agentStatus = { ...s.agentStatus, state: 'idle' }
            }
          }, 2000)
          loadConversations()
        },
        // onStopped
        () => {
          s.loading = false
          s.abortController = null
          s.agentStatus = { state: 'idle', model: s.agentStatus.model }
        },
        s.abortController.signal,
      )
    } catch (err: any) {
      if (err?.name === 'AbortError') {
        // 用户主动停止，状态已由 stopConversation 更新，无需操作
        return
      }
      s.messages[assistantIdx].content = '⚠️ 请求失败，请检查后端和 Ollama 是否正常运行。'
      s.loading = false
      s.abortController = null
      s.agentStatus = { state: 'idle', model: '' }
    }
  }

  return {
    conversations,
    currentConvId,
    messages,
    loading,
    agentStatus,
    activeConvIds,
    loadConversations,
    selectConversation,
    restoreFromHash,
    newConversation,
    removeConversation,
    send,
    cancelStream,
    stopConversation,
  }
}
