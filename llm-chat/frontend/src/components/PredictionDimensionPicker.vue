<script setup lang="ts">
/**
 * 预测维度多选面板（设计文档 §3）。
 *
 * ★ 为什么不复用聊天的 clarification 工具：那个是 **LLM 判断意图不明时** 才触发的，
 *   且硬上限 4 项 / 8 选项（tools/builtin/clarification.py），而 v2 有 26 维；
 *   它的答案还会被拍平成自由文本再走一遍聊天全图。这里要的是**确定性 UI**：
 *   每次发起预测都弹、零 LLM 往返、直接产出 dimensions: string[] 给预测 API。
 *
 * ★ 维度解释文案（desc）**只从后端来**（GET /api/prediction/dimensions ←
 *   graph/prediction/dimensions.py 的 Dimension.desc）。本组件**不写任何维度文案**，
 *   否则必然与后端漂移。
 *
 * 用法（父组件在预测弹窗的第 2 步里嵌入；步骤 1 是选市场 + 输代码）：
 *   <PredictionDimensionPicker
 *     v-model="selectedDims"
 *     v-model:horizon="horizon"
 *     :market="predictMarket"
 *     :admin-key="admin.adminKey.value"
 *     @error="msg => ElMessage.warning(msg)"
 *   />
 * 父组件用 selectedDims 直接调 startPrediction({ symbol, market, horizon, dimensions: selectedDims })。
 */
import { ref, computed, watch, onMounted } from 'vue'
import { Refresh, Loading } from '@element-plus/icons-vue'
import { getPredictionDimensions } from '../api'
import type { Dimension, DimensionCatalog, PredictionMarket } from '../types'
import { dimensionSupports } from '../types'

const props = withDefaults(defineProps<{
  /** 已勾选的维度 key（v-model） */
  modelValue: string[]
  /** 当前市场 —— 决定哪些维度可见（A 股不显示 sec_filings） */
  market: PredictionMarket
  /** 预测周期（交易日，v-model:horizon）。用户拍板默认 7 */
  horizon?: number
  /** 管理员 key（预测 API 需要） */
  adminKey?: string
  /** 是否显示周期选择（父组件若自己管周期可关掉） */
  showHorizon?: boolean
  /** 只读（预测进行中不允许改） */
  disabled?: boolean
  /**
   * 父组件已经拿到过注册表时可直接注入，避免重复请求。
   * 不传则本组件在 mount / market 变化时自行拉取。
   */
  catalog?: DimensionCatalog | null
}>(), {
  horizon: 7,
  adminKey: '',
  showHorizon: true,
  disabled: false,
  catalog: null,
})

const emit = defineEmits<{
  (e: 'update:modelValue', keys: string[]): void
  (e: 'update:horizon', horizon: number): void
  /** 注册表加载完成（父组件可缓存，避免每次开弹窗都请求） */
  (e: 'loaded', catalog: DimensionCatalog): void
  /** 加载失败 —— 父组件决定怎么提示；本组件内部也会显示重试条 */
  (e: 'error', message: string): void
}>()

/** 可选周期档位（设计文档 §5.5：7 个交易日 ≈ 1.5 自然周） */
const HORIZON_OPTIONS = [5, 7, 10] as const

const catalog = ref<DimensionCatalog | null>(props.catalog)
const loading = ref(false)
const loadError = ref('')
const activeGroups = ref<string[]>([])

// ── 注册表加载 ───────────────────────────────────────────────────────────────

async function load(force = false) {
  // 父组件注入了注册表就不自己拉（除非强制刷新）
  if (!force && props.catalog) {
    applyCatalog(props.catalog)
    return
  }
  loading.value = true
  loadError.value = ''
  try {
    const data = await getPredictionDimensions(props.market, props.adminKey || undefined)
    applyCatalog(data)
    emit('loaded', data)
  } catch (e: any) {
    // 铁律 #9：不吞异常。加载失败要让用户看到并能重试 —— 拿不到维度就没法发起预测。
    const msg = e?.message || '维度列表加载失败'
    loadError.value = msg
    console.error('[PredictionDimensionPicker] 加载维度失败', e)
    emit('error', msg)
  } finally {
    loading.value = false
  }
}

/** 上一份注册表的默认勾选集 —— 用来判断用户到底动没动过选择 */
const prevDefaults = ref<string[] | null>(null)

function sameSet(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false
  const s = new Set(b)
  return a.every((k) => s.has(k))
}

function applyCatalog(data: DimensionCatalog) {
  catalog.value = data
  // 展开策略：含默认勾选维度的组默认展开；「进阶维度」（默认全关）折叠起来
  activeGroups.value = data.groups
    .filter((g) => g.keys.some((k) => data.defaults.includes(k)))
    .map((g) => g.name)

  const current = props.modelValue
  if (!current.length) {
    // 首次进入（父组件没给初值）→ 用后端默认勾选
    emit('update:modelValue', [...data.defaults])
  } else if (prevDefaults.value && sameSet(current, prevDefaults.value)) {
    // 用户没动过选择（还停在上一个市场的默认集）→ 跟随新市场的默认集。
    // 否则从 A 股切美股时，sec_filings（美股默认开）永远不会被勾上。
    emit('update:modelValue', [...data.defaults])
  } else {
    // 用户手动改过 → 尊重他的选择，只剔除不适用当前市场的维度
    pruneForMarket()
  }
  prevDefaults.value = [...data.defaults]
}

onMounted(load)

// 父组件后续注入了新的 catalog（如缓存命中）
watch(() => props.catalog, (c) => { if (c) applyCatalog(c) })

// 市场切换：重新拉该市场的维度（后端已按 market 过滤），并剔除不适用的已选项
watch(() => props.market, () => { load(true) })

// ── 维度视图 ─────────────────────────────────────────────────────────────────

const dimMap = computed<Record<string, Dimension>>(() => {
  const m: Record<string, Dimension> = {}
  for (const d of catalog.value?.dimensions ?? []) m[d.key] = d
  return m
})

/** 该市场可用的维度（双保险：后端已过滤，前端再按 markets 兜一层） */
const visibleDims = computed<Dimension[]>(() =>
  (catalog.value?.dimensions ?? []).filter((d) => dimensionSupports(d, props.market)),
)

const visibleKeys = computed(() => visibleDims.value.map((d) => d.key))

/** 分组（组名 + 该市场可见的 key）。空组不渲染 —— 选 A 股时「美股专属」整组消失 */
const groups = computed(() => {
  const vis = new Set(visibleKeys.value)
  return (catalog.value?.groups ?? [])
    .map((g) => ({ name: g.name, keys: g.keys.filter((k) => vis.has(k)) }))
    .filter((g) => g.keys.length > 0)
})

/** 只统计"可见"的已选项 —— 界面上看不见的东西不该被算进 17/26 */
const selected = computed(() => props.modelValue.filter((k) => visibleKeys.value.includes(k)))
const selectedCount = computed(() => selected.value.length)
const totalCount = computed(() => visibleKeys.value.length)

const defaultKeys = computed(() =>
  (catalog.value?.defaults ?? []).filter((k) => visibleKeys.value.includes(k)),
)

const searchCount = computed(
  () => selected.value.filter((k) => dimMap.value[k]?.kind === 'search').length,
)
const dataCount = computed(
  () => selected.value.filter((k) => dimMap.value[k]?.kind === 'data').length,
)

/** 一维搜索 ≈ 2 次 Tavily 调用（1 次 search + 1 次 extract）。数据型维度不消耗 Tavily 预算 */
const tavilyCalls = computed(() => searchCount.value * 2)

function isChecked(key: string): boolean {
  return props.modelValue.includes(key)
}

function toggle(key: string, checked: boolean) {
  if (props.disabled) return
  const next = checked
    ? (props.modelValue.includes(key) ? props.modelValue : [...props.modelValue, key])
    : props.modelValue.filter((k) => k !== key)
  emit('update:modelValue', next)
}

// ── 批量操作 ─────────────────────────────────────────────────────────────────

function selectAll() {
  if (props.disabled) return
  emit('update:modelValue', [...visibleKeys.value])
}

function selectNone() {
  if (props.disabled) return
  emit('update:modelValue', [])
}

function restoreDefaults() {
  if (props.disabled) return
  emit('update:modelValue', [...defaultKeys.value])
}

/** 组级全选/取消（半选状态由 indeterminate 表达） */
function groupChecked(keys: string[]): boolean {
  return keys.length > 0 && keys.every((k) => props.modelValue.includes(k))
}
function groupIndeterminate(keys: string[]): boolean {
  const hit = keys.filter((k) => props.modelValue.includes(k)).length
  return hit > 0 && hit < keys.length
}
function toggleGroup(keys: string[], checked: boolean) {
  if (props.disabled) return
  const set = new Set(props.modelValue)
  for (const k of keys) checked ? set.add(k) : set.delete(k)
  emit('update:modelValue', [...set])
}

/** 切市场后剔除不再适用的已选维度（否则会把 sec_filings 提交给 A 股，被后端 422 / 静默丢弃） */
function pruneForMarket() {
  if (!catalog.value) return
  const vis = new Set(visibleKeys.value)
  const kept = props.modelValue.filter((k) => vis.has(k))
  if (kept.length !== props.modelValue.length) emit('update:modelValue', kept)
}

watch(visibleKeys, () => pruneForMarket())

function setHorizon(h: number) {
  if (props.disabled) return
  emit('update:horizon', h)
}
</script>

<template>
  <div class="dim-picker">
    <!-- 加载中 -->
    <div v-if="loading && !catalog" class="dim-loading">
      <el-icon class="is-loading"><Loading /></el-icon>
      <span>正在加载维度列表…</span>
    </div>

    <!-- 加载失败：可重试（不吞异常） -->
    <el-alert
      v-else-if="loadError && !catalog"
      type="error"
      :closable="false"
      show-icon
      class="dim-error"
    >
      <template #title>维度列表加载失败</template>
      <div class="dim-error-body">
        <span class="dim-error-msg">{{ loadError }}</span>
        <el-button size="small" :icon="Refresh" @click="load(true)">重试</el-button>
      </div>
    </el-alert>

    <template v-else>
      <!-- 工具条：计数 + 批量操作 -->
      <div class="dim-toolbar">
        <div class="dim-count">
          已选 <b>{{ selectedCount }}</b> / {{ totalCount }}
        </div>
        <div class="dim-actions">
          <el-button size="small" text :disabled="disabled" @click="selectAll">全选</el-button>
          <el-button size="small" text :disabled="disabled" @click="selectNone">全不选</el-button>
          <el-button size="small" text :disabled="disabled" @click="restoreDefaults">恢复默认</el-button>
        </div>
      </div>

      <!-- 分组多选 -->
      <el-collapse v-model="activeGroups" class="dim-groups">
        <el-collapse-item
          v-for="g in groups"
          :key="g.name"
          :name="g.name"
        >
          <template #title>
            <div class="dim-group-title">
              <!-- 只拦住复选框自己的点击：@click.stop 若加在整行上，点标题就再也展不开/收不起了 -->
              <span class="dim-group-check" @click.stop>
                <el-checkbox
                  :model-value="groupChecked(g.keys)"
                  :indeterminate="groupIndeterminate(g.keys)"
                  :disabled="disabled"
                  @change="(v: any) => toggleGroup(g.keys, !!v)"
                />
              </span>
              <span class="dim-group-name">{{ g.name }}</span>
              <span class="dim-group-badge">
                {{ g.keys.filter((k) => isChecked(k)).length }} / {{ g.keys.length }}
              </span>
            </div>
          </template>

          <div class="dim-list">
            <label
              v-for="key in g.keys"
              :key="key"
              class="dim-item"
              :class="{ 'is-on': isChecked(key), 'is-disabled': disabled }"
            >
              <el-checkbox
                :model-value="isChecked(key)"
                :disabled="disabled"
                @change="(v: any) => toggle(key, !!v)"
              />
              <div class="dim-body">
                <div class="dim-head">
                  <span class="dim-label">{{ dimMap[key]?.label || key }}</span>
                  <!-- 数据型维度：把取数函数名当"工具名"亮出来（与 SSE tool_trace 的 tool 字段同名） -->
                  <el-tag
                    v-if="dimMap[key]?.kind === 'data'"
                    size="small"
                    type="success"
                    effect="plain"
                  >数据</el-tag>
                  <code v-if="dimMap[key]?.fetcher" class="dim-fetcher">{{ dimMap[key]?.fetcher }}()</code>
                </div>
                <!-- 解释文案：后端 Dimension.desc 单一真相源，前端不硬编码 -->
                <div class="dim-desc">{{ dimMap[key]?.desc }}</div>
              </div>
            </label>
          </div>
        </el-collapse-item>
      </el-collapse>

      <!-- 周期 + 成本预估 -->
      <div v-if="showHorizon" class="dim-horizon">
        <span class="dim-horizon-label">预测周期</span>
        <el-radio-group
          :model-value="horizon"
          :disabled="disabled"
          size="small"
          @change="(v: any) => setHorizon(Number(v))"
        >
          <el-radio-button v-for="h in HORIZON_OPTIONS" :key="h" :value="h">
            {{ h }} 交易日
          </el-radio-button>
        </el-radio-group>
        <span class="dim-horizon-note">未来 {{ horizon }} 个交易日（跳过周末，约 {{ Math.round(horizon * 1.4) }} 自然日）</span>
      </div>

      <div class="dim-estimate">
        资讯搜索 {{ searchCount }} 维（约 {{ tavilyCalls }} 次 Tavily 调用）· 结构化取数 {{ dataCount }} 维（不占 Tavily 预算）
        <span v-if="selectedCount === 0" class="dim-warn">· 至少选 1 个维度，否则后端会回退为默认维度</span>
      </div>
    </template>
  </div>
</template>

<style scoped>
.dim-picker {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.dim-loading {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 24px;
  justify-content: center;
  color: var(--cf-text-3);
  font-size: 13px;
}

.dim-error-body {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 4px;
}
.dim-error-msg {
  flex: 1;
  font-size: 12px;
  word-break: break-all;
}

.dim-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.dim-count {
  font-size: 13px;
  color: var(--cf-text-3);
}
.dim-count b {
  color: var(--cf-text-1);
  font-size: 15px;
}
.dim-actions {
  display: flex;
  gap: 2px;
}

.dim-groups {
  max-height: 46vh;
  overflow-y: auto;
  border-top: 1px solid var(--cf-border-soft);
  border-bottom: 1px solid var(--cf-border-soft);
}

.dim-group-title {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
}
.dim-group-check {
  display: inline-flex;
  align-items: center;
}
.dim-group-name {
  font-weight: 600;
  font-size: 13.5px;
  color: var(--cf-text-1);
}
.dim-group-badge {
  font-size: 12px;
  color: var(--cf-text-4);
  font-variant-numeric: tabular-nums;
}

.dim-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 2px 0 6px;
}

.dim-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid transparent;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s;
}
.dim-item:hover {
  background: var(--cf-bg-2);
}
.dim-item.is-on {
  background: var(--cf-bg-2);
  border-color: var(--cf-border-soft);
}
.dim-item.is-disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

.dim-body {
  flex: 1;
  min-width: 0;
}
.dim-head {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.dim-label {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--cf-text-1);
}
.dim-fetcher {
  font-family: var(--cf-font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 11px;
  color: var(--cf-text-4);
  background: var(--cf-bg-2);
  padding: 1px 5px;
  border-radius: 4px;
}
/* 维度解释：后端 Dimension.desc，直接上屏 */
.dim-desc {
  margin-top: 3px;
  font-size: 12px;
  line-height: 1.55;
  color: var(--cf-text-3);
}

.dim-horizon {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.dim-horizon-label {
  font-size: 13px;
  font-weight: 600;
  color: var(--cf-text-1);
}
.dim-horizon-note {
  font-size: 12px;
  color: var(--cf-text-4);
}

.dim-estimate {
  font-size: 12px;
  color: var(--cf-text-4);
  line-height: 1.6;
}
.dim-warn {
  color: #e6a23c;
}
</style>
