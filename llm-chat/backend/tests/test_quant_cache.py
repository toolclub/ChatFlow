"""量化磁盘缓存 / 适配器 — 单元测试

覆盖：
  1. write_spot/read_spot 往返 + 新鲜度判定
  2. write_bars 按 date 拆分写入；read_bars_range 整段读回
  3. CachedDataAdapter 命中缓存时不调 provider
  4. 缺失日期回源 + 自动写盘
  5. prune 按天数和大小清理
  6. _parse_analysis_json 容错
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pandas as pd
import pytest

from quant import cache_disk
from quant.data_adapter import CachedDataAdapter
from quant.domain import (
    ProviderCapability,
    ProviderHealth,
    ProviderHealthStatus,
)
from quant.provider_registry import ProviderRegistry


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr("quant.cache_disk.QUANT_CACHE_DIR", str(tmp_path / "qc"))
    yield


# ── cache_disk ──────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_spot_roundtrip():
    df = pd.DataFrame({"symbol": ["A", "B", "C"], "price": [1.0, 2.0, 3.0]})
    asyncio.run(cache_disk.write_spot("cn_a", df))
    out = asyncio.run(cache_disk.read_spot("cn_a"))
    assert out is not None
    assert len(out) == 3
    assert list(out["symbol"]) == ["A", "B", "C"]


@pytest.mark.unit
def test_spot_freshness_window():
    df = pd.DataFrame({"symbol": ["A"], "price": [1.0]})
    asyncio.run(cache_disk.write_spot("cn_a", df))
    # fresh 默认 600 秒，刚写入肯定 fresh
    assert asyncio.run(cache_disk.is_spot_fresh("cn_a", fresh_seconds=600))
    # 1 秒窗口必然过期（写入到调用 fresh 至少要 ms 级延迟，但本机即时）
    assert not asyncio.run(cache_disk.is_spot_fresh("cn_a", fresh_seconds=0))


@pytest.mark.unit
def test_bars_split_and_range_read():
    rows = []
    for d_offset in range(3):
        d = (date.today() - timedelta(days=d_offset)).isoformat()
        for sym in ("A", "B"):
            rows.append({"symbol": sym, "date": d, "close": 10.0 + d_offset})
    df = pd.DataFrame(rows)
    asyncio.run(cache_disk.write_bars("cn_a", df))

    cached, missing = asyncio.run(cache_disk.read_bars_range(
        "cn_a", date.today() - timedelta(days=2), date.today(),
    ))
    assert cached is not None
    assert len(cached) == 6
    assert set(cached["symbol"]) == {"A", "B"}


@pytest.mark.unit
def test_index_roundtrip():
    asyncio.run(cache_disk.write_index("hs300", ["000001.SZ", "600000.SH"]))
    out = asyncio.run(cache_disk.read_index("hs300"))
    assert out == ["000001.SZ", "600000.SH"]


@pytest.mark.unit
def test_meta_update():
    asyncio.run(cache_disk.update_meta({"k": 1}))
    asyncio.run(cache_disk.update_meta({"k2": "v"}))
    m = asyncio.run(cache_disk.read_meta())
    assert m["k"] == 1 and m["k2"] == "v"


@pytest.mark.unit
def test_prune_by_age():
    # 写一份很老的 spot
    old_day = date.today() - timedelta(days=20)
    df = pd.DataFrame({"symbol": ["X"], "price": [1.0]})
    asyncio.run(cache_disk.write_spot("cn_a", df, day=old_day))
    asyncio.run(cache_disk.write_spot("cn_a", df, day=date.today()))

    stats = asyncio.run(cache_disk.prune(retention_days=90))
    # 老 spot（>7天）应该被删，新 spot 应保留
    assert any("cn_a_" in name for name in stats["deleted"])
    fresh = asyncio.run(cache_disk.read_spot("cn_a", day=date.today()))
    assert fresh is not None


# ── CachedDataAdapter ───────────────────────────────────────────────────────

class _CallCountingProvider:
    name = "fake"
    priority = 10
    capabilities = {
        ProviderCapability.REALTIME_SNAPSHOT,
        ProviderCapability.DAILY_BARS,
        ProviderCapability.INDEX_WEIGHT,
    }

    def __init__(self) -> None:
        self.spot_calls = 0
        self.bars_calls = 0
        self.index_calls = 0

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(status=ProviderHealthStatus.OK)

    async def realtime_snapshot(self, market: str = "cn_a") -> pd.DataFrame:
        self.spot_calls += 1
        return pd.DataFrame({"symbol": ["A", "B"], "price": [10.0, 20.0]})

    async def daily_bars(self, symbols, start, end, **_):
        self.bars_calls += 1
        rows = []
        for sym in symbols:
            for d_offset in range(3):
                d = (date.today() - timedelta(days=d_offset)).isoformat()
                rows.append({"symbol": sym, "date": d, "close": 10.0 + d_offset})
        return pd.DataFrame(rows)

    async def index_constituents(self, index_code: str) -> list[str]:
        self.index_calls += 1
        return ["A", "B"]


@pytest.mark.unit
def test_adapter_spot_cache_hit():
    p = _CallCountingProvider()
    reg = ProviderRegistry()
    reg.register(p)
    a = CachedDataAdapter(registry=reg)

    # 第一次：miss → provider 调一次
    df1 = asyncio.run(a.spot("cn_a"))
    assert len(df1) == 2
    assert p.spot_calls == 1

    # 第二次：hit disk_cache → 不再调 provider
    df2 = asyncio.run(a.spot("cn_a"))
    assert len(df2) == 2
    assert p.spot_calls == 1


@pytest.mark.unit
def test_adapter_bars_cache_hit():
    p = _CallCountingProvider()
    reg = ProviderRegistry()
    reg.register(p)
    a = CachedDataAdapter(registry=reg)

    start = date.today() - timedelta(days=2)
    end = date.today()

    df1 = asyncio.run(a.bars(["A", "B"], start, end))
    assert not df1.empty
    assert p.bars_calls == 1

    # 同区间同 symbol → 命中缓存
    df2 = asyncio.run(a.bars(["A", "B"], start, end))
    assert not df2.empty
    assert p.bars_calls == 1


@pytest.mark.unit
def test_adapter_index_cache_hit():
    p = _CallCountingProvider()
    reg = ProviderRegistry()
    reg.register(p)
    a = CachedDataAdapter(registry=reg)

    syms1 = asyncio.run(a.index_constituents("hs300"))
    assert syms1 == ["A", "B"]
    assert p.index_calls == 1

    syms2 = asyncio.run(a.index_constituents("hs300"))
    assert syms2 == ["A", "B"]
    assert p.index_calls == 1


# ── analysis json parsing ───────────────────────────────────────────────────

@pytest.mark.unit
def test_parse_analysis_json_clean():
    from graph.quant_agent import _parse_analysis_json
    a, r = _parse_analysis_json('{"analysis":"AAA","risk_notes":["r1","r2"]}')
    assert a == "AAA" and r == ["r1", "r2"]


@pytest.mark.unit
def test_parse_analysis_json_with_prefix_text():
    from graph.quant_agent import _parse_analysis_json
    raw = '一些前置文字{"analysis":"BBB","risk_notes":["x"]}尾巴'
    a, r = _parse_analysis_json(raw)
    assert a == "BBB" and r == ["x"]


@pytest.mark.unit
def test_parse_analysis_json_fallback_plain():
    from graph.quant_agent import _parse_analysis_json
    a, r = _parse_analysis_json("纯文本无结构")
    assert a == "纯文本无结构" and r == []


@pytest.mark.unit
def test_parse_analysis_json_string_risk():
    from graph.quant_agent import _parse_analysis_json
    a, r = _parse_analysis_json('{"analysis":"x","risk_notes":"single"}')
    assert r == ["single"]
