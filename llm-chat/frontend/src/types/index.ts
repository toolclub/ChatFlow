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

/** 渲染模式（后端 DISPLAY_MODE 协议，通过 SSE 事件传递） */
export type ToolDisplayMode = 'terminal' | 'file_write' | 'file_read' | 'default'

export interface ToolCallRecord {
  name: string
  input: Record<string, unknown>
  output?: string
  results?: SearchResultItem[]
  searchItems?: SearchItem[]
  fetchStatus?: 'loading' | 'done' | 'fail'
  done: boolean
  step_index?: number   // DB 字段：所属计划步骤索引（刷新恢复时用于分发到对应步骤）
  /** 渲染模式：由后端 SkillRegistry 声明，通过 SSE 协议传递 */
  displayMode?: ToolDisplayMode
}

export interface PlanStep {
  id: string
  title: string
  description: string
  status: 'pending' | 'running' | 'done' | 'failed'
  result?: string
}

/**
 * 结构化思考段（spec「模型思考流程」协议）
 *
 * 后端按 (node, step_index, phase) 三元组作为唯一 key 累积 delta。
 * 前端按相同规则分发到 message.thinkingSegments 或 step.thinkingSegments。
 */
export interface ThinkingSegment {
  node: string              // 来源节点：planner/route_model/call_model/call_model_after_tool/reflector/vision
  step_index: number | null // 有计划时的步骤索引；消息级段为 null
  phase: 'reasoning' | 'content'  // 推理链 vs 模型外显文本
  content: string           // 累积内容（不是 delta）
}

/** SSE 协议的 thinking 事件 payload（仅含增量 delta） */
export interface ThinkingEvent {
  node: string
  step_index: number | null
  phase: 'reasoning' | 'content'
  delta: string
}

export interface StepRecord {
  index: number
  title: string
  status: 'pending' | 'running' | 'done' | 'failed'
  toolCalls: ToolCallRecord[]
  thinking: string                        // 拼接纯文本（向后兼容，= thinkingSegments 内容拼接）
  thinkingSegments: ThinkingSegment[]     // 结构化段，按到达顺序
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

export interface UploadedFile {
  id: number
  name: string
  size: number
  path?: string
  language?: string
  mime?: string
}

export interface Message {
  role: 'user' | 'assistant'
  content: string
  intent?: string            // 胶囊意图标签（显示在气泡里），如"写代码 · 算法题"
  thinking?: string                         // 拼接纯文本（向后兼容）
  thinkingSegments?: ThinkingSegment[]      // 结构化段（权威数据源，优先渲染）
  steps?: StepRecord[]
  images?: string[]
  files?: UploadedFile[]               // 用户上传的文件（user 消息，source='uploaded'）
  timestamp?: number
  message_id?: string                  // DB 业务 ID（用于 plan.message_id 精确匹配）
  toolCalls?: ToolCallRecord[]
  artifacts?: FileArtifact[]           // 关联的产物元数据（DB 外键，刷新恢复用）
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
  files?: UploadedFile[]    // 用户已上传的文件（在发送前调用 /api/files/upload 得到 id）
  intent?: string           // API 前缀（如 [PPT:corp_blue] / [algo]），不出现在气泡
  intentLabel?: string      // 气泡里显示的意图标签（如"做 PPT · 企业蓝" / "写代码 · 算法题"）
  contextRefs?: {type: string, id: string}[]
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
  id?: number         // DB 主键（按需加载内容时用）
  name: string        // 文件名 e.g. "baidu_tech.html"
  path: string        // 完整路径 e.g. "/sandbox/baidu_tech.html"
  content: string     // 文件内容（PPTX 为 base64 编码）— 可为空（元数据模式）
  language: string    // 语言标记 e.g. "html", "python", "pptx"
  message_id?: string // 关联的 assistant 消息 ID
  binary?: boolean    // 是否为二进制文件（PPTX 等）
  downloadable?: boolean // 是否应显示下载入口（如 sandbox_download 产物）
  source?: string     // generated | uploaded
  size?: number       // 文件大小（字节）
  slide_count?: number // PPT 页数
  theme?: string      // PPT 主题名
  slides_html?: string[] // PPT 每页的 HTML 预览
  created_at?: number
}

/**
 * 从文件路径推断"语言"——这是 sandbox 工具产出物的协议标识，
 * 用于认知面板（CognitivePanel）渲染策略。**不是**用户上传预览的派发依据；
 * 上传预览走 src/preview/ 模块，按文件名后缀独立决策。
 */
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
    pptx: 'pptx', ppt: 'pptx', pdf: 'pdf',
    tar: 'archive', gz: 'archive', tgz: 'archive', zip: 'archive',
    jar: 'archive', war: 'archive', ear: 'archive',
  }
  return map[ext] || 'text'
}

/** 该语言是否可在 iframe 中预览（认知面板用） */
export function isPreviewable(lang: string): boolean {
  return ['html', 'svg'].includes(lang)
}

/** 该文件是否可下载（二进制文件，认知面板用） */
export function isDownloadable(lang: string): boolean {
  return ['pptx', 'pdf'].includes(lang)
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

// ── 量化模块类型 ─────────────────────────────────────────────────────────────

export interface QuantProviderInfo {
  name: string
  enabled: boolean
  priority: number
  capabilities: string[]
  health: 'ok' | 'degraded' | 'down'
  message?: string
}

export interface QuantScreenCriteria {
  market?: 'cn_a'
  universe?: 'all' | 'hs300' | 'zz500' | 'custom'
  custom_symbols?: string[]
  industries?: string[]
  min_market_cap?: number | null
  min_avg_turnover?: number | null
  pe_range?: [number, number] | null
  pb_range?: [number, number] | null
  roe_min?: number | null
  momentum_window?: number
  volatility_window?: number
  exclude_st?: boolean
  exclude_suspended?: boolean
  exclude_new_stocks_days?: number
  top_n?: number
  weights?: Record<string, number>
}

export interface QuantFactorScore {
  symbol: string
  name: string
  industry?: string
  technical: number
  fundamental: number
  liquidity: number
  risk: number
  total: number
  rank: number
  reasons: string[]
  warnings: string[]
  raw: Record<string, number | null>
}

export interface QuantProviderTrace {
  provider: string
  capability: string
  status: 'ok' | 'fallback' | 'error'
  elapsed_ms: number
  rows: number
  error?: string
}

export interface QuantScreenResult {
  snapshot_id: string
  criteria: QuantScreenCriteria
  rows: QuantFactorScore[]
  provider_trace: QuantProviderTrace[]
  weights: Record<string, number>
  universe_size: number
  as_of_date: string
  generated_at: number
  warnings: string[]
  /** LLM 流式分析结果（异步回填，初始为空） */
  analysis?: string
  /** LLM 提取的风险点（异步回填） */
  risk_notes?: string[]
}
