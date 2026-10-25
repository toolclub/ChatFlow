import { consumeChatStream } from './chatStream'
import type {
  ClarificationData, FileArtifact, PlanStep, ThinkingEvent, ToolHistoryEvent, UploadedFile, ToolDisplayMode,
  QuantProviderInfo, QuantScreenCriteria, QuantScreenResult,
  PredictionRecord, PredictionSnapshot, NewsItem, NewsDimension, Forecast,
  PredictionProgress, MarketDataErrorKind, PredictionMarket,
  Dimension, DimensionGroup, DimensionCatalog, DimensionKind,
  FilingDimensionResult, FormulaStep, ForecastCandle, ToolTrace, EventMarker,
} from '../types'
import { DIMENSION_KIND_LABELS } from '../types'

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

export async function get<T>(path: string, params: Record<string, any> = {}, headers: Record<string, string> = {}): Promise<T> {
  const query = new URLSearchParams(params).toString()
  const url = `${API_BASE}${path}${query ? '?' + query : ''}`
  const res = await fetchWithAuth(url, { headers })
  if (!res.ok) {
    const error = await res.json().catch(() => ({}))
    throw new Error(error.detail || `Request failed with status ${res.status}`)
  }
  return res.json()
}

export async function post<T>(path: string, body: any): Promise<T> {
  const res = await fetchWithAuth(`${API_BASE}${path}`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const error = await res.json().catch(() => ({}))
    throw new Error(error.detail || `Request failed with status ${res.status}`)
  }
  return res.json()
}

export async function put<T>(path: string, body: any): Promise<T> {
  const res = await fetchWithAuth(`${API_BASE}${path}`, {
    method: 'PUT',
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const error = await res.json().catch(() => ({}))
    throw new Error(error.detail || `Request failed with status ${res.status}`)
  }
  return res.json()
}

export async function patch<T>(path: string, body: any): Promise<T> {
  const res = await fetchWithAuth(`${API_BASE}${path}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const error = await res.json().catch(() => ({}))
    throw new Error(error.detail || `Request failed with status ${res.status}`)
  }
  return res.json()
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
  return post<{ id: string }>('/api/conversations', { title })
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
  } catch {
    return null
  } finally {
    clearTimeout(timeoutId)
    controller.abort()
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
  if (!res.ok) {
    const error = await res.json().catch(() => ({}))
    throw new Error(error.detail || `加载对话失败 (${res.status})`)
  }
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
  onToolCall: (name: string, input: Record<string, unknown>, displayMode?: ToolDisplayMode, toolRunId?: string) => void,
  onToolResult: (name: string, data: Record<string, unknown>) => void,
  onSearchItem: (item: { url: string; title: string; status: string; tool_run_id?: string }) => void,
  onStatus: (status: string, model?: string) => void,
  onRoute: (model: string, intent: string) => void,
  onPlanGenerated: (steps: PlanStep[]) => void,
  onReflection: (content: string, decision: string) => void,
  onDone: () => void,
  onStopped: () => void,
  signal?: AbortSignal,
  onThinking?: (ev: ThinkingEvent) => void,
  onSandboxOutput?: (toolName: string, stream: string, text: string, toolRunId?: string) => void,
  onFileArtifact?: (artifact: FileArtifact) => void,
  onToolCallStart?: (name: string, displayMode?: ToolDisplayMode) => void,
  _onResumeContext?: (userMessage: string, images: string[]) => void,
  messageId?: string,
  onToolCallArgs?: (text: string) => void,
  onClarification?: (data: ClarificationData) => void,
  onInterrupted?: () => void,
) {
  const result = await consumeChatStream({
    initial: () => reconnect(lastIndex, messageId || ''),
    initialRequestSafe: true,
    reconnect,
    cursor: lastIndex,
    messageId,
    signal,
    onEvent: dispatch,
    onConnection: (reconnecting) => onStatus(reconnecting ? 'reconnecting' : 'connected'),
  })
  if (result === 'done') onDone()
  else if (result === 'stopped') onStopped()
  else onInterrupted?.()

  function reconnect(cursor: number, roundId: string) {
    const query = new URLSearchParams({ after_event_id: String(cursor) })
    if (roundId) query.set('message_id', roundId)
    return fetchWithAuth(`${API_BASE}/api/conversations/${convId}/resume?${query}`, { signal })
  }
  function dispatch(data: Record<string, any>) {
    if (data.thinking) {
      const ev = normalizeThinking(data.thinking)
      if (ev) onThinking?.(ev)
    }
    if (data.clarification) onClarification?.(data.clarification)
    if (data.sandbox_output) onSandboxOutput?.(data.sandbox_output.tool_name, data.sandbox_output.stream, data.sandbox_output.text, data.sandbox_output.tool_run_id)
    if (data.file_artifact) onFileArtifact?.(data.file_artifact)
    if (data.tool_call_start) onToolCallStart?.(data.tool_call_start.name, data.tool_call_start.display_mode)
    if (data.tool_call_args) onToolCallArgs?.(data.tool_call_args.text || '')
    if (data.content) onChunk(data.content)
    if (data.tool_call) onToolCall(data.tool_call.name, data.tool_call.input, data.tool_call.display_mode, data.tool_call.tool_run_id)
    if (data.tool_result) onToolResult(data.tool_result.name, data.tool_result)
    if (data.search_item) onSearchItem(data.search_item)
    if (data.status) onStatus(data.status, data.model)
    if (data.route) onRoute(data.route.model, data.route.intent)
    if (data.plan_generated) onPlanGenerated(data.plan_generated.steps)
    if (data.reflection) onReflection(data.reflection.content, data.reflection.decision)
    if (data.error) onChunk('\n\n⚠️ ' + data.error)
  }
}

export async function sendMessage(
  conversationId: string,
  message: string,
  model: string,
  images: string[],
  agentMode: boolean,
  forcePlan: Record<string, unknown>[] | null | undefined,
  onChunk: (text: string) => void,
  onToolCall: (name: string, input: Record<string, unknown>, displayMode?: ToolDisplayMode, toolRunId?: string) => void,
  onToolResult: (name: string, data: Record<string, unknown>) => void,
  onSearchItem: (item: { url: string; title: string; status: string; tool_run_id?: string }) => void,
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
  onSandboxOutput?: (toolName: string, stream: string, text: string, toolRunId?: string) => void,
  onFileArtifact?: (artifact: FileArtifact) => void,
  onToolCallStart?: (name: string, displayMode?: ToolDisplayMode) => void,
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

  const result = await consumeChatStream({
    initial: () => fetchWithAuth(`${API_BASE}/api/chat`, {
      method: 'POST', body: JSON.stringify(body), signal,
    }),
    reconnect: (cursor, roundId) => {
      const query = new URLSearchParams({ after_event_id: String(cursor) })
      if (roundId) query.set('message_id', roundId)
      return fetchWithAuth(`${API_BASE}/api/conversations/${conversationId}/resume?${query}`, { signal })
    },
    signal,
    onEvent: dispatch,
    onConnection: (reconnecting) => onStatus(reconnecting ? 'reconnecting' : 'connected'),
  })
  if (result === 'done') onDone()
  else if (result === 'stopped') onStopped()
  else onInterrupted?.()

  function dispatch(data: Record<string, any>) {
    if (data.thinking) {
      const ev = normalizeThinking(data.thinking)
      if (ev) onThinking?.(ev)
    }
    if (data.clarification) onClarification?.(data.clarification)
    if (data.sandbox_output) onSandboxOutput?.(data.sandbox_output.tool_name, data.sandbox_output.stream, data.sandbox_output.text, data.sandbox_output.tool_run_id)
    if (data.file_artifact) onFileArtifact?.(data.file_artifact)
    if (data.tool_call_start) onToolCallStart?.(data.tool_call_start.name, data.tool_call_start.display_mode)
    if (data.tool_call_args) onToolCallArgs?.(data.tool_call_args.text || '')
    if (data.content) onChunk(data.content)
    if (data.tool_call) onToolCall(data.tool_call.name, data.tool_call.input, data.tool_call.display_mode, data.tool_call.tool_run_id)
    if (data.tool_result) onToolResult(data.tool_result.name, data.tool_result)
    if (data.search_item) onSearchItem(data.search_item)
    if (data.status) onStatus(data.status, data.model)
    if (data.route) onRoute(data.route.model, data.route.intent)
    if (data.plan_generated) onPlanGenerated(data.plan_generated.steps)
    if (data.reflection) onReflection(data.reflection.content, data.reflection.decision)
    if (data.error) onChunk('\n\n⚠️ ' + data.error)
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

export async function fetchQuantCacheStatus(adminKey?: string): Promise<QuantCacheStatus> {
  const headers: Record<string, string> = {}
  if (adminKey) headers['X-Admin-Key'] = adminKey
  const res = await fetchWithAuth(`${API_BASE}/api/quant/cache/status`, { headers })
  if (!res.ok) throw new Error(`获取缓存状态失败: ${res.status}`)
  return res.json()
}

export async function refreshQuantCache(kinds?: string[], adminKey?: string): Promise<{ scheduled: string[]; worker: string }> {
  const headers: Record<string, string> = {}
  if (adminKey) headers['X-Admin-Key'] = adminKey
  // post() 不支持自定义 header，这里用 fetchWithAuth 直接发
  const res = await fetchWithAuth(`${API_BASE}/api/quant/cache/refresh`, {
    method: 'POST',
    body: JSON.stringify(kinds ?? null),
    headers,
  })
  if (!res.ok) {
    const error = await res.json().catch(() => ({}))
    throw new Error(error.detail || `Request failed with status ${res.status}`)
  }
  return res.json()
}

export async function fetchActiveQuantSession(market?: string, adminKey?: string): Promise<{ active: boolean; snapshot_id?: string; status?: string; criteria?: QuantScreenCriteria }> {
  const headers: Record<string, string> = {}
  if (adminKey) headers['X-Admin-Key'] = adminKey
  let url = `${API_BASE}/api/quant/session/active`
  if (market) url += `?market=${encodeURIComponent(market)}`
  const res = await fetchWithAuth(url, { headers })
  if (!res.ok) return { active: false }
  return res.json()
}

export async function fetchQuantSnapshot(snapshotId: string, adminKey?: string): Promise<QuantScreenResult> {
  const headers: Record<string, string> = {}
  if (adminKey) headers['X-Admin-Key'] = adminKey
  const res = await fetchWithAuth(`${API_BASE}/api/quant/snapshot/${snapshotId}`, { headers })
  if (!res.ok) throw new Error(`获取快照失败: ${res.status}`)
  return res.json()
}

export async function fetchStockChart(symbol: string, days: number = 240, adminKey?: string, signal?: AbortSignal): Promise<any> {
  const headers: Record<string, string> = {}
  if (adminKey) headers['X-Admin-Key'] = adminKey
  const res = await fetchWithAuth(`${API_BASE}/api/quant/stock/${symbol}/chart?days=${days}`, { headers, signal })
  if (!res.ok) throw new Error(`获取图表失败: ${res.status}`)
  return res.json()
}

export async function fetchQuantProviders(adminKey?: string): Promise<QuantProviderInfo[]> {
  const headers: Record<string, string> = {}
  if (adminKey) headers['X-Admin-Key'] = adminKey
  const res = await fetchWithAuth(`${API_BASE}/api/quant/providers`, { headers })
  if (!res.ok) throw new Error(`获取 Provider 失败: ${res.status}`)
  return res.json()
}

export async function runQuantScreen(criteria: QuantScreenCriteria, adminKey?: string): Promise<QuantScreenResult> {
  const headers: Record<string, string> = {}
  if (adminKey) headers['X-Admin-Key'] = adminKey
  const res = await fetchWithAuth(`${API_BASE}/api/quant/screen`, {
    method: 'POST',
    body: JSON.stringify(criteria),
    headers,
  })
  if (!res.ok) {
    const error = await res.json().catch(() => ({}))
    throw new Error(error.detail || `Request failed with status ${res.status}`)
  }
  return res.json()
}

export interface StreamQuantAnalyzeHandlers {
  /** 推理过程 token（reasoning_content）—— 推理模型才会推送，走思考块 UI */
  onThinking?: (text: string) => void
  /** 最终内容增量 token */
  onDelta: (text: string) => void
  /** 解析后的结构化结果 */
  onDone: (
    analysis: string,
    riskNotes: string[],
    thinkingSegments?: Array<{ node: string; step_index: number | null; phase: string; content: string }>,
  ) => void
  onError: (msg: string) => void
}

/**
 * 流式订阅快照分析。后端按 SSE 推送 thinking / delta / done / error 四类事件。
 *
 *   onThinking: reasoning_content 增量（推理模型，无则不推）
 *   onDelta:    最终内容 token 增量（含 JSON 控制字符，调用方负责剥离展示）
 *   onDone:     {analysis, risk_notes, thinking_segments} 结构化结果
 *   onError:    服务端报错或网络断开
 *
 * 旧调用方式（前 4 个独立回调参数）保留向后兼容。
 */
export async function streamQuantAnalyze(
  snapshotId: string,
  ...rest:
    | [handlers: StreamQuantAnalyzeHandlers, signal?: AbortSignal]
    | [
        onDelta: (text: string) => void,
        onDone: (analysis: string, riskNotes: string[]) => void,
        onError: (msg: string) => void,
        signal?: AbortSignal,
      ]
): Promise<void> {
  // 解析两种调用形态
  let handlers: StreamQuantAnalyzeHandlers
  let signal: AbortSignal | undefined
  if (typeof rest[0] === 'object' && rest[0] !== null && 'onDelta' in (rest[0] as object)) {
    handlers = rest[0] as StreamQuantAnalyzeHandlers
    signal = rest[1] as AbortSignal | undefined
  } else {
    handlers = {
      onDelta: rest[0] as (text: string) => void,
      onDone: rest[1] as (analysis: string, riskNotes: string[]) => void,
      onError: rest[2] as (msg: string) => void,
    }
    signal = rest[3] as AbortSignal | undefined
  }

  const res = await fetchWithAuth(`${API_BASE}/api/quant/snapshot/${snapshotId}/analyze`, {
    signal,
  })
  if (!res.ok) {
    handlers.onError(`分析请求失败 (HTTP ${res.status})`)
    return
  }
  const reader = res.body?.getReader()
  if (!reader) {
    handlers.onError('浏览器不支持流式读取')
    return
  }
  const decoder = new TextDecoder()
  let buffer = ''

  function processLine(line: string) {
    if (!line.startsWith('data: ')) return
    try {
      const ev = JSON.parse(line.slice(6))
      if (ev.event === 'thinking' && typeof ev.text === 'string') {
        handlers.onThinking?.(ev.text)
      } else if (ev.event === 'delta' && typeof ev.text === 'string') {
        handlers.onDelta(ev.text)
      } else if (ev.event === 'done') {
        handlers.onDone(ev.analysis || '', ev.risk_notes || [], ev.thinking_segments || [])
      } else if (ev.event === 'error') {
        handlers.onError(String(ev.message || '分析失败'))
      }
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
    if (!signal?.aborted) handlers.onError(e?.message || '连接中断')
  }
}

// ── 股票预测模块 API ─────────────────────────────────────────────────────────

export interface PredictionCriteria {
  symbol: string
  market: 'cn_a' | 'us_stock'
  horizon?: number
  methods?: string[]
  dimensions?: string[]
}

/** POST /api/prediction/predict — 发起预测，立即返回 prediction_id（status=queued） */
export async function startPrediction(
  criteria: PredictionCriteria,
  adminKey: string,
): Promise<{ prediction_id: string; status: string }> {
  const res = await fetchWithAuth(`${API_BASE}/api/prediction/predict`, {
    method: 'POST',
    body: JSON.stringify(criteria),
    headers: { 'X-Admin-Key': adminKey },
  })
  if (!res.ok) {
    const error = await res.json().catch(() => ({}))
    throw new Error(error.detail || `Request failed with status ${res.status}`)
  }
  return res.json()
}

/** POST /api/prediction/{id}/stop — 停止预测（取消后台图 + astream + LLM HTTP 流，不再发 token） */
export async function stopPrediction(predictionId: string, adminKey: string): Promise<{ ok: boolean; status: string }> {
  const res = await fetchWithAuth(`${API_BASE}/api/prediction/${predictionId}/stop`, {
    method: 'POST',
    headers: { 'X-Admin-Key': adminKey },
  })
  if (!res.ok) {
    const error = await res.json().catch(() => ({}))
    throw new Error(error.detail || `Request failed with status ${res.status}`)
  }
  return res.json()
}

// ── v2：维度注册表 / 暂停 / 恢复 / 删除 ────────────────────────────────────────

/** 后端 groups 的两种可能序列化形态：[["组名", ["k1","k2"]]] 或 [{name, keys}] */
type RawGroup = [string, string[]] | { name?: string; keys?: string[] } | unknown

/** 单个维度：宽松解析（缺字段给安全默认值，未知 kind 归为 search —— 只影响分组，不影响能否勾选） */
function normalizeDimension(raw: any): Dimension | null {
  if (!raw || typeof raw.key !== 'string' || !raw.key) return null
  const kind: DimensionKind = raw.kind === 'data' ? 'data' : 'search'
  const markets = Array.isArray(raw.markets)
    ? (raw.markets.filter((m: unknown) => m === 'cn_a' || m === 'us_stock') as PredictionMarket[])
    : (['cn_a', 'us_stock'] as PredictionMarket[])
  return {
    key: raw.key,
    label: typeof raw.label === 'string' && raw.label ? raw.label : (raw.name || raw.key),
    desc: typeof raw.desc === 'string' ? raw.desc : '',
    kind,
    markets,
    fetcher: typeof raw.fetcher === 'string' ? raw.fetcher : null,
    default_on: raw.default_on !== false,
  }
}

/**
 * 归一化 GET /api/prediction/dimensions 的响应。
 *
 * 容忍三种形态（后端 router 尚未定稿，前端不该因为一层包装就白屏）：
 *   1. { dimensions: [...], groups: [...], defaults: [...] }
 *   2. { dimensions: [...] }                  → groups 按 kind 兜底分组、defaults 按 default_on 兜底
 *   3. [...]（裸数组）                         → 同上
 *
 * groups 缺失时按 kind 分组（data → 结构化数据 / search → 资讯搜索），
 * 组名与 key 顺序都以后端为准 —— 前端只在后端没给时才兜底。
 */
export function normalizeDimensionCatalog(raw: any): DimensionCatalog {
  const rawDims: any[] = Array.isArray(raw)
    ? raw
    : Array.isArray(raw?.dimensions)
      ? raw.dimensions
      : []
  const dimensions = rawDims
    .map(normalizeDimension)
    .filter((d): d is Dimension => d !== null)

  const known = new Set(dimensions.map((d) => d.key))

  // groups：后端给了就用后端的（单一真相源），否则按 kind 兜底
  let groups: DimensionGroup[] = []
  const rawGroups: RawGroup[] = Array.isArray(raw?.groups) ? raw.groups : []
  for (const g of rawGroups) {
    let name = ''
    let keys: string[] = []
    if (Array.isArray(g) && typeof g[0] === 'string') {
      name = g[0]
      keys = Array.isArray(g[1]) ? (g[1] as string[]) : []
    } else if (g && typeof g === 'object') {
      const o = g as { name?: string; keys?: string[] }
      name = typeof o.name === 'string' ? o.name : ''
      keys = Array.isArray(o.keys) ? o.keys : []
    }
    // 只保留本次返回里真实存在的 key（后端按 market 过滤后，组里可能有不适用的残留 key）
    const kept = keys.filter((k) => known.has(k))
    if (name && kept.length) groups.push({ name, keys: kept })
  }
  if (!groups.length && dimensions.length) {
    const byKind: Record<DimensionKind, string[]> = { data: [], search: [] }
    for (const d of dimensions) byKind[d.kind].push(d.key)
    groups = (['data', 'search'] as DimensionKind[])
      .filter((k) => byKind[k].length)
      .map((k) => ({ name: DIMENSION_KIND_LABELS[k], keys: byKind[k] }))
  }

  const defaults: string[] = Array.isArray(raw?.defaults)
    ? (raw.defaults as string[]).filter((k) => known.has(k))
    : dimensions.filter((d) => d.default_on).map((d) => d.key)

  return { dimensions, groups, defaults }
}

/**
 * GET /api/prediction/dimensions — 维度注册表（26 维）。
 *
 * ★ 维度解释文案（Dimension.desc）是**后端单一真相源**，前端不再写一份。
 * market 传入时后端只返回该市场适用的维度（A 股不会返回 sec_filings）。
 */
export async function getPredictionDimensions(
  market?: PredictionMarket,
  adminKey?: string,
): Promise<DimensionCatalog> {
  const params = market ? `?market=${encodeURIComponent(market)}` : ''
  const headers: Record<string, string> = {}
  if (adminKey) headers['X-Admin-Key'] = adminKey
  const res = await fetchWithAuth(`${API_BASE}/api/prediction/dimensions${params}`, { headers })
  if (!res.ok) throw new Error(`获取预测维度失败: ${res.status}`)
  return normalizeDimensionCatalog(await res.json())
}

/**
 * POST /api/prediction/{id}/pause — 暂停预测。
 *
 * 语义（用户拍板，UI 必须如实说明）：**取消在途任务 + 保留已完成维度 + 可续跑剩余**。
 * 不是"冻结"——后端无 checkpointer，做不到真冻结；已跑到一半的那个维度会丢，
 * resume 时重跑。返回 status=paused（**非终态**）。
 */
export async function pausePrediction(
  predictionId: string,
  adminKey: string,
): Promise<{ ok: boolean; status: string }> {
  const res = await fetchWithAuth(`${API_BASE}/api/prediction/${predictionId}/pause`, {
    method: 'POST',
    headers: { 'X-Admin-Key': adminKey },
  })
  if (!res.ok) {
    const error = await res.json().catch(() => ({}))
    throw new Error(error.detail || `暂停失败 (HTTP ${res.status})`)
  }
  return res.json()
}

/**
 * POST /api/prediction/{id}/resume — 续跑（只跑未完成的维度）。
 *
 * 返回后状态回到 queued/news_searching/inferring 之一 —— 调用方拿到后
 * **必须重新 streamPrediction**（暂停时旧流已终结）。游标传已收到的 lastEventId，
 * 避免全量重放。
 */
export async function resumePrediction(
  predictionId: string,
  adminKey: string,
): Promise<{ ok: boolean; status: string }> {
  const res = await fetchWithAuth(`${API_BASE}/api/prediction/${predictionId}/resume`, {
    method: 'POST',
    headers: { 'X-Admin-Key': adminKey },
  })
  if (!res.ok) {
    const error = await res.json().catch(() => ({}))
    throw new Error(error.detail || `继续预测失败 (HTTP ${res.status})`)
  }
  return res.json()
}

/** DELETE /api/prediction/{id} — 删除预测记录（连带 event_log） */
export async function deletePrediction(
  predictionId: string,
  adminKey: string,
): Promise<{ ok: boolean }> {
  const res = await fetchWithAuth(`${API_BASE}/api/prediction/${predictionId}`, {
    method: 'DELETE',
    headers: { 'X-Admin-Key': adminKey },
  })
  if (!res.ok) {
    const error = await res.json().catch(() => ({}))
    throw new Error(error.detail || `删除失败 (HTTP ${res.status})`)
  }
  return res.json().catch(() => ({ ok: true }))
}

/** GET /api/prediction/records — 当前用户的预测记录列表 */
export async function fetchPredictionRecords(
  market?: 'cn_a' | 'us_stock',
  adminKey?: string,
): Promise<PredictionRecord[]> {
  const params: Record<string, string> = {}
  if (market) params.market = market
  const headers: Record<string, string> = {}
  if (adminKey) headers['X-Admin-Key'] = adminKey
  const res = await fetchWithAuth(
    `${API_BASE}/api/prediction/records${Object.keys(params).length ? '?' + new URLSearchParams(params).toString() : ''}`,
    { headers },
  )
  if (!res.ok) throw new Error(`获取预测记录失败: ${res.status}`)
  const data = await res.json()
  return data.records || data || []
}

/** GET /api/prediction/active — 检查是否有进行中的预测会话（刷新恢复用） */
export async function fetchActivePrediction(
  market?: 'cn_a' | 'us_stock',
  adminKey?: string,
): Promise<{ active: boolean; prediction_id?: string; status?: string }> {
  const params: Record<string, string> = {}
  if (market) params.market = market
  const headers: Record<string, string> = {}
  if (adminKey) headers['X-Admin-Key'] = adminKey
  const res = await fetchWithAuth(
    `${API_BASE}/api/prediction/active${Object.keys(params).length ? '?' + new URLSearchParams(params).toString() : ''}`,
    { headers },
  )
  if (!res.ok) return { active: false }
  return res.json()
}

/** GET /api/prediction/{id} — 预测快照完整状态（full-state replay） */
export async function fetchPredictionSnapshot(
  id: string,
  adminKey?: string,
): Promise<PredictionSnapshot> {
  const headers: Record<string, string> = {}
  if (adminKey) headers['X-Admin-Key'] = adminKey
  const res = await fetchWithAuth(`${API_BASE}/api/prediction/${id}`, { headers })
  if (!res.ok) throw new Error(`获取预测快照失败: ${res.status}`)
  return res.json()
}

/** 流中断的原因（都不是"预测失败"，绝不能报成预测失败） */
export type PredictionInterruptReason =
  | 'network'        // fetch 抛错 / 连接被重置（断网、后端重启）
  | 'server_closed'  // 服务端主动关闭流，但没发 done/error（进程被杀 / 反代超时）
  | 'stalled'        // 连续 N 秒零字节（含 ping 都没有）—— 心跳都停了，链路已死

/** 中断详情，交给 UI 显示"连接已断开 / 正在重连"，而不是"预测失败" */
export interface PredictionInterruption {
  reason: PredictionInterruptReason
  /** 中文可读文案，可直接上屏 */
  message: string
  /** 已尝试的重连次数 */
  attempts: number
  /** 已收到的最大 event id —— 调用方可拿它手动重连，不丢进度 */
  lastEventId: number
  /** 是否还能自动/手动恢复（后台图仍在跑，重连即可续上；false = 已放弃） */
  recoverable: boolean
}

export interface StreamPredictionHandlers {
  /** 状态机转换（queued/news_searching/inferring/done/failed/stopped） */
  onProgress?: (status: string) => void
  /** 细粒度阶段进度（契约 A/F：preflight/资讯维度 i/n），填补长时间静默 */
  onPhaseProgress?: (progress: PredictionProgress) => void
  /** 推理过程 token（reasoning phase） */
  onThinking?: (text: string) => void
  /** 内容增量 token（content phase，同样展示在思考区） */
  onContent?: (text: string) => void
  /** 资讯维度结果流入 */
  onNews?: (item: NewsItem | NewsDimension) => void
  /** 预测数值结果（方向/幅度/置信区间/拐点） */
  onForecast?: (forecast: Forecast) => void
  /** 拐点标注 */
  onInflection?: (points: Array<{ date: string; price: number; type: string }>) => void

  // ── v2 新增事件（后端 fsm/sse_events.py 枚举）──────────────────────────────
  /**
   * 取数工具调用轨迹。每个数据型维度 2 条（phase=start / end），
   * 渲染成终端风格滚动日志：「stock_announcements(symbol=…) … 388 条 / 1.9s」。
   */
  onToolTrace?: (trace: ToolTrace) => void
  /**
   * 数据型维度结果（公告/定增/减持/SEC filing）—— 与 onNews 平级。
   * ★ 卡片必须显示 item.source + item.confidence + 维度的 source_note（用户拍板：诚实标注口径）。
   */
  onDataItem?: (result: FilingDimensionResult) => void
  /** 单条公式推导（边算边推：ARIMA / GARCH / 事件分 / GBM …） */
  onFormulaStep?: (step: FormulaStep) => void
  /** 逐日预测蜡烛（horizon=7 → 7 条，K 线一根根长出来）。同 key(day) 重放需幂等 */
  onForecastDay?: (candle: ForecastCandle) => void
  /** K 线上的公告事件打点（爆雷/定增/减持） */
  onEventMarker?: (marker: EventMarker) => void
  /**
   * 已暂停。**非终态**（可 resume），但对这条流是终点：
   * 后台图已被 cancel，不会再有事件 —— 所以流内部不再重连（重连只会挂死流）。
   * UI 应显示"已暂停 / 继续"，而不是"连接断开"。
   */
  onPaused?: (status?: string) => void
  /** 流结束，后端已落库 */
  onDone: (snapshot: PredictionSnapshot | null) => void
  /**
   * 预测本身失败（后端 error 事件）。kind 来自 MarketDataError.kind，
   * 用于区分"数据源限流/网络不通"和"算错了"。
   * 注意：网络断线不会走这里 —— 走 onInterrupted。
   */
  onError: (msg: string, kind?: MarketDataErrorKind) => void
  /** 用户主动停止（AbortSignal）或后端 stopped 事件 */
  onStopped?: () => void
  /** 每条事件的 event_log row id（后端在每条 yield 事件里带 id 字段） */
  onEventId?: (id: number) => void
  /**
   * 流意外终止（没收到 done/error/stopped）。后台图仍在跑，
   * UI 应显示"连接已断开，正在重连"而非"预测失败"。
   * 未提供时降级为 onError(网络措辞的 message)。
   */
  onInterrupted?: (info: PredictionInterruption) => void
  /** 即将发起第 attempt 次重连（用于显示"正在重连 2/5…"） */
  onReconnect?: (attempt: number, maxAttempts: number, delayMs: number) => void
  /** 停滞：连续 silentMs 毫秒没收到任何字节（含 ping）。之后会自动重连 */
  onStalled?: (silentMs: number) => void
}

export interface StreamPredictionOptions {
  /** 停滞判定阈值（毫秒）。后端心跳 3s 一次，30s 零字节即视为链路已死 */
  stallTimeoutMs?: number
  /** 最多自动重连次数（超出后走 onInterrupted，recoverable=true 允许用户手动重连） */
  maxReconnectAttempts?: number
  /** 指数退避基数（毫秒）：1s / 2s / 4s / 8s / 16s，上限 30s */
  reconnectBaseDelayMs?: number
}

const PREDICTION_STALL_TIMEOUT_MS = 30_000
const PREDICTION_MAX_RECONNECT = 5
const PREDICTION_RECONNECT_BASE_MS = 1_000
const PREDICTION_RECONNECT_MAX_MS = 30_000

/** 可恢复的中断 → 中文文案。明确说"网络/连接"，绝不说"预测失败"。 */
const INTERRUPT_MESSAGES: Record<PredictionInterruptReason, string> = {
  network: '与服务器的连接已断开（网络异常）。预测仍在后台运行，重连后会继续。',
  server_closed: '服务器提前关闭了连接（可能正在重启）。预测仍在后台运行，重连后会继续。',
  stalled: '连接已停止响应（超过 30 秒无任何数据，含心跳）。预测可能仍在后台运行，正在尝试重连。',
}

/** 可被 AbortSignal 打断的 sleep */
function abortableSleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal?.aborted) return resolve()
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    function onAbort() {
      clearTimeout(timer)
      resolve()
    }
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

/**
 * 流式订阅预测。单条 SSE 流贯穿 preflight→news_searching→inferring→done 全阶段。
 *
 * afterEventId：resume 游标。后端 GET /stream?after_event_id=X 会先回放
 * event_log 中 id>X 的已落库事件，再继续 drain 实时流。刷新/重进页面时
 * 传 0 → 全量回放 + 续流。断线自动重连时，用「已收到的最大 ev.id」作为游标续传，
 * 不再全量重放 4000+ 条 thinking。
 *
 * 事件分发（事件值匹配后端 SSEEventType 枚举值）：
 *   'prediction_progress' → onProgress(ev.status) 和/或 onPhaseProgress({phase,label,index,total,detail})
 *   'thinking'            → onThinking(ev.text)   (reasoning phase)
 *   'content'             → onContent(ev.text)     (content phase，同样展示在思考区)
 *   'news_item'           → onNews(ev)
 *   'forecast'            → onForecast(ev.forecast)
 *   'inflection'          → onInflection(ev.points)
 *   'tool_trace'          → onToolTrace(ev)      (v2：取数工具调用轨迹 start/end)
 *   'data_item'           → onDataItem(ev)       (v2：数据型维度结果，与 news_item 平级)
 *   'formula_step'        → onFormulaStep(ev)    (v2：公式推导，边算边推)
 *   'forecast_day'        → onForecastDay(ev)    (v2：逐日预测蜡烛 ×horizon)
 *   'event_marker'        → onEventMarker(ev)    (v2：K 线公告打点)
 *   'paused'              → onPaused()           (v2：非终态，但这条流到此为止，不重连)
 *   'ping'                → 只刷新停滞计时器，不渲染（心跳不进 event_log，无 id）
 *   'done' / 'stopped'    → onDone / onStopped（终态，不再重连）
 *   'error'               → onError(ev.message, ev.error_kind)（终态，不再重连）
 *
 * v2 事件全部走**已有的** eid 游标 + 停滞检测 + 重连骨架 —— 只在 switch 里加分支，
 * 不重写 parser。重放（afterEventId=0）时它们会按 event_log 顺序原样重发，
 * 所以回调实现必须幂等（如 forecast_day 按 day 覆盖而不是 push 重复）。
 *
 * 三种"没收到终态就结束"的情况全部走 onInterrupted（网络问题），并自动按
 * 指数退避重连；只有耗尽重试次数才把 recoverable 交回给 UI。
 * 用户主动 abort 走 onStopped。
 *
 * 注意：预测模块用 'thinking' 和 'content'（不是 'delta'），不沿用 quant 的违规。
 */
export async function streamPrediction(
  predictionId: string,
  adminKey: string,
  handlers: StreamPredictionHandlers,
  signal?: AbortSignal,
  afterEventId: number = 0,
  options: StreamPredictionOptions = {},
): Promise<void> {
  const stallTimeoutMs = options.stallTimeoutMs ?? PREDICTION_STALL_TIMEOUT_MS
  const maxAttempts = options.maxReconnectAttempts ?? PREDICTION_MAX_RECONNECT
  const baseDelayMs = options.reconnectBaseDelayMs ?? PREDICTION_RECONNECT_BASE_MS

  /** resume 游标：已收到的最大 event_log id。重连时从这里续，避免全量重放 */
  let cursor = afterEventId
  /** 终态标记：收到 done/error/stopped 之一就不再重连 */
  let terminal = false
  let attempts = 0

  /** 跑一次连接。返回中断原因；返回 null 表示已到达终态。 */
  async function runOnce(): Promise<PredictionInterruptReason | null> {
    const url =
      `${API_BASE}/api/prediction/${predictionId}/stream` +
      `?after_event_id=${cursor}`

    let res: Response
    try {
      res = await fetchWithAuth(url, {
        signal,
        headers: { 'X-Admin-Key': adminKey },
      })
    } catch (e: any) {
      if (signal?.aborted) return null
      // fetch 抛错 = 连不上（断网 / 后端没起来），可重试
      console.warn('[prediction SSE] 连接失败，将重试', e?.message)
      return 'network'
    }

    if (!res.ok) {
      // 5xx / 502 / 504 = 服务端暂时不可用（重启中、网关超时）→ 当作网络问题重连
      if (res.status >= 500) return 'network'
      // 4xx = 鉴权/不存在，重连没有意义 → 终态错误
      terminal = true
      handlers.onError(`预测流请求失败 (HTTP ${res.status})`)
      return null
    }

    const reader = res.body?.getReader()
    if (!reader) {
      terminal = true
      handlers.onError('浏览器不支持流式读取')
      return null
    }

    const decoder = new TextDecoder()
    let buffer = ''
    let lastDataTime = Date.now()
    let stalled = false

    // 独立计时器检测停滞，不干扰 reader.read() 的 await。
    // 后端每 3s 一个 ping；连 ping 都收不到 = socket 已死（这正是 198s 空洞的症状）。
    // 轮询间隔取 min(5s, 阈值/2)，保证小阈值也能被及时发现（默认 30s 阈值 → 5s，与聊天一致）。
    const pollMs = Math.max(50, Math.min(5_000, Math.floor(stallTimeoutMs / 2)))
    const stallTimer = setInterval(() => {
      const silent = Date.now() - lastDataTime
      if (silent > stallTimeoutMs) {
        stalled = true
        try { handlers.onStalled?.(silent) } catch (e) { console.error('[prediction SSE] onStalled error', e) }
        console.warn(`[prediction SSE] ${Math.round(silent / 1000)}s 无数据（含心跳），主动断开重连`)
        reader.cancel().catch(() => {})
      }
    }, pollMs)

    function processLine(line: string) {
      if (!line.startsWith('data: ')) return
      let ev: any
      try {
        ev = JSON.parse(line.slice(6))
      } catch {
        return  // 半行 / 非 JSON，忽略
      }

      // 心跳：只刷新停滞计时器，不渲染、不推游标（ping 不入 event_log，故无 id）
      if (ev.event === 'ping' || ev.ping) {
        lastDataTime = Date.now()
        return
      }

      // resume 游标：后端在每条落库事件里带 event_log 的自增 id
      if (typeof ev.id === 'number') {
        if (ev.id > cursor) cursor = ev.id
        handlers.onEventId?.(ev.id)
      }

      switch (ev.event) {
        case 'prediction_progress':
          // 两种 payload 可能同时出现：状态机转换（status）和细粒度阶段（phase）
          if (typeof ev.status === 'string') handlers.onProgress?.(ev.status)
          if (typeof ev.phase === 'string') {
            handlers.onPhaseProgress?.({
              phase: ev.phase as PredictionProgress['phase'],
              label: typeof ev.label === 'string' ? ev.label : undefined,
              index: typeof ev.index === 'number' ? ev.index : undefined,
              total: typeof ev.total === 'number' ? ev.total : undefined,
              detail: typeof ev.detail === 'string' ? ev.detail : undefined,
              updated_at: typeof ev.updated_at === 'number' ? ev.updated_at : undefined,
            })
          }
          break
        case 'thinking':
          if (typeof ev.text === 'string') handlers.onThinking?.(ev.text)
          break
        case 'content':
          if (typeof ev.text === 'string') handlers.onContent?.(ev.text)
          break
        case 'news_item':
          handlers.onNews?.(ev)
          break
        case 'forecast':
          if (ev.forecast) handlers.onForecast?.(ev.forecast as Forecast)
          break
        case 'inflection':
          if (ev.points) handlers.onInflection?.(ev.points)
          break

        // ── v2 事件（payload 与 news_item 同构：结构体字段被摊平进事件体）──────
        // 容忍两种形态：字段摊平 或 包一层（ev.trace / ev.result / ev.step / ev.candle / ev.marker）。
        case 'tool_trace': {
          const t = (ev.trace ?? ev) as any
          if (typeof t.tool !== 'string' || !t.tool) break
          handlers.onToolTrace?.({
            phase: t.phase === 'end' ? 'end' : 'start',
            dimension: String(t.dimension ?? ''),
            tool: t.tool,
            args: (t.args && typeof t.args === 'object') ? t.args : undefined,
            rows: typeof t.rows === 'number' ? t.rows : undefined,
            elapsed_ms: typeof t.elapsed_ms === 'number' ? t.elapsed_ms : undefined,
            summary: typeof t.summary === 'string' ? t.summary : undefined,
            error: typeof t.error === 'string' && t.error ? t.error : undefined,
            ts: typeof t.ts === 'number' ? t.ts : undefined,
          })
          break
        }
        case 'data_item': {
          const d = (ev.result ?? ev.item ?? ev) as any
          if (typeof d.dimension !== 'string' || !d.dimension) break
          handlers.onDataItem?.({
            ...d,
            items: Array.isArray(d.items) ? d.items : [],
            score: typeof d.score === 'number' ? d.score : 0,
          } as FilingDimensionResult)
          break
        }
        case 'formula_step': {
          const s = (ev.step ?? ev) as any
          if (typeof s.key !== 'string' || !s.key) break
          handlers.onFormulaStep?.(s as FormulaStep)
          break
        }
        case 'forecast_day': {
          const c = (ev.candle ?? ev) as any
          // day + OHLC 是刚需；缺一根就不画（宁可少画，不画错）
          if (typeof c.day !== 'number' || typeof c.c !== 'number') break
          handlers.onForecastDay?.({
            day: c.day,
            date: String(c.date ?? ''),
            o: Number(c.o ?? c.c),
            h: Number(c.h ?? c.c),
            l: Number(c.l ?? c.c),
            c: Number(c.c),
            p10: typeof c.p10 === 'number' ? c.p10 : undefined,
            p50: typeof c.p50 === 'number' ? c.p50 : undefined,
            p90: typeof c.p90 === 'number' ? c.p90 : undefined,
          })
          break
        }
        case 'event_marker': {
          const m = (ev.marker ?? ev) as any
          if (typeof m.date !== 'string' || !m.date) break
          handlers.onEventMarker?.({
            date: m.date,
            event_type: String(m.event_type ?? ''),
            title: typeof m.title === 'string' ? m.title : undefined,
            score: typeof m.score === 'number' ? m.score : undefined,
            dimension: typeof m.dimension === 'string' ? m.dimension : undefined,
          })
          break
        }
        case 'paused':
          // ★ 对流而言是终点（后台图已 cancel，不会再有事件）→ terminal=true 阻止重连，
          //   否则会走 server_closed → 退避重连 5 次 → 谎报"连接已断开"。
          //   但对状态机**不是终态**：status=paused，用户点"继续"可 resume 续跑。
          terminal = true
          try { handlers.onPaused?.(typeof ev.status === 'string' ? ev.status : 'paused') }
          catch (e) { console.error('[prediction SSE] onPaused error', e) }
          break

        case 'done':
          terminal = true
          try { handlers.onDone((ev.snapshot as PredictionSnapshot | null) ?? null) }
          catch (e) { console.error('[prediction SSE] onDone error', e) }
          break
        case 'stopped':
          terminal = true
          try { handlers.onStopped?.() } catch (e) { console.error('[prediction SSE] onStopped error', e) }
          break
        case 'error':
          terminal = true
          try {
            handlers.onError(
              String(ev.message || '预测失败'),
              typeof ev.error_kind === 'string' ? (ev.error_kind as MarketDataErrorKind) : undefined,
            )
          } catch (e) { console.error('[prediction SSE] onError error', e) }
          break
        default:
          // 未知事件类型静默忽略（前向兼容）
          break
      }
    }

    try {
      while (true) {
        let done: boolean
        let value: Uint8Array | undefined
        try {
          const result = await reader.read()
          done = result.done
          value = result.value
        } catch (e: any) {
          // 读取中途抛错 = 连接被重置
          if (signal?.aborted) return null
          console.warn('[prediction SSE] 读取中断', e?.message)
          return stalled ? 'stalled' : 'network'
        }
        if (done) break

        lastDataTime = Date.now()
        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() ?? ''
        for (const line of lines) processLine(line)
        if (terminal) return null   // 终态：立刻收工，不再读残余
      }
      // 处理残留 buffer（最后一个事件可能没以 \n 结尾）
      if (buffer.trim()) processLine(buffer.trim())
    } finally {
      clearInterval(stallTimer)
    }

    if (terminal) return null
    if (signal?.aborted) return null

    // 流正常结束（done=true）但没收到 done/error/stopped —— 服务端提前关流
    return stalled ? 'stalled' : 'server_closed'
  }

  while (true) {
    const reason = await runOnce()

    if (reason === null) {
      // 到达终态，或用户 abort
      if (!terminal && signal?.aborted) handlers.onStopped?.()
      return
    }

    // 用户在这期间点了停止
    if (signal?.aborted) {
      handlers.onStopped?.()
      return
    }

    if (attempts >= maxAttempts) {
      // 重试耗尽：交回给 UI。recoverable=true —— 后台图还在跑，
      // 用户可以拿 lastEventId 手动重连，绝不能显示成"预测失败"。
      const info: PredictionInterruption = {
        reason,
        message: `${INTERRUPT_MESSAGES[reason]}（已重试 ${attempts} 次仍未恢复，可手动重连）`,
        attempts,
        lastEventId: cursor,
        recoverable: true,
      }
      if (handlers.onInterrupted) {
        handlers.onInterrupted(info)
      } else {
        // 降级：没实现 onInterrupted 也必须让用户看到"这是网络问题"
        handlers.onError(info.message)
      }
      return
    }

    attempts += 1
    const delayMs = Math.min(
      baseDelayMs * 2 ** (attempts - 1),
      PREDICTION_RECONNECT_MAX_MS,
    )

    // 先告诉 UI"连接断了，正在重连"（不是失败），再退避
    handlers.onInterrupted?.({
      reason,
      message: INTERRUPT_MESSAGES[reason],
      attempts,
      lastEventId: cursor,
      recoverable: true,
    })
    try { handlers.onReconnect?.(attempts, maxAttempts, delayMs) }
    catch (e) { console.error('[prediction SSE] onReconnect error', e) }

    await abortableSleep(delayMs, signal)
    if (signal?.aborted) {
      handlers.onStopped?.()
      return
    }
    // 下一轮 runOnce 会用推进后的 cursor 续传，不全量重放
  }
}

// ── LLM 供应商管理（管理面板）API ────────────────────────────────────────────
// 全部需 X-Admin-Key 头，无 key → 403；错误体为 FastAPI 默认 {detail}。
// post/put 帮助函数不支持自定义 header，因此照 refreshQuantCache/startPrediction
// 的惯例直接走 fetchWithAuth。

export interface LlmProvider {
  id: number
  name: string
  base_url: string
  api_key: string
  chat_model: string
  summary_model: string | null
  router_model: string | null
  vision_model: string | null
  extra_models: string[]
  remark: string | null
  enabled: boolean
  is_active: boolean
  created_at: number
  updated_at: number
}

/** 当前全局生效配置：source=db 表示数据库供应商覆盖，env 表示 .env 默认 */
export interface LlmProviderEffective {
  source: 'db' | 'env'
  base_url: string
  chat_model: string
  models: string[]
}

export interface LlmProviderPayload {
  name: string
  base_url: string
  api_key: string
  chat_model: string
  summary_model?: string | null
  router_model?: string | null
  vision_model?: string | null
  extra_models?: string[]
  remark?: string | null
  enabled?: boolean
}

export interface LlmProviderTestResult {
  ok: boolean
  latency_ms: number
  content: string
  error: string | null
}

/** 管理端点统一错误：带出后端 detail 文本，并附 status 供 UI 区分 403（密钥失效） */
async function llmAdminError(res: Response, fallback: string): Promise<never> {
  const data = await res.json().catch(() => ({}))
  const err = new Error(data.detail || `${fallback} (HTTP ${res.status})`) as Error & { status?: number }
  err.status = res.status
  throw err
}

/** GET /api/admin/llm-providers — 供应商列表 + 当前生效配置 */
export async function fetchLlmProviders(adminKey: string): Promise<{ providers: LlmProvider[]; effective: LlmProviderEffective }> {
  const res = await fetchWithAuth(`${API_BASE}/api/admin/llm-providers`, { headers: { 'X-Admin-Key': adminKey } })
  if (!res.ok) return llmAdminError(res, '获取供应商列表失败')
  return res.json()
}

/** POST /api/admin/llm-providers — 新增供应商（name 重复 → 409） */
export async function createLlmProvider(payload: LlmProviderPayload, adminKey: string): Promise<{ provider: LlmProvider }> {
  const res = await fetchWithAuth(`${API_BASE}/api/admin/llm-providers`, {
    method: 'POST',
    body: JSON.stringify(payload),
    headers: { 'X-Admin-Key': adminKey },
  })
  if (!res.ok) return llmAdminError(res, '新增供应商失败')
  return res.json()
}

/** PUT /api/admin/llm-providers/{id} — 全量更新 */
export async function updateLlmProvider(id: number, payload: LlmProviderPayload, adminKey: string): Promise<{ provider: LlmProvider }> {
  const res = await fetchWithAuth(`${API_BASE}/api/admin/llm-providers/${id}`, {
    method: 'PUT',
    body: JSON.stringify(payload),
    headers: { 'X-Admin-Key': adminKey },
  })
  if (!res.ok) return llmAdminError(res, '更新供应商失败')
  return res.json()
}

/** DELETE /api/admin/llm-providers/{id} */
export async function deleteLlmProvider(id: number, adminKey: string): Promise<{ ok: boolean }> {
  const res = await fetchWithAuth(`${API_BASE}/api/admin/llm-providers/${id}`, {
    method: 'DELETE',
    headers: { 'X-Admin-Key': adminKey },
  })
  if (!res.ok) return llmAdminError(res, '删除供应商失败')
  return res.json()
}

/** POST /api/admin/llm-providers/{id}/activate — 设为全局当前，所有用户立即生效 */
export async function activateLlmProvider(id: number, adminKey: string): Promise<{ provider: LlmProvider }> {
  const res = await fetchWithAuth(`${API_BASE}/api/admin/llm-providers/${id}/activate`, {
    method: 'POST',
    headers: { 'X-Admin-Key': adminKey },
  })
  if (!res.ok) return llmAdminError(res, '切换供应商失败')
  return res.json()
}

/** POST /api/admin/llm-providers/deactivate — 恢复 .env 默认配置 */
export async function deactivateLlmProvider(adminKey: string): Promise<{ ok: boolean }> {
  const res = await fetchWithAuth(`${API_BASE}/api/admin/llm-providers/deactivate`, {
    method: 'POST',
    headers: { 'X-Admin-Key': adminKey },
  })
  if (!res.ok) return llmAdminError(res, '恢复默认配置失败')
  return res.json()
}

/** POST /api/admin/llm-providers/test — 连通性测试（传 {id} 用已存配置，或现场 {base_url, api_key, model}） */
export async function testLlmProvider(
  body: { id?: number; base_url?: string; api_key?: string; model?: string },
  adminKey: string,
): Promise<LlmProviderTestResult> {
  const res = await fetchWithAuth(`${API_BASE}/api/admin/llm-providers/test`, {
    method: 'POST',
    body: JSON.stringify(body),
    headers: { 'X-Admin-Key': adminKey },
  })
  if (!res.ok) return llmAdminError(res, '测试连接失败')
  return res.json()
}

/** POST /api/admin/llm-providers/fetch-models — 拉供应商模型列表（失败时后端抛 502 + detail） */
export async function fetchLlmProviderModels(baseUrl: string, apiKey: string, adminKey: string): Promise<{ models: string[] }> {
  const res = await fetchWithAuth(`${API_BASE}/api/admin/llm-providers/fetch-models`, {
    method: 'POST',
    body: JSON.stringify({ base_url: baseUrl, api_key: apiKey }),
    headers: { 'X-Admin-Key': adminKey },
  })
  if (!res.ok) return llmAdminError(res, '获取模型列表失败')
  return res.json()
}