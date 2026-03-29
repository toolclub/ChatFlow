import type { PlanStep } from '../types'

const API_BASE = ''

// ── 浏览器唯一标识（localStorage 持久化，同浏览器同 origin 共享） ─────────────
function getClientId(): string {
  let id = localStorage.getItem('cf_client_id')
  if (!id) {
    id = crypto.randomUUID()
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

export async function stopStream(convId: string): Promise<void> {
  await fetch(`${API_BASE}/api/chat/${convId}/stop`, {
    method: 'POST',
    headers: { 'X-Client-ID': getClientId() },
  }).catch(() => {})
}

export async function sendMessage(
  conversationId: string,
  message: string,
  model: string,
  images: string[],
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
) {
  const body: Record<string, unknown> = {
    conversation_id: conversationId,
    message,
    model,
  }
  if (images.length > 0) {
    body.images = images.map(img => img.replace(/^data:image\/[a-z]+;base64,/, ''))
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

  let buffer = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break

    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() ?? ''

    for (const line of lines) {
      if (!line.startsWith('data: ')) continue
      try {
        const data = JSON.parse(line.slice(6))
        if (data.content)           onChunk(data.content)
        if (data.tool_call)         onToolCall(data.tool_call.name, data.tool_call.input)
        if (data.tool_result)       onToolResult(data.tool_result.name, data.tool_result)
        if (data.search_item)       onSearchItem(data.search_item)
        if (data.status)            onStatus(data.status, data.model)
        if (data.route)             onRoute(data.route.model, data.route.intent)
        if (data.plan_generated)    onPlanGenerated(data.plan_generated.steps)
        if (data.reflection)        onReflection(data.reflection.content, data.reflection.decision)
        if (data.done)              onDone()
        if (data.stopped)           onStopped()
      } catch {}
    }
  }
}
