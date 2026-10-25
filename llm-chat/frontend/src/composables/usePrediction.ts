import { ref, computed } from 'vue'
import * as api from '../api'
import type {
  PredictionRecord,
  PredictionSnapshot,
  NewsItem,
  NewsDimension,
  Forecast,
  PredictionProgress,
  MarketDataErrorKind,
  FilingDimensionResult,
  FormulaStep,
  ForecastCandle,
  EventMarker,
  ToolTrace,
  ModelParams,
} from '../types'
import {
  isPredictionStreamable,
  isPredictionPaused,
  hasPredictionProgress,
  predictionStatusLabel,
} from '../types'
import { useAdmin } from './useAdmin'

/**
 * 股票预测工作台 composable（单例，module-scoped state）。
 *
 * 与 useQuant 的关键差异：预测整条流水线（preflight→news_searching→inferring→done）
 * 是单条 SSE 流，startPredict 拿到 prediction_id 后立即 streamPrediction，
 * 而非 useQuant 的 poll-then-SSE。
 *
 * 三条不变式（三者都曾是 bug 的来源）：
 *  1. 「谁在跑」(streamingId) 与「在看谁」(viewingId) 是两件事。所有流式回调必须
 *     先校验事件所属的 prediction 就是当前查看的那条，否则跑着 A 时点开 B 会把 A 的
 *     token 灌进 B；stopPredict 也必须停 streamingId 而不是 viewingId。
 *  2. 网络断开 ≠ 预测失败。onInterrupted / onReconnect 只更新连接状态提示，
 *     绝不置 status=failed；只有后端 error 事件才是真失败。
 *  3. thinking 不逐字 setState。重放 4000+ 条事件时按窗口批量刷入（见 appendThinking），
 *     否则刷新页面会把整个推理过程重新"打字机"一遍。
 *
 * v2 增补的两条：
 *  4. 所有 v2 流式回调必须**幂等**。afterEventId=0 重连会把已落库事件原样重放一遍：
 *     - forecast_day 按 day **覆盖**（不是 push），否则刷新后会画出 14 根蜡烛；
 *     - formula_step 按 key 覆盖；data_item 按 dimension 覆盖；
 *     - tool_trace 按 (dimension, tool) 配对（start 建行、end 补完同一行）；
 *     - event_marker 按 (date, event_type, title) 去重。
 *  5. 刷新恢复**不依赖事件重放**：GET /{id} 的落库列（formula_steps / filing_digest /
 *     forecast_candles / event_markers / tool_traces）就能把整个 v2 界面重建出来。
 *     重放只是锦上添花（且因为 4 是幂等的，重放不会画重）。
 */

// ── 展示状态 ──
const records = ref<PredictionRecord[]>([])
const isPredicting = ref(false)
const predictionStage = ref<string>('')                    // 状态机阶段中文提示
const progress = ref<PredictionProgress | null>(null)      // 细粒度阶段进度（契约 A/F）
const currentSnapshot = ref<PredictionSnapshot | null>(null)
const newsDigest = ref<NewsDimension[]>([])
const forecast = ref<Forecast | null>(null)
const thinking = ref<string>('')
const errorMsg = ref<string>('')                           // 真正的预测失败
const errorKind = ref<MarketDataErrorKind | ''>('')        // 失败类型（限流/网络/无数据/代码错）

// ── v2 产物（全部按 key 归并，重放幂等）──
const filingDigest = ref<FilingDimensionResult[]>([])      // 数据型维度（公告/定增/减持/SEC）
const formulaSteps = ref<FormulaStep[]>([])                // 公式推导序列
const forecastCandles = ref<ForecastCandle[]>([])          // 预测 OHLC 蜡烛（按 day 索引覆盖）
const eventMarkers = ref<EventMarker[]>([])                // K 线公告打点
const toolTraces = ref<ToolTraceRow[]>([])                 // 取数工具调用日志

/**
 * 工具调用日志行：把 tool_trace 的 start / end 两条事件**配对成一行**。
 * 后端 payload 带 id（`<pred_id>:<dim>:<fetcher>`），start/end 同 id；
 * 没有 id 时退化为 (dimension, tool) —— 同一次预测内也唯一。
 */
export interface ToolTraceRow {
  key: string
  dimension: string
  tool: string
  args?: Record<string, unknown>
  /** 是否已收到 end（false = 调用进行中，UI 转圈） */
  done: boolean
  ok?: boolean
  rows?: number
  elapsed_ms?: number
  summary?: string
  error?: string
  ts?: number
}

// ── 连接状态（与"预测失败"严格分离）──
const connectionMsg = ref<string>('')                      // "连接已断开，正在重连…"
const isReconnecting = ref(false)
const canManualReconnect = ref(false)                      // 自动重连耗尽 → 允许手动重连

// ── 身份：在看谁 / 谁在跑 ──
/** 发起预测后、拿到 prediction_id 前的占位（让详情面板立刻打开） */
export const PENDING_ID = '__pending__'
const viewingId = ref<string | null>(null)                 // 详情面板正在展示的记录（null = 列表页）
const streamingId = ref<string | null>(null)               // SSE 流当前挂在哪条预测上

/** 正在查看的这条就是正在跑的这条 —— 「停止」按钮 / 进度条只在此时有意义 */
const isViewingLive = computed(
  () =>
    isPredicting.value &&
    streamingId.value !== null &&
    (viewingId.value === streamingId.value || viewingId.value === PENDING_ID),
)

/** 正在看的这条是暂停态 → 显示「继续」按钮 */
const isViewingPaused = computed(() => isPredictionPaused(currentSnapshot.value?.status))

/** 公式参数：优先流式 forecast，其次落库快照 */
const modelParams = computed<ModelParams | null>(
  () => forecast.value?.model_params ?? currentSnapshot.value?.model_params ?? null,
)

let _predictAbort: AbortController | null = null
let _recordsRequest: { key: string; promise: Promise<void> } | null = null
let _recordsVersion = 0
let _snapshotVersion = 0
let _snapshotAbort: AbortController | null = null
let _chartAbort: AbortController | null = null
/** pause/resume/delete 在途标记，避免用户狂点导致并发请求 */
const isPausing = ref(false)
const isResuming = ref(false)

/**
 * 上次收到的 event_log row id（SSE 事件 id 字段）。模块级、内存中。
 * streamPrediction 内部自己维护重连游标；这里保留一份供手动重连 / resume 续传用。
 *
 * ★ 必须**连同它属于哪条预测**一起记：event_log 的 id 是全局自增的，跑完 A（eid 到 5000）
 *   再去 resume B，若拿 5000 当 B 的游标，B 的全部历史事件都会被跳过 —— 界面上就是
 *   "继续之后前面跑完的维度全没了"。_cursorFor() 保证游标只在同一条预测内复用。
 */
let _lastEventId: number = 0
let _lastEventPid: string | null = null

function _cursorFor(pid: string): number {
  return _lastEventPid === pid ? _lastEventId : 0
}

// ── thinking 批量渲染（根因 #5：刷新要重放 4000+ 条 thinking）─────────────────
//
// 重放窗口内：静默累积，窗口结束一次性塞进去（不走逐字动画）。
// 窗口之外（实时 token）：50ms 合并刷入，肉眼仍是流式打字，但把 N 次响应式写入
// 压成 20 次/秒，避免长推理把主线程打满。
const LIVE_FLUSH_MS = 50
const REPLAY_WINDOW_MS = 1200
let _thinkBuf = ''
let _liveTimer: number | null = null
let _replayTimer: number | null = null
let _replayUntil = 0
/** 重连/重放前不立刻清空 thinking（会闪白），等第一条 thinking 事件到达时再清 */
let _pendingThinkingReset = false

function _flushThinking() {
  if (!_thinkBuf) return
  thinking.value += _thinkBuf
  _thinkBuf = ''
}

function _resetThinkingBuffers() {
  if (_liveTimer !== null) { clearTimeout(_liveTimer); _liveTimer = null }
  if (_replayTimer !== null) { clearTimeout(_replayTimer); _replayTimer = null }
  _thinkBuf = ''
  _replayUntil = 0
  _pendingThinkingReset = false
}

function appendThinking(text: string) {
  if (_pendingThinkingReset) {
    // 第一条流式/重放事件到达 → 丢掉 loadSnapshot 塞的 DB 文本，改由 event_log 重放为准
    _pendingThinkingReset = false
    thinking.value = ''
    _thinkBuf = ''
  }
  _thinkBuf += text

  const now = Date.now()
  if (now < _replayUntil) {
    // 重放窗口：不逐字渲染，窗口到点后整块塞入
    if (_replayTimer === null) {
      _replayTimer = window.setTimeout(() => {
        _replayTimer = null
        _flushThinking()
      }, _replayUntil - now + 20) as unknown as number
    }
    return
  }

  if (_liveTimer === null) {
    _liveTimer = window.setTimeout(() => {
      _liveTimer = null
      _flushThinking()
    }, LIVE_FLUSH_MS) as unknown as number
  }
}

// ── v2 幂等归并器（重放安全）───────────────────────────────────────────────────

/** 数据型维度：按 dimension 覆盖。重放的同一维度合并而非重复追加 */
function mergeFilingDimension(d: FilingDimensionResult) {
  if (!d?.dimension) return
  const idx = filingDigest.value.findIndex(x => x.dimension === d.dimension)
  if (idx >= 0) {
    filingDigest.value[idx] = { ...filingDigest.value[idx], ...d }
  } else {
    filingDigest.value.push(d)
  }
}

/** 公式步骤：按 key 覆盖，保持首次到达的顺序（推导顺序 = 展示顺序） */
function mergeFormulaStep(s: FormulaStep) {
  if (!s?.key) return
  const idx = formulaSteps.value.findIndex(x => x.key === s.key)
  if (idx >= 0) {
    formulaSteps.value[idx] = { ...formulaSteps.value[idx], ...s }
  } else {
    formulaSteps.value.push(s)
  }
}

/**
 * 预测蜡烛：★ 按 day **覆盖**，绝不 push。
 * push 会让 afterEventId=0 的全量重放画出 14 根蜡烛（7 根原始 + 7 根重放）。
 */
function mergeForecastCandle(c: ForecastCandle) {
  if (!c || typeof c.day !== 'number' || c.day < 1) return
  const arr = forecastCandles.value.slice()
  const i = c.day - 1
  // 中间有洞时先占位（乱序到达也不会错位；空洞由渲染层过滤）
  while (arr.length < i) arr.push(undefined as unknown as ForecastCandle)
  arr[i] = c
  forecastCandles.value = arr
}

/** 事件打点：按 (date, event_type, title) 去重 */
function mergeEventMarker(m: EventMarker) {
  if (!m?.date) return
  const sig = `${m.date}|${m.event_type}|${m.title ?? ''}`
  const exists = eventMarkers.value.some(
    x => `${x.date}|${x.event_type}|${x.title ?? ''}` === sig,
  )
  if (!exists) eventMarkers.value.push(m)
}

/** 工具轨迹：start 建行、end 补完同一行（配对键优先用后端下发的 id） */
function mergeToolTrace(t: ToolTrace) {
  if (!t?.tool) return
  const key = String((t as ToolTrace & { id?: string }).id || `${t.dimension}:${t.tool}`)
  const idx = toolTraces.value.findIndex(x => x.key === key)
  const isEnd = t.phase === 'end'
  const patch: Partial<ToolTraceRow> = {
    dimension: t.dimension,
    tool: t.tool,
    args: t.args,
    ts: t.ts,
  }
  if (isEnd) {
    patch.done = true
    patch.ok = (t as ToolTrace & { ok?: boolean }).ok ?? !t.error
    patch.rows = t.rows
    patch.elapsed_ms = t.elapsed_ms
    patch.summary = t.summary
    patch.error = t.error
  }
  if (idx >= 0) {
    toolTraces.value[idx] = { ...toolTraces.value[idx], ...patch }
  } else {
    toolTraces.value.push({
      key,
      dimension: t.dimension,
      tool: t.tool,
      args: t.args,
      done: isEnd,
      ...patch,
    } as ToolTraceRow)
  }
}

/** 清空 v2 产物（换记录 / 重新发起时） */
function _resetV2() {
  filingDigest.value = []
  formulaSteps.value = []
  forecastCandles.value = []
  eventMarkers.value = []
  toolTraces.value = []
}

export function usePrediction() {
  const admin = useAdmin()

  /** 事件是否该应用到当前视图：既是当前这条流，又是正在看的这条记录 */
  function isLiveFor(id: string): boolean {
    return streamingId.value === id && viewingId.value === id
  }

  /** 清空上一条记录残留的错误/进度/连接提示（切换记录时必须调，否则横幅会串台） */
  function resetTransient() {
    errorMsg.value = ''
    errorKind.value = ''
    connectionMsg.value = ''
    isReconnecting.value = false
    canManualReconnect.value = false
    progress.value = null
    predictionStage.value = ''
  }

  /** 把状态机/进度增量同步进列表行，列表页也能看到"正在搜索 3/12" */
  function patchRecord(id: string, patch: Partial<PredictionRecord>) {
    const idx = records.value.findIndex(r => r.id === id)
    if (idx >= 0) records.value[idx] = { ...records.value[idx], ...patch }
  }

  /** 拉取当前用户的预测记录列表 */
  function loadRecords(market?: 'cn_a' | 'us_stock'): Promise<void> {
    const requestKey = `${market || ''}:${admin.verifiedAdminKey.value}`
    if (_recordsRequest?.key === requestKey) return _recordsRequest.promise
    const version = ++_recordsVersion
    const request = { key: requestKey, promise: Promise.resolve() }
    request.promise = (async () => {
      try {
        const rows = await api.fetchPredictionRecords(market, admin.verifiedAdminKey.value)
        if (version === _recordsVersion) records.value = rows
      } catch (e: any) {
        if (version === _recordsVersion) errorMsg.value = e?.message || '获取预测记录失败'
      } finally {
        if (_recordsRequest === request) _recordsRequest = null
      }
    })()
    _recordsRequest = request
    return request.promise
  }

  /**
   * 加载预测快照完整状态（full-state replay，刷新恢复用）。
   *
   * ★ v2：整个界面（公式卡片 / 数据卡片 / 预测蜡烛 / 打点 / 工具日志）都能**只靠这里**
   *   重建出来 —— 不依赖 SSE 事件重放。事件重放到达时会按 key 覆盖，不会画重。
   */
  async function loadSnapshot(id: string): Promise<PredictionSnapshot | null> {
    const version = ++_snapshotVersion
    _snapshotAbort?.abort()
    const snapshotController = new AbortController()
    _snapshotAbort = snapshotController
    _chartAbort?.abort()
    try {
      const key = admin.verifiedAdminKey.value
      const snap = await api.fetchPredictionSnapshot(id, key, snapshotController.signal)
      if (version !== _snapshotVersion || viewingId.value !== id) return null

      // K 线历史。已完成记录后端已把当时的 bars 落库到 historical_bars（图表契约结构），
      // 有内容就直接用、**跳过现拉**（这就是用户要的：美股链路会被雅虎限流卡死甚至超时）。
      // 旧记录 / 运行中未落库 → historical_bars 为空对象 → 回退现拉 fetchStockChart（保持兼容）。
      if (snap.historical_bars && (snap.historical_bars.dates?.length ?? 0) > 0) {
        snap.historical_data = {
          dates: snap.historical_bars.dates || [],
          values: snap.historical_bars.values || [],
          ma5: snap.historical_bars.ma5 as number[] | undefined,
          ma10: snap.historical_bars.ma10 as number[] | undefined,
          ma20: snap.historical_bars.ma20 as number[] | undefined,
          volumes: snap.historical_bars.volumes as unknown as [string, number, number][] | undefined,
        }
      } else if (snap.symbol && snap.market) {
        // Chart loading must not delay snapshot rendering or SSE attachment.
        const controller = new AbortController()
        _chartAbort = controller
        const timer = setTimeout(() => controller.abort(), 15_000)
        void api.fetchStockChart(snap.symbol, 240, key, controller.signal).then(chart => {
          if (version !== _snapshotVersion || viewingId.value !== id || !currentSnapshot.value) return
          currentSnapshot.value.historical_data = {
            dates: chart.dates || [], values: chart.values || [],
            ma5: chart.ma5, ma10: chart.ma10, ma20: chart.ma20, volumes: chart.volumes,
          }
        }).catch(error => {
          if (!controller.signal.aborted) console.warn('fetch historical K-line failed', error)
        }).finally(() => {
          clearTimeout(timer)
          if (_chartAbort === controller) _chartAbort = null
        })
      }

      currentSnapshot.value = snap
      newsDigest.value = snap.news_digest || []
      forecast.value = snap.forecast || null
      _resetThinkingBuffers()
      thinking.value = snap.stream_text ?? snap.thinking_segments?.map(s => s.content).join('\n') ?? ''
      _lastEventPid = id
      _lastEventId = snap.event_cursor ?? 0

      // ── v2：从落库列重建（DB 列优先，其次 forecast 内嵌的同名字段）──
      filingDigest.value = snap.filing_digest || []
      formulaSteps.value = snap.formula_steps || snap.forecast?.formula_steps || []
      forecastCandles.value = snap.forecast_candles || snap.forecast?.forecast_candles || []
      eventMarkers.value = snap.event_markers || []
      toolTraces.value = []
      for (const t of snap.tool_traces || []) mergeToolTrace(t)

      // 刷新恢复：先用落库的 progress 秒级重建"正在搜索 3/12"，不必等事件重放完
      progress.value = hasPredictionProgress(snap.progress) ? snap.progress : null

      // 失败的记录必须显示失败原因（后端现在会给"数据源限流"这类可行动的原因）
      if (snap.status === 'failed' && snap.error_message) {
        errorMsg.value = snap.error_message
        errorKind.value = snap.error_kind || ''
      } else {
        errorMsg.value = ''
        errorKind.value = ''
      }
      if (isPredictionPaused(snap.status)) {
        predictionStage.value = predictionStatusLabel(snap.status)
      }
      return snap
    } catch (e: any) {
      if (!snapshotController.signal.aborted && version === _snapshotVersion && viewingId.value === id) errorMsg.value = e?.message || '获取预测快照失败'
      return null
    }
  }

  /**
   * 检查是否有进行中的预测会话（刷新/重进页面恢复）。
   *
   * 关键：重连 SSE 的同时必须把详情面板打开并定位到那条记录（viewingId），
   * 否则会变成一条看不见的流 —— 后台在跑、屏幕上什么都没有。
   */
  async function checkActiveSession(isCurrent: () => boolean = () => true, market?: 'cn_a' | 'us_stock') {
    try {
      const key = admin.verifiedAdminKey.value
      const version = _snapshotVersion
      const previousView = viewingId.value
      const res = await api.fetchActivePrediction(market, key)
      if (!isCurrent() || version !== _snapshotVersion || viewingId.value !== previousView) return
      if (!res.active || !res.prediction_id) return

      const pid = res.prediction_id
      _abortStream()
      resetTransient()
      viewingId.value = pid                        // ← 打开详情面板并定位到这条
      const snap = await loadSnapshot(pid)
      // ★ 用 isPredictionStreamable 而不是 !isPredictionTerminal：paused 非终态但后台图已被
      //   cancel，连上去只会挂一条死流 → 30s 停滞 → 假的「连接已断开」。
      if (snap && isPredictionStreamable(snap.status)) {
        void streamPredict(pid, snap.event_cursor ?? 0, { hydrated: true })
      }
    } catch (e) {
      console.warn('Check active prediction session failed', e)
    }
  }

  /**
   * 发起预测。POST 立即返回 prediction_id，随后连 SSE（单流贯穿全阶段）。
   */
  async function startPredict(criteria: api.PredictionCriteria) {
    const version = ++_snapshotVersion
    _snapshotAbort?.abort()
    _abortStream()
    _lastEventId = 0
    _lastEventPid = null
    _resetThinkingBuffers()
    resetTransient()
    _resetV2()

    isPredicting.value = true
    thinking.value = ''
    newsDigest.value = []
    forecast.value = null
    currentSnapshot.value = null
    predictionStage.value = '排队中...'
    viewingId.value = PENDING_ID                   // 立刻开面板，用户不用盯着列表等
    streamingId.value = null

    const key = admin.verifiedAdminKey.value
    if (!key) {
      errorMsg.value = '请先完成管理员验证'
      isPredicting.value = false
      viewingId.value = null
      return
    }

    try {
      const res = await api.startPrediction(criteria, key)
      if (!res.prediction_id) {
        throw new Error('后端未返回 prediction_id')
      }
      if (version !== _snapshotVersion || viewingId.value !== PENDING_ID) {
        void loadRecords()
        return
      }
      viewingId.value = res.prediction_id
      const snap = await loadSnapshot(res.prediction_id)
      if (!snap || viewingId.value !== res.prediction_id) return
      if (isPredictionStreamable(snap.status)) {
        void streamPredict(res.prediction_id, snap.event_cursor ?? 0, { hydrated: true })
      } else {
        isPredicting.value = false
      }
      void loadRecords()
    } catch (e: any) {
      errorMsg.value = e?.message || '发起预测失败'
      isPredicting.value = false
      predictionStage.value = ''
      if (viewingId.value === PENDING_ID) viewingId.value = null
    }
  }

  /**
   * 从列表点开一条记录。运行中 → 必须重连 SSE 续流，否则面板看起来是空的、死的。
   * 暂停态**不连流**（后台图已 cancel），界面全靠 loadSnapshot 的落库列重建。
   */
  async function openDetail(id: string) {
    // Stop callbacks before hydrating a checkpoint, including reopening the
    // same prediction; otherwise new tokens can be overwritten by the GET.
    _abortStream()
    isPredicting.value = false
    _resetThinkingBuffers()
    resetTransient()
    _resetV2()
    viewingId.value = id

    const snap = await loadSnapshot(id)
    if (snap && isPredictionStreamable(snap.status)) {
      void streamPredict(id, snap.event_cursor ?? 0, { hydrated: true })
    }
  }

  function backToList() {
    ++_snapshotVersion
    _snapshotAbort?.abort()
    _chartAbort?.abort()
    if (viewingId.value === PENDING_ID) isPredicting.value = false
    viewingId.value = null
    // 不断流：后台还在跑的那条继续跑，回列表还能看到进度行
  }

  /**
   * 断开当前 SSE（只断客户端，不停后台的图）。
   *
   * 必须同时把 streamingId 置空 —— 它是"哪条流是当前流"的唯一判据：
   *  - 留着旧 id 会让 stopPredict 停错那条（看着已完成的 B，却把后台在跑的 A 杀了）；
   *  - abort 会让 api 层回调一次 onStopped（它分不清"用户停止"和"切走了视图"），
   *    streamingId 已清空 → 那条回调被下面的 isThisStream 挡掉，
   *    于是后台仍在跑的预测不会被误标成「已停止」。
   */
  function _abortStream() {
    if (_predictAbort) {
      _predictAbort.abort()
      _predictAbort = null
    }
    streamingId.value = null
  }

  /**
   * 连接预测 SSE 流，分发事件到 state。单流贯穿 preflight→news_searching→inferring→done。
   *
   * opts.replay=true 表示这次连接会先回放已落库事件（刷新恢复 / 点开运行中的记录）。
   * 此时开一个重放窗口：窗口内的 thinking 静默累积、一次性塞入，不走逐字动画。
   */
  async function streamPredict(
    predictionId: string,
    afterEventId: number = 0,
    opts: { replay?: boolean; hydrated?: boolean } = {},
  ) {
    _abortStream()
    _predictAbort = new AbortController()
    const signal = _predictAbort.signal

    streamingId.value = predictionId
    isPredicting.value = true
    connectionMsg.value = ''
    isReconnecting.value = false
    canManualReconnect.value = false

    const key = admin.verifiedAdminKey.value

    // 这条流是不是仍然是"当前流"。被 _abortStream / 新的 streamPredict 顶掉之后，
    // 迟到的回调必须全部作废 —— 否则被切走的旧流会继续改列表、甚至把自己标成已停止。
    const isThisStream = () => streamingId.value === predictionId && _predictAbort?.signal === signal

    // 重放不立刻清空 thinking（会闪白），等第一条 thinking 事件到达再清（见 appendThinking）
    _flushThinking()
    _resetThinkingBuffers()
    _pendingThinkingReset = afterEventId === 0 && !opts.hydrated
    if (opts.replay) _replayUntil = Date.now() + REPLAY_WINDOW_MS

    // ★ 只有全量重放（游标 0）才清空已渲染的维度/预测。
    //   游标 > 0 的续传（手动重连 / resume）只会收到 cursor 之后的新事件 —— 此时清空
    //   等于把已经跑完的 8 个维度、已经画好的蜡烛全擦掉，且永远不会被重新发一遍。
    if (afterEventId === 0 && !opts.hydrated) {
      newsDigest.value = []
      forecast.value = null
      _resetV2()
    }

    try {
      await api.streamPrediction(
        predictionId,
        key,
        {
          onEventId: (id) => {
            if (!isThisStream()) return
            if (_lastEventPid !== predictionId) {
              _lastEventPid = predictionId
              _lastEventId = 0
            }
            if (id > _lastEventId) _lastEventId = id
          },

          onProgress: (status) => {
            if (!isThisStream()) return
            // 状态机转换：列表行永远更新；详情面板只在看着这条时更新
            patchRecord(predictionId, { status: status as PredictionRecord['status'] })
            if (!isThisStream() || !isLiveFor(predictionId)) return
            predictionStage.value = predictionStatusLabel(status)
            if (currentSnapshot.value) {
              currentSnapshot.value.status = status as PredictionSnapshot['status']
            }
            // 收到实时事件 = 链路活着，清掉重连提示
            if (connectionMsg.value) {
              connectionMsg.value = ''
              isReconnecting.value = false
            }
          },

          onPhaseProgress: (p) => {
            if (!isThisStream()) return
            patchRecord(predictionId, { progress: p })
            if (!isThisStream() || !isLiveFor(predictionId)) return
            progress.value = p
            if (connectionMsg.value) {
              connectionMsg.value = ''
              isReconnecting.value = false
            }
          },

          onThinking: (text) => {
            if (!isThisStream() || !isLiveFor(predictionId)) return
            appendThinking(text)
          },

          onContent: (text) => {
            if (!isThisStream() || !isLiveFor(predictionId)) return
            appendThinking(text)
          },

          onNews: (item) => {
            if (!isThisStream() || !isLiveFor(predictionId)) return
            const dim = item as Partial<NewsDimension>
            const rawItem = item as Partial<NewsItem>
            if (dim && dim.dimension) {
              // 按维度归并（回放的重复 NEWS_ITEM 事件合并而非重复追加）
              const idx = newsDigest.value.findIndex(d => d.dimension === dim.dimension)
              if (idx >= 0) {
                newsDigest.value[idx] = { ...newsDigest.value[idx], ...dim } as NewsDimension
              } else {
                newsDigest.value.push({ dimension: dim.dimension, query: dim.query || '', ...dim } as NewsDimension)
              }
            } else if (rawItem && rawItem.title) {
              let general = newsDigest.value.find(d => d.dimension === '其他')
              if (!general) {
                general = { dimension: '其他', query: '', results: [] }
                newsDigest.value.push(general)
              }
              general.results = general.results || []
              general.results.push(rawItem as NewsItem)
            }
          },

          onForecast: (fc) => {
            if (!isThisStream() || !isLiveFor(predictionId)) return
            forecast.value = fc
            if (currentSnapshot.value) currentSnapshot.value.forecast = fc
            // 终态 FORECAST 里若带了蜡烛/公式（后端一次性补齐），按 key 归并（不覆盖已流式到达的）
            for (const c of fc.forecast_candles || []) mergeForecastCandle(c)
            for (const s of fc.formula_steps || []) mergeFormulaStep(s)
          },

          onInflection: (points) => {
            if (!isThisStream() || !isLiveFor(predictionId)) return
            if (currentSnapshot.value) {
              currentSnapshot.value.inflection_points = points as PredictionSnapshot['inflection_points']
            }
            if (forecast.value) {
              forecast.value.inflection_points = points as Forecast['inflection_points']
            }
          },

          // ── v2 事件：全部幂等归并（重放不重复渲染）──────────────────────────
          onToolTrace: (t) => {
            if (!isThisStream() || !isLiveFor(predictionId)) return
            mergeToolTrace(t)
          },

          onDataItem: (d) => {
            if (!isThisStream() || !isLiveFor(predictionId)) return
            mergeFilingDimension(d)
          },

          onFormulaStep: (s) => {
            if (!isThisStream() || !isLiveFor(predictionId)) return
            mergeFormulaStep(s)
          },

          onForecastDay: (c) => {
            if (!isThisStream() || !isLiveFor(predictionId)) return
            mergeForecastCandle(c)
          },

          onEventMarker: (m) => {
            if (!isThisStream() || !isLiveFor(predictionId)) return
            mergeEventMarker(m)
          },

          // 已暂停：**非终态**（可 resume），但这条流到此为止（后台图已被 cancel）。
          // 绝不能走 onInterrupted 的"连接断开"话术 —— 那是假象，用户是自己点的暂停。
          onPaused: () => {
            if (!isThisStream()) return
            _applyPaused(predictionId)
          },

          onDone: () => {
            if (!isThisStream()) return
            // done 仅作完成信号；权威最终态（analysis/risk_notes/forecast/historical_data）从 DB 拉
            _flushThinking()
            streamingId.value = null
            isPredicting.value = false
            connectionMsg.value = ''
            isReconnecting.value = false
            canManualReconnect.value = false
            progress.value = null
            if (viewingId.value === predictionId) {
              predictionStage.value = '已完成'
              void loadSnapshot(predictionId)
            }
            void loadRecords()
          },

          // 真正的预测失败（后端 error 事件），不是网络问题
          onError: (msg, kind) => {
            if (!isThisStream()) return
            _flushThinking()
            streamingId.value = null
            isPredicting.value = false
            connectionMsg.value = ''
            isReconnecting.value = false
            canManualReconnect.value = false
            progress.value = null
            patchRecord(predictionId, {
              status: 'failed',
              error_message: msg,
              error_kind: kind,
              progress: {},
            })
            if (viewingId.value === predictionId || viewingId.value === PENDING_ID) {
              errorMsg.value = msg
              errorKind.value = kind || ''
              predictionStage.value = '失败'
              if (currentSnapshot.value) currentSnapshot.value.status = 'failed'
            }
            void loadRecords()
          },

          // 用户主动停止 或 后端 stopped 事件。
          // 注意：切换视图导致的 abort 也会走到这里，但那时 streamingId 已被清空 →
          // isThisStream() 为 false → 不会把后台仍在跑的预测误标成「已停止」。
          onStopped: () => {
            if (!isThisStream()) return
            _flushThinking()
            streamingId.value = null
            isPredicting.value = false
            connectionMsg.value = ''
            isReconnecting.value = false
            canManualReconnect.value = false
            progress.value = null
            patchRecord(predictionId, { status: 'stopped', progress: {} })
            if (viewingId.value === predictionId) {
              predictionStage.value = '已停止'
              if (currentSnapshot.value) currentSnapshot.value.status = 'stopped'
            }
            void loadRecords()
          },

          // ── 网络类事件：绝不能报成"预测失败" ──
          onReconnect: (attempt, maxAttempts, delayMs) => {
            if (!isThisStream()) return
            isReconnecting.value = true
            canManualReconnect.value = false
            connectionMsg.value =
              `连接已断开，正在重连（${attempt}/${maxAttempts}，${Math.round(delayMs / 1000)}s 后重试）…预测仍在后台运行`
          },

          onStalled: (silentMs) => {
            if (!isThisStream()) return
            isReconnecting.value = true
            connectionMsg.value =
              `连接已停止响应（${Math.round(silentMs / 1000)}s 无数据），正在重连…预测仍在后台运行`
          },

          onInterrupted: (info) => {
            if (!isThisStream()) return
            _flushThinking()
            // 自动重连耗尽。后台图还在跑 → 保持 predicting 态，提供手动重连，
            // 绝不置 failed、绝不把浏览器原始错误串糊到用户脸上。
            isReconnecting.value = false
            canManualReconnect.value = info.recoverable
            connectionMsg.value = info.message
            if (!info.recoverable) {
              isPredicting.value = false
              streamingId.value = null
            }
          },
        },
        signal,
        afterEventId,
      )
    } catch (e: any) {
      // streamPrediction 内部已兜住绝大多数异常；这里是最后一道网（例如 handler 抛错、
      // fetch 在极早期 reject）。绝不能让它变成 unhandled rejection —— 那会把 UI
      // 永远钉在 predicting 且不给任何提示。
      if (!signal.aborted) {
        console.warn('[prediction] stream terminated unexpectedly', e)
        if (isThisStream()) {
          connectionMsg.value = '与服务器的连接意外中断（网络异常）。预测仍在后台运行，可点击重连继续。'
          canManualReconnect.value = true
          isReconnecting.value = false
        }
      }
    } finally {
      if (isThisStream()) _flushThinking()
      if (_predictAbort?.signal === signal) _predictAbort = null
    }
  }

  /** 手动重连（自动重连耗尽后由用户触发），用游标续传不丢进度 */
  async function reconnect() {
    const pid = streamingId.value || viewingId.value
    if (!pid || pid === PENDING_ID) return
    connectionMsg.value = '正在重连…'
    canManualReconnect.value = false
    await streamPredict(pid, _cursorFor(pid), { replay: true })
  }

  /** 停止预测：停的是「正在跑的那条」，不是「正在看的那条」 */
  async function stopPredict() {
    const pid = streamingId.value
    const connection = _predictAbort
    const key = admin.verifiedAdminKey.value
    if (pid && key) {
      try {
        // 后端停止：取消后台 graph_task + astream + LLM HTTP 流（不只是断 SSE）
        await api.stopPrediction(pid, key)
      } catch (e) {
        console.warn('stop prediction backend failed', e)
        if (_predictAbort === connection) connectionMsg.value = '尚未确认停止，请重试'
        return
      }
    }
    if (pid) patchRecord(pid, { status: 'stopped', progress: {} })
    if (_predictAbort !== connection) return
    _abortStream()
    _flushThinking()
    isPredicting.value = false
    streamingId.value = null
    isReconnecting.value = false
    canManualReconnect.value = false
    connectionMsg.value = ''
    progress.value = null
    predictionStage.value = '已停止'
    if (pid) {
      patchRecord(pid, { status: 'stopped', progress: {} })
      if (viewingId.value === pid && currentSnapshot.value) {
        currentSnapshot.value.status = 'stopped'
      }
    }
    void loadRecords()
  }

  /**
   * 落地「已暂停」态。onPaused 事件 与 pausePredict() 的 HTTP 返回都会走这里
   * （谁先到都行，幂等）。★ 不清空 newsDigest / filingDigest / candles ——
   * 暂停的语义就是「保留已完成维度」，擦掉就等于骗人。
   */
  function _applyPaused(pid: string) {
    if (streamingId.value && streamingId.value !== pid) {
      patchRecord(pid, { status: 'paused' })
      return
    }
    _flushThinking()
    _abortStream()
    isPredicting.value = false
    isReconnecting.value = false
    canManualReconnect.value = false
    connectionMsg.value = ''
    patchRecord(pid, { status: 'paused' })
    if (viewingId.value === pid) {
      predictionStage.value = predictionStatusLabel('paused')
      if (currentSnapshot.value) currentSnapshot.value.status = 'paused'
    }
  }

  /**
   * 暂停预测。诚实语义（后端无 checkpointer）：**取消在途任务 + 保留已完成维度 +
   * 可续跑剩余**。跑到一半的那个维度会丢，resume 时重跑 —— UI 文案必须这么写。
   */
  async function pausePredict() {
    const pid = streamingId.value || viewingId.value
    const key = admin.verifiedAdminKey.value
    if (!pid || pid === PENDING_ID || !key || isPausing.value) return
    isPausing.value = true
    try {
      await api.pausePrediction(pid, key)
      _applyPaused(pid)
      void loadRecords()
    } catch (e: any) {
      // 暂停失败不是预测失败：只提示，别把在跑的预测标成 failed
      connectionMsg.value = e?.message || '暂停失败，预测仍在后台运行'
    } finally {
      isPausing.value = false
    }
  }

  /**
   * 继续预测（只跑未完成的维度）。
   *
   * resume 后后台会开一条新的图任务 → 必须**重新连流**（暂停时旧流已终结）。
   * 游标传已收到的最大 eid：不重放已看过的几千条 thinking，也不会丢新事件。
   * 连流前先 loadSnapshot：把 DB 里已完成的维度/公式/蜡烛拉回来（游标续传不会重发它们）。
   */
  async function resumePredict() {
    const pid = viewingId.value || streamingId.value
    const key = admin.verifiedAdminKey.value
    if (!pid || pid === PENDING_ID || !key || isResuming.value) return
    isResuming.value = true
    try {
      const res = await api.resumePrediction(pid, key)
      patchRecord(pid, { status: (res.status || 'queued') as PredictionRecord['status'] })
      if (viewingId.value !== pid) return
      resetTransient()
      predictionStage.value = predictionStatusLabel(res.status || 'queued')
      patchRecord(pid, { status: (res.status || 'queued') as PredictionRecord['status'] })
      // 已完成的维度/公式/蜡烛在 DB 里，游标续传不会重发 → 先拉回来再连流
      const snap = await loadSnapshot(pid)
      if (!snap || viewingId.value !== pid) return
      if (isPredictionStreamable(snap.status)) {
        void streamPredict(pid, snap.event_cursor ?? 0, { hydrated: true })
      } else {
        isPredicting.value = false
      }
      void loadRecords()
    } catch (e: any) {
      if (viewingId.value === pid) errorMsg.value = e?.message || '继续预测失败'
    } finally {
      isResuming.value = false
    }
  }

  /** 删除预测记录（连带 event_log）。正在看这条 → 删完退回列表 */
  async function deletePredict(id: string) {
    const key = admin.verifiedAdminKey.value
    if (!id || !key) return
    if (streamingId.value === id) _abortStream()
    await api.deletePrediction(id, key)          // 失败向上抛：调用方弹 ElMessage.error
    records.value = records.value.filter(r => r.id !== id)
    if (viewingId.value === id) {
      viewingId.value = null
      currentSnapshot.value = null
      isPredicting.value = false
      resetTransient()
      _resetV2()
      newsDigest.value = []
      forecast.value = null
      thinking.value = ''
    }
    void loadRecords()
  }

  return {
    // state
    records,
    isPredicting,
    isViewingLive,
    isViewingPaused,
    isPausing,
    isResuming,
    predictionStage,
    progress,
    currentSnapshot,
    newsDigest,
    forecast,
    thinking,
    errorMsg,
    errorKind,
    connectionMsg,
    isReconnecting,
    canManualReconnect,
    viewingId,
    streamingId,
    // v2 state
    filingDigest,
    formulaSteps,
    forecastCandles,
    eventMarkers,
    toolTraces,
    modelParams,
    // actions
    loadRecords,
    checkActiveSession,
    startPredict,
    streamPredict,
    loadSnapshot,
    openDetail,
    backToList,
    reconnect,
    stopPredict,
    pausePredict,
    resumePredict,
    deletePredict,
  }
}
