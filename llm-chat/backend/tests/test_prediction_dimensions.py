"""维度注册表契约（graph/prediction/dimensions.py + domain.PredictionCriteria）—— 纯 mock。

注册表是前后端的**唯一真相源**：前端 Picker 全靠 GET /dimensions 下发的 desc/kind/markets
渲染（零文案硬编码），后端靠它决定跑哪些 fetcher。它一歪，两头一起歪。

  T-PRED-DIM-01  26 维：key 唯一、无空字段、分组不重不漏
  T-PRED-DIM-02  20 search + 6 data；data 维必须有 fetcher，search 维必须没有
  T-PRED-DIM-03  market 过滤：sec_filings 只在美股；A 股默认集里不含它
  T-PRED-DIM-04  validate_dimensions：未注册 key / 跨市场 key 都判非法
  T-PRED-DIM-05  PredictionCriteria：垃圾 key 静默丢弃（不 400），全垃圾 → 回退默认集
  T-PRED-DIM-06  to_payload 的字段与前端 Dimension 接口逐一对齐（markets 是数组）
  T-PRED-DIM-07  ★ 端到端：gather 里 kind=data 的维度**一次 Tavily 都不调、不占预算**
                 （Tavily 按次计费；把 6 个数据维接进搜索通道 = 每次预测白烧 12 次检索）
"""
from __future__ import annotations

import asyncio

import pytest

import graph.prediction.dimensions as D
import graph.prediction.news_search as ns
from graph.prediction.domain import PredictionCriteria

pytestmark = pytest.mark.unit

EXPECTED_TOTAL = 26
EXPECTED_DATA = 6
EXPECTED_SEARCH = 20


# ══════════════════════════════════════════════════════════════════════════════
# T-PRED-DIM-01 / 02：注册表自洽
# ══════════════════════════════════════════════════════════════════════════════

def test_registry_has_26_unique_keys():
    """T-PRED-DIM-01：26 维、key 唯一（dict 天然去重 → 用 all_keys 的长度反查重复定义）。"""
    keys = D.all_keys()
    assert len(keys) == EXPECTED_TOTAL, f"维度数应为 {EXPECTED_TOTAL}，实得 {len(keys)}"
    assert len(set(keys)) == len(keys), "存在重复 key（后定义的会静默覆盖先定义的）"
    assert set(keys) == set(D.DIMENSION_REGISTRY)


def test_every_dimension_has_display_fields():
    """前端零文案硬编码 → label/desc 必须后端下发，缺一个就是空白复选框。"""
    for key, d in D.DIMENSION_REGISTRY.items():
        assert d.key == key, f"注册表 key 与 Dimension.key 不一致: {key} vs {d.key}"
        assert d.label.strip(), f"{key} 缺 label"
        assert d.desc.strip(), f"{key} 缺 desc（前端要直接显示这句话）"
        assert d.kind in ("search", "data"), f"{key} kind 非法: {d.kind}"
        assert d.markets, f"{key} 未声明适用市场"
        assert set(d.markets) <= {"cn_a", "us_stock"}, f"{key} 市场值非法: {d.markets}"
        assert d.name == d.label, "name 是 label 的别名，必须一致"


def test_groups_cover_every_key_exactly_once():
    """T-PRED-DIM-01：分组不重不漏 —— 漏了的维度在前端面板里根本不出现（用户永远勾不到）。"""
    grouped: list[str] = []
    for _name, keys in D.DIMENSION_GROUPS:
        grouped.extend(keys)

    assert len(grouped) == len(set(grouped)), "同一 key 出现在多个分组里"
    assert set(grouped) == set(D.all_keys()), (
        f"分组与注册表不一致：漏 {set(D.all_keys()) - set(grouped)}，"
        f"多 {set(grouped) - set(D.all_keys())}"
    )


def test_kind_split_and_fetcher_contract():
    """T-PRED-DIM-02：6 data + 20 search；data 必须有 fetcher（否则维度必然 failed）。"""
    data_keys = D.data_keys()
    search_keys = D.search_keys()

    assert len(data_keys) == EXPECTED_DATA, f"data 维应为 {EXPECTED_DATA}，实得 {data_keys}"
    assert len(search_keys) == EXPECTED_SEARCH, f"search 维应为 {EXPECTED_SEARCH}"
    assert len(data_keys) + len(search_keys) == EXPECTED_TOTAL

    for k in data_keys:
        assert D.DIMENSION_REGISTRY[k].fetcher, f"data 维 {k} 未声明 fetcher"
    for k in search_keys:
        assert D.DIMENSION_REGISTRY[k].fetcher is None, (
            f"search 维 {k} 不该有 fetcher（会被 _run_dimension 误判成数据型）"
        )


def test_split_by_kind_preserves_membership():
    """split_by_kind 是节点分流的依据，不能吞 key。"""
    keys = D.all_keys()
    search, data = D.split_by_kind(keys)
    assert set(search) | set(data) == set(keys)
    assert not (set(search) & set(data)), "同一 key 既算搜索型又算数据型"


# ══════════════════════════════════════════════════════════════════════════════
# T-PRED-DIM-03 / 04：市场过滤
# ══════════════════════════════════════════════════════════════════════════════

def test_sec_filings_is_us_only():
    """T-PRED-DIM-03：A 股不该出现 SEC 备案维度（勾了必然取不到数）。"""
    sec = D.DIMENSION_REGISTRY["sec_filings"]
    assert sec.supports("us_stock") is True
    assert sec.supports("cn_a") is False

    assert "sec_filings" not in D.dimensions_for_market("cn_a")
    assert "sec_filings" in D.dimensions_for_market("us_stock")
    assert "sec_filings" not in D.default_dimensions("cn_a")


def test_market_filter_is_a_subset_of_registry():
    """按市场过滤只做减法，绝不凭空多出 key。"""
    for market in ("cn_a", "us_stock"):
        for_market = D.dimensions_for_market(market)
        assert set(for_market) <= set(D.all_keys())
        assert all(D.DIMENSION_REGISTRY[k].supports(market) for k in for_market)

        defaults = D.default_dimensions(market)
        assert set(defaults) <= set(for_market), "默认集里有该市场不支持的维度"
        assert all(D.DIMENSION_REGISTRY[k].default_on for k in defaults)


def test_validate_dimensions_rejects_unknown_and_cross_market():
    """T-PRED-DIM-04：垃圾 key 与跨市场 key 都进 invalid（v1 完全不校验 → 照跑 Tavily 烧钱）。"""
    valid, invalid = D.validate_dimensions(
        ["macro_policy", "sec_filings", "not_a_real_dimension", "macro_policy"],
        market="cn_a",
    )
    assert valid == ["macro_policy"], "重复 key 应去重且保持顺序"
    assert set(invalid) == {"sec_filings", "not_a_real_dimension"}

    valid_us, invalid_us = D.validate_dimensions(["sec_filings"], market="us_stock")
    assert valid_us == ["sec_filings"] and invalid_us == []


# ══════════════════════════════════════════════════════════════════════════════
# T-PRED-DIM-05 / 06：请求契约 + 序列化契约
# ══════════════════════════════════════════════════════════════════════════════

def test_criteria_drops_invalid_keys_but_keeps_good_ones():
    """T-PRED-DIM-05：单个坏 key 不该让整单预测 400 —— 丢弃 + warning，好 key 照跑。"""
    c = PredictionCriteria(
        symbol="000001.SZ", market="cn_a",
        dimensions=["negative_event", "sec_filings", "bogus_key"],
    )
    assert c.dimensions == ["negative_event"]
    assert c.resolved_dimensions() == ["negative_event"]


def test_criteria_all_invalid_falls_back_to_defaults():
    """全是垃圾 key → 回退默认集，而不是跑一张空图（用户会对着空白页等 90 秒）。"""
    c = PredictionCriteria(symbol="000001.SZ", market="cn_a", dimensions=["x", "y"])
    assert c.dimensions == D.default_dimensions("cn_a")
    assert "sec_filings" not in c.dimensions


def test_criteria_default_horizon_is_7():
    """用户拍板：7 个交易日（画 7 根蜡烛）。"""
    c = PredictionCriteria(symbol="000001.SZ", market="cn_a")
    assert c.horizon == 7
    assert c.dimensions is None
    assert c.resolved_dimensions() == D.default_dimensions("cn_a")


def test_to_payload_matches_frontend_contract():
    """T-PRED-DIM-06：前端 Dimension 接口 = {key,label,desc,kind,markets[],fetcher,default_on}。"""
    payload = D.to_payload("us_stock")
    assert payload, "美股维度不该为空"

    expect = {"key", "label", "desc", "kind", "markets", "fetcher", "default_on"}
    for row in payload:
        assert set(row) == expect, f"payload 字段与前端契约不符: {set(row) ^ expect}"
        assert isinstance(row["markets"], list), "markets 必须序列化成 JSON 数组（不是 tuple）"
        assert isinstance(row["default_on"], bool)

    assert {r["key"] for r in payload} == set(D.dimensions_for_market("us_stock"))


# ══════════════════════════════════════════════════════════════════════════════
# T-PRED-DIM-07：★ 数据型维度不烧 Tavily 预算（端到端过 gather_all_dimensions）
# ══════════════════════════════════════════════════════════════════════════════

def test_data_dimensions_never_touch_tavily_in_gather(monkeypatch):
    """★ 混跑 3 搜索维 + 2 数据维：Tavily 只被搜索维调用，预算计数只被搜索维递增。

    Tavily 按次计费。若数据型维度误走搜索通道，每次预测会为「公告/减持」白烧检索额度，
    而且拿回来的是搜索引擎摘要而非结构化公告表 —— 钱和数据质量双输。
    """
    import quant.data as quant_data

    tavily_calls: list[str] = []
    budget_hits: list[str] = []

    # ★ 不桩掉 tavily_search 本体 —— 预算计数器就长在它里面（先递增再发 HTTP），
    #   桩掉它这条断言就永远是 0（假绿）。改为桩掉**它下面的 httpx**：真函数照跑、
    #   预算照数，但一个字节都不出网。
    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"answer": "无重大事件", "results": []}

    class _Client:
        def __init__(self, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, url, json=None, **_kw):
            tavily_calls.append((json or {}).get("query", ""))
            return _Resp()

    monkeypatch.setattr(ns.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(ns, "TAVILY_API_KEY", "test-key-not-real")

    async def _fake_incr(pid):
        budget_hits.append(pid)
        return len(budget_hits)

    async def _fake_fetch(symbol, *, market="cn_a", months=24, classify=False):
        return []       # 取数成功但 0 条（干净票）——够了，重点是「有没有走 Tavily」

    monkeypatch.setattr(ns, "_incr_prediction_count", _fake_incr)
    monkeypatch.setattr(quant_data, "fetch_stock_notices", _fake_fetch, raising=False)
    monkeypatch.setattr(quant_data, "fetch_negative_events", _fake_fetch, raising=False)

    # LLM 只会被搜索维的情绪打分用到
    class _LLM:
        async def astream(self, *_a, **_kw):
            yield '{"sentiment": 0.0, "reason": "无"}'

    monkeypatch.setattr(ns, "get_chat_llm", lambda *_a, **_kw: _LLM())

    search_dims = ["macro_policy", "market_sentiment", "capital_flow"]
    data_dims = ["announcements_full", "negative_event"]

    out = asyncio.run(ns.gather_all_dimensions(
        "000001.SZ", "cn_a", "pred_dim_budget",
        dimensions=search_dims + data_dims,
    ))

    assert len(tavily_calls) == len(search_dims), (
        f"Tavily 调用次数({len(tavily_calls)}) != 搜索维度数({len(search_dims)}) —— "
        "数据型维度很可能被误接进了搜索通道"
    )
    assert len(budget_hits) == len(search_dims), (
        f"Tavily 预算计数({len(budget_hits)}) != 搜索维度数({len(search_dims)}) —— "
        "数据型维度在消耗按次计费的检索额度"
    )

    # 两类结果各归各的桶（节点按 kind 分流写 news_digest / filing_digest）
    assert {d["dimension"] for d in out["news_digest"]} == set(search_dims)
    assert {d["dimension"] for d in out["filing_digest"]} == set(data_dims)
    assert all(d.get("kind") == "data" for d in out["filing_digest"])
