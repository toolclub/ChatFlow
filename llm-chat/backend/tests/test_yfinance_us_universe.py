"""yfinance_us universe 多源 fallback — 单元测试

不真调网络/akshare，只测 fallback 顺序、格式转换、size 截断逻辑。
"""
from __future__ import annotations

import pandas as pd
import pytest


@pytest.mark.unit
def test_universe_falls_back_when_all_sources_fail(monkeypatch):
    """T-YF-UNIV-01：全部源失败 → 内置 45 只 fallback。"""
    from quant.providers import yfinance_us_provider as yf_mod

    monkeypatch.setattr(yf_mod, "_universe_from_akshare", lambda size: [])
    monkeypatch.setattr(yf_mod, "_universe_from_stockanalysis", lambda size: [])
    monkeypatch.setattr(yf_mod, "_universe_from_wikipedia", lambda size: [])

    out = yf_mod.YFinanceUSProvider._get_universe_tickers()
    assert out == yf_mod._BUILTIN_FALLBACK_TICKERS
    assert len(out) == 45


@pytest.mark.unit
def test_universe_uses_first_successful_source(monkeypatch):
    """T-YF-UNIV-02：优先级顺序 akshare → stockanalysis → wikipedia → 内置；
    用第一个返回非空列表的源。"""
    from quant.providers import yfinance_us_provider as yf_mod

    calls = []

    def _ak(size):
        calls.append("ak")
        return ["AAPL", "MSFT"]

    def _sa(size):
        calls.append("sa")
        raise AssertionError("不应触发 stockanalysis（akshare 已成功）")

    monkeypatch.setattr(yf_mod, "_universe_from_akshare", _ak)
    monkeypatch.setattr(yf_mod, "_universe_from_stockanalysis", _sa)

    out = yf_mod.YFinanceUSProvider._get_universe_tickers()
    assert out == ["AAPL", "MSFT"]
    assert calls == ["ak"]


@pytest.mark.unit
def test_universe_falls_through_on_exception(monkeypatch):
    """T-YF-UNIV-03：源 1 抛异常 → fallback 源 2，不崩主流程。"""
    from quant.providers import yfinance_us_provider as yf_mod

    def _ak(size):
        raise RuntimeError("akshare 挂了")

    monkeypatch.setattr(yf_mod, "_universe_from_akshare", _ak)
    monkeypatch.setattr(yf_mod, "_universe_from_stockanalysis", lambda size: ["NVDA", "TSLA"])
    monkeypatch.setattr(yf_mod, "_universe_from_wikipedia", lambda size: [])

    out = yf_mod.YFinanceUSProvider._get_universe_tickers()
    assert out == ["NVDA", "TSLA"]


@pytest.mark.unit
def test_universe_respects_size_cap(monkeypatch):
    """T-YF-UNIV-04：源返回 1000 只 → 按 size 截断。"""
    from quant.providers import yfinance_us_provider as yf_mod

    monkeypatch.setattr("quant.config.QUANT_YFINANCE_US_UNIVERSE_SIZE", 50)
    monkeypatch.setattr(yf_mod, "_universe_from_akshare",
                        lambda size: [f"T{i}" for i in range(1000)])

    out = yf_mod.YFinanceUSProvider._get_universe_tickers()
    assert len(out) == 50


@pytest.mark.unit
def test_akshare_universe_parses_em_code_and_dot_tickers(monkeypatch):
    """T-YF-UNIV-05：akshare 源解析 em_code 格式 + 把 BRK.B 转 BRK-B（Yahoo 格式）。"""
    from quant.providers import yfinance_us_provider as yf_mod

    fake_df = pd.DataFrame([
        {"代码": "105.AAPL", "成交额": 1e10},
        {"代码": "105.BRK.B", "成交额": 5e9},   # 名字含 . —— 应转 BRK-B
        {"代码": "106.MSFT", "成交额": 2e10},
        {"代码": "无效格式", "成交额": 1e8},     # 没有 .，跳过
        {"代码": "105.AAPL", "成交额": 1e9},     # 重复，去重
    ])

    class _FakeAk:
        @staticmethod
        def stock_us_spot_em():
            return fake_df

    monkeypatch.setitem(__import__("sys").modules, "akshare", _FakeAk)

    out = yf_mod._universe_from_akshare(size=10)
    # 按成交额降序：MSFT > AAPL > BRK.B（无效格式被过滤；重复 AAPL 去重）
    assert out == ["MSFT", "AAPL", "BRK-B"]


@pytest.mark.unit
def test_universe_registers_builtin_cn_names(monkeypatch):
    """T-YF-UNIV-07：_get_universe_tickers 始终预填 45 只大盘中文名映射。

    保证即使 universe 走 stockanalysis / wikipedia（源本身无中文名），
    spot 阶段对常见大盘股也能显示中文。
    """
    from quant.providers import yfinance_us_provider as yf_mod

    # 清空 cache
    yf_mod._TICKER_TO_CN_NAME.clear()
    monkeypatch.setattr(yf_mod, "_universe_from_akshare", lambda size: [])
    monkeypatch.setattr(yf_mod, "_universe_from_stockanalysis", lambda size: [])
    monkeypatch.setattr(yf_mod, "_universe_from_wikipedia", lambda size: [])

    yf_mod.YFinanceUSProvider._get_universe_tickers()

    assert yf_mod._TICKER_TO_CN_NAME["AAPL"] == "苹果"
    assert yf_mod._TICKER_TO_CN_NAME["NVDA"] == "英伟达"
    assert yf_mod._TICKER_TO_CN_NAME["BRK-B"] == "伯克希尔哈撒韦"


@pytest.mark.unit
def test_akshare_universe_extracts_cn_names(monkeypatch):
    """T-YF-UNIV-08：akshare 源解析时把"名称"列写入中文名 cache。"""
    from quant.providers import yfinance_us_provider as yf_mod

    yf_mod._TICKER_TO_CN_NAME.clear()

    fake_df = pd.DataFrame([
        {"代码": "105.AAPL", "名称": "苹果", "成交额": 1e10},
        {"代码": "105.GOOGL", "名称": "谷歌-A", "成交额": 5e9},
        {"代码": "105.PLTR", "名称": "Palantir", "成交额": 8e9},
    ])

    class _FakeAk:
        @staticmethod
        def stock_us_spot_em():
            return fake_df

    monkeypatch.setitem(__import__("sys").modules, "akshare", _FakeAk)
    yf_mod._universe_from_akshare(size=10)

    assert yf_mod._TICKER_TO_CN_NAME["AAPL"] == "苹果"
    assert yf_mod._TICKER_TO_CN_NAME["GOOGL"] == "谷歌-A"
    assert yf_mod._TICKER_TO_CN_NAME["PLTR"] == "Palantir"


@pytest.mark.unit
def test_stockanalysis_universe_dedup_across_pages(monkeypatch):
    """T-YF-UNIV-06：stockanalysis 多页（SP500 + NDX100）抓取后去重。"""
    from quant.providers import yfinance_us_provider as yf_mod

    sp500_html = (
        "<html><body><table><thead><tr><th>Symbol</th></tr></thead>"
        "<tbody><tr><td>AAPL</td></tr><tr><td>MSFT</td></tr></tbody>"
        "</table></body></html>"
    )
    ndx100_html = (
        "<html><body><table><thead><tr><th>Symbol</th></tr></thead>"
        "<tbody><tr><td>AAPL</td></tr><tr><td>NVDA</td></tr></tbody>"
        "</table></body></html>"
    )

    class _FakeResp:
        def __init__(self, text):
            self.text = text
        def raise_for_status(self):
            pass

    class _FakeClient:
        def __init__(self, *a, **kw):
            self._calls = 0
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def get(self, url):
            self._calls += 1
            return _FakeResp(sp500_html if "sp-500" in url else ndx100_html)

    import httpx as _httpx
    monkeypatch.setattr(_httpx, "Client", _FakeClient)

    out = yf_mod._universe_from_stockanalysis(size=10)
    # AAPL 出现在两个页 → 去重；保留出现顺序
    assert out == ["AAPL", "MSFT", "NVDA"]
