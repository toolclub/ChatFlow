<script setup lang="ts">
import { onMounted, onUnmounted } from 'vue'
import { useQuant } from '../composables/useQuant'
import { ChatLineRound, Filter, DataAnalysis, Warning, TrendCharts } from '@element-plus/icons-vue'

const quant = useQuant()

let _statusTimer: number | null = null

onMounted(() => {
  quant.loadProviders()
  quant.loadCacheStatus()
  quant.checkActiveSession()
  // 智能预热：进入页面自动触发，后端已实现 5min 冷却保护
  quant.refreshCacheNow()
  // 每 30s 刷新一次缓存徽标
  _statusTimer = window.setInterval(() => quant.loadCacheStatus(), 30_000) as unknown as number
})

onUnmounted(() => {
  if (_statusTimer !== null) clearInterval(_statusTimer)
})

const emit = defineEmits<{
  (e: 'switch-workspace', workspace: 'chat' | 'quant'): void
  // 把 snapshot_id 抛给 App.vue（App 持有规范的 chat 实例，useChat 非单例不能在子组件里 send）
  (e: 'continue-with-snapshot', snapshotId: string): void
  (e: 'open-stock-detail', stock: any): void
}>()

function continueInChat() {
  const snapId = quant.screenResult.value?.snapshot_id
  if (!snapId) return
  emit('continue-with-snapshot', snapId)
  emit('switch-workspace', 'chat')
}

function getScoreColor(score: number): string {
  if (score >= 70) return '#FB7299'
  if (score >= 60) return '#00AEEC'
  return '#9499A0'
}
</script>

<template>
  <div class="quant-view">
    <!-- Header -->
    <div class="quant-header">
      <div class="header-left">
        <el-icon class="header-icon"><TrendCharts /></el-icon>
        <span class="header-title">量化选股工作台</span>
        <span class="header-subtitle">Alpha 1.0</span>
      </div>
      <div class="header-right">
        <!-- 缓存徽标：spot 数据时效 + 手动刷新 -->
        <span class="cache-badge" :class="{ stale: !quant.cacheStatus.value?.spot_latest }">
          <span class="cache-dot" :class="{ running: quant.cacheStatus.value?.warmer_running }"></span>
          数据更新于 {{ quant.cacheAgeText.value }}
          <span v-if="quant.cacheStatus.value" class="cache-meta">
            · spot {{ quant.cacheStatus.value.spot_files }} 份
            · bars {{ quant.cacheStatus.value.bars_files }} 天
            · {{ quant.cacheStatus.value.total_mb }}MB
          </span>
        </span>
        <el-button
          v-if="quant.screenResult.value"
          type="primary"
          class="bili-btn-primary"
          :icon="ChatLineRound"
          @click="continueInChat"
        >
          接入对话深挖
        </el-button>
      </div>
    </div>
    
    <div class="quant-container">
      <!-- Sidebar Filters -->
      <div class="filter-sidebar">
        <div class="filter-section">
          <div class="section-title">
            <el-icon><Filter /></el-icon> 核心条件
          </div>
          
          <div class="filter-item">
            <label>目标市场 (Market)</label>
            <el-select v-model="quant.criteria.value.market" size="default" class="bili-select">
              <el-option label="中国 A 股" value="cn_a" />
              <el-option label="美国股票" value="us_stock" />
            </el-select>
          </div>

          <div class="filter-item">
            <label>股票池 (Universe)</label>
            <el-select v-model="quant.criteria.value.universe" size="default" class="bili-select">
              <template v-if="quant.criteria.value.market === 'cn_a'">
                <el-option label="全市场 (A股)" value="all" />
                <el-option label="沪深300" value="hs300" />
                <el-option label="中证500" value="zz500" />
              </template>
              <template v-else>
                <el-option label="全市场 (US)" value="all" />
                <el-option label="纳斯达克 (Nasdaq)" value="nasdaq" />
                <el-option label="标普500 (S&P 500)" value="sp500" />
              </template>
            </el-select>
          </div>

          <div class="filter-item">
            <label>最低市值 ({{ quant.criteria.value.market === 'cn_a' ? '亿元' : '亿美元' }})</label>
            <el-input-number v-model="quant.criteria.value.min_market_cap" :min="0" :step="100" class="bili-input-number" />
          </div>

          <div class="filter-item" v-if="quant.criteria.value.market === 'cn_a'">
            <el-checkbox v-model="quant.criteria.value.exclude_st" label="剔除 ST / 退市标的" />
          </div>

          <div class="filter-item">
            <label>PE 范围 (动态)</label>
            <div class="range-inputs">
              <el-input-number v-model="quant.criteria.value.pe_range[0]" :min="-100" :max="1000" size="small" />
              <span class="range-sep">-</span>
              <el-input-number v-model="quant.criteria.value.pe_range[1]" :min="-100" :max="1000" size="small" />
            </div>
          </div>
        </div>

        <div class="filter-section">
          <div class="section-title">
            <el-icon><DataAnalysis /></el-icon> 因子权重
          </div>
          <div class="weight-item" v-for="(val, key) in quant.criteria.value.weights" :key="key">
            <div class="weight-label">
              <span>{{ {technical:'技术', fundamental:'基本', liquidity:'流动', risk:'风险'}[key] }}</span>
              <span class="weight-val">{{ (val * 100).toFixed(0) }}%</span>
            </div>
            <el-slider v-model="quant.criteria.value.weights[key]" :min="0" :max="1" :step="0.05" :show-tooltip="false" />
          </div>
        </div>
        
        <div class="sidebar-actions">
          <el-button 
            type="primary" 
            class="run-btn"
            :loading="quant.isScreening.value" 
            @click="quant.runScreen"
          >
            {{ quant.isScreening.value ? '计算因子中...' : '开始筛选' }}
          </el-button>
          <div v-if="quant.errorMsg.value" class="error-text">
            {{ quant.errorMsg.value }}
          </div>
        </div>
      </div>

      <!-- Main Result Area -->
      <!-- 渲染优先级：loading（哪怕已有旧结果也压在上面）→ result → empty -->
      <div class="main-content">
        <div v-if="quant.isScreening.value" class="loading-layout">
          <div class="bili-loading-box">
            <div class="bili-kaomoji">(｡･ω･｡)</div>
            <div class="bili-loading-text">{{ quant.screenStage.value || '正在筛选...' }}</div>
            <div class="bili-loading-hint">
              首次冷启可能需要 1-2 分钟，后台会把数据写盘缓存，下次秒回。
            </div>
          </div>
        </div>
        <div v-else-if="quant.screenResult.value" class="result-layout">
          
          <!-- LLM Analysis Card -->
          <div class="analysis-card">
            <div class="card-header">
              <el-icon color="#00AEEC"><ChatLineRound /></el-icon>
              <span>AI 选股洞察</span>
              <span v-if="quant.isAnalyzing.value" class="analyze-pulse">分析中…</span>
            </div>
            <div class="analysis-body">
              <p class="analysis-text">
                {{ quant.screenResult.value.analysis
                   || (quant.isAnalyzing.value ? '正在生成深度分析...' : '') }}
              </p>
              <div class="risk-tags" v-if="quant.screenResult.value.risk_notes?.length">
                <el-tag
                  v-for="note in quant.screenResult.value.risk_notes"
                  :key="note"
                  type="danger"
                  effect="light"
                  class="risk-tag"
                  size="small"
                >
                  <el-icon><Warning /></el-icon> {{ note }}
                </el-tag>
              </div>
            </div>
          </div>

          <!-- Result Table Card -->
          <div class="table-card">
            <div class="card-header">
              <span>筛选结果 (Top {{ quant.screenResult.value.rows.length }})</span>
              <span class="as-of">数据截至: {{ quant.screenResult.value.as_of_date }}</span>
            </div>
            
            <el-table 
              :data="quant.screenResult.value.rows" 
              style="width: 100%" 
              header-cell-class-name="bili-table-header"
              row-class-name="bili-table-row"
            >
              <el-table-column prop="rank" label="#" width="60" align="center" />
              <el-table-column label="名称 / 代码" width="160">
                <template #default="scope">
                  <div class="stock-info" @click="emit('open-stock-detail', scope.row)" style="cursor: pointer;">
                    <span class="stock-name">{{ scope.row.name }}</span>
                    <span class="stock-symbol">{{ scope.row.symbol }}</span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column prop="total" label="综合评分" width="100">
                <template #default="scope">
                  <span class="score-text" :style="{color: getScoreColor(scope.row.total)}">
                    {{ scope.row.total.toFixed(1) }}
                  </span>
                </template>
              </el-table-column>
              <el-table-column prop="technical" label="技术面" width="80" />
              <el-table-column prop="fundamental" label="基本面" width="80" />
              <el-table-column prop="liquidity" label="流动性" width="80" />
              <el-table-column label="特征分析">
                <template #default="scope">
                  <div class="reasons-flow">
                    <span v-for="r in scope.row.reasons" :key="r" class="bili-tag-mini">{{ r }}</span>
                  </div>
                </template>
              </el-table-column>
            </el-table>

            <div class="traces" v-if="quant.screenResult.value.provider_trace?.length">
              <span class="trace-label">数据源追踪:</span>
              <span 
                v-for="t in quant.screenResult.value.provider_trace" 
                :key="t.provider + t.capability"
                class="trace-item"
                :class="t.status"
              >
                {{ t.provider }}.{{ t.capability }} ({{ t.rows }}行)
              </span>
            </div>
          </div>
        </div>

        <!-- Empty State（兜底：既不在 loading 也无 result）-->
        <div v-else class="empty-layout">
          <div class="empty-content">
            <el-empty :image-size="120" description="请在左侧设置筛选条件" />
            <p class="empty-tip">支持 10000+ A股/美股全市场实时打分分析</p>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.quant-view {
  flex: 1;
  display: flex;
  flex-direction: column;
  background: var(--cf-bg-2);
  border-radius: var(--cf-radius-lg);
  overflow: hidden;
  height: 100%;
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
  gap: 12px;
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
  font-size: 12px;
  color: #FB7299;
  background: rgba(251, 114, 153, 0.1);
  padding: 2px 8px;
  border-radius: 10px;
  font-weight: 600;
}

/* ── Container ── */
.quant-container {
  flex: 1;
  display: flex;
  overflow: hidden;
}

/* ── Sidebar ── */
.filter-sidebar {
  width: 280px;
  background: var(--cf-card);
  border-right: 1px solid var(--cf-border-soft);
  display: flex;
  flex-direction: column;
  padding: 20px;
  overflow-y: auto;
}
.filter-section {
  margin-bottom: 24px;
}
.section-title {
  font-size: 14px;
  font-weight: 700;
  color: var(--cf-text-2);
  margin-bottom: 16px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.filter-item {
  margin-bottom: 16px;
}
.filter-item label {
  display: block;
  font-size: 12px;
  color: var(--cf-text-3);
  margin-bottom: 6px;
}
.range-inputs {
  display: flex;
  align-items: center;
  gap: 8px;
}
.range-sep { color: var(--cf-text-4); }

.weight-item {
  margin-bottom: 12px;
}
.weight-label {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  color: var(--cf-text-2);
  margin-bottom: 4px;
}
.weight-val {
  color: #00AEEC;
  font-weight: 600;
}

.run-btn {
  width: 100%;
  height: 40px;
  border-radius: 20px;
  background: #00AEEC !important;
  border: none !important;
  font-weight: 600;
  box-shadow: 0 4px 12px rgba(0, 174, 236, 0.2);
  transition: all 0.2s;
}
.run-btn:hover {
  transform: translateY(-1px);
  box-shadow: 0 6px 16px rgba(0, 174, 236, 0.3);
}
.error-text {
  margin-top: 10px;
  font-size: 12px;
  color: #F25D59;
  text-align: center;
}

/* ── Main Content ── */
.main-content {
  flex: 1;
  padding: 20px;
  overflow-y: auto;
}
.result-layout {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

/* ── Cards ── */
.analysis-card, .table-card {
  background: var(--cf-card);
  border-radius: 12px;
  border: 1px solid var(--cf-border-soft);
  overflow: hidden;
}
.card-header {
  padding: 12px 20px;
  border-bottom: 1px solid var(--cf-border-soft);
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 700;
  font-size: 15px;
}
.analysis-body {
  padding: 16px 20px;
}
.analysis-text {
  font-size: 14px;
  line-height: 1.6;
  color: var(--cf-text-2);
  margin: 0 0 12px;
}
.risk-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.as-of {
  margin-left: auto;
  font-size: 12px;
  font-weight: 400;
  color: var(--cf-text-4);
}
.analyze-pulse {
  margin-left: auto;
  font-size: 11px;
  color: #00AEEC;
  background: rgba(0, 174, 236, 0.1);
  padding: 2px 8px;
  border-radius: 10px;
  font-weight: 500;
  animation: analyze-blink 1.2s ease-in-out infinite;
}
@keyframes analyze-blink {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}

/* ── Table Styles ── */
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
.score-text {
  font-weight: 800;
  font-size: 15px;
}
.reasons-flow {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.bili-tag-mini {
  font-size: 11px;
  background: var(--cf-bg-2);
  color: var(--cf-text-3);
  padding: 1px 6px;
  border-radius: 4px;
  border: 1px solid var(--cf-border-soft);
}

.traces {
  padding: 12px 20px;
  background: var(--cf-bg-2);
  font-size: 11px;
  display: flex;
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
}
.trace-label { color: var(--cf-text-4); }
.trace-item { color: var(--cf-text-3); }
.trace-item.ok { color: #00AEEC; }
.trace-item.fallback { color: #E6A23C; }

/* ── States ── */
.empty-layout, .loading-layout {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
}
.empty-tip {
  margin-top: -10px;
  font-size: 13px;
  color: var(--cf-text-4);
}
.bili-loading-box {
  text-align: center;
}
.bili-kaomoji {
  font-size: 48px;
  margin-bottom: 16px;
  color: #00AEEC;
  animation: kaomoji-bounce 1s infinite alternate;
}
@keyframes kaomoji-bounce {
  from { transform: translateY(0); }
  to { transform: translateY(-10px); }
}
.bili-loading-text {
  color: var(--cf-text-3);
  font-size: 14px;
}
.bili-loading-hint {
  color: var(--cf-text-4);
  font-size: 12px;
  margin-top: 8px;
  max-width: 360px;
  line-height: 1.5;
}

/* ── Cache Badge ── */
.header-right {
  display: flex;
  align-items: center;
  gap: 12px;
}
.cache-badge {
  font-size: 12px;
  color: var(--cf-text-3);
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border-radius: 12px;
  background: var(--cf-bg-2);
  border: 1px solid var(--cf-border-soft);
}
.cache-badge.stale {
  color: #E6A23C;
  background: rgba(230, 162, 60, 0.08);
}
.cache-meta {
  color: var(--cf-text-4);
  font-size: 11px;
}
.cache-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #909399;
}
.cache-dot.running {
  background: #00AEEC;
  box-shadow: 0 0 0 0 rgba(0, 174, 236, 0.6);
  animation: cache-dot-pulse 2s infinite;
}
@keyframes cache-dot-pulse {
  0%   { box-shadow: 0 0 0 0 rgba(0, 174, 236, 0.6); }
  70%  { box-shadow: 0 0 0 6px rgba(0, 174, 236, 0); }
  100% { box-shadow: 0 0 0 0 rgba(0, 174, 236, 0); }
}

/* ── Bilibili Overrides ── */
:deep(.el-slider__bar) { background-color: #00AEEC; }
:deep(.el-slider__button) { border-color: #00AEEC; }
:deep(.bili-table-header) {
  background-color: var(--cf-bg-2) !important;
  color: var(--cf-text-3);
  font-weight: 600;
  font-size: 13px;
}
</style>
