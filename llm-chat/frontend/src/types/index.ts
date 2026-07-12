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
  market?: 'cn_a' | 'us_stock'
  universe?: 'all' | 'hs300' | 'zz500' | 'nasdaq' | 'sp500' | 'custom'
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

// ── 用户认证与设置 ────────────────────────────────────────────────────────────

export interface User {
  id: string
  email: string
  name: string
  avatar_url: string
  bio?: string
  locale: string
  timezone: string
  is_active: boolean
  is_verified: boolean
  last_login_at: number
  created_at: number
}

export interface UserSettings {
  theme: 'light' | 'dark' | 'system'
  default_model: string
  agent_mode_default: boolean
  language: string
  notifications_enabled: boolean
  sidebar_collapsed: boolean
  custom_settings: Record<string, any>
}

export interface AuthSession {
  id: string
  device_info: string
  ip_address: string
  is_active: boolean
  created_at: number
  expires_at: number
}

// ── 股票预测模块类型 ─────────────────────────────────────────────────────────

/** 市场标识（对应后端 graph/prediction/domain.py Market） */
export type PredictionMarket = 'cn_a' | 'us_stock'

/**
 * 预测记录状态（对应后端 fsm/prediction.py PredictionStatus 枚举值）
 *
 * 七个成员必须与后端枚举一一对应 —— 漏一个会让 UI 回退到原始英文串、
 * tag 变灰、进度条塌成 0%。
 *
 * ★ paused 是 v2 新增，且**不是终态**（stopped 才是）。语义 = 取消在途任务 +
 *   保留已完成维度 + 可 resume 续跑剩余维度（后端无 checkpointer，做不到真冻结）。
 */
export type PredictionStatus =
  | 'queued'
  | 'news_searching'
  | 'inferring'
  | 'paused'
  | 'done'
  | 'failed'
  | 'stopped'

/** 状态 → 中文标签（唯一真相源；禁止各组件各写一份 stageMap） */
export const PREDICTION_STATUS_LABELS: Record<PredictionStatus, string> = {
  queued: '排队中',
  news_searching: '资讯搜索中',
  inferring: '模型推理中',
  paused: '已暂停',
  done: '已完成',
  failed: '失败',
  stopped: '已停止',
}

/**
 * 状态 → 进度条百分比（stopped/failed 保留已走到的进度，不塌成 0）。
 *
 * 这只是**没有细粒度 progress 时的兜底**：真实进度优先走 PredictionProgress。
 * paused 停在半路（既非 0 也非 100），取 50 只是让进度条别归零 —— 暂停时
 * 已完成的维度结果仍在库里，续跑只跑剩余维度。
 */
export const PREDICTION_STATUS_PERCENT: Record<PredictionStatus, number> = {
  queued: 5,
  news_searching: 35,
  inferring: 75,
  paused: 50,
  done: 100,
  failed: 100,
  stopped: 100,
}

/** 是否为终态（done/failed/stopped）—— 终态不应再显示 loading / 进度动画。
 *  ★ paused 不在此列：它可以 resume 回 queued 继续跑。 */
export const PREDICTION_TERMINAL_STATUSES: readonly PredictionStatus[] = [
  'done',
  'failed',
  'stopped',
] as const

export function isPredictionTerminal(status?: string | null): boolean {
  return !!status && (PREDICTION_TERMINAL_STATUSES as readonly string[]).includes(status)
}

/** 是否处于进行中（queued/news_searching/inferring）—— paused 不算「在跑」 */
export function isPredictionActive(status?: string | null): boolean {
  return status === 'queued' || status === 'news_searching' || status === 'inferring'
}

/** 已暂停（可续跑） */
export function isPredictionPaused(status?: string | null): boolean {
  return status === 'paused'
}

/**
 * 该状态下是否应该（重）连 SSE 流。
 *
 * ★ 别再用 `!isPredictionTerminal(status)` 判断要不要连流 —— paused 非终态，
 *   但后台图已经被 cancel 了，连上去只会挂一条永远收不到事件的死流（直到 30s
 *   停滞检测触发 → 指数退避重连 5 次 → 报「连接已断开」，全是假象）。
 *   暂停态要等用户点「继续」（resume → 状态回 queued）后再连。
 */
export function isPredictionStreamable(status?: string | null): boolean {
  return isPredictionActive(status)
}

/** 状态 → 中文标签；未知值原样回显（前向兼容，不再吐生硬英文枚举给用户看） */
export function predictionStatusLabel(status?: string | null): string {
  if (!status) return ''
  return PREDICTION_STATUS_LABELS[status as PredictionStatus] ?? status
}

// ── 阶段进度（契约 A：prediction_snapshots.progress JSONB / SSE prediction_progress） ──

/** 进度阶段。preflight = 开跑前的行情预检（2 秒判死，不进状态机） */
export type PredictionPhase = 'preflight' | 'news_searching' | 'inferring'

/** 阶段 → 中文标签 */
export const PREDICTION_PHASE_LABELS: Record<PredictionPhase, string> = {
  preflight: '行情预检',
  news_searching: '资讯搜索',
  inferring: '模型推理',
}

/**
 * 阶段进度快照。
 *
 * 两处共用同一结构：
 *   - DB：prediction_snapshots.progress（覆盖写，刷新恢复时直接读，不必等 SSE 重放）
 *   - SSE：prediction_progress 事件的 payload
 *
 * 空对象 {} 表示"无进行中的阶段"（终态会被后端清空），调用方须用
 * hasPredictionProgress() 判定，不要直接读 .phase。
 */
export interface PredictionProgress {
  phase: PredictionPhase
  label?: string          // 当前维度/子任务名，如 "地缘政治"
  index?: number          // 1-based 当前序号
  total?: number          // 总数，如 12 个资讯维度
  detail?: string         // 人类可读细节，如 "正在搜索…"
  updated_at?: number     // 秒级时间戳
}

/** progress 是否有内容（后端终态会写 {}；老数据也是 {}） */
export function hasPredictionProgress(
  p?: PredictionProgress | Record<string, never> | null,
): p is PredictionProgress {
  return !!p && typeof (p as PredictionProgress).phase === 'string'
}

/**
 * 进度 → 一行中文文案，如 "正在搜索：地缘政治 (3/12)"。
 * 无进度返回 ''，调用方据此决定是否渲染标签。
 */
export function predictionProgressText(
  p?: PredictionProgress | Record<string, never> | null,
): string {
  if (!hasPredictionProgress(p)) return ''
  const phase = PREDICTION_PHASE_LABELS[p.phase] ?? p.phase
  const head = p.detail || phase
  const name = p.label ? `：${p.label}` : ''
  const counter =
    typeof p.index === 'number' && typeof p.total === 'number' && p.total > 0
      ? ` (${p.index}/${p.total})`
      : ''
  return `${head}${name}${counter}`
}

/**
 * 行情数据故障类型（对应后端 graph/prediction/market_data.py MarketDataError.kind）。
 * 用于把"数据源限流/网络不通"和"预测算错了"区分开 —— 二者的用户动作完全不同。
 */
export type MarketDataErrorKind = 'rate_limited' | 'network' | 'no_data' | 'bad_symbol'

/** 故障类型 → 用户可采取的动作提示（message 由后端给，这里只补"该怎么办"） */
export const MARKET_DATA_ERROR_HINTS: Record<MarketDataErrorKind, string> = {
  rate_limited: '数据源被限流，稍后重试即可；也可在 .env 关闭智能选股预热以释放额度',
  network: '行情数据源网络不通，请检查网络或 DNS 后重试',
  no_data: '该标的没有可用的历史行情',
  bad_symbol: '请确认代码是否正确（A股如 000001.SZ，美股如 MU）',
}

/** 预测记录列表项（轻量，来自 GET /api/prediction/records） */
export interface PredictionRecord {
  id: string
  symbol: string
  name?: string
  market: 'cn_a' | 'us_stock'
  status: PredictionStatus
  horizon: number
  predicted_price?: number
  predicted_direction?: 'up' | 'down' | 'flat'
  predicted_range?: [number, number]
  confidence?: number
  target_date?: string
  error_message?: string
  /** 行情故障类型（仅 status=failed 且死于预检/取数时有值） */
  error_kind?: MarketDataErrorKind
  /** 阶段进度快照；终态被后端清空为 {}，老数据也是 {} */
  progress?: PredictionProgress | Record<string, never>
  /** 最后更新时间戳（秒）。0 = 无进度信息（migration 前的老行），不要拿去格式化时间 */
  updated_at?: number
  created_at: number
}

// ── v2 维度注册表（GET /api/prediction/dimensions；后端 graph/prediction/dimensions.py）──

/**
 * 维度类型：
 *   search —— 走 Tavily 资讯搜索（消耗 Tavily 预算，每维约 2 次调用）
 *   data   —— 走结构化取数（fetcher 指向 quant.data.* 函数名，不消耗 Tavily 预算）
 */
export type DimensionKind = 'search' | 'data'

/**
 * 单个预测维度（后端 Dimension dataclass 的 JSON 投影）。
 *
 * ★ `desc` 是**后端单一真相源**（dimensions.py 的 desc 字段），直接渲染到多选框。
 *   前端**禁止**再写一份维度文案 —— 会与后端漂移。
 */
export interface Dimension {
  key: string
  label: string                 // 中文名
  desc: string                  // 一句话作用（后端下发，直接上屏）
  kind: DimensionKind
  markets: PredictionMarket[]   // 适用市场（后端 tuple → JSON 数组）
  fetcher?: string | null       // kind=data 时的取数函数名（= 前端展示的"工具名"）
  default_on: boolean           // 是否默认勾选
}

/** 维度分组（后端 DIMENSION_GROUPS：组名 → key 列表） */
export interface DimensionGroup {
  name: string
  keys: string[]
}

/** GET /api/prediction/dimensions 的归一化结果 */
export interface DimensionCatalog {
  dimensions: Dimension[]       // 该市场可用的全部维度（未传 market 时为全 26 维）
  groups: DimensionGroup[]      // 分组（后端没给则前端按 kind 兜底derive）
  defaults: string[]            // 默认勾选的 key
}

/** 维度是否适用于该市场 */
export function dimensionSupports(dim: Dimension, market: PredictionMarket): boolean {
  return dim.markets.includes(market)
}

/** kind → 兜底分组名（仅当后端没下发 groups 时使用） */
export const DIMENSION_KIND_LABELS: Record<DimensionKind, string> = {
  data: '结构化数据',
  search: '资讯搜索',
}

// ── v2 数据型维度产物（公告 / 定增 / 减持 / SEC filing）──────────────────────────

/**
 * 数据可信度（后端 domain.Confidence）。用户拍板：数据口径有缺口，
 * **UI 必须诚实标注**，不假装精确。
 *   structured   —— 硬结构化字段（公告类型列、业绩预告类型、SEC 8-K item 编号）
 *   llm_inferred —— 公告标题经 LLM 语义判定（大股东减持、爆雷严重度多属此类）
 *   estimated    —— 数值取不到，保守常量兜底（如拿不到总股本时的摊薄比例）
 */
export type FilingConfidence = 'structured' | 'llm_inferred' | 'estimated'

/** 可信度 → 中文标签（唯一真相源，禁止各组件各写一份） */
export const FILING_CONFIDENCE_LABELS: Record<FilingConfidence, string> = {
  structured: '硬数据',
  llm_inferred: '模型推断',
  estimated: '估算',
}

/** 可信度 → 标签色（Element Plus tag type 语义） */
export const FILING_CONFIDENCE_TAG: Record<FilingConfidence, 'success' | 'warning' | 'info'> = {
  structured: 'success',
  llm_inferred: 'warning',
  estimated: 'info',
}

export function filingConfidenceLabel(c?: string | null): string {
  if (!c) return ''
  return FILING_CONFIDENCE_LABELS[c as FilingConfidence] ?? c
}

/** 事件类型 → 中文标签（公告判定结果；未知值原样回显） */
export const PREDICTION_EVENT_TYPE_LABELS: Record<string, string> = {
  negative: '负面事件',
  placement: '定增/增发',
  reduction: '股东减持',
  st_delisting_risk: '退市风险警示',
  violation: '违法违规',
  neutral: '中性',
}

/** 事件类型 → K 线打点颜色（设计文档 §6：爆雷红 / 定增橙 / 减持紫） */
export const PREDICTION_EVENT_TYPE_COLORS: Record<string, string> = {
  negative: '#F25D59',
  st_delisting_risk: '#F25D59',
  violation: '#F25D59',
  placement: '#FF9F1C',
  reduction: '#9B59B6',
  neutral: '#909399',
}

export function predictionEventTypeLabel(t?: string | null): string {
  if (!t) return ''
  return PREDICTION_EVENT_TYPE_LABELS[t] ?? t
}

export function predictionEventTypeColor(t?: string | null): string {
  if (!t) return PREDICTION_EVENT_TYPE_COLORS.neutral
  return PREDICTION_EVENT_TYPE_COLORS[t] ?? PREDICTION_EVENT_TYPE_COLORS.neutral
}

/**
 * 单条结构化披露事件（后端 domain.FilingItem）。
 *
 * ★ 渲染卡片时 `source` + `confidence` **必须一起显示**（用户拍板）。
 *   v2 只用「标题 + 公告类型」判定，不解析 PDF 正文 —— 见所属维度的 source_note。
 */
export interface FilingItem {
  title: string
  date: string                  // YYYY-MM-DD
  ann_type: string              // 公告类型（东财原值 / SEC form 类型）
  url: string
  // 判定结果（与 NewsDimension 的情绪打分同构）
  event_type: string            // negative | placement | reduction | st_delisting_risk | violation | neutral
  negative_score: number        // -1..0（0 = 无负面）
  reason: string
  // 诚实标注
  source: string                // akshare_notice | akshare_yjyg | sec_edgar | ...
  confidence: FilingConfidence
  // 量化字段（取不到为 null → UI 标「估算」，不假装精确）
  dilution_ratio?: number | null    // 摊薄比例 = 发行股数 / 总股本
  discount_rate?: number | null     // 折价率 = (最新价 − 发行价) / 最新价
  reduction_ratio?: number | null   // 减持占总股本比例
  decay_weight?: number | null      // 时间衰减权重 w = exp(-Δt/τ)
}

/**
 * 单个数据型维度的结果（后端 domain.FilingDimensionResult；
 * SSE data_item 的 payload / prediction_snapshots.filing_digest 数组项）。
 *
 * 与 NewsDimension 平级：`dimension` / `failed` 同名，恢复续跑时
 * 按 `news_digest + filing_digest` 里 failed=false 的并集求"已完成维度"。
 */
export interface FilingDimensionResult {
  dimension: string
  kind?: 'data'
  items: FilingItem[]
  score: number                 // 该维饱和分 S ∈ (-1, 0]
  summary?: string
  // 工具轨迹 / 成本可观测（"演出"工具调用用）
  fetcher?: string
  total_fetched?: number        // 原始拉取条数（"返回 388 条"）
  candidates?: number           // 规则过滤后进 LLM 候选池的条数
  llm_judged?: number           // 真正走了 LLM 的条数
  cache_hits?: number           // 命中 announcement_cache 的条数
  elapsed_ms?: number
  source_note?: string          // 口径说明（如「基于公告标题与类型分类，未解析 PDF 正文」）—— UI 必须显示
  // 维度级降级：该维挂掉不影响其余维度
  failed?: boolean
  error?: string
}

// ── v2 工具调用轨迹（SSE tool_trace）──────────────────────────────────────────

/**
 * 一次取数函数调用的轨迹（设计文档 §4.2）。每个数据型维度发 2 条：phase=start / end。
 * 前端渲染成终端风格滚动日志：「stock_announcements(symbol=002594, months=24) … 388 条 / 1.9s」
 */
export interface ToolTrace {
  phase: 'start' | 'end'
  dimension: string
  tool: string                          // 取数函数名（= Dimension.fetcher）
  args?: Record<string, unknown>
  rows?: number                         // phase=end：返回条数
  elapsed_ms?: number                   // phase=end
  summary?: string                      // phase=end：一句话结果
  error?: string                        // phase=end 且失败
  ts?: number
}

// ── v2 公式可解释性（SSE formula_step / model_params）──────────────────────────

/** 公式全参数（后端 domain.ModelParams；prediction_snapshots.model_params JSONB） */
export interface ModelParams {
  last_close: number
  // ARIMA
  arima_order: [number, number, number]                    // (p, d, q)
  arima_aic: number
  arima_grid_tried: Array<Record<string, unknown>>         // [{order, aic}] 网格搜索过程
  arima_drift_annual: number                               // μ_ARIMA
  // GARCH(1,1)
  garch_omega: number
  garch_alpha: number
  garch_beta: number
  garch_persistence: number                                // α+β（<1 才平稳）
  garch_sigma_annual: number                               // σ_GARCH
  // 情绪 / 事件
  news_sentiment: number                                   // S_news ∈ [-1,1]
  event_scores: Record<string, number>                     // {negative, placement, reduction}
  event_weights: Record<string, number>                    // {negative: .5, placement: .2, reduction: .3}
  filing_score: number                                     // F
  filing_weight: number                                    // λ
  sentiment_total: number                                  // S_total = (1−λ)·S_news + λ·F
  // 最终代入 GBM
  alpha_used: number                                       // α = 0.15
  drift_mu_annual_final: number                            // μ
  sigma_annual_final: number                               // σ
  vol_multiplier_news: number                              // M_news
  vol_multiplier_event: number                             // M_event
  // 蒙特卡洛
  mc_n_paths: number
  mc_percentiles: number[]
  mc_dt: number                                            // 1/252
  // 影线标定（预测 K 线）
  shadow_k_up: number
  shadow_k_dn: number
  shadow_lookback_bars: number
}

/**
 * 单条公式推导（SSE formula_step；prediction_snapshots.formula_steps JSONB）。
 * 前端用等宽字体渲染 formula + substituted，params 表格化。
 * `formula_latex` 后端已下发但**当前不渲染**（前端无 LaTeX 依赖，P3 再上 KaTeX）。
 */
export interface FormulaStep {
  key: string                              // arima | garch | event_score | sentiment_total | gbm | shadow
  title?: string
  formula?: string                         // 等宽纯文本公式
  formula_latex?: string                   // 暂存不渲染
  params?: Record<string, number | string>
  substituted?: string                     // 代入实际数字后的算式
  result?: number | null
  result_text?: string
  explain?: string                         // 一句人话解释
}

/**
 * 单根预测蜡烛（SSE forecast_day；prediction_snapshots.forecast_candles JSONB）。
 *
 * ★ 诚实标注（UI 必须写）：影线由历史平均影线比例合成，不代表真实日内极值分布。
 */
export interface ForecastCandle {
  day: number                   // 第几个交易日（1..horizon）
  date: string                  // YYYY-MM-DD
  o: number
  h: number
  l: number
  c: number
  p10?: number
  p50?: number
  p90?: number
}

/** K 线上的公告事件打点（SSE event_marker；prediction_snapshots.event_markers JSONB） */
export interface EventMarker {
  date: string
  event_type: string            // negative | placement | reduction | ...
  title?: string
  score?: number                // -1..0
  dimension?: string
}

/** 单条资讯（Tavily 归一化） */
export interface NewsItem {
  title: string
  url: string
  snippet: string
  full_content?: string
}

/** 资讯维度（LLM 查询 + Tavily 结果 + 情绪打分） */
export interface NewsDimension {
  dimension: string
  query: string
  answer?: string
  results?: NewsItem[]
  sentiment?: number          // -1..+1
  sentiment_reason?: string
  /** 维度级降级：该维数据源挂了只标 failed，其余维度继续（不整单 failed） */
  failed?: boolean
  error?: string
}

/** 预测数值结果（蒙特卡洛 + ARIMA + GARCH 输出） */
export interface Forecast {
  dates: string[]
  point_forecast: number[]
  p10: number[]
  p50: number[]
  p90: number[]
  inflection_points?: Array<{ date: string; price: number; type: 'peak' | 'trough' }>
  confidence?: number
  model_name?: string
  /** 注意：macd / boll / kdj 是**嵌套 dict**，渲染前必须展开，直接插值会得到 [object Object] */
  technical_indicators?: Record<string, number | string | Record<string, number | string>>
  // ── v2 新增（后端 ForecastResult）──
  model_params?: ModelParams | null      // 公式全参数
  forecast_candles?: ForecastCandle[]    // 预测 OHLC 蜡烛
  formula_steps?: FormulaStep[]          // 公式推导序列
}

/** 预测快照完整状态（GET /api/prediction/{id}，刷新回放用） */
export interface PredictionSnapshot {
  id: string
  client_id?: string
  user_id?: string
  conversation_id?: string
  criteria: {
    symbol: string
    market: 'cn_a' | 'us_stock'
    horizon?: number
    methods?: string[]
    dimensions?: string[]
  }
  market: 'cn_a' | 'us_stock'
  symbol: string
  name?: string
  status: PredictionStatus
  horizon?: number
  target_date?: string
  predicted_price?: number
  predicted_range?: [number, number]
  confidence?: number
  model_name?: string
  news_digest?: NewsDimension[]
  news_sentiment?: number
  forecast?: Forecast
  inflection_points?: Array<{ date: string; price: number; type: 'peak' | 'trough' }>
  confidence_band?: { dates: string[]; p10: number[]; p50: number[]; p90: number[] }
  technical_indicators?: Record<string, number | string>
  historical_data?: { dates: string[]; values: [number, number, number, number][]; ma5?: number[]; ma10?: number[]; ma20?: number[]; volumes?: [string, number, number][] }
  analysis?: string
  risk_notes?: string[]
  thinking_segments?: Array<{ node: string; step_index: number | null; phase: string; content: string }>
  error_message?: string
  /** 行情故障类型（预检/取数失败时由后端 MarketDataError.kind 落库） */
  error_kind?: MarketDataErrorKind

  // ── v2 新增列（prediction_snapshots.*；刷新恢复靠 DB 列重建，不靠重放全部事件）──
  /** 数据型维度结果（公告/定增/减持/SEC）—— 与 news_digest 平级 */
  filing_digest?: FilingDimensionResult[]
  /** 公式全参数（§5.2） */
  model_params?: ModelParams | null
  /** 公式推导序列（刷新后重建公式卡片） */
  formula_steps?: FormulaStep[]
  /** 预测 OHLC 蜡烛（7 根） */
  forecast_candles?: ForecastCandle[]
  /** K 线公告事件打点 */
  event_markers?: EventMarker[]
  /** 取数工具调用轨迹 */
  tool_traces?: ToolTrace[]
  /** 事件综合分 F ∈ [-1, 0]（§5.3） */
  event_score?: number
  /** 阶段进度快照（刷新恢复时立刻拿到当前阶段，不必等 SSE 重放完 4000+ 条 thinking） */
  progress?: PredictionProgress | Record<string, never>
  /** 最后更新时间戳（秒）。0 = 无进度信息，不要格式化（会显示 1970） */
  updated_at?: number
  created_at: number
}
