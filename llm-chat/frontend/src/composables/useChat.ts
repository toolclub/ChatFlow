import { ref, reactive, computed } from 'vue'
import type { ClarificationData, Message, StepRecord, ConversationInfo, SendPayload, AgentStatus, CognitiveState, TraceEntry, PlanStep, ThinkingEvent, ThinkingSegment } from '../types'
import { makeEmptyCognitiveState } from '../types'
import * as api from '../api'

/**
 * 应用一个 thinking SSE 事件到消息/步骤的结构化段列表（spec 协议）。
 *
 * 合并规则（纯字段判定，不解析任何字符串）：
 *   1. step_index !== null 且 msg.steps[step_index] 存在 → 进入该 step 的 thinkingSegments
 *   2. 否则 → 进入 msg.thinkingSegments
 *   3. **只**比较目标列表的栈顶段：key=(node,step_index,phase) 相同才 append；
 *      否则 push 新段。不回找历史段，保证时间顺序严格 = SSE 到达顺序。
 *   4. 同时维护纯文本 thinking（COMPAT，旧 UI 路径才读）。
 */
function applyThinkingEvent(msg: Message, ev: ThinkingEvent): void {
  let targetList: ThinkingSegment[]
  const stepIdx = ev.step_index
  if (stepIdx !== null && msg.steps && stepIdx >= 0 && stepIdx < msg.steps.length) {
    const step = msg.steps[stepIdx]
    if (!step.thinkingSegments) step.thinkingSegments = []
    targetList = step.thinkingSegments
    step.thinking = (step.thinking || '') + ev.delta
  } else {
    if (!msg.thinkingSegments) msg.thinkingSegments = []
    targetList = msg.thinkingSegments
    msg.thinking = (msg.thinking || '') + ev.delta
  }

  const top = targetList[targetList.length - 1]
  if (top && top.node === ev.node && top.step_index === ev.step_index && top.phase === ev.phase) {
    top.content += ev.delta
  } else {
    targetList.push({
      node: ev.node,
      step_index: ev.step_index,
      phase: ev.phase,
      content: ev.delta,
    })
  }
}

/**
 * DB 恢复时：把消息级 thinking_segments 按 step_index 分配到 msg 或 step。
 * 同时重建 step.thinking 纯文本。
 */
function hydrateThinkingSegments(msg: Message, segments: ThinkingSegment[]): void {
  if (!segments?.length) return
  msg.thinkingSegments = []
  for (const seg of segments) {
    const si = seg.step_index
    if (si !== null && msg.steps && si >= 0 && si < msg.steps.length) {
      const step = msg.steps[si]
      if (!step.thinkingSegments) step.thinkingSegments = []
      step.thinkingSegments.push({ ...seg })
      step.thinking = (step.thinking || '') + seg.content
    } else {
      msg.thinkingSegments.push({ ...seg })
    }
  }
}

/** 读取用户当前选择的 Agent/Chat 模式（从 localStorage 持久化状态读取） */
function getCurrentAgentMode(): boolean {
  const saved = localStorage.getItem('cf_agent_mode')
  return saved !== 'false'  // 默认 true
}

interface ConvState {
  messages: Message[]
  loading: boolean
  agentStatus: AgentStatus
  abortController: AbortController | null
  cognitive: CognitiveState
  activeStepIndex: number   // which step currently receives tool/thinking/content events; -1 = no steps
  canContinue: boolean      // true when the last response was interrupted (e.g. recursion limit)
}

function makeConvState(): ConvState {
  return {
    messages: [],
    loading: false,
    agentStatus: { state: 'idle', model: '' },
    abortController: null,
    cognitive: makeEmptyCognitiveState(),
    activeStepIndex: -1,
    canContinue: false,
  }
}

export function useChat() {
  const conversations = ref<ConversationInfo[]>([])
  const currentConvId = ref<string | null>(null)
  const convStates = reactive<Record<string, ConvState>>({})
  const initialLoading = ref(false)  // 页面刷新时从 DB 恢复数据的全局 loading

  // ── 防抖：快速切换对话时 last-wins，丢弃过时的 full-state 响应 ──
  let _selectVersion = 0                       // 递增版本号，标记最新一次 select
  const _selectCache = new Map<string, number>() // conv_id → 上次请求的时间戳（同会话去重）
  const _SELECT_SAME_DEBOUNCE_MS = 2000          // 同一对话 2 秒内不重复请求

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
  const canContinue = computed<boolean>(() =>
    currentConvId.value ? convStates[currentConvId.value]?.canContinue ?? false : false
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

    // ── 防抖策略：同会话去重 + 跨会话 last-wins ──
    // 1. 同一对话 2 秒内不重复请求（防 F5 猛刷）
    const now = Date.now()
    const lastTime = _selectCache.get(id) || 0
    if (now - lastTime < _SELECT_SAME_DEBOUNCE_MS) return
    _selectCache.set(id, now)

    // 2. 不同对话快速切换时，版本号递增使过时响应自动失效（last-wins）
    const version = ++_selectVersion
    const s = getOrCreate(id)

    // 首次加载该对话时显示全局 loading 遮罩（防止闪烁"新对话"空页面）
    const needsLoading = s.messages.length === 0
    if (needsLoading) initialLoading.value = true

    // ── 一次性从 DB 获取完整状态（full-state API 直接查数据库，跨 worker 安全） ──
    try {
      const fullState = await api.fetchFullState(id)

      // last-wins：如果用户在等待期间又切换了别的对话，丢弃这个过时响应
      if (version !== _selectVersion) return

      if (fullState.error) {
        s.messages = []; s.cognitive = makeEmptyCognitiveState(); s.canContinue = false
        return
      }

      // ── 从 DB 恢复所有消息（含 thinking、tool_executions、stream 状态） ──
      s.messages = (fullState.messages || []).map((m: any) => {
        // 完成的消息用 content，未完成的用 stream_buffer（正在生成中的中间状态）
        // DB-first：tool_summary 和 step_summary 已存入独立字段，content 是纯文本。
        // COMPAT：旧消息 content 中可能嵌有标记，清理后显示。
        const rawContent = m.role === 'assistant'
          ? (m.stream_completed !== false ? m.content : (m.content || m.stream_buffer || ''))
          : m.content
        const cleanContent = m.role === 'assistant'
          ? (rawContent || '')
              .replace(/\n\n【工具调用记录】[\s\S]*$/, '')  // COMPAT: 旧数据兼容
              .replace(/\n\n【执行过程摘要】[\s\S]*$/, '')  // COMPAT: 旧数据兼容
          : rawContent

        const msg: Message = {
          role: m.role,
          content: cleanContent,
          timestamp: m.timestamp,
          message_id: m.message_id || undefined,  // DB 业务 ID，用于 plan.message_id 精确匹配
        }

        // thinking 直接从 messages 表恢复（向后兼容纯文本字段）
        if (m.thinking) msg.thinking = m.thinking
        // 结构化思考段：先以扁平列表挂到消息级，若后续恢复了 plan/steps 再重新分发到 step
        if (m.thinking_segments?.length) {
          msg.thinkingSegments = (m.thinking_segments as ThinkingSegment[]).map(s => ({ ...s }))
        }
        if (m.images?.length) msg.images = m.images

        // tool_executions 从新表恢复（含 step_index 用于步骤分发）
        if (m.tool_executions?.length) {
          msg.toolCalls = m.tool_executions.map((te: any) => ({
            name: te.tool_name,
            input: te.tool_input || {},
            output: te.tool_output || undefined,
            searchItems: te.search_items?.length ? te.search_items : undefined,
            fetchStatus: te.tool_name === 'fetch_webpage'
              ? (te.status === 'done' ? 'done' : 'loading') : undefined,
            done: te.status !== 'running',  // error/done/timeout 都算完成
            step_index: te.step_index ?? undefined,  // DB 字段，用于步骤分发
            displayMode: te.display_mode || 'default',  // 协议字段：由后端 SkillRegistry 提供
          }))
        }

        // artifacts 元数据从 message 关联恢复（DB 外键，不再依赖正则）
        if (m.artifacts?.length) {
          msg.artifacts = m.artifacts
        }

        return msg
      })

      // ── 恢复认知面板 ──
      s.cognitive = makeEmptyCognitiveState()
      // 合并所有 artifacts：message 关联的 + 顶层 orphan 的，去重
      const seen = new Set<string>()
      const allArtifacts: any[] = []
      // 来源1：每条 message 上关联的 artifacts（新数据，有 message_id）
      for (const m of s.messages) {
        if (m.artifacts?.length) {
          for (const a of m.artifacts) {
            const key = a.path || a.name
            if (!seen.has(key)) { seen.add(key); allArtifacts.push(a) }
          }
        }
      }
      // 来源2：顶层 orphan artifacts（旧数据，无 message_id）
      for (const a of (fullState.artifacts || [])) {
        const key = a.path || a.name
        if (!seen.has(key)) { seen.add(key); allArtifacts.push(a) }
      }
      s.cognitive.artifacts = allArtifacts
      if (fullState.plan) {
        s.cognitive.plan = fullState.plan.steps.map((st: any) => ({
          id: st.id, title: st.title, description: st.description ?? '',
          status: st.status ?? 'done', result: st.result ?? '',
        }))

        // ── DB-first：从 plan + tool_executions 精确重建 msg.steps ──
        // plan.steps[i].result 保存了每步的模型输出（最多 3000 字符）
        // tool_executions[].step_index 标记了工具属于哪个步骤
        const planSteps = fullState.plan.steps || []
        const planMsgId = fullState.plan.message_id || ''
        const hasStepResults = planSteps.some((st: any) => st.result)
        if (hasStepResults && s.messages.length > 0) {
          // 通过 plan.message_id 精确匹配 assistant 消息（无 message_id 时降级到最后一条）
          const targetIdx = planMsgId
            ? s.messages.findLastIndex((m: any) => m.role === 'assistant' && m.message_id === planMsgId)
            : -1
          const assistantIdx = targetIdx >= 0
            ? targetIdx
            : s.messages.findLastIndex((m: Message) => m.role === 'assistant')
          if (assistantIdx >= 0) {
            const lastAssistant = s.messages[assistantIdx]
            // 按 step_index 分发 toolCalls 到对应步骤
            const toolsByStep = new Map<number, typeof lastAssistant.toolCalls>()
            if (lastAssistant.toolCalls) {
              for (const tc of lastAssistant.toolCalls) {
                const si = (tc as any).step_index
                if (si != null) {
                  if (!toolsByStep.has(si)) toolsByStep.set(si, [])
                  toolsByStep.get(si)!.push(tc)
                }
              }
            }
            lastAssistant.steps = planSteps.map((st: any, i: number): StepRecord => ({
              index: i,
              title: st.title,
              status: st.status ?? 'done',
              toolCalls: toolsByStep.get(i) ?? [],
              thinking: '',
              thinkingSegments: [],
              content: st.result ?? '',
            }))
            // 有 step_index 的工具已分配到步骤，从 message 级别移除避免重复
            if (toolsByStep.size > 0 && lastAssistant.toolCalls) {
              lastAssistant.toolCalls = lastAssistant.toolCalls.filter(
                (tc: any) => tc.step_index == null
              )
            }
            // steps 就绪后，把消息级 thinkingSegments 按 step_index 重新分发到对应 step
            const rawSegs = lastAssistant.thinkingSegments || []
            if (rawSegs.length && lastAssistant.steps) {
              lastAssistant.thinkingSegments = []
              lastAssistant.thinking = ''  // 重置累计，按段重建
              for (const st of lastAssistant.steps) {
                st.thinkingSegments = []; st.thinking = ''
              }
              hydrateThinkingSegments(lastAssistant, rawSegs)
            }
          }
        }
      }
      s.canContinue = false

      // ── 如果有未完成的流式消息，自动恢复 SSE 连接（不阻塞，fire-and-forget） ──
      if (fullState.has_streaming) {
        const streamingMsg = (fullState.messages || []).findLast(
          (m: any) => m.role === 'assistant' && m.stream_completed === false
        )
        const streamingMsgId = streamingMsg?.message_id || ''
        // 不 await：resume 是长连接，阻塞会导致 initialLoading 遮罩卡住
        _resumeActiveStream(id, s, fullState.last_event_id || 0, streamingMsgId)
      }

    } catch (err) {
      console.error('[selectConversation] 加载失败:', err)
      s.messages = []; s.cognitive = makeEmptyCognitiveState(); s.canContinue = false
    } finally {
      if (needsLoading) initialLoading.value = false
    }
  }

  /**
   * 恢复活跃的流式输出：自动重连 SSE，从缓冲区 index=0 重放全部事件。
   *
   * 注意：流式期间消息尚未保存到 DB（save_response 在最后执行），
   * 所以 fetchConversation 返回的是不含当前轮的历史。
   * 我们从 buffer index=0 开始重放，前端会收到完整的当前轮事件。
   */
  async function _resumeActiveStream(convId: string, s: ConvState, lastEventId: number, messageId: string = '') {
    // DB-first：messages 表已有 user + assistant(stream_completed=false) 两条记录。
    // 如果最后一条不是 assistant，补一个空的（兼容旧数据）。
    if (s.messages.length === 0 || s.messages[s.messages.length - 1].role !== 'assistant') {
      s.messages.push({ role: 'assistant', content: '' })
    }
    let assistantIdx = s.messages.length - 1
    // 清空 content / thinking（resume 会从事件重放中重建所有 segments）
    s.messages[assistantIdx].content = ''
    s.messages[assistantIdx].thinking = ''
    s.messages[assistantIdx].thinkingSegments = []
    s.messages[assistantIdx].toolCalls = undefined
    s.messages[assistantIdx].steps = undefined

    s.loading = true
    s.agentStatus = { state: 'thinking', model: '' }
    s.abortController = new AbortController()
    s.activeStepIndex = -1
    s.cognitive.isActive = true

    const msg = () => s.messages[assistantIdx]
    const activeStep = (): StepRecord | null => {
      const steps = msg().steps
      if (!steps || s.activeStepIndex < 0) return null
      return steps[s.activeStepIndex] ?? steps[steps.length - 1] ?? null
    }

    try {
      await api.resumeStream(
        convId, 0,  // 从头重放当前轮事件（通过 messageId 过滤）
        // onChunk
        (chunk) => {
          const step = activeStep()
          const target = step || msg()
          target.content = (target.content || '') + chunk
        },
        // onToolCall
        (name, input, displayMode) => {
          const step = activeStep()
          const toolList = step ? step.toolCalls : (msg().toolCalls ??= [])
          const placeholderIdx = toolList.findIndex(t => t.name === name && !t.done && (t.input as any)?._generating)
          const prevMode = placeholderIdx >= 0 ? toolList[placeholderIdx].displayMode : undefined
          const tc = { name, input, done: false, displayMode: displayMode || prevMode || 'default', ...(name === 'fetch_webpage' ? { fetchStatus: 'loading' as const } : {}) }
          if (placeholderIdx >= 0) {
            const prev = toolList[placeholderIdx]
            if (prev.output) tc.output = prev.output
            toolList[placeholderIdx] = tc
          } else toolList.push(tc)
          s.agentStatus = { ...s.agentStatus, state: 'tool', tool: name }
        },
        // onToolResult
        (name, data) => {
          const step = activeStep(); const toolList = step ? step.toolCalls : msg().toolCalls
          const tc = toolList?.findLast(t => t.name === name && !t.done)
          if (tc) {
            if (name === 'fetch_webpage') tc.fetchStatus = (data.status as 'done' | 'fail') || 'done'
            else if (data.results) tc.results = data.results as any
            // sandbox 工具已有流式终端输出时，不用 tool_result 覆盖（保留进度日志）
            else if (data.output && (!tc.output || !(data as any).is_sandbox)) tc.output = data.output as string
            tc.done = true
          }
          s.agentStatus = { ...s.agentStatus, state: 'thinking', tool: undefined }
        },
        // onSearchItem
        (item) => {
          const step = activeStep(); const toolList = step ? step.toolCalls : msg().toolCalls
          const tc = toolList?.findLast(t => t.name === 'web_search' && !t.done)
          if (tc) { if (!tc.searchItems) tc.searchItems = []; tc.searchItems.push({ url: item.url, title: item.title, status: item.status as any }) }
        },
        // onStatus
        (status, model) => {
          if (status === 'routing') s.agentStatus = { state: 'routing', model: s.agentStatus.model }
          else if (status === 'planning') s.agentStatus = { ...s.agentStatus, state: 'planning' }
          else if (status === 'thinking' && model) s.agentStatus = { ...s.agentStatus, state: 'thinking', model }
          else if (status === 'saving') s.agentStatus = { ...s.agentStatus, state: 'saving' }
        },
        // onRoute
        (model, intent) => { s.agentStatus = { state: 'thinking', model, intent } },
        // onPlanGenerated
        (planSteps) => {
          const m = msg()
          if (!m.steps) {
            m.steps = planSteps.map((st, i): StepRecord => ({
              index: i, title: st.title, status: st.status, toolCalls: [], thinking: '', thinkingSegments: [], content: st.result ?? '',
            }))
            s.activeStepIndex = 0
          } else {
            planSteps.forEach((st, i) => {
              if (m.steps![i]) { m.steps![i].status = st.status; m.steps![i].title = st.title; if (st.result && st.status === 'done' && !m.steps![i].content) m.steps![i].content = st.result }
              else m.steps!.push({ index: i, title: st.title, status: st.status, toolCalls: [], thinking: '', thinkingSegments: [], content: st.result ?? '' })
            })
            const runningIdx = planSteps.findIndex(st => st.status === 'running')
            if (runningIdx >= 0) s.activeStepIndex = runningIdx
          }
          s.cognitive.plan = planSteps; s.cognitive.isActive = true
          const ri = planSteps.findIndex(st => st.status === 'running')
          s.cognitive.currentStepIndex = ri >= 0 ? ri : s.activeStepIndex
        },
        // onReflection
        (content, decision) => {
          s.cognitive.reflection = content; s.cognitive.reflectorDecision = decision
          s.agentStatus = { ...s.agentStatus, state: 'reflecting' }
        },
        // onDone
        () => {
          const m = msg()
          if (m.steps) m.steps = m.steps.map(step => ({ ...step, status: step.status === 'failed' ? 'failed' : 'done' }))
          if (s.cognitive.plan.length > 0) s.cognitive.plan = s.cognitive.plan.map(step => ({ ...step, status: step.status === 'failed' ? 'failed' : 'done' }))
          s.agentStatus = { ...s.agentStatus, state: 'done' }
          s.loading = false; s.canContinue = false; s.abortController = null; s.cognitive.isActive = false
          setTimeout(() => { if (s.agentStatus.state === 'done') s.agentStatus = { ...s.agentStatus, state: 'idle' } }, 2000)
          loadConversations()
        },
        // onStopped
        () => {
          s.loading = false; s.abortController = null
          s.agentStatus = { state: 'idle', model: s.agentStatus.model }; s.cognitive.isActive = false
        },
        s.abortController.signal,
        // onThinking: 结构化事件按 (node, step_index, phase) 分发到 msg/step 的 segments
        (ev) => { applyThinkingEvent(msg(), ev) },
        // onSandboxOutput：按 toolName 精确匹配，兜底最后一个未完成工具
        (toolName, stream, text) => {
          const step = activeStep(); const toolList = step ? step.toolCalls : msg().toolCalls
          const tc = toolList?.findLast(t => t.name === toolName && !t.done) || toolList?.findLast(t => !t.done)
          if (tc) tc.output = (tc.output || '') + text
        },
        // onFileArtifact（去重：DB 已加载的 + SSE 重放的可能重复）
        (artifact) => {
          const key = artifact.path || artifact.name
          if (!s.cognitive.artifacts.some(a => (a.path || a.name) === key)) {
            s.cognitive.artifacts.push(artifact)
          }
        },
        // onToolCallStart
        (name, displayMode) => {
          const placeholder = { name, input: { _generating: true }, done: false, displayMode: displayMode || 'default' }
          const step = activeStep()
          if (step) step.toolCalls.push(placeholder)
          else { if (!msg().toolCalls) msg().toolCalls = []; msg().toolCalls!.push(placeholder) }
          s.agentStatus = { ...s.agentStatus, state: 'tool', tool: name }
        },
        // onResumeContext — 用户消息已在 DB 中，忽略
        undefined,
        // messageId — 限定只回放当前轮的事件
        messageId,
        // onToolCallArgs — token 流式输出
        (text: string) => {
          const step = activeStep(); const toolList = step ? step.toolCalls : msg().toolCalls
          const tc = toolList?.findLast(t => !t.done && (t.input as any)?._generating)
            || toolList?.findLast(t => !t.done)
          if (tc) tc.output = (tc.output || '') + text
        },
      )
    } catch (err: any) {
      if (err?.name === 'AbortError') return
      s.loading = false; s.abortController = null
      s.agentStatus = { state: 'idle', model: '' }; s.cognitive.isActive = false
    }
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

  async function batchRemoveConversations(ids: string[]) {
    if (!ids.length) return
    await Promise.all(ids.map(id => convStates[id]?.loading ? stopConversation(id) : Promise.resolve()))
    await api.batchDeleteConversations(ids)
    ids.forEach(id => delete convStates[id])
    if (currentConvId.value && ids.includes(currentConvId.value)) {
      currentConvId.value = null
      window.location.hash = ''
    }
    await loadConversations()
  }

  async function restoreFromHash() {
    const id = window.location.hash.slice(1)
    if (id) {
      initialLoading.value = true
      try {
        await selectConversation(id)
      } finally {
        initialLoading.value = false
      }
    }
  }

  // 下一次 send 附加的 force_plan（由 applyModifiedPlan 设置，send 消费后清空）
  const _nextForcePlan = ref<PlanStep[] | null>(null)

  async function send({ text, images, agentMode, forcePlan }: SendPayload) {
    // forcePlan 参数优先，其次用 _nextForcePlan（兼容两种调用方式）
    const activeForcePlan = forcePlan || _nextForcePlan.value
    _nextForcePlan.value = null
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
    s.canContinue = false

    // ── 辅助：获取当前活跃步骤（多步模式下） ───────────────────────────────
    const msg = () => s.messages[assistantIdx]
    const activeStep = (): StepRecord | null => {
      const steps = msg().steps
      if (!steps || s.activeStepIndex < 0) return null
      return steps[s.activeStepIndex] ?? steps[steps.length - 1] ?? null
    }

    try {
      await api.sendMessage(
        convId, text, '', images, agentMode,
        activeForcePlan as any,
        // onChunk — 预检澄清通过 onClarification SSE 事件处理。
        // COMPAT: 模型主动输出 [NEED_CLARIFICATION] 时，标记前的文本转入 thinking。
        (chunk) => {
          const step = activeStep()
          const target = step || msg()
          target.content = (target.content || '') + chunk

          // COMPAT: 模型主动澄清 — 标记前的推理文字转入 thinking（作为 clarification 节点的段）
          const tag = '[NEED_CLARIFICATION]'
          const idx = target.content.indexOf(tag)
          if (idx >= 0) {
            const reasoning = target.content.slice(0, idx).trim()
            if (reasoning) {
              applyThinkingEvent(msg(), {
                node: 'clarification',
                step_index: step ? step.index : null,
                phase: 'reasoning',
                delta: reasoning,
              })
            }
            target.content = target.content.slice(idx)
          }
        },
        // onToolCall
        (name, input, displayMode) => {
          const step = activeStep()
          const toolList = step ? step.toolCalls : (msg().toolCalls ??= [])
          // 替换 tool_call_start 创建的 placeholder（同名且 _generating 标记）
          const placeholderIdx = toolList.findIndex(
            t => t.name === name && !t.done && (t.input as any)?._generating
          )
          const prevMode = placeholderIdx >= 0 ? toolList[placeholderIdx].displayMode : undefined
          const tc = { name, input, done: false, displayMode: displayMode || prevMode || 'default', ...(name === 'fetch_webpage' ? { fetchStatus: 'loading' as const } : {}) }
          if (placeholderIdx >= 0) {
            // 保留 _generating 阶段 tool_call_args 积累的流式输出（全流程透明）
            const prev = toolList[placeholderIdx]
            if (prev.output) tc.output = prev.output
            toolList[placeholderIdx] = tc
          } else {
            toolList.push(tc)
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
            else if (data.output) {
              // sandbox 工具已有流式终端输出时，不用 tool_result 覆盖（保留进度日志）
              if (!tc.output || !(data as any).is_sandbox) {
                tc.output = data.output as string
              }
            }
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
          if (status === 'vision_analyze') s.agentStatus = { state: 'vision_analyze', model: '' }
          else if (status === 'routing')   s.agentStatus = { state: 'routing', model: s.agentStatus.model }
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
              toolCalls: [], thinking: '', thinkingSegments: [], content: st.result ?? '',
            }))
            s.activeStepIndex = 0
          } else {
            // 更新已有步骤状态（reflector 推送更新）
            // 中间步骤的内容通过 result 字段传递（这些步骤使用静默模式，不走流式 onChunk）
            planSteps.forEach((st, i) => {
              if (m.steps![i]) {
                m.steps![i].status = st.status
                m.steps![i].title = st.title
                // 中间步骤完成时，用 result 填充 content（静默模式不走 onChunk 流式推送）
                if (st.result && st.status === 'done' && !m.steps![i].content) {
                  m.steps![i].content = st.result
                }
              } else {
                m.steps!.push({ index: i, title: st.title, status: st.status, toolCalls: [], thinking: '', thinkingSegments: [], content: st.result ?? '' })
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
          s.canContinue = false  // 正常完成，确保不显示"继续"按钮
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
        // onThinking: 结构化事件按 (node, step_index, phase) 分发到 msg/step 的 segments
        (ev) => { applyThinkingEvent(msg(), ev) },
        // onClarification — 预检澄清从 SSE 事件直接获取。
        // COMPAT: 模型主动澄清时 content 可能含 [NEED_CLARIFICATION] 残留，一并清理。
        (data: ClarificationData) => {
          const prev = s.messages[assistantIdx]
          const residual = (prev.content || '').replace(/\[NEED_CLARIFICATION\][\s\S]*/i, '').trim()
          s.messages[assistantIdx] = {
            ...prev,
            content: '',
            thinking: (prev.thinking || '') + (residual || ''),
            clarification: data,
          }
          // 等待用户交互，结束 loading 状态
          s.loading = false
          s.abortController = null
          s.agentStatus = { ...s.agentStatus, state: 'idle' }
          s.cognitive.isActive = false
        },
        // onInterrupted：后端中断（recursion limit 等），已保存部分响应，可以继续
        () => {
          s.canContinue = true
          s.loading = false
          s.abortController = null
          s.agentStatus = { state: 'idle', model: s.agentStatus.model }
          s.cognitive.isActive = false
        },
        // onSandboxOutput：按 toolName 精确匹配，兜底最后一个未完成工具
        (toolName: string, stream: string, text: string) => {
          const step = activeStep()
          const toolList = step ? step.toolCalls : msg().toolCalls
          const tc = toolList?.findLast(t => t.name === toolName && !t.done) || toolList?.findLast(t => !t.done)
          if (!tc) return
          tc.output = (tc.output || '') + text
        },
        // onFileArtifact：沙箱文件产物（sandbox_write 成功后推送，去重）
        (artifact) => {
          const key = artifact.path || artifact.name
          if (!s.cognitive.artifacts.some(a => (a.path || a.name) === key)) {
            s.cognitive.artifacts.push(artifact)
            addTrace(s.cognitive, { type: 'info', content: `📄 文件已创建: ${artifact.name}` })
          }
        },
        // onToolCallStart：工具参数开始生成（LLM 刚开始输出 tool_call arguments）
        // 立即创建一个 placeholder tool call，让前端终端 loading 状态提前展示
        (name, displayMode) => {
          const placeholder = { name, input: { _generating: true }, done: false, displayMode: displayMode || 'default' }
          const step = activeStep()
          if (step) {
            const last = step.toolCalls[step.toolCalls.length - 1]
            if (last && last.name === name && !last.done && (last.input as any)._generating) return
            step.toolCalls.push(placeholder)
          } else {
            if (!msg().toolCalls) msg().toolCalls = []
            const last = msg().toolCalls![msg().toolCalls!.length - 1]
            if (last && last.name === name && !last.done && (last.input as any)._generating) return
            msg().toolCalls!.push(placeholder)
          }
          s.agentStatus = { ...s.agentStatus, state: 'tool', tool: name }
          addTrace(s.cognitive, { type: 'tool_call', content: `⏳ 正在生成 ${name} 参数...`, toolName: name })
        },
        // onToolCallArgs：工具参数片段流式到达，追加到当前生成中的工具 output（终端实时显示代码生成）
        (text: string) => {
          const step = activeStep()
          const toolList = step ? step.toolCalls : msg().toolCalls
          // 优先找 _generating 标记的（来自 tool_call_start），兜底找最后一个未完成的
          const tc = toolList?.findLast(t => !t.done && (t.input as any)?._generating)
            || toolList?.findLast(t => !t.done)
          if (tc) tc.output = (tc.output || '') + text
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

      // 若上一轮已有执行计划，将其附加到消息中，供 planner 参考继续/调整
      const activePlan = s.cognitive.plan
      let planContext = ''
      if (activePlan.length > 0) {
        const planLines = activePlan
          .map((step, i) => `${i + 1}. ${step.title}${step.description ? '：' + step.description : ''}`)
          .join('\n')
        planContext = `\n\n[原有执行计划]\n${planLines}\n请基于上述补充信息继续执行或调整计划。`
      }

      const formatted = originalIntent
        ? `${originalIntent}${supplement ? `\n\n补充说明：${supplement}` : ''}${planContext}`
        : (supplement || '继续')

      await send({ text: formatted, images: [], agentMode: getCurrentAgentMode() })
    }
  }

  async function applyModifiedPlan(modifiedPlan: PlanStep[]) {
    if (!currentConvId.value) return
    const s = convStates[currentConvId.value]
    if (!s) return
    if (s.loading) await stopConversation()

    // 找到原始用户目标（不是"请按以下修改后的计划执行任务"这种合成文本）
    let originalGoal = ''
    for (let i = s.messages.length - 1; i >= 0; i--) {
      const m = s.messages[i]
      if (m.role !== 'user') continue
      if (m.workflowGoal) { originalGoal = m.workflowGoal; break }
      if (!m.workflowPlan) { originalGoal = m.content; break }
    }

    // 更新认知面板（前端立即显示新计划状态）
    s.cognitive.plan = modifiedPlan.map(step => ({ ...step }))
    s.cognitive.currentStepIndex = 0

    // 替换最后一条 assistant 消息（避免旧结果和新结果重复显示）
    // 如果最后一条是 assistant，替换它；否则追加新的
    const lastMsg = s.messages[s.messages.length - 1]
    if (lastMsg?.role === 'assistant') {
      s.messages[s.messages.length - 1] = { role: 'assistant', content: '' }
    } else {
      s.messages.push({ role: 'assistant', content: '' })
    }
    const assistantIdx = s.messages.length - 1

    // 发请求：用原始目标作为 message，force_plan 携带编辑后的计划
    // 后端 planner 看到 force_plan 直接使用，跳过 LLM 规划，已完成步骤跳过
    const convId = currentConvId.value!
    s.loading = true
    s.agentStatus = { state: 'planning', model: '' }
    s.abortController = new AbortController()
    s.activeStepIndex = -1
    s.cognitive.isActive = true
    s.canContinue = false

    const msg = () => s.messages[assistantIdx]
    const activeStep = (): StepRecord | null => {
      const steps = msg().steps
      if (!steps || s.activeStepIndex < 0) return null
      return steps[s.activeStepIndex] ?? steps[steps.length - 1] ?? null
    }

    try {
      await api.sendMessage(
        convId, originalGoal || '执行修改后的计划', '', [], true,
        modifiedPlan as any,  // force_plan
        (chunk) => { const step = activeStep(); const target = step || msg(); target.content = (target.content || '') + chunk },
        (name, input, displayMode) => {
          const step = activeStep()
          const toolList = step ? step.toolCalls : (msg().toolCalls ??= [])
          const placeholderIdx = toolList.findIndex(t => t.name === name && !t.done && (t.input as any)?._generating)
          const prevMode = placeholderIdx >= 0 ? toolList[placeholderIdx].displayMode : undefined
          const tc = { name, input, done: false, displayMode: displayMode || prevMode || 'default', ...(name === 'fetch_webpage' ? { fetchStatus: 'loading' as const } : {}) }
          if (placeholderIdx >= 0) { const prev = toolList[placeholderIdx]; if (prev.output) tc.output = prev.output; toolList[placeholderIdx] = tc } else toolList.push(tc)
          s.agentStatus = { ...s.agentStatus, state: 'tool', tool: name }
          addTrace(s.cognitive, { type: 'tool_call', content: `调用 ${name}: ${JSON.stringify(input).slice(0, 180)}`, toolName: name })
        },
        (name, data) => {
          const step = activeStep(); const toolList = step ? step.toolCalls : msg().toolCalls
          const tc = toolList?.findLast(t => t.name === name && !t.done)
          if (tc) { if (name === 'fetch_webpage') tc.fetchStatus = (data.status as any) || 'done'; else if (data.output) tc.output = data.output as string; tc.done = true }
          s.agentStatus = { ...s.agentStatus, state: 'thinking', tool: undefined }
          addTrace(s.cognitive, { type: 'tool_result', content: `${name} 执行完成`, toolName: name })
        },
        (item) => {
          const step = activeStep(); const toolList = step ? step.toolCalls : msg().toolCalls
          const tc = toolList?.findLast(t => t.name === 'web_search' && !t.done)
          if (tc) { if (!tc.searchItems) tc.searchItems = []; tc.searchItems.push({ url: item.url, title: item.title, status: item.status as any }) }
        },
        (status, model) => {
          if (status === 'routing') s.agentStatus = { state: 'routing', model: s.agentStatus.model }
          else if (status === 'planning') s.agentStatus = { ...s.agentStatus, state: 'planning' }
          else if (status === 'thinking' && model) s.agentStatus = { ...s.agentStatus, state: 'thinking', model }
          else if (status === 'saving') s.agentStatus = { ...s.agentStatus, state: 'saving' }
        },
        (model, intent) => { s.agentStatus = { state: 'thinking', model, intent } },
        (planSteps) => {
          const m = msg()
          if (!m.steps) {
            // 首次收到计划：已完成步骤保留上次的完整 result 作为内容
            m.steps = planSteps.map((st, i): StepRecord => ({
              index: i, title: st.title, status: st.status, toolCalls: [], thinking: '',
              thinkingSegments: [],
              content: st.result ?? '',
            }))
            // activeStepIndex 跳到第一个非 done 步骤
            const firstPending = planSteps.findIndex(st => st.status !== 'done')
            s.activeStepIndex = firstPending >= 0 ? firstPending : 0
          } else {
            planSteps.forEach((st, i) => {
              if (m.steps![i]) { m.steps![i].status = st.status; m.steps![i].title = st.title; if (st.result && st.status === 'done' && !m.steps![i].content) m.steps![i].content = st.result }
              else m.steps!.push({ index: i, title: st.title, status: st.status, toolCalls: [], thinking: '', thinkingSegments: [], content: st.result ?? '' })
            })
            const runningIdx = planSteps.findIndex(st => st.status === 'running')
            if (runningIdx >= 0) s.activeStepIndex = runningIdx
          }
          s.cognitive.plan = planSteps; s.cognitive.isActive = true
          const ri = planSteps.findIndex(st => st.status === 'running')
          s.cognitive.currentStepIndex = ri >= 0 ? ri : s.activeStepIndex
        },
        (content, decision) => { s.cognitive.reflection = content; s.cognitive.reflectorDecision = decision; s.agentStatus = { ...s.agentStatus, state: 'reflecting' } },
        () => {
          const m = msg()
          if (m.steps) m.steps = m.steps.map(step => ({ ...step, status: step.status === 'failed' ? 'failed' : 'done' }))
          if (s.cognitive.plan.length > 0) s.cognitive.plan = s.cognitive.plan.map(step => ({ ...step, status: step.status === 'failed' ? 'failed' : 'done' }))
          s.agentStatus = { ...s.agentStatus, state: 'done' }; s.loading = false; s.canContinue = false; s.abortController = null; s.cognitive.isActive = false
          setTimeout(() => { if (s.agentStatus.state === 'done') s.agentStatus = { ...s.agentStatus, state: 'idle' } }, 2000)
          loadConversations()
        },
        () => { s.loading = false; s.abortController = null; s.agentStatus = { state: 'idle', model: s.agentStatus.model }; s.cognitive.isActive = false },
        s.abortController.signal,
        (ev) => { applyThinkingEvent(msg(), ev) },
        undefined,  // onClarification (not needed for plan execution)
        () => { s.canContinue = true; s.loading = false; s.abortController = null; s.agentStatus = { state: 'idle', model: s.agentStatus.model }; s.cognitive.isActive = false },
        (() => {
          const bufMap = new Map<any, string>(); let rafScheduled = false
          function flush() { bufMap.forEach((buf, tc) => { tc.output = (tc.output || '') + buf }); bufMap.clear(); rafScheduled = false }
          return (toolName: string, stream: string, text: string) => {
            const step = activeStep(); const toolList = step ? step.toolCalls : msg().toolCalls
            const tc = toolList?.findLast(t => t.name === toolName && !t.done) || toolList?.findLast(t => !t.done)
            if (!tc) return; bufMap.set(tc, (bufMap.get(tc) || '') + text)
            if (!rafScheduled) { rafScheduled = true; requestAnimationFrame(flush) }
          }
        })(),
        (artifact) => {
          const key = artifact.path || artifact.name
          if (!s.cognitive.artifacts.some(a => (a.path || a.name) === key)) {
            s.cognitive.artifacts.push(artifact)
            addTrace(s.cognitive, { type: 'info', content: `📄 文件已创建: ${artifact.name}` })
          }
        },
        (name, displayMode) => {
          const placeholder = { name, input: { _generating: true }, done: false, displayMode: displayMode || 'default' }
          const step = activeStep()
          if (step) { const last = step.toolCalls[step.toolCalls.length - 1]; if (last && last.name === name && !last.done && (last.input as any)._generating) return; step.toolCalls.push(placeholder) }
          else { if (!msg().toolCalls) msg().toolCalls = []; const last = msg().toolCalls![msg().toolCalls!.length - 1]; if (last && last.name === name && !last.done && (last.input as any)._generating) return; msg().toolCalls!.push(placeholder) }
          s.agentStatus = { ...s.agentStatus, state: 'tool', tool: name }
        },
      )
    } catch (err: any) {
      if (err?.name === 'AbortError') return
      s.messages[assistantIdx].content = '⚠️ 执行计划失败，请重试。'
      s.loading = false; s.abortController = null; s.agentStatus = { state: 'idle', model: '' }; s.cognitive.isActive = false
    }
  }

  /**
   * 用户点击"继续"按钮：发 "继续" 触发后端 Planner 的 DB 续写恢复逻辑。
   *
   * 后端 Planner 检测到 "继续" 后：
   *   1. 从 DB 加载上次中断的计划（含已完成步骤的结果）
   *   2. 直接跳到第一个未完成步骤执行，跳过已完成步骤，不重跑搜索
   *   3. call_model 使用 plan_goal（原始任务）而非 "继续" 构建聚焦上下文
   */
  async function continueLast() {
    if (!currentConvId.value) return
    const s = convStates[currentConvId.value]
    if (!s || s.loading) return
    s.canContinue = false
    await send({ text: '继续', images: [], agentMode: getCurrentAgentMode() })
  }

  /**
   * 重新生成最后一条 AI 回复：删除最后一轮 (user + assistant)，用原始消息重新发送。
   */
  async function regenerate() {
    if (!currentConvId.value) return
    const s = convStates[currentConvId.value]
    if (!s || s.loading) return
    // Find last user message
    let lastUserIdx = -1
    for (let i = s.messages.length - 1; i >= 0; i--) {
      if (s.messages[i].role === 'user') { lastUserIdx = i; break }
    }
    if (lastUserIdx < 0) return
    const lastUserMsg = s.messages[lastUserIdx]
    // Remove last user + assistant pair from frontend
    s.messages.splice(lastUserIdx)
    // Re-send with same content (respect current mode selection)
    await send({
      text: lastUserMsg.content,
      images: lastUserMsg.images ?? [],
      agentMode: getCurrentAgentMode(),
    })
  }

  /**
   * 编辑用户消息：替换指定消息内容，删除该消息之后的所有消息，重新发送。
   */
  async function editMessage(msgIndex: number, newContent: string) {
    if (!currentConvId.value) return
    const s = convStates[currentConvId.value]
    if (!s || s.loading) return
    const msg = s.messages[msgIndex]
    if (!msg || msg.role !== 'user') return
    // Truncate messages from this point
    s.messages.splice(msgIndex)
    // Re-send with new content (respect current mode selection)
    await send({
      text: newContent,
      images: msg.images ?? [],
      agentMode: getCurrentAgentMode(),
    })
  }

  function dismissContinue() {
    if (!currentConvId.value) return
    const s = convStates[currentConvId.value]
    if (s) s.canContinue = false
  }

  return {
    conversations, currentConvId, messages, loading, agentStatus, cognitive, activeConvIds,
    canContinue, initialLoading,
    loadConversations, selectConversation, restoreFromHash,
    newConversation, removeConversation, batchRemoveConversations,
    send, cancelStream, stopConversation, applyModifiedPlan, submitClarification, continueLast,
    dismissContinue, regenerate, editMessage,
  }
}
