"""流动性因子 — 近 N 日平均成交额（亿元）"""
from __future__ import annotations

import pandas as pd


def compute_liquidity_factors(spot: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
    """优先用 bars 算 20 日均成交额（亿元），bars 为空时退到 spot 当日成交额（亿元）。"""
    if not bars.empty and "amount" in bars.columns:
        avg_yuan = bars.groupby("symbol")["amount"].mean()
        avg_amount = (avg_yuan / 1e8).rename("avg_turnover")
    else:
        avg_amount = pd.Series(dtype=float, name="avg_turnover")

    if "symbol" in spot.columns:
        spot_idx = spot.set_index("symbol")
        if "amount" in spot_idx.columns:
            spot_amount = pd.to_numeric(spot_idx["amount"], errors="coerce").rename("avg_turnover")
            avg_amount = avg_amount.combine_first(spot_amount)
    return avg_amount.to_frame()
