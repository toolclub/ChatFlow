"""股票预测模块配置常量（自包含，不污染 quant/config.py 的选股配置）。

行情：预测节点用显式 start/end（2 年）调 get_adapter().bars()，provider 支持长区间；
      跨运行缓存如需保留长历史，可把 .env 的 QUANT_CACHE_RETENTION_DAYS 调到 ~730。
资讯：复用 .env TAVILY_API_KEY，但预算独立于聊天轮次（5 次/轮）——预测跑在独立 graph，
      不能共享 web_search 工具的 _default scope，否则 12 维立刻打爆上限。
"""
from __future__ import annotations

import logging
import os

from graph.prediction.dimensions import DIMENSION_REGISTRY

logger = logging.getLogger("graph.prediction.config")


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    """读 int 环境变量并钳制到 [minimum, maximum]；缺失/非法则回退 default。

    非法值记 warning（铁律 #9：不静默吞掉配置错误）。
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        val = int(raw.strip())
    except ValueError:
        logger.warning("%s=%r 非整数，回退默认值 %d", name, raw, default)
        return default
    clamped = max(minimum, min(maximum, val))
    if clamped != val:
        logger.warning(
            "%s=%d 超出允许区间 [%d, %d]，钳制为 %d", name, val, minimum, maximum, clamped,
        )
    return clamped


def _float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    """读 float 环境变量并钳制到 [minimum, maximum]；缺失/非法则回退 default（铁律 #9）。"""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        val = float(raw.strip())
    except ValueError:
        logger.warning("%s=%r 非浮点数，回退默认值 %s", name, raw, default)
        return default
    clamped = max(minimum, min(maximum, val))
    if clamped != val:
        logger.warning(
            "%s=%s 超出允许区间 [%s, %s]，钳制为 %s", name, val, minimum, maximum, clamped,
        )
    return clamped


# ── 行情数据 ──────────────────────────────────────────────────────────────────
PREDICTION_BARS_LOOKBACK_DAYS: int = 730   # 预测取 2 年日线（ARIMA/GARCH/蒙特卡洛热身够用）
# 默认预测 7 个交易日（用户拍板，设计文档 §5.5 / 开放问题 #1）：画 7 根预测蜡烛，
# 视觉上就是"7 天"。前端标注"未来 7 个交易日（约 1.5 自然周）"，并提供 5/7/10 三档。
PREDICTION_HORIZON_DAYS: int = _int_env("PREDICTION_HORIZON_DAYS", 7, 1, 30)

# ── Tavily 资讯搜索（与 tools/builtin/web_search.py 同源，但独立预算 + 全量不截取） ──
TAVILY_API_KEY: str = os.environ.get("TAVILY_API_KEY", "")
TAVILY_URL: str = "https://api.tavily.com/search"
TAVILY_TIMEOUT: float = 20.0
TAVILY_MAX_RESULTS: int = 10              # 拿全部结果（不截断数量）
TAVILY_CHUNKS_PER_SOURCE: int = 3
TAVILY_RETRY_TIMES: int = 3
TAVILY_RETRY_DELAY: float = 1.0           # 指数退避：1s → 2s → 4s
# 资讯实时性：Tavily days 参数（只返回最近 N 天发布的文章，basic search 才生效）
TAVILY_DAYS: int = int(os.environ.get("PREDICTION_TAVILY_DAYS", "14"))   # 默认最近 14 天

# per-prediction 预算（独立于聊天轮次 5 次上限）
PREDICTION_WEBSEARCH_BUDGET: int = 30     # 12 维 × ~2 query/维 + 重试余量

# ── 资讯搜索并发 ──────────────────────────────────────────────────────────────
# 串行 12 维 = 12 × (1 LLM query 生成 + 2 Tavily + 1 LLM 情绪打分) → 实测 6~16 分钟，
# 且 Tavily HTTP 往返期间 SSE 上长达 198 秒零字节（"卡住感"根因）。
# 并发跑 → 目标 1~3 分钟。别调太高：Tavily 与 LLM 网关都有速率限制，
# 打爆后触发 429/重试退避，反而比串行更慢。
PREDICTION_NEWS_CONCURRENCY: int = _int_env("PREDICTION_NEWS_CONCURRENCY", 5, 1, 12)

# 单维 thinking 缓冲上限（字符）。并发下 12 维的 thinking token 若逐个直推会交错成乱码，
# 故按维缓冲、维度结束时作为一个带标题的块整体推送（同时把 event_log 里的 thinking 行数
# 从数千条降到每维 1 条）。超长则截断，防止单条 SSE / event_log 行过大。
PREDICTION_NEWS_THINKING_MAX_CHARS: int = _int_env(
    "PREDICTION_NEWS_THINKING_MAX_CHARS", 6000, 500, 100000,
)

# ── 进度事件 phase 值（契约 A 的 progress.phase / 契约 F 的 SSE payload）──────
# 注意：这里是 progress **阶段名**，不是 SSE 事件类型 —— 事件类型一律取
# fsm.sse_events.SSEEventType.PREDICTION_PROGRESS.value（铁律 #3）。
PREDICTION_PHASE_NEWS: str = "news_searching"

# ── 维度（全部派生自 dimensions.DIMENSION_REGISTRY，26 维单一真相源）──────────────
# v1 在此硬编码 3 份清单，v2 收进注册表；下面的名字保留只为向后兼容既有调用方。
#
# ★ NEWS_DIMENSIONS 的语义**不变**：仍是「默认开启的 12 个 Tavily 搜索型维度」。
#   它是 search_all_dimensions(dimensions=None) 的兜底值 —— 不要把数据型维度混进来，
#   否则 Tavily 会去搜"全量历史公告"这种根本不该搜的东西（且 test_prediction_news 会红）。
#   要「默认全开的 17 维（含数据型）」请用 DEFAULT_DIMENSIONS。
NEWS_DIMENSIONS: list[str] = [
    d.key for d in DIMENSION_REGISTRY.values() if d.kind == "search" and d.default_on
]

# 默认关闭的 9 维（v1 叫 EXTENSIBLE，v2 已真正接入，用户可在多选框里勾）
NEWS_DIMENSIONS_EXTENSIBLE: list[str] = [
    d.key for d in DIMENSION_REGISTRY.values() if not d.default_on
]

# 默认勾选的全部维度（17 = 12 搜索 + 5 数据；sec_filings 仅美股，按市场过滤后 A 股为 16）
DEFAULT_DIMENSIONS: list[str] = [
    d.key for d in DIMENSION_REGISTRY.values() if d.default_on
]

# 维度 → 中文标签（前端展示 + LLM query 生成参考）
DIMENSION_LABELS: dict[str, str] = {
    key: dim.label for key, dim in DIMENSION_REGISTRY.items()
}

# ── 资讯情绪 → 蒙特卡洛漂移修正 ───────────────────────────────────────────────
NEWS_SENTIMENT_ALPHA: float = 0.15   # |sentiment|=1 时最多 ±15% 年化漂移修正
NEWS_SENTIMENT_EXTREME_THRESHOLD: float = 0.7  # |sentiment|>此值触发波动率扩大
NEWS_VOLATILITY_MULTIPLIER: float = 1.5

# ── 蒙特卡洛 ──────────────────────────────────────────────────────────────────
MONTE_CARLO_PATHS: int = 2000
MONTE_CARLO_PERCENTILES: tuple = (10, 50, 90)
MONTE_CARLO_DT: float = 1.0 / 252.0        # 一个交易日（GBM 的 dt，年化 252 个交易日）

# ── 公告抓取与 LLM 判定（设计文档 §2.3 三层判定）────────────────────────────────
# 元数据拉全量（标题+类型+日期，成本 1-2s，用于历史违规/减持次数的信用画像），
# 但 LLM 只判定近 N 个月的候选池 —— 全量 2000 条公告直接喂 LLM 必爆 token（用户拍板 #2）。
PREDICTION_ANNOUNCE_MONTHS: int = _int_env("PREDICTION_ANNOUNCE_MONTHS", 24, 1, 240)

# LLM 精判批大小。实测：batch30 → 14.0s / ~1145 prompt + ~1057 completion token；
# batch60 → 55.9s 且 reasoning_tokens 飙到 2776，有输出截断风险 → 别往上调。
PREDICTION_ANN_BATCH: int = _int_env("PREDICTION_ANN_BATCH", 30, 5, 50)

# 公告 LLM 判定的并发批数（Semaphore，复用 search_all_dimensions 的并发骨架）
PREDICTION_ANN_CONCURRENCY: int = _int_env("PREDICTION_ANN_CONCURRENCY", 4, 1, 8)

# announcement_cache 有效期（天）。同一条公告（symbol+ann_date+title_hash）只 LLM 打分一次，
# 缓存期内复跑同一只股票的公告维度 ≈ 0 次 LLM 调用。
PREDICTION_ANN_CACHE_DAYS: int = _int_env("PREDICTION_ANN_CACHE_DAYS", 90, 1, 3650)

# ── 事件分 → 公式（设计文档 §5.3 饱和函数族）──────────────────────────────────
# 通用饱和公式：S = -(1 - exp(-Σ xᵢ·wᵢ / κ))，S ∈ (-1, 0]
# 时间衰减：    wᵢ = exp(-Δtᵢ / τ)
# 单调、有界、可解释：不会因为一堆 -0.2 的小事叠成 -1，也不会被一条公告刷爆。
PREDICTION_EVENT_DECAY_TAU_DAYS: float = _float_env(
    "PREDICTION_EVENT_DECAY_TAU_DAYS", 45.0, 1.0, 365.0,
)   # τ=45 天 → 半衰期 ≈ 31 天
PREDICTION_KAPPA_NEGATIVE: float = _float_env("PREDICTION_KAPPA_NEGATIVE", 2.0, 0.1, 100.0)
PREDICTION_KAPPA_PLACEMENT: float = _float_env("PREDICTION_KAPPA_PLACEMENT", 2.0, 0.1, 100.0)
PREDICTION_KAPPA_REDUCTION: float = _float_env("PREDICTION_KAPPA_REDUCTION", 3.0, 0.1, 100.0)
# 解禁抛压：x = 占总市值比例(%) / 1.0，一次 3% 的解禁就相当于 x=3 → 饱和分约 -0.63
PREDICTION_KAPPA_LOCKUP: float = _float_env("PREDICTION_KAPPA_LOCKUP", 3.0, 0.1, 100.0)

# 定增强度归一化基准（§5.3-②）：xⱼ = 摊薄比例/0.10 + max(0, 折价率)/0.20
PREDICTION_PLACEMENT_DILUTION_BASE: float = 0.10
PREDICTION_PLACEMENT_DISCOUNT_BASE: float = 0.20
# 取不到总股本时的保守兜底摊薄比例（UI 必须标"估算"，不假装精确）
PREDICTION_PLACEMENT_DILUTION_FALLBACK: float = 0.05

# 减持强度归一化基准（§5.3-③）：x_k = 减持占总股本比例 / 1%
PREDICTION_REDUCTION_RATIO_BASE: float = 0.01

# 事件综合分 F = clamp(w_neg·S_neg + w_pp·S_pp + w_sr·S_sr, -1, 0)
# 支持 .env 覆盖：PREDICTION_EVENT_WEIGHTS="0.5,0.2,0.3"（顺序 negative,placement,reduction）
_DEFAULT_EVENT_WEIGHTS: dict[str, float] = {
    "negative": 0.5,
    "placement": 0.2,
    "reduction": 0.3,
    # 解禁抛压独立成一路。F 本来就钳在 [-1,0]（engine.py:540），权重和 >1 不会溢出；
    # 多一类风险就该多一份下拉，这是合意的。
    "lockup": 0.2,
}


def _parse_event_weights() -> dict[str, float]:
    """解析 PREDICTION_EVENT_WEIGHTS="0.5,0.2,0.3"；非法则回退默认（铁律 #9）。"""
    raw = os.environ.get("PREDICTION_EVENT_WEIGHTS")
    if not raw or not raw.strip():
        return dict(_DEFAULT_EVENT_WEIGHTS)
    parts = [p.strip() for p in raw.split(",")]
    keys = list(_DEFAULT_EVENT_WEIGHTS)
    if len(parts) != len(keys):
        logger.warning(
            "PREDICTION_EVENT_WEIGHTS=%r 需要 %d 个逗号分隔的权重（%s），回退默认值 %s",
            raw, len(keys), ",".join(keys), _DEFAULT_EVENT_WEIGHTS,
        )
        return dict(_DEFAULT_EVENT_WEIGHTS)
    try:
        vals = [float(p) for p in parts]
    except ValueError:
        logger.warning("PREDICTION_EVENT_WEIGHTS=%r 含非浮点数，回退默认值 %s", raw, _DEFAULT_EVENT_WEIGHTS)
        return dict(_DEFAULT_EVENT_WEIGHTS)
    if any(v < 0 for v in vals):
        logger.warning("PREDICTION_EVENT_WEIGHTS=%r 含负权重，回退默认值 %s", raw, _DEFAULT_EVENT_WEIGHTS)
        return dict(_DEFAULT_EVENT_WEIGHTS)
    return dict(zip(keys, vals))


PREDICTION_EVENT_WEIGHTS: dict[str, float] = _parse_event_weights()

# S_total = (1 − λ)·S_news + λ·F，λ = 0.4
# 公告是法定披露，可信度高于 Tavily 新闻，但只覆盖公司自身事件（不含宏观/地缘）→ 给 0.4 不给 0.6。
PREDICTION_FILING_WEIGHT: float = _float_env("PREDICTION_FILING_WEIGHT", 0.4, 0.0, 1.0)

# 事件驱动的波动率放大：M_event = 1 + k·|min(0, F)| → 最多 ×1.5
# 设计意图：爆雷不只是"往下走"，更是"不确定性放大" → 同时压低 μ 和抬高 σ，
# 图上表现为预测中枢下移 + 置信带明显变宽。
PREDICTION_EVENT_VOL_K: float = _float_env("PREDICTION_EVENT_VOL_K", 0.5, 0.0, 5.0)

# ── 预测 K 线影线标定（设计文档 §5.4）──────────────────────────────────────────
# k_up = mean((High − max(Open,Close)) / Close)，k_dn = mean((min(Open,Close) − Low) / Close)
# 取近 N 根历史 K 线做标定（可解释、非拍脑袋）。
# UI 必须标注：「预测蜡烛的影线由历史平均影线比例合成，不代表真实日内极值分布」。
PREDICTION_SHADOW_LOOKBACK_BARS: int = _int_env("PREDICTION_SHADOW_LOOKBACK_BARS", 120, 20, 500)
