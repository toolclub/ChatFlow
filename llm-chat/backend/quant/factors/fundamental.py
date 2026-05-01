"""基本面因子 — PE / PB / 市值

数据源优先级：
  1. spot 快照（akshare EM clist / tushare daily_basic 都有 PE/PB/市值）
  2. bars 最新日的 peTTM / pbMRQ（baostock 的 K 线自带这些字段）

caller 负责把 bars 传进来；spot 缺失字段时，自动用 bars 兜底。
"""
from __future__ import annotations

import logging
import time
import numpy as np
import pandas as pd

logger = logging.getLogger("quant.timer")


def _spot_col_or_nan(df: pd.DataFrame, col: str) -> pd.Series:
    if col in df.columns:
        return pd.to_numeric(df[col], errors="coerce")
    return pd.Series(np.nan, index=df.index, dtype="float64")


def compute_fundamental_factors(
    spot: pd.DataFrame,
    bars: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """返回索引为 symbol 的 DataFrame，列：pe, pb, market_cap"""
    t_start = time.perf_counter()
    if spot.empty:
        return pd.DataFrame(columns=["pe", "pb", "market_cap"])

    df = spot.set_index("symbol").copy()
    out = pd.DataFrame(index=df.index)
    out["pe"] = _spot_col_or_nan(df, "pe")
    out["pb"] = _spot_col_or_nan(df, "pb")
    out["market_cap"] = _spot_col_or_nan(df, "market_cap")

    if bars is not None and not bars.empty and "symbol" in bars.columns:
        latest = (
            bars.sort_values(["symbol", "date"])
                .groupby("symbol")
                .tail(1)
                .set_index("symbol")
        )
        if "peTTM" in latest.columns:
            bars_pe = pd.to_numeric(latest["peTTM"], errors="coerce").reindex(out.index)
            out["pe"] = out["pe"].combine_first(bars_pe)
        if "pbMRQ" in latest.columns:
            bars_pb = pd.to_numeric(latest["pbMRQ"], errors="coerce").reindex(out.index)
            out["pb"] = out["pb"].combine_first(bars_pb)
            
    logger.info("      ∟ [基本面] 因子提取完成, 耗时: %.0fms", (time.perf_counter() - t_start) * 1000)
    return out
