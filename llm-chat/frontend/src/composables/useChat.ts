import { ref } from 'vue'
import type { Message, ConversationInfo, SendPayload, AgentStatus } from '../types'
import * as api from '../api'

export function useChat() {
  const conversations = ref<ConversationInfo[]>([])
  const currentConvId = ref<string | null>(null)
  const messages = ref<Message[]>([])
  const loading = ref(false)
  const agentStatus = ref<AgentStatus>({ state: 'idle', model: '' })

  // 用于取消正在进行的 SSE 请求
  let abortController: AbortController | null = null

  function cancelStream() {
    if (abortController) {
      abortController.abort()
      abortController = null
    }
    loading.value = false
    agentStatus.value = { state: 'idle', model: '' }
  }

  async function loadConversations() {
    conversations.value = await api.fetchConversations()
  }

  async function selectConversation(id: string) {
    // 切换对话时取消当前流
    cancelStream()
    currentConvId.value = id
    window.location.hash = id
    const data = await api.fetchConversation(id)
    messages.value = (data.messages || []).map((m: Message) => ({
      role: m.role,
      content: m.content,
      images: m.images,
      timestamp: m.timestamp,
    }))
  }

  async function newConversation() {
    cancelStream()
    const data = await api.createConversation()
    currentConvId.value = data.id
    window.location.hash = data.id
    messages.value = []
    await loadConversations()
  }

  async function removeConversation(id: string) {
    if (currentConvId.value === id) cancelStream()
    await api.deleteConversation(id)
    if (currentConvId.value === id) {
      currentConvId.value = null
      messages.value = []
      window.location.hash = ''
    }
    await loadConversations()
  }

  async function restoreFromHash() {
    const id = window.location.hash.slice(1)
    if (id) await selectConversation(id)
  }

  async function send({ text, images }: SendPayload) {
    if (!text.trim() && images.length === 0) return
    if (loading.value) return

    if (!currentConvId.value) {
      const data = await api.createConversation(text.slice(0, 30) || '图片对话')
      currentConvId.value = data.id
    }

    messages.value.push({ role: 'user', content: text, images: images.length > 0 ? images : undefined })
    messages.value.push({ role: 'assistant', content: '' })
    const assistantIdx = messages.value.length - 1

    loading.value = true
    agentStatus.value = { state: 'routing', model: '' }

    abortController = new AbortController()

    try {
      await api.sendMessage(
        currentConvId.value!,
        text,
        '',
        images,
        // onChunk
        (chunk) => {
          messages.value[assistantIdx].content += chunk
        },
        // onToolCall
        (name, input) => {
          const msg = messages.value[assistantIdx]
          if (!msg.toolCalls) msg.toolCalls = []
          // fetch_webpage：初始状态 loading
          if (name === 'fetch_webpage') {
            msg.toolCalls.push({ name, input, done: false, fetchStatus: 'loading' })
          } else {
            msg.toolCalls.push({ name, input, done: false })
          }
          agentStatus.value = { ...agentStatus.value, state: 'tool', tool: name }
        },
        // onToolResult
        (name, data) => {
          const msg = messages.value[assistantIdx]
          const tc = msg.toolCalls?.findLast(t => t.name === name && !t.done)
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
          agentStatus.value = { ...agentStatus.value, state: 'thinking', tool: undefined }
        },
        // onSearchItem：逐条追加搜索结果
        (item) => {
          const msg = messages.value[assistantIdx]
          // 找当前未完成的 web_search toolCall
          const tc = msg.toolCalls?.findLast(t => t.name === 'web_search' && !t.done)
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
            agentStatus.value = { state: 'routing', model: '' }
          } else if (status === 'thinking' && model) {
            agentStatus.value = { ...agentStatus.value, state: 'thinking', model }
          }
        },
        // onRoute
        (model, _intent) => {
          agentStatus.value = { state: 'thinking', model }
        },
        // onDone
        () => {
          agentStatus.value = { ...agentStatus.value, state: 'done' }
          loading.value = false
          abortController = null
          setTimeout(() => {
            agentStatus.value = { ...agentStatus.value, state: 'idle' }
          }, 2000)
          loadConversations()
        },
        abortController.signal,
      )
    } catch (err: any) {
      if (err?.name === 'AbortError') return  // 用户主动取消，不报错
      messages.value[assistantIdx].content = '⚠️ 请求失败，请检查后端和 Ollama 是否正常运行。'
      loading.value = false
      abortController = null
      agentStatus.value = { state: 'idle', model: '' }
    }
  }

  return {
    conversations, currentConvId, messages, loading,
    agentStatus,
    loadConversations, selectConversation, restoreFromHash,
    newConversation, removeConversation, send, cancelStream,
  }
}
