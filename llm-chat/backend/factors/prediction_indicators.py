"""预测模块技术指标（纯 pandas/numpy 实现，不依赖 pandas-ta）。

所有函数：
  - 不修改输入（返回新 Series / dict）。
  - 历史不足 / 含 NaN 时返回 NaN 填充结果并打 warning（不抛异常）。
  - 仅做数学计算，无 LLM、无 DB。
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger("prediction.factors")


# ── 单序列原语 ────────────────────────────────────────────────────────────────

def ma(close: pd.Series, n: int) -> pd.Series:
    """简单移动平均（n 期）。不足 n 期处为 NaN，不修改输入。"""
    if close is None or len(close) == 0:
        return pd.Series(dtype=float)
    s = pd.to_numeric(close, errors="coerce").astype(float)
    res = s.rolling(window=n, min_periods=n).mean()
    if res.isna().all():
        logger.warning("ma(n=%d): 无足够历史，结果全 NaN（len=%d）", n, len(s))
    return res


def ema(close: pd.Series, n: int) -> pd.Series:
    """指数移动平均（span=n, adjust=False，与通达信/常用定义一致）。"""
    if close is None or len(close) == 0:
        return pd.Series(dtype=float)
    s = pd.to_numeric(close, errors="coerce").astype(float)
    if len(s) < n:
        logger.warning("ema(n=%d): 历史不足（len=%d），结果部分 NaN", n, len(s))
    # adjust=False 使用递推公式：y_t = (1-α)y_{t-1} + α x_t, α = 2/(n+1)
    res = s.ewm(span=n, adjust=False, min_periods=n).mean()
    # 前 n-1 个用 NaN（与 MA 对齐，避免热身期误导）
    res = res.copy()
    res.iloc[: n - 1] = np.nan
    return res


# ── MACD ──────────────────────────────────────────────────────────────────────

def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """MACD 指标。

    DIF = EMA(close, fast) - EMA(close, slow)
    DEA = EMA(DIF, signal)
    hist = 2 * (DIF - DEA)        # 通达信式（×2），与同花顺一致
    """
    s = pd.to_numeric(close, errors="coerce").astype(float)
    if len(s) < slow:
        logger.warning("macd: 历史不足 slow=%d（len=%d），返回 NaN", slow, len(s))
        nan = pd.Series(np.nan, index=s.index, dtype=float)
        return {"dif": nan, "dea": nan, "hist": nan}
    ema_fast = ema(s, fast)
    ema_slow = ema(s, slow)
    dif = ema_fast - ema_slow
    # DEA = EMA(DIF, signal)，DIF 前 slow-1 位为 NaN，ewm 会自动从首个有效值起算
    dea = dif.ewm(span=signal, adjust=False, min_periods=signal).mean()
    dea = dea.copy()
    # 与 DIF 有效区间对齐：DEA 须在 DIF 有效且有足够热身后才有意义
    first_valid = dif.first_valid_index()
    if first_valid is not None:
        pos = dif.index.get_loc(first_valid)
        dea.iloc[: pos + signal - 1] = np.nan
    hist = 2 * (dif - dea)
    return {"dif": dif, "dea": dea, "hist": hist}


# ── RSI（Wilder） ─────────────────────────────────────────────────────────────

def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's RSI，clamp 到 [0, 100]。"""
    s = pd.to_numeric(close, errors="coerce").astype(float)
    if len(s) < n + 1:
        logger.warning("rsi(n=%d): 历史不足（len=%d），返回 NaN", n, len(s))
        return pd.Series(np.nan, index=s.index, dtype=float)
    delta = s.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    # Wilder 平滑 = EWM with alpha=1/n (等价于 span=2n-1, adjust=False)
    avg_gain = gain.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    avg_loss = loss.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi_val = 100.0 - (100.0 / (1.0 + rs))
    # avg_loss=0 → RS=inf → RSI=100；avg_gain=0 且 avg_loss>0 → RSI=0
    rsi_val = rsi_val.where(avg_loss > 0, 100.0)
    rsi_val = rsi_val.where(~((avg_gain == 0) & (avg_loss > 0)), 0.0)
    rsi_val = rsi_val.clip(lower=0.0, upper=100.0)
    # 前 n 期无定义 → NaN
    rsi_val = rsi_val.copy()
    rsi_val.iloc[:n] = np.nan
    return rsi_val


# ── Bollinger Bands ───────────────────────────────────────────────────────────

def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> dict:
    """布林带。mid=MA(n), upper/lower=mid ± k*std(n)。"""
    s = pd.to_numeric(close, errors="coerce").astype(float)
    if len(s) < n:
        logger.warning("bollinger(n=%d): 历史不足（len=%d），返回 NaN", n, len(s))
        nan = pd.Series(np.nan, index=s.index, dtype=float)
        return {"upper": nan, "mid": nan, "lower": nan}
    mid = s.rolling(window=n, min_periods=n).mean()
    std = s.rolling(window=n, min_periods=n).std(ddof=1)
    upper = mid + k * std
    lower = mid - k * std
    return {"upper": upper, "mid": mid, "lower": lower}


# ── KDJ ───────────────────────────────────────────────────────────────────────

def _sma_chinese(x: pd.Series, m: int) -> pd.Series:
    """通达信式 SMA：y_t = (x_t + (m-1)*y_{t-1}) / m  (即 alpha=1/m 的 EWM)。

    等价于 ewm(alpha=1/m, adjust=False)，但需在首个有效值后开始递推。
    """
    return x.ewm(alpha=1.0 / m, adjust=False, min_periods=1).mean()


def kdj(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    n: int = 9,
    m1: int = 3,
    m2: int = 3,
) -> dict:
    """KDJ 随机指标。

    RSV = (C - Ln) / (Hn - Ln) * 100
    K   = SMA(RSV, m1)
    D   = SMA(K,   m2)
    J   = 3*K - 2*D
    """
    h = pd.to_numeric(high, errors="coerce").astype(float)
    l = pd.to_numeric(low, errors="coerce").astype(float)
    c = pd.to_numeric(close, errors="coerce").astype(float)
    if len(c) < n:
        logger.warning("kdj(n=%d): 历史不足（len=%d），返回 NaN", n, len(c))
        nan = pd.Series(np.nan, index=c.index, dtype=float)
        return {"k": nan, "d": nan, "j": nan}
    hh = h.rolling(window=n, min_periods=n).max()
    ll = l.rolling(window=n, min_periods=n).min()
    denom = (hh - ll).replace(0.0, np.nan)
    rsv = (c - ll) / denom * 100.0
    # 分母为 0（n 期内高低相同）→ RSV 视为 50（中性）
    rsv = rsv.where(~denom.isna() & (denom != 0), 50.0)
    k = _sma_chinese(rsv, m1)
    d = _sma_chinese(k, m2)
    j = 3 * k - 2 * d
    # RSV 前 n-1 期无定义 → K/D/J 同样不定义
    k = k.copy(); k.iloc[: n - 1] = np.nan
    d = d.copy(); d.iloc[: n - 1] = np.nan
    j = j.copy(); j.iloc[: n - 1] = np.nan
    return {"k": k, "d": d, "j": j}


# ── 预测 K 线影线标定（设计文档 §5.4）────────────────────────────────────────────

# 影线系数的合理上界：单日影线超过收盘价的 30% 属于异常（多半是脏数据 / 拆股未复权），
# 钳制后仍保证 OHLC 不变式（k_up ≥ 0 → H ≥ max(O,C)；0 ≤ k_dn < 1 → L ≤ min(O,C) 且 L > 0）。
SHADOW_K_MAX: float = 0.30


def calibrate_shadows(df: pd.DataFrame, lookback: int = 120) -> dict:
    """从历史 K 线标定平均影线比例（设计文档 §5.4 第 1 步）。

        k_up = mean( (High − max(Open, Close)) / Close )    ← 近 lookback 根
        k_dn = mean( (min(Open, Close) − Low)  / Close )

    这是「可解释、非拍脑袋」的做法：预测蜡烛的影线长度直接来自该股自己的历史日内波动
    习惯（庄股影线长、大盘股影线短），而不是一个魔法常数。

    诚实标注（UI 必须写）：预测蜡烛的影线是历史平均比例的合成，**不代表真实日内极值分布**。

    Returns {"k_up": float, "k_dn": float, "bars": int}
      - 缺列 / 历史不足 / 全 NaN → {"k_up": 0.0, "k_dn": 0.0, "bars": 0} + warning
        （k=0 时蜡烛退化为无影线实体，OHLC 不变式仍成立 —— 宁可没影线，不可造假影线）
    """
    empty = {"k_up": 0.0, "k_dn": 0.0, "bars": 0}
    if df is None or len(df) == 0:
        logger.warning("calibrate_shadows: 输入为空，影线系数取 0")
        return empty

    required = {"open", "high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        logger.warning("calibrate_shadows: 缺少列 %s，影线系数取 0", sorted(missing))
        return empty

    work = df.copy()
    if "date" in work.columns:
        work = work.sort_values("date")
    work = work.tail(max(int(lookback), 1))

    o = pd.to_numeric(work["open"], errors="coerce").astype(float)
    h = pd.to_numeric(work["high"], errors="coerce").astype(float)
    l = pd.to_numeric(work["low"], errors="coerce").astype(float)
    c = pd.to_numeric(work["close"], errors="coerce").astype(float)

    body_hi = np.maximum(o, c)
    body_lo = np.minimum(o, c)
    denom = c.where(c > 0)  # 收盘价 ≤ 0 的脏行直接作废（除零 → NaN → 被 nanmean 忽略）

    up = (h - body_hi) / denom
    dn = (body_lo - l) / denom

    # 负值只可能来自脏数据（high < max(open,close)）——截断到 0，不让它反向拉低均值
    up = up.where(np.isfinite(up) & (up >= 0.0))
    dn = dn.where(np.isfinite(dn) & (dn >= 0.0))

    n_valid = int(min(up.notna().sum(), dn.notna().sum()))
    if n_valid == 0:
        logger.warning("calibrate_shadows: %d 根 K 线中无有效影线样本，影线系数取 0", len(work))
        return empty

    k_up = float(up.mean(skipna=True))
    k_dn = float(dn.mean(skipna=True))
    if not np.isfinite(k_up):
        k_up = 0.0
    if not np.isfinite(k_dn):
        k_dn = 0.0

    k_up_c = float(min(max(k_up, 0.0), SHADOW_K_MAX))
    k_dn_c = float(min(max(k_dn, 0.0), SHADOW_K_MAX))
    if (k_up_c, k_dn_c) != (k_up, k_dn):
        logger.warning(
            "calibrate_shadows: 影线系数异常 (k_up=%.4f, k_dn=%.4f)，钳制到 [0, %.2f]",
            k_up, k_dn, SHADOW_K_MAX,
        )

    return {"k_up": k_up_c, "k_dn": k_dn_c, "bars": n_valid}


# ── 汇总 ──────────────────────────────────────────────────────────────────────

def _last_finite(s: pd.Series) -> float:
    """取最后一个有限值，没有则返回 NaN。"""
    if s is None or len(s) == 0:
        return float("nan")
    s = s.dropna()
    if s.empty:
        return float("nan")
    return float(s.iloc[-1])


def _last_dict(d: dict) -> dict:
    """对 dict of Series，取每个序列最后一个有限值。"""
    out: dict[str, Any] = {}
    for key, val in d.items():
        if isinstance(val, pd.Series):
            out[key] = _last_finite(val)
        elif isinstance(val, dict):
            out[key] = _last_dict(val)
        else:
            out[key] = val
    return out


def compute_all(df: pd.DataFrame) -> dict:
    """计算全部技术指标快照，供 technical_indicators 字段 + 分析使用。

    输入 df 列：date, open, high, low, close, volume（归一化 schema）。
    返回 dict：
      {ma5, ma10, ma20, ema12, ema26, macd:{dif,dea,hist}, rsi14,
       boll:{upper,mid,lower}, kdj:{k,d,j}, last_close}

    所有标量为「最新一个有效值」（不足历史返回 NaN）。
    不修改输入 df。
    """
    out: dict[str, Any] = {
        "ma5": float("nan"),
        "ma10": float("nan"),
        "ma20": float("nan"),
        "ema12": float("nan"),
        "ema26": float("nan"),
        "macd": {"dif": float("nan"), "dea": float("nan"), "hist": float("nan")},
        "rsi14": float("nan"),
        "boll": {"upper": float("nan"), "mid": float("nan"), "lower": float("nan")},
        "kdj": {"k": float("nan"), "d": float("nan"), "j": float("nan")},
        "last_close": float("nan"),
    }
    if df is None or len(df) == 0:
        logger.warning("compute_all: 输入为空，返回全 NaN 指标")
        return out

    required = {"close"}
    if not required.issubset(set(df.columns)):
        logger.warning("compute_all: 缺少必要列 close，返回全 NaN 指标（cols=%s）", list(df.columns))
        return out

    # 不修改输入：按 date 排序后拷贝
    work = df.copy()
    if "date" in work.columns:
        work = work.sort_values("date").reset_index(drop=True)

    close = pd.to_numeric(work["close"], errors="coerce").astype(float)
    if close.dropna().empty:
        logger.warning("compute_all: close 全为 NaN，返回全 NaN 指标")
        return out

    ma5 = ma(close, 5)
    ma10 = ma(close, 10)
    ma20 = ma(close, 20)
    ema12 = ema(close, 12)
    ema26 = ema(close, 26)
    macd_d = macd(close)
    rsi14 = rsi(close, 14)
    boll_d = bollinger(close, 20, 2)

    kdj_d = {"k": pd.Series(dtype=float), "d": pd.Series(dtype=float), "j": pd.Series(dtype=float)}
    if {"high", "low"}.issubset(set(work.columns)):
        kdj_d = kdj(work["high"], work["low"], close)
    else:
        logger.warning("compute_all: 缺少 high/low 列，KDJ 返回 NaN")

    out["ma5"] = _last_finite(ma5)
    out["ma10"] = _last_finite(ma10)
    out["ma20"] = _last_finite(ma20)
    out["ema12"] = _last_finite(ema12)
    out["ema26"] = _last_finite(ema26)
    out["macd"] = {
        "dif": _last_finite(macd_d["dif"]),
        "dea": _last_finite(macd_d["dea"]),
        "hist": _last_finite(macd_d["hist"]),
    }
    out["rsi14"] = _last_finite(rsi14)
    out["boll"] = {
        "upper": _last_finite(boll_d["upper"]),
        "mid": _last_finite(boll_d["mid"]),
        "lower": _last_finite(boll_d["lower"]),
    }
    out["kdj"] = {
        "k": _last_finite(kdj_d["k"]),
        "d": _last_finite(kdj_d["d"]),
        "j": _last_finite(kdj_d["j"]),
    }
    out["last_close"] = _last_finite(close)
    return out
