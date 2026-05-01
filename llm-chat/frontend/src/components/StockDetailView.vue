<script setup lang="ts">
import { onMounted, onUnmounted, ref, nextTick, computed } from 'vue'
import { ArrowLeft, TrendCharts, TopRight, Warning, InfoFilled, PieChart, DataAnalysis, List } from '@element-plus/icons-vue'
import * as echarts from 'echarts'

const props = defineProps<{
  stock: any
}>()

const emit = defineEmits<{
  (e: 'back'): void
}>()

const chartRef = ref<HTMLElement | null>(null)
const radarRef = ref<HTMLElement | null>(null)
let chartInstance: echarts.ECharts | null = null
let radarInstance: echarts.ECharts | null = null
const loading = ref(true)
const error = ref('')

// 获取涨跌幅颜色
function getChangeColor(val: number | string) {
  const n = typeof val === 'number' ? val : parseFloat(val as string)
  if (isNaN(n) || n === 0) return 'var(--cf-text-3)'
  return n > 0 ? '#F25D59' : '#00B578'
}

async function initChart() {
  if (!chartRef.value) return
  loading.value = true
  
  try {
    const { fetchStockChart } = await import('../api')
    const data = await fetchStockChart(props.stock.symbol)
    
    if (!data.dates || data.dates.length === 0) {
      error.value = '未获取到 K 线数据'
      return
    }

    chartInstance = echarts.init(chartRef.value)
    
    const upColor = '#F25D59'
    const downColor = '#00B578'

    const option = {
      backgroundColor: 'transparent',
      animation: true,
      legend: {
        top: 0,
        left: 'center',
        data: ['K线', 'MA5', 'MA10', 'MA20'],
        textStyle: { color: '#9499A0', fontSize: 11 }
      },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross', lineStyle: { color: '#00AEEC', width: 1, type: 'dashed' } },
        borderWidth: 0,
        backgroundColor: 'rgba(255, 255, 255, 0.95)',
        padding: 10,
        textStyle: { color: '#18191C', fontSize: 12 },
        extraCssText: 'box-shadow: 0 4px 12px rgba(0,0,0,0.1); border-radius: 8px;'
      },
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      grid: [
        { left: '40', right: '10', top: '30', height: '65%' },
        { left: '40', right: '10', top: '75%', height: '15%' }
      ],
      xAxis: [
        {
          type: 'category',
          data: data.dates,
          boundaryGap: false,
          axisLine: { lineStyle: { color: 'var(--cf-border-soft)' } },
          axisLabel: { color: '#9499A0', fontSize: 10 },
          splitLine: { show: false },
          min: 'dataMin', max: 'dataMax'
        },
        {
          type: 'category',
          gridIndex: 1,
          data: data.dates,
          boundaryGap: false,
          axisLine: { lineStyle: { color: 'var(--cf-border-soft)' } },
          axisTick: { show: false },
          splitLine: { show: false },
          axisLabel: { show: false },
          min: 'dataMin', max: 'dataMax'
        }
      ],
      yAxis: [
        { 
          scale: true, 
          position: 'left',
          axisLabel: { color: '#9499A0', fontSize: 10 },
          splitLine: { lineStyle: { color: 'var(--cf-border-soft)', type: 'dashed' } }
        },
        { 
          scale: true, 
          gridIndex: 1, 
          splitNumber: 2, 
          axisLabel: { show: false }, 
          axisLine: { show: false }, 
          axisTick: { show: false }, 
          splitLine: { show: false } 
        }
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1], start: 80, end: 100 },
        { show: false, xAxisIndex: [0, 1], type: 'slider', start: 80, end: 100 }
      ],
      series: [
        {
          name: 'K线',
          type: 'candlestick',
          data: data.values,
          itemStyle: {
            color: upColor, color0: downColor,
            borderColor: upColor, borderColor0: downColor
          },
        },
        { name: 'MA5', type: 'line', data: data.ma5, smooth: true, showSymbol: false, lineStyle: { color: '#FF9736', width: 1, opacity: 0.8 } },
        { name: 'MA10', type: 'line', data: data.ma10, smooth: true, showSymbol: false, lineStyle: { color: '#00AEEC', width: 1, opacity: 0.8 } },
        { name: 'MA20', type: 'line', data: data.ma20, smooth: true, showSymbol: false, lineStyle: { color: '#FB7299', width: 1, opacity: 0.8 } },
        {
          name: '成交量',
          type: 'bar',
          xAxisIndex: 1, yAxisIndex: 1,
          data: data.volumes.map((v: any) => ({
            value: v[1],
            itemStyle: { color: v[2] === 1 ? upColor : downColor, opacity: 0.7 }
          }))
        }
      ]
    }
    chartInstance.setOption(option)
  } catch (e: any) {
    error.value = e.message || '初始化图表失败'
  } finally {
    loading.value = false
  }
}

async function initRadar() {
  if (!radarRef.value) return
  radarInstance = echarts.init(radarRef.value)
  
  const v = props.stock.raw || {}
  const technical = props.stock.technical || 0
  const fundamental = props.stock.fundamental || 0
  const liquidity = props.stock.liquidity || 0
  
  // 模拟一些其他维度的展示逻辑
  const volatilityScore = Math.max(0, Math.min(100, 100 - (v.volatility || 0) * 20))
  const momentumScore = Math.max(0, Math.min(100, (v.momentum || 0) + 50))

  const option = {
    radar: {
      indicator: [
        { name: '技术面', max: 100 },
        { name: '基本面', max: 100 },
        { name: '流动性', max: 100 },
        { name: '稳定性', max: 100 },
        { name: '动量', max: 100 }
      ],
      shape: 'circle',
      splitNumber: 4,
      axisName: { color: '#9499A0', fontSize: 11 },
      splitLine: { lineStyle: { color: 'var(--cf-border-soft)' } },
      splitArea: { show: false },
      axisLine: { lineStyle: { color: 'var(--cf-border-soft)' } }
    },
    series: [{
      type: 'radar',
      data: [{
        value: [technical, fundamental, liquidity, volatilityScore, momentumScore],
        name: '综合评估',
        itemStyle: { color: '#00AEEC' },
        areaStyle: { color: 'rgba(0, 174, 236, 0.2)' },
        lineStyle: { width: 2 },
        symbol: 'circle',
        symbolSize: 4
      }]
    }]
  }
  radarInstance.setOption(option)
}

function handleResize() {
  chartInstance?.resize()
  radarInstance?.resize()
}

onMounted(async () => {
  await nextTick()
  initChart()
  initRadar()
  window.addEventListener('resize', handleResize)
})

onUnmounted(() => {
  window.removeEventListener('resize', handleResize)
  chartInstance?.dispose()
  radarInstance?.dispose()
})

const scoreColor = computed(() => {
  const s = props.stock.total || 0
  if (s >= 70) return '#FB7299'
  if (s >= 60) return '#00AEEC'
  return '#9499A0'
})

function getXueqiuUrl() {
  const [code, market] = props.stock.symbol.split('.')
  const xqMarket = market === 'SH' ? 'SH' : 'SZ'
  return `https://xueqiu.com/S/${xqMarket}${code}`
}
</script>

<template>
  <div class="stock-detail-container">
    <!-- Header: 股票基本概况 -->
    <div class="stock-header card-glow">
      <div class="header-left">
        <el-button class="back-btn" :icon="ArrowLeft" circle @click="emit('back')" />
        <div class="title-group">
          <div class="name-row">
            <h1 class="stock-name">{{ stock.name }}</h1>
            <span class="stock-symbol">{{ stock.symbol }}</span>
          </div>
          <div class="tags-row">
            <el-tag size="small" class="bili-tag">A股主板</el-tag>
            <el-tag size="small" type="info" plain class="bili-tag">{{ stock.industry || '通用板块' }}</el-tag>
          </div>
        </div>
      </div>

      <div class="header-price" v-if="stock.price">
        <div class="main-price" :style="{color: getChangeColor(stock.pct_chg)}">{{ stock.price.toFixed(2) }}</div>
        <div class="price-meta">
          <span :style="{color: getChangeColor(stock.pct_chg)}">
            {{ stock.pct_chg > 0 ? '+' : '' }}{{ stock.pct_chg.toFixed(2) }}%
          </span>
        </div>
      </div>

      <div class="header-stats">
        <div class="stat-item">
          <div class="label">综合评分</div>
          <div class="value total-score" :style="{color: scoreColor}">{{ stock.total.toFixed(1) }}</div>
        </div>
        <div class="stat-sep"></div>
        <div class="stat-item">
          <div class="label">流通市值</div>
          <div class="value">{{ stock.mkt_cap?.toFixed(1) || '-' }} 亿</div>
        </div>
        <div class="stat-sep"></div>
        <div class="stat-item">
          <div class="label">市盈率(TTM)</div>
          <div class="value">{{ stock.pe?.toFixed(1) || '-' }}</div>
        </div>
      </div>

      <div class="header-right">
        <el-link :href="getXueqiuUrl()" target="_blank" type="primary" :underline="false" class="xq-btn">
          雪球实时行情 <el-icon><TopRight /></el-icon>
        </el-link>
      </div>
    </div>

    <!-- Main Content -->
    <div class="stock-content">
      <!-- Left: K-Line & Market Data -->
      <div class="content-left">
        <!-- 行情九宫格 -->
        <div class="market-grid card-glow">
          <div class="grid-item">
            <span class="label">今开</span>
            <span class="val" :style="{color: getChangeColor((stock.raw?.open || 0) - (stock.raw?.prev_close || 0))}">
              {{ stock.raw?.open?.toFixed(2) || '-' }}
            </span>
          </div>
          <div class="grid-item">
            <span class="label">最高</span>
            <span class="val" style="color: #F25D59">{{ stock.raw?.high?.toFixed(2) || '-' }}</span>
          </div>
          <div class="grid-item">
            <span class="label">成交量</span>
            <span class="val">{{ (stock.raw?.volume || 0).toFixed(1) }} 万手</span>
          </div>
          <div class="grid-item">
            <span class="label">昨收</span>
            <span class="val">{{ stock.raw?.prev_close?.toFixed(2) || '-' }}</span>
          </div>
          <div class="grid-item">
            <span class="label">最低</span>
            <span class="val" style="color: #00B578">{{ stock.raw?.low?.toFixed(2) || '-' }}</span>
          </div>
          <div class="grid-item">
            <span class="label">成交额</span>
            <span class="val">{{ stock.raw?.amount?.toFixed(2) || '-' }} 亿</span>
          </div>
          <div class="grid-item">
            <span class="label">换手率</span>
            <span class="val">{{ stock.raw?.turnover_rate?.toFixed(2) || '-' }}%</span>
          </div>
          <div class="grid-item">
            <span class="label">振幅</span>
            <span class="val">{{ stock.raw?.amplitude?.toFixed(2) || '-' }}%</span>
          </div>
          <div class="grid-item">
            <span class="label">量比</span>
            <span class="val">{{ stock.raw?.volume_ratio?.toFixed(2) || '-' }}</span>
          </div>
        </div>

        <!-- 巨幅 K 线 -->
        <div class="chart-box card-glow">
          <div class="box-header">
            <div class="title"><el-icon><TrendCharts /></el-icon> 历史走势 (Daily)</div>
            <div class="actions">
              <span class="act-item active">日K</span>
              <span class="act-item">均线叠加</span>
            </div>
          </div>
          <div v-loading="loading" class="chart-wrapper">
            <div v-if="error" class="chart-error">{{ error }}</div>
            <div ref="chartRef" class="echarts-dom"></div>
          </div>
        </div>
      </div>

      <!-- Right: AI Analysis & Factor Scores -->
      <div class="content-right">
        <!-- 因子雷达图 -->
        <div class="radar-box card-glow">
          <div class="box-header">
            <div class="title"><el-icon><PieChart /></el-icon> 因子雷达</div>
          </div>
          <div class="radar-wrapper">
            <div ref="radarRef" class="echarts-dom"></div>
          </div>
        </div>

        <!-- 核心特征 -->
        <div class="tags-box card-glow">
          <div class="box-header">
            <div class="title"><el-icon><DataAnalysis /></el-icon> 核心特征</div>
          </div>
          <div class="tags-list">
            <span v-for="tag in stock.reasons" :key="tag" class="bili-tag-lg">{{ tag }}</span>
            <div v-if="!stock.reasons?.length" class="empty-hint">模型暂未识别显著特征</div>
          </div>
        </div>

        <!-- 风险警告 -->
        <div class="risk-box card-glow" v-if="stock.risk_notes?.length">
          <div class="box-header">
            <div class="title risk"><el-icon><Warning /></el-icon> 风险提示</div>
          </div>
          <div class="risk-list">
            <div v-for="risk in stock.risk_notes" :key="risk" class="risk-item">
              <span class="dot"></span> {{ risk }}
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.stock-detail-container {
  flex: 1;
  display: flex;
  flex-direction: column;
  padding: 16px;
  gap: 16px;
  background-color: var(--cf-bg);
  height: 100vh;
  overflow: hidden;
  box-sizing: border-box;
}

/* ── Common Card Style ── */
.card-glow {
  background: var(--cf-card);
  border: 1px solid var(--cf-border-soft);
  border-radius: 12px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.03);
  transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
}
.card-glow:hover {
  border-color: var(--cf-border-glow);
  box-shadow: 0 8px 24px rgba(0, 174, 236, 0.08);
}

.box-header {
  padding: 12px 16px;
  border-bottom: 1px solid var(--cf-border-soft);
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.box-header .title {
  font-size: 14px;
  font-weight: 700;
  color: var(--cf-text-2);
  display: flex;
  align-items: center;
  gap: 8px;
}
.box-header .title.risk { color: #F25D59; }

/* ── Header Section ── */
.stock-header {
  height: 90px;
  display: flex;
  align-items: center;
  padding: 0 20px;
}
.header-left { display: flex; align-items: center; gap: 20px; flex: 1; }
.back-btn { border: none; background: var(--cf-bg-2); }
.back-btn:hover { background: #00AEEC; color: #fff; }

.title-group { display: flex; flex-direction: column; gap: 4px; }
.name-row { display: flex; align-items: baseline; gap: 10px; }
.stock-name { font-size: 24px; font-weight: 800; margin: 0; color: var(--cf-text-1); }
.stock-symbol { font-family: monospace; font-size: 14px; color: var(--cf-text-3); }
.tags-row { display: flex; gap: 6px; }

.header-price { flex: 0.8; display: flex; flex-direction: column; align-items: center; }
.main-price { font-size: 32px; font-weight: 800; line-height: 1; }
.price-meta { font-size: 14px; font-weight: 600; margin-top: 4px; }

.header-stats { flex: 1.5; display: flex; align-items: center; justify-content: center; gap: 24px; }
.stat-item { display: flex; flex-direction: column; align-items: center; }
.stat-item .label { font-size: 11px; color: var(--cf-text-4); margin-bottom: 4px; }
.stat-item .value { font-size: 18px; font-weight: 700; color: var(--cf-text-2); }
.stat-item .value.total-score { font-size: 22px; }
.stat-sep { width: 1px; height: 30px; background: var(--cf-border-soft); }

.header-right { flex: 1; display: flex; justify-content: flex-end; }
.xq-btn { font-weight: 700; background: var(--cf-active); padding: 8px 16px; border-radius: 20px; }

/* ── Content Layout ── */
.stock-content { flex: 1; display: flex; gap: 16px; min-height: 0; }

/* Left Content */
.content-left { flex: 2.5; display: flex; flex-direction: column; gap: 16px; min-width: 0; }

.market-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
  padding: 16px;
}
.grid-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: var(--cf-bg-2);
  padding: 8px 12px;
  border-radius: 8px;
}
.grid-item .label { font-size: 12px; color: var(--cf-text-4); }
.grid-item .val { font-size: 14px; font-weight: 700; font-family: monospace; }

.chart-box { flex: 1; display: flex; flex-direction: column; }
.chart-wrapper { flex: 1; position: relative; }
.echarts-dom { width: 100%; height: 100%; }

/* Right Content */
.content-right { flex: 1; display: flex; flex-direction: column; gap: 16px; min-width: 300px; overflow-y: auto; }

.radar-box { height: 260px; }
.radar-wrapper { flex: 1; height: calc(100% - 45px); }

.tags-box { padding-bottom: 20px; }
.tags-list { padding: 16px; display: flex; flex-wrap: wrap; gap: 8px; }
.bili-tag-lg {
  background: linear-gradient(135deg, #F0F9FF 0%, #E3F2FD 100%);
  color: #0077B6;
  border: 1px solid #BEE3F8;
  padding: 8px 16px;
  border-radius: 10px;
  font-size: 13px;
  font-weight: 700;
}

.risk-box { background: #FFF9F9; border-color: #FFE3E3; }
.risk-list { padding: 16px; display: flex; flex-direction: column; gap: 10px; }
.risk-item { font-size: 13px; color: #444; display: flex; gap: 8px; align-items: flex-start; }
.risk-item .dot { width: 6px; height: 6px; background: #F25D59; border-radius: 50%; margin-top: 6px; flex-shrink: 0; }

.empty-hint { font-size: 12px; color: var(--cf-text-4); text-align: center; padding: 20px 0; font-style: italic; }
.chart-error { height: 100%; display: flex; align-items: center; justify-content: center; color: #F25D59; font-weight: 600; }

.act-item { font-size: 11px; padding: 2px 10px; border-radius: 4px; color: var(--cf-text-4); cursor: pointer; }
.act-item.active { background: #00AEEC; color: #fff; }
</style>
