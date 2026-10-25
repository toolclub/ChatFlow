/** Durable SSE transport. Reconnects only with GET; a lost POST is never resubmitted. */
export class StreamHttpError extends Error {
  constructor(public status: number, message: string) { super(message) }
}

export async function checkStreamResponse(response: Response): Promise<void> {
  if (response.ok && response.body) return
  const detail = await response.json().catch(() => ({}))
  throw new StreamHttpError(response.status, detail.detail || `连接失败 (${response.status})`)
}

function delay(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const abort = () => { clearTimeout(timer); reject(new DOMException('Aborted', 'AbortError')) }
    const timer = setTimeout(() => { signal?.removeEventListener('abort', abort); resolve() }, ms)
    if (signal?.aborted) abort()
    else signal?.addEventListener('abort', abort, { once: true })
  })
}

export async function consumeChatStream(options: {
  initial: () => Promise<Response>
  reconnect: (cursor: number, messageId: string) => Promise<Response>
  onEvent: (data: Record<string, any>) => void
  onConnection?: (reconnecting: boolean) => void
  signal?: AbortSignal
  cursor?: number
  messageId?: string
  retryDelayMs?: number
  idleTimeoutMs?: number
  maxRetries?: number
  initialRequestSafe?: boolean
}): Promise<'done' | 'stopped' | 'interrupted'> {
  let cursor = options.cursor || 0
  let messageId = options.messageId || ''
  let retries = 0
  let initial = true
  let interrupted = false
  while (!options.signal?.aborted) {
    try {
      const response = await (initial ? options.initial() : options.reconnect(cursor, messageId))
      initial = false
      await checkStreamResponse(response)
      messageId = response.headers.get('X-Message-ID') || messageId
      const reader = response.body!.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      let eventId = 0
      let dataLines: string[] = []
      let terminal: 'done' | 'stopped' | 'interrupted' | undefined
      let lastActivity = Date.now()
      const cancel = () => { void reader.cancel().catch(() => {}) }
      const timer = setInterval(() => {
        if (Date.now() - lastActivity > (options.idleTimeoutMs ?? 45_000)) cancel()
      }, Math.min(1000, options.idleTimeoutMs ?? 45_000))
      options.signal?.addEventListener('abort', cancel, { once: true })
      const dispatch = () => {
        if (!dataLines.length) { eventId = 0; return }
        const data = JSON.parse(dataLines.join('\n'))
        dataLines = []
        const id = Number(data.id || eventId || 0)
        eventId = 0
        if (id && id <= cursor) return
        messageId = data.message_id || messageId
        options.onEvent(data)
        if (id) cursor = id
        if (data.can_continue) interrupted = true
        if (data.done) terminal = interrupted ? 'interrupted' : 'done'
        if (data.stopped) terminal = 'stopped'
        retries = 0
        options.onConnection?.(false)
      }
      const line = (value: string) => {
        if (!value) dispatch()
        else if (value.startsWith('id:')) eventId = Number(value.slice(3).trim()) || 0
        else if (value.startsWith('data:')) dataLines.push(value.slice(5).replace(/^ /, ''))
      }
      try {
        while (!terminal && !options.signal?.aborted) {
          const chunk = await reader.read()
          if (chunk.done) break
          lastActivity = Date.now()
          buffer += decoder.decode(chunk.value, { stream: true })
          const lines = buffer.split('\n')
          buffer = lines.pop() || ''
          for (const value of lines) {
            line(value.replace(/\r$/, ''))
            if (terminal) break
          }
        }
        // Do not apply an incomplete event at EOF; replay it from its durable cursor.
      } finally {
        clearInterval(timer)
        options.signal?.removeEventListener('abort', cancel)
        await reader.cancel().catch(() => {})
        reader.releaseLock()
      }
      if (terminal) return terminal
    } catch (error) {
      if (options.signal?.aborted) return 'stopped'
      if (error instanceof StreamHttpError && error.status < 500 && error.status !== 429) throw error
      // Without an acknowledged round identity, retrying could attach to an older turn.
      if ((initial && !options.initialRequestSafe) || (!cursor && !messageId)) throw error
      initial = false
    }
    if (options.signal?.aborted) return 'stopped'
    if (!cursor && !messageId) throw new Error('连接中断，请重新打开对话确认本轮是否已开始')
    if (retries++ >= (options.maxRetries ?? 8)) throw new Error('连接恢复失败，已保留当前内容，请重新打开对话重试')
    options.onConnection?.(true)
    await delay(Math.min((options.retryDelayMs ?? 500) * 2 ** (retries - 1), 8000), options.signal)
  }
  return 'stopped'
}
