/** Shared authenticated transport for JSON, multipart and streaming requests. */
let refreshPromise: Promise<string> | null = null
let authGeneration = 0

export function setAccessToken(token: string | null): void {
  authGeneration++
  if (token) localStorage.setItem('cf_access_token', token)
  else localStorage.removeItem('cf_access_token')
}

export function getClientId(): string {
  let id = localStorage.getItem('cf_client_id')
  if (!id || id.length > 36) {
    id = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
          const r = Math.floor(Math.random() * 16)
          return (c === 'x' ? r : (r & 3) | 8).toString(16)
        })
    localStorage.setItem('cf_client_id', id)
  }
  return id
}

async function refreshAccessToken(): Promise<string> {
  if (refreshPromise) return refreshPromise
  const generation = authGeneration
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), 10_000)
  const pending = (async () => {
    const response = await fetch('/api/auth/token/refresh', {
      method: 'POST', credentials: 'include', signal: controller.signal,
    })
    if (!response.ok) {
      if (response.status === 401 && generation === authGeneration) setAccessToken(null)
      throw new Error('登录状态刷新失败')
    }
    const data = await response.json()
    if (typeof data.access_token !== 'string' || !data.access_token) throw new Error('无效的登录凭据')
    if (generation !== authGeneration) throw new Error('登录状态已改变')
    localStorage.setItem('cf_access_token', data.access_token)
    return data.access_token as string
  })()
  refreshPromise = pending
  try { return await pending }
  finally {
    clearTimeout(timeout)
    if (refreshPromise === pending) refreshPromise = null
  }
}

function abortable<T>(promise: Promise<T>, signal?: AbortSignal | null): Promise<T> {
  if (!signal) return promise
  return new Promise((resolve, reject) => {
    const abort = () => reject(new DOMException('Aborted', 'AbortError'))
    if (signal.aborted) { abort(); return }
    signal.addEventListener('abort', abort, { once: true })
    promise.then(resolve, reject).finally(() => signal.removeEventListener('abort', abort))
  })
}

export async function fetchWithAuth(url: string, options: RequestInit = {}): Promise<Response> {
  const generation = authGeneration
  const token = localStorage.getItem('cf_access_token')
  const headers = new Headers(options.headers)
  headers.set('X-Client-ID', getClientId())
  if (typeof options.body === 'string' && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const response = await fetch(url, { ...options, headers, credentials: 'include' })
  if (response.status !== 401 || generation !== authGeneration) return response

  try {
    // A delayed 401 can arrive after a different request already refreshed.
    const current = localStorage.getItem('cf_access_token')
    const next = current && current !== token ? current : await abortable(refreshAccessToken(), options.signal)
    if (options.signal?.aborted) throw new DOMException('Aborted', 'AbortError')
    if (generation !== authGeneration) return response
    headers.set('Authorization', `Bearer ${next}`)
    await response.body?.cancel()
    return await fetch(url, { ...options, headers, credentials: 'include' })
  } catch (error) {
    if (options.signal?.aborted) throw error
    return response
  }
}

export async function readJson<T = any>(response: Response): Promise<T> {
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = typeof body.detail === 'string' ? body.detail : `请求失败 (${response.status})`
    throw new Error(detail)
  }
  return body as T
}
