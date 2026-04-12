import type { ClarificationData, FileArtifact, PlanStep, ToolHistoryEvent } from '../types'

const API_BASE = ''

function generateUUID(): string {
  // crypto.randomUUID() requires a secure context (HTTPS/localhost)
  // Fallback for HTTP environments (e.g. direct LAN access)
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0
    return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16)
  })
}

function getClientId(): string {
  let id = localStorage.getItem('cf_client_id')
  if (!id) {
    id = generateUUID()
    localStorage.setItem('cf_client_id', id)
  }
  return id
}

function commonHeaders(): Record<string, string> {
  return {
    'Content-Type': 'application/json',
    'X-Client-ID': getClientId(),
  }
}

export async function fetchModels(): Promise<string[]> {
  const res = await fetch(`${API_BASE}/api/models`, {
    headers: { 'X-Client-ID': getClientId() },
  })
  const data = await res.json()
  return data.models || []
}

export async function fetchConversations() {
  const res = await fetch(`${API_BASE}/api/conversations`, {
    headers: { 'X-Client-ID': getClientId() },
  })
  const data = await res.json()
  return data.conversations || []
}

export async function createConversation(title: string = '新对话') {
  const res = await fetch(`${API_BASE}/api/conversations`, {
    method: 'POST',
    headers: commonHeaders(),
    body: JSON.stringify({ title }),
  })
  return res.json()
}

export async function fetchConversation(id: string) {
  const res = await fetch(`${API_BASE}/api/conversations/${id}`, {
    headers: { 'X-Client-ID': getClientId() },
  })
  return res.json()
}

export async function deleteConversation(id: string) {
  await fetch(`${API_BASE}/api/conversations/${id}`, {
    method: 'DELETE',
    headers: { 'X-Client-ID': getClientId() },
  })
}

export async function renameConversation(id: string, title: string) {
  await fetch(`${API_BASE}/api/conversations/${id}`, {
    method: 'PATCH',
    headers: commonHeaders(),
    body: JSON.stringify({ title }),
  })
}

export async function stopStream(convId: string): Promise<void> {
  await fetch(`${API_BASE}/api/chat/${convId}/stop`, {
    method: 'POST',
    headers: { 'X-Client-ID': getClientId() },
  }).catch(() => {})
}

export async function fetchConvTools(convId: string): Promise<ToolHistoryEvent[]> {
  const res = await fetch(`${API_BASE}/api/conversations/${convId}/tools`, {
    headers: { 'X-Client-ID': getClientId() },
  })
  const data = await res.json()
  return data.events || []
}

export async function fetchLatestPlan(convId: string): Promise<{ id: string; goal: string; steps: PlanStep[] } | null> {
  const res = await fetch(`${API_BASE}/api/conversations/${convId}/plan`, {
    headers: { 'X-Client-ID': getClientId() },
  })
  const data = await res.json()
  return data.plan || null
}

export async function fetchConvArtifacts(convId: string): Promise<FileArtifact[]> {
  const res = await fetch(`${API_BASE}/api/conversations/${convId}/artifacts`, {
    headers: { 'X-Client-ID': getClientId() },
  })
  const data = await res.json()
  return data.artifacts || []
}

/** 按需加载单个产物的完整内容（含二进制、slides_html） */
export async function fetchArtifactContent(artifactId: number): Promise<FileArtifact | null> {
  const res = await fetch(`${API_BASE}/api/artifacts/${artifactId}`, {
    headers: { 'X-Client-ID': getClientId() },
  })
  const data = await res.json()
  return data.error ? null : data
}

/** 获取对话的完整状态（含消息详情、计划、产物等，供刷新后恢复） */
export async function fetchFullState(convId: string) {
  const res = await fetch(`${API_BASE}/api/conversations/${convId}/full-state`, {
    headers: { 'X-Client-ID': getClientId() },
  })
  return res.json()
}

/** 快速检查对话是否有活跃的流式输出 */
export async function fetchStreamingStatus(convId: string): Promise<{ streaming: boolean; last_event_id: number }> {
  const res = await fetch(`${API_BASE}/api/conversations/${convId}/streaming-status`, {
    headers: { 'X-Client-ID': getClientId() },
  })
  return res.json()
}

/** 恢复流式输出（SSE 重连） */
export async function resumeStream(
  convId: string,
  lastIndex: number,
  onChunk: (text: string) => void,
  onToolCall: (name: string, input: Record<string, unknown>) => void,
  onToolResult: (name: string, data: Record<string, unknown>) => void,
  onSearchItem: (item: { url: string; title: string; status: string }) => void,
  onStatus: (status: string, model?: string) => void,
  onRoute: (model: string, intent: string) => void,
  onPlanGenerated: (steps: PlanStep[]) => void,
  onReflection: (content: string, decision: string) => void,
  onDone: () => void,
  onStopped: () => void,
  signal?: AbortSignal,
  onThinking?: (text: string) => void,
  onSandboxOutput?: (toolName: string, stream: string, text: string) => void,
  onFileArtifact?: (artifact: FileArtifact) => void,
  onToolCallStart?: (name: string) => void,
  onResumeContext?: (userMessage: string, images: string[]) => void,
  messageId?: string,
  onToolCallArgs?: (text: string) => void,
) {
  let url = `${API_BASE}/api/conversations/${convId}/resume?after_event_id=${lastIndex}`
  if (messageId) url += `&message_id=${encodeURIComponent(messageId)}`
  const res = await fetch(url, {
    headers: { 'X-Client-ID': getClientId() },
    signal,
  })

  const reader = res.body?.getReader()
  const decoder = new TextDecoder()
  if (!reader) return

  let lastDataTime = Date.now()
  let buffer = ''
  let streamDone = false

  function processLine(line: string) {
    if (!line.startsWith('data: ')) return
    try {
      const data = JSON.parse(line.slice(6))
      if (data.thinking)        onThinking?.(data.thinking)
      if (data.clarification)   onClarification?.(data.clarification)
      if (data.sandbox_output)  onSandboxOutput?.(data.sandbox_output.tool_name, data.sandbox_output.stream, data.sandbox_output.text)
      if (data.file_artifact)   onFileArtifact?.(data.file_artifact as FileArtifact)
      if (data.tool_call_start) onToolCallStart?.(data.tool_call_start.name)
      if (data.tool_call_args)     { lastDataTime = Date.now(); onToolCallArgs?.(data.tool_call_args.text || '') }
      if (data.content)         onChunk(data.content)
      if (data.tool_call)       onToolCall(data.tool_call.name, data.tool_call.input)
      if (data.tool_result)     onToolResult(data.tool_result.name, data.tool_result)
      if (data.search_item)     onSearchItem(data.search_item)
      if (data.status)          onStatus(data.status, data.model)
      if (data.route)           onRoute(data.route.model, data.route.intent)
      if (data.plan_generated)  onPlanGenerated(data.plan_generated.steps)
      if (data.reflection)      onReflection(data.reflection.content, data.reflection.decision)
      if (data.error)           { onChunk('\n\n⚠️ ' + data.error); if (data.can_continue) onInterrupted?.() }
      if (data.ping)            lastDataTime = Date.now()
      if (data.done)            { streamDone = true; try { onDone() } catch(e) { console.error('[SSE] onDone error', e) } }
      if (data.stopped)         { streamDone = true; try { onStopped() } catch(e) { console.error('[SSE] onStopped error', e) } }
    } catch {}
  }

  try {
    while (true) {
      let done: boolean
      let value: Uint8Array | undefined
      try {
        const result = await reader.read()
        done = result.done
        value = result.value
      } catch {
        break
      }
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() ?? ''
      for (const line of lines) processLine(line)
    }
    if (buffer.trim()) processLine(buffer.trim())
  } finally {}

  if (!streamDone) {
    if (signal?.aborted) onStopped()
  }
}

export async function sendMessage(
  conversationId: string,
  message: string,
  model: string,
  images: string[],
  agentMode: boolean,
  forcePlan?: Record<string, unknown>[] | null,
  onChunk: (text: string) => void,
  onToolCall: (name: string, input: Record<string, unknown>) => void,
  onToolResult: (name: string, data: Record<string, unknown>) => void,
  onSearchItem: (item: { url: string; title: string; status: string }) => void,
  onStatus: (status: string, model?: string) => void,
  onRoute: (model: string, intent: string) => void,
  onPlanGenerated: (steps: PlanStep[]) => void,
  onReflection: (content: string, decision: string) => void,
  onDone: () => void,
  onStopped: () => void,
  signal?: AbortSignal,
  onThinking?: (text: string) => void,
  onClarification?: (data: ClarificationData) => void,
  onInterrupted?: () => void,
  onSandboxOutput?: (toolName: string, stream: string, text: string) => void,
  onFileArtifact?: (artifact: FileArtifact) => void,
  onToolCallStart?: (name: string) => void,
  onToolCallArgs?: (text: string) => void,
) {
  const body: Record<string, unknown> = {
    conversation_id: conversationId,
    message,
    model,
    agent_mode: agentMode,
  }
  if (images.length > 0) {
    body.images = images
  }
  if (forcePlan?.length) {
    body.force_plan = forcePlan
  }

  const res = await fetch(`${API_BASE}/api/chat`, {
    method: 'POST',
    headers: commonHeaders(),
    body: JSON.stringify(body),
    signal,
  })

  const reader = res.body?.getReader()
  const decoder = new TextDecoder()

  if (!reader) return

  const IDLE_TIMEOUT_MS = 300_000  // 5 分钟：生成大 HTML / 长文档时 LLM 可能持续输出 2~3 分钟
  let lastDataTime = Date.now()
  let buffer = ''
  let streamDone = false

  // 用 setInterval 独立检查空闲超时，完全不干扰 reader.read() 的 await
  const idleTimer = setInterval(() => {
    if (Date.now() - lastDataTime > IDLE_TIMEOUT_MS) {
      console.warn('[SSE] 超过 120s 无数据，主动断开')
      reader.cancel()
    }
  }, 5000)

  function processLine(line: string) {
    if (!line.startsWith('data: ')) return
    try {
      const data = JSON.parse(line.slice(6))
      // ── 诊断：追踪 tool_call 和 sandbox_output 的处理 ──
      if (data.tool_call) console.log('[SSE诊断] tool_call 到达', data.tool_call.name)
      if (data.sandbox_output) console.log('[SSE诊断] sandbox_output 到达', data.sandbox_output.tool_name, data.sandbox_output.text?.slice(0, 50))
      if (data.thinking)        onThinking?.(data.thinking)
      if (data.clarification)  onClarification?.(data.clarification)
      if (data.sandbox_output) onSandboxOutput?.(data.sandbox_output.tool_name, data.sandbox_output.stream, data.sandbox_output.text)
      if (data.file_artifact)    onFileArtifact?.(data.file_artifact as FileArtifact)
      if (data.tool_call_start) onToolCallStart?.(data.tool_call_start.name)
      if (data.tool_call_args)     { lastDataTime = Date.now(); onToolCallArgs?.(data.tool_call_args.text || '') }
      if (data.content)         onChunk(data.content)
      if (data.tool_call)      onToolCall(data.tool_call.name, data.tool_call.input)
      if (data.tool_result)    onToolResult(data.tool_result.name, data.tool_result)
      if (data.search_item)    onSearchItem(data.search_item)
      if (data.status)         onStatus(data.status, data.model)
      if (data.route)          onRoute(data.route.model, data.route.intent)
      if (data.plan_generated) onPlanGenerated(data.plan_generated.steps)
      if (data.reflection)     onReflection(data.reflection.content, data.reflection.decision)
      if (data.error)          { onChunk('\n\n⚠️ ' + data.error); if (data.can_continue) onInterrupted?.() }
      if (data.ping)           lastDataTime = Date.now()
      // done/stopped 单独 try-catch，确保 streamDone 标记不丢失
      if (data.done)           { streamDone = true; try { onDone() } catch(e) { console.error('[SSE] onDone error', e) } }
      if (data.stopped)        { streamDone = true; try { onStopped() } catch(e) { console.error('[SSE] onStopped error', e) } }
    } catch {}
  }

  try {
    while (true) {
      let done: boolean
      let value: Uint8Array | undefined
      try {
        const result = await reader.read()
        done = result.done
        value = result.value
      } catch {
        break
      }
      if (done) break

      lastDataTime = Date.now()
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() ?? ''
      for (const line of lines) processLine(line)
    }
    // 处理残留 buffer（最后一个事件可能没以 \n 结尾）
    if (buffer.trim()) processLine(buffer.trim())
  } finally {
    clearInterval(idleTimer)
  }

  if (!streamDone) {
    // 区分"用户主动 Stop"和"连接意外断开"：
    //   signal.aborted → 用户点了停止按钮，走 onStopped（不显示 Continue）
    //   其他情况      → 网络中断 / 服务端取消，走 onInterrupted（显示 Continue 按钮）
    if (signal?.aborted) {
      onStopped()
    } else {
      onInterrupted?.()
    }
  }
}