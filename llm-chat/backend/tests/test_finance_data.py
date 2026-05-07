"""finance 数据层 — 单元测试

验证 quant/data/cache.py 的写穿透 / 命中 / Redis 不可用降级逻辑，
以及各数据 helper 的归一化、空数据兜底（不真连 akshare）。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from quant.data import cache as cache_mod


# ── 缓存层 ────────────────────────────────────────────────────────────────────

class _FakeRedis:
    def __init__(self, broken: bool = False):
        self._store: dict[str, str] = {}
        self.broken = broken
        self.get_calls = 0
        self.set_calls = 0

    async def get(self, key):
        self.get_calls += 1
        if self.broken:
            raise RuntimeError("redis down")
        return self._store.get(key)

    async def set(self, key, value, ex=None):
        self.set_calls += 1
        if self.broken:
            raise RuntimeError("redis down")
        self._store[key] = value


@pytest.fixture
def _patch_redis(monkeypatch):
    """注入 fake redis；返回 holder，方便测试切换 broken。"""
    holder: dict[str, _FakeRedis] = {"r": _FakeRedis()}

    def _get():
        return holder["r"]

    monkeypatch.setattr("db.redis_state._get_redis", _get)
    return holder


@pytest.mark.unit
def test_cache_get_or_fetch_miss_then_hit(_patch_redis):
    """T-FINANCE-DATA-01：未命中 → 调 fetcher → 写回；二次命中 → 不再调 fetcher。"""
    fetch_count = 0

    async def fetcher():
        nonlocal fetch_count
        fetch_count += 1
        return [{"k": "v"}]

    async def _run():
        v1 = await cache_mod.get_or_fetch("test", "k1", fetcher, ttl_seconds=60)
        v2 = await cache_mod.get_or_fetch("test", "k1", fetcher, ttl_seconds=60)
        return v1, v2

    v1, v2 = asyncio.run(_run())
    assert v1 == v2 == [{"k": "v"}]
    assert fetch_count == 1, "缓存命中时不该再调 fetcher"


@pytest.mark.unit
def test_cache_redis_unavailable_fallthrough(_patch_redis):
    """T-FINANCE-DATA-02：Redis 全程报错 → 直连 fetcher，不阻断。"""
    _patch_redis["r"] = _FakeRedis(broken=True)
    fetch_count = 0

    async def fetcher():
        nonlocal fetch_count
        fetch_count += 1
        return {"x": 1}

    async def _run():
        v1 = await cache_mod.get_or_fetch("ns", "k", fetcher, ttl_seconds=10)
        v2 = await cache_mod.get_or_fetch("ns", "k", fetcher, ttl_seconds=10)
        return v1, v2

    v1, v2 = asyncio.run(_run())
    assert v1 == v2 == {"x": 1}
    # Redis 不可用 → 每次都调 fetcher（不缓存）
    assert fetch_count == 2


# ── news 归一化 ────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_news_normalize_filters_empty_and_renames():
    """T-FINANCE-DATA-03：news 列名归一化 + 过滤空标题。"""
    import pandas as pd
    from quant.data.news import _normalize_news_df

    df = pd.DataFrame([
        {"新闻标题": "A 利好", "发布时间": "2026-05-07 10:00:00", "来源": "新华社", "新闻链接": "http://a"},
        {"新闻标题": "", "发布时间": "2026-05-07 09:00:00", "来源": "X", "新闻链接": "http://b"},
        {"新闻标题": "B 中性", "发布时间": "2026-05-06 15:00:00", "来源": "财经网", "新闻链接": "http://c"},
    ])
    out = _normalize_news_df(df, limit=10)
    titles = [r["title"] for r in out]
    assert titles == ["A 利好", "B 中性"]
    assert out[0]["source"] == "新华社"
    assert out[0]["url"] == "http://a"
    assert out[0]["date"].startswith("2026-05-07")


@pytest.mark.unit
def test_money_flow_normalize_picks_today():
    """T-FINANCE-DATA-04：money_flow 取最后一行为 today，history 含近 5 日。"""
    import pandas as pd
    from quant.data.money_flow import _normalize

    df = pd.DataFrame([
        {"日期": f"2026-05-{d:02d}", "主力净流入-净额": d * 10.0, "主力净流入-净占比": d * 0.1}
        for d in range(1, 8)
    ])
    out = _normalize(df)
    assert len(out["history"]) == 5
    # 最新一行是 5 月 7 日（最大日期，akshare 升序）
    assert out["today"]["date"] == "2026-05-07"
    assert out["today"]["main_net_inflow"] == 70.0


@pytest.mark.unit
def test_industry_normalize_sorts_by_change():
    """T-FINANCE-DATA-05：industry 按涨跌幅降序裁剪 top_n。"""
    import pandas as pd
    from quant.data.industry import _normalize

    df = pd.DataFrame([
        {"板块名称": "X", "涨跌幅": -1.0, "成交额": 100, "领涨股": "x1"},
        {"板块名称": "Y", "涨跌幅": 5.0, "成交额": 200, "领涨股": "y1"},
        {"板块名称": "Z", "涨跌幅": 2.0, "成交额": 300, "领涨股": "z1"},
    ])
    out = _normalize(df, top_n=2)
    assert [r["name"] for r in out] == ["Y", "Z"]
    assert out[0]["change_pct"] == 5.0
