export interface SearchResultItem {
  title: string
  url: string
  snippet: string
}

export interface SearchItem {
  url: string
  title: string
  status: 'loading' | 'done' | 'fail'
}

export interface ToolCallRecord {
  name: string
  input: Record<string, unknown>
  output?: string
  results?: SearchResultItem[]   // legacy (不再使用)
  searchItems?: SearchItem[]     // web_search 逐条实时追加
  fetchStatus?: 'loading' | 'done' | 'fail'  // fetch_webpage 状态
  done: boolean
}

export interface Message {
  role: 'user' | 'assistant'
  content: string
  images?: string[]  // base64 data URLs
  timestamp?: number
  toolCalls?: ToolCallRecord[]
}

export interface ConversationInfo {
  id: string
  title: string
  updated_at: number
}

export interface ConversationDetail {
  id: string
  title: string
  system_prompt: string
  messages: Message[]
  mid_term_summary: string
}

export interface SendPayload {
  text: string
  images: string[]
}

export interface AgentStatus {
  state: 'idle' | 'routing' | 'thinking' | 'tool' | 'done'
  model: string   // 当前工作模型
  tool?: string   // 当前调用的工具名
}
