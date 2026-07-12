"""加固行情客户端（graph/prediction/market_data.py）—— 单元测试

覆盖「雅虎 429 → 行情拉空 → 报『历史行情为空，无法预测』」这条根因链的修复：

  T-PRED-MD-01: 429 限流 → MarketDataError(kind="rate_limited")，且重试 FETCH_RETRY_TIMES 次
  T-PRED-MD-02: DNS 解析失败 / 连接超时 → kind="network"（同样重试）
  T-PRED-MD-03: 拉到空 DataFrame 且无任何证据 → kind="no_data"，且**不重试**（只调 1 次）
  T-PRED-MD-04: provider 兜底 —— 第一个 provider 挂、第二个成功 → fetch_bars 正常返回
  T-PRED-MD-05: 正常路径 → adapter 直接命中，返回非空 DataFrame（date 升序 / close 数值）
  T-PRED-MD-06: 代码格式非法 → kind="bad_symbol"，零网络开销（adapter 一次都不调）
  T-PRED-MD-07: provider 吞异常打的日志行也算证据（429 日志 → rate_limited，不误判成 no_data）
  T-PRED-MD-08: 根数不足 min_bars → kind="no_data" 且不重试（再拉一次也是这么多）
  T-PRED-MD-09: preflight 成功 → {"ok":True,"symbol":归一化后,...}；失败 → 抛 MarketDataError

隔离策略（对齐 test_prediction_news.py）：
  - 无真实网络/DB：monkeypatch market_data 模块级 get_adapter / _provider_chain / _probe。
    _probe 必须桩掉 —— 它是真的 HTTP 请求（雅虎 / 东财），漏桩会让单测依赖外网。
  - asyncio.sleep 置 no-op：否则退避 1s/2s/4s 会让每个重试用例真睡 7 秒。
  - 全 async，用 asyncio.run 驱动（铁律 #5：IO 全在 async def 内）。
运行：pytest tests/test_prediction_market_data.py -m unit -v
"""
from __future__ import annotations

import asyncio

import pandas as pd
import pytest

import graph.prediction.market_data as md


# ════════════════════════════════════════════════════════════════════════════
# 测试替身
# ════════════════════════════════════════════════════════════════════════════

def _bars_df(symbol: str = "MU", rows: int = 60) -> pd.DataFrame:
    """构造 provider 归一化列的日线 DataFrame（symbol/date/OHLCV）。"""
    dates = pd.date_range("2026-01-05", periods=rows, freq="D").strftime("%Y-%m-%d")
    return pd.DataFrame({
        "symbol": [symbol] * rows,
        "date": list(dates),
        "open": [10.0 + i * 0.1 for i in range(rows)],
        "high": [10.5 + i * 0.1 for i in range(rows)],
        "low": [9.5 + i * 0.1 for i in range(rows)],
        "close": [10.2 + i * 0.1 for i in range(rows)],
        "volume": [1000 + i for i in range(rows)],
    })


class _FakeAdapter:
    """替代 quant.data_adapter.get_adapter()。行为可配：返回 df / 返回空 / 抛异常。"""

    def __init__(self, behavior="ok", df: pd.DataFrame | None = None, exc: Exception | None = None):
        self.behavior = behavior          # "ok" | "empty" | "raise"
        self.df = df
        self.exc = exc
        self.calls = 0
        self.captured: list[dict] = []

    async def bars(self, **kwargs) -> pd.DataFrame:
        self.calls += 1
        self.captured.append(kwargs)
        if self.behavior == "raise":
            raise self.exc  # type: ignore[misc]
        if self.behavior == "empty":
            return pd.DataFrame()
        return self.df if self.df is not None else _bars_df()


class _FakeProvider:
    """替代 quant.providers.base.MarketDataProvider（只需 name / daily_bars）。"""

    def __init__(self, name: str, behavior="ok", df: pd.DataFrame | None = None,
                 exc: Exception | None = None):
        self.name = name
        self.priority = 1
        self.behavior = behavior          # "ok" | "empty" | "raise"
        self.df = df
        self.exc = exc
        self.calls = 0

    async def daily_bars(self, symbols, start=None, end=None) -> pd.DataFrame:
        self.calls += 1
        if self.behavior == "raise":
            raise self.exc  # type: ignore[misc]
        if self.behavior == "empty":
            return pd.DataFrame()
        return self.df if self.df is not None else _bars_df(symbols[0])


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """指数退避（1s/2s/4s）不真睡 —— 否则每个重试用例要慢 7 秒。"""
    async def _nosleep(_t):
        return None
    monkeypatch.setattr(md.asyncio, "sleep", _nosleep)


@pytest.fixture(autouse=True)
def _no_provider_chain(monkeypatch):
    """默认无兜底 provider（各用例按需覆盖）。不桩会真去读 registry。"""
    monkeypatch.setattr(md, "_provider_chain", lambda market: [])


@pytest.fixture(autouse=True)
def _no_probe(monkeypatch):
    """_probe 是真 HTTP 请求（雅虎 / 东财）—— 单测必须桩掉，且默认给出最弱的结论
    （no_data），这样"能被诊断出 429/network"的用例证明的是证据链而非探针巧合。"""
    probe_calls: list[tuple[str, str]] = []

    async def _fake_probe(symbol: str, market: str):
        probe_calls.append((symbol, market))
        return md.KIND_NO_DATA, "探针：数据源未返回该标的的 K 线"

    monkeypatch.setattr(md, "_probe", _fake_probe)
    return probe_calls


# ════════════════════════════════════════════════════════════════════════════
# T-PRED-MD-01: 429 限流 → rate_limited + 重试
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_rate_limited_raises_and_retries(monkeypatch):
    """雅虎 429（本次事故的元凶）→ kind=rate_limited，且重试 FETCH_RETRY_TIMES 次。

    断言调用次数 == retry_times + 1（首次 + 3 次重试）：限流是瞬时故障，重试才有意义。
    """
    adapter = _FakeAdapter(
        behavior="raise",
        exc=RuntimeError("YFRateLimitError: Too Many Requests. Rate limited. HTTP 429"),
    )
    monkeypatch.setattr(md, "get_adapter", lambda: adapter)

    with pytest.raises(md.MarketDataError) as ei:
        asyncio.run(md.fetch_bars("MU", "us_stock", 730))

    assert ei.value.kind == md.KIND_RATE_LIMITED, f"应判为限流，实际 {ei.value.kind}"
    # 用户可读的中文文案（会直接进 SSE error 事件 / DB error_message）
    assert "限流" in ei.value.message
    assert "429" in ei.value.message
    # 首次 + FETCH_RETRY_TIMES 次重试
    assert adapter.calls == md.FETCH_RETRY_TIMES + 1, (
        f"限流应重试 {md.FETCH_RETRY_TIMES} 次（共 {md.FETCH_RETRY_TIMES + 1} 次调用），"
        f"实际调用 {adapter.calls} 次"
    )


@pytest.mark.unit
def test_rate_limited_is_retryable_kind():
    """限流 / 网络属可重试；无数据 / 代码错不可重试（重试多少次都一样）。"""
    assert md.KIND_RATE_LIMITED in md.RETRYABLE_KINDS
    assert md.KIND_NETWORK in md.RETRYABLE_KINDS
    assert md.KIND_NO_DATA not in md.RETRYABLE_KINDS
    assert md.KIND_BAD_SYMBOL not in md.RETRYABLE_KINDS


# ════════════════════════════════════════════════════════════════════════════
# T-PRED-MD-02: DNS / 超时 → network
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_dns_failure_maps_to_network(monkeypatch):
    """A股 sina DNS 解析失败 → kind=network（不能笼统报"没数据"）。"""
    adapter = _FakeAdapter(
        behavior="raise",
        exc=OSError(
            "[Errno -3] Temporary failure in name resolution: "
            "vip.stock.finance.sina.com.cn"
        ),
    )
    monkeypatch.setattr(md, "get_adapter", lambda: adapter)

    with pytest.raises(md.MarketDataError) as ei:
        asyncio.run(md.fetch_bars("000001.SZ", "cn_a", 730))

    assert ei.value.kind == md.KIND_NETWORK, f"DNS 失败应判 network，实际 {ei.value.kind}"
    assert "网络" in ei.value.message
    assert adapter.calls == md.FETCH_RETRY_TIMES + 1


@pytest.mark.unit
def test_timeout_maps_to_network(monkeypatch):
    """连接超时 → kind=network。"""
    adapter = _FakeAdapter(behavior="raise", exc=TimeoutError("connection timed out"))
    monkeypatch.setattr(md, "get_adapter", lambda: adapter)

    with pytest.raises(md.MarketDataError) as ei:
        asyncio.run(md.fetch_bars("MU", "us_stock", 730))

    assert ei.value.kind == md.KIND_NETWORK
    assert adapter.calls == md.FETCH_RETRY_TIMES + 1


# ════════════════════════════════════════════════════════════════════════════
# T-PRED-MD-03: 空 DataFrame → no_data，且不重试
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_empty_df_maps_to_no_data_without_retry(monkeypatch, _no_probe):
    """adapter 返回空 df 且无任何异常/日志证据 → 探针给出 no_data → **不重试**。

    这是 429 修复的另一半：no_data 重试 3 次只会白等 7 秒，还额外打 3 遍数据源。
    """
    adapter = _FakeAdapter(behavior="empty")
    monkeypatch.setattr(md, "get_adapter", lambda: adapter)

    with pytest.raises(md.MarketDataError) as ei:
        asyncio.run(md.fetch_bars("MU", "us_stock", 730))

    assert ei.value.kind == md.KIND_NO_DATA
    assert adapter.calls == 1, f"no_data 不可重试，应只调 1 次，实际 {adapter.calls} 次"
    # 诊断走到了探针（没有异常/日志证据时的最后手段）
    assert _no_probe == [("MU", "us_stock")]


# ════════════════════════════════════════════════════════════════════════════
# T-PRED-MD-04: provider 兜底（registry 把"返回空"当成功、不会自动降级）
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_provider_fallback_second_provider_succeeds(monkeypatch):
    """adapter 拉空 → 第一个 provider 挂（429）→ 第二个 provider 成功 → 正常返回。

    这正是"yfinance 被 429 拉空却没降级到 akshare_us"的修复点：registry 认为
    "provider 返回空 DataFrame" == 成功，所以不会 fallback，必须手动逐个兜底。
    """
    adapter = _FakeAdapter(behavior="empty")
    monkeypatch.setattr(md, "get_adapter", lambda: adapter)

    p1 = _FakeProvider("yfinance_us", behavior="raise", exc=RuntimeError("HTTP 429 Too Many Requests"))
    p2 = _FakeProvider("akshare_us", behavior="ok", df=_bars_df("MU", 60))
    monkeypatch.setattr(md, "_provider_chain", lambda market: [p1, p2])

    df = asyncio.run(md.fetch_bars("MU", "us_stock", 730))

    assert not df.empty, "第二个 provider 成功时 fetch_bars 应返回非空数据"
    assert len(df) == 60
    assert p1.calls == 1, "第一个 provider 应被尝试过"
    assert p2.calls == 1, "第一个挂了应降级到第二个 provider"
    assert adapter.calls == 1, "兜底成功后不应再重试 adapter"


@pytest.mark.unit
def test_provider_fallback_skips_empty_provider(monkeypatch):
    """provider 返回空（不是抛异常）也要继续兜底下一个 —— 这是最阴的一种失败形态。"""
    adapter = _FakeAdapter(behavior="empty")
    monkeypatch.setattr(md, "get_adapter", lambda: adapter)

    p1 = _FakeProvider("yfinance_us", behavior="empty")     # 静默返回空
    p2 = _FakeProvider("akshare_us", behavior="ok", df=_bars_df("MU", 45))
    monkeypatch.setattr(md, "_provider_chain", lambda market: [p1, p2])

    df = asyncio.run(md.fetch_bars("MU", "us_stock", 730))

    assert len(df) == 45
    assert p1.calls == 1 and p2.calls == 1


@pytest.mark.unit
def test_all_providers_fail_raises_with_kind(monkeypatch):
    """adapter + 全部 provider 都挂（429）→ 抛 rate_limited（证据来自 provider 异常）。"""
    adapter = _FakeAdapter(behavior="empty")
    monkeypatch.setattr(md, "get_adapter", lambda: adapter)

    p1 = _FakeProvider("yfinance_us", behavior="raise",
                       exc=RuntimeError("YFRateLimitError: rate limit"))
    p2 = _FakeProvider("akshare_us", behavior="raise",
                       exc=RuntimeError("HTTP 429 too many requests"))
    monkeypatch.setattr(md, "_provider_chain", lambda market: [p1, p2])

    with pytest.raises(md.MarketDataError) as ei:
        asyncio.run(md.fetch_bars("MU", "us_stock", 730))

    assert ei.value.kind == md.KIND_RATE_LIMITED
    # 每轮重试都会重新走一遍 provider 链
    assert p1.calls == md.FETCH_RETRY_TIMES + 1
    assert p2.calls == md.FETCH_RETRY_TIMES + 1


# ════════════════════════════════════════════════════════════════════════════
# T-PRED-MD-05: 正常路径
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_happy_path_returns_normalized_bars(monkeypatch):
    """adapter 命中 → 返回非空 DataFrame，date 升序 ISO 字符串、close 数值化。

    下游 compute_node 用 .iloc[-1] 取最新收盘价、engine.forecast 用 close 序列，
    两者都依赖升序 —— 这里把该契约钉住。
    """
    adapter = _FakeAdapter(behavior="ok", df=_bars_df("MU", 501))
    monkeypatch.setattr(md, "get_adapter", lambda: adapter)

    df = asyncio.run(md.fetch_bars("MU", "us_stock", 730))

    assert len(df) == 501
    assert adapter.calls == 1, "正常路径不应有重试开销"
    # 下游依赖的列齐全
    assert {"date", "open", "high", "low", "close", "volume", "symbol"} <= set(df.columns)
    # date 升序（去重后）
    dates = list(df["date"])
    assert dates == sorted(dates), "date 必须升序，否则 forecast 的 close 序列是乱的"
    # close 全数值、无空
    assert df["close"].notna().all()
    assert pd.api.types.is_numeric_dtype(df["close"])
    # adapter 收到的是归一化后的 symbol + 正确的 market
    assert adapter.captured[0]["symbols"] == ["MU"]
    assert adapter.captured[0]["market"] == "us_stock"


@pytest.mark.unit
def test_cn_plain_code_normalized(monkeypatch):
    """A股 6 位裸码归一化：000001 → 000001.SZ（provider 要带后缀）。"""
    adapter = _FakeAdapter(behavior="ok", df=_bars_df("000001.SZ", 60))
    monkeypatch.setattr(md, "get_adapter", lambda: adapter)

    df = asyncio.run(md.fetch_bars("000001", "cn_a", 730))

    assert not df.empty
    assert adapter.captured[0]["symbols"] == ["000001.SZ"], "6 位裸码应归一化为带后缀代码"


# ════════════════════════════════════════════════════════════════════════════
# T-PRED-MD-06: 代码格式非法 → bad_symbol，零网络开销
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.parametrize("symbol,market", [
    ("@@bad@@", "cn_a"),
    ("", "cn_a"),
    ("12345", "cn_a"),        # 位数不对
    ("!!!", "us_stock"),
])
def test_malformed_symbol_is_bad_symbol_without_network(monkeypatch, symbol, market):
    """格式非法的代码在发任何请求之前就判死（实测 0.000s）。"""
    adapter = _FakeAdapter(behavior="ok")
    monkeypatch.setattr(md, "get_adapter", lambda: adapter)

    with pytest.raises(md.MarketDataError) as ei:
        asyncio.run(md.fetch_bars(symbol, market, 730))

    assert ei.value.kind == md.KIND_BAD_SYMBOL
    assert "格式不正确" in ei.value.message
    assert adapter.calls == 0, "格式非法必须零网络开销，不能白打数据源"


@pytest.mark.unit
def test_bad_symbol_message_distinguishes_not_found_from_malformed():
    """「数据源查无此票」不能说成「你格式写错了」—— 对格式合法的 ZZZQQ 建议改格式是误导。"""
    malformed = md._message(md.KIND_BAD_SYMBOL, "@@x@@", "cn_a", malformed=True)
    not_found = md._message(md.KIND_BAD_SYMBOL, "ZZZQQ", "us_stock")
    assert "格式不正确" in malformed
    assert "格式不正确" not in not_found
    assert "查无此标的" in not_found


# ════════════════════════════════════════════════════════════════════════════
# T-PRED-MD-07: provider 吞掉的日志行也算证据
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_provider_swallowed_log_becomes_evidence(monkeypatch, _no_probe):
    """provider 把 429 吞成空 df、只打了一行日志 → 仍要诊断出 rate_limited。

    akshare/yfinance provider 都是 try/except → return 空 DataFrame，异常根本传不出来。
    唯一的证据就是它们吞异常前打的那行日志（market_data._capture_provider_logs 在旁听）。
    没有这条链路，429 会被探针（或缺省）误判成 no_data，用户看到的仍是"没数据"。
    """
    import logging

    provider_logger = logging.getLogger("yfinance")

    class _SwallowingAdapter(_FakeAdapter):
        async def bars(self, **kwargs):
            self.calls += 1
            self.captured.append(kwargs)
            # 复刻 provider 的真实行为：log 一行然后返回空 df（异常被吞）
            provider_logger.error(
                "MU: YFRateLimitError('Too Many Requests. Rate limited. Try after a while.')"
            )
            return pd.DataFrame()

    adapter = _SwallowingAdapter(behavior="empty")
    monkeypatch.setattr(md, "get_adapter", lambda: adapter)

    with pytest.raises(md.MarketDataError) as ei:
        asyncio.run(md.fetch_bars("MU", "us_stock", 730))

    assert ei.value.kind == md.KIND_RATE_LIMITED, (
        "provider 吞掉的 429 日志必须被采信为限流证据，否则会误报成 no_data"
    )
    # 有证据就不该再花一次 HTTP 探针
    assert _no_probe == [], "已有日志证据时不应再发诊断探针"
    assert adapter.calls == md.FETCH_RETRY_TIMES + 1, "限流可重试"


@pytest.mark.unit
def test_sniff_does_not_trust_yfinance_delisted_wording():
    """yfinance 的 "possibly delisted; no timezone found" 在限流时也会打 ——
    刻意不把它当成 not-found 证据，否则会把"雅虎限流"误报成"你的代码写错了"。"""
    assert md._sniff_text("MU: possibly delisted; no timezone found") is None
    # 硬证据（404 / quote not found）才认
    assert md._sniff_text("HTTP Error 404: Quote not found for symbol: ZZZQQ") == md.KIND_BAD_SYMBOL


# ════════════════════════════════════════════════════════════════════════════
# T-PRED-MD-08: 根数不足
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_insufficient_bars_raises_no_data_without_retry(monkeypatch):
    """拿到数据但根数 < min_bars → no_data 且不重试（再拉一次也是这么多根）。"""
    adapter = _FakeAdapter(behavior="ok", df=_bars_df("MU", 5))
    monkeypatch.setattr(md, "get_adapter", lambda: adapter)

    with pytest.raises(md.MarketDataError) as ei:
        asyncio.run(md.fetch_bars("MU", "us_stock", 730))

    assert ei.value.kind == md.KIND_NO_DATA
    assert "5" in ei.value.message and str(md.MIN_BARS_FOR_FORECAST) in ei.value.message
    assert adapter.calls == 1, "根数不足不该重试"


@pytest.mark.unit
def test_min_bars_zero_accepts_short_history(monkeypatch):
    """min_bars=0（preflight 用）→ 短历史也算通过：预检只关心"拉不拉得到"。"""
    adapter = _FakeAdapter(behavior="ok", df=_bars_df("MU", 3))
    monkeypatch.setattr(md, "get_adapter", lambda: adapter)

    df = asyncio.run(md.fetch_bars("MU", "us_stock", 25, min_bars=0))
    assert len(df) == 3


# ════════════════════════════════════════════════════════════════════════════
# T-PRED-MD-09: preflight
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_preflight_ok_returns_summary(monkeypatch):
    """预检成功 → {"ok":True, symbol 归一化, bars, last_close, last_date}。"""
    adapter = _FakeAdapter(
        behavior="ok", df=_bars_df("000001.SZ", md.PREFERRED_BARS_FOR_FORECAST),
    )
    monkeypatch.setattr(md, "get_adapter", lambda: adapter)

    res = asyncio.run(md.preflight("000001", "cn_a"))

    assert res["ok"] is True
    assert res["symbol"] == "000001.SZ", "预检要回写归一化后的代码，供下游 news/compute 复用"
    assert res["market"] == "cn_a"
    assert res["bars"] == md.PREFERRED_BARS_FOR_FORECAST
    assert isinstance(res["last_close"], float)
    assert adapter.calls == 1


@pytest.mark.unit
def test_preflight_rejects_insufficient_history(monkeypatch):
    """★ 回归：根数不够热身 → 预检就要判死，不能拖到 compute。

    真实事故（601288.SH 农业银行）：磁盘缓存只有 28 根，老预检只拉 25 天、且
    min_bars=0（"只关心拉不拉得到"），于是它以 bars=17 通过预检 → 跑完 4 个数据维度
    + 资讯搜索好几分钟 → 才在 compute 里撞上"仅取到 28 根，至少需要 30 根"。
    那正是 v1 花大力气修掉的 fail-slow，绝不能从预检这扇门再溜回来。
    预检必须按 compute 的同一门槛（MIN_BARS_FOR_FORECAST）判死。
    """
    short = md.MIN_BARS_FOR_FORECAST - 1
    adapter = _FakeAdapter(behavior="ok", df=_bars_df("601288.SH", short))
    monkeypatch.setattr(md, "get_adapter", lambda: adapter)
    # 直连兜底也拿不到更多（模拟 provider 链也只有这么点历史）
    monkeypatch.setattr(md, "_provider_chain", lambda market: [])

    with pytest.raises(md.MarketDataError) as ei:
        asyncio.run(md.preflight("601288.SH", "cn_a"))

    assert ei.value.kind == md.KIND_NO_DATA
    assert str(short) in str(ei.value), "错误信息要如实说清只拿到几根、至少需要几根"


@pytest.mark.unit
def test_fetch_bars_backfills_when_cache_range_too_short(monkeypatch):
    """★ 回归：adapter 命中缓存但根数不足 → 必须绕过缓存直连 provider 回源全量区间。

    根因在 quant/data_adapter.py:164-169 —— cover_ratio 只统计"请求的 symbol 在不在
    缓存里"，完全不看缓存覆盖的**日期区间**。磁盘里只有 28 天的票会以 coverage=100%
    命中，把 730 天的请求悄悄缩水成 28 根。老代码只在 adapter 返回**全空**时才降级到
    provider 直连，"非空但太短"直接原样返回 → 601288.SH 永远只有 28 根，无法预测。
    实测修复后：28 → 483 根。
    """
    thin = _bars_df("601288.SH", 28)            # 缓存里只有 28 根
    full = _bars_df("601288.SH", 483)           # provider 直连能回源 483 根
    adapter = _FakeAdapter(behavior="ok", df=thin)
    monkeypatch.setattr(md, "get_adapter", lambda: adapter)

    class _FullProvider:
        name = "tushare"

    async def _fake_provider_bars(provider, symbol, start, end):
        return full

    monkeypatch.setattr(md, "_provider_chain", lambda market: [_FullProvider()])
    monkeypatch.setattr(md, "_provider_bars", _fake_provider_bars)

    df = asyncio.run(md.fetch_bars("601288.SH", "cn_a", 730))

    assert len(df) == 483, "缓存太短时必须回源，不能拿着 28 根就交差"


@pytest.mark.unit
def test_preflight_rate_limited_raises(monkeypatch):
    """预检失败 → 抛 MarketDataError（PreflightNode 据此置 state.error/error_kind）。"""
    adapter = _FakeAdapter(behavior="raise", exc=RuntimeError("HTTP 429 rate limit"))
    monkeypatch.setattr(md, "get_adapter", lambda: adapter)

    with pytest.raises(md.MarketDataError) as ei:
        asyncio.run(md.preflight("MU", "us_stock"))

    assert ei.value.kind == md.KIND_RATE_LIMITED
    # 预检重试次数更少（判死要快）：首次 + PREFLIGHT_RETRY_TIMES
    assert adapter.calls == md.PREFLIGHT_RETRY_TIMES + 1 < md.FETCH_RETRY_TIMES + 1


@pytest.mark.unit
def test_preflight_bad_symbol_zero_network(monkeypatch):
    """预检对格式非法的代码零网络开销直接抛（快速判死的地基）。"""
    adapter = _FakeAdapter(behavior="ok")
    monkeypatch.setattr(md, "get_adapter", lambda: adapter)

    with pytest.raises(md.MarketDataError) as ei:
        asyncio.run(md.preflight("@@bad@@", "cn_a"))

    assert ei.value.kind == md.KIND_BAD_SYMBOL
    assert adapter.calls == 0


@pytest.mark.unit
def test_market_data_error_unknown_kind_falls_back():
    """非法 kind 兜底为 no_data（避免前端拿到未知 kind 无从处置）。"""
    err = md.MarketDataError("garbage_kind", "某种失败")
    assert err.kind == md.KIND_NO_DATA
    assert err.message == "某种失败"
    assert md.ERROR_KINDS == frozenset({"rate_limited", "network", "no_data", "bad_symbol"})
