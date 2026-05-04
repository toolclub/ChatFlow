<script setup lang="ts">
import { onMounted, onUnmounted, ref, nextTick, computed } from 'vue'
import { ArrowLeft, TrendCharts, TopRight, Warning, InfoFilled, PieChart, DataAnalysis, List, Connection, Compass } from '@element-plus/icons-vue'
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

// ── 工具函数 ──
function getChangeColor(val: number | string) {
  const n = typeof val === 'number' ? val : parseFloat(val as string)
  if (isNaN(n) || n === 0) return 'var(--cf-text-3)'
  return n > 0 ? '#F25D59' : '#00B578'
}

function formatNum(val: any, dec = 2) {
  if (val === undefined || val === null || isNaN(val)) return '-'
  return Number(val).toFixed(dec)
}

// ── ECharts 初始化 ──
async function initChart() {
  if (!chartRef.value) return
  loading.value = true
  
  try {
    const { fetchStockChart } = await import('../api')
    const data = await fetchStockChart(props.stock.symbol)
    
    if (!data.dates || data.dates.length === 0) {
      error.value = '未获取到 K 线数据，正在后台为您同步，请稍后刷新。'
      return
    }

    chartInstance = echarts.init(chartRef.value)
    const upColor = '#F25D59'
    const downColor = '#00B578'

    const option = {
      backgroundColor: 'transparent',
      animationDuration: 800,
      legend: {
        top: 0,
        right: 20,
        data: ['K线', 'MA5', 'MA10', 'MA20'],
        textStyle: { color: '#9499A0', fontSize: 11 },
        itemWidth: 10,
        itemHeight: 2
      },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross', lineStyle: { color: '#00AEEC', width: 1, type: 'dashed' } },
        borderWidth: 0,
        backgroundColor: 'rgba(255, 255, 255, 0.98)',
        padding: 12,
        textStyle: { color: '#18191C', fontSize: 12 },
        extraCssText: 'box-shadow: 0 8px 24px rgba(0,0,0,0.15); border-radius: 8px;'
      },
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      grid: [
        { left: '20', right: '50', top: '35', height: '65%', containLabel: true },
        { left: '20', right: '50', top: '78%', height: '15%', containLabel: true }
      ],
      xAxis: [
        {
          type: 'category',
          data: data.dates,
          boundaryGap: false,
          axisLine: { lineStyle: { color: 'var(--cf-border-soft)' } },
          axisLabel: { color: '#9499A0', fontSize: 10, maxInterval: 30 },
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
          position: 'right',
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
        { type: 'inside', xAxisIndex: [0, 1], start: 85, end: 100 },
        { 
          show: true, 
          xAxisIndex: [0, 1], 
          type: 'slider', 
          bottom: '2%', 
          height: 20,
          borderColor: 'transparent',
          fillerColor: 'rgba(0, 174, 236, 0.1)',
          handleIcon: 'path://M10.7,11.9v-1.3H9.3v1.3c-4.9,0.3-8.8,4.4-8.8,9.4c0,5,3.9,9.1,8.8,9.4v1.3h1.3v-1.3c4.9-0.3,8.8-4.4,8.8-9.4C19.5,16.3,15.6,12.2,10.7,11.9z',
          handleSize: '100%',
          handleStyle: { color: '#00AEEC' },
          textStyle: { color: 'transparent' }
        }
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
        { name: 'MA5', type: 'line', data: data.ma5, smooth: true, showSymbol: false, lineStyle: { color: '#FF9736', width: 1.2 } },
        { name: 'MA10', type: 'line', data: data.ma10, smooth: true, showSymbol: false, lineStyle: { color: '#00AEEC', width: 1.2 } },
        { name: 'MA20', type: 'line', data: data.ma20, smooth: true, showSymbol: false, lineStyle: { color: '#FB7299', width: 1.2 } },
        {
          name: '成交量',
          type: 'bar',
          xAxisIndex: 1, yAxisIndex: 1,
          data: data.volumes.map((v: any) => ({
            value: v[1],
            itemStyle: { color: v[2] === 1 ? upColor : downColor, opacity: 0.6 }
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
  const volScore = Math.max(0, Math.min(100, 100 - (v.volatility || 0) * 15))
  const momScore = Math.max(0, Math.min(100, (v.momentum || 0) + 50))

  const option = {
    radar: {
      indicator: [
        { name: '技术面', max: 100 },
        { name: '基本面', max: 100 },
        { name: '流动性', max: 100 },
        { name: '风控度', max: 100 },
        { name: '成长性', max: 100 }
      ],
      shape: 'circle',
      splitNumber: 4,
      axisName: { color: '#9499A0', fontSize: 11, fontWeight: 600 },
      splitLine: { lineStyle: { color: 'var(--cf-border-soft)' } },
      splitArea: { show: true, areaStyle: { color: ['rgba(244,245,247,0.3)', 'rgba(255,255,255,0.1)'] } },
      axisLine: { lineStyle: { color: 'var(--cf-border-soft)' } }
    },
    series: [{
      type: 'radar',
      data: [{
        value: [technical, fundamental, liquidity, volScore, momScore],
        name: '多维诊断',
        symbol: 'circle',
        symbolSize: 5,
        itemStyle: { color: '#00AEEC' },
        areaStyle: { 
          color: new echarts.graphic.RadialGradient(0.5, 0.5, 1, [
            { color: 'rgba(0, 174, 236, 0.5)', offset: 0 },
            { color: 'rgba(0, 174, 236, 0.05)', offset: 1 }
          ])
        },
        lineStyle: { width: 3, color: '#00AEEC', shadowBlur: 10, shadowColor: 'rgba(0, 174, 236, 0.4)' }
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

function getXueqiuUrl() {
  const [code, market] = props.stock.symbol.split('.')
  if (market === 'US') {
    return `https://xueqiu.com/S/${code}`
  }
  const xqMarket = market === 'SH' ? 'SH' : 'SZ'
  return `https://xueqiu.com/S/${xqMarket}${code}`
}

const scoreColor = computed(() => {
  const s = props.stock.total || 0
  if (s >= 70) return '#FB7299'
  if (s >= 60) return '#00AEEC'
  return '#9499A0'
})
</script>

<template>
  <div class="fintech-dashboard">
    <!-- 1. Top Glass Header -->
    <div class="glass-header card-glow">
      <div class="header-left">
        <el-button class="back-btn" :icon="ArrowLeft" circle @click="emit('back')" />
        <div class="stock-title">
          <div class="stock-name-row">
            <span class="name">{{ stock.name }}</span>
            <span class="symbol">{{ stock.symbol }}</span>
            <el-tag size="small" class="bili-tag-filled">{{ stock.symbol.endsWith('.US') ? '美股' : 'A股' }}</el-tag>
          </div>
          <div class="industry-row">
            <el-icon><Compass /></el-icon> {{ stock.industry || '综合性行业' }}
          </div>
        </div>
      </div>

      <div class="header-price" v-if="stock.price">
        <div class="price-val" :style="{color: getChangeColor(stock.pct_chg)}">{{ formatNum(stock.price) }}</div>
        <div class="price-pct" :style="{color: getChangeColor(stock.pct_chg)}">
          {{ stock.pct_chg > 0 ? '+' : '' }}{{ formatNum(stock.pct_chg) }}%
        </div>
      </div>

      <div class="header-metrics">
        <div class="metric-box">
          <span class="m-label">综合评分</span>
          <span class="m-value score" :style="{color: scoreColor}">{{ formatNum(stock.total, 1) }}</span>
        </div>
        <div class="m-divider"></div>
        <div class="metric-box">
          <span class="m-label">市值 ({{ stock.symbol.endsWith('.US') ? '亿刀' : '亿' }})</span>
          <span class="m-value">{{ formatNum(stock.mkt_cap, 1) }}</span>
        </div>
        <div class="m-divider"></div>
        <div class="metric-box">
          <span class="m-label">PE(TTM)</span>
          <span class="m-value">{{ formatNum(stock.pe, 1) }}</span>
        </div>
      </div>

      <div class="header-right">
        <el-button type="primary" class="xq-link-btn" @click="window.open(getXueqiuUrl(), '_blank')">
          <el-icon style="margin-right: 4px;"><TopRight /></el-icon> 实时行情
        </el-button>
      </div>
    </div>

    <!-- 2. Main Layout -->
    <div class="main-grid">
      <!-- Left: Multi-pane Chart Area -->
      <div class="chart-area content-card card-glow">
        <div class="area-header">
          <span class="area-title"><el-icon><TrendCharts /></el-icon> 历史走势 (120日)</span>
          <div class="period-switcher">
            <span class="p-btn active">日K</span>
            <span class="p-btn">分时</span>
          </div>
        </div>
        
        <!-- 九宫格实时面板（嵌入在图表上方） -->
        <div class="grid-panel">
          <div class="grid-cell"><span class="l">今开</span><span class="v" :style="{color: getChangeColor((stock.raw?.open || 0) - (stock.raw?.prev_close || 0))}">{{ formatNum(stock.raw?.open) }}</span></div>
          <div class="grid-cell"><span class="l">最高</span><span class="v" style="color: #F25D59">{{ formatNum(stock.raw?.high) }}</span></div>
          <div class="grid-cell"><span class="l">成交量</span><span class="v">{{ formatNum((stock.raw?.volume || 0), 1) }}万</span></div>
          <div class="grid-cell"><span class="l">昨收</span><span class="v">{{ formatNum(stock.raw?.prev_close) }}</span></div>
          <div class="grid-cell"><span class="l">最低</span><span class="v" style="color: #00B578">{{ formatNum(stock.raw?.low) }}</span></div>
          <div class="grid-cell"><span class="l">成交额</span><span class="v">{{ formatNum(stock.raw?.amount, 2) }}{{ stock.symbol.endsWith('.US') ? '亿刀' : '亿' }}</span></div>
          <div class="grid-cell"><span class="l">换手</span><span class="v">{{ formatNum(stock.raw?.turnover_rate) }}%</span></div>
          <div class="grid-cell"><span class="l">振幅</span><span class="v">{{ formatNum(stock.raw?.amplitude) }}%</span></div>
          <div class="grid-cell"><span class="l">量比</span><span class="v">{{ formatNum(stock.raw?.volume_ratio) }}</span></div>
        </div>

        <div v-loading="loading" class="chart-viewport">
          <div v-if="error" class="error-ui">
            <el-icon size="40"><Warning /></el-icon>
            <p>{{ error }}</p>
          </div>
          <div ref="chartRef" class="echarts-dom"></div>
        </div>
      </div>

      <!-- Right: AI Diagnosis & Insights -->
      <div class="insight-area">
        <!-- Radar Section -->
        <div class="radar-card content-card card-glow">
          <div class="area-header"><span class="area-title"><el-icon><PieChart /></el-icon> 多维评估</span></div>
          <div class="radar-viewport">
            <div ref="radarRef" class="echarts-dom"></div>
          </div>
        </div>

        <!-- Tags & Analysis -->
        <div class="feature-card content-card card-glow">
          <div class="area-header"><span class="area-title"><el-icon><DataAnalysis /></el-icon> 核心特征</span></div>
          <div class="tags-wall">
            <span v-for="tag in stock.reasons" :key="tag" class="bili-tag-fancy">{{ tag }}</span>
            <div v-if="!stock.reasons?.length" class="empty-state">未识别到特定技术特征</div>
          </div>
        </div>

        <!-- Risk Board -->
        <div class="risk-card content-card card-glow" v-if="stock.risk_notes?.length">
          <div class="area-header"><span class="area-title risk"><el-icon><Warning /></el-icon> 风险监测</span></div>
          <div class="risk-list">
            <div v-for="risk in stock.risk_notes" :key="risk" class="risk-line">
              <span class="bullet"></span> {{ risk }}
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.fintech-dashboard {
  flex: 1;
  display: flex;
  flex-direction: column;
  height: 100vh;
  background: var(--cf-bg);
  padding: 16px;
  gap: 16px;
  overflow: hidden;
  box-sizing: border-box;
}

/* ── Glass Header ── */
.glass-header {
  height: 90px;
  background: var(--cf-card);
  border: 1px solid var(--cf-border-soft);
  border-radius: 16px;
  display: flex;
  align-items: center;
  padding: 0 24px;
}

.header-left { display: flex; align-items: center; gap: 20px; flex: 1; }
.back-btn { background: var(--cf-bg-2); border: none; font-weight: bold; }
.back-btn:hover { background: #00AEEC; color: #fff; transform: scale(1.1); }

.stock-title { display: flex; flex-direction: column; gap: 2px; }
.stock-name-row { display: flex; align-items: baseline; gap: 10px; }
.stock-name-row .name { font-size: 26px; font-weight: 900; color: var(--cf-text-1); }
.stock-name-row .symbol { font-family: 'JetBrains Mono', monospace; font-size: 14px; color: var(--cf-text-4); }
.industry-row { font-size: 12px; color: var(--cf-text-4); display: flex; align-items: center; gap: 5px; }

.header-price { flex: 0.7; display: flex; flex-direction: column; align-items: center; border-left: 1px solid var(--cf-border-soft); }
.price-val { font-size: 34px; font-weight: 800; line-height: 1.1; font-family: 'Inter', sans-serif; }
.price-pct { font-size: 14px; font-weight: 700; }

.header-metrics { flex: 1.5; display: flex; align-items: center; justify-content: center; gap: 32px; border-left: 1px solid var(--cf-border-soft); }
.metric-box { display: flex; flex-direction: column; align-items: center; gap: 4px; }
.m-label { font-size: 10px; color: var(--cf-text-4); text-transform: uppercase; letter-spacing: 0.5px; }
.m-value { font-size: 18px; font-weight: 700; color: var(--cf-text-2); }
.m-value.score { font-size: 24px; }
.m-divider { width: 1px; height: 32px; background: var(--cf-border-soft); }

.header-right { flex: 0.8; display: flex; justify-content: flex-end; }
.xq-link-btn { border-radius: 99px; padding: 10px 20px; font-weight: 800; background: #00AEEC; border: none; }

/* ── Grid Layout ── */
.main-grid { flex: 1; display: flex; gap: 16px; min-height: 0; }

.chart-area { flex: 2.8; display: flex; flex-direction: column; min-width: 0; padding: 20px; position: relative; }
.insight-area { flex: 1; display: flex; flex-direction: column; gap: 16px; min-width: 320px; overflow-y: auto; }

.content-card { background: var(--cf-card); border-radius: 16px; border: 1px solid var(--cf-border-soft); }
.area-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.area-title { font-size: 14px; font-weight: 800; color: var(--cf-text-2); display: flex; align-items: center; gap: 8px; }
.area-title.risk { color: #F25D59; }

/* 行情九宫格 */
.grid-panel {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
  background: var(--cf-bg-2);
  padding: 12px;
  border-radius: 12px;
  margin-bottom: 20px;
}
.grid-cell { display: flex; justify-content: space-between; align-items: baseline; }
.grid-cell .l { font-size: 11px; color: var(--cf-text-4); }
.grid-cell .v { font-size: 13px; font-weight: 700; font-family: 'JetBrains Mono', monospace; }

.chart-viewport { flex: 1; width: 100%; position: relative; }
.echarts-dom { width: 100%; height: 100%; }

/* ── Sidebar Cards ── */
.radar-card { height: 300px; padding: 20px; }
.radar-viewport { flex: 1; height: calc(100% - 30px); }

.feature-card { padding: 20px; }
.tags-wall { display: flex; flex-wrap: wrap; gap: 10px; padding: 8px 0; }
.bili-tag-fancy {
  background: linear-gradient(135deg, #00AEEC 0%, #FB7299 100%);
  color: #fff;
  padding: 6px 14px;
  border-radius: 8px;
  font-size: 12px;
  font-weight: 800;
  box-shadow: 0 4px 10px rgba(0, 174, 236, 0.2);
}

.risk-card { background: #FFF9F9; padding: 20px; border-color: #FFE3E3; }
.risk-list { display: flex; flex-direction: column; gap: 12px; }
.risk-line { font-size: 13px; color: #444; line-height: 1.5; display: flex; gap: 8px; }
.risk-line .bullet { width: 6px; height: 6px; background: #F25D59; border-radius: 50%; margin-top: 7px; flex-shrink: 0; }

/* ── Effects ── */
.card-glow { box-shadow: 0 4px 12px rgba(0, 0, 0, 0.03); transition: all 0.4s cubic-bezier(0.165, 0.84, 0.44, 1); }
.card-glow:hover { transform: translateY(-2px); border-color: var(--cf-border-glow); box-shadow: 0 12px 32px rgba(0, 174, 236, 0.1); }

.period-switcher { display: flex; background: var(--cf-bg-2); padding: 3px; border-radius: 8px; gap: 4px; }
.p-btn { font-size: 10px; padding: 4px 12px; border-radius: 6px; color: var(--cf-text-4); cursor: pointer; transition: 0.2s; }
.p-btn.active { background: #fff; color: #00AEEC; font-weight: bold; box-shadow: 0 2px 6px rgba(0,0,0,0.1); }

.error-ui { height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; color: #F25D59; text-align: center; gap: 10px; }
.empty-state { color: var(--cf-text-4); font-size: 12px; font-style: italic; width: 100%; text-align: center; padding: 20px 0; }

.bili-tag-filled { background: #00AEEC; border: none; color: #fff; font-size: 10px; font-weight: bold; border-radius: 4px; }
</style>
