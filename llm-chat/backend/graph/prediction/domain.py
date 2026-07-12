"""股票预测领域模型（Pydantic v2）—— 预测图各节点 / 前后端的数据契约。

所有结构化结果走独立 JSONB 字段（prediction_snapshots.*），不塞 message.content（铁律 #2）。
"""
from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator

from graph.prediction.dimensions import DIMENSION_REGISTRY, default_dimensions

logger = logging.getLogger("graph.prediction.domain")

# 市场枚举（目前仅 A 股 / 美股）
Market = Literal["cn_a", "us_stock"]

# 数据来源可信度（用户拍板：数据口径有缺口，UI 必须诚实标注，不假装精确）
#   structured   —— 硬结构化字段（akshare 的「公告类型」列、业绩预告「预告类型」、SEC 8-K item 编号）
#   llm_inferred —— 公告标题经 LLM 语义判定（大股东减持、爆雷严重度多属此类）
#   estimated    —— 数值取不到，用保守兜底常量估算（如拿不到总股本时的摊薄比例）
Confidence = Literal["structured", "llm_inferred", "estimated"]


# ── 请求 ──────────────────────────────────────────────────────────────────────

class PredictionCriteria(BaseModel):
    """预测请求条件（存 prediction_snapshots.criteria JSONB）。"""
    symbol: str                              # A 股如 000001.SZ；美股如 AAPL
    market: Market
    # 用户拍板：7 个交易日（画 7 根预测蜡烛）。前端标注「未来 7 个交易日（约 1.5 自然周）」。
    horizon: int = 7
    methods: list[str] = Field(
        default_factory=lambda: ["technical", "arima", "garch", "montecarlo", "news"]
    )
    # 预测维度；None = 该市场的默认勾选维度（见 dimensions.default_dimensions）
    dimensions: list[str] | None = None

    @field_validator("dimensions")
    @classmethod
    def _validate_dimensions(
        cls, v: list[str] | None, info: ValidationInfo,
    ) -> list[str] | None:
        """校验维度 key 合法 + 与市场匹配。

        v1 没有任何校验：传垃圾 key 会照跑 Tavily 烧钱（一维 ≈ 1 次 LLM + 2 次 Tavily）。
        这里静默丢弃非法 key 而不是抛 400 —— 单个坏 key 不该让整单预测失败，
        但必须记 warning（铁律 #9），且全部非法时回退默认维度而非跑一个空图。

        注意：market 在 dimensions 之前声明，所以此处 info.data 里一定已有 market
        （除非 market 自己校验失败 —— 那时跳过市场匹配检查，让 market 的错误自己冒泡）。
        """
        if v is None:
            return None

        market = info.data.get("market")

        valid: list[str] = []
        unknown: list[str] = []
        wrong_market: list[str] = []
        seen: set[str] = set()
        for key in v:
            if key in seen:
                continue
            seen.add(key)
            dim = DIMENSION_REGISTRY.get(key)
            if dim is None:
                unknown.append(key)
            elif market is not None and not dim.supports(market):
                wrong_market.append(key)
            else:
                valid.append(key)

        if unknown:
            logger.warning("预测维度未注册，已忽略: %s", unknown)
        if wrong_market:
            logger.warning("预测维度不支持市场 %s，已忽略: %s", market, wrong_market)

        if not valid:
            fallback = default_dimensions(market)
            logger.warning(
                "预测维度全部非法（传入 %s），回退默认维度 %d 个", v, len(fallback),
            )
            return fallback
        return valid

    def resolved_dimensions(self) -> list[str]:
        """实际要跑的维度：显式传入则用之，否则用该市场的默认勾选维度。"""
        if self.dimensions:
            return list(self.dimensions)
        return default_dimensions(self.market)


# ── 资讯搜索（搜索型维度）────────────────────────────────────────────────────────

class NewsItem(BaseModel):
    """单条资讯结果（完整原文，不截取）。"""
    title: str = ""
    url: str = ""
    snippet: str = ""        # tavily content 字段（摘要）
    full_content: str = ""  # raw_content 完整原文（不截断——用户明确要求拿全部数据）


class NewsDimensionResult(BaseModel):
    """单维度资讯搜索结果 + LLM 情绪打分。"""
    dimension: str
    query: str
    answer: str = ""                         # tavily 综合答案
    results: list[NewsItem] = Field(default_factory=list)
    sentiment: float = 0.0                   # -1..1，LLM 打分（蒙特卡洛漂移修正因子）
    sentiment_reason: str = ""               # 一句理由
    # 维度级降级：数据源挂了只标该维 failed，其余维度继续（不整单 failed）。
    # 恢复续跑时据此判断哪些维度还要重跑（failed 的要重跑）。
    failed: bool = False
    error: str = ""


# ── 数据型维度（公告 / 定增 / 减持 / SEC）────────────────────────────────────────

class FilingItem(BaseModel):
    """单条结构化披露事件（公告 / 业绩预告 / SEC filing）。

    诚实标注（用户拍板）：`source` + `confidence` 必须随卡片一起显示。
    v2 只用「标题 + 公告类型」，不解析 PDF 正文 —— UI 要写明这一点。
    """
    title: str = ""
    date: str = ""                    # 公告日期 YYYY-MM-DD
    ann_type: str = ""                # 公告类型（akshare 返回结果里的「公告类型」列 / SEC form 类型）
    url: str = ""

    # 判定结果（与 news_search.score_sentiment 的输出契约同构，analyze_node 可直接复用聚合逻辑）
    event_type: str = ""              # negative | placement | reduction | st_delisting_risk | violation | neutral
    negative_score: float = 0.0       # -1..0（0 = 无负面）
    reason: str = ""                  # 一句理由

    # 诚实标注
    source: str = ""                  # akshare_notice | akshare_yjyg | sec_edgar | ...
    confidence: Confidence = "llm_inferred"

    # 量化字段（取不到则为 None，UI 标「估算」，不假装精确）
    dilution_ratio: float | None = None    # 摊薄比例 = 发行股数 / 总股本
    discount_rate: float | None = None     # 折价率 = (最新价 − 发行价) / 最新价
    reduction_ratio: float | None = None   # 减持占总股本比例
    decay_weight: float | None = None      # 时间衰减权重 w = exp(-Δt/τ)

    # 解禁 / 回购（lockup_buyback 维度）。
    # ★ FilingItem 是 pydantic 模型，**没在这里声明的键会被 FilingItem(**d) 静默丢弃** ——
    #   踩过：fetch_lockup_buyback 明明返回了 lockup_pct/days_until，但没在此声明，
    #   经 _filing_item 归一化后全被吃掉 → _intensity 读不到 → 解禁强度恒为 0 →
    #   维度分 0.0 → 事件分里根本看不到解禁这一路（数据卡片却照常显示，极具迷惑性）。
    lockup_pct: float | None = None        # 解禁占总市值比例（%），抛压强度的输入
    days_until: int | None = None          # 距解禁还有几天（前瞻加权：越近抛压越重）
    support_ratio: float | None = None     # 回购占总股本比例（%），托底强度


class FilingDimensionResult(BaseModel):
    """单个数据型维度的结果（存 prediction_snapshots.filing_digest JSONB 数组的一项）。

    与 NewsDimensionResult 平级：两者的 `dimension` / `failed` 字段同名，
    恢复续跑时统一按 `{d.dimension for d in news_digest + filing_digest if not d.failed}` 求已完成集合。
    """
    dimension: str
    kind: Literal["data"] = "data"
    items: list[FilingItem] = Field(default_factory=list)

    # 该维度的饱和分 S ∈ (-1, 0]（设计文档 §5.3 的 S_neg / S_pp / S_sr）
    score: float = 0.0
    summary: str = ""

    # 工具轨迹 / 成本可观测（前端"演出"工具调用用）
    fetcher: str = ""                 # 实际调用的取数函数名
    total_fetched: int = 0            # 原始拉取条数（"返回 388 条"）
    candidates: int = 0               # 规则过滤后进入 LLM 候选池的条数
    llm_judged: int = 0               # 真正走了 LLM 的条数
    cache_hits: int = 0               # 命中 announcement_cache 的条数
    elapsed_ms: int = 0

    # UI 必须显示的口径说明（如「基于公告标题与类型分类，未解析 PDF 正文」）
    source_note: str = ""

    # 维度级降级
    failed: bool = False
    error: str = ""


# ── 计算引擎输出 ───────────────────────────────────────────────────────────────

class ModelParams(BaseModel):
    """公式全参数（设计文档 §5.2）—— 让预测「可解释」而不是算完就扔。

    v1 把 ARIMA order/AIC、GARCH ω/α/β、蒙特卡洛 N/分位数全丢在局部变量里，
    最多 log 一行。v2 全链路贯穿：engine → ForecastResult.model_params
    → prediction_snapshots.model_params JSONB → 前端公式卡片。
    """
    last_close: float = 0.0

    # ── ARIMA ──
    arima_order: tuple[int, int, int] = (0, 0, 0)     # (p, d, q)
    arima_aic: float = 0.0
    arima_grid_tried: list[dict] = Field(default_factory=list)  # [{order, aic}]，展示网格搜索过程
    arima_drift_annual: float = 0.0                   # μ_ARIMA

    # ── GARCH(1,1) ──
    garch_omega: float = 0.0
    garch_alpha: float = 0.0
    garch_beta: float = 0.0
    garch_persistence: float = 0.0                    # α+β（<1 才平稳，直接给用户看这个健康度）
    garch_sigma_annual: float = 0.0                   # σ_GARCH

    # ── 情绪 / 事件（设计文档 §5.3）──
    news_sentiment: float = 0.0                       # S_news ∈ [-1, 1]
    event_scores: dict = Field(default_factory=dict)  # {negative: -0.39, placement: -0.12, reduction: -0.25}
    event_weights: dict = Field(default_factory=dict) # {negative: 0.5, placement: 0.2, reduction: 0.3}
    filing_score: float = 0.0                         # F = clamp(Σ wᵢ·Sᵢ, -1, 0)
    filing_weight: float = 0.0                        # λ（PREDICTION_FILING_WEIGHT）
    sentiment_total: float = 0.0                      # S_total = (1−λ)·S_news + λ·F

    # ── 最终代入 GBM 的值 ──
    alpha_used: float = 0.0                           # α（NEWS_SENTIMENT_ALPHA = 0.15）
    drift_mu_annual_final: float = 0.0                # μ = μ_ARIMA + α·S_total
    sigma_annual_final: float = 0.0                   # σ = σ_GARCH × M_news × M_event
    vol_multiplier_news: float = 1.0                  # M_news
    vol_multiplier_event: float = 1.0                 # M_event = 1 + k·|min(0, F)|

    # ── 蒙特卡洛 ──
    mc_n_paths: int = 0
    mc_percentiles: tuple = ()
    mc_dt: float = 0.0                                # 1/252

    # ── 影线标定（预测 K 线用，设计文档 §5.4）──
    shadow_k_up: float = 0.0
    shadow_k_dn: float = 0.0
    shadow_lookback_bars: int = 0


class FormulaStep(BaseModel):
    """单条公式推导（SSE FORMULA_STEP + prediction_snapshots.formula_steps JSONB）。

    前端公式卡片：等宽公式 + 参数表 + 代入 + 结果 + 解释。
    formula_latex 先下发不渲染（前端暂无 LaTeX 依赖，P3 再上 KaTeX）。
    """
    key: str                                  # arima | garch | event_score | sentiment_total | gbm | shadow
    title: str = ""                           # 卡片标题，如 "ARIMA 漂移项"
    formula: str = ""                         # 等宽纯文本公式
    formula_latex: str = ""                   # 同一公式的 LaTeX（前端暂存不渲染）
    params: dict = Field(default_factory=dict)   # 参数名 → 值（渲染参数表）
    substituted: str = ""                     # 代入实际数字后的算式
    result: float | None = None               # 数值结果
    result_text: str = ""                     # 非数值结果 / 带单位的展示串
    explain: str = ""                         # 一句人话解释：这一步在干什么、为什么


class ForecastCandle(BaseModel):
    """单根预测蜡烛（SSE FORECAST_DAY + prediction_snapshots.forecast_candles JSONB）。

    OHLC 由蒙特卡洛路径合成（§5.4）：跨 2000 条路径对 O/H/L/C 各取中位数。
    诚实标注（UI 必须写）：「预测蜡烛的影线由历史平均影线比例合成，不代表真实日内极值分布」。
    """
    day: int                                  # 第几个交易日（1..horizon）
    date: str                                 # YYYY-MM-DD
    o: float
    h: float
    l: float
    c: float
    p10: float = 0.0                          # 置信带下沿
    p50: float = 0.0                          # 中位线
    p90: float = 0.0                          # 置信带上沿


class EventMarker(BaseModel):
    """K 线上的公告事件打点（SSE EVENT_MARKER + prediction_snapshots.event_markers JSONB）。"""
    date: str                                 # YYYY-MM-DD
    event_type: str                           # negative | placement | reduction | ...
    title: str = ""
    score: float = 0.0                        # -1..0
    dimension: str = ""                       # 来自哪个维度


class ForecastResult(BaseModel):
    """计算引擎输出（ARIMA + GARCH + 蒙特卡洛 + 技术指标 + 资讯/事件修正）。"""
    dates: list[str] = Field(default_factory=list)           # 预测日期序列
    point_forecast: list[float] = Field(default_factory=list)  # 中位点预测
    p10: list[float] = Field(default_factory=list)           # 10 分位（置信带下）
    p50: list[float] = Field(default_factory=list)           # 50 分位（中位线）
    p90: list[float] = Field(default_factory=list)           # 90 分位（置信带上）
    inflection_points: list[dict] = Field(default_factory=list)  # [{date, price, type:"peak"|"trough"}]
    confidence: float = 0.0                                  # 0..1
    model_name: str = ""                                     # e.g. "ARIMA+GARCH+MC+news"
    technical_indicators: dict = Field(default_factory=dict)  # {rsi, macd_signal, boll_pos, kdj_j, ...}

    # ── v2 新增 ──
    model_params: ModelParams | None = None                  # 公式全参数（§5.2）
    forecast_candles: list[ForecastCandle] = Field(default_factory=list)  # 预测 OHLC 蜡烛（§5.4）
    formula_steps: list[FormulaStep] = Field(default_factory=list)        # 公式推导序列（§5.7）


# ── 最终结果 ──────────────────────────────────────────────────────────────────

class PredictionResult(BaseModel):
    """预测最终结果（对应 prediction_snapshots 多个字段）。"""
    status: str
    forecast: ForecastResult | None = None
    news_digest: list[NewsDimensionResult] = Field(default_factory=list)
    news_sentiment: float = 0.0
    analysis: str = ""
    risk_notes: list[str] = Field(default_factory=list)
    thinking_segments: list[dict] = Field(default_factory=list)
    error_message: str = ""

    # ── v2 新增 ──
    filing_digest: list[FilingDimensionResult] = Field(default_factory=list)  # 数据型维度结果
    event_score: float = 0.0                                 # §5.3 的 F
    event_markers: list[EventMarker] = Field(default_factory=list)


# ── 历史 K 线 → 前端图表契约 ────────────────────────────────────────────────────

# 空图表结构：旧记录 / bars_df 缺列时返回，前端据此回退现拉（fetchStockChart）。
_EMPTY_CHART: dict = {"dates": [], "values": [], "volumes": [], "ma5": [], "ma10": [], "ma20": []}


def bars_to_chart(bars_df) -> dict:
    """预测时内存里的 bars_df → 前端 PredictionChart 需要的图表结构。

    落库用（prediction_snapshots.historical_bars）：预测跑到 compute 成功后调一次，
    把历史 K 线固化进 DB，已完成记录再点开时前端直接渲染、不再现拉行情（美股链路会被限流卡死）。

    转换逻辑照抄自 quant/service.py:get_stock_chart_data（约 192-228 行，选股侧）——
    刻意复制而非 import，避免预测侧依赖选股服务；两侧输出结构须保持一致。

    契约（★ 与选股侧一致，前端 candlestick / volume 直接吃）：
      - dates:   ["YYYY-MM-DD", ...]（升序）
      - values:  [[open, close, low, high], ...]  ★ ECharts candlestick 序，不是 OHLC
      - volumes: [[i, volume, flag], ...]，flag = close>=open ? 1 : -1
      - ma5/ma10/ma20: close.rolling(N).mean()，NaN → None（★ 不是 0）

    防御：bars_df 为 None / 空 / 缺 date/open/high/low/close/volume 任一列 →
    返回空结构（前端回退现拉），而不是抛异常（铁律 #9：不吞异常，但这里是可预期的降级）。
    """
    import pandas as pd

    if bars_df is None or not isinstance(bars_df, pd.DataFrame) or bars_df.empty:
        return dict(_EMPTY_CHART)

    required = {"date", "open", "high", "low", "close", "volume"}
    missing = required - set(bars_df.columns)
    if missing:
        logger.warning("bars_to_chart: bars_df 缺列 %s，返回空图表（前端将回退现拉）", sorted(missing))
        return dict(_EMPTY_CHART)

    df = bars_df.sort_values("date").reset_index(drop=True)

    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()

    def _safe_float(val):
        # 防御：列名重复时 row["open"] 可能返回 Series（不是标量），取首个值
        if isinstance(val, pd.Series):
            if val.empty:
                return None
            val = val.iloc[0]
        try:
            return None if pd.isna(val) else float(val)
        except (TypeError, ValueError):
            return None

    values = [
        [_safe_float(row["open"]), _safe_float(row["close"]), _safe_float(row["low"]), _safe_float(row["high"])]
        for _, row in df.iterrows()
    ]
    dates = df["date"].astype(str).tolist()
    volumes = []
    for i, row in df.iterrows():
        color_flag = 1 if row["close"] >= row["open"] else -1
        volumes.append([i, _safe_float(row["volume"]), color_flag])

    return {
        "dates": dates,
        "values": values,
        "volumes": volumes,
        "ma5": [_safe_float(x) for x in df["ma5"]],
        "ma10": [_safe_float(x) for x in df["ma10"]],
        "ma20": [_safe_float(x) for x in df["ma20"]],
    }
