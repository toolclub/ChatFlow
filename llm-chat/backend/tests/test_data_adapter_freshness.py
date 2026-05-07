"""data_adapter 缓存日期新鲜度判定 + yfinance end 修正 — 单元测试

验证之前的 bug：cover_ratio=100% 时缓存永远不刷，K 线卡在首次拉取那天的数据。
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest


# ── _expected_last_trading_day ────────────────────────────────────────────────

def _utc(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("UTC"))


@pytest.mark.unit
def test_expected_last_trading_day_us_friday_after_close():
    """T-ADAPTER-FRESH-01：周五 NY 收盘后 → 预期最新日 = 周五。"""
    from quant.data_adapter import _expected_last_trading_day
    # 2026-05-08 周五，NY 17:00 EDT = UTC 21:00
    now = _utc(2026, 5, 8, 21, 0)
    assert _expected_last_trading_day("us_stock", now) == date(2026, 5, 8)


@pytest.mark.unit
def test_expected_last_trading_day_us_saturday():
    """周六（任意时间）→ 预期最新日 = 上周五。"""
    from quant.data_adapter import _expected_last_trading_day
    # 2026-05-09 周六，NY 12:00 = UTC 16:00
    now = _utc(2026, 5, 9, 16, 0)
    assert _expected_last_trading_day("us_stock", now) == date(2026, 5, 8)


@pytest.mark.unit
def test_expected_last_trading_day_us_sunday():
    """周日 → 预期最新日 = 上周五（不是周一，因为周一还没收盘）。"""
    from quant.data_adapter import _expected_last_trading_day
    now = _utc(2026, 5, 10, 12, 0)  # NY 周日 08:00
    assert _expected_last_trading_day("us_stock", now) == date(2026, 5, 8)


@pytest.mark.unit
def test_expected_last_trading_day_us_monday_before_close():
    """周一 NY 盘前 → 预期最新日 = 上周五。"""
    from quant.data_adapter import _expected_last_trading_day
    # 2026-05-11 周一 NY 09:00 EDT = UTC 13:00（盘前）
    now = _utc(2026, 5, 11, 13, 0)
    assert _expected_last_trading_day("us_stock", now) == date(2026, 5, 8)


@pytest.mark.unit
def test_expected_last_trading_day_us_monday_after_close():
    """周一 NY 盘后 → 预期最新日 = 周一。"""
    from quant.data_adapter import _expected_last_trading_day
    # 2026-05-11 周一 NY 17:00 EDT = UTC 21:00（盘后）
    now = _utc(2026, 5, 11, 21, 0)
    assert _expected_last_trading_day("us_stock", now) == date(2026, 5, 11)


@pytest.mark.unit
def test_expected_last_trading_day_cn_thursday_evening():
    """周四 CST 22:00（远超 15:30 收盘）→ 预期最新日 = 周四。"""
    from quant.data_adapter import _expected_last_trading_day
    # 2026-05-07 周四 CST 22:00 = UTC 14:00
    now = _utc(2026, 5, 7, 14, 0)
    assert _expected_last_trading_day("cn_a", now) == date(2026, 5, 7)


@pytest.mark.unit
def test_expected_last_trading_day_cn_thursday_morning():
    """周四 CST 09:00（盘前）→ 预期最新日 = 周三。"""
    from quant.data_adapter import _expected_last_trading_day
    # 2026-05-07 周四 CST 09:00 = UTC 01:00
    now = _utc(2026, 5, 7, 1, 0)
    assert _expected_last_trading_day("cn_a", now) == date(2026, 5, 6)


# ── _max_cached_date ──────────────────────────────────────────────────────────

@pytest.mark.unit
def test_max_cached_date_extracts_latest():
    from quant.data_adapter import _max_cached_date
    df = pd.DataFrame([
        {"symbol": "A", "date": "2026-05-04", "close": 1.0},
        {"symbol": "A", "date": "2026-05-06", "close": 2.0},
        {"symbol": "B", "date": "2026-05-05", "close": 3.0},
    ])
    assert _max_cached_date(df) == date(2026, 5, 6)


@pytest.mark.unit
def test_max_cached_date_handles_empty_and_none():
    from quant.data_adapter import _max_cached_date
    assert _max_cached_date(None) is None
    assert _max_cached_date(pd.DataFrame()) is None
    assert _max_cached_date(pd.DataFrame([{"symbol": "A"}])) is None  # 无 date 列


# ── adapter.bars 缓存陈旧时强制回源 ────────────────────────────────────────────

class _FakeRegistry:
    def __init__(self, return_df: pd.DataFrame):
        self._df = return_df
        self.calls: list[dict] = []

    async def call_with_fallback(self, capability, invoker, trace, market=None):
        self.calls.append({"capability": capability, "market": market})
        # 直接返回固定 df；不真调 invoker（invoker 内部会要求 provider.daily_bars 接口）
        return self._df


@pytest.mark.unit
def test_bars_cover_full_but_stale_forces_refetch(monkeypatch, tmp_path):
    """T-ADAPTER-FRESH-08：cover_ratio=100% 但 max_cached < expected → 全量回源。"""
    from quant import cache_disk
    from quant.data_adapter import CachedDataAdapter

    # 隔离磁盘缓存
    monkeypatch.setattr("quant.cache_disk.QUANT_CACHE_DIR", str(tmp_path))

    # 模拟"今天 = 周四盘后" → 预期最新日 2026-05-07
    monkeypatch.setattr(
        "quant.data_adapter._expected_last_trading_day",
        lambda market, now=None: date(2026, 5, 7),
    )

    # 缓存里只有 2026-05-04（陈旧），但标的覆盖率 100%
    stale = pd.DataFrame([
        {"symbol": "AAPL.US", "date": "2026-05-04", "close": 100.0},
        {"symbol": "TSLA.US", "date": "2026-05-04", "close": 200.0},
    ])

    async def _fake_read(market, s, e):
        return stale, None
    monkeypatch.setattr(cache_disk, "read_bars_range", _fake_read)
    async def _fake_read_sym(market, sym, s, e):
        return stale[stale["symbol"] == sym]
    monkeypatch.setattr(cache_disk, "read_bars_for_symbol", _fake_read_sym)

    # provider 回源会返回新数据（含 2026-05-05/06/07）
    fresh = pd.DataFrame([
        {"symbol": s, "date": d, "close": 999.0}
        for s in ("AAPL.US", "TSLA.US")
        for d in ("2026-05-05", "2026-05-06", "2026-05-07")
    ])
    write_calls: list[pd.DataFrame] = []
    async def _fake_write(market, df):
        write_calls.append(df)
    monkeypatch.setattr(cache_disk, "write_bars", _fake_write)

    fake_reg = _FakeRegistry(fresh)
    adapter = CachedDataAdapter(registry=fake_reg)

    result = asyncio.run(adapter.bars(
        symbols=["AAPL.US", "TSLA.US"],
        start="2026-04-01", end="2026-05-07",
        market="us_stock",
    ))

    # 触发了回源（registry.call_with_fallback 被调用）
    assert len(fake_reg.calls) == 1, "陈旧缓存应触发回源"
    # 写盘了新数据
    assert len(write_calls) == 1
    # 返回的 df 含新日期
    dates_returned = set(result["date"].astype(str).tolist())
    assert "2026-05-07" in dates_returned, f"返回缺最新日: {dates_returned}"


@pytest.mark.unit
def test_bars_partial_cover_and_stale_forces_full_refetch(monkeypatch, tmp_path):
    """T-ADAPTER-FRESH-08b：cover_ratio 高（97%）但日期陈旧 → 仍然全量回源（不只拉缺失的）。

    修复 missing=1 + stale 时只拉 1 只导致剩 44 只日期不更新的 bug。
    """
    from quant import cache_disk
    from quant.data_adapter import CachedDataAdapter

    monkeypatch.setattr("quant.cache_disk.QUANT_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "quant.data_adapter._expected_last_trading_day",
        lambda market, now=None: date(2026, 5, 7),
    )

    # 缓存：44 只标的，最新日 2026-05-04（陈旧）
    stale = pd.DataFrame([
        {"symbol": f"S{i}.US", "date": "2026-05-04", "close": 100.0}
        for i in range(44)
    ])
    async def _fake_read(market, s, e):
        return stale, None
    monkeypatch.setattr(cache_disk, "read_bars_range", _fake_read)
    async def _fake_read_sym(market, sym, s, e):
        return stale[stale["symbol"] == sym]
    monkeypatch.setattr(cache_disk, "read_bars_for_symbol", _fake_read_sym)

    fresh = pd.DataFrame([
        {"symbol": s, "date": d, "close": 999.0}
        for s in [f"S{i}.US" for i in range(45)]
        for d in ("2026-05-05", "2026-05-06", "2026-05-07")
    ])
    captured: dict = {}
    async def _fake_write(market, df):
        captured["written"] = df
    monkeypatch.setattr(cache_disk, "write_bars", _fake_write)

    fake_reg = _FakeRegistry(fresh)
    adapter = CachedDataAdapter(registry=fake_reg)

    # 请求 45 只（44 在缓存 + 1 missing）
    requested = [f"S{i}.US" for i in range(45)]
    asyncio.run(adapter.bars(
        symbols=requested,
        start="2026-04-01", end="2026-05-07",
        market="us_stock",
    ))

    # 关键验证：尽管 cover_ratio = 44/45 = 97.7%（>= 80%），
    # 但 stale 应触发全量回源 → registry 收到的请求含全部 45 只标的
    assert len(fake_reg.calls) == 1
    # written df 应含 45 只（不是 1 只）
    assert "written" in captured
    written_syms = set(captured["written"]["symbol"].astype(str).unique())
    assert len(written_syms) == 45, f"陈旧时只回源了 {len(written_syms)} 只，期望 45"


@pytest.mark.unit
def test_bars_per_symbol_freshness_dirty_partial_write(monkeypatch, tmp_path):
    """T-ADAPTER-FRESH-08c：全局 max_date 已到 expected，但请求 symbol 在 expected
    那天覆盖率不足 → 触发回源（修复"5/6 文件只含 PLTR，AAPL 还是 5/5"的脏写）。
    """
    from quant import cache_disk
    from quant.data_adapter import CachedDataAdapter

    monkeypatch.setattr("quant.cache_disk.QUANT_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "quant.data_adapter._expected_last_trading_day",
        lambda market, now=None: date(2026, 5, 6),
    )

    # cache：5/4 含全部股票；5/6 只有 PLTR 一只（部分写盘脏数据）
    requested_syms = ["AAPL.US", "MSFT.US", "NVDA.US", "PLTR.US"]
    cached_df = pd.concat([
        # 5/4 全部股票
        pd.DataFrame([{"symbol": s, "date": "2026-05-04", "close": 100.0} for s in requested_syms]),
        # 5/6 只有 PLTR（脏写场景）
        pd.DataFrame([{"symbol": "PLTR.US", "date": "2026-05-06", "close": 50.0}]),
    ], ignore_index=True)

    async def _fake_read(market, s, e):
        return cached_df, None
    monkeypatch.setattr(cache_disk, "read_bars_range", _fake_read)
    async def _fake_read_sym(market, sym, s, e):
        return cached_df[cached_df["symbol"] == sym]
    monkeypatch.setattr(cache_disk, "read_bars_for_symbol", _fake_read_sym)

    fresh = pd.DataFrame([
        {"symbol": s, "date": d, "close": 999.0}
        for s in requested_syms
        for d in ("2026-05-05", "2026-05-06")
    ])
    written: list = []
    async def _fake_write(market, df):
        written.append(df)
    monkeypatch.setattr(cache_disk, "write_bars", _fake_write)

    fake_reg = _FakeRegistry(fresh)
    adapter = CachedDataAdapter(registry=fake_reg)
    asyncio.run(adapter.bars(
        symbols=requested_syms,
        start="2026-04-01", end="2026-05-06",
        market="us_stock",
    ))

    # 关键：尽管 cached_max_date=5/6（全局新鲜），但 5/6 当天只有 PLTR（25% 覆盖 < 80%）
    # 应触发回源，把所有股票的 5/5、5/6 数据补上
    assert len(fake_reg.calls) == 1, "5/6 文件只含 PLTR 时应触发回源"
    assert len(written) == 1


@pytest.mark.unit
def test_bars_cover_full_and_fresh_uses_cache(monkeypatch, tmp_path):
    """T-ADAPTER-FRESH-09：缓存覆盖 + 日期新鲜 → 命中缓存，不回源。"""
    from quant import cache_disk
    from quant.data_adapter import CachedDataAdapter

    monkeypatch.setattr("quant.cache_disk.QUANT_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "quant.data_adapter._expected_last_trading_day",
        lambda market, now=None: date(2026, 5, 7),
    )

    fresh_cache = pd.DataFrame([
        {"symbol": "AAPL.US", "date": "2026-05-07", "close": 100.0},
        {"symbol": "TSLA.US", "date": "2026-05-07", "close": 200.0},
    ])
    async def _fake_read(market, s, e):
        return fresh_cache, None
    monkeypatch.setattr(cache_disk, "read_bars_range", _fake_read)
    async def _fake_read_sym(market, sym, s, e):
        return fresh_cache[fresh_cache["symbol"] == sym]
    monkeypatch.setattr(cache_disk, "read_bars_for_symbol", _fake_read_sym)

    fake_reg = _FakeRegistry(pd.DataFrame())
    adapter = CachedDataAdapter(registry=fake_reg)

    result = asyncio.run(adapter.bars(
        symbols=["AAPL.US", "TSLA.US"],
        start="2026-04-01", end="2026-05-07",
        market="us_stock",
    ))
    assert len(fake_reg.calls) == 0, "新鲜缓存不该触发回源"
    assert len(result) == 2


# ── yfinance end exclusive 修正 ───────────────────────────────────────────────

@pytest.mark.unit
def test_yfinance_download_bars_passes_end_plus_one(monkeypatch):
    """T-ADAPTER-FRESH-10：_download_bars 把 end +1 天传给 yfinance（绕开 exclusive 坑）。"""
    from quant.providers import yfinance_us_provider as yf_mod

    captured = {}

    class _FakeYF:
        @staticmethod
        def download(tickers, start, end, auto_adjust, threads):
            captured["start"] = start
            captured["end"] = end
            return pd.DataFrame()  # 空，函数走兜底返回

    yf_mod.YFinanceUSProvider._download_bars(
        _FakeYF, ["AAPL"], start="2026-05-01", end="2026-05-07",
    )
    assert captured["start"] == "2026-05-01"
    assert captured["end"] == "2026-05-08", \
        f"yfinance end 应为请求 end +1 天（exclusive 修正），实际 {captured['end']}"


@pytest.mark.unit
def test_write_bars_handles_legacy_multiindex_existing(tmp_path, monkeypatch):
    """T-ADAPTER-FRESH-12：磁盘上历史脏数据（MultiIndex columns）与新单层数据 concat 时不崩。

    防止 `Can only union MultiIndex with MultiIndex or Index of tuples` 写盘失败。
    """
    import pickle
    from quant import cache_disk

    monkeypatch.setattr("quant.cache_disk.QUANT_CACHE_DIR", str(tmp_path))

    # 1. 先伪造一份 MultiIndex columns 的"脏"历史缓存写入磁盘
    bad_existing = pd.DataFrame(
        [[100, 101, 99, 100, 1_000_000, "AAPL", "2026-05-05"]],
        columns=pd.MultiIndex.from_tuples([
            ("open", ""), ("close", ""), ("low", ""), ("high", ""),
            ("volume", ""), ("symbol", ""), ("date", ""),
        ]),
    )
    p = cache_disk.bars_path("us_stock", date(2026, 5, 5))
    p.parent.mkdir(parents=True, exist_ok=True)
    cache_disk._sync_write_df(p, bad_existing)

    # 2. 新拉取的数据是单层 columns（修复后的 yfinance 输出）
    fresh = pd.DataFrame([
        {"symbol": "AAPL", "date": "2026-05-05", "open": 105, "close": 106, "high": 107, "low": 104, "volume": 2_000_000},
        {"symbol": "TSLA", "date": "2026-05-05", "open": 200, "close": 202, "high": 203, "low": 199, "volume": 3_000_000},
    ])

    # 3. write_bars 应该展平历史脏数据后正常合并写盘，不抛异常
    size = asyncio.run(cache_disk.write_bars("us_stock", fresh))
    assert size > 0

    # 4. 读回验证：两条记录共存（按 symbol/date 去重 keep=last，新数据覆盖旧 AAPL）
    written = cache_disk._sync_read_df(p)
    assert written is not None and not written.empty
    assert not isinstance(written.columns, pd.MultiIndex)
    assert {"symbol", "date", "open", "close"}.issubset(set(written.columns))
    aapl_row = written[written["symbol"] == "AAPL"].iloc[0]
    assert aapl_row["close"] == 106  # keep=last 保留新数据


@pytest.mark.unit
def test_yfinance_download_bars_flattens_multiindex_for_single_ticker():
    """T-ADAPTER-FRESH-11：yfinance 单 ticker 返回 MultiIndex columns 时正确展平。

    防止 write_bars `drop_duplicates(subset=["symbol","date"])`
    在 MultiIndex columns 上抛 `Index(['date','symbol'], dtype='str')` 错误。
    """
    from quant.providers import yfinance_us_provider as yf_mod

    # 模拟新版 yfinance 单 ticker 也返回 MultiIndex columns
    dates = pd.to_datetime(["2026-05-05", "2026-05-06", "2026-05-07"])
    multi_cols = pd.MultiIndex.from_product(
        [["Open", "High", "Low", "Close", "Volume"], ["AAPL"]]
    )
    raw = pd.DataFrame(
        [[100, 102, 99, 101, 1_000_000],
         [101, 103, 100, 102, 1_100_000],
         [102, 104, 101, 103, 1_200_000]],
        index=dates,
        columns=multi_cols,
    )
    raw.index.name = "Date"

    class _FakeYF:
        @staticmethod
        def download(tickers, start, end, auto_adjust, threads):
            return raw

    out = yf_mod.YFinanceUSProvider._download_bars(
        _FakeYF, ["AAPL"], start="2026-05-01", end="2026-05-07",
    )
    # 列必须是单层（不能是 MultiIndex）
    assert not isinstance(out.columns, pd.MultiIndex), \
        f"输出 columns 应展平为单层，实际：{out.columns}"
    # 关键列存在
    for col in ("date", "symbol", "open", "close"):
        assert col in out.columns, f"缺列 {col}: 实际 {list(out.columns)}"
    assert (out["symbol"] == "AAPL").all()
    assert len(out) == 3
