import { ref, reactive, computed } from 'vue'
import type { ClarificationData, Message, StepRecord, ConversationInfo, SendPayload, AgentStatus, CognitiveState, TraceEntry, PlanStep } from '../types'
import { makeEmptyCognitiveState } from '../types'
import * as api from '../api'

interface ConvState {
  messages: Message[]
  loading: boolean
  agentStatus: AgentStatus
  abortController: AbortController | null
  cognitive: CognitiveState
  activeStepIndex: number   // which step currently receives tool/thinking/content events; -1 = no steps
}

function makeConvState(): ConvState {
  return {
    messages: [],
    loading: false,
    agentStatus: { state: 'idle', model: '' },
    abortController: null,
    cognitive: makeEmptyCognitiveState(),
    activeStepIndex: -1,
  }
}

export function useChat() {
  const conversations = ref<ConversationInfo[]>([])
  const currentConvId = ref<string | null>(null)
  const convStates = reactive<Record<string, ConvState>>({})

  // Used to attach workflowPlan/workflowGoal to the next user message push
  const _nextWorkflowPlan = ref<PlanStep[] | null>(null)
  const _nextWorkflowGoal = ref<string>('')

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
  const cognitive = computed<CognitiveState>(() =>
    currentConvId.value
      ? convStates[currentConvId.value]?.cognitive ?? makeEmptyCognitiveState()
      : makeEmptyCognitiveState()
  )
  const activeConvIds = computed<Set<string>>(
    () => new Set(Object.keys(convStates).filter(id => convStates[id].loading))
  )

  function getOrCreate(id: string): ConvState {
    if (!convStates[id]) convStates[id] = makeConvState()
    return convStates[id]
  }

  function addTrace(cog: CognitiveState, entry: Omit<TraceEntry, 'timestamp'>) {
    cog.traceLog.push({ ...entry, timestamp: Date.now() })
    if (cog.traceLog.length > 200) cog.traceLog.splice(0, cog.traceLog.length - 200)
  }

  async function stopConversation(convId?: string) {
    const id = convId ?? currentConvId.value
    if (!id) return
    const s = convStates[id]
    if (!s) return
    if (s.abortController) {
      s.abortController.abort()
      s.abortController = null
    }
    await api.stopStream(id)
    s.loading = false
    s.agentStatus = { state: 'idle', model: s.agentStatus.model }
    s.cognitive.isActive = false
    // Mark running steps as failed on stop
    if (s.cognitive.plan.length > 0) {
      s.cognitive.plan = s.cognitive.plan.map(step => ({
        ...step,
        status: step.status === 'running' ? 'failed' : step.status,
      }))
    }
  }

  function cancelStream(convId?: string) {
    const id = convId ?? currentConvId.value
    if (!id) return
    const s = convStates[id]
    if (!s) return
    if (s.abortController) { s.abortController.abort(); s.abortController = null }
    s.loading = false
    s.agentStatus = { state: 'idle', model: s.agentStatus.model }
    s.cognitive.isActive = false
  }

  async function loadConversations() {
    conversations.value = await api.fetchConversations()
  }

  async function selectConversation(id: string) {
    currentConvId.value = id
    window.location.hash = id
    if (convStates[id]?.loading) return
    const data = await api.fetchConversation(id)
    const s = getOrCreate(id)
    s.messages = (data.messages || []).map((m: Message) => ({
      role: m.role,
      content: m.role === 'assistant'
        ? m.content.replace(/\n\n【工具调用记录】[\s\S]*$/, '')
        : m.content,
      images: m.images,
      timestamp: m.timestamp,
    }))
    s.cognitive = makeEmptyCognitiveState()
    // 加载工具调用历史（供刷新后复现）
    try {
      const toolEvents = await api.fetchConvTools(id)
      s.cognitive.historyEvents = toolEvents
    } catch {}
  }

  async function newConversation() {
    const data = await api.createConversation()
    currentConvId.value = data.id
    window.location.hash = data.id
    convStates[data.id] = makeConvState()
    await loadConversations()
  }

  async function removeConversation(id: string) {
    if (convStates[id]?.loading) await stopConversation(id)
    await api.deleteConversation(id)
    delete convStates[id]
    if (currentConvId.value === id) { currentConvId.value = null; window.location.hash = '' }
    await loadConversations()
  }

  async function restoreFromHash() {
    const id = window.location.hash.slice(1)
    if (id) await selectConversation(id)
  }

  async function send({ text, images }: SendPayload) {
    if (!text.trim() && images.length === 0) return
    if (!currentConvId.value) {
      const data = await api.createConversation(text.slice(0, 30) || '图片对话')
      currentConvId.value = data.id
    }
    const convId = currentConvId.value!
    const s = getOrCreate(convId)
    if (s.loading) return

    // Push user message (with optional workflowPlan for special rendering)
    const userMsg: Message = {
      role: 'user',
      content: text,
      images: images.length > 0 ? images : undefined,
    }
    if (_nextWorkflowPlan.value) {
      userMsg.workflowPlan = _nextWorkflowPlan.value
      userMsg.workflowGoal = _nextWorkflowGoal.value
      _nextWorkflowPlan.value = null
      _nextWorkflowGoal.value = ''
    }
    s.messages.push(userMsg)
    s.messages.push({ role: 'assistant', content: '' })
    const assistantIdx = s.messages.length - 1

    s.loading = true
    s.agentStatus = { state: 'routing', model: '' }
    s.abortController = new AbortController()
    s.activeStepIndex = -1
    s.cognitive = makeEmptyCognitiveState()
    s.cognitive.isActive = true

    // ── 辅助：获取当前活跃步骤（多步模式下） ───────────────────────────────
    const msg = () => s.messages[assistantIdx]
    const activeStep = (): StepRecord | null => {
      const steps = msg().steps
      if (!steps || s.activeStepIndex < 0) return null
      return steps[s.activeStepIndex] ?? steps[steps.length - 1] ?? null
    }

    try {
      await api.sendMessage(
        convId, text, '', images,
        // onChunk
        (chunk) => {
          const step = activeStep()
          if (step) step.content += chunk
          else msg().content += chunk
        },
        // onToolCall
        (name, input) => {
          const tc = name === 'fetch_webpage'
            ? { name, input, done: false, fetchStatus: 'loading' as const }
            : { name, input, done: false }
          const step = activeStep()
          if (step) {
            step.toolCalls.push(tc)
          } else {
            if (!msg().toolCalls) msg().toolCalls = []
            msg().toolCalls!.push(tc)
          }
          s.agentStatus = { ...s.agentStatus, state: 'tool', tool: name }
          addTrace(s.cognitive, { type: 'tool_call', content: `调用 ${name}: ${JSON.stringify(input).slice(0, 180)}`, toolName: name })
        },
        // onToolResult
        (name, data) => {
          const step = activeStep()
          const toolList = step ? step.toolCalls : msg().toolCalls
          const tc = toolList?.findLast(t => t.name === name && !t.done)
          if (tc) {
            if (name === 'fetch_webpage') tc.fetchStatus = (data.status as 'done' | 'fail') || 'done'
            else if (data.results) tc.results = data.results as any
            else if (data.output) tc.output = data.output as string
            tc.done = true
          }
          s.agentStatus = { ...s.agentStatus, state: 'thinking', tool: undefined }
          addTrace(s.cognitive, { type: 'tool_result', content: `${name} 执行完成`, toolName: name })
        },
        // onSearchItem
        (item) => {
          const step = activeStep()
          const toolList = step ? step.toolCalls : msg().toolCalls
          const tc = toolList?.findLast(t => t.name === 'web_search' && !t.done)
          if (tc) {
            if (!tc.searchItems) tc.searchItems = []
            tc.searchItems.push({ url: item.url, title: item.title, status: item.status as any })
          }
          addTrace(s.cognitive, { type: 'search_item', content: `搜索: ${item.title || item.url}` })
        },
        // onStatus
        (status, model) => {
          if (status === 'routing')        s.agentStatus = { state: 'routing', model: s.agentStatus.model }
          else if (status === 'planning')  s.agentStatus = { ...s.agentStatus, state: 'planning' }
          else if (status === 'thinking' && model) s.agentStatus = { ...s.agentStatus, state: 'thinking', model }
          else if (status === 'saving')    s.agentStatus = { ...s.agentStatus, state: 'saving' }
        },
        // onRoute
        (model, intent) => { s.agentStatus = { state: 'thinking', model, intent } },
        // onPlanGenerated
        (planSteps) => {
          const m = msg()
          if (!m.steps) {
            // 首次建立步骤数组
            m.steps = planSteps.map((st, i): StepRecord => ({
              index: i, title: st.title, status: st.status,
              toolCalls: [], thinking: '', content: '',
            }))
            s.activeStepIndex = 0
          } else {
            // 更新已有步骤状态（reflector 推送更新）
            planSteps.forEach((st, i) => {
              if (m.steps![i]) {
                m.steps![i].status = st.status
                m.steps![i].title = st.title
              } else {
                m.steps!.push({ index: i, title: st.title, status: st.status, toolCalls: [], thinking: '', content: '' })
              }
            })
            const runningIdx = planSteps.findIndex(st => st.status === 'running')
            if (runningIdx >= 0) s.activeStepIndex = runningIdx
          }
          s.cognitive.plan = planSteps
          s.cognitive.isActive = true
          const ri = planSteps.findIndex(st => st.status === 'running')
          s.cognitive.currentStepIndex = ri >= 0 ? ri : s.activeStepIndex
        },
        // onReflection
        (content, decision) => {
          s.cognitive.reflection = content
          s.cognitive.reflectorDecision = decision
          s.agentStatus = { ...s.agentStatus, state: 'reflecting' }
          addTrace(s.cognitive, { type: 'reflection', content: `反思（${decision}）: ${content}` })
        },
        // onDone
        () => {
          // 标记所有步骤完成
          const m = msg()
          if (m.steps) {
            m.steps = m.steps.map(step => ({ ...step, status: step.status === 'failed' ? 'failed' : 'done' }))
          }
          if (s.cognitive.plan.length > 0) {
            s.cognitive.plan = s.cognitive.plan.map(step => ({ ...step, status: step.status === 'failed' ? 'failed' : 'done' }))
          }
          s.agentStatus = { ...s.agentStatus, state: 'done' }
          s.loading = false
          s.abortController = null
          s.cognitive.isActive = false
          setTimeout(() => {
            if (s.agentStatus.state === 'done') s.agentStatus = { ...s.agentStatus, state: 'idle' }
          }, 2000)
          loadConversations()
        },
        // onStopped
        () => {
          const m = msg()
          if (m.steps) {
            m.steps = m.steps.map(step => ({ ...step, status: step.status === 'running' ? 'failed' : step.status }))
          }
          if (s.cognitive.plan.length > 0) {
            s.cognitive.plan = s.cognitive.plan.map(step => ({ ...step, status: step.status === 'running' ? 'failed' : step.status }))
          }
          s.loading = false
          s.abortController = null
          s.agentStatus = { state: 'idle', model: s.agentStatus.model }
          s.cognitive.isActive = false
        },
        s.abortController.signal,
        // onThinking
        (thinking) => {
          const step = activeStep()
          if (step) step.thinking += thinking
          else msg().thinking = (msg().thinking ?? '') + thinking
        },
        // onClarification
        (data: ClarificationData) => {
          // 替换整个消息对象（而非仅新增属性），确保 Vue 响应式系统检测到变化
          // 同时清空流式输出的原始 [NEED_CLARIFICATION] 标记文字
          s.messages[assistantIdx] = {
            ...s.messages[assistantIdx],
            content: '',
            clarification: data,
          }
          // 等待用户交互，结束 loading 状态
          s.loading = false
          s.abortController = null
          s.agentStatus = { ...s.agentStatus, state: 'idle' }
          s.cognitive.isActive = false
        },
      )
    } catch (err: any) {
      if (err?.name === 'AbortError') return
      s.messages[assistantIdx].content = '⚠️ 网络连接失败，请检查后端服务是否正常运行。'
      s.loading = false
      s.abortController = null
      s.agentStatus = { state: 'idle', model: '' }
      s.cognitive.isActive = false
    }
  }

  /**
   * 用户提交澄清卡片后，将答案格式化为自然语言，作为新消息发送。
   * 同时清除当前 assistant 消息上的 clarification 数据（卡片已提交）。
   */
  async function submitClarification(answers: Record<string, string | string[]>) {
    if (!currentConvId.value) return
    const s = convStates[currentConvId.value]
    if (!s) return

    // 取出触发澄清的那条 assistant 消息，清除其 clarification 字段
    const lastMsg = s.messages[s.messages.length - 1]
    if (lastMsg?.role === 'assistant' && lastMsg.clarification) {
      const { items } = lastMsg.clarification
      lastMsg.clarification = undefined

      // 找到触发本次澄清的原始用户消息（assistantIdx-1）
      // 将原始意图与补充答案合并，确保路由模型能正确识别任务类型
      const originalUserMsg = s.messages[s.messages.length - 2]
      const originalIntent = originalUserMsg?.role === 'user'
        ? originalUserMsg.content
        : ''

      // 格式化用户答案
      const lines: string[] = []
      items.forEach(item => {
        const val = answers[item.id]
        if (!val || (Array.isArray(val) && val.length === 0)) return
        const display = Array.isArray(val) ? val.join('、') : String(val)
        if (display.trim()) lines.push(`${item.label}：${display}`)
      })

      // 组合：原始意图 + 补充说明，路由器能看到原始任务
      const supplement = lines.join('，')
      const formatted = originalIntent
        ? `${originalIntent}${supplement ? `\n\n补充说明：${supplement}` : ''}`
        : (supplement || '继续')

      await send({ text: formatted, images: [] })
    }
  }

  async function applyModifiedPlan(modifiedPlan: PlanStep[]) {
    if (!currentConvId.value) return
    const s = convStates[currentConvId.value]
    if (!s) return
    if (s.loading) await stopConversation()

    // Preserve original user goal across multiple re-executions
    let originalGoal = ''
    for (let i = s.messages.length - 1; i >= 0; i--) {
      const m = s.messages[i]
      if (m.role !== 'user') continue
      if (m.workflowGoal) { originalGoal = m.workflowGoal; break }
      if (!m.workflowPlan) { originalGoal = m.content; break }
    }

    const planText = modifiedPlan
      .map((step, i) => `${i + 1}. ${step.title}${step.description ? ': ' + step.description : ''}`)
      .join('\n')
    const backendMessage = `请按以下修改后的计划执行任务：\n${planText}`

    // Attach workflow plan so MessageItem renders it as a card
    const resetPlan = modifiedPlan.map(step => ({ ...step, status: 'pending' as const, result: '' }))
    _nextWorkflowPlan.value = resetPlan
    _nextWorkflowGoal.value = originalGoal
    s.cognitive.plan = resetPlan
    s.cognitive.currentStepIndex = 0

    await send({ text: backendMessage, images: [] })
  }

  return {
    conversations, currentConvId, messages, loading, agentStatus, cognitive, activeConvIds,
    loadConversations, selectConversation, restoreFromHash,
    newConversation, removeConversation,
    send, cancelStream, stopConversation, applyModifiedPlan, submitClarification,
  }
}
