<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onUnmounted } from 'vue'
import { usePrediction } from '../composables/usePrediction'
import { useAdmin } from '../composables/useAdmin'
import PredictionDisclaimer from './PredictionDisclaimer.vue'
import PredictionChart from './PredictionChart.vue'
import PredictionFormulaCard from './PredictionFormulaCard.vue'
import PredictionDimensionPicker from './PredictionDimensionPicker.vue'
import {
  TrendCharts, ChatLineRound, Warning, Aim, Money, Suitcase, Refresh,
  Connection, Loading, VideoPause, VideoPlay, Delete, Document, Link,
} from '@element-plus/icons-vue'
import type {
  PredictionStatus, PredictionRecord, PredictionMarket,
  DimensionCatalog, FilingItem, FilingDimensionResult,
} from '../types'
import {
  PREDICTION_STATUS_PERCENT,
  MARKET_DATA_ERROR_HINTS,
  FILING_CONFIDENCE_TAG,
  predictionStatusLabel,
  predictionProgressText,
  hasPredictionProgress,
  isPredictionTerminal,
  isPredictionActive,
  isPredictionPaused,
  filingConfidenceLabel,
  predictionEventTypeLabel,
  predictionEventTypeColor,
} from '../types'

const emit = defineEmits<{
  (e: 'switch-workspace', workspace: 'chat' | 'quant'): void
  (e: 'continue-with-prediction', predictionId: string): void
}>()

const admin = useAdmin()
const pred = usePrediction()

// ── 发起预测：两步弹窗（步骤 1 选市场+输代码 → 步骤 2 勾维度）───────────────────
const showPredictDialog = ref(false)
const dialogStep = ref<1 | 2>(1)
const predictMarket = ref<PredictionMarket>('cn_a')
const symbolInput = ref('')
const submitting = ref(false)

// 维度选择（默认集由 PredictionDimensionPicker 从后端注册表拉，前端不硬编码维度）
const selectedDims = ref<string[]>([])
const horizon = ref(7)                                  // 用户拍板：默认 7 个交易日
/** 注册表缓存：开一次弹窗拉一次就够了，避免每次开都请求 */
const dimCatalog = ref<DimensionCatalog | null>(null)

function openPredictDialog() {
  dialogStep.value = 1
  showPredictDialog.value = true
}

function nextStep() {
  if (!symbolInput.value.trim()) {
    import('element-plus').then(m => m.ElMessage.warning('请输入标的代码'))
    return
  }
  dialogStep.value = 2
}

async function submitPredict() {
  const sym = symbolInput.value.trim()
  if (!sym) {
    dialogStep.value = 1
    return
  }
  if (!selectedDims.value.length) {
    import('element-plus').then(m => m.ElMessage.warning('请至少勾选一个分析维度'))
    return
  }
  submitting.value = true
  showPredictDialog.value = false
  await pred.startPredict({
    symbol: sym,
    market: predictMarket.value,
    horizon: horizon.value,
    dimensions: selectedDims.value,
  })
  submitting.value = false
}

function onCatalogLoaded(cat: DimensionCatalog) {
  dimCatalog.value = cat
}

// 交互面板由 composable 的 viewingId 驱动 —— checkActiveSession 恢复会话时
// 能直接把面板打开并定位到那条记录（本地 ref 做不到，那正是"隐形流式会话"的根因）
const showInteraction = computed(() => pred.viewingId.value !== null)

// 密码输入（未验证时）
const adminKeyInput = ref('')
const verifying = ref(false)

async function doVerify() {
  verifying.value = true
  const ok = await admin.verify(adminKeyInput.value)
  verifying.value = false
  if (ok) {
    adminKeyInput.value = ''
    // 拉列表 + 恢复活跃会话由 watch(admin.isVerified) 统一处理（侧边栏解锁也走同一条路）
  } else {
    import('element-plus').then(m => m.ElMessage.error('密钥验证失败'))
  }
}

/**
 * 解锁来源有两处：本页门禁 doVerify，以及侧边栏/其它页面的管理员验证。
 * 只在 onMounted 里拉数据会漏掉后者 —— 正在跑的预测被孤儿化，列表空白最多 30 秒。
 */
watch(
  () => admin.isVerified.value,
  async (verified) => {
    if (!verified) return
    await pred.loadRecords()
    await pred.checkActiveSession()
  },
)

// 点开记录：运行中会自动重连 SSE 续流（openDetail 内处理；paused 不连流）
async function openDetail(id: string) {
  await pred.openDetail(id)
}

function backToList() {
  pred.backToList()
}

function continueInChat() {
  if (!pred.currentSnapshot.value?.id) return
  emit('continue-with-prediction', pred.currentSnapshot.value.id)
  emit('switch-workspace', 'chat')
}

// ── 暂停 / 继续 / 删除 ────────────────────────────────────────────────────────
//
// 暂停的诚实语义（后端无 checkpointer）：取消在途任务 + 保留已完成维度 + 续跑剩余。
// 跑到一半的那个维度会丢、resume 时重跑 —— 二次确认里必须把这句写给用户看。
async function doPause() {
  const m = await import('element-plus')
  try {
    await m.ElMessageBox.confirm(
      '暂停会取消正在进行的取数/推理任务，但已完成的维度结果会保留，点「继续」时只跑剩余维度。'
        + '注意：正在跑到一半的那个维度会被丢弃，继续时重跑。',
      '暂停预测',
      { confirmButtonText: '暂停', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return                                   // 用户点了取消
  }
  await pred.pausePredict()
}

async function doResume() {
  await pred.resumePredict()
}

async function doDelete(id: string, symbol?: string) {
  const m = await import('element-plus')
  try {
    await m.ElMessageBox.confirm(
      `确定删除 ${symbol || id} 的预测记录？该记录的推理过程、资讯与公告结果会一并删除，不可恢复。`,
      '删除预测记录',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await pred.deletePredict(id)
    m.ElMessage.success('已删除')
  } catch (e: any) {
    m.ElMessage.error(e?.message || '删除失败')
  }
}

// ── 状态着色（中文标签统一走 types 的唯一真相源，含 stopped / paused）──
function statusTagType(status: PredictionStatus): 'info' | 'warning' | 'primary' | 'success' | 'danger' {
  switch (status) {
    case 'queued': return 'info'
    case 'news_searching': return 'warning'
    case 'inferring': return 'primary'
    case 'paused': return 'warning'
    case 'done': return 'success'
    case 'failed': return 'danger'
    case 'stopped': return 'info'
    default: return 'info'
  }
}

function statusText(status?: PredictionStatus | string | null): string {
  return predictionStatusLabel(status) || '-'
}

function sentimentColor(s?: number): string {
  if (s == null) return 'var(--cf-text-4)'
  if (s > 0.2) return '#F25D59'
  if (s < -0.2) return '#00B578'
  return 'var(--cf-text-3)'
}

function sentimentLabel(s?: number): string {
  if (s == null) return '—'
  if (s > 0.2) return '偏多'
  if (s < -0.2) return '偏空'
  return '中性'
}

function formatTime(ts: number): string {
  if (!ts) return '-'
  return new Date(ts * 1000).toLocaleString()
}

function formatNum(v: any, dec = 2): string {
  if (v == null || isNaN(v)) return '-'
  return Number(v).toFixed(dec)
}

// ── 技术指标展开（修 [object Object] bug）──────────────────────────────────────
//
// factors/prediction_indicators.py 返回的 macd / boll / kdj 是**嵌套 dict**
// （{dif,dea,hist} / {upper,mid,lower} / {k,d,j}）。老代码 `{{ val }}` 直接插值
// 会渲染成字面量 [object Object]。这里拍平成 (label, value) 列表。
const INDICATOR_LABELS: Record<string, string> = {
  ma5: 'MA5', ma10: 'MA10', ma20: 'MA20', ma60: 'MA60',
  rsi: 'RSI', rsi6: 'RSI6', rsi14: 'RSI14',
  last_close: '最新收盘', atr: 'ATR', vol: '成交量',
  'macd.dif': 'MACD DIF', 'macd.dea': 'MACD DEA', 'macd.hist': 'MACD 柱',
  'boll.upper': 'BOLL 上轨', 'boll.mid': 'BOLL 中轨', 'boll.lower': 'BOLL 下轨',
  'kdj.k': 'KDJ K', 'kdj.d': 'KDJ D', 'kdj.j': 'KDJ J',
}

function indicatorLabel(path: string): string {
  return INDICATOR_LABELS[path] || path.replace(/\./g, ' ').toUpperCase()
}

const rawIndicators = computed<Record<string, unknown> | null>(() => {
  const v =
    (pred.currentSnapshot.value?.technical_indicators as Record<string, unknown> | undefined) ||
    (pred.forecast.value?.technical_indicators as Record<string, unknown> | undefined)
  return v && typeof v === 'object' ? v : null
})

/** 拍平后的指标格子：嵌套 dict 展开成 macd.dif / boll.upper / … */
const indicatorCells = computed<Array<{ path: string; label: string; value: string }>>(() => {
  const src = rawIndicators.value
  if (!src) return []
  const out: Array<{ path: string; label: string; value: string }> = []
  const push = (path: string, val: unknown) => {
    if (val == null) return
    if (typeof val === 'number') {
      if (!isFinite(val)) return                          // NaN（历史不足）不上屏，不显示 "NaN"
      out.push({ path, label: indicatorLabel(path), value: formatNum(val) })
    } else if (typeof val === 'string' || typeof val === 'boolean') {
      out.push({ path, label: indicatorLabel(path), value: String(val) })
    }
  }
  for (const [k, v] of Object.entries(src)) {
    if (v && typeof v === 'object' && !Array.isArray(v)) {
      for (const [sk, sv] of Object.entries(v as Record<string, unknown>)) {
        push(`${k}.${sk}`, sv)                            // ← 嵌套 dict 展开，不再 [object Object]
      }
    } else {
      push(k, v)
    }
  }
  return out
})

// ── 进度：细粒度阶段 > 状态机阶段 ────────────────────────────────────────────
//
// 老实现按状态机把百分比钉死在 40%（资讯搜索）——那正是"搜索时像卡住了"的观感来源：
// 12 个维度、数分钟，进度条一格不动。现在按 progress.index/total 实时推进。
const progressPercent = computed(() => {
  const st = pred.currentSnapshot.value?.status
  if (isPredictionTerminal(st)) return 100

  const p = pred.progress.value
  if (hasPredictionProgress(p)) {
    const frac = p.total && p.total > 0 ? Math.min(1, Math.max(0, (p.index ?? 0) / p.total)) : 0
    switch (p.phase) {
      case 'preflight': return 8
      case 'news_searching': return Math.round(15 + 45 * frac)   // 15% → 60%
      case 'inferring': return Math.round(65 + 30 * frac)        // 65% → 95%
    }
  }
  if (st) return PREDICTION_STATUS_PERCENT[st as PredictionStatus] ?? 0
  return pred.isPredicting.value ? 5 : 0
})

// 进度文案："正在搜索：地缘政治 (3/12)" > 状态机中文标签
const progressText = computed(() => {
  const detail = predictionProgressText(pred.progress.value)
  if (detail) return detail
  return pred.predictionStage.value || statusText(pred.currentSnapshot.value?.status)
})

const isRunning = computed(() => pred.isViewingLive.value)
const isPaused = computed(() => pred.isViewingPaused.value)

// ── 已用时计时器（每秒跳，让屏幕上始终有东西在动）──
const nowMs = ref(Date.now())
let _tickTimer: number | null = null

const elapsedText = computed(() => {
  const snap = pred.currentSnapshot.value
  if (!isRunning.value || !snap?.created_at) return ''
  const secs = Math.max(0, Math.floor(nowMs.value / 1000 - snap.created_at))
  const h = Math.floor(secs / 3600)
  const m = Math.floor((secs % 3600) / 60)
  const s = secs % 60
  const pad = (n: number) => String(n).padStart(2, '0')
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`
})

// 资讯搜索骨架屏：已知总维度数、还没回来的那些先占位，避免大片空白
const newsSkeletonCount = computed(() => {
  const p = pred.progress.value
  if (!isRunning.value || !hasPredictionProgress(p) || p.phase !== 'news_searching') return 0
  const total = p.total ?? 0
  const done = pred.newsDigest.value.length + pred.filingDigest.value.length
  return Math.max(0, Math.min(12, total - done))
})

// ── 失败原因（可行动的提示）──
const errorHint = computed(() => {
  const kind = pred.errorKind.value
  return kind ? MARKET_DATA_ERROR_HINTS[kind] : ''
})

// ── v2 数据卡片 ──────────────────────────────────────────────────────────────
/** 单维最多先展示 6 条，其余折叠（平安银行 1733 条公告不能一次铺满屏） */
const FILING_PREVIEW = 6
const expandedDims = ref<Set<string>>(new Set())

function toggleDim(dim: string) {
  const s = new Set(expandedDims.value)
  if (s.has(dim)) s.delete(dim)
  else s.add(dim)
  expandedDims.value = s
}

function visibleItems(d: FilingDimensionResult): FilingItem[] {
  const items = d.items || []
  return expandedDims.value.has(d.dimension) ? items : items.slice(0, FILING_PREVIEW)
}

function confidenceTag(c?: string | null): 'success' | 'warning' | 'info' {
  if (!c) return 'info'
  return FILING_CONFIDENCE_TAG[c as keyof typeof FILING_CONFIDENCE_TAG] ?? 'info'
}

/**
 * 判定失败的兜底（confidence=estimated + negative_score=0）**必须**显式说"未计分"。
 * 绝不能渲染成"无风险"—— 那是拿"没判出来"冒充"判出来是安全的"。
 */
function confidenceHint(item: FilingItem): string {
  if (item.confidence === 'estimated') return '判定失败，未计入事件分'
  if (item.confidence === 'llm_inferred') return '模型据标题推断，未解析公告正文'
  if (item.confidence === 'structured') return '来自结构化字段，非模型推断'
  return ''
}

/** 工具调用日志：一行一次 fetcher 调用（start 建行、end 补完） */
function traceArgsText(args?: Record<string, unknown>): string {
  if (!args) return ''
  return Object.entries(args)
    .map(([k, v]) => `${k}=${v}`)
    .join(', ')
}

// ── 流式思考面板自动滚底（但不抢用户的滚动）──
const thinkingRef = ref<HTMLElement | null>(null)
const thinkingAutoScroll = ref(true)

function onThinkingScroll() {
  const el = thinkingRef.value
  if (!el) return
  // 用户手动上滚 → 暂停自动滚；滚回底部附近 → 恢复
  const distToBottom = el.scrollHeight - el.scrollTop - el.clientHeight
  thinkingAutoScroll.value = distToBottom < 32
}

watch(
  () => pred.thinking.value,
  async () => {
    if (!thinkingAutoScroll.value) return
    await nextTick()
    const el = thinkingRef.value
    if (el) el.scrollTop = el.scrollHeight
  },
)

// 切换记录时重置滚动跟随 + 数据卡片展开态
watch(() => pred.viewingId.value, () => {
  thinkingAutoScroll.value = true
  expandedDims.value = new Set()
})

// ── 列表行进度（运行中的记录在列表里也要看得出在飞）──
function rowProgressText(row: PredictionRecord): string {
  if (!isPredictionActive(row.status)) return ''
  return predictionProgressText(row.progress)
}

// ── 列表自动刷新 ────────────────────────────────────────────────────────────
//
// 老实现 `!pred.isPredicting` —— 恰好在最需要刷新的时候（有任务在跑）把刷新关了。
// 现在：有活跃记录 → 10s 刷；全是终态 → 30s 刷。
const REFRESH_TICK_MS = 10_000
let _refreshTimer: number | null = null
let _idleTicks = 0

function hasActive(): boolean {
  return pred.isPredicting.value || pred.records.value.some(r => isPredictionActive(r.status))
}

onMounted(async () => {
  if (admin.isVerified.value) {
    await pred.loadRecords()
    await pred.checkActiveSession()
  }

  _tickTimer = window.setInterval(() => { nowMs.value = Date.now() }, 1_000) as unknown as number

  _refreshTimer = window.setInterval(() => {
    if (!admin.isVerified.value) return
    if (hasActive()) {
      _idleTicks = 0
      void pred.loadRecords()
      return
    }
    _idleTicks += 1
    if (_idleTicks >= 3) {          // 30s
      _idleTicks = 0
      void pred.loadRecords()
    }
  }, REFRESH_TICK_MS) as unknown as number
})

onUnmounted(() => {
  if (_refreshTimer !== null) clearInterval(_refreshTimer)
  if (_tickTimer !== null) clearInterval(_tickTimer)
})
</script>

<template>
  <div class="prediction-view">
    <!-- 未验证：密码门禁 -->
    <div v-if="!admin.isVerified.value" class="admin-gate">
      <div class="gate-card">
        <div class="gate-title">
          <el-icon color="#FB7299"><TrendCharts /></el-icon>
          <span>股票预测 · 管理员验证</span>
        </div>
        <p class="gate-desc">该功能需管理员密钥解锁，与智能选股共享同一会话。</p>
        <el-input
          v-model="adminKeyInput"
          type="password"
          placeholder="请输入管理密钥..."
          show-password
          @keydown.enter="doVerify"
        />
        <el-button
          type="primary"
          class="bili-btn-primary"
          :loading="verifying"
          @click="doVerify"
          style="margin-top: 12px;"
        >验证并进入</el-button>
      </div>
    </div>

    <!-- 已验证：主视图 -->
    <template v-else>
      <!-- 交互面板 -->
      <div v-if="showInteraction" class="interaction-panel">
        <div class="interaction-header">
          <div class="header-left">
            <el-button class="back-btn" :icon="Refresh" circle @click="backToList" />
            <span class="header-title">
              {{ pred.currentSnapshot.value?.symbol }} 预测详情
            </span>
            <el-tag v-if="pred.currentSnapshot.value" :type="statusTagType(pred.currentSnapshot.value.status)" size="small">
              {{ statusText(pred.currentSnapshot.value.status) }}
            </el-tag>
            <span v-if="isRunning && elapsedText" class="elapsed-badge">
              <el-icon class="spin"><Loading /></el-icon>
              已用时 {{ elapsedText }}
            </span>
          </div>
          <div class="header-right">
            <el-button
              v-if="isRunning"
              :icon="VideoPause"
              :loading="pred.isPausing.value"
              @click="doPause"
            >暂停</el-button>
            <el-button
              v-if="isPaused"
              type="primary"
              class="bili-btn-primary"
              :icon="VideoPlay"
              :loading="pred.isResuming.value"
              @click="doResume"
            >继续</el-button>
            <el-button
              v-if="isRunning"
              type="danger"
              @click="pred.stopPredict()"
            >停止</el-button>
            <el-button
              v-if="pred.currentSnapshot.value?.status === 'done'"
              type="primary"
              class="bili-btn-primary"
              :icon="ChatLineRound"
              @click="continueInChat"
            >接入对话深挖</el-button>
          </div>
        </div>

        <div class="interaction-body">
          <PredictionDisclaimer />

          <!-- 进度条：细粒度阶段 + 实时百分比 + 计时器（长时间资讯搜索不再"像卡住"） -->
          <div class="progress-section">
            <div class="progress-head">
              <div class="progress-label">
                <el-icon v-if="isRunning" class="spin inline-spin"><Loading /></el-icon>
                {{ progressText }}
              </div>
              <div v-if="isRunning && elapsedText" class="progress-elapsed">已用时 {{ elapsedText }}</div>
            </div>
            <el-progress
              :percentage="progressPercent"
              :status="pred.currentSnapshot.value?.status === 'failed' ? 'exception' : undefined"
              :stroke-width="10"
              :color="[
                { color: '#909399', percentage: 30 },
                { color: '#E6A23C', percentage: 50 },
                { color: '#00AEEC', percentage: 80 },
                { color: '#00B578', percentage: 100 },
              ]"
            />
          </div>

          <!-- 已暂停：这不是失败、也不是断线。明确告诉用户"保留了什么、继续会做什么" -->
          <div v-if="isPaused" class="paused-banner">
            <el-icon><VideoPause /></el-icon>
            <div class="paused-body">
              <div class="paused-title">预测已暂停</div>
              <div class="paused-hint">
                已完成的维度结果已保留。点「继续」只会跑剩余维度；被中断的那个维度会重跑。
              </div>
            </div>
            <el-button
              type="primary"
              size="small"
              :icon="VideoPlay"
              :loading="pred.isResuming.value"
              @click="doResume"
            >继续</el-button>
          </div>

          <!-- 连接中断/重连（这是网络问题，不是预测失败 —— 两条横幅严格分开） -->
          <div v-if="pred.connectionMsg.value" class="conn-banner">
            <el-icon :class="{ spin: pred.isReconnecting.value }"><Connection /></el-icon>
            <span class="conn-text">{{ pred.connectionMsg.value }}</span>
            <el-button
              v-if="pred.canManualReconnect.value"
              size="small"
              type="warning"
              text
              @click="pred.reconnect()"
            >手动重连</el-button>
          </div>

          <!-- 预测失败原因（后端会给出「数据源限流」「网络不通」这类可行动的原因） -->
          <div v-if="pred.errorMsg.value" class="error-banner">
            <el-icon><Warning /></el-icon>
            <div class="error-body">
              <div class="error-msg">{{ pred.errorMsg.value }}</div>
              <div v-if="errorHint" class="error-hint">{{ errorHint }}</div>
            </div>
          </div>

          <!-- ── 工具调用日志（消费 tool_trace）：终端风格，实时滚动 ── -->
          <div v-if="pred.toolTraces.value.length" class="tools-section">
            <div class="section-title">
              <el-icon><Link /></el-icon> 取数工具调用
              <span class="section-meta">{{ pred.toolTraces.value.length }} 次</span>
            </div>
            <div class="tool-log">
              <div v-for="t in pred.toolTraces.value" :key="t.key" class="tool-row">
                <el-icon v-if="!t.done" class="spin tool-icon running"><Loading /></el-icon>
                <span v-else class="tool-icon" :class="t.ok === false ? 'failed' : 'ok'">
                  {{ t.ok === false ? '✕' : '✓' }}
                </span>
                <code class="tool-call">{{ t.tool }}({{ traceArgsText(t.args) }})</code>
                <span v-if="!t.done" class="tool-result pending">调用中…</span>
                <span v-else-if="t.ok === false" class="tool-result failed">
                  {{ t.error || t.summary || '调用失败' }}
                </span>
                <span v-else class="tool-result">
                  {{ t.summary || `返回 ${t.rows ?? 0} 条` }}
                  <template v-if="t.elapsed_ms != null"> · {{ (t.elapsed_ms / 1000).toFixed(1) }}s</template>
                </span>
              </div>
            </div>
          </div>

          <!-- ── 结构化披露数据卡片（消费 data_item）：一张张实时出现 ── -->
          <div v-if="pred.filingDigest.value.length" class="filing-section">
            <div class="section-title">
              <el-icon><Document /></el-icon> 结构化披露数据
              <span class="section-meta">公告 / 定增 / 减持 / SEC filing</span>
            </div>

            <div
              v-for="d in pred.filingDigest.value"
              :key="d.dimension"
              class="filing-dim"
            >
              <div class="filing-dim-head">
                <span class="filing-dim-name">{{ d.dimension }}</span>
                <el-tag v-if="d.failed" type="danger" size="small" effect="plain">取数失败</el-tag>
                <span v-else-if="d.score != null" class="filing-score" :style="{ color: d.score < -0.01 ? '#F25D59' : 'var(--cf-text-4)' }">
                  事件分 {{ formatNum(d.score) }}
                </span>
                <span class="filing-stats">
                  <template v-if="d.total_fetched != null">拉取 {{ d.total_fetched }} 条</template>
                  <template v-if="d.candidates != null"> · 候选 {{ d.candidates }}</template>
                  <template v-if="d.llm_judged != null"> · LLM 精判 {{ d.llm_judged }}</template>
                  <template v-if="d.cache_hits"> · 命中缓存 {{ d.cache_hits }}</template>
                </span>
              </div>

              <p v-if="d.failed && d.error" class="filing-error">{{ d.error }}</p>
              <p v-else-if="d.summary" class="filing-summary">{{ d.summary }}</p>

              <!-- ★ 口径声明（后端 source_note）：用户拍板必须原样透出，不许省 -->
              <p v-if="d.source_note" class="filing-note">口径：{{ d.source_note }}</p>

              <div v-if="d.items?.length" class="filing-cards">
                <div
                  v-for="(item, i) in visibleItems(d)"
                  :key="`${d.dimension}-${i}-${item.date}-${item.title}`"
                  class="filing-card"
                  :style="{ borderLeftColor: predictionEventTypeColor(item.event_type) }"
                >
                  <div class="fi-head">
                    <a v-if="item.url" :href="item.url" target="_blank" class="fi-title">{{ item.title }}</a>
                    <span v-else class="fi-title">{{ item.title }}</span>
                  </div>

                  <div class="fi-tags">
                    <span class="fi-date">{{ item.date }}</span>
                    <el-tag v-if="item.ann_type" size="small" effect="plain">{{ item.ann_type }}</el-tag>
                    <el-tag
                      size="small"
                      effect="dark"
                      :style="{ background: predictionEventTypeColor(item.event_type), border: 'none' }"
                    >{{ predictionEventTypeLabel(item.event_type) }}</el-tag>

                    <!-- ★★ 置信度标注（用户拍板：诚实标注，不假装精确）——
                         structured=硬数据(实心) / llm_inferred=模型推断 / estimated=判定失败未计分 -->
                    <el-tooltip :content="confidenceHint(item)" placement="top" :disabled="!confidenceHint(item)">
                      <el-tag
                        size="small"
                        :type="confidenceTag(item.confidence)"
                        :effect="item.confidence === 'structured' ? 'dark' : 'plain'"
                        class="fi-confidence"
                      >{{ filingConfidenceLabel(item.confidence) }}</el-tag>
                    </el-tooltip>

                    <span
                      v-if="item.negative_score != null && item.negative_score < 0"
                      class="fi-score"
                    >负面度 {{ formatNum(item.negative_score) }}</span>
                    <span v-else-if="item.confidence === 'estimated'" class="fi-score muted">未计分</span>
                  </div>

                  <p v-if="item.reason" class="fi-reason">{{ item.reason }}</p>

                  <div class="fi-meta">
                    <span class="fi-source">来源：{{ item.source }}</span>
                    <span v-if="item.dilution_ratio != null">摊薄 {{ formatNum(item.dilution_ratio * 100) }}%</span>
                    <span v-if="item.discount_rate != null">折价 {{ formatNum(item.discount_rate * 100) }}%</span>
                    <span v-if="item.reduction_ratio != null">减持 {{ formatNum(item.reduction_ratio * 100) }}%</span>
                    <span v-if="item.decay_weight != null">时间权重 {{ formatNum(item.decay_weight) }}</span>
                  </div>
                </div>
              </div>

              <el-button
                v-if="(d.items?.length ?? 0) > FILING_PREVIEW"
                text
                size="small"
                class="filing-more"
                @click="toggleDim(d.dimension)"
              >
                {{ expandedDims.has(d.dimension)
                  ? '收起'
                  : `展开其余 ${(d.items?.length ?? 0) - FILING_PREVIEW} 条` }}
              </el-button>
            </div>
          </div>

          <!-- 资讯维度列表（搜索中：已回来的实时铺，没回来的用骨架屏占位） -->
          <div v-if="pred.newsDigest.value.length || newsSkeletonCount" class="news-section">
            <div class="section-title">
              <el-icon><Aim /></el-icon> 资讯维度分析
              <span v-if="newsSkeletonCount" class="section-meta">
                {{ pred.newsDigest.value.length + pred.filingDigest.value.length }} / {{ pred.progress.value?.total }} 维已完成
              </span>
            </div>
            <div class="news-grid">
              <div v-for="dim in pred.newsDigest.value" :key="dim.dimension" class="news-dim-card">
                <div class="dim-header">
                  <span class="dim-name">{{ dim.dimension }}</span>
                  <el-tag v-if="dim.failed" type="danger" size="small" effect="plain">失败</el-tag>
                  <span v-else class="dim-sentiment" :style="{color: sentimentColor(dim.sentiment)}">
                    {{ sentimentLabel(dim.sentiment) }} {{ dim.sentiment != null ? dim.sentiment.toFixed(2) : '' }}
                  </span>
                </div>
                <p v-if="dim.failed && dim.error" class="dim-reason">{{ dim.error }}</p>
                <p v-if="dim.answer" class="dim-answer">{{ dim.answer }}</p>
                <p v-if="dim.sentiment_reason" class="dim-reason">{{ dim.sentiment_reason }}</p>
                <div v-if="dim.results?.length" class="dim-results">
                  <a v-for="(r, i) in dim.results.slice(0, 3)" :key="i" :href="r.url" target="_blank" class="news-link">
                    {{ r.title }}
                  </a>
                </div>
              </div>
              <!-- 骨架屏：搜索中的维度（屏幕上始终有东西在动，不是一片空白） -->
              <div v-for="n in newsSkeletonCount" :key="`sk-${n}`" class="news-dim-card skeleton-card">
                <el-skeleton :rows="2" animated />
              </div>
            </div>
          </div>

          <!-- 公式推导卡片（消费 formula_step；刷新后由落库的 formula_steps 重建） -->
          <PredictionFormulaCard
            v-if="pred.formulaSteps.value.length || (isRunning && pred.progress.value?.phase === 'inferring')"
            :steps="pred.formulaSteps.value"
            :model-params="pred.modelParams.value"
            :running="isRunning && pred.progress.value?.phase === 'inferring'"
          />

          <!-- 技术指标摘要（macd/boll/kdj 是嵌套 dict，已拍平 —— 不再 [object Object]） -->
          <div v-if="indicatorCells.length" class="indicators-section">
            <div class="section-title"><el-icon><TrendCharts /></el-icon> 技术指标</div>
            <div class="indicators-grid">
              <div v-for="cell in indicatorCells" :key="cell.path" class="indicator-cell">
                <span class="ind-label">{{ cell.label }}</span>
                <span class="ind-value">{{ cell.value }}</span>
              </div>
            </div>
          </div>

          <!-- 预测图表（历史/预测/蜡烛任一有数据即渲染；全空时图表内部走空态，不炸） -->
          <div
            v-if="pred.currentSnapshot.value && (pred.currentSnapshot.value.historical_data || pred.forecast.value || pred.currentSnapshot.value.forecast || pred.forecastCandles.value.length)"
            class="chart-section"
          >
            <PredictionChart
              :symbol="pred.currentSnapshot.value.symbol"
              :historical-data="pred.currentSnapshot.value.historical_data"
              :forecast-data="pred.forecast.value || pred.currentSnapshot.value.forecast"
              :forecast-candles="pred.forecastCandles.value"
              :event-markers="pred.eventMarkers.value"
              :technical-indicators="rawIndicators || undefined"
              :inflection-points="pred.currentSnapshot.value.inflection_points || pred.forecast.value?.inflection_points"
              :empty-hint="pred.errorMsg.value"
            />
          </div>

          <!-- 流式思考面板（自动滚底，用户上滚时让位）—— 放在结论之后，默认折叠不抢视线 -->
          <details v-if="pred.thinking.value" class="thinking-block">
            <summary>
              <el-icon><ChatLineRound /></el-icon>
              推理过程
              <span class="thinking-len">({{ pred.thinking.value.length }} 字)</span>
              <span v-if="!thinkingAutoScroll" class="thinking-paused">已暂停自动滚动</span>
            </summary>
            <pre
              ref="thinkingRef"
              class="thinking-text"
              @scroll="onThinkingScroll"
            >{{ pred.thinking.value }}</pre>
          </details>

          <!-- 分析文本 + 风险标签 -->
          <div v-if="pred.currentSnapshot.value?.analysis" class="analysis-section">
            <div class="section-title"><el-icon><ChatLineRound /></el-icon> 综合分析</div>
            <p class="analysis-text">{{ pred.currentSnapshot.value.analysis }}</p>
            <div v-if="pred.currentSnapshot.value.risk_notes?.length" class="risk-tags">
              <el-tag
                v-for="note in pred.currentSnapshot.value.risk_notes"
                :key="note"
                type="danger"
                effect="light"
                size="small"
                class="risk-tag"
              >
                <el-icon><Warning /></el-icon> {{ note }}
              </el-tag>
            </div>
          </div>
        </div>
      </div>

      <!-- 记录列表 -->
      <div v-else class="list-view">
        <div class="quant-header">
          <div class="header-left">
            <el-icon class="header-icon"><TrendCharts /></el-icon>
            <span class="header-title">股票预测工作台</span>
            <span class="header-subtitle">Forecast</span>
          </div>
          <div class="header-right">
            <el-button
              type="primary"
              class="bili-btn-primary"
              :icon="TrendCharts"
              :loading="submitting"
              @click="openPredictDialog"
            >预测</el-button>
          </div>
        </div>

        <div class="list-container">
          <PredictionDisclaimer />

          <div class="table-card">
            <div class="card-header">
              <span>预测记录 (共 {{ pred.records.value.length }} 条)</span>
              <el-button :icon="Refresh" text @click="pred.loadRecords()">刷新</el-button>
            </div>
            <el-table
              v-if="pred.records.value.length > 0"
              :data="pred.records.value"
              style="width: 100%"
              header-cell-class-name="bili-table-header"
              row-class-name="bili-table-row"
            >
              <el-table-column label="时间" width="160">
                <template #default="scope">{{ formatTime(scope.row.created_at) }}</template>
              </el-table-column>
              <el-table-column label="标的" width="140">
                <template #default="scope">
                  <div class="stock-info">
                    <span class="stock-name">{{ scope.row.name || scope.row.symbol }}</span>
                    <span class="stock-symbol">{{ scope.row.symbol }}</span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="市场" width="80">
                <template #default="scope">
                  <el-tag size="small" :type="scope.row.market === 'cn_a' ? 'primary' : 'warning'">
                    {{ scope.row.market === 'cn_a' ? 'A股' : '美股' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="状态 / 进度" width="230">
                <template #default="scope">
                  <div class="status-cell">
                    <el-tag :type="statusTagType(scope.row.status)" size="small">
                      <el-icon v-if="isPredictionActive(scope.row.status)" class="spin tag-spin"><Loading /></el-icon>
                      {{ statusText(scope.row.status) }}
                    </el-tag>
                    <!-- 运行中的记录在列表里也看得出在飞（后端落库的 progress 字段） -->
                    <span v-if="rowProgressText(scope.row)" class="row-progress">
                      {{ rowProgressText(scope.row) }}
                    </span>
                    <span v-else-if="isPredictionPaused(scope.row.status)" class="row-paused">
                      已保留已完成维度，可继续
                    </span>
                    <span
                      v-else-if="scope.row.status === 'failed' && scope.row.error_message"
                      class="row-error"
                      :title="scope.row.error_message"
                    >{{ scope.row.error_message }}</span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="预测方向" width="110">
                <template #default="scope">
                  <span v-if="scope.row.predicted_direction" :style="{color: scope.row.predicted_direction === 'up' ? '#F25D59' : scope.row.predicted_direction === 'down' ? '#00B578' : 'var(--cf-text-3)'}">
                    {{ scope.row.predicted_direction === 'up' ? '↑ 看涨' : scope.row.predicted_direction === 'down' ? '↓ 看跌' : '→ 震荡' }}
                  </span>
                  <span v-else>-</span>
                </template>
              </el-table-column>
              <el-table-column label="置信区间" width="180">
                <template #default="scope">
                  <span v-if="scope.row.predicted_range?.length === 2">
                    {{ formatNum(scope.row.predicted_range[0]) }} ~ {{ formatNum(scope.row.predicted_range[1]) }}
                    <span v-if="scope.row.confidence != null" class="conf-meta"> ({{ (scope.row.confidence * 100).toFixed(0) }}%)</span>
                  </span>
                  <span v-else>-</span>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="170" align="center">
                <template #default="scope">
                  <el-button type="primary" text size="small" @click="openDetail(scope.row.id)">查看详情</el-button>
                  <el-button
                    type="danger"
                    text
                    size="small"
                    :icon="Delete"
                    @click.stop="doDelete(scope.row.id, scope.row.symbol)"
                  >删除</el-button>
                </template>
              </el-table-column>
            </el-table>
            <div v-else class="empty-layout">
              <el-empty :image-size="120" description="暂无预测记录，点击右上角「预测」开始" />
            </div>
          </div>
        </div>
      </div>
    </template>

    <!-- 预测弹窗：两步 —— ① 市场 + 代码　② 分析维度（26 维，后端下发解释） -->
    <el-dialog
      v-model="showPredictDialog"
      :title="dialogStep === 1 ? '发起股票预测 · 选择标的' : '发起股票预测 · 分析维度'"
      :width="dialogStep === 1 ? '440px' : '720px'"
      top="6vh"
      center
    >
      <!-- 步骤 1 -->
      <div v-if="dialogStep === 1" class="predict-form">
        <div class="form-item">
          <label>市场</label>
          <el-radio-group v-model="predictMarket">
            <el-radio value="cn_a"><el-icon><Money /></el-icon> A股</el-radio>
            <el-radio value="us_stock"><el-icon><Suitcase /></el-icon> 美股</el-radio>
          </el-radio-group>
        </div>
        <div class="form-item">
          <label>标的代码</label>
          <el-input
            v-model="symbolInput"
            :placeholder="predictMarket === 'cn_a' ? '如 000001.SZ / 平安银行' : '如 AAPL'"
            clearable
            @keydown.enter="nextStep"
          />
        </div>
      </div>

      <!-- 步骤 2：维度多选（组件自己拉后端注册表；desc 全部来自后端，前端零硬编码） -->
      <div v-else class="dim-step">
        <PredictionDimensionPicker
          v-model="selectedDims"
          v-model:horizon="horizon"
          :market="predictMarket"
          :admin-key="admin.verifiedAdminKey.value"
          :catalog="dimCatalog"
          @loaded="onCatalogLoaded"
          @error="msg => import('element-plus').then(m => m.ElMessage.warning(msg))"
        />
      </div>

      <template #footer>
        <template v-if="dialogStep === 1">
          <el-button @click="showPredictDialog = false">取消</el-button>
          <el-button type="primary" class="bili-btn-primary" @click="nextStep">下一步：选维度</el-button>
        </template>
        <template v-else>
          <el-button @click="dialogStep = 1">上一步</el-button>
          <el-button
            type="primary"
            class="bili-btn-primary"
            :loading="submitting"
            :disabled="!selectedDims.length"
            @click="submitPredict"
          >开始预测（{{ selectedDims.length }} 维 / {{ horizon }} 个交易日）</el-button>
        </template>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.prediction-view {
  flex: 1;
  display: flex;
  flex-direction: column;
  background: var(--cf-bg-2);
  border-radius: var(--cf-radius-lg);
  overflow: hidden;
  height: 100%;
}

/* ── 密码门禁 ── */
.admin-gate {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}
.gate-card {
  width: 360px;
  background: var(--cf-card);
  border: 1px solid var(--cf-border-soft);
  border-radius: 16px;
  padding: 32px 28px;
  box-shadow: var(--cf-shadow-md);
}
.gate-title {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 18px;
  font-weight: 700;
  color: var(--cf-text-1);
  margin-bottom: 8px;
}
.gate-desc {
  font-size: 12px;
  color: var(--cf-text-4);
  margin-bottom: 20px;
}

/* ── Header ── */
.quant-header {
  height: 60px;
  background: var(--cf-card);
  border-bottom: 1px solid var(--cf-border-soft);
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0 24px;
  flex-shrink: 0;
}
.header-left {
  display: flex;
  align-items: center;
  gap: 16px;
}
.header-icon {
  font-size: 24px;
  color: #00AEEC;
}
.header-title {
  font-size: 18px;
  font-weight: 700;
  color: var(--cf-text-1);
}
.header-subtitle {
  font-size: 11px;
  color: #FB7299;
  background: rgba(251, 114, 153, 0.1);
  padding: 1px 6px;
  border-radius: 4px;
  font-weight: 600;
}
.header-right {
  display: flex;
  align-items: center;
  gap: 12px;
}

.bili-btn-primary {
  background: #00AEEC !important;
  border: none !important;
  font-weight: 600;
  border-radius: 20px !important;
}
.bili-btn-primary:hover {
  background: #0095CC !important;
}

.back-btn {
  background: var(--cf-bg-2);
  border: none;
}
.back-btn:hover {
  background: #00AEEC;
  color: #fff;
}

/* ── 列表视图 ── */
.list-container {
  flex: 1;
  padding: 20px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.table-card {
  background: var(--cf-card);
  border-radius: 12px;
  border: 1px solid var(--cf-border-soft);
  overflow: hidden;
}
.card-header {
  padding: 12px 20px;
  border-bottom: 1px solid var(--cf-border-soft);
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-weight: 700;
  font-size: 15px;
}
.stock-info {
  display: flex;
  flex-direction: column;
}
.stock-name {
  font-weight: 600;
  color: var(--cf-text-1);
}
.stock-symbol {
  font-size: 11px;
  color: var(--cf-text-4);
  font-family: monospace;
}
.conf-meta {
  color: var(--cf-text-4);
  font-size: 11px;
}
.status-cell {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
}
.row-progress {
  font-size: 11px;
  color: #00AEEC;
  line-height: 1.4;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 210px;
}
.row-paused {
  font-size: 11px;
  color: #E6A23C;
  line-height: 1.4;
}
.row-error {
  font-size: 11px;
  color: #F25D59;
  line-height: 1.4;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 210px;
}
.empty-layout {
  padding: 40px 0;
  display: flex;
  align-items: center;
  justify-content: center;
}

/* ── 交互面板 ── */
.interaction-panel {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.interaction-header {
  height: 60px;
  background: var(--cf-card);
  border-bottom: 1px solid var(--cf-border-soft);
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0 24px;
  flex-shrink: 0;
}
.interaction-body {
  flex: 1;
  padding: 20px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.progress-section {
  background: var(--cf-card);
  border: 1px solid var(--cf-border-soft);
  border-radius: 12px;
  padding: 16px 20px;
}
.progress-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  margin-bottom: 10px;
}
.progress-label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 14px;
  font-weight: 600;
  color: var(--cf-text-2);
}
.progress-elapsed {
  font-size: 12px;
  color: var(--cf-text-4);
  font-variant-numeric: tabular-nums;
  font-family: ui-monospace, SFMono-Regular, monospace;
}

/* 运行中动效：屏幕上始终有东西在动 */
.spin {
  animation: cf-spin 1s linear infinite;
}
@keyframes cf-spin {
  to { transform: rotate(360deg); }
}
.inline-spin {
  color: #00AEEC;
}
.tag-spin {
  margin-right: 3px;
}
.elapsed-badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  color: var(--cf-text-4);
  font-variant-numeric: tabular-nums;
}

/* 已暂停（橙色，非失败非断线） */
.paused-banner {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  background: rgba(230, 162, 60, 0.08);
  border: 1px solid rgba(230, 162, 60, 0.35);
  border-radius: 10px;
  color: #E6A23C;
}
.paused-body {
  flex: 1;
}
.paused-title {
  font-size: 13px;
  font-weight: 700;
}
.paused-hint {
  font-size: 11.5px;
  color: var(--cf-text-3);
  line-height: 1.5;
  margin-top: 2px;
}

/* 连接中断 / 重连（网络问题，橙色；与红色的"预测失败"严格区分） */
.conn-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  background: rgba(230, 162, 60, 0.08);
  border: 1px solid rgba(230, 162, 60, 0.35);
  border-radius: 10px;
  color: #E6A23C;
  font-size: 13px;
}
.conn-text {
  flex: 1;
}

.error-banner {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 10px 14px;
  background: rgba(242, 93, 89, 0.08);
  border: 1px solid rgba(242, 93, 89, 0.3);
  border-radius: 10px;
  color: #F25D59;
  font-size: 13px;
}
.error-body {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.error-msg {
  font-weight: 600;
  line-height: 1.5;
  word-break: break-word;
}
.error-hint {
  font-size: 12px;
  color: var(--cf-text-3);
  line-height: 1.5;
}

.thinking-block {
  padding: 10px 14px;
  background: var(--cf-bg-2);
  border: 1px solid var(--cf-border-soft);
  border-radius: 10px;
  font-size: 12px;
  color: var(--cf-text-3);
}
.thinking-block summary {
  cursor: pointer;
  user-select: none;
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
  color: var(--cf-text-2);
}
.thinking-len {
  margin-left: 4px;
  color: var(--cf-text-4);
  font-weight: 400;
}
.thinking-paused {
  margin-left: auto;
  font-size: 11px;
  font-weight: 400;
  color: #E6A23C;
}
.thinking-text {
  margin: 10px 0 0;
  padding: 10px;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: ui-monospace, SFMono-Regular, monospace;
  font-size: 12px;
  line-height: 1.6;
  color: var(--cf-text-3);
  background: var(--cf-card);
  border-radius: 6px;
  max-height: 320px;
  overflow-y: auto;
}

.section-title {
  font-size: 14px;
  font-weight: 700;
  color: var(--cf-text-2);
  margin-bottom: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.section-meta {
  font-size: 11px;
  font-weight: 400;
  color: var(--cf-text-4);
}

/* ── 工具调用日志（终端风格） ── */
.tool-log {
  background: var(--cf-card);
  border: 1px solid var(--cf-border-soft);
  border-radius: 10px;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 5px;
  max-height: 220px;
  overflow-y: auto;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11.5px;
}
.tool-row {
  display: flex;
  align-items: center;
  gap: 8px;
  line-height: 1.6;
}
.tool-icon {
  width: 14px;
  flex-shrink: 0;
  text-align: center;
  font-weight: 700;
}
.tool-icon.ok { color: #00B578; }
.tool-icon.failed { color: #F25D59; }
.tool-icon.running { color: #00AEEC; }
.tool-call {
  color: var(--cf-text-2);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 60%;
}
.tool-result {
  color: var(--cf-text-4);
  margin-left: auto;
  white-space: nowrap;
}
.tool-result.pending { color: #00AEEC; }
.tool-result.failed {
  color: #F25D59;
  white-space: normal;
}

/* ── 结构化披露数据卡片 ── */
.filing-dim {
  background: var(--cf-card);
  border: 1px solid var(--cf-border-soft);
  border-radius: 10px;
  padding: 12px 14px;
  margin-bottom: 12px;
}
.filing-dim-head {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 6px;
}
.filing-dim-name {
  font-size: 13px;
  font-weight: 700;
  color: var(--cf-text-1);
}
.filing-score {
  font-size: 12px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.filing-stats {
  margin-left: auto;
  font-size: 11px;
  color: var(--cf-text-4);
  font-family: ui-monospace, SFMono-Regular, monospace;
}
.filing-summary {
  margin: 0 0 6px;
  font-size: 12px;
  color: var(--cf-text-2);
  line-height: 1.6;
}
.filing-error {
  margin: 0 0 6px;
  font-size: 12px;
  color: #F25D59;
  line-height: 1.6;
}
/* 口径声明：诚实标注，必须显眼 */
.filing-note {
  margin: 0 0 10px;
  padding: 6px 8px;
  font-size: 11px;
  line-height: 1.5;
  color: #E6A23C;
  background: rgba(230, 162, 60, 0.07);
  border-left: 2px solid rgba(230, 162, 60, 0.5);
  border-radius: 4px;
}
.filing-cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 10px;
}
.filing-card {
  border: 1px solid var(--cf-border-soft);
  border-left: 3px solid #909399;
  border-radius: 8px;
  padding: 10px 12px;
  background: var(--cf-bg-2);
  animation: fi-in 0.25s ease-out;
}
@keyframes fi-in {
  from { opacity: 0; transform: translateY(6px); }
  to { opacity: 1; transform: none; }
}
.fi-head {
  margin-bottom: 6px;
}
.fi-title {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--cf-text-1);
  line-height: 1.5;
  text-decoration: none;
  display: block;
}
a.fi-title:hover {
  color: #00AEEC;
  text-decoration: underline;
}
.fi-tags {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 5px;
  margin-bottom: 6px;
}
.fi-date {
  font-size: 11px;
  color: var(--cf-text-4);
  font-family: ui-monospace, SFMono-Regular, monospace;
}
.fi-confidence {
  font-weight: 700;
}
.fi-score {
  font-size: 11px;
  font-weight: 700;
  color: #F25D59;
  font-variant-numeric: tabular-nums;
}
.fi-score.muted {
  color: var(--cf-text-4);
}
.fi-reason {
  margin: 0 0 6px;
  font-size: 11.5px;
  color: var(--cf-text-3);
  line-height: 1.55;
}
.fi-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  font-size: 10.5px;
  color: var(--cf-text-4);
  font-family: ui-monospace, SFMono-Regular, monospace;
}
.fi-source {
  color: var(--cf-text-4);
}
.filing-more {
  margin-top: 8px;
}

/* 资讯维度 */
.news-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 12px;
}
.news-dim-card {
  background: var(--cf-card);
  border: 1px solid var(--cf-border-soft);
  border-radius: 10px;
  padding: 12px 14px;
}
.skeleton-card {
  opacity: 0.6;
}
.dim-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
}
.dim-name {
  font-size: 13px;
  font-weight: 700;
  color: var(--cf-text-1);
}
.dim-sentiment {
  font-size: 12px;
  font-weight: 600;
}
.dim-answer {
  font-size: 12px;
  color: var(--cf-text-2);
  line-height: 1.5;
  margin: 0 0 4px;
}
.dim-reason {
  font-size: 11px;
  color: var(--cf-text-4);
  margin: 0 0 6px;
}
.dim-results {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.news-link {
  font-size: 11px;
  color: #00AEEC;
  text-decoration: none;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.news-link:hover {
  text-decoration: underline;
}

/* 技术指标 */
.indicators-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
  gap: 10px;
}
.indicator-cell {
  display: flex;
  flex-direction: column;
  background: var(--cf-card);
  border: 1px solid var(--cf-border-soft);
  border-radius: 8px;
  padding: 8px 12px;
}
.ind-label {
  font-size: 11px;
  color: var(--cf-text-4);
}
.ind-value {
  font-size: 15px;
  font-weight: 700;
  color: var(--cf-text-1);
  font-family: monospace;
}

.analysis-section {
  background: var(--cf-card);
  border: 1px solid var(--cf-border-soft);
  border-radius: 12px;
  padding: 16px 20px;
}
.analysis-text {
  font-size: 14px;
  line-height: 1.7;
  color: var(--cf-text-2);
  margin: 0 0 12px;
  white-space: pre-wrap;
}
.risk-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.risk-tag {
  display: flex;
  align-items: center;
  gap: 4px;
}

.chart-section {
  background: var(--cf-card);
  border: 1px solid var(--cf-border-soft);
  border-radius: 12px;
  padding: 4px;
}

/* ── 预测弹窗 ── */
.predict-form {
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.form-item label {
  display: block;
  font-size: 12px;
  color: var(--cf-text-3);
  margin-bottom: 8px;
}
.dim-step {
  max-height: 62vh;
  overflow-y: auto;
}

/* ── Table Overrides ── */
:deep(.bili-table-header) {
  background-color: var(--cf-bg-2) !important;
  color: var(--cf-text-3);
  font-weight: 600;
  font-size: 13px;
}
</style>
