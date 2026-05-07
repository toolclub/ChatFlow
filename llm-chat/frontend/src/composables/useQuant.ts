import { ref, computed, watch } from 'vue'
import * as api from '../api'
import type { QuantProviderInfo, QuantScreenCriteria, QuantScreenResult } from '../types'
import type { QuantCacheStatus } from '../api'

/**
 * 量化工作台 composable。注意：非单例 — 每次 useQuant() 都是独立 state，
 * 子组件不要再 useQuant 一次（会和父级断开）。
 */
export function useQuant(initialMarket?: string) {
  const providers = ref<QuantProviderInfo[]>([])
  const loadingProviders = ref(false)

  const isScreening = ref(false)
  const isAnalyzing = ref(false)
  // 阶段提示（hint）：fetching_spot / scoring / fetching_bars / analyzing
  // 不靠后端推；前端按 wall-clock 时间和阶段经验推断，给用户"在动"的反馈
  const screenStage = ref<string>('')

  const screenResult = ref<QuantScreenResult | null>(null)
  const errorMsg = ref<string>('')
  const analysisStreaming = ref<string>('')
  // 推理过程（reasoning_content）— 仅推理模型推送；展示在思考折叠块
  const analysisThinking = ref<string>('')

  const cacheStatus = ref<QuantCacheStatus | null>(null)

  let _analyzeAbort: AbortController | null = null
  let _stageTimer: number | null = null

  const cacheAgeText = computed<string>(() => {
    const s = cacheStatus.value?.spot_latest
    if (!s) return '尚未缓存'
    const sec = s.age_seconds
    if (sec < 60) return `${sec}秒前`
    if (sec < 3600) return `${Math.floor(sec / 60)}分钟前`
    if (sec < 86400) return `${Math.floor(sec / 3600)}小时前`
    return `${Math.floor(sec / 86400)}天前`
  })

  const criteria = ref<QuantScreenCriteria>({
    market: initialMarket || 'cn_a',
    universe: initialMarket === 'us_stock' ? 'nasdaq' : 'hs300',
    top_n: 50,
    min_market_cap: initialMarket === 'us_stock' ? 100 : 500,
    pe_range: [0, 100],
    momentum_window: 60,
    volatility_window: 20,
    exclude_st: true,
    exclude_suspended: true,
    weights: {
      technical: 0.35,
      fundamental: 0.35,
      liquidity: 0.20,
      risk: 0.10,
    },
  })

  // 市场切换时自动重置股票池
  watch(() => criteria.value.market, (newMarket) => {
    if (newMarket === 'us_stock') {
      criteria.value.universe = 'nasdaq'
      criteria.value.min_market_cap = 100 // 美股通常用美元，单位可能不同，这里仅作默认
    } else {
      criteria.value.universe = 'hs300'
      criteria.value.min_market_cap = 500
    }
  })

  async function loadProviders() {
    loadingProviders.value = true
    try {
      providers.value = await api.fetchQuantProviders()
    } catch (e: any) {
      errorMsg.value = e.message || 'Failed to load providers'
    } finally {
      loadingProviders.value = false
    }
  }

  async function loadCacheStatus() {
    try {
      cacheStatus.value = await api.fetchQuantCacheStatus()
    } catch {
      cacheStatus.value = null
    }
  }

  async function refreshCacheNow() {
    try {
      await api.refreshQuantCache()
      // 等几秒再重新拉状态，给后台 warmer 一点时间
      setTimeout(loadCacheStatus, 3000)
    } catch (e: any) {
      errorMsg.value = e.message || '触发缓存刷新失败'
    }
  }

  function _startStageHint() {
    _clearStageHint()
    let elapsed = 0
    screenStage.value = '读取行情快照...'
    _stageTimer = window.setInterval(() => {
      elapsed += 2
      // 经验值：spot 缓存命中 <1s；冷启 spot 30-60s；bars 60-120s
      if (elapsed < 5) screenStage.value = '读取行情快照...'
      else if (elapsed < 15) screenStage.value = '正在筛选候选标的...'
      else if (elapsed < 60) screenStage.value = '正在计算因子（动量/波动/估值）...'
      else if (elapsed < 90) screenStage.value = '历史 K 线缓存未命中，正在拉取最近 80 个交易日...'
      else screenStage.value = '后台数据回源较慢，预计 1-2 分钟，下次会快很多（已自动写盘缓存）'
    }, 2000) as unknown as number
  }

  function _clearStageHint() {
    if (_stageTimer !== null) {
      clearInterval(_stageTimer)
      _stageTimer = null
    }
  }

  async function runScreen() {
    if (_analyzeAbort) {
      _analyzeAbort.abort()
      _analyzeAbort = null
    }
    isScreening.value = true
    isAnalyzing.value = false
    errorMsg.value = ''
    analysisStreaming.value = ''
    analysisThinking.value = ''
    _startStageHint()
    try {
      // 1. 发起选股（后端立即返回 ID）
      const initRes = await api.runQuantScreen(criteria.value)
      if (initRes.snapshot_id) {
        // 2. 进入轮询等待结果
        await _pollResult(initRes.snapshot_id)
      }
    } catch (e: any) {
      errorMsg.value = e.message || 'Screening failed'
      isScreening.value = false
      _clearStageHint()
    }
  }

  async function _pollResult(snapshotId: string) {
    const MAX_POLLS = 100 // 约 200 秒
    let count = 0
    
    const timer = setInterval(async () => {
      count++
      if (count > MAX_POLLS) {
        clearInterval(timer)
        errorMsg.value = '筛选任务超时，请稍后刷新页面查看'
        isScreening.value = false
        _clearStageHint()
        return
      }

      try {
        const snap = await api.fetchQuantSnapshot(snapshotId)
        if (snap.status === 'DONE') {
          clearInterval(timer)
          screenResult.value = snap
          isScreening.value = false
          _clearStageHint()
          
          if (snap.rows?.length > 0) {
            void startAnalyze(snapshotId)
          }
          void loadCacheStatus()
        } else if (snap.status === 'FAILED') {
          clearInterval(timer)
          errorMsg.value = '选股逻辑执行失败，请检查参数或查看后端日志'
          isScreening.value = false
          _clearStageHint()
        }
        // COMPUTING 状态则继续等待
      } catch (e) {
        console.error('Polling error', e)
      }
    }, 2000)
  }

  async function checkActiveSession() {
    try {
      const res = await api.fetchActiveQuantSession(criteria.value.market)
      if (res.active && res.snapshot_id) {
        if (res.criteria) {
          criteria.value = { ...criteria.value, ...res.criteria }
        }

        // 如果已经完成，直接拉取快照展示
        if (res.status === 'DONE') {
          const snap = await api.fetchQuantSnapshot(res.snapshot_id)
          screenResult.value = snap
          if (snap.rows?.length > 0 && !snap.analysis) {
            void startAnalyze(res.snapshot_id)
          }
          void loadCacheStatus()
          return
        }

        // 否则进入轮询等待
        isScreening.value = true
        _startStageHint()
        await _pollResult(res.snapshot_id)
      }
    } catch (e) {
      console.warn('Check active session failed', e)
    }
  }

  async function startAnalyze(snapshotId: string) {
    isAnalyzing.value = true
    analysisStreaming.value = ''
    analysisThinking.value = ''
    _analyzeAbort = new AbortController()
    try {
      await api.streamQuantAnalyze(
        snapshotId,
        {
          onThinking: (text) => {
            analysisThinking.value += text
          },
          onDelta: (delta) => {
            analysisStreaming.value += delta
            if (screenResult.value) {
              screenResult.value.analysis = analysisStreaming.value
            }
          },
          onDone: (analysis, riskNotes) => {
            if (screenResult.value) {
              screenResult.value.analysis = analysis || analysisStreaming.value
              screenResult.value.risk_notes = riskNotes || []
            }
          },
          onError: (msg) => {
            if (screenResult.value && !screenResult.value.analysis) {
              screenResult.value.analysis = `（分析失败：${msg}）`
            }
          },
        },
        _analyzeAbort.signal,
      )
    } finally {
      isAnalyzing.value = false
      _analyzeAbort = null
    }
  }
async function loadStockChart(symbol: string) {
  try {
    return await api.fetchStockChart(symbol)
  } catch (e: any) {
    errorMsg.value = e.message || '获取图表数据失败'
    return null
  }
}

return {
  providers,
  loadingProviders,
  isScreening,
  isAnalyzing,
  screenStage,
  screenResult,
  errorMsg,
  criteria,
  analysisStreaming,
  analysisThinking,
  cacheStatus,
  cacheAgeText,
  loadProviders,
  loadCacheStatus,
  refreshCacheNow,
  runScreen,
  checkActiveSession,
  loadStockChart,
}
}

