<script setup lang="ts">
import { onMounted, onUnmounted, ref, computed, nextTick, watch } from 'vue'
import * as echarts from 'echarts'
import type { ForecastCandle, EventMarker } from '../types'
import { predictionEventTypeColor, predictionEventTypeLabel } from '../types'

/**
 * 预测走势图（设计文档 §6：业界标注规格）。
 *
 * 数据可能缺席（预测失败、行情拉不到、还没跑到 compute 阶段）——
 * historicalData / forecastData / forecastCandles 都是可选的，全空时走空态，
 * 绝不能让 echarts 炸。
 *
 * v2 增量（全是 option 加项，无结构性重写）：
 *   · 预测蜡烛：第二个 candlestick series，虚线边框 + 半透明 → 与历史实体蜡烛一眼可分。
 *     消费 SSE `forecast_day`，**一根一根长出来**（增量 setOption，不是等 done 再画）。
 *   · 历史/预测分界 markLine（'预测起点'）+ 终值 endLabel + 目标价横线
 *   · 置信带渐宽渐淡（横向 linearGradient，opacity 0.25 → 0.08）
 *   · 支撑/压力位 markLine（近 60 日高低 与 BOLL 上下轨 取极值）
 *   · 公告事件打点（消费 `event_marker`，打在公告日；爆雷红/定增橙/减持紫，颜色取自
 *     types 的 predictionEventTypeColor —— 唯一真相源，本文件不再自己写一份色表）
 *
 * ★ 增量渲染的实现要点：首帧 notMerge=true 建立结构，其后 notMerge=false 合并更新。
 *   若每帧都 notMerge=true，整张图会重放 800ms 入场动画 —— 7 根蜡烛长出来的过程会
 *   变成 7 次全图闪烁，而不是"又长了一根"。
 */
const props = defineProps<{
  symbol: string
  historicalData?: {
    dates: string[]
    values: [number, number, number, number][]   // [open, close, low, high]
    ma5?: number[]
    ma10?: number[]
    ma20?: number[]
    volumes?: [string, number, number][]           // [date, volume, direction]
  }
  ma5?: number[]
  ma10?: number[]
  ma20?: number[]
  volumes?: [string, number, number][]
  forecastData?: {
    dates: string[]
    point_forecast: number[]
    p10: number[]
    p50: number[]
    p90: number[]
  }
  /** v2：预测 OHLC 蜡烛（逐日流式到达，按 day 覆盖去重 —— 见 usePrediction） */
  forecastCandles?: ForecastCandle[]
  /** v2：K 线上的公告事件打点（爆雷/定增/减持） */
  eventMarkers?: EventMarker[]
  /** v2：技术指标（只用 boll.upper / boll.lower 画支撑压力位；缺失就只用近 60 日高低） */
  technicalIndicators?: Record<string, unknown>
  inflectionPoints?: Array<{ date: string; price: number; type: string }>
  /** 空态文案（如失败原因），由父组件透传 */
  emptyHint?: string
}>()

const chartRef = ref<HTMLElement | null>(null)
let chartInstance: echarts.ECharts | null = null
/** 首帧用 notMerge 建结构，其后合并更新（否则每根新蜡烛都会重放全图入场动画） */
let _rendered = false

const upColor = '#F25D59'
const downColor = '#00B578'
const forecastColor = '#FB7299'

/** 有效蜡烛：流式到达可能有空洞（day 乱序），渲染前先滤掉 */
const candles = computed<ForecastCandle[]>(
  () => (props.forecastCandles || []).filter(
    (c): c is ForecastCandle => !!c && typeof c.c === 'number' && isFinite(c.c),
  ),
)

/** 有没有任何可画的东西：历史 K 线 / 预测序列 / 预测蜡烛 */
const hasData = computed(
  () =>
    (props.historicalData?.values?.length ?? 0) > 0 ||
    (props.forecastData?.point_forecast?.length ?? 0) > 0 ||
    candles.value.length > 0,
)

/** 已经长出几根预测蜡烛（给标题用："预测 3/7 根"） */
const candleCount = computed(() => candles.value.length)

function _num(v: unknown): number | null {
  return typeof v === 'number' && isFinite(v) ? v : null
}

/** BOLL 是嵌套 dict（factors/prediction_indicators.py: {upper, mid, lower}），不是标量 */
function bollBand(): { upper: number | null; lower: number | null } {
  const boll = props.technicalIndicators?.boll as Record<string, unknown> | undefined
  if (!boll || typeof boll !== 'object') return { upper: null, lower: null }
  return { upper: _num(boll.upper), lower: _num(boll.lower) }
}

/**
 * 支撑 / 压力位（§6）：
 *   压力位 = max(近 60 日最高, BOLL 上轨)   支撑位 = min(近 60 日最低, BOLL 下轨)
 * 历史不足 / BOLL 缺失时按可得的那一半算；两者都没有 → null（不画，不编）。
 */
function supportResistance(): { support: number | null; resistance: number | null } {
  const values = props.historicalData?.values ?? []
  let hi: number | null = null
  let lo: number | null = null
  const window = values.slice(-60)
  for (const v of window) {
    const h = _num(v?.[3])
    const l = _num(v?.[2])
    if (h != null) hi = hi == null ? h : Math.max(hi, h)
    if (l != null) lo = lo == null ? l : Math.min(lo, l)
  }
  const { upper, lower } = bollBand()
  const resistance = hi == null ? upper : upper == null ? hi : Math.max(hi, upper)
  const support = lo == null ? lower : lower == null ? lo : Math.min(lo, lower)
  return { support, resistance }
}

/**
 * 合并历史与预测的 x 轴日期 + null 桥接预测线到最后一根历史收盘，
 * 使预测段无缝衔接历史 K 线（x 轴延伸到未来日期）。
 */
function buildChartData() {
  const histDates = props.historicalData?.dates || []
  const histValues = props.historicalData?.values || []
  const fc = props.forecastData
  const cs = candles.value

  // 合并 x 轴：历史日期 + 预测日期（蜡烛的日期优先，其次 forecastData.dates）
  const allDates = [...histDates]
  const pushDate = (d: string) => {
    if (d && !allDates.includes(d)) allDates.push(d)
  }
  for (const c of cs) pushDate(c.date)
  for (const d of fc?.dates || []) pushDate(d)

  const idxOf = new Map<string, number>()
  allDates.forEach((d, i) => idxOf.set(d, i))

  const lastClose: number | null = histValues.length
    ? _num(histValues[histValues.length - 1]?.[1])
    : null
  const bridgeIdx = histDates.length - 1   // 桥接点：最后一根历史 K 线

  // 预测中位线：用 null 填充历史区间，最后一个历史收盘作为桥接点，再接预测值
  const pointForecast: (number | null)[] = allDates.map(() => null)
  if (fc?.point_forecast?.length && lastClose != null && bridgeIdx >= 0) {
    pointForecast[bridgeIdx] = lastClose
    for (let i = 0; i < fc.point_forecast.length; i++) {
      const at = idxOf.get(fc.dates?.[i] ?? '') ?? histDates.length + i
      pointForecast[at] = fc.point_forecast[i]
    }
  }

  // p10 / p90 置信带：同样 null 桥接（p10 隐形基线 + (p90−p10) 堆叠面积 = 教科书画法）
  const p10: (number | null)[] = allDates.map(() => null)
  const p90: (number | null)[] = allDates.map(() => null)
  if (fc?.p10?.length && fc?.p90?.length && lastClose != null && bridgeIdx >= 0) {
    p10[bridgeIdx] = lastClose
    p90[bridgeIdx] = lastClose
    for (let i = 0; i < Math.min(fc.p10.length, fc.p90.length); i++) {
      const at = idxOf.get(fc.dates?.[i] ?? '') ?? histDates.length + i
      p10[at] = fc.p10[i]
      p90[at] = fc.p90[i]
    }
  }

  // 预测蜡烛：ECharts candlestick 的数据序是 [open, close, lowest, highest]
  //
  // ★ 占位必须用 '-'（ECharts 的空值标记），**绝不能用 null**。
  //   candlestick 走 WhiskerBoxCommonMixin.getInitialData，它会对每个数据项读 .value；
  //   遇到 null 直接抛 TypeError: Cannot read properties of null (reading 'value')，
  //   于是 setOption 整个中断 —— ZRender 的 canvas 层是首帧渲染时才惰性创建的，
  //   结果就是 echarts 实例在、根 div 在、**一个 canvas 都没有**，历史 K 线和预测蜡烛
  //   一起消失（实测：canvas 数 = 0）。line 系列可以吃 null（形成断点），candlestick 不行。
  const EMPTY = '-' as const
  const fcCandles: ([number, number, number, number] | typeof EMPTY)[] =
    allDates.map(() => EMPTY)
  for (const c of cs) {
    const at = idxOf.get(c.date) ?? (bridgeIdx >= 0 ? bridgeIdx + c.day : -1)
    if (at < 0 || at >= allDates.length) continue
    const o = _num(c.o), cl = _num(c.c), l = _num(c.l), h = _num(c.h)
    if (o == null || cl == null || l == null || h == null) continue
    fcCandles[at] = [o, cl, l, h]
  }

  // MA 线（null 对齐到 allDates，历史区间有值）
  const alignMA = (ma?: number[]) => {
    if (!ma) return allDates.map(() => null)
    const out: (number | null)[] = allDates.map(() => null)
    for (let i = 0; i < Math.min(ma.length, histDates.length); i++) {
      out[i] = ma[i]
    }
    return out
  }

  return {
    allDates,
    histDates,
    histValues,
    lastClose,
    pointForecast,
    p10,
    p90,
    fcCandles,
    ma5: alignMA(props.ma5 ?? props.historicalData?.ma5),
    ma10: alignMA(props.ma10 ?? props.historicalData?.ma10),
    ma20: alignMA(props.ma20 ?? props.historicalData?.ma20),
  }
}

/**
 * 公告事件打点：只打在**确实存在于历史 K 线上的日期**（非交易日/超出窗口的公告日
 * 没有对应的 x 坐标）。价格取当日最高价，pin 悬在 K 线上方，不遮挡实体。
 */
function buildEventMarkPoints(histDates: string[], histValues: [number, number, number, number][]) {
  const markers = props.eventMarkers || []
  if (!markers.length || !histDates.length) return []
  const idxOf = new Map<string, number>()
  histDates.forEach((d, i) => idxOf.set(d, i))

  const out: any[] = []
  for (const m of markers) {
    const i = idxOf.get(m.date)
    if (i == null) continue                       // 该公告日不在图上 → 不打点（不硬凑坐标）
    const high = _num(histValues[i]?.[3])
    if (high == null) continue
    const color = predictionEventTypeColor(m.event_type)
    out.push({
      name: m.title || predictionEventTypeLabel(m.event_type),
      coord: [m.date, high],
      symbol: 'pin',
      symbolSize: 26,
      symbolOffset: [0, '-55%'],
      itemStyle: { color, borderColor: '#fff', borderWidth: 1 },
      label: { show: false },
      // tooltip 用：2026-05-12 · 减持 · <标题> · 负面度 -0.60
      _evt: {
        date: m.date,
        type: predictionEventTypeLabel(m.event_type),
        title: m.title || '',
        score: m.score,
      },
    })
  }
  return out
}

function renderChart() {
  // 无数据 → 不 init echarts（空态由模板渲染），避免对空序列取 [last][1] 炸掉
  if (!hasData.value) {
    disposeChart()
    return
  }
  if (!chartRef.value) return
  if (!chartInstance) {
    chartInstance = echarts.init(chartRef.value)
    _rendered = false
  }

  const {
    allDates, histDates, histValues, lastClose,
    pointForecast, p10, p90, fcCandles, ma5, ma10, ma20,
  } = buildChartData()

  const { support, resistance } = supportResistance()
  const eventPoints = buildEventMarkPoints(histDates, histValues)
  const boundaryDate = histDates.length ? histDates[histDates.length - 1] : null

  // 目标价 = 最后一根预测蜡烛的收盘（没有蜡烛时退回 p50 末值）
  const lastCandle = candles.value.length ? candles.value[candles.value.length - 1] : null
  const targetPrice =
    _num(lastCandle?.c) ??
    _num(props.forecastData?.point_forecast?.[props.forecastData.point_forecast.length - 1])

  // 历史 K 线的 markLine：现价 + 支撑/压力 + 历史/预测分界（都只在有值时才加）
  const histMarkLines: any[] = []
  if (lastClose != null) {
    histMarkLines.push({
      yAxis: lastClose,
      label: {
        position: 'end', formatter: `现价 ${lastClose.toFixed(2)}`,
        backgroundColor: upColor, color: '#fff', padding: [2, 4], borderRadius: 2, fontSize: 10,
      },
      lineStyle: { type: 'dashed', color: upColor, width: 1, opacity: 0.8 },
    })
  }
  if (resistance != null) {
    histMarkLines.push({
      yAxis: resistance,
      label: {
        position: 'insideStartTop', formatter: `压力位 ${resistance.toFixed(2)}`,
        color: '#F25D59', fontSize: 10,
      },
      lineStyle: { type: 'dotted', color: '#F25D59', width: 1, opacity: 0.55 },
    })
  }
  if (support != null) {
    histMarkLines.push({
      yAxis: support,
      label: {
        position: 'insideStartBottom', formatter: `支撑位 ${support.toFixed(2)}`,
        color: '#00B578', fontSize: 10,
      },
      lineStyle: { type: 'dotted', color: '#00B578', width: 1, opacity: 0.55 },
    })
  }
  if (boundaryDate && (candles.value.length || props.forecastData?.point_forecast?.length)) {
    histMarkLines.push({
      xAxis: boundaryDate,
      label: { formatter: '预测起点', color: '#9499A0', fontSize: 10, position: 'insideEndTop' },
      lineStyle: { type: 'dashed', color: '#9499A0', width: 1, opacity: 0.7 },
    })
  }

  const fcMarkLines: any[] = []
  if (targetPrice != null) {
    fcMarkLines.push({
      yAxis: targetPrice,
      label: {
        position: 'end', formatter: `目标价 ${targetPrice.toFixed(2)}`,
        backgroundColor: forecastColor, color: '#fff', padding: [2, 4], borderRadius: 2, fontSize: 10,
      },
      lineStyle: { type: 'dashed', color: forecastColor, width: 1, opacity: 0.8 },
    })
  }

  const option = {
    backgroundColor: 'transparent',
    animationDuration: 500,
    animationDurationUpdate: 350,
    legend: {
      top: 0,
      right: 20,
      data: ['K线', 'MA5', 'MA10', 'MA20', '预测蜡烛', '预测中位', '置信区间'],
      textStyle: { color: '#9499A0', fontSize: 11 },
      itemWidth: 10,
      itemHeight: 2,
    },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross', lineStyle: { color: '#00AEEC', width: 1, type: 'dashed' } },
      borderWidth: 0,
      backgroundColor: 'rgba(255, 255, 255, 0.98)',
      padding: 12,
      textStyle: { color: '#18191C', fontSize: 12 },
      extraCssText: 'box-shadow: 0 8px 24px rgba(0,0,0,0.15); border-radius: 8px; max-width: 320px; white-space: normal;',
      formatter: function (params: any) {
        const list = Array.isArray(params) ? params : [params]
        if (!list.length) return ''
        const name = list[0].name
        const idx = list[0].dataIndex
        let res = `<div style="margin-bottom:5px; font-weight:bold; color:#9499A0">${name}</div>`

        const row = (k: string, v: string, color = '#18191C') => `
          <div style="display:flex; justify-content:space-between; gap:20px; margin-bottom:3px;">
            <span style="color:#9499A0">${k}</span>
            <span style="font-weight:bold; color:${color}">${v}</span>
          </div>`

        for (const item of list) {
          const sname = item.seriesName
          if (sname === 'K线' || sname === '预测蜡烛') {
            const d = item.data
            // candlestick 的 data 是 [dataIndex, open, close, low, high]（ECharts 会前插 index）
            if (!d || d[1] == null) continue
            const open = Number(d[1]), close = Number(d[2]), low = Number(d[3]), high = Number(d[4])
            if (sname === '预测蜡烛') {
              res += `<div style="color:${forecastColor}; font-weight:bold; margin-bottom:3px;">预测蜡烛（合成影线）</div>`
            }
            res += row('开盘', open.toFixed(2))
            res += row('收盘', close.toFixed(2), close >= open ? upColor : downColor)
            res += row('最低', low.toFixed(2))
            res += row('最高', high.toFixed(2))
          } else if (sname === '置信区间') {
            // 该 series 画的是 (p90 − p10) 的堆叠增量，直接显示会误导 —— 显示原始区间
            const lo = p10[idx], hi = p90[idx]
            if (lo != null && hi != null) {
              res += row('P10–P90', `${Number(lo).toFixed(2)} ~ ${Number(hi).toFixed(2)}`, forecastColor)
            }
          } else if (sname === 'p10') {
            continue                                  // 隐形基线，不进 tooltip
          } else if (item.value != null) {
            res += row(sname, Number(item.value).toFixed(2), item.color)
          }
        }

        // 当日公告事件（打点的 tooltip 由 axis trigger 带不出来，这里按日期回查）
        const evts = (props.eventMarkers || []).filter(m => m.date === name)
        for (const e of evts) {
          const color = predictionEventTypeColor(e.event_type)
          const score = typeof e.score === 'number' ? ` · 负面度 ${e.score.toFixed(2)}` : ''
          res += `<div style="margin-top:6px; padding-top:6px; border-top:1px dashed #E3E5E7; color:${color}; font-size:11px; line-height:1.5;">
              ${predictionEventTypeLabel(e.event_type)}${score}<br/>${e.title || ''}
            </div>`
        }
        return res
      },
    },
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    grid: [
      { left: '20', right: '60', top: '35', height: '65%', containLabel: true },
      { left: '20', right: '60', top: '78%', height: '15%', containLabel: true },
    ],
    xAxis: [
      {
        type: 'category',
        data: allDates,
        boundaryGap: false,
        axisLine: { lineStyle: { color: 'var(--cf-border-soft)' } },
        axisLabel: { color: '#9499A0', fontSize: 10, maxInterval: 30 },
        splitLine: { show: false },
        min: 'dataMin', max: 'dataMax',
      },
      {
        type: 'category',
        gridIndex: 1,
        data: allDates,
        boundaryGap: false,
        axisLine: { lineStyle: { color: 'var(--cf-border-soft)' } },
        axisTick: { show: false },
        splitLine: { show: false },
        axisLabel: { show: false },
        min: 'dataMin', max: 'dataMax',
      },
    ],
    yAxis: [
      {
        scale: true,
        position: 'right',
        axisLabel: { color: '#9499A0', fontSize: 10 },
        splitLine: { lineStyle: { color: 'var(--cf-border-soft)', type: 'dashed' } },
      },
      {
        scale: true,
        gridIndex: 1,
        splitNumber: 2,
        axisLabel: { show: false },
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { show: false },
      },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1], start: 70, end: 100 },
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
        textStyle: { color: 'transparent' },
      },
    ],
    series: [
      // ── 历史 K 线（实体蜡烛）+ 现价/支撑/压力/分界线 + 公告事件打点 ──
      {
        name: 'K线',
        type: 'candlestick',
        data: histValues,
        itemStyle: {
          color: upColor, color0: downColor,
          borderColor: upColor, borderColor0: downColor,
        },
        markLine: {
          symbol: ['none', 'none'],
          silent: true,
          data: histMarkLines,
        },
        markPoint: {
          // 公告事件 pin（爆雷红/定增橙/减持紫，颜色来自 types 的唯一真相源）
          data: eventPoints,
          tooltip: {
            formatter: (p: any) => {
              const e = p?.data?._evt
              if (!e) return p?.name ?? ''
              const score = typeof e.score === 'number' ? ` · 负面度 ${e.score.toFixed(2)}` : ''
              return `${e.date} · ${e.type}${score}<br/>${e.title}`
            },
          },
        },
      },
      { name: 'MA5', type: 'line', data: ma5, smooth: true, showSymbol: false, lineStyle: { color: '#FF9736', width: 1.2 } },
      { name: 'MA10', type: 'line', data: ma10, smooth: true, showSymbol: false, lineStyle: { color: '#00AEEC', width: 1.2 } },
      { name: 'MA20', type: 'line', data: ma20, smooth: true, showSymbol: false, lineStyle: { color: '#FB7299', width: 1.2 } },

      // ── 预测蜡烛：虚化边框 + 半透明，与历史实体蜡烛一眼可分（一根根长出来）──
      {
        name: '预测蜡烛',
        type: 'candlestick',
        data: fcCandles,
        itemStyle: {
          color: 'rgba(242, 93, 89, 0.28)',
          color0: 'rgba(0, 181, 120, 0.28)',
          borderColor: upColor,
          borderColor0: downColor,
          borderWidth: 1.2,
          opacity: 0.75,
        },
      },

      // ── 预测中位线：粉色虚线，null 桥接到最后历史收盘，终点钉目标价 ──
      {
        name: '预测中位',
        type: 'line',
        data: pointForecast,
        smooth: true,
        showSymbol: false,
        connectNulls: false,
        lineStyle: { color: forecastColor, type: 'dashed', width: 2 },
        endLabel: {
          show: true,
          color: forecastColor,
          fontSize: 11,
          fontWeight: 'bold',
          formatter: (p: any) => (p.value == null ? '' : Number(p.value).toFixed(2)),
        },
        markLine: {
          symbol: ['none', 'none'],
          silent: true,
          data: fcMarkLines,
        },
        // 拐点标注：markPoint 在预测曲线局部极值
        markPoint: {
          symbol: 'pin',
          symbolSize: 44,
          data: (props.inflectionPoints || []).map((p) => ({
            coord: [p.date, p.price],
            value: p.type === 'peak' ? '峰' : '谷',
            itemStyle: { color: p.type === 'peak' ? upColor : downColor },
            label: { color: '#fff', fontSize: 10, fontWeight: 600 },
          })),
        },
      },

      // ── 置信带下界 p10（堆叠面积带的隐形下沿）──
      {
        name: 'p10',
        type: 'line',
        data: p10,
        smooth: true,
        showSymbol: false,
        lineStyle: { opacity: 0 },
        stack: 'confidence-band',
        symbol: 'none',
        silent: true,
      },
      // ── 置信带上界 p90：与 p10 的差值形成阴影带；渐宽渐淡（不确定性随 horizon 增长）──
      {
        name: '置信区间',
        type: 'line',
        data: p90.map((v, i) => {
          const lower = p10[i]
          if (v == null || lower == null) return null
          return (v as number) - (lower as number)
        }),
        smooth: true,
        showSymbol: false,
        lineStyle: { opacity: 0 },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 1, 0, [
            { offset: 0, color: 'rgba(251, 114, 153, 0.25)' },
            { offset: 1, color: 'rgba(251, 114, 153, 0.08)' },
          ]),
        },
        stack: 'confidence-band',
        symbol: 'none',
        silent: true,
      },
    ],
  }

  // 首帧建结构（notMerge），其后合并更新 —— 否则每根新蜡烛都会重放整图入场动画
  chartInstance.setOption(option as any, !_rendered)
  _rendered = true
}

function handleResize() {
  chartInstance?.resize()
}

function disposeChart() {
  chartInstance?.dispose()
  chartInstance = null
  _rendered = false
}

onMounted(async () => {
  await nextTick()
  renderChart()
  window.addEventListener('resize', handleResize)
})

onUnmounted(() => {
  window.removeEventListener('resize', handleResize)
  disposeChart()
})

// 数据变化时重绘（空 → 有数据时需先等 v-if 把 DOM 挂上，故 nextTick）。
// forecastCandles 每到一根就触发一次 → 蜡烛逐根长出来。
watch(
  () => [
    props.historicalData,
    props.forecastData,
    props.forecastCandles,
    props.eventMarkers,
    props.inflectionPoints,
    props.technicalIndicators,
  ],
  async () => {
    await nextTick()
    renderChart()
  },
  { deep: true },
)
</script>

<template>
  <div class="prediction-chart">
    <div class="chart-header">
      <span class="area-title">
        {{ symbol }} 预测走势
        <span v-if="candleCount" class="candle-count">预测蜡烛 {{ candleCount }} 根</span>
      </span>
      <div v-if="hasData" class="legend-hint">
        <span class="legend-item"><span class="candle-dot"></span> 预测蜡烛</span>
        <span class="legend-item"><span class="dot" style="background:#FB7299"></span> 预测中位</span>
        <span class="legend-item"><span class="band-dot"></span> 置信区间 (P10–P90)</span>
        <span class="legend-item"><span class="dot" style="background:#F25D59"></span> 爆雷</span>
        <span class="legend-item"><span class="dot" style="background:#FF9F1C"></span> 定增</span>
        <span class="legend-item"><span class="dot" style="background:#9B59B6"></span> 减持</span>
      </div>
    </div>

    <!-- 空态：无历史行情且无预测结果（预测失败 / 行情拉不到 / 还没跑到 compute） -->
    <div v-if="!hasData" class="chart-empty">
      <el-empty :image-size="90" :description="emptyHint || '暂无行情与预测数据'" />
    </div>
    <div v-else ref="chartRef" class="echarts-dom"></div>

    <!-- 免责声明：任何形态下都在（含空态）。影线口径必须写明，不假装是真实日内极值 -->
    <div class="chart-disclaimer">
      预测曲线由模型基于历史行情与资讯情绪推演，仅供研究参考，不构成投资建议。
      <template v-if="candleCount">
        预测蜡烛的<b>影线由历史平均影线比例合成，不代表真实日内极值分布</b>。
      </template>
    </div>
  </div>
</template>

<style scoped>
.prediction-chart {
  display: flex;
  flex-direction: column;
  height: 500px;
  background: var(--cf-card);
  border-radius: 12px;
  border: 1px solid var(--cf-border-soft);
  padding: 16px;
}
.chart-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}
.area-title {
  font-size: 14px;
  font-weight: 700;
  color: var(--cf-text-2);
  display: flex;
  align-items: center;
  gap: 8px;
}
.candle-count {
  font-size: 11px;
  font-weight: 600;
  color: #FB7299;
  background: rgba(251, 114, 153, 0.1);
  padding: 1px 6px;
  border-radius: 4px;
}
.legend-hint {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  font-size: 11px;
  color: var(--cf-text-4);
}
.legend-item {
  display: flex;
  align-items: center;
  gap: 5px;
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
}
.candle-dot {
  width: 7px;
  height: 11px;
  display: inline-block;
  border: 1px dashed #F25D59;
  background: rgba(242, 93, 89, 0.25);
  border-radius: 1px;
}
.band-dot {
  width: 10px;
  height: 8px;
  background: rgba(251, 114, 153, 0.25);
  border: 1px solid rgba(251, 114, 153, 0.5);
  border-radius: 2px;
  display: inline-block;
}
.echarts-dom {
  width: 100%;
  flex: 1;
  min-height: 380px;
}
.chart-empty {
  flex: 1;
  min-height: 380px;
  display: flex;
  align-items: center;
  justify-content: center;
}
.chart-disclaimer {
  padding-top: 8px;
  font-size: 11px;
  color: var(--cf-text-4);
  text-align: center;
  line-height: 1.5;
}
</style>
