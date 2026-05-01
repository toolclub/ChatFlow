"""量化选股 — 单元测试

用一个返回固定数据的 FakeProvider 驱动 QuantScreeningService，
验证：
  1. 整个 pipeline 跑通（universe → 过滤 → 因子 → 排序）
  2. 综合分按权重合成
  3. provider_trace 记录了所有调用
  4. 排序结果稳定（动量正、波动率小、PE 合理的票排在前面）
"""
from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timedelta

import pandas as pd
import pytest

from quant.domain import ProviderCapability, ProviderHealth, ProviderHealthStatus, ScreenCriteria
from quant.provider_registry import ProviderRegistry
from quant.service import QuantScreeningService


# 每个测试用独立的临时目录跑磁盘缓存，避免跨测试 / 跨次运行的污染
@pytest.fixture(autouse=True)
def _isolate_quant_cache(tmp_path, monkeypatch):
    monkeypatch.setattr("quant.cache_disk.QUANT_CACHE_DIR", str(tmp_path / "quant_cache"))
    yield


# ── Fake Provider ────────────────────────────────────────────────────────────

def _spot_fixture() -> pd.DataFrame:
    """5 只股票的快照：A 优、B 中、C 弱、D ST、E 停牌"""
    rows = [
        # symbol, name,  price, pe,   pb,  cap(亿), amount(亿), 60d,    vol,
        ("000001.SZ", "优股A", 12.0, 10.0, 1.2, 800.0, 12.0, 25.0, 1_000_000),
        ("600000.SH", "中股B", 8.0, 18.0, 1.8, 500.0, 6.0, 8.0, 800_000),
        ("300001.SZ", "弱股C", 30.0, 80.0, 5.0, 300.0, 4.0, -5.0, 600_000),
        ("000999.SZ", "ST天成", 3.0, 50.0, 4.0, 80.0, 0.6, -15.0, 200_000),
        ("600999.SH", "停牌E", 5.0, 12.0, 1.0, 200.0, 0.0, 0.0, 0),
    ]
    df = pd.DataFrame(rows, columns=[
        "symbol", "name", "price", "pe", "pb",
        "market_cap", "amount", "change_pct_60d", "volume",
    ])
    df["as_of_date"] = datetime.now().strftime("%Y-%m-%d")
    return df


def _bars_fixture(symbols: list[str]) -> pd.DataFrame:
    """每只股票 70 天日线，价格按预设趋势造"""
    base_trends = {
        "000001.SZ": ("up", 10.0, 0.005),    # 上涨 + 低波动
        "600000.SH": ("flat", 7.5, 0.012),   # 震荡 + 中波动
        "300001.SZ": ("down", 32.0, 0.025),  # 下跌 + 高波动
    }
    end = datetime.now()
    out = []
    for sym in symbols:
        trend = base_trends.get(sym)
        if not trend:
            continue
        direction, base_price, vol = trend
        price = base_price
        for i in range(70):
            day = end - timedelta(days=70 - i)
            if direction == "up":
                price = price * (1 + 0.004)
            elif direction == "down":
                price = price * (1 - 0.003)
            # 加点小噪声，让 std > 0
            jitter = 1 + ((i % 7) - 3) * vol * 0.1
            close = price * jitter
            out.append({
                "symbol": sym,
                "date": day.strftime("%Y-%m-%d"),
                "open": close * 0.99,
                "close": close,
                "high": close * 1.01,
                "low": close * 0.98,
                "volume": 1_000_000,
                "amount": close * 1_000_000,
                "change_pct": 0.4 if direction == "up" else -0.3,
                "amplitude": vol * 100,
                "turnover_rate": 1.5,
            })
    return pd.DataFrame(out)


class FakeProvider:
    name = "fake"
    priority = 1
    capabilities = {
        ProviderCapability.STOCK_LIST,
        ProviderCapability.REALTIME_SNAPSHOT,
        ProviderCapability.DAILY_BARS,
        ProviderCapability.INDEX_WEIGHT,
    }

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(status=ProviderHealthStatus.OK)

    async def list_stocks(self, market: str = "cn_a"):
        return []

    async def realtime_snapshot(self, market: str = "cn_a") -> pd.DataFrame:
        return _spot_fixture()

    async def daily_bars(self, symbols, start, end, adjust="qfq") -> pd.DataFrame:
        return _bars_fixture(symbols)

    async def index_constituents(self, index_code: str) -> list[str]:
        return ["000001.SZ", "600000.SH", "300001.SZ"]


# ── 测试 ────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_screen_pipeline_end_to_end():
    registry = ProviderRegistry()
    registry.register(FakeProvider())
    service = QuantScreeningService(registry=registry)

    result = asyncio.run(service.screen(ScreenCriteria(top_n=10)))

    # 基本结构
    assert result.snapshot_id.startswith("qs_")
    assert result.universe_size == 5
    assert result.weights == {
        "technical": 0.35, "fundamental": 0.35,
        "liquidity": 0.20, "risk": 0.10,
    }
    # provider_trace 应至少包含 spot + bars 两次调用
    caps_called = {t.capability for t in result.provider_trace}
    assert "realtime_snapshot" in caps_called
    assert "daily_bars" in caps_called

    # 默认 exclude_st + exclude_suspended → 至少剔除 D 和 E
    syms = [r.symbol for r in result.rows]
    assert "000999.SZ" not in syms
    assert "600999.SH" not in syms

    # A 应该排第一（动量+ + 波动小 + 估值合理）
    assert result.rows[0].symbol == "000001.SZ"
    assert result.rows[0].rank == 1
    assert 0 <= result.rows[0].total <= 100

    # 每行都有 reasons / raw 字段
    for row in result.rows:
        assert isinstance(row.reasons, list) and row.reasons
        assert "pe" in row.raw or "pb" in row.raw


@pytest.mark.unit
def test_universe_index_constituents():
    registry = ProviderRegistry()
    registry.register(FakeProvider())
    service = QuantScreeningService(registry=registry)

    result = asyncio.run(service.screen(ScreenCriteria(universe="hs300")))
    # FakeProvider.index_constituents 返回 3 只
    assert result.universe_size == 3


@pytest.mark.unit
def test_hard_filters_pe_range():
    registry = ProviderRegistry()
    registry.register(FakeProvider())
    service = QuantScreeningService(registry=registry)

    result = asyncio.run(service.screen(ScreenCriteria(pe_range=(5.0, 20.0))))
    syms = [r.symbol for r in result.rows]
    # PE 80 的 C 应被排除
    assert "300001.SZ" not in syms


@pytest.mark.unit
def test_factor_z_score_handles_empty():
    from quant.factors.scorer import zscore

    s = pd.Series([], dtype=float)
    out = zscore(s)
    assert out.empty


# ── 多 provider 注册 / fallback ─────────────────────────────────────────────

class _RaisingProvider(FakeProvider):
    """模拟 provider 抛错；可指定哪些 capability 抛错"""
    name = "raising"

    def __init__(self, raise_on: set[str], priority: int = 1):
        self.priority = priority
        self._raise_on = raise_on

    async def realtime_snapshot(self, market: str = "cn_a"):
        if "realtime_snapshot" in self._raise_on:
            raise RuntimeError("simulated 503")
        return _spot_fixture()

    async def daily_bars(self, symbols, start, end, adjust="qfq"):
        if "daily_bars" in self._raise_on:
            raise RuntimeError("simulated 503")
        return _bars_fixture(symbols)

    async def index_constituents(self, index_code: str):
        if "index_weight" in self._raise_on:
            raise RuntimeError("simulated 503")
        return ["000001.SZ", "600000.SH", "300001.SZ"]


@pytest.mark.unit
def test_registry_priority_first_provider_wins():
    from quant.provider_registry import ProviderRegistry

    primary = FakeProvider()
    primary.name = "primary"
    primary.priority = 10

    backup = FakeProvider()
    backup.name = "backup"
    backup.priority = 50

    reg = ProviderRegistry()
    reg.register(backup)   # 故意倒着加
    reg.register(primary)

    service = QuantScreeningService(registry=reg)
    result = asyncio.run(service.screen(ScreenCriteria(top_n=3)))

    # 所有 trace 都来自 primary（priority 更小）
    used = {t.provider for t in result.provider_trace}
    assert "primary" in used
    assert "backup" not in used


@pytest.mark.unit
def test_registry_fallback_on_error():
    from quant.provider_registry import ProviderRegistry

    bad = _RaisingProvider(raise_on={"realtime_snapshot"})
    bad.name = "bad"
    bad.priority = 10

    good = FakeProvider()
    good.name = "good"
    good.priority = 50

    reg = ProviderRegistry()
    reg.register(bad)
    reg.register(good)

    service = QuantScreeningService(registry=reg)
    result = asyncio.run(service.screen(ScreenCriteria(top_n=3)))

    # spot 应该 fallback 到 good
    spot_traces = [t for t in result.provider_trace if t.capability == "realtime_snapshot"]
    assert len(spot_traces) == 2
    assert spot_traces[0].provider == "bad" and spot_traces[0].status == "error"
    assert spot_traces[1].provider == "good" and spot_traces[1].status == "fallback"
    # 最终能选出股
    assert len(result.rows) > 0


@pytest.mark.unit
def test_registry_all_fail_raises():
    from quant.provider_registry import NoProviderAvailable, ProviderRegistry

    reg = ProviderRegistry()
    reg.register(_RaisingProvider(raise_on={"realtime_snapshot"}, priority=10))

    service = QuantScreeningService(registry=reg)
    with pytest.raises(NoProviderAvailable):
        asyncio.run(service.screen(ScreenCriteria(top_n=3)))


# ── compute_fundamental_factors 兜底 ─────────────────────────────────────────

@pytest.mark.unit
def test_fundamental_factors_augment_from_bars():
    from quant.factors.fundamental import compute_fundamental_factors

    # spot 缺 PE / PB
    spot = pd.DataFrame({
        "symbol": ["000001.SZ", "600000.SH"],
        "name": ["A", "B"],
    })
    # bars 带 peTTM / pbMRQ
    bars = pd.DataFrame([
        {"symbol": "000001.SZ", "date": "2026-04-29", "close": 10, "peTTM": 12.5, "pbMRQ": 1.4},
        {"symbol": "000001.SZ", "date": "2026-04-30", "close": 10, "peTTM": 13.0, "pbMRQ": 1.5},
        {"symbol": "600000.SH", "date": "2026-04-30", "close": 8, "peTTM": 7.8, "pbMRQ": 0.9},
    ])
    out = compute_fundamental_factors(spot, bars=bars)
    assert out.loc["000001.SZ", "pe"] == pytest.approx(13.0)  # 取最新一天
    assert out.loc["000001.SZ", "pb"] == pytest.approx(1.5)
    assert out.loc["600000.SH", "pe"] == pytest.approx(7.8)


@pytest.mark.unit
def test_fundamental_factors_spot_takes_precedence():
    from quant.factors.fundamental import compute_fundamental_factors

    # spot 已有 PE / PB
    spot = pd.DataFrame({
        "symbol": ["000001.SZ"],
        "pe": [10.0],
        "pb": [1.0],
    })
    # bars 也有，但 spot 不应被覆盖
    bars = pd.DataFrame([
        {"symbol": "000001.SZ", "date": "2026-04-30", "close": 10, "peTTM": 99.9, "pbMRQ": 9.9},
    ])
    out = compute_fundamental_factors(spot, bars=bars)
    assert out.loc["000001.SZ", "pe"] == pytest.approx(10.0)
    assert out.loc["000001.SZ", "pb"] == pytest.approx(1.0)


# ── BaoStock 代码格式 ───────────────────────────────────────────────────────

@pytest.mark.unit
def test_baostock_symbol_conversion():
    from quant.providers.baostock_provider import BaoStockProvider

    assert BaoStockProvider._internal_to_bs("000001.SZ") == "sz.000001"
    assert BaoStockProvider._internal_to_bs("600000.SH") == "sh.600000"
    assert BaoStockProvider._internal_to_bs("430090.BJ") == "bj.430090"
    assert BaoStockProvider._internal_to_bs("invalid") == ""

    assert BaoStockProvider._bs_code_to_internal("sh.600000") == "600000.SH"
    assert BaoStockProvider._bs_code_to_internal("sz.000001") == "000001.SZ"
    assert BaoStockProvider._bs_code_to_internal("bj.430090") == "430090.BJ"
    assert BaoStockProvider._bs_code_to_internal("") == ""
