import type {
  ClarificationData, FileArtifact, PlanStep, ThinkingEvent, ToolHistoryEvent, UploadedFile,
  QuantProviderInfo, QuantScreenCriteria, QuantScreenResult,
} from '../types'

/** 归一化 SSE thinking 字段：协议要求 dict，COMPAT 降级到裸字符串。 */
function normalizeThinking(raw: unknown): ThinkingEvent | null {
  if (!raw) return null
  if (typeof raw === 'string') {
    // COMPAT: legacy 裸字符串（旧后端/异常路径）→ 归入未知节点
    return { node: '', step_index: null, phase: 'reasoning', delta: raw }
  }
  if (typeof raw === 'object' && 'delta' in (raw as Record<string, unknown>)) {
    const r = raw as Record<string, unknown>
    const delta = String(r.delta ?? '')
    if (!delta) return null
    const phase = r.phase === 'content' ? 'content' : 'reasoning'
    const stepIdx = typeof r.step_index === 'number' ? r.step_index : null
    return { node: String(r.node ?? ''), step_index: stepIdx, phase, delta }
  }
  return null
}

const API_BASE = ''
let isRefreshing = false
let refreshPromise: Promise<string> | null = null

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

async function refreshAccessToken(): Promise<string> {
  if (isRefreshing && refreshPromise) {
    return refreshPromise
  }

  isRefreshing = true
  refreshPromise = fetch(`${API_BASE}/api/auth/token/refresh`, {
    method: 'POST',
    credentials: 'include',
  })
    .then(res => {
      if (!res.ok) throw new Error('Refresh failed')
      return res.json()
    })
    .then(data => {
      localStorage.setItem('cf_access_token', data.access_token)
      return data.access_token
    })
    .finally(() => {
      isRefreshing = false
      refreshPromise = null
    })

  return refreshPromise
}

async function fetchWithAuth(url: string, options: RequestInit = {}): Promise<Response> {
  const token = localStorage.getItem('cf_access_token')
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    'X-Client-ID': getClientId(),
    ...options.headers as Record<string, string>,
  }

  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }

  const response = await fetch(url, {
    ...options,
    headers,
    credentials: 'include',
  })

  if (response.status === 401) {
    try {
      const newToken = await refreshAccessToken()
      headers['Authorization'] = `Bearer ${newToken}`
      return fetch(url, { ...options, headers, credentials: 'include' })
    } catch {
      localStorage.removeItem('cf_access_token')
      // Optional: trigger a logout event or reload
      return response
    }
  }

  return response
}

export async function get<T>(path: string): Promise<T> {
  const res = await fetchWithAuth(`${API_BASE}${path}`)
  return res.json()
}

export async function post<T>(path: string, body: any): Promise<T> {
  const res = await fetchWithAuth(`${API_BASE}${path}`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  return res.json()
}

export async function put<T>(path: string, body: any): Promise<T> {
  const res = await fetchWithAuth(`${API_BASE}${path}`, {
    method: 'PUT',
    body: JSON.stringify(body),
  })
  return res.json()
}

export async function patch<T>(path: string, body: any): Promise<T> {
  const res = await fetchWithAuth(`${API_BASE}${path}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  })
  return res.json()
}

function commonHeaders(): Record<string, string> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    'X-Client-ID': getClientId(),
  }
  const token = localStorage.getItem('cf_access_token')
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }
  return headers
}

export async function fetchModels(): Promise<string[]> {
  const res = await fetchWithAuth(`${API_BASE}/api/models`)
  const data = await res.json()
  return data.models || []
}

export async function fetchConversations() {
  const res = await fetchWithAuth(`${API_BASE}/api/conversations`)
  const data = await res.json()
  return data.conversations || []
}

export async function createConversation(title: string = '新对话') {
  return post('/api/conversations', { title })
}

export async function fetchConversation(id: string) {
  const res = await fetchWithAuth(`${API_BASE}/api/conversations/${id}`)
  return res.json()
}

export async function deleteConversation(id: string) {
  await fetchWithAuth(`${API_BASE}/api/conversations/${id}`, {
    method: 'DELETE',
  })
}

export async function batchDeleteConversations(ids: string[]): Promise<{ ok: boolean; deleted: number }> {
  return post('/api/conversations/batch-delete', { conversation_ids: ids })
}

export async function renameConversation(id: string, title: string) {
  await fetchWithAuth(`${API_BASE}/api/conversations/${id}`, {
    method: 'PATCH',
    body: JSON.stringify({ title }),
  })
}

export async function stopStream(convId: string): Promise<void> {
  await fetchWithAuth(`${API_BASE}/api/chat/${convId}/stop`, {
    method: 'POST',
  }).catch(() => {})
}

export async function stopStreamWithToken(
  convId: string,
  stopToken: string,
  timeoutMs: number = 30000
): Promise<{ can_continue: boolean; reason: string } | null> {
  const controller = new AbortController()
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs)

  try {
    const res = await fetchWithAuth(`${API_BASE}/api/chat/${convId}/stop`, {
      method: 'POST',
      headers: {
        'X-Stop-Token': stopToken,
      },
      body: JSON.stringify({
        conversation_id: convId,
        stop_token: stopToken,
        timeout_ms: timeoutMs,
      }),
      signal: controller.signal,
    })
    clearTimeout(timeoutId)

    if (!res.ok) return null

    // 读取 SSE 流获取 stop_confirmed
    const reader = res.body?.getReader()
    if (!reader) return null

    const decoder = new TextDecoder()
    let buffer = ''

    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''

      for (const line of lines) {
        if (line.startsWith('data: ')) {
          try {
            const data = JSON.parse(line.slice(6))
            if (data.stop_confirmed) {
              return {
                can_continue: data.can_continue ?? false,
                reason: data.reason ?? 'stopped',
              }
            }
          } catch {}
        }
      }
    }

    return null
  } catch (e) {
    clearTimeout(timeoutId)
    return null
  }
}

export async function fetchConvTools(convId: string): Promise<ToolHistoryEvent[]> {
  const res = await fetchWithAuth(`${API_BASE}/api/conversations/${convId}/tools`)
  const data = await res.json()
  return data.events || []
}

export async function fetchLatestPlan(convId: string): Promise<{ id: string; goal: string; steps: PlanStep[] } | null> {
  const res = await fetchWithAuth(`${API_BASE}/api/conversations/${convId}/plan`)
  const data = await res.json()
  return data.plan || null
}

export async function fetchConvArtifacts(convId: string): Promise<FileArtifact[]> {
  const res = await fetchWithAuth(`${API_BASE}/api/conversations/${convId}/artifacts`)
  const data = await res.json()
  return data.artifacts || []
}

/** 上传单个文件到对话沙箱（multipart/form-data）。返回 artifact 元数据。 */
export async function uploadFile(convId: string, file: File): Promise<UploadedFile> {
  const form = new FormData()
  form.append('conv_id', convId)
  form.append('file', file)
  // 注意：fetchWithAuth 会自动带上 Authorization 和 X-Client-ID
  // 但是我们这里手动处理 FormData，不要设置 Content-Type 让浏览器自动设置 boundary
  const token = localStorage.getItem('cf_access_token')
  const headers: Record<string, string> = {
    'X-Client-ID': getClientId(),
  }
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }

  const res = await fetch(`${API_BASE}/api/files/upload`, {
    method: 'POST',
    headers,
    body: form,
    credentials: 'include',
  })
  if (!res.ok) {
    let detail = ''
    try { detail = (await res.json()).detail || '' } catch {}
    throw new Error(detail || `上传失败 (HTTP ${res.status})`)
  }
  return res.json()
}

/** 按需加载单个产物的完整内容（含二进制、slides_html） */
export async function fetchArtifactContent(artifactId: number): Promise<FileArtifact | null> {
  const res = await fetchWithAuth(`${API_BASE}/api/artifacts/${artifactId}`)
  const data = await res.json()
  return data.error ? null : data
}

/** 用产物下载 URL 拉原始字节（供 PDF/Excel/图片 模态预览使用，避免传 base64 经状态层） */
export async function fetchArtifactBlob(artifactId: number): Promise<Blob> {
  const res = await fetchWithAuth(`${API_BASE}/api/artifacts/${artifactId}/download`)
  if (!res.ok) throw new Error(`下载失败: ${res.status}`)
  return res.blob()
}

/** 获取对话的完整状态（含消息详情、计划、产物等，供刷新后恢复） */
export async function fetchFullState(convId: string) {
  const res = await fetchWithAuth(`${API_BASE}/api/conversations/${convId}/full-state`)
  return res.json()
}

/** 快速检查对话是否有活跃的流式输出 */
export async function fetchStreamingStatus(convId: string): Promise<{ streaming: boolean; last_event_id: number }> {
  const res = await fetchWithAuth(`${API_BASE}/api/conversations/${convId}/streaming-status`)
  return res.json()
}

/** 恢复流式输出（SSE 重连） */
export async function resumeStream(
  convId: string,
  lastIndex: number,
  onChunk: (text: string) => void,
  onToolCall: (name: string, input: Record<string, unknown>, displayMode?: string) => void,
  onToolResult: (name: string, data: Record<string, unknown>) => void,
  onSearchItem: (item: { url: string; title: string; status: string }) => void,
  onStatus: (status: string, model?: string) => void,
  onRoute: (model: string, intent: string) => void,
  onPlanGenerated: (steps: PlanStep[]) => void,
  onReflection: (content: string, decision: string) => void,
  onDone: () => void,
  onStopped: () => void,
  signal?: AbortSignal,
  onThinking?: (ev: ThinkingEvent) => void,
  onSandboxOutput?: (toolName: string, stream: string, text: string) => void,
  onFileArtifact?: (artifact: FileArtifact) => void,
  onToolCallStart?: (name: string, displayMode?: string) => void,
  onResumeContext?: (userMessage: string, images: string[]) => void,
  messageId?: string,
  onToolCallArgs?: (text: string) => void,
  onClarification?: (data: ClarificationData) => void,
  onInterrupted?: () => void,
) {
  let url = `${API_BASE}/api/conversations/${convId}/resume?after_event_id=${lastIndex}`
  if (messageId) url += `&message_id=${encodeURIComponent(messageId)}`
  const res = await fetchWithAuth(url, {
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
      if (data.thinking) {
        const ev = normalizeThinking(data.thinking)
        if (ev) onThinking?.(ev)
      }
      if (data.clarification)   onClarification?.(data.clarification)
      if (data.sandbox_output)  onSandboxOutput?.(data.sandbox_output.tool_name, data.sandbox_output.stream, data.sandbox_output.text)
      if (data.file_artifact)   onFileArtifact?.(data.file_artifact as FileArtifact)
      if (data.tool_call_start) onToolCallStart?.(data.tool_call_start.name, data.tool_call_start.display_mode)
      if (data.tool_call_args)     { lastDataTime = Date.now(); onToolCallArgs?.(data.tool_call_args.text || '') }
      if (data.content)         onChunk(data.content)
      if (data.tool_call)       onToolCall(data.tool_call.name, data.tool_call.input, data.tool_call.display_mode)
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
  onToolCall: (name: string, input: Record<string, unknown>, displayMode?: string) => void,
  onToolResult: (name: string, data: Record<string, unknown>) => void,
  onSearchItem: (item: { url: string; title: string; status: string }) => void,
  onStatus: (status: string, model?: string) => void,
  onRoute: (model: string, intent: string) => void,
  onPlanGenerated: (steps: PlanStep[]) => void,
  onReflection: (content: string, decision: string) => void,
  onDone: () => void,
  onStopped: () => void,
  signal?: AbortSignal,
  onThinking?: (ev: ThinkingEvent) => void,
  onClarification?: (data: ClarificationData) => void,
  onInterrupted?: () => void,
  onSandboxOutput?: (toolName: string, stream: string, text: string) => void,
  onFileArtifact?: (artifact: FileArtifact) => void,
  onToolCallStart?: (name: string, displayMode?: string) => void,
  onToolCallArgs?: (text: string) => void,
  fileIds?: number[],
  contextRefs?: {type: string, id: string}[],
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
  if (fileIds?.length) {
    body.file_ids = fileIds
  }
  if (contextRefs?.length) {
    body.context_refs = contextRefs
  }

  const res = await fetchWithAuth(`${API_BASE}/api/chat`, {
    method: 'POST',
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
      if (data.thinking) {
        const ev = normalizeThinking(data.thinking)
        if (ev) onThinking?.(ev)
      }
      if (data.clarification)  onClarification?.(data.clarification)
      if (data.sandbox_output) onSandboxOutput?.(data.sandbox_output.tool_name, data.sandbox_output.stream, data.sandbox_output.text)
      if (data.file_artifact)    onFileArtifact?.(data.file_artifact as FileArtifact)
      if (data.tool_call_start) onToolCallStart?.(data.tool_call_start.name, data.tool_call_start.display_mode)
      if (data.tool_call_args)     { lastDataTime = Date.now(); onToolCallArgs?.(data.tool_call_args.text || '') }
      if (data.content)         onChunk(data.content)
      if (data.tool_call)      onToolCall(data.tool_call.name, data.tool_call.input, data.tool_call.display_mode)
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

// ── 量化模块 API ─────────────────────────────────────────────────────────────

export interface QuantCacheStatus {
  root: string
  spot_latest: { name: string; date: string | null; age_seconds: number; size_kb: number } | null
  spot_files: number
  bars_latest: { name: string; date: string | null; age_seconds: number; size_kb: number } | null
  bars_files: number
  bars_oldest_date: string | null
  index_files: string[]
  total_mb: number
  warmer_running: boolean
  meta: Record<string, any>
}

export async function fetchQuantCacheStatus(): Promise<QuantCacheStatus> {
  const res = await fetchWithAuth(`${API_BASE}/api/quant/cache/status`)
  if (!res.ok) throw new Error(`获取缓存状态失败: ${res.status}`)
  return res.json()
}

export async function refreshQuantCache(kinds?: string[]): Promise<{ scheduled: string[]; worker: string }> {
  return post('/api/quant/cache/refresh', kinds || null)
}

export async function fetchActiveQuantSession(market?: string): Promise<{ active: boolean; snapshot_id?: string; status?: string; criteria?: QuantScreenCriteria }> {
  let url = `${API_BASE}/api/quant/session/active`
  if (market) url += `?market=${encodeURIComponent(market)}`
  const res = await fetchWithAuth(url)
  if (!res.ok) return { active: false }
  return res.json()
}

export async function fetchQuantSnapshot(snapshotId: string): Promise<QuantScreenResult> {
  const res = await fetchWithAuth(`${API_BASE}/api/quant/snapshot/${snapshotId}`)
  if (!res.ok) throw new Error(`获取快照失败: ${res.status}`)
  return res.json()
}

export async function fetchStockChart(symbol: string, days: number = 240): Promise<any> {
  const res = await fetchWithAuth(`${API_BASE}/api/quant/stock/${symbol}/chart?days=${days}`)
  if (!res.ok) throw new Error(`获取图表失败: ${res.status}`)
  return res.json()
}

export async function fetchQuantProviders(): Promise<QuantProviderInfo[]> {
  const res = await fetchWithAuth(`${API_BASE}/api/quant/providers`)
  if (!res.ok) throw new Error(`获取 Provider 失败: ${res.status}`)
  return res.json()
}

export async function runQuantScreen(criteria: QuantScreenCriteria): Promise<QuantScreenResult> {
  return post('/api/quant/screen', criteria)
}

/**
 * 流式订阅快照分析。后端按 SSE 推送 delta / done / error 三类事件。
 *
 *   onDelta:  每个 LLM token（含 JSON 控制字符，调用方负责剥离展示）
 *   onDone:   收到 {analysis, risk_notes} 结构化结果
 *   onError:  服务端报错或网络断开
 */
export async function streamQuantAnalyze(
  snapshotId: string,
  onDelta: (text: string) => void,
  onDone: (analysis: string, riskNotes: string[]) => void,
  onError: (msg: string) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetchWithAuth(`${API_BASE}/api/quant/snapshot/${snapshotId}/analyze`, {
    signal,
  })
  if (!res.ok) {
    onError(`分析请求失败 (HTTP ${res.status})`)
    return
  }
  const reader = res.body?.getReader()
  if (!reader) {
    onError('浏览器不支持流式读取')
    return
  }
  const decoder = new TextDecoder()
  let buffer = ''

  function processLine(line: string) {
    if (!line.startsWith('data: ')) return
    try {
      const ev = JSON.parse(line.slice(6))
      if (ev.event === 'delta' && typeof ev.text === 'string') onDelta(ev.text)
      else if (ev.event === 'done') onDone(ev.analysis || '', ev.risk_notes || [])
      else if (ev.event === 'error') onError(String(ev.message || '分析失败'))
    } catch {}
  }

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() ?? ''
      for (const line of lines) processLine(line)
    }
    if (buffer.trim()) processLine(buffer.trim())
  } catch (e: any) {
    if (!signal?.aborted) onError(e?.message || '连接中断')
  }
}