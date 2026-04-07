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
    const data = await api.fetchConversation(id)
    const s = getOrCreate(id)
    s.messages = (data.messages || []).map((m: Message) => ({
      role: m.role,
      content: m.role === 'assistant'
        ? m.content
            .replace(/\n\n【工具调用记录】[\s\S]*$/, '')
            .replace(/\n\n【执行过程摘要】[\s\S]*$/, '')
        : m.content,
      images: m.images,
      timestamp: m.timestamp,
    }))
    s.cognitive = makeEmptyCognitiveState()
    s.canContinue = false
    // 并行加载工具调用历史 + 最新执行计划（供刷新后复现）
    try {
      const [toolEvents, latestPlan, artifacts] = await Promise.all([
        api.fetchConvTools(id),
        api.fetchLatestPlan(id),
        api.fetchConvArtifacts(id),
      ])
      s.cognitive.historyEvents = toolEvents
      s.cognitive.artifacts = artifacts
      if (latestPlan) {
        // 刷新加载时对话已结束，所有步骤标记为 done（DB 中最后一步可能还是 running）
        s.cognitive.plan = latestPlan.steps.map(st => ({
          id: st.id,
          title: st.title,
          description: st.description ?? '',
          status: 'done' as const,
          result: st.result ?? '',
        }))
      }
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
        // onChunk — 检测到 [NEED_CLARIFICATION 前的文字自动转为 thinking
        (chunk) => {
          const step = activeStep()
          const target = step || msg()
          target.content = (target.content || '') + chunk

          // 如果内容中出现 [NEED_CLARIFICATION，把之前的推理文字转入 thinking
          const tag = '[NEED_CLARIFICATION]'
          const idx = target.content.indexOf(tag)
          if (idx >= 0) {
            const reasoning = target.content.slice(0, idx).trim()
            if (reasoning) {
              if (step) step.thinking = (step.thinking || '') + reasoning
              else msg().thinking = (msg().thinking || '') + reasoning
            }
            // 保留 tag 之后的内容（会被 onClarification 最终清空）
            target.content = target.content.slice(idx)
          }
        },
        // onToolCall
        (name, input) => {
          const tc = name === 'fetch_webpage'
            ? { name, input, done: false, fetchStatus: 'loading' as const }
            : { name, input, done: false }
          const step = activeStep()
          const toolList = step ? step.toolCalls : (msg().toolCalls ??= [])
          // 替换 tool_call_start 创建的 placeholder（同名且 _generating 标记）
          const placeholderIdx = toolList.findIndex(
            t => t.name === name && !t.done && (t.input as any)?._generating
          )
          if (placeholderIdx >= 0) {
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
              toolCalls: [], thinking: '', content: st.result ?? '',
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
                m.steps!.push({ index: i, title: st.title, status: st.status, toolCalls: [], thinking: '', content: st.result ?? '' })
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
        // onThinking
        (thinking) => {
          const step = activeStep()
          if (step) step.thinking += thinking
          else msg().thinking = (msg().thinking ?? '') + thinking
        },
        // onClarification
        (data: ClarificationData) => {
          // 将澄清前的推理文字转入 thinking（折叠展示），清空 content 显示卡片
          const prev = s.messages[assistantIdx]
          const reasoningText = (prev.content || '').replace(/\[NEED_CLARIFICATION\][\s\S]*/i, '').trim()
          s.messages[assistantIdx] = {
            ...prev,
            content: '',
            thinking: (prev.thinking || '') + (reasoningText ? reasoningText : ''),
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
        // onSandboxOutput：沙箱实时终端输出（execute_code/run_shell 执行过程中）
        // ⚠️ 顺序必须与 api/index.ts 的 sendMessage 签名一致：onSandboxOutput → onFileArtifact → onToolCallStart
        // 使用 requestAnimationFrame 批量刷新，减少 Vue 响应式更新频率
        (() => {
          const bufMap = new Map<any, string>()
          let rafScheduled = false
          function flush() {
            bufMap.forEach((buf, tc) => { tc.output = (tc.output || '') + buf })
            bufMap.clear()
            rafScheduled = false
          }
          return (toolName: string, stream: string, text: string) => {
            const step = activeStep()
            const toolList = step ? step.toolCalls : msg().toolCalls
            const tc = toolList?.findLast(t =>
              (t.name === 'execute_code' || t.name === 'run_shell') && !t.done
            )
            if (!tc) return
            bufMap.set(tc, (bufMap.get(tc) || '') + text)
            if (!rafScheduled) {
              rafScheduled = true
              requestAnimationFrame(flush)
            }
          }
        })(),
        // onFileArtifact：沙箱文件产物（sandbox_write 成功后推送）
        (artifact) => {
          s.cognitive.artifacts.push(artifact)
          addTrace(s.cognitive, { type: 'info', content: `📄 文件已创建: ${artifact.name}` })
        },
        // onToolCallStart：工具参数开始生成（LLM 刚开始输出 tool_call arguments）
        // 立即创建一个 placeholder tool call，让前端终端 loading 状态提前展示
        (name) => {
          const placeholder = { name, input: { _generating: true }, done: false }
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

      await send({ text: formatted, images: [], agentMode: true })
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
        (name, input) => {
          const tc = name === 'fetch_webpage' ? { name, input, done: false, fetchStatus: 'loading' as const } : { name, input, done: false }
          const step = activeStep()
          const toolList = step ? step.toolCalls : (msg().toolCalls ??= [])
          const placeholderIdx = toolList.findIndex(t => t.name === name && !t.done && (t.input as any)?._generating)
          if (placeholderIdx >= 0) toolList[placeholderIdx] = tc; else toolList.push(tc)
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
              content: st.result ?? '',
            }))
            // activeStepIndex 跳到第一个非 done 步骤
            const firstPending = planSteps.findIndex(st => st.status !== 'done')
            s.activeStepIndex = firstPending >= 0 ? firstPending : 0
          } else {
            planSteps.forEach((st, i) => {
              if (m.steps![i]) { m.steps![i].status = st.status; m.steps![i].title = st.title; if (st.result && st.status === 'done' && !m.steps![i].content) m.steps![i].content = st.result }
              else m.steps!.push({ index: i, title: st.title, status: st.status, toolCalls: [], thinking: '', content: st.result ?? '' })
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
        (thinking) => { const step = activeStep(); if (step) step.thinking += thinking; else msg().thinking = (msg().thinking ?? '') + thinking },
        undefined,  // onClarification (not needed for plan execution)
        () => { s.canContinue = true; s.loading = false; s.abortController = null; s.agentStatus = { state: 'idle', model: s.agentStatus.model }; s.cognitive.isActive = false },
        (() => {
          const bufMap = new Map<any, string>(); let rafScheduled = false
          function flush() { bufMap.forEach((buf, tc) => { tc.output = (tc.output || '') + buf }); bufMap.clear(); rafScheduled = false }
          return (toolName: string, stream: string, text: string) => {
            const step = activeStep(); const toolList = step ? step.toolCalls : msg().toolCalls
            const tc = toolList?.findLast(t => (t.name === 'execute_code' || t.name === 'run_shell') && !t.done)
            if (!tc) return; bufMap.set(tc, (bufMap.get(tc) || '') + text)
            if (!rafScheduled) { rafScheduled = true; requestAnimationFrame(flush) }
          }
        })(),
        (artifact) => { s.cognitive.artifacts.push(artifact); addTrace(s.cognitive, { type: 'info', content: `📄 文件已创建: ${artifact.name}` }) },
        (name) => {
          const placeholder = { name, input: { _generating: true }, done: false }
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
    await send({ text: '继续', images: [], agentMode: true })
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
    // Re-send with same content
    await send({
      text: lastUserMsg.content,
      images: lastUserMsg.images ?? [],
      agentMode: true,
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
    // Re-send with new content
    await send({
      text: newContent,
      images: msg.images ?? [],
      agentMode: true,
    })
  }

  function dismissContinue() {
    if (!currentConvId.value) return
    const s = convStates[currentConvId.value]
    if (s) s.canContinue = false
  }

  return {
    conversations, currentConvId, messages, loading, agentStatus, cognitive, activeConvIds,
    canContinue,
    loadConversations, selectConversation, restoreFromHash,
    newConversation, removeConversation,
    send, cancelStream, stopConversation, applyModifiedPlan, submitClarification, continueLast,
    dismissContinue, regenerate, editMessage,
  }
}
