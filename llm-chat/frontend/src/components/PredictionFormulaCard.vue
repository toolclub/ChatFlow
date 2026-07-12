<script setup lang="ts">
/**
 * 公式推导卡片（设计文档 §5.7）。
 *
 * 消费 SSE `formula_step`（经 usePrediction.formulaSteps 归并）+ 落库的
 * `prediction_snapshots.formula_steps` —— 两条路径产出同一个数组，本组件不关心来源。
 *
 * ★ 用户拍板：**不引 KaTeX**。公式用等宽字体渲染纯文本 `formula`，
 *   `formula_latex` 后端下发但这里**故意不渲染**（P3 若要好看再上依赖）。
 *
 * 每张卡片回答三个问题（这是 v2 最实在的修复 —— v1 是"算完就扔"）：
 *   1. 公式长什么样        → formula（等宽）
 *   2. 参数是多少          → params 表格
 *   3. 代进去等于多少      → substituted + result（真实数字，不是符号）
 *   外加一句人话 explain。
 */
import { computed } from 'vue'
import { Loading } from '@element-plus/icons-vue'
import type { FormulaStep, ModelParams } from '../types'

const props = withDefaults(defineProps<{
  steps: FormulaStep[]
  /** 全参数（可选）：只用来在页脚展示蒙特卡洛/影线标定这类"非公式"的元信息 */
  modelParams?: ModelParams | null
  /** 推导仍在进行中 → 末尾显示"正在推导…"占位，屏幕上有东西在动 */
  running?: boolean
}>(), {
  modelParams: null,
  running: false,
})

/** 后端 key → 中文标题兜底（后端给了 title 就用后端的，唯一真相源仍在后端） */
const KEY_TITLES: Record<string, string> = {
  indicators: '技术指标',
  arima: 'ARIMA 漂移项',
  garch: 'GARCH(1,1) 条件波动率',
  event_score: '事件分（公告/定增/减持）',
  sentiment: '情绪融合',
  sentiment_total: '情绪融合',
  gbm: '几何布朗运动（蒙特卡洛）',
  monte_carlo: '蒙特卡洛路径模拟',
  shadow: '影线标定',
}

function stepTitle(s: FormulaStep): string {
  return s.title || KEY_TITLES[s.key] || s.key
}

const visibleSteps = computed(() => props.steps.filter(s => s && s.key))

/** params 是 Record<string, number|string>，表格化时保持后端下发的插入顺序 */
function paramEntries(s: FormulaStep): Array<[string, string]> {
  const p = s.params
  if (!p) return []
  return Object.entries(p).map(([k, v]) => [k, fmtVal(v)])
}

/**
 * 数值格式化：既不能把 2.1e-6 打成 0.00，也不能把 0.981 打成 9.81e-1。
 * 小数量级走科学计数法，常规量级保留 4 位有效小数。
 */
function fmtVal(v: unknown): string {
  if (v == null) return '—'
  if (typeof v === 'string') return v
  if (typeof v !== 'number' || !isFinite(v)) return String(v)
  const a = Math.abs(v)
  if (a !== 0 && (a < 1e-4 || a >= 1e7)) return v.toExponential(2)
  if (Number.isInteger(v)) return String(v)
  return v.toFixed(4)
}

function resultText(s: FormulaStep): string {
  if (s.result_text) return s.result_text
  if (s.result != null) return fmtVal(s.result)
  return ''
}

/** 蒙特卡洛/影线这类元信息只有拿到 model_params 才显示，拿不到就整块不渲染（不编数字） */
const mcMeta = computed(() => {
  const mp = props.modelParams
  if (!mp) return null
  const paths = mp.mc_n_paths
  if (!paths) return null
  return {
    paths,
    percentiles: (mp.mc_percentiles || []).join(' / '),
    dt: mp.mc_dt,
    kUp: mp.shadow_k_up,
    kDn: mp.shadow_k_dn,
    bars: mp.shadow_lookback_bars,
  }
})
</script>

<template>
  <div v-if="visibleSteps.length || running" class="formula-card">
    <div class="fc-head">
      <span class="fc-title">推导过程</span>
      <span class="fc-meta">
        每一步都给出公式、参数与代入后的实际数值，模型不是黑箱
      </span>
    </div>

    <div class="fc-steps">
      <div v-for="(s, i) in visibleSteps" :key="s.key" class="fc-step">
        <div class="fc-step-head">
          <span class="fc-index">{{ i + 1 }}</span>
          <span class="fc-step-title">{{ stepTitle(s) }}</span>
          <code class="fc-key">{{ s.key }}</code>
        </div>

        <!-- 1. 公式（符号态，等宽） -->
        <pre v-if="s.formula" class="fc-formula">{{ s.formula }}</pre>

        <!-- 2. 参数表 -->
        <div v-if="paramEntries(s).length" class="fc-params">
          <div v-for="[k, v] in paramEntries(s)" :key="k" class="fc-param">
            <span class="fc-param-k">{{ k }}</span>
            <span class="fc-param-v">{{ v }}</span>
          </div>
        </div>

        <!-- 3. 代入实际数值 -->
        <pre v-if="s.substituted" class="fc-formula fc-substituted">{{ s.substituted }}</pre>

        <div v-if="resultText(s)" class="fc-result">
          <span class="fc-result-label">结果</span>
          <span class="fc-result-value">{{ resultText(s) }}</span>
        </div>

        <p v-if="s.explain" class="fc-explain">{{ s.explain }}</p>
      </div>

      <!-- 推导中占位：后端逐步推 formula_step，卡片一张张弹出 -->
      <div v-if="running" class="fc-step fc-pending">
        <el-icon class="fc-spin"><Loading /></el-icon>
        <span>正在推导…</span>
      </div>
    </div>

    <!-- 元信息（蒙特卡洛设定 + 影线标定），只在后端真给了 model_params 时显示 -->
    <div v-if="mcMeta" class="fc-foot">
      <div class="fc-foot-row">
        <span class="fc-foot-k">蒙特卡洛</span>
        <span class="fc-foot-v">
          {{ mcMeta.paths.toLocaleString() }} 条路径 · 分位数 {{ mcMeta.percentiles }} · dt = {{ fmtVal(mcMeta.dt) }}
        </span>
      </div>
      <div class="fc-foot-row">
        <span class="fc-foot-k">影线标定</span>
        <span class="fc-foot-v">
          k_up = {{ fmtVal(mcMeta.kUp) }} · k_dn = {{ fmtVal(mcMeta.kDn) }}
          <template v-if="mcMeta.bars">（近 {{ mcMeta.bars }} 根历史 K 线均值）</template>
        </span>
      </div>
      <p class="fc-honest">
        预测蜡烛的影线由历史平均影线比例合成，<b>不代表真实日内极值分布</b>。
      </p>
    </div>
  </div>
</template>

<style scoped>
.formula-card {
  background: var(--cf-card);
  border: 1px solid var(--cf-border-soft);
  border-radius: 12px;
  padding: 16px 20px;
}
.fc-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}
.fc-title {
  font-size: 14px;
  font-weight: 700;
  color: var(--cf-text-2);
}
.fc-meta {
  font-size: 11px;
  color: var(--cf-text-4);
}
.fc-steps {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.fc-step {
  border: 1px solid var(--cf-border-soft);
  border-left: 3px solid #00AEEC;
  border-radius: 8px;
  padding: 12px 14px;
  background: var(--cf-bg-2);
  animation: fc-in 0.25s ease-out;
}
@keyframes fc-in {
  from { opacity: 0; transform: translateY(6px); }
  to { opacity: 1; transform: none; }
}
.fc-step-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.fc-index {
  width: 18px;
  height: 18px;
  flex-shrink: 0;
  border-radius: 50%;
  background: #00AEEC;
  color: #fff;
  font-size: 11px;
  font-weight: 700;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.fc-step-title {
  font-size: 13px;
  font-weight: 700;
  color: var(--cf-text-1);
}
.fc-key {
  margin-left: auto;
  font-size: 10px;
  color: var(--cf-text-4);
  font-family: ui-monospace, SFMono-Regular, monospace;
}
.fc-formula {
  margin: 0 0 8px;
  padding: 8px 10px;
  background: var(--cf-card);
  border: 1px solid var(--cf-border-soft);
  border-radius: 6px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--cf-text-2);
  white-space: pre-wrap;
  word-break: break-word;
  overflow-x: auto;
}
.fc-substituted {
  border-color: rgba(0, 174, 236, 0.35);
  color: #00AEEC;
}
.fc-params {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}
.fc-param {
  display: inline-flex;
  align-items: baseline;
  gap: 5px;
  padding: 2px 8px;
  border-radius: 5px;
  background: var(--cf-card);
  border: 1px solid var(--cf-border-soft);
  font-size: 11.5px;
}
.fc-param-k {
  color: var(--cf-text-4);
  font-family: ui-monospace, SFMono-Regular, monospace;
}
.fc-param-v {
  font-weight: 700;
  color: var(--cf-text-1);
  font-family: ui-monospace, SFMono-Regular, monospace;
  font-variant-numeric: tabular-nums;
}
.fc-result {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 6px;
}
.fc-result-label {
  font-size: 11px;
  color: var(--cf-text-4);
}
.fc-result-value {
  font-size: 14px;
  font-weight: 700;
  color: #00B578;
  font-family: ui-monospace, SFMono-Regular, monospace;
  font-variant-numeric: tabular-nums;
}
.fc-explain {
  margin: 0;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--cf-text-3);
}
.fc-pending {
  display: flex;
  align-items: center;
  gap: 8px;
  border-left-color: var(--cf-border-soft);
  color: var(--cf-text-4);
  font-size: 12px;
}
.fc-spin {
  animation: cf-spin 1s linear infinite;
  color: #00AEEC;
}
@keyframes cf-spin {
  to { transform: rotate(360deg); }
}
.fc-foot {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px dashed var(--cf-border-soft);
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.fc-foot-row {
  display: flex;
  gap: 10px;
  font-size: 11.5px;
}
.fc-foot-k {
  min-width: 60px;
  color: var(--cf-text-4);
}
.fc-foot-v {
  color: var(--cf-text-3);
  font-family: ui-monospace, SFMono-Regular, monospace;
}
.fc-honest {
  margin: 6px 0 0;
  font-size: 11px;
  line-height: 1.6;
  color: #E6A23C;
}
</style>
