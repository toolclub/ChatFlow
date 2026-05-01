"""风险因子 — ST / 停牌 / 异常标记"""
from __future__ import annotations

import pandas as pd


def _is_st_name(name: object) -> bool:
    s = str(name or "").upper().replace(" ", "")
    return (
        s.startswith("ST")
        or s.startswith("*ST")
        or s.startswith("S*ST")
        or "退" in s
        or "退市" in str(name or "")
    )


def compute_risk_factors(spot: pd.DataFrame) -> pd.DataFrame:
    """返回索引为 symbol 的 DataFrame，列：is_st, is_suspended, has_negative_pe"""
    if spot.empty:
        return pd.DataFrame(columns=["is_st", "is_suspended", "has_negative_pe"])

    df = spot.set_index("symbol").copy()
    out = pd.DataFrame(index=df.index)

    out["is_st"] = df["name"].map(_is_st_name) if "name" in df.columns else False

    if "volume" in df.columns:
        out["is_suspended"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0) <= 0
    else:
        out["is_suspended"] = False

    if "pe" in df.columns:
        pe = pd.to_numeric(df["pe"], errors="coerce")
        out["has_negative_pe"] = pe.fillna(0) <= 0
    else:
        out["has_negative_pe"] = False
    return out
