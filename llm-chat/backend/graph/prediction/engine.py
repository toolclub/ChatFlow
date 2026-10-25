"""股票预测计算引擎（纯数学，NO LLM, NO DB）。

ARIMA(趋势) + GARCH(波动率) + 事件分(公告) + 蒙特卡洛(GBM 路径) → 预测 OHLC 蜡烛。

v2 相对 v1 的四处修复（设计文档 §5.1~§5.6）：
  1. **参数不再算完就扔**：`_arima_forecast` / `_garch_vol` 回传 (p,d,q)/AIC/网格 与 ω/α/β/持续性，
     全部落进 `ModelParams` → ForecastResult.model_params → DB → 前端公式卡片。
  2. **事件分双通道**（§5.3）：公告/定增/减持经饱和函数 + 45 天时间衰减 → F，
     与资讯情绪融合成 S_total，**同时**压低 μ 与抬高 σ（M_event）——
     爆雷不只是「往下走」，更是「不确定性放大」：中枢下移 + 置信带变宽。
  3. **真 K 线**（§5.4）：蒙特卡洛按日循环，用历史标定的影线系数 k_up/k_dn 合成 OHLC。
     v1 只有一条 p50 折线，那不是 K 线。
  4. **流式推导**（§5.6）：`forecast(on_step=…)` 边算边推 FORMULA_STEP / FORECAST_DAY。

铁律遵守：
  - #3 SSE 事件类型取 SSEEventType.*.value（本模块只产 payload，不做 dispatch/DB）
  - #5 阻塞计算一律进入量化专用有界线程池
  - #9 不吞异常：except 必须至少 logger.warning + 降级

依赖 statsmodels / arch（Docker 容器内可用）；两者缺失时自动降级 naive drift / 历史 std。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import math
from datetime import date as _date
from typing import Any, Awaitable, Callable

import numpy as np
import pandas as pd

from quant.execution import run_compute

from factors.prediction_indicators import calibrate_shadows, compute_all
from fsm.sse_events import SSEEventType
from graph.prediction.config import (
    MONTE_CARLO_DT,
    MONTE_CARLO_PATHS,
    MONTE_CARLO_PERCENTILES,
    NEWS_SENTIMENT_ALPHA,
    NEWS_SENTIMENT_EXTREME_THRESHOLD,
    NEWS_VOLATILITY_MULTIPLIER,
    PREDICTION_EVENT_DECAY_TAU_DAYS,
    PREDICTION_EVENT_VOL_K,
    PREDICTION_EVENT_WEIGHTS,
    PREDICTION_FILING_WEIGHT,
    PREDICTION_HORIZON_DAYS,
    PREDICTION_KAPPA_NEGATIVE,
    PREDICTION_KAPPA_PLACEMENT,
    PREDICTION_KAPPA_LOCKUP,
    PREDICTION_KAPPA_REDUCTION,
    PREDICTION_PLACEMENT_DILUTION_BASE,
    PREDICTION_PLACEMENT_DILUTION_FALLBACK,
    PREDICTION_PLACEMENT_DISCOUNT_BASE,
    PREDICTION_REDUCTION_RATIO_BASE,
    PREDICTION_SHADOW_LOOKBACK_BARS,
)
from graph.prediction.domain import ForecastCandle, ForecastResult, FormulaStep, ModelParams

logger = logging.getLogger("prediction.engine")

# on_step 回调签名：接收一个 SSE payload dict（含 "type" 字段），可以是同步或异步函数。
StepCallback = Callable[[dict], Awaitable[None] | None]

MODEL_NAME = "ARIMA+GARCH+Event+MC"

# 事件分三通道（§5.3）：FilingItem.event_type → 通道
# 退市风险 / 违规处罚都是「爆雷」性质，并入 negative 通道（S_neg），不新开权重。
_EVENT_CHANNEL: dict[str, str] = {
    "negative": "negative",
    "st_delisting_risk": "negative",
    "violation": "negative",
    "placement": "placement",
    "reduction": "reduction",
}

# 数据型维度 key → 通道（items 为空但维度自带 score 时的兜底映射）
_DIMENSION_CHANNEL: dict[str, str] = {
    "negative_event": "negative",
    "private_placement": "placement",
    "stake_reduction": "reduction",
    # 解禁/回购刻意**不进** _EVENT_CHANNEL（逐条通道）：它的净额要把「回购托底」从
    # 「解禁抛压」里减掉，而逐条通道的 _item_strength 只会累加正强度（x<=0 直接跳过），
    # 表达不了抵消。所以由 announcements.compute_dimension_score 算好净分，
    # 走这里的「维度自带 score」兜底路径（见下方 counts[channel]==0 分支）。
    "lockup_buyback": "lockup",
}

_KAPPA: dict[str, float] = {
    "negative": PREDICTION_KAPPA_NEGATIVE,
    "placement": PREDICTION_KAPPA_PLACEMENT,
    "reduction": PREDICTION_KAPPA_REDUCTION,
    "lockup": PREDICTION_KAPPA_LOCKUP,
}

# 影线系数上界（与 factors.prediction_indicators.SHADOW_K_MAX 一致），
# 这里再钳一次是为了保证 OHLC 不变式（0 ≤ k_dn < 1 → L > 0）即便调用方直接传参也成立。
_K_DN_MAX = 0.9


# ── on_step 推送 ──────────────────────────────────────────────────────────────

async def _emit(on_step: StepCallback | None, payload: dict) -> None:
    """推一条流式事件。回调炸了不能让整个预测挂掉（铁律 #9：至少 warning）。"""
    if on_step is None:
        return
    try:
        res = on_step(payload)
        if inspect.isawaitable(res):
            await res
    except Exception as exc:  # noqa: BLE001 — 回调是调用方的代码，失败只降级不中断计算
        logger.warning("on_step 回调失败 type=%s: %s", payload.get("type"), exc)


def _step_payload(step: FormulaStep) -> dict:
    """FormulaStep → SSE payload（铁律 #3：类型取枚举）。

    ★ 额外带一个 `stage` 字段（= step.key 的别名）：
      db.prediction_store.append_prediction_formula_step 以 "stage" 为去重键，
      payload 里缺这个字段会导致 SQL 的 `e->>'stage' IS DISTINCT FROM NULL` 为 false，
      **把之前所有公式步骤全删掉**。别删这行。
    """
    d = step.model_dump()
    d["stage"] = step.key
    d["type"] = SSEEventType.FORMULA_STEP.value
    return d


def _candle_payload(candle: ForecastCandle) -> dict:
    """ForecastCandle → SSE payload（去重键是 date，模型自带该字段）。"""
    d = candle.model_dump()
    d["type"] = SSEEventType.FORECAST_DAY.value
    return d


def _fmt(x: float, digits: int = 4) -> str:
    """数字格式化：极小/极大用科学计数，其余定点 —— 给 substituted 串用。"""
    if x is None or not np.isfinite(x):
        return "nan"
    ax = abs(x)
    if ax != 0 and (ax < 1e-4 or ax >= 1e6):
        return f"{x:.2e}"
    return f"{x:.{digits}f}"


# ── ARIMA ─────────────────────────────────────────────────────────────────────

def _arima_forecast(close: pd.Series, horizon: int) -> tuple[list[float], float, dict]:
    """ARIMA 拟合 + 预测。

    Returns (arima_path[horizon], arima_annualized_drift, meta)。
    meta = {order, aic, grid: [{order, aic}], fallback: bool}
      —— v1 只把 order/AIC log 一行就丢了（§5.1 的病根），v2 全部回传。
    失败降级：naive drift = last_close * (1 + mean_daily_return)^t。
    """
    s = pd.to_numeric(close, errors="coerce").dropna().astype(float)
    last_close = float(s.iloc[-1]) if not s.empty else float("nan")
    if len(s) < 30 or math.isnan(last_close) or last_close <= 0:
        logger.warning("ARIMA: 历史不足或 last_close 非法（len=%d），降级 naive drift", len(s))
        path, ann = _naive_drift(s, horizon, last_close)
        return path, ann, {"order": (0, 0, 0), "aic": 0.0, "grid": [], "fallback": True}

    # 简单 AIC 网格（p,d,q in {0,1,2} × d in {0,1}），挑最小 AIC；全失败则降级
    best_aic: float | None = None
    best_order: tuple[int, int, int] | None = None
    best_model = None
    grid: list[dict] = []
    for p in (0, 1, 2):
        for d in (0, 1):
            for q in (0, 1, 2):
                try:
                    m = _fit_arima(s, order=(p, d, q))
                    if m is None:
                        continue
                    aic = float(m.aic)
                    if np.isfinite(aic):
                        grid.append({"order": [p, d, q], "aic": round(aic, 2)})
                    if best_aic is None or (np.isfinite(aic) and aic < best_aic):
                        best_aic, best_order, best_model = aic, (p, d, q), m
                except Exception as exc:  # noqa: BLE001 — 网格内单格失败可接受
                    logger.debug("ARIMA(%d,%d,%d) 拟合失败: %s", p, d, q, exc)

    if best_model is None or best_order is None:
        logger.warning("ARIMA: 所有阶数拟合失败，降级 naive drift")
        path, ann = _naive_drift(s, horizon, last_close)
        return path, ann, {"order": (0, 0, 0), "aic": 0.0, "grid": grid, "fallback": True}

    try:
        fcast = best_model.forecast(steps=horizon)
        fcast = np.asarray(fcast, dtype=float).flatten().tolist()
        if len(fcast) < horizon:
            fcast = fcast + [fcast[-1]] * (horizon - len(fcast))
    except Exception as exc:  # noqa: BLE001
        logger.warning("ARIMA: forecast 失败 (%s)，降级 naive drift", exc)
        path, ann = _naive_drift(s, horizon, last_close)
        return path, ann, {"order": best_order, "aic": float(best_aic or 0.0), "grid": grid, "fallback": True}

    # 年化漂移：末预测点相对 last_close 的对数收益，按每日摊平后年化（252 交易日）
    try:
        fN = float(fcast[-1])
        if fN > 0 and last_close > 0:
            ann = math.log(fN / last_close) / max(horizon, 1) * 252.0
        else:
            ann = 0.0
    except Exception as exc:  # noqa: BLE001
        logger.warning("ARIMA: 年化漂移计算失败 (%s)，取 0", exc)
        ann = 0.0
    if not np.isfinite(ann):
        ann = 0.0

    # AIC 升序（前端展示网格搜索过程：最优在最前）
    grid.sort(key=lambda g: g["aic"])
    meta = {
        "order": best_order,
        "aic": float(best_aic or 0.0),
        "grid": grid[:9],           # §5.2：展示 9 组候选就够，别把 18 组全塞给前端
        "fallback": False,
    }
    logger.info("ARIMA 选用阶数 %s, AIC=%.2f, 年化漂移=%.4f", best_order, best_aic or 0.0, ann)
    return fcast, ann, meta


def _fit_arima(s: pd.Series, order: tuple[int, int, int]):
    """延迟 import statsmodels（系统 python 未装）。单阶数拟合，失败返回 None。"""
    try:
        from statsmodels.tsa.arima.model import ARIMA  # type: ignore
    except Exception as exc:  # noqa: BLE001
        logger.warning("statsmodels 不可用 (%s)，ARIMA 降级", exc)
        return None
    model = ARIMA(s, order=order)
    return model.fit()


def _naive_drift(s: pd.Series, horizon: int, last_close: float) -> tuple[list[float], float]:
    """降级：用历史日均收益的复利外推。"""
    if math.isnan(last_close) or last_close <= 0:
        return [0.0] * horizon, 0.0
    rets = s.pct_change().dropna()
    mean_daily = float(rets.mean()) if not rets.empty else 0.0
    if not np.isfinite(mean_daily):
        mean_daily = 0.0
    path = [last_close * ((1.0 + mean_daily) ** (t + 1)) for t in range(horizon)]
    ann = math.log(1.0 + mean_daily) * 252.0 if (1.0 + mean_daily) > 0 else 0.0
    logger.warning("naive drift 生效: mean_daily=%.5f, 年化漂移=%.4f", mean_daily, ann)
    return path, ann


# ── GARCH ─────────────────────────────────────────────────────────────────────

def _garch_vol(close: pd.Series, horizon: int) -> tuple[float, list[float], dict]:
    """GARCH(1,1) 预测条件方差路径。

    Returns (sigma_annual, variance_path[horizon], meta)。
    meta = {omega, alpha, beta, persistence, fallback}
      —— v1 的 `res.params` 里 ω/α/β 直接丢弃（§5.1），v2 回传。
      ω 已从「百分数收益率方差」换算回「小数收益率方差」（÷100²），
      α/β 无量纲不受影响；persistence = α+β（<1 才平稳）。
    失败降级：历史日收益 std 年化，α=β=0。
    """
    s = pd.to_numeric(close, errors="coerce").dropna().astype(float)
    rets = s.pct_change().dropna()
    if len(rets) < 30:
        logger.warning("GARCH: 历史不足（len=%d），降级历史 std", len(rets))
        return _garch_fallback(rets, horizon)

    try:
        from arch import arch_model  # type: ignore
    except Exception as exc:  # noqa: BLE001
        logger.warning("arch 不可用 (%s)，GARCH 降级历史 std", exc)
        return _garch_fallback(rets, horizon)

    try:
        # 收益率以百分数传入更稳（arch 惯例），结果再缩回
        rets_pct = rets * 100.0
        am = arch_model(rets_pct, vol="Garch", p=1, q=1, mean="Zero", rescale=False)
        res = am.fit(disp="off")
        f = res.forecast(horizon=horizon, reindex=False)
        var_pct = np.asarray(f.variance.values[-1], dtype=float).flatten()  # 百分数收益率的方差
        var_pct = np.where(np.isfinite(var_pct) & (var_pct > 0), var_pct, np.nan)
        var_daily = var_pct / (100.0 ** 2)  # 转回小数收益率的方差
        last_var = float(np.nanmean(var_daily[-1])) if np.isfinite(var_daily[-1]) else float(np.nanmean(var_daily))
        if not np.isfinite(last_var) or last_var <= 0:
            logger.warning("GARCH: 方差预测非正 (%s)，降级历史 std", last_var)
            return _garch_fallback(rets, horizon)
        sigma_annual = math.sqrt(last_var * 252.0)
        var_path = list(var_daily)
        if len(var_path) < horizon:
            var_path = var_path + [var_path[-1]] * (horizon - len(var_path))

        # ── §5.2：留住 ω/α/β ──
        params = res.params
        omega_pct = _param_get(params, "omega")
        alpha = _param_get(params, "alpha[1]", "alpha")
        beta = _param_get(params, "beta[1]", "beta")
        omega = omega_pct / (100.0 ** 2)   # 换算回小数收益率方差单位
        persistence = alpha + beta
        if persistence >= 1.0:
            logger.warning(
                "GARCH: α+β=%.4f ≥ 1（非平稳，波动无限累积）—— 参数照实回传，UI 需提示",
                persistence,
            )
        meta = {
            "omega": float(omega),
            "alpha": float(alpha),
            "beta": float(beta),
            "persistence": float(persistence),
            "fallback": False,
        }
        logger.info(
            "GARCH(1,1) 拟合成功: ω=%.3e α=%.4f β=%.4f α+β=%.4f sigma_annual=%.4f",
            omega, alpha, beta, persistence, sigma_annual,
        )
        return sigma_annual, var_path[:horizon], meta
    except Exception as exc:  # noqa: BLE001
        logger.warning("GARCH: 拟合/预测失败 (%s)，降级历史 std", exc)
        return _garch_fallback(rets, horizon)


def _param_get(params: Any, *names: str) -> float:
    """从 arch 的 params(Series) 里按候选名取值，取不到返回 0.0（铁律 #9：不静默）。"""
    for n in names:
        try:
            val = float(params[n])
            if np.isfinite(val):
                return val
        except Exception:  # noqa: BLE001 — 换下一个候选名
            continue
    logger.warning("GARCH: 参数 %s 缺失，取 0", names)
    return 0.0


def _hist_std_vol(rets: pd.Series, horizon: int) -> tuple[float, list[float]]:
    """降级：历史日收益 std 年化，方差路径用恒定方差填充。

    契约与 v1 一致（(sigma_annual, var_path) 二元组）—— GARCH 的 ω/α/β meta 由
    `_garch_fallback` 包一层补上，这样这个纯数学降级函数保持可单测、无冗余返回值。
    """
    if rets is None or rets.empty:
        sigma_annual = 0.20  # 合理兜底 20% 年化
    else:
        std = float(rets.std(ddof=1))
        if not np.isfinite(std) or std <= 0:
            std = 0.20 / math.sqrt(252.0)
        sigma_annual = std * math.sqrt(252.0)
    var_daily = (sigma_annual ** 2) / 252.0
    var_path = [var_daily] * horizon
    logger.warning("历史 std 降级: sigma_annual=%.4f", sigma_annual)
    return sigma_annual, var_path


def _garch_fallback(rets: pd.Series, horizon: int) -> tuple[float, list[float], dict]:
    """GARCH 不可用时的降级：历史 std + 一份「参数缺席」的 meta（α=β=0，ω=日方差）。

    meta.fallback=True → 公式卡片会明说「GARCH 不可用，已降级历史 std」，
    而不是把 α=β=0 当成真实拟合结果糊弄用户。
    """
    sigma_annual, var_path = _hist_std_vol(rets, horizon)
    var_daily = (sigma_annual ** 2) / 252.0
    meta = {
        "omega": float(var_daily),
        "alpha": 0.0,
        "beta": 0.0,
        "persistence": 0.0,
        "fallback": True,
    }
    return sigma_annual, var_path, meta


# ── 事件分（§5.3 饱和函数族）──────────────────────────────────────────────────

def _saturate(weighted_sum: float, kappa: float) -> float:
    """通用饱和公式：S = -(1 - exp(-Σ xᵢ·wᵢ / κ))，S ∈ (-1, 0]。

    单调、有界、可解释：不会被一条公告刷爆，也不会因为一堆 -0.2 的小事叠成 -1。
    """
    if kappa <= 0:
        logger.warning("_saturate: κ=%s 非法，取 1.0", kappa)
        kappa = 1.0
    x = max(0.0, float(weighted_sum))
    if not np.isfinite(x):
        logger.warning("_saturate: Σxw 非有限值，取 0")
        return 0.0
    return -(1.0 - math.exp(-x / kappa))


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """兼容 dict / pydantic 对象两种 filing_digest 形态。"""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _decay_weight(item: Any, as_of: _date | None, tau: float) -> float:
    """时间衰减 wᵢ = exp(-Δtᵢ / τ)，τ=45 天（半衰期 ≈ 31 天）。

    优先用 FilingItem.decay_weight（announcements.py 可能已算好）；
    否则用 item.date 与 as_of 之差现算；日期缺失/不可解析 → w=1.0 并 warning（铁律 #9）。
    """
    pre = _field(item, "decay_weight")
    if pre is not None:
        try:
            w = float(pre)
            if np.isfinite(w) and w >= 0:
                return min(w, 1.0)
        except (TypeError, ValueError):
            pass
        logger.warning("filing item decay_weight 非法 (%r)，改为按日期现算", pre)

    raw = _field(item, "date") or ""
    if not raw or as_of is None:
        logger.warning("filing item 缺少日期（title=%r），时间衰减取 w=1.0",
                       str(_field(item, "title", ""))[:40])
        return 1.0
    try:
        d = pd.Timestamp(str(raw)).date()
    except Exception as exc:  # noqa: BLE001
        logger.warning("filing item 日期不可解析 (%r): %s，时间衰减取 w=1.0", raw, exc)
        return 1.0
    delta = max(0, (as_of - d).days)
    tau_safe = tau if tau > 0 else 45.0
    return float(math.exp(-delta / tau_safe))


def _item_strength(channel: str, item: Any) -> tuple[float, str]:
    """单条事件的强度 xᵢ（§5.3 的 ①②③）+ 一行可读来源说明。"""
    if channel == "negative":
        # ① xᵢ = |negative_score|（LLM 给的 -1~0）
        try:
            x = abs(float(_field(item, "negative_score", 0.0) or 0.0))
        except (TypeError, ValueError):
            logger.warning("negative_score 非数值 (%r)，取 0", _field(item, "negative_score"))
            x = 0.0
        return x, f"|score|={_fmt(x, 2)}"

    if channel == "placement":
        # ② xⱼ = 摊薄比例/0.10 + max(0, 折价率)/0.20
        dil = _field(item, "dilution_ratio")
        est = dil is None
        try:
            dil_v = float(dil) if dil is not None else PREDICTION_PLACEMENT_DILUTION_FALLBACK
        except (TypeError, ValueError):
            logger.warning("dilution_ratio 非数值 (%r)，用保守兜底 %.2f", dil,
                           PREDICTION_PLACEMENT_DILUTION_FALLBACK)
            dil_v, est = PREDICTION_PLACEMENT_DILUTION_FALLBACK, True
        disc = _field(item, "discount_rate")
        try:
            disc_v = max(0.0, float(disc)) if disc is not None else 0.0
        except (TypeError, ValueError):
            logger.warning("discount_rate 非数值 (%r)，取 0", disc)
            disc_v = 0.0
        x = dil_v / PREDICTION_PLACEMENT_DILUTION_BASE + disc_v / PREDICTION_PLACEMENT_DISCOUNT_BASE
        note = f"摊薄{dil_v:.1%}{'(估算)' if est else ''}+折价{disc_v:.1%}"
        return max(0.0, x), note

    if channel == "reduction":
        # ③ x_k = 减持占总股本比例 / 1%（拿不到比例 → 按公告条数计 x=1）
        ratio = _field(item, "reduction_ratio")
        if ratio is None:
            return 1.0, "无比例(按条数计 x=1)"
        try:
            r = float(ratio)
        except (TypeError, ValueError):
            logger.warning("reduction_ratio 非数值 (%r)，按条数计 x=1", ratio)
            return 1.0, "无比例(按条数计 x=1)"
        base = PREDICTION_REDUCTION_RATIO_BASE or 0.01
        return max(0.0, r / base), f"减持{r:.2%}"

    return 0.0, ""


def compute_event_scores(
    filing_digest: list[Any] | None,
    as_of: _date | None = None,
    tau: float = PREDICTION_EVENT_DECAY_TAU_DAYS,
) -> dict:
    """事件分（§5.3）：FilingItem 列表 → S_neg / S_pp / S_sr → F。

    输入 filing_digest：FilingDimensionResult 的 dict 或对象列表（两种都吃）。
      - failed 的维度整维跳过（数据源挂了，不能当成「没有坏消息」）。
      - 跨维度按 (date, title) 去重：announcements_full 与 negative_event 会重叠，
        不去重会把同一条爆雷算两次。
      - 某通道无 items 但该维度自带 score（announcements.py 已算好）→ 用它兜底。

    Returns {
      "scores":   {negative, placement, reduction},   # 各通道 S ∈ (-1, 0]
      "sums":     {negative, placement, reduction},   # 各通道 Σ xᵢ·wᵢ（给 substituted 用）
      "counts":   {negative, placement, reduction},
      "terms":    {channel: [ "0.90×0.85", ... ]},    # 逐项代入串
      "F":        float,                              # clamp(Σ wᵢ·Sᵢ, -1, 0)
    }
    """
    # ★ 通道清单从 _KAPPA 派生，**不要写死**。
    #   之前这里是硬编码的 ("negative","placement","reduction")，新增 lockup 通道后
    #   sums/counts/terms/scores 里没有它的 key → scores[channel] 直接 KeyError('lockup')，
    #   整单预测报「预测计算失败：'lockup'」。_KAPPA 是通道的单一真相源，跟着它走就不会再漏。
    channels = tuple(_KAPPA)
    sums = {c: 0.0 for c in channels}
    counts = {c: 0 for c in channels}
    terms: dict[str, list[str]] = {c: [] for c in channels}
    scores = {c: 0.0 for c in channels}
    fallback_channels: set[str] = set()   # 走「维度自带 score」的通道 → 展示层别套饱和公式模板

    dims = list(filing_digest or [])
    seen: set[tuple[str, str]] = set()

    for dim in dims:
        if _field(dim, "failed", False):
            logger.warning(
                "事件分：维度 %s 取数失败，整维跳过（不当作「无坏消息」）",
                _field(dim, "dimension", "?"),
            )
            continue
        for item in _field(dim, "items", []) or []:
            etype = str(_field(item, "event_type", "") or "")
            channel = _EVENT_CHANNEL.get(etype)
            if channel is None:
                continue  # neutral / 未知类型不进公式
            key = (str(_field(item, "date", "") or ""), str(_field(item, "title", "") or ""))
            if key in seen:
                continue
            seen.add(key)

            x, note = _item_strength(channel, item)
            if x <= 0:
                continue
            w = _decay_weight(item, as_of, tau)
            sums[channel] += x * w
            counts[channel] += 1
            if len(terms[channel]) < 6:  # substituted 串最多列 6 项，其余用 "+…"
                terms[channel].append(f"{_fmt(x, 2)}×{_fmt(w, 2)}[{note}]")

    for c in channels:
        scores[c] = _saturate(sums[c], _KAPPA[c])

    # 兜底：某通道一条 item 都没有，但对应维度自带 score（announcements.py 已按同一公式算过）。
    #
    # lockup 通道**永远**走这条路：解禁/回购要做净额抵消（回购托底），而上面的逐条循环只会
    # 累加正强度（x<=0 直接 continue），表达不了"减掉"。所以净分在 announcements.py 里算好。
    #
    # ★ 走兜底时必须把 counts / terms 一起补上，否则公式卡片会撒谎：
    #     - counts 不补 → result_text 显示「解禁 0 条」，可明明有 1 笔
    #     - terms 不补 → substituted 打出 "-(1 - exp(-0.000/3)) = -0.100"，算式和结果对不上
    #   这个功能的全部意义就是把公式如实摊开，展示层失真比不展示更糟。
    for dim in dims:
        if _field(dim, "failed", False):
            continue
        channel = _DIMENSION_CHANNEL.get(str(_field(dim, "dimension", "") or ""))
        if channel is None or counts[channel] > 0:
            continue
        try:
            s = float(_field(dim, "score", 0.0) or 0.0)
        except (TypeError, ValueError):
            logger.warning("维度 %s 的 score 非数值，忽略", _field(dim, "dimension"))
            continue
        if s < 0:
            scores[channel] = max(-1.0, s)
            items = _field(dim, "items", []) or []
            counts[channel] = len(items)
            summary = str(_field(dim, "summary", "") or "").split("；")[0]
            terms[channel] = [summary] if summary else [f"维度自带 S={_fmt(s, 3)}"]
            fallback_channels.add(channel)

    f_raw = sum(PREDICTION_EVENT_WEIGHTS.get(c, 0.0) * scores[c] for c in channels)
    f = float(max(-1.0, min(0.0, f_raw)))

    return {"scores": scores, "sums": sums, "counts": counts, "terms": terms,
            "fallback": fallback_channels, "F": f}


# ── 蒙特卡洛（§5.4 逐日 OHLC）─────────────────────────────────────────────────

def _gbm_prices(
    last_close: float,
    drift_mu_annual: float,
    sigma_annual: float,
    horizon: int,
    n_paths: int,
) -> np.ndarray:
    """GBM 价格路径矩阵 (n_paths, horizon)。

    S_{t+1} = S_t · exp((μ − ½σ²)·dt + σ·√dt·Z),  Z~N(0,1),  dt = 1/252
    """
    dt = MONTE_CARLO_DT
    mu = float(drift_mu_annual)
    sigma = max(float(sigma_annual), 1e-6)  # 防 0
    z = np.random.standard_normal((n_paths, horizon))
    log_ret = (mu - 0.5 * sigma * sigma) * dt + sigma * math.sqrt(dt) * z
    return last_close * np.exp(np.cumsum(log_ret, axis=1))


def _monte_carlo(
    last_close: float,
    drift_mu_annual: float,
    sigma_annual: float,
    horizon: int,
    n_paths: int,
    percentiles: tuple,
) -> dict[str, np.ndarray]:
    """GBM 蒙特卡洛 → 每日跨路径分位数 {"p10": arr, "p50": arr, "p90": arr}。

    v1 的收盘价分位数通道（置信带），v2 仍在用：`_monte_carlo_paths` 负责 OHLC 蜡烛，
    分位数仍由本函数定义 —— 两者共用同一条 `_gbm_prices` 路径生成逻辑。
    """
    prices = _gbm_prices(last_close, drift_mu_annual, sigma_annual, horizon, n_paths)
    return {f"p{p}": np.percentile(prices, p, axis=0) for p in percentiles}


def _monte_carlo_paths(
    last_close: float,
    drift_mu_annual: float,
    sigma_annual: float,
    horizon: int,
    n_paths: int,
    k_up: float,
    k_dn: float,
) -> dict[str, np.ndarray]:
    """GBM 蒙特卡洛 → (n_paths, horizon) 的 O/H/L/C 四个矩阵（§5.4）。

    Oᵢ,ₜ = Sᵢ,ₜ₋₁ (t=1 → last_close)；Cᵢ,ₜ = Sᵢ,ₜ
    Hᵢ,ₜ = max(O,C)·(1+k_up)；Lᵢ,ₜ = min(O,C)·(1−k_dn)

    不变式（跨路径取中位数后仍成立）：
      median(H) = (1+k_up)·median(max(O,C)) ≥ max(median O, median C)   [k_up ≥ 0]
      median(L) = (1−k_dn)·median(min(O,C)) ≤ min(median O, median C)   [0 ≤ k_dn < 1]
      —— 因为 max(O,C) ≥ O 逐点成立 → 中位数保序；正数缩放与中位数可交换。
    """
    prices = _gbm_prices(last_close, drift_mu_annual, sigma_annual, horizon, n_paths)  # = C

    opens = np.empty_like(prices)
    opens[:, 0] = last_close
    if horizon > 1:
        opens[:, 1:] = prices[:, :-1]

    body_hi = np.maximum(opens, prices)
    body_lo = np.minimum(opens, prices)
    ku = max(0.0, float(k_up))
    kd = min(max(0.0, float(k_dn)), _K_DN_MAX)
    highs = body_hi * (1.0 + ku)
    lows = body_lo * (1.0 - kd)

    return {"open": opens, "high": highs, "low": lows, "close": prices}


def _inflection_points(dates: list[str], p50: list[float]) -> list[dict]:
    """扫描 p50 预测序列的方向反转点。"""
    pts: list[dict] = []
    if len(p50) < 2:
        return pts
    arr = np.asarray(p50, dtype=float)
    diffs = np.diff(arr)
    signs = np.sign(diffs)
    for i in range(len(diffs) - 1):
        s1, s2 = signs[i], signs[i + 1]
        if s1 == 0 or s2 == 0:
            continue
        if s1 * s2 < 0:
            idx = i + 1
            pts.append({
                "date": dates[idx] if idx < len(dates) else "",
                "price": float(arr[idx]),
                "type": "peak" if s1 > 0 else "trough",
            })
    return pts


def _future_dates(last_date: pd.Timestamp, horizon: int) -> list[str]:
    """未来 horizon 个交易日（跳过周末；不含节假日日历 —— UI 已标注为「交易日」近似）。"""
    dates = pd.bdate_range(start=last_date + pd.Timedelta(days=1), periods=horizon)
    return [d.strftime("%Y-%m-%d") for d in dates]


# ── 主入口 ────────────────────────────────────────────────────────────────────

async def forecast(
    bars_df: "pd.DataFrame",
    news_sentiment: float,
    horizon: int = PREDICTION_HORIZON_DAYS,
    *,
    filing_digest: list[Any] | None = None,
    on_step: StepCallback | None = None,
) -> dict:
    """计算引擎主入口（v2）。

    Args:
      bars_df:        列 date(str ISO), open, high, low, close, volume（providers 已归一化）
      news_sentiment: S_news ∈ [-1, 1]（资讯维度聚合情绪）
      horizon:        预测交易日数，默认 7（用户拍板，§5.5）
      filing_digest:  数据型维度结果（FilingDimensionResult 的 dict 或对象列表）。
                      None/[] → 事件分 F=0，退化成 v1 的「纯资讯情绪」行为。
      on_step:        流式推导回调 `(payload: dict) -> Awaitable|None`。同步/异步都吃。
                      payload["type"] ∈ {formula_step, forecast_day}（SSEEventType 枚举值）。
                      回调抛异常只 warning，不中断计算（铁律 #9）。

    Returns: ForecastResult.model_dump()（新增 model_params / forecast_candles / formula_steps）。
             事件分 F 见 result["model_params"]["filing_score"]。
    """
    horizon = max(1, int(horizon or PREDICTION_HORIZON_DAYS))

    # ── 输入校验 / 规整 ──
    if bars_df is None or len(bars_df) == 0:
        logger.warning("forecast: bars_df 为空")
        return ForecastResult(model_name=MODEL_NAME).model_dump()

    work = bars_df.copy()
    if "date" in work.columns:
        work = work.sort_values("date").reset_index(drop=True)
    close = pd.to_numeric(work["close"], errors="coerce").dropna().astype(float)
    if close.empty:
        logger.warning("forecast: close 序列为空")
        return ForecastResult(model_name=MODEL_NAME).model_dump()
    last_close = float(close.iloc[-1])
    if not np.isfinite(last_close) or last_close <= 0:
        logger.warning("forecast: last_close 非法 (%s)", last_close)
        return ForecastResult(model_name=MODEL_NAME).model_dump()

    ns = float(news_sentiment) if news_sentiment is not None else 0.0
    if not np.isfinite(ns):
        logger.warning("forecast: news_sentiment 非有限值 (%r)，取 0", news_sentiment)
        ns = 0.0
    ns = max(-1.0, min(1.0, ns))

    steps: list[FormulaStep] = []

    async def _push(step: FormulaStep) -> None:
        steps.append(step)
        await _emit(on_step, _step_payload(step))

    # ── Step 1: 技术指标 + 影线标定（纯 pandas/numpy → to_thread）──────────────
    technical, shadow = await asyncio.gather(
        run_compute(compute_all, work),
        run_compute(calibrate_shadows, work, PREDICTION_SHADOW_LOOKBACK_BARS),
    )
    macd_d = technical.get("macd") or {}
    boll_d = technical.get("boll") or {}
    kdj_d = technical.get("kdj") or {}
    await _push(FormulaStep(
        key="technical",
        title="技术指标快照",
        formula="MACD: DIF=EMA₁₂−EMA₂₆, DEA=EMA₉(DIF), hist=2(DIF−DEA) | RSI=100−100/(1+RS) | BOLL=MA₂₀±2σ | KDJ: J=3K−2D",
        formula_latex=(
            r"\mathrm{DIF}=\mathrm{EMA}_{12}-\mathrm{EMA}_{26},\ "
            r"\mathrm{RSI}=100-\frac{100}{1+RS},\ "
            r"\mathrm{BOLL}=\mathrm{MA}_{20}\pm 2\sigma,\ J=3K-2D"
        ),
        params={
            "ma5": technical.get("ma5"), "ma10": technical.get("ma10"), "ma20": technical.get("ma20"),
            "macd_dif": macd_d.get("dif"), "macd_dea": macd_d.get("dea"), "macd_hist": macd_d.get("hist"),
            "rsi14": technical.get("rsi14"),
            "boll_upper": boll_d.get("upper"), "boll_mid": boll_d.get("mid"), "boll_lower": boll_d.get("lower"),
            "kdj_k": kdj_d.get("k"), "kdj_d": kdj_d.get("d"), "kdj_j": kdj_d.get("j"),
            "last_close": last_close,
        },
        substituted=(
            f"MA5={_fmt(technical.get('ma5', float('nan')), 2)} / "
            f"MA20={_fmt(technical.get('ma20', float('nan')), 2)} / "
            f"RSI14={_fmt(technical.get('rsi14', float('nan')), 1)} / "
            f"MACD_hist={_fmt(macd_d.get('hist', float('nan')), 3)} / "
            f"KDJ_J={_fmt(kdj_d.get('j', float('nan')), 1)}"
        ),
        result=last_close,
        result_text=f"最新收盘 {_fmt(last_close, 2)}",
        explain="技术指标只用于给分析节点看盘面（超买/超卖、多空排列），不直接进 GBM 公式。",
    ))

    # ── Step 2 & 3: ARIMA + GARCH（重计算 → to_thread 并行）────────────────────
    (arima_path, arima_drift, arima_meta), (garch_sigma, _var_path, garch_meta) = await asyncio.gather(
        run_compute(_arima_forecast, close, horizon),
        run_compute(_garch_vol, close, horizon),
    )

    p_, d_, q_ = arima_meta["order"]
    grid = arima_meta["grid"]
    grid_txt = ", ".join(f"({g['order'][0]},{g['order'][1]},{g['order'][2]}):AIC={g['aic']:.1f}" for g in grid[:9])
    await _push(FormulaStep(
        key="arima",
        title=f"ARIMA({p_},{d_},{q_}) 趋势漂移",
        formula="(1−Σφᵢ·Lⁱ)(1−L)ᵈ·yₜ = (1+Σθⱼ·Lʲ)·εₜ    →    μ_ARIMA = ln(ŷ_H / S₀) / H × 252",
        formula_latex=(
            r"\left(1-\sum_{i=1}^{p}\phi_i L^i\right)(1-L)^d y_t"
            r"=\left(1+\sum_{j=1}^{q}\theta_j L^j\right)\varepsilon_t"
        ),
        params={
            "p": p_, "d": d_, "q": q_,
            "aic": round(float(arima_meta["aic"]), 2),
            "grid_tried": grid,
            "last_close": last_close,
            "arima_last_point": float(arima_path[-1]) if arima_path else None,
            "horizon": horizon,
            "fallback": arima_meta["fallback"],
        },
        substituted=(
            f"μ_ARIMA = ln({_fmt(float(arima_path[-1]) if arima_path else last_close, 2)} / "
            f"{_fmt(last_close, 2)}) / {horizon} × 252 = {_fmt(arima_drift)}"
        ),
        result=float(arima_drift),
        result_text=(
            f"网格搜索 {len(grid)} 组，AIC 最小 = ({p_},{d_},{q_}) AIC={arima_meta['aic']:.2f}"
            + (f" | 候选: {grid_txt}" if grid_txt else "")
            + (" | ⚠ ARIMA 不可用，已降级 naive drift" if arima_meta["fallback"] else "")
        ),
        explain="ARIMA 给出「趋势中枢」：年化漂移 μ_ARIMA 就是 GBM 里价格的平均增长速度。AIC 越小模型越好（惩罚过拟合）。",
    ))

    g_omega = float(garch_meta["omega"])
    g_alpha = float(garch_meta["alpha"])
    g_beta = float(garch_meta["beta"])
    g_persist = float(garch_meta["persistence"])
    await _push(FormulaStep(
        key="garch",
        title="GARCH(1,1) 条件波动率",
        formula="σ²ₜ = ω + α·ε²ₜ₋₁ + β·σ²ₜ₋₁    →    σ_GARCH = √(σ²_H × 252)",
        formula_latex=r"\sigma_t^2=\omega+\alpha\varepsilon_{t-1}^2+\beta\sigma_{t-1}^2",
        params={
            "omega": g_omega, "alpha": g_alpha, "beta": g_beta,
            "persistence": g_persist,
            "sigma_annual": float(garch_sigma),
            "fallback": garch_meta["fallback"],
        },
        substituted=(
            f"σ²ₜ = {_fmt(g_omega)} + {_fmt(g_alpha)}·ε²ₜ₋₁ + {_fmt(g_beta)}·σ²ₜ₋₁"
            f"  →  σ_GARCH = {_fmt(garch_sigma)}（年化 {garch_sigma * 100:.2f}%）"
        ),
        result=float(garch_sigma),
        result_text=(
            f"α+β = {g_persist:.4f} "
            + ("< 1，模型平稳" if g_persist < 1.0 else "≥ 1，⚠ 非平稳（波动无限累积，结果需谨慎看）")
            + ("｜⚠ GARCH 不可用，已降级历史 std（α=β=0）" if garch_meta["fallback"] else "")
        ),
        explain="α+β 是波动的「记忆性」：越接近 1，今天大跌后明天大概率继续剧烈波动。σ_GARCH 决定 GBM 置信带的宽度。",
    ))

    # ── Step 4: 事件分（§5.3）────────────────────────────────────────────────
    as_of: _date | None = None
    if "date" in work.columns and len(work):
        try:
            as_of = pd.Timestamp(work["date"].iloc[-1]).date()
        except Exception as exc:  # noqa: BLE001
            logger.warning("forecast: 末根 K 线日期不可解析 (%s)，时间衰减以今天为基准", exc)
    if as_of is None:
        as_of = pd.Timestamp.now().date()

    ev = await run_compute(
        compute_event_scores, filing_digest, as_of, PREDICTION_EVENT_DECAY_TAU_DAYS,
    )
    f_score = ev["F"]

    # ★ 展示用的公式必须**跟着实际参与计算的通道走**，不能写死三项。
    #   f_raw 是 `sum(w_c·S_c for c in channels)`（见 compute_event_scores），channels 由
    #   _KAPPA 派生。如果这里还硬编码 0.5·S_neg + 0.2·S_pp + 0.3·S_sr，一旦勾了
    #   lockup_buyback，「公式里写的」就和「实际算的」对不上 —— 而这个功能的全部意义
    #   就是把公式如实摊开给用户看。宁可公式长一点，也不能演一个假的。
    _CHAN_CN = {
        "negative": ("S_neg", "爆雷"),
        "placement": ("S_pp", "定增"),
        "reduction": ("S_sr", "减持"),
        "lockup": ("S_lk", "解禁"),
    }
    active = [c for c in ev["scores"] if ev["counts"].get(c) or ev["scores"].get(c)]
    if not active:
        active = ["negative", "placement", "reduction"]
    weights = {c: PREDICTION_EVENT_WEIGHTS.get(c, 0.0) for c in active}

    def _sym(c: str) -> str:
        return _CHAN_CN.get(c, (f"S_{c}", c))[0]

    def _cn(c: str) -> str:
        return _CHAN_CN.get(c, (f"S_{c}", c))[1]

    def _chan_sub(name: str) -> str:
        kappa = _KAPPA[name]
        t = ev["terms"][name]
        body = " + ".join(t) if t else "0"
        # 走兜底的通道（lockup：净额已在 announcements 里算好）不能套饱和公式模板 ——
        # 它的 sums 是空的，硬套会打出 "-(1 - exp(-0.000/κ)) = -0.100" 这种自相矛盾的算式。
        if name in ev.get("fallback", ()):
            return f"{_sym(name)} = {_fmt(ev['scores'][name], 3)}（{body}）"
        more = " + …" if ev["counts"][name] > len(t) else ""
        return (
            f"{_sym(name)} = -(1 - exp(-({body}{more}) / {kappa:g})) "
            f"= -(1 - exp(-{_fmt(ev['sums'][name], 3)}/{kappa:g})) = {_fmt(ev['scores'][name], 3)}"
        )

    f_expr = " + ".join(f"{weights[c]:g}·{_sym(c)}" for c in active)
    f_subst = " + ".join(
        f"{weights[c]:g}×{_fmt(ev['scores'][c], 3)}" for c in active
    )

    await _push(FormulaStep(
        key="event_score",
        title="公告事件分 F（饱和函数 + 45 天时间衰减）",
        formula=(
            "S = -(1 - exp(-Σ xᵢ·wᵢ / κ)),  wᵢ = exp(-Δtᵢ / τ), τ=45d\n"
            f"F = clamp({f_expr}, -1, 0)"
        ),
        formula_latex=(
            r"S=-\left(1-e^{-\sum_i x_i w_i/\kappa}\right),\ w_i=e^{-\Delta t_i/\tau},\ "
            r"F=\mathrm{clamp}\left(\sum_c w_c S_c,-1,0\right)"
        ),
        params={
            "tau_days": PREDICTION_EVENT_DECAY_TAU_DAYS,
            **{f"kappa_{c}": _KAPPA[c] for c in active},
            **{_sym(c): ev["scores"][c] for c in active},
            "counts": ev["counts"],
            "weights": weights,
            "F": f_score,
        },
        substituted=(
            "\n".join(_chan_sub(c) for c in active)
            + f"\nF = {f_subst} = {_fmt(f_score, 3)}"
        ),
        result=float(f_score),
        result_text=(
            " / ".join(f"{_cn(c)} {ev['counts'][c]} 条" for c in active)
            + f" → F = {_fmt(f_score, 3)}"
        ),
        explain="饱和函数保证「一堆小事叠不成大雷、一条大雷也刷不爆」；时间衰减让 3 个月前的旧公告自动淡出（半衰期≈31 天）。解禁按「还有几天」加权（越临近抛压越重），回购只抵消抛压、不独立推高预测。",
    ))

    # ── Step 5: 情绪融合 S_total ──────────────────────────────────────────────
    lam = PREDICTION_FILING_WEIGHT
    s_total = (1.0 - lam) * ns + lam * f_score
    await _push(FormulaStep(
        key="sentiment_total",
        title="情绪融合 S_total",
        formula="S_total = (1−λ)·S_news + λ·F,  λ=0.4",
        formula_latex=r"S_{total}=(1-\lambda)S_{news}+\lambda F",
        params={"S_news": ns, "F": f_score, "lambda": lam, "S_total": s_total},
        substituted=(
            f"S_total = (1−{lam:g})×{_fmt(ns, 3)} + {lam:g}×{_fmt(f_score, 3)} = {_fmt(s_total, 3)}"
        ),
        result=float(s_total),
        explain="公告是法定披露，可信度高于搜索来的新闻，但只覆盖公司自身事件（不含宏观/地缘）→ 给 λ=0.4 而不是 0.6。",
    ))

    # ── Step 6: 代入 GBM（漂移 + 波动率双通道）────────────────────────────────
    mu = arima_drift + NEWS_SENTIMENT_ALPHA * s_total
    m_news = NEWS_VOLATILITY_MULTIPLIER if abs(ns) > NEWS_SENTIMENT_EXTREME_THRESHOLD else 1.0
    m_event = 1.0 + PREDICTION_EVENT_VOL_K * abs(min(0.0, f_score))
    sigma = garch_sigma * m_news * m_event
    if not np.isfinite(mu):
        logger.warning("forecast: μ 非有限值，取 0")
        mu = 0.0
    if not np.isfinite(sigma) or sigma <= 0:
        logger.warning("forecast: σ 非法 (%s)，兜底 20%% 年化", sigma)
        sigma = 0.20

    await _push(FormulaStep(
        key="gbm",
        title="几何布朗运动（GBM）参数",
        formula=(
            "μ = μ_ARIMA + α·S_total,  α=0.15\n"
            "M_news = 1.5 if |S_news|>0.7 else 1.0;  M_event = 1 + k·|min(0,F)|,  k=0.5\n"
            "σ = σ_GARCH × M_news × M_event\n"
            "S_{t+1} = Sₜ · exp((μ − ½σ²)·dt + σ·√dt·Z),  Z~N(0,1),  dt = 1/252"
        ),
        formula_latex=(
            r"S_{t+1}=S_t\exp\left[(\mu-\tfrac12\sigma^2)dt+\sigma\sqrt{dt}\,Z\right],\ "
            r"\mu=\mu_{ARIMA}+\alpha S_{total},\ \sigma=\sigma_{GARCH}M_{news}M_{event}"
        ),
        params={
            "mu_arima": float(arima_drift),
            "alpha": NEWS_SENTIMENT_ALPHA,
            "S_total": float(s_total),
            "mu_final": float(mu),
            "sigma_garch": float(garch_sigma),
            "M_news": float(m_news),
            "M_event": float(m_event),
            "sigma_final": float(sigma),
            "dt": MONTE_CARLO_DT,
            "last_close": last_close,
        },
        substituted=(
            f"μ = {_fmt(arima_drift)} + {NEWS_SENTIMENT_ALPHA:g}×{_fmt(s_total, 3)} = {_fmt(mu)}\n"
            f"σ = {_fmt(garch_sigma)} × {m_news:g}(M_news) × {_fmt(m_event, 3)}(M_event) = {_fmt(sigma)}\n"
            f"S₀ = {_fmt(last_close, 2)},  dt = 1/252 = {MONTE_CARLO_DT:.6f}"
        ),
        result=float(mu),
        result_text=f"μ={_fmt(mu)}（年化 {mu * 100:.2f}%）, σ={_fmt(sigma)}（年化 {sigma * 100:.2f}%）",
        explain=(
            "负面事件走**双通道**：既压低中枢 μ（S_total<0），又抬高波动 σ（M_event>1）。"
            "所以图上看到的是「预测中枢下移 + 置信带变宽」——爆雷的本质是不确定性放大，不只是跌。"
        ),
    ))

    # ── Step 7: 影线标定（§5.4）───────────────────────────────────────────────
    k_up = float(shadow.get("k_up", 0.0))
    k_dn = float(shadow.get("k_dn", 0.0))
    shadow_bars = int(shadow.get("bars", 0))
    await _push(FormulaStep(
        key="shadow",
        title="预测蜡烛影线标定",
        formula=(
            "k_up = mean((High − max(Open,Close)) / Close)\n"
            "k_dn = mean((min(Open,Close) − Low) / Close)\n"
            "H = max(O,C)·(1+k_up);  L = min(O,C)·(1−k_dn)"
        ),
        formula_latex=(
            r"k_{up}=\overline{\frac{H-\max(O,C)}{C}},\quad k_{dn}=\overline{\frac{\min(O,C)-L}{C}}"
        ),
        params={"k_up": k_up, "k_dn": k_dn, "lookback_bars": shadow_bars},
        substituted=(
            f"近 {shadow_bars} 根历史 K 线 → k_up = {k_up:.4f}（上影 {k_up:.2%}）, "
            f"k_dn = {k_dn:.4f}（下影 {k_dn:.2%}）"
        ),
        result=k_up,
        result_text=f"k_up={k_up:.4f} / k_dn={k_dn:.4f}（{shadow_bars} 根标定）",
        explain=(
            "预测蜡烛的影线由**该股历史平均影线比例**合成（庄股影线长、大盘股影线短），"
            "不代表真实日内极值分布 —— 这一点 UI 必须诚实标注。"
        ),
    ))

    # ── Step 8: 蒙特卡洛逐日 → OHLC 蜡烛 + 分位数 ─────────────────────────────
    mats = await run_compute(
        _monte_carlo_paths,
        last_close, mu, sigma, horizon, MONTE_CARLO_PATHS, k_up, k_dn,
    )

    last_date = (
        pd.Timestamp(work["date"].iloc[-1]) if "date" in work.columns and len(work)
        else pd.Timestamp.now().normalize()
    )
    dates = _future_dates(last_date, horizon)
    if len(dates) < horizon:
        dates = dates + [""] * (horizon - len(dates))

    candles: list[ForecastCandle] = []
    p10_list: list[float] = []
    p50_list: list[float] = []
    p90_list: list[float] = []
    pcts = MONTE_CARLO_PERCENTILES or (10, 50, 90)

    for t in range(horizon):
        c_col = mats["close"][:, t]
        o = float(np.median(mats["open"][:, t]))
        h = float(np.median(mats["high"][:, t]))
        low = float(np.median(mats["low"][:, t]))
        c = float(np.median(c_col))
        p10 = float(np.percentile(c_col, pcts[0]))
        p50 = float(np.percentile(c_col, 50))
        p90 = float(np.percentile(c_col, pcts[-1]))

        # 不变式兜底（数学上恒成立，浮点极端情况再钳一次 —— 前端画 K 线依赖它）
        h = max(h, o, c)
        low = min(low, o, c)
        if not (p10 <= p50 <= p90):
            logger.warning("分位数逆序 day=%d (p10=%.4f p50=%.4f p90=%.4f)，已排序修正", t + 1, p10, p50, p90)
            p10, p50, p90 = sorted((p10, p50, p90))

        candle = ForecastCandle(
            day=t + 1, date=dates[t],
            o=round(o, 4), h=round(h, 4), l=round(low, 4), c=round(c, 4),
            p10=round(p10, 4), p50=round(p50, 4), p90=round(p90, 4),
        )
        candles.append(candle)
        p10_list.append(candle.p10)
        p50_list.append(candle.p50)
        p90_list.append(candle.p90)
        await _emit(on_step, _candle_payload(candle))

    point_forecast = list(p50_list)

    # ── Step 9: 拐点 + 置信度 ─────────────────────────────────────────────────
    inflection = _inflection_points(dates, p50_list)

    confidence = 0.0
    try:
        p50_arr = np.asarray(p50_list, dtype=float)
        band = np.asarray(p90_list, dtype=float) - np.asarray(p10_list, dtype=float)
        ratio = np.where(
            np.isfinite(p50_arr) & (np.abs(p50_arr) > 1e-9), band / np.abs(p50_arr), np.nan,
        )
        mean_ratio = float(np.nanmean(ratio))
        if not np.isfinite(mean_ratio):
            mean_ratio = 1.0
        confidence = float(max(0.0, min(1.0, 1.0 - mean_ratio)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("confidence 计算失败 (%s)，兜底 0", exc)
        confidence = 0.0

    model_params = ModelParams(
        last_close=last_close,
        arima_order=tuple(arima_meta["order"]),          # type: ignore[arg-type]
        arima_aic=float(arima_meta["aic"]),
        arima_grid_tried=list(arima_meta["grid"]),
        arima_drift_annual=float(arima_drift),
        garch_omega=g_omega,
        garch_alpha=g_alpha,
        garch_beta=g_beta,
        garch_persistence=g_persist,
        garch_sigma_annual=float(garch_sigma),
        news_sentiment=ns,
        # 全通道透出（含 lockup）—— ModelParams 是公式卡片的落库真相源，
        # 少一个通道就等于对用户少说了一项实际参与计算的因子。
        event_scores=dict(ev["scores"]),
        event_weights={c: PREDICTION_EVENT_WEIGHTS.get(c, 0.0) for c in ev["scores"]},
        filing_score=float(f_score),
        filing_weight=lam,
        sentiment_total=float(s_total),
        alpha_used=NEWS_SENTIMENT_ALPHA,
        drift_mu_annual_final=float(mu),
        sigma_annual_final=float(sigma),
        vol_multiplier_news=float(m_news),
        vol_multiplier_event=float(m_event),
        mc_n_paths=MONTE_CARLO_PATHS,
        mc_percentiles=tuple(pcts),
        mc_dt=MONTE_CARLO_DT,
        shadow_k_up=k_up,
        shadow_k_dn=k_dn,
        shadow_lookback_bars=shadow_bars,
    )

    result = ForecastResult(
        dates=dates,
        point_forecast=point_forecast,
        p10=p10_list,
        p50=p50_list,
        p90=p90_list,
        inflection_points=inflection,
        confidence=confidence,
        model_name=MODEL_NAME,
        technical_indicators=technical,
        model_params=model_params,
        forecast_candles=candles,
        formula_steps=steps,
    )
    return result.model_dump()


async def run_forecast(
    bars_df: "pd.DataFrame",
    news_sentiment: float,
    horizon: int = PREDICTION_HORIZON_DAYS,
    *,
    filing_digest: list[Any] | None = None,
    on_step: StepCallback | None = None,
) -> dict:
    """forecast 的别名入口（节点层常用 run_* 命名）。"""
    return await forecast(
        bars_df, news_sentiment, horizon,
        filing_digest=filing_digest, on_step=on_step,
    )
