"""股票预测计算引擎（forecast + 技术指标）—— 单元测试

验证 graph/prediction/engine.py::forecast + factors/prediction_indicators.py::compute_all。
statsmodels / arch 在系统 python 不可用（仅 Docker 容器内有），故在 sys.modules 层
注入 fake 模块，让 forecast 走真实 ARIMA + GARCH 代码路径（而非 naive drift 降级）：

  - fake ARIMA.fit().forecast(steps) 返回固定步进漂移（last_close × 1.002^(t+1)）
  - fake arch_model().fit().forecast() 返回固定方差（→ sigma_annual = 0.30）

铁律对照：
  #4 async 纪律 — forecast 是 async def，测试用 asyncio.run 驱动
  #2 结构化数据走独立字段 — 检查 ForecastResult 的独立 key（point_forecast/p10/...）
  #9 不吞异常 — 引擎 except 块均 logger.warning + 降级，这里只读其产出
"""
from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from fsm.sse_events import SSEEventType
from graph.prediction.engine import MODEL_NAME


# ── sys.modules 层 fake statsmodels / arch（import-collect 即生效，test 间共享） ──
# engine 的 _fit_arima / _garch_vol 是函数内 lazy import：
#   from statsmodels.tsa.arima.model import ARIMA   # _fit_arima
#   from arch import arch_model                      # _garch_vol
# 只要 sys.modules 里有对应模块即可被 from-import 命中，无需真正安装。

_ARIMA_STEP: float = 0.002          # 0.2% / 日 固定步进漂移
_FIXED_SIGMA_ANNUAL: float = 0.30   # GARCH 固定年化波动率
_FIXED_VAR_DAILY: float = _FIXED_SIGMA_ANNUAL ** 2 / 252.0
_FIXED_VAR_PCT: float = _FIXED_VAR_DAILY * (100.0 ** 2)  # arch 以百分数收益率计方差


class _ARIMAFit:
    """fake ARIMA.fit() 结果：固定步进漂移 forecast + 固定 AIC。"""

    def __init__(self, s: pd.Series):
        self._s = s
        self.aic = -100.0  # 任意有限值，确保网格选出此阶数

    def forecast(self, steps: int) -> list[float]:
        last = float(self._s.iloc[-1])
        return [last * (1.0 + _ARIMA_STEP) ** (t + 1) for t in range(steps)]


class _ARIMA:
    """fake statsmodels.tsa.arima.model.ARIMA：构造时记下序列，fit 返回 _ARIMAFit。"""

    def __init__(self, s, order):
        self._s = s

    def fit(self):
        return _ARIMAFit(self._s)


class _ArchForecast:
    """fake arch fit.forecast() 结果：variance.values[-1] → 固定方差。"""

    def __init__(self):
        # variance 是 DataFrame，.values[-1] 取最后一行
        self.variance = pd.DataFrame({"h.1": [_FIXED_VAR_PCT]})


# fake GARCH(1,1) 拟合参数（§5.2：v1 把 res.params 直接丢弃，v2 必须回传给公式卡片）。
# α+β = 0.98 < 1 → 平稳。ω 以「百分数收益率方差」为单位（arch 惯例），engine ÷100² 换算回小数。
_FAKE_OMEGA_PCT: float = 0.02
_FAKE_ALPHA: float = 0.08
_FAKE_BETA: float = 0.90


class _ArchFit:
    def __init__(self):
        # 真 arch 的 res.params 是 pd.Series，索引名 omega / alpha[1] / beta[1]。
        # ★ 原 fake 没有这个属性 → engine 读 res.params 抛 AttributeError → 被 `except Exception`
        #   兜住并**静默降级成历史 std**（α=β=0，fallback=True），GARCH 分支从未被真正测到。
        #   补上它，下面 T-PRED-V2-01/02 才是在测真 GARCH 路径而不是测降级路径。
        self.params = pd.Series({
            "omega": _FAKE_OMEGA_PCT,
            "alpha[1]": _FAKE_ALPHA,
            "beta[1]": _FAKE_BETA,
        })

    def forecast(self, horizon, reindex=False):
        return _ArchForecast()


class _ArchModel:
    def __init__(self, *args, **kwargs):
        pass

    def fit(self, disp="off"):
        return _ArchFit()


def _install_fake_statsmodels_arch() -> None:
    """把 fake statsmodels / arch 注册进 sys.modules（**无条件覆盖**）。

    ★ 原来这里写的是 `if "statsmodels" not in sys.modules:` —— 全套件跑时会被静默架空：
      tests/test_prediction_auth.py（按字母序更早被 collect）在模块级也塞了一份**哑桩**
      （ARIMA = object、arch_model = lambda: None）。哑桩先占坑 → 本文件的 guard 跳过安装
      → forecast 里 ARIMA 构造失败走 naive drift、GARCH 走 `'NoneType' object has no
      attribute 'fit'` 降级历史 std。
      结果：单跑本文件走真 ARIMA/GARCH 路径，全套件跑走降级路径 —— 同一批断言测的是两套代码。
      老断言只校验区间（0<=confidence<=1）所以一直是绿的，掩盖了这件事。
      改成无条件覆盖：谁先塞的哑桩都不影响，本文件永远测真路径。
    """
    sm = types.ModuleType("statsmodels")
    tsa = types.ModuleType("statsmodels.tsa")
    arima_pkg = types.ModuleType("statsmodels.tsa.arima")
    arima_model = types.ModuleType("statsmodels.tsa.arima.model")
    arima_model.ARIMA = _ARIMA
    sm.tsa = tsa
    tsa.arima = arima_pkg
    arima_pkg.model = arima_model
    sys.modules["statsmodels"] = sm
    sys.modules["statsmodels.tsa"] = tsa
    sys.modules["statsmodels.tsa.arima"] = arima_pkg
    sys.modules["statsmodels.tsa.arima.model"] = arima_model

    arch_mod = types.ModuleType("arch")
    arch_mod.arch_model = _ArchModel
    sys.modules["arch"] = arch_mod


# 模块导入时装一次（供 collect 期的 from-import 用）
_install_fake_statsmodels_arch()


@pytest.fixture(autouse=True)
def _force_fake_statsmodels_arch():
    """每个用例开跑前重装一次 —— 彻底免疫「别的测试模块在 collect 期塞哑桩」的顺序依赖。"""
    _install_fake_statsmodels_arch()
    yield


# ── 合成 bars_df（~60 行、上升趋势 close）─────────────────────────────────────

def _make_bars(n: int = 60, start_price: float = 10.0) -> pd.DataFrame:
    """构造 n 日上升日线：close 每日 +0.5%，high/low 在 close 上下，含 open/volume。"""
    end = pd.Timestamp.now().normalize()
    rows = []
    price = start_price
    for i in range(n):
        day = end - timedelta(days=n - 1 - i)
        # 强上升趋势：+0.5%/日，让 KDJ 的 J 能突破 100
        price = price * (1.0 + 0.005)
        close = round(price, 4)
        rows.append({
            "date": day.strftime("%Y-%m-%d"),
            "open": round(close * 0.998, 4),
            "high": round(close * 1.006, 4),
            "low": round(close * 0.994, 4),
            "close": close,
            "volume": 1_000_000 + i * 1000,
        })
    return pd.DataFrame(rows)


def _make_bars_falling(n: int = 60, start_price: float = 50.0) -> pd.DataFrame:
    """构造 n 日下降日线：close 每日 -0.5%，用于验证 KDJ J 能跌破 0。"""
    end = pd.Timestamp.now().normalize()
    rows = []
    price = start_price
    for i in range(n):
        day = end - timedelta(days=n - 1 - i)
        price = price * (1.0 - 0.005)
        close = round(price, 4)
        rows.append({
            "date": day.strftime("%Y-%m-%d"),
            "open": round(close * 1.002, 4),
            "high": round(close * 1.006, 4),
            "low": round(close * 0.994, 4),
            "close": close,
            "volume": 1_000_000 + i * 1000,
        })
    return pd.DataFrame(rows)


def _make_bars_reversal_up(
    n: int = 60, down_days: int = 50, start_price: float = 50.0, up_rate: float = 0.02
) -> pd.DataFrame:
    """先下行 down_days 日再急速反弹——KDJ 的 K 快速越过滞后的 D，使 J>100。"""
    end = pd.Timestamp.now().normalize()
    rows = []
    price = start_price
    for i in range(n):
        day = end - timedelta(days=n - 1 - i)
        if i < down_days:
            price = price * (1.0 - 0.004)
        else:
            price = price * (1.0 + up_rate)
        close = round(price, 4)
        rows.append({
            "date": day.strftime("%Y-%m-%d"),
            "open": close,
            "high": round(close * 1.004, 4),
            "low": round(close * 0.996, 4),
            "close": close,
            "volume": 1_000_000 + i * 1000,
        })
    return pd.DataFrame(rows)


def _make_bars_reversal_down(
    n: int = 60, up_days: int = 50, start_price: float = 10.0, down_rate: float = 0.02
) -> pd.DataFrame:
    """先上行 up_days 日再急速回落——KDJ 的 K 快速跌破滞后的 D，使 J<0。"""
    end = pd.Timestamp.now().normalize()
    rows = []
    price = start_price
    for i in range(n):
        day = end - timedelta(days=n - 1 - i)
        if i < up_days:
            price = price * (1.0 + 0.004)
        else:
            price = price * (1.0 - down_rate)
        close = round(price, 4)
        rows.append({
            "date": day.strftime("%Y-%m-%d"),
            "open": close,
            "high": round(close * 1.004, 4),
            "low": round(close * 0.996, 4),
            "close": close,
            "volume": 1_000_000 + i * 1000,
        })
    return pd.DataFrame(rows)


# ── 测试用例 ──────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_forecast_returns_full_structure():
    """T-PRED-01：forecast 返回 dict 结构完整、长度对齐 horizon。"""
    from graph.prediction.engine import forecast

    horizon = 5
    bars = _make_bars(n=60)
    np.random.seed(42)
    res = asyncio.run(forecast(bars, news_sentiment=0.0, horizon=horizon))

    required = {
        "dates", "point_forecast", "p10", "p50", "p90",
        "inflection_points", "confidence", "model_name", "technical_indicators",
    }
    assert required.issubset(res.keys()), f"缺 key: {required - set(res.keys())}"

    assert len(res["dates"]) == horizon
    assert len(res["point_forecast"]) == horizon
    assert len(res["p10"]) == horizon
    assert len(res["p50"]) == horizon
    assert len(res["p90"]) == horizon
    assert isinstance(res["inflection_points"], list)
    assert 0.0 <= res["confidence"] <= 1.0
    assert res["model_name"] == MODEL_NAME
    assert isinstance(res["technical_indicators"], dict)


@pytest.mark.unit
def test_forecast_quantile_ordering():
    """T-PRED-02：每个时点 p10 <= p50 <= p90。"""
    from graph.prediction.engine import forecast

    bars = _make_bars(n=60)
    np.random.seed(7)
    res = asyncio.run(forecast(bars, news_sentiment=0.0, horizon=5))

    p10, p50, p90 = res["p10"], res["p50"], res["p90"]
    for i in range(len(p10)):
        # 全部有限
        assert np.isfinite(p10[i]) and np.isfinite(p50[i]) and np.isfinite(p90[i])
        assert p10[i] <= p50[i] + 1e-9, f"p10>p50 @ {i}: {p10[i]} > {p50[i]}"
        assert p50[i] <= p90[i] + 1e-9, f"p50>p90 @ {i}: {p50[i]} > {p90[i]}"


@pytest.mark.unit
def test_forecast_point_forecast_equals_p50():
    """T-PRED-03：point_forecast 与 p50 一致（中位点预测）。"""
    from graph.prediction.engine import forecast

    bars = _make_bars(n=60)
    np.random.seed(123)
    res = asyncio.run(forecast(bars, news_sentiment=0.0, horizon=5))
    assert res["point_forecast"] == res["p50"]


@pytest.mark.unit
def test_forecast_dates_are_future_business_days():
    """T-PRED-04：dates 是 last_date 之后的 horizon 个工作日（跳周末）。"""
    from graph.prediction.engine import forecast

    bars = _make_bars(n=60)
    res = asyncio.run(forecast(bars, news_sentiment=0.0, horizon=5))
    last_date = pd.Timestamp(bars["date"].iloc[-1])
    expected = pd.bdate_range(start=last_date + pd.Timedelta(days=1), periods=5)
    assert res["dates"] == [d.strftime("%Y-%m-%d") for d in expected]


@pytest.mark.unit
def test_technical_indicators_ranges():
    """T-PRED-05：technical_indicators 各指标取值合规。

    rsi ∈ [0,100]；macd 含 dif/dea/hist；boll upper>=mid>=lower；
    kdj J 在强趋势下能突破 100（不被 clip 到 [0,100]）。
    """
    from graph.prediction.engine import forecast

    bars = _make_bars(n=60)
    res = asyncio.run(forecast(bars, news_sentiment=0.0, horizon=5))
    ti = res["technical_indicators"]

    # rsi 在 [0,100]（强上升趋势下应接近 100）
    rsi = ti["rsi14"]
    assert np.isfinite(rsi), "rsi14 应有限值"
    assert 0.0 <= rsi <= 100.0, f"rsi14 越界: {rsi}"

    # macd 三键齐全
    macd = ti["macd"]
    assert {"dif", "dea", "hist"}.issubset(macd.keys()), f"macd 缺键: {macd}"

    # bollinger upper>=mid>=lower
    boll = ti["boll"]
    assert boll["upper"] >= boll["mid"] - 1e-9, f"boll upper<mid: {boll}"
    assert boll["mid"] >= boll["lower"] - 1e-9, f"boll mid<lower: {boll}"

    # kdj 三键齐全且 J 有限值（J 不 clip 到 [0,100] 的能力在 T-PRED-06 单测验证）
    kdj = ti["kdj"]
    assert {"k", "d", "j"}.issubset(kdj.keys())
    assert np.isfinite(kdj["j"]), "kdj.j 应有限值"
    # RSI 强上升趋势下接近上界
    assert rsi >= 50.0, f"上升趋势 RSI 应 >=50: {rsi}"


@pytest.mark.unit
def test_kdj_j_can_exceed_100_and_drop_below_0():
    """T-PRED-06：直接验证 KDJ J 不被 clip——下行反转上行序列 J>100，上行反转下行 J<0。"""
    from factors.prediction_indicators import kdj

    # 下行 50 日后急速反弹 → K 快速越过滞后的 D → J>100
    up = _make_bars_reversal_up(n=60, down_days=50, up_rate=0.02)
    kdj_up = kdj(up["high"], up["low"], up["close"])
    j_up = float(kdj_up["j"].dropna().iloc[-1])
    assert j_up > 100.0, f"反转上行 J 应 >100: {j_up}"

    # 上行 50 日后急速回落 → K 快速跌破滞后的 D → J<0
    down = _make_bars_reversal_down(n=60, up_days=50, down_rate=0.02)
    kdj_down = kdj(down["high"], down["low"], down["close"])
    j_down = float(kdj_down["j"].dropna().iloc[-1])
    assert j_down < 0.0, f"反转下行 J 应 <0: {j_down}"


@pytest.mark.unit
def test_news_sentiment_shifts_drift_direction():
    """T-PRED-07：资讯情绪修正方向——sentiment=+1 的点预测均值高于 sentiment=-1。

    NEWS_SENTIMENT_ALPHA=0.15：drift_mu = arima_drift + 0.15*ns。
    ±1 同样触发波动率扩大（|ns|>0.7），sigma 相同 → 唯一差异是漂移方向。
    固定 np.random seed 保证 MC 噪声一致。
    """
    from graph.prediction.engine import forecast

    bars = _make_bars(n=60)
    horizon = 5

    np.random.seed(2024)
    res_pos = asyncio.run(forecast(bars, news_sentiment=1.0, horizon=horizon))
    np.random.seed(2024)
    res_neg = asyncio.run(forecast(bars, news_sentiment=-1.0, horizon=horizon))

    mean_pos = float(np.mean(res_pos["point_forecast"]))
    mean_neg = float(np.mean(res_neg["point_forecast"]))
    assert mean_pos > mean_neg, (
        f"sentiment=+1 点预测均值应高于 -1: pos={mean_pos} neg={mean_neg}"
    )


@pytest.mark.unit
def test_news_sentiment_clamped_to_range():
    """T-PRED-08：news_sentiment 超界被钳制到 [-1,1]，极端值不崩。"""
    from graph.prediction.engine import forecast

    bars = _make_bars(n=60)
    # 超大正/负值都应被钳制，不抛异常
    np.random.seed(1)
    res = asyncio.run(forecast(bars, news_sentiment=999.0, horizon=5))
    assert len(res["point_forecast"]) == 5
    np.random.seed(1)
    res2 = asyncio.run(forecast(bars, news_sentiment=-999.0, horizon=5))
    assert len(res2["point_forecast"]) == 5


@pytest.mark.unit
def test_forecast_empty_bars_returns_default():
    """T-PRED-09：空 bars_df → 默认 ForecastResult（model_name 占位、列表为空）。"""
    from graph.prediction.engine import forecast

    res = asyncio.run(forecast(pd.DataFrame(), news_sentiment=0.0, horizon=5))
    assert res["model_name"] == MODEL_NAME
    assert res["point_forecast"] == []
    assert res["dates"] == []
    assert res["confidence"] == 0.0


@pytest.mark.unit
def test_forecast_all_nan_close_returns_default():
    """T-PRED-10：close 全 NaN → 降级返回默认 ForecastResult。"""
    from graph.prediction.engine import forecast

    bars = pd.DataFrame({
        "date": ["2026-01-01", "2026-01-02"],
        "open": [10, 10], "high": [10, 10], "low": [10, 10],
        "close": [None, None], "volume": [0, 0],
    })
    res = asyncio.run(forecast(bars, news_sentiment=0.0, horizon=5))
    assert res["model_name"] == MODEL_NAME
    assert res["point_forecast"] == []


@pytest.mark.unit
def test_compute_all_returns_all_indicators():
    """T-PRED-11：compute_all 快照含全部指标 key 且 last_close 正确。"""
    from factors.prediction_indicators import compute_all

    bars = _make_bars(n=60)
    ti = compute_all(bars)
    expected_keys = {
        "ma5", "ma10", "ma20", "ema12", "ema26",
        "macd", "rsi14", "boll", "kdj", "last_close",
    }
    assert expected_keys.issubset(ti.keys()), f"缺 key: {expected_keys - set(ti.keys())}"
    last_close = float(bars["close"].iloc[-1])
    assert ti["last_close"] == pytest.approx(last_close)
    # MA5 应有限
    assert np.isfinite(ti["ma5"])
    assert np.isfinite(ti["ma20"])


@pytest.mark.unit
def test_compute_all_empty_df_returns_nan_snapshot():
    """T-PRED-12：compute_all 空 df → 全 NaN 指标快照（不抛异常）。"""
    from factors.prediction_indicators import compute_all

    ti = compute_all(pd.DataFrame())
    assert np.isnan(ti["rsi14"])
    assert np.isnan(ti["last_close"])
    assert np.isnan(ti["macd"]["dif"])


@pytest.mark.unit
def test_inflection_points_detects_reversal():
    """T-PRED-13：_inflection_points 在先升后降序列里识别 peak 反转点。"""
    from graph.prediction.engine import _inflection_points

    dates = ["d1", "d2", "d3", "d4", "d5"]
    # 先升后降 → d3/d4 附近应有 peak
    p50 = [10.0, 11.0, 12.0, 11.0, 10.0]
    pts = _inflection_points(dates, p50)
    assert len(pts) >= 1
    peak = next(p for p in pts if p["type"] == "peak")
    assert peak["price"] == pytest.approx(12.0)
    assert peak["date"] == "d3"


@pytest.mark.unit
def test_naive_drift_fallback_path():
    """T-PRED-14：历史不足 30 行时 _arima_forecast 走 naive drift 降级（不依赖 statsmodels）。"""
    from graph.prediction.engine import _naive_drift

    s = pd.Series([10.0 * (1.0 + 0.001) ** i for i in range(20)])
    last_close = float(s.iloc[-1])
    path, ann = _naive_drift(s, horizon=5, last_close=last_close)
    assert len(path) == 5
    assert all(p > last_close for p in path)  # 正漂移序列应单调上升
    assert np.isfinite(ann)


@pytest.mark.unit
def test_hist_std_vol_fallback():
    """T-PRED-15：_hist_std_vol 降级返回正 sigma 与 horizon 长度方差路径。"""
    from graph.prediction.engine import _hist_std_vol

    rets = pd.Series([0.01, -0.005, 0.02, -0.01, 0.015] * 6)
    sigma, var_path = _hist_std_vol(rets, horizon=5)
    assert sigma > 0
    assert len(var_path) == 5
    assert all(v > 0 for v in var_path)


@pytest.mark.unit
def test_monte_carlo_quantile_ordering():
    """T-PRED-16：_monte_carlo 直接验证 p10<=p50<=p90 且长度=horizon。"""
    from graph.prediction.engine import _monte_carlo

    np.random.seed(99)
    out = _monte_carlo(
        last_close=100.0,
        drift_mu_annual=0.10,
        sigma_annual=0.30,
        horizon=5,
        n_paths=500,
        percentiles=(10, 50, 90),
    )
    p10, p50, p90 = out["p10"], out["p50"], out["p90"]
    assert len(p10) == len(p50) == len(p90) == 5
    for i in range(5):
        assert p10[i] <= p50[i] + 1e-9
        assert p50[i] <= p90[i] + 1e-9


# ══════════════════════════════════════════════════════════════════════════════
# v2 新增：ModelParams / 事件分双通道 / 7 根 OHLC 蜡烛 / on_step 流式推导
#
#   T-PRED-V2-01  ModelParams 真带回 ARIMA order/AIC/网格 + GARCH ω/α/β（v1 全丢弃）
#   T-PRED-V2-02  GARCH 平稳性：α+β < 1，且 persistence == α+β
#   T-PRED-V2-03  ★ 事件分双通道：给定 F<0 → μ 被压低 **且** σ 被抬高
#   T-PRED-V2-04  无公告 → F=0、M_event=1（不能凭空扣分）
#   T-PRED-V2-05  失败的维度不当「无坏消息」（整维跳过，不参与事件分）
#   T-PRED-V2-06  7 根 OHLC 蜡烛不变式：h≥max(o,c)、l≤min(o,c)、l>0、p10≤p50≤p90
#   T-PRED-V2-07  on_step 按序推 FORMULA_STEP（全部）→ FORECAST_DAY（day 1..7）
#   T-PRED-V2-08  FORMULA_STEP payload 必须带 stage（= key）——落库去重键，缺了会清空整列
#   T-PRED-V2-09  on_step 回调抛异常 → 只降级不中断计算（铁律 #9）
#
# 全部合成数据，零真实行情。
# ══════════════════════════════════════════════════════════════════════════════

_TODAY = pd.Timestamp.now().normalize()


def _filing_digest(*, negatives=0, reductions=0, days_ago: int = 1) -> list[dict]:
    """构造 filing_digest（FilingDimensionResult dict 列表）。"""
    d = (_TODAY - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    neg_items = [
        {
            "title": f"关于公司被立案调查的公告（{i}）", "date": d, "ann_type": "违法违规",
            "url": "", "event_type": "negative", "negative_score": -0.9,
            "reason": "立案调查", "source": "eastmoney_notice", "confidence": "llm_inferred",
            "dilution_ratio": None, "discount_rate": None, "reduction_ratio": None,
        }
        for i in range(negatives)
    ]
    red_items = [
        {
            "title": f"关于股东减持股份的公告（{i}）", "date": d, "ann_type": "股东减持",
            "url": "", "event_type": "reduction", "negative_score": -0.3,
            "reason": "减持 2%", "source": "eastmoney_ggcg", "confidence": "structured",
            "dilution_ratio": None, "discount_rate": None, "reduction_ratio": 0.02,
        }
        for i in range(reductions)
    ]
    digest = []
    if neg_items:
        digest.append({
            "dimension": "negative_event", "kind": "data", "items": neg_items,
            "score": -0.5, "summary": "", "failed": False,
        })
    if red_items:
        digest.append({
            "dimension": "stake_reduction", "kind": "data", "items": red_items,
            "score": -0.4, "summary": "", "failed": False,
        })
    return digest


def _run_forecast(**kw):
    np.random.seed(7)
    bars = kw.pop("bars", None)
    if bars is None:
        bars = _make_bars(n=90)
    return asyncio.run(forecast_fn(bars, **kw))


from graph.prediction.engine import forecast as forecast_fn  # noqa: E402


# ── T-PRED-V2-01 / 02：ModelParams ───────────────────────────────────────────

@pytest.mark.unit
def test_model_params_carries_arima_and_garch():
    """T-PRED-V2-01：v1 把 ARIMA order/AIC、GARCH ω/α/β 丢在局部变量里，v2 必须全链路带出来。"""
    res = _run_forecast(news_sentiment=0.0, horizon=7)

    mp = res["model_params"]
    assert mp, "model_params 缺失 —— 前端公式卡片会整块空白"

    # ARIMA
    assert len(tuple(mp["arima_order"])) == 3, "arima_order 应为 (p,d,q)"
    assert all(isinstance(int(x), int) for x in mp["arima_order"])
    assert np.isfinite(mp["arima_aic"]) and mp["arima_aic"] != 0.0, "AIC 未回传（v1 的老毛病）"
    assert mp["arima_grid_tried"], "网格搜索过程未回传（公式卡片要展示试了哪些阶数）"
    assert all({"order", "aic"} <= set(g) for g in mp["arima_grid_tried"])

    # GARCH —— ★ 必须是真拟合参数，不是 None、不是 0
    assert mp["garch_omega"] > 0.0, "ω 未回传（说明 GARCH 静默降级成历史 std 了）"
    assert mp["garch_alpha"] == pytest.approx(_FAKE_ALPHA)
    assert mp["garch_beta"] == pytest.approx(_FAKE_BETA)
    # ω 从「百分数收益率方差」换算回小数收益率方差：÷100²
    assert mp["garch_omega"] == pytest.approx(_FAKE_OMEGA_PCT / 10_000.0)
    assert mp["garch_sigma_annual"] == pytest.approx(_FIXED_SIGMA_ANNUAL, rel=1e-6)

    # 蒙特卡洛 / 影线标定设定（UI 页脚要展示）
    assert mp["mc_n_paths"] > 0
    assert len(tuple(mp["mc_percentiles"])) >= 3
    assert mp["mc_dt"] > 0
    assert mp["shadow_lookback_bars"] > 0
    assert 0.0 <= mp["shadow_k_dn"] < 1.0, "k_dn ≥ 1 会让预测蜡烛下影线跌破 0"


@pytest.mark.unit
def test_garch_persistence_below_one():
    """T-PRED-V2-02：α+β < 1 才平稳（≥1 = 波动无限累积，UI 必须提示）。"""
    res = _run_forecast(news_sentiment=0.0, horizon=7)
    mp = res["model_params"]

    assert mp["garch_persistence"] == pytest.approx(mp["garch_alpha"] + mp["garch_beta"])
    assert mp["garch_persistence"] < 1.0, (
        f"GARCH 非平稳 α+β={mp['garch_persistence']}"
    )


# ── T-PRED-V2-03 / 04 / 05：事件分双通道 ─────────────────────────────────────

@pytest.mark.unit
def test_event_score_lowers_drift_and_raises_vol():
    """★ T-PRED-V2-03：公告事件走**双通道** —— 既压低漂移 μ，又抬高波动 σ。

    只压 μ 不抬 σ 会得出「稳稳地跌」的荒谬结论：爆雷的真实后果是
    「往下偏 + 抖得更厉害」。这条断言是 §5.3 的核心，也是 v2 相对 v1 的主要增量。
    """
    base = _run_forecast(news_sentiment=0.0, horizon=7, filing_digest=None)
    hit = _run_forecast(
        news_sentiment=0.0, horizon=7,
        filing_digest=_filing_digest(negatives=3, reductions=2),
    )

    b, h = base["model_params"], hit["model_params"]

    # 事件分本身
    assert b["filing_score"] == 0.0, "无公告时 F 必须为 0"
    assert h["filing_score"] < 0.0, "有爆雷+减持时 F 必须为负"
    assert -1.0 <= h["filing_score"] <= 0.0, "F 必须落在 [-1, 0]（饱和函数保证）"
    assert h["event_scores"]["negative"] < 0.0
    assert h["event_scores"]["reduction"] < 0.0
    assert h["event_weights"], "权重要回传给公式卡片"

    # 通道 1：漂移被压低
    assert h["sentiment_total"] < b["sentiment_total"], "S_total 未被事件分拉低"
    assert h["drift_mu_annual_final"] < b["drift_mu_annual_final"], (
        "★ 事件分没有压低漂移 μ —— 爆雷预测出上涨"
    )

    # 通道 2：波动被抬高
    assert h["vol_multiplier_event"] > 1.0, "M_event 未被事件分抬高"
    assert b["vol_multiplier_event"] == 1.0
    assert h["sigma_annual_final"] > b["sigma_annual_final"], (
        "★ 事件分没有抬高波动 σ —— 会得出「稳稳地跌」的假精确结论"
    )

    # 两条通道的 GARCH/ARIMA 底座没变（差异只来自事件分）
    assert h["garch_sigma_annual"] == pytest.approx(b["garch_sigma_annual"])
    assert h["arima_drift_annual"] == pytest.approx(b["arima_drift_annual"])


@pytest.mark.unit
def test_no_filings_means_no_event_penalty():
    """T-PRED-V2-04：没有公告 ≠ 扣分。F=0、M_event=1，σ/μ 与纯资讯路径一致。"""
    res = _run_forecast(news_sentiment=0.0, horizon=7, filing_digest=[])
    mp = res["model_params"]

    assert mp["filing_score"] == 0.0
    assert mp["vol_multiplier_event"] == 1.0
    # 通道清单跟着 engine._KAPPA 走（negative/placement/reduction/lockup），别写死条数 ——
    # 每加一个事件通道都必须在这里如实透出，否则公式卡片会漏掉一项实际参与计算的因子。
    from graph.prediction.engine import _KAPPA
    assert set(mp["event_scores"]) == set(_KAPPA)
    assert all(v == 0.0 for v in mp["event_scores"].values()), "没有公告 ≠ 扣分"


@pytest.mark.unit
def test_failed_dimension_is_not_treated_as_clean():
    """T-PRED-V2-05：取数失败的维度整维跳过 —— 绝不能把「没拿到数据」算成「没有坏消息」。"""
    failed_digest = [{
        "dimension": "negative_event", "kind": "data",
        "items": [], "score": -0.9,          # 分数是脏的，但维度 failed → 必须整维忽略
        "failed": True, "error": "akshare 429",
    }]
    res = _run_forecast(news_sentiment=0.0, horizon=7, filing_digest=failed_digest)

    assert res["model_params"]["filing_score"] == 0.0, (
        "failed 维度的残留 score 被计进了事件分"
    )


# ── T-PRED-V2-06：7 根 OHLC 蜡烛不变式 ───────────────────────────────────────

@pytest.mark.unit
def test_seven_candles_ohlc_invariants():
    """T-PRED-V2-06：horizon=7（用户拍板）→ 7 根蜡烛，且 OHLC 不变式恒成立。

    前端 ECharts 画 K 线直接依赖这些不变式；h<max(o,c) 会画出「上影线朝下」的鬼图。
    """
    res = _run_forecast(news_sentiment=0.0, horizon=7)

    candles = res["forecast_candles"]
    assert len(candles) == 7, f"应有 7 根预测蜡烛，实得 {len(candles)}"
    assert len(res["dates"]) == 7 == len(res["p50"])

    for i, c in enumerate(candles):
        assert c["day"] == i + 1, "day 必须 1..7 连续（前端按 day-1 覆盖，不 push）"
        assert c["date"] == res["dates"][i], "蜡烛日期与 forecast.dates 必须对齐"

        assert c["h"] >= max(c["o"], c["c"]) - 1e-9, f"day{c['day']}: 最高价低于开/收盘"
        assert c["l"] <= min(c["o"], c["c"]) + 1e-9, f"day{c['day']}: 最低价高于开/收盘"
        assert c["l"] > 0, f"day{c['day']}: 最低价 ≤ 0（k_dn 失控）"

        assert c["p10"] <= c["p50"] + 1e-9 <= c["p90"] + 2e-9, f"day{c['day']}: 分位数逆序"
        assert c["p50"] == pytest.approx(res["p50"][i]), "蜡烛 p50 与 forecast.p50 不一致"

    # point_forecast 就是 p50（前端画中枢线）
    assert res["point_forecast"] == res["p50"]


# ── T-PRED-V2-07 / 08 / 09：on_step 流式推导 ─────────────────────────────────

@pytest.mark.unit
def test_on_step_streams_formula_steps_then_candles():
    """T-PRED-V2-07：先推完所有 FORMULA_STEP，再逐根推 FORECAST_DAY（day 1..7）。"""
    seen: list[dict] = []

    async def _on_step(payload):
        seen.append(payload)

    np.random.seed(7)
    res = asyncio.run(forecast_fn(
        _make_bars(n=90), news_sentiment=-0.2, horizon=7,
        filing_digest=_filing_digest(negatives=2),
        on_step=_on_step,
    ))

    # 铁律 #3：事件类型取自 SSEEventType 枚举，不是字面量
    types_seen = {p["type"] for p in seen}
    assert types_seen <= {
        SSEEventType.FORMULA_STEP.value, SSEEventType.FORECAST_DAY.value,
    }, f"推了未知事件类型: {types_seen}"

    steps = [p for p in seen if p["type"] == SSEEventType.FORMULA_STEP.value]
    candles = [p for p in seen if p["type"] == SSEEventType.FORECAST_DAY.value]

    # 顺序：所有公式步骤都在第一根蜡烛之前（前端先铺公式卡片，再让蜡烛一根根长出来）
    first_candle_at = seen.index(candles[0])
    last_step_at = seen.index(steps[-1])
    assert last_step_at < first_candle_at, "公式步骤与蜡烛交错推送，前端卡片会乱序弹出"

    # 推导链完整（§5.2 的 8 步）
    keys = [s["key"] for s in steps]
    assert keys == ["technical", "arima", "garch", "event_score", "sentiment_total",
                    "gbm", "shadow"], f"公式推导链不完整/乱序: {keys}"

    # 蜡烛 day 严格 1..7 递增
    assert [c["day"] for c in candles] == [1, 2, 3, 4, 5, 6, 7]

    # 流式推出去的 == 最终落库的（刷新重建不能少东西）
    assert len(res["formula_steps"]) == len(steps)
    assert len(res["forecast_candles"]) == len(candles)
    assert [s["key"] for s in res["formula_steps"]] == keys


@pytest.mark.unit
def test_formula_step_payload_carries_stage_dedupe_key():
    """T-PRED-V2-08：FORMULA_STEP 必须带 stage（= key）。

    append_prediction_formula_step 以 "stage" 为去重键，SQL 里
    `e->>'stage' IS DISTINCT FROM NULL` —— payload 缺这个字段会把**之前所有公式步骤全删掉**。
    """
    seen: list[dict] = []

    async def _on_step(p):
        seen.append(p)

    np.random.seed(7)
    asyncio.run(forecast_fn(
        _make_bars(n=90), news_sentiment=0.0, horizon=3, on_step=_on_step,
    ))

    steps = [p for p in seen if p["type"] == SSEEventType.FORMULA_STEP.value]
    for s in steps:
        assert s.get("stage") == s["key"], f"公式步骤 {s['key']} 缺 stage 去重键"

    # FORECAST_DAY 的去重键是 date（ForecastCandle 自带）
    candles = [p for p in seen if p["type"] == SSEEventType.FORECAST_DAY.value]
    assert all(c.get("date") for c in candles), "蜡烛缺 date（落库去重键）"


@pytest.mark.unit
def test_on_step_exception_does_not_break_forecast():
    """T-PRED-V2-09：回调是调用方的代码 —— 它炸了只能降级（warning），不能带崩整个预测。"""
    calls = {"n": 0}

    async def _boom(_payload):
        calls["n"] += 1
        raise RuntimeError("SSE 队列已关闭")

    np.random.seed(7)
    res = asyncio.run(forecast_fn(
        _make_bars(n=90), news_sentiment=0.0, horizon=7, on_step=_boom,
    ))

    assert calls["n"] > 0, "回调根本没被调用，这条用例没测到东西"
    assert len(res["forecast_candles"]) == 7, "回调抛异常导致预测结果不完整"
    assert res["model_params"]["last_close"] > 0
    assert res["model_name"] == MODEL_NAME
