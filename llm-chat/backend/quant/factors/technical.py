"""技术面因子 — 动量、波动率、均线偏离

输入：bars(daily OHLCV，归一化列名) + spot(实时快照)
输出：以 symbol 为索引的 DataFrame，列：momentum, volatility, ma_deviation
"""
from __future__ import annotations

import logging
import time
import numpy as np
import pandas as pd

logger = logging.getLogger("quant.timer")

def compute_technical_factors(
    spot: pd.DataFrame,
    bars: pd.DataFrame,
    *,
    momentum_window: int = 60,
    volatility_window: int = 20,
) -> pd.DataFrame:
    out_records: list[dict] = []
    t_start = time.perf_counter()

    if not bars.empty and "symbol" in bars.columns:
        logger.info("      ∟ [技术因子] 开始分组计算, 原始行数: %d", len(bars))
        bars_sorted = bars.sort_values(["symbol", "date"])
        groups = list(bars_sorted.groupby("symbol"))
        total_syms = len(groups)
        logger.info("      ∟ [技术因子] 识别到 %d 只股票的 K 线数据", total_syms)

        for idx, (sym, group) in enumerate(groups, start=1):
            # 每 100 只股票打一个点，避免刷屏但保留进度感
            if idx % 100 == 0 or idx == total_syms:
                logger.info("      ∟ [技术因子] 进度: %d/%d (%.0f%%)", idx, total_syms, idx/total_syms*100)

            closes = pd.to_numeric(group["close"], errors="coerce").dropna().reset_index(drop=True)
            if len(closes) < 2:
                out_records.append(_empty_row(sym))
                continue

            window_close = closes.tail(momentum_window + 1)
            if len(window_close) >= 2 and window_close.iloc[0] > 0:
                momentum = (window_close.iloc[-1] / window_close.iloc[0] - 1.0) * 100.0
            else:
                momentum = np.nan

            rets = closes.pct_change().dropna()
            vol_window = rets.tail(volatility_window)
            volatility = (
                vol_window.std(ddof=0) * 100.0
                if len(vol_window) >= 5 else np.nan
            )

            ma_window = closes.tail(volatility_window)
            ma = ma_window.mean() if not ma_window.empty else np.nan
            if ma and not np.isnan(ma) and ma > 0:
                ma_dev = (closes.iloc[-1] - ma) / ma * 100.0
            else:
                ma_dev = np.nan

            out_records.append({
                "symbol": sym,
                "momentum": momentum,
                "volatility": volatility,
                "ma_deviation": ma_dev,
            })

    logger.info("      ∟ [技术因子] 核心计算完成, 耗时: %.0fms", (time.perf_counter() - t_start) * 1000)

    df = pd.DataFrame(out_records).set_index("symbol") if out_records else pd.DataFrame(
        columns=["momentum", "volatility", "ma_deviation"]
    )

    # spot 兜底：bars 缺失时用 60 日涨幅
    if "symbol" in spot.columns and "change_pct_60d" in spot.columns:
        spot_idx = spot.set_index("symbol")
        fallback = pd.to_numeric(spot_idx["change_pct_60d"], errors="coerce")
        df = df.reindex(spot_idx.index)
        df["momentum"] = df["momentum"].combine_first(fallback)
    return df


def _empty_row(symbol: str) -> dict:
    return {
        "symbol": symbol,
        "momentum": np.nan,
        "volatility": np.nan,
        "ma_deviation": np.nan,
    }
