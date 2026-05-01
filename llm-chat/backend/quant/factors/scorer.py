"""通用打分原语 — winsorize / z-score / 0-100 平台分"""
from __future__ import annotations

import logging
import time
import numpy as np
import pandas as pd

logger = logging.getLogger("quant.timer")


def winsorize(s: pd.Series, lower: float = 0.025, upper: float = 0.975) -> pd.Series:
    """按分位裁剪极值。NaN 保留不变。"""
    valid = s.dropna()
    if valid.empty:
        return s
    lo, hi = valid.quantile([lower, upper])
    return s.clip(lower=lo, upper=hi)


def zscore(s: pd.Series, *, do_winsorize: bool = True) -> pd.Series:
    """标准化 — NaN 保留为 NaN（由 caller 决定如何 fillna）。"""
    t0 = time.perf_counter()
    if do_winsorize:
        s = winsorize(s)
    valid = s.dropna()
    if valid.empty:
        return pd.Series(np.nan, index=s.index)
    mean = valid.mean()
    std = valid.std(ddof=0)
    res = (s - mean) / std if std != 0 else (s - mean)
    
    # 仅对大规模计算打印日志
    if len(s) > 100:
        logger.debug("        ∟ [打分] Z-Score 完成 | 样本: %d | 耗时: %.1fms", len(s), (time.perf_counter() - t0) * 1000)
    return res


def percentile_rank(s: pd.Series) -> pd.Series:
    """0..1 之间的百分位（NaN 保留）。"""
    return s.rank(pct=True, na_option="keep")


def to_score_100(z: pd.Series, *, scale: float = 20.0, base: float = 50.0) -> pd.Series:
    """z-score → 0..100 平台分（中位 50，σ≈scale）。"""
    score = base + scale * z.fillna(0)
    return score.clip(lower=0, upper=100)


def compose_scores(
    components: dict[str, pd.Series],
    weights: dict[str, float],
) -> pd.Series:
    """按权重合成综合分。components/weights 的 key 必须一致。"""
    t0 = time.perf_counter()
    if not components:
        raise ValueError("compose_scores 收到空的 components")
    common_idx = None
    for s in components.values():
        common_idx = s.index if common_idx is None else common_idx.union(s.index)
    total = pd.Series(0.0, index=common_idx)
    weight_sum = 0.0
    for key, w in weights.items():
        if key not in components or w <= 0:
            continue
        total = total.add(components[key].reindex(common_idx).fillna(0) * w, fill_value=0)
        weight_sum += w
    if weight_sum <= 0:
        raise ValueError("权重之和必须 > 0")
        
    res = total / weight_sum
    logger.info("      ∟ [打分] 综合分合成完成 | 维度: %s | 耗时: %.1fms", list(components.keys()), (time.perf_counter() - t0) * 1000)
    return res
