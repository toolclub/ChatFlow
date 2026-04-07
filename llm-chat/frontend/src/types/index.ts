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
  results?: SearchResultItem[]
  searchItems?: SearchItem[]
  fetchStatus?: 'loading' | 'done' | 'fail'
  done: boolean
}

export interface PlanStep {
  id: string
  title: string
  description: string
  status: 'pending' | 'running' | 'done' | 'failed'
  result?: string
}

export interface StepRecord {
  index: number
  title: string
  status: 'pending' | 'running' | 'done' | 'failed'
  toolCalls: ToolCallRecord[]
  thinking: string
  content: string
}

// ── 澄清问询卡片 ──────────────────────────────────────────────────────────────

export interface ClarificationItem {
  id: string
  type: 'single_choice' | 'multi_choice' | 'text'
  label: string
  options?: string[]       // single_choice / multi_choice 有此字段
  placeholder?: string     // text 有此字段
}

export interface ClarificationData {
  question: string
  items: ClarificationItem[]
}

export interface Message {
  role: 'user' | 'assistant'
  content: string
  thinking?: string
  steps?: StepRecord[]
  images?: string[]
  timestamp?: number
  toolCalls?: ToolCallRecord[]
  workflowPlan?: PlanStep[]
  workflowGoal?: string
  clarification?: ClarificationData   // 模型需要澄清时附带的交互卡片数据
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
  agentMode: boolean
  forcePlan?: PlanStep[]    // 用户编辑后的强制计划（跳过 planner LLM 规划）
}

export interface AgentStatus {
  state: 'idle' | 'vision_analyze' | 'routing' | 'planning' | 'thinking' | 'tool' | 'reflecting' | 'saving' | 'done'
  model: string
  tool?: string
  intent?: string
}

export interface TraceEntry {
  type: 'tool_call' | 'tool_result' | 'reflection' | 'step_start' | 'search_item' | 'info'
  content: string
  toolName?: string
  timestamp: number
}

export interface ToolHistoryEvent {
  id: number
  tool_name: string
  tool_input: Record<string, unknown>
  created_at: number
}

export interface CognitiveState {
  plan: PlanStep[]
  currentStepIndex: number
  reflection: string
  reflectorDecision: string
  traceLog: TraceEntry[]
  isActive: boolean
  historyEvents: ToolHistoryEvent[]
  artifacts: FileArtifact[]           // 文件产物（sandbox_write 生成的文件）
}

// ── 文件产物（沙箱生成的文件） ─────────────────────────────────────────────────
export interface FileArtifact {
  name: string        // 文件名 e.g. "baidu_tech.html"
  path: string        // 完整路径 e.g. "/sandbox/baidu_tech.html"
  content: string     // 文件内容
  language: string    // 语言标记 e.g. "html", "python"
}

/** 从文件路径推断语言 */
export function detectLanguage(path: string): string {
  const ext = path.split('.').pop()?.toLowerCase() ?? ''
  const map: Record<string, string> = {
    html: 'html', htm: 'html', svg: 'svg', css: 'css',
    js: 'javascript', mjs: 'javascript', jsx: 'javascript',
    ts: 'typescript', tsx: 'typescript',
    py: 'python', rb: 'ruby', go: 'go', rs: 'rust',
    java: 'java', kt: 'kotlin', c: 'c', cpp: 'cpp', h: 'c',
    sh: 'shell', bash: 'shell', zsh: 'shell',
    json: 'json', yaml: 'yaml', yml: 'yaml', toml: 'toml',
    xml: 'xml', md: 'markdown', sql: 'sql', vue: 'vue',
    txt: 'text', csv: 'text', log: 'text',
  }
  return map[ext] || 'text'
}

/** 该语言是否可在 iframe 中预览 */
export function isPreviewable(lang: string): boolean {
  return ['html', 'svg'].includes(lang)
}

export function makeEmptyCognitiveState(): CognitiveState {
  return {
    plan: [],
    currentStepIndex: 0,
    reflection: '',
    reflectorDecision: '',
    traceLog: [],
    isActive: false,
    historyEvents: [],
    artifacts: [],
  }
}
