"""公告三层判定（graph/prediction/announcements.py）+ 数据型维度分流（news_search.py）。

全部走 mock：不碰 akshare / SEC / Tavily / Postgres（真实取数已在容器里实测过，见设计文档 §1）。

  T-PRED-ANN-01  规则层：噪音丢弃 / 退市风险规则定级 -1.0 / 0 次 LLM
  T-PRED-ANN-02  候选池：needs_llm 条目才进 LLM，批大小 ≤ PREDICTION_ANN_BATCH
  T-PRED-ANN-03  缓存命中 → 0 次 LLM
  T-PRED-ANN-04  LLM 判定：THINK 前缀剥离 + confidence=llm_inferred + 回写缓存
  T-PRED-ANN-05  LLM 失败 → confidence=estimated（不伪装成「中性无风险」）
  T-PRED-ANN-06  announcements_full 维度不跑 LLM（时间线展示维度）
  T-PRED-ANN-07  事件分：饱和函数 + 时间衰减 + F 聚合
  T-PRED-ANN-08  分流：kind=data 走 _run_data_dimension，**不消耗 Tavily 预算**
  T-PRED-ANN-09  TOOL_TRACE start/end payload 结构
  T-PRED-ANN-10  DATA_ITEM payload 结构
  T-PRED-ANN-11  fetcher 未实现（lockup_buyback）→ 维度标 failed，不连累其余维度
  T-PRED-ANN-12  数据源抛错 → 维度 failed（不伪装成「这只票很干净」）
  T-PRED-ANN-13  两类维度在同一个 gather 里并发，返回 news_digest + filing_digest
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import replace

import pytest

import graph.prediction.announcements as ann
import graph.prediction.news_search as ns
from graph.prediction.config import PREDICTION_ANN_BATCH

pytestmark = pytest.mark.unit


# ── fakes ────────────────────────────────────────────────────────────────────

class _FakeLLM:
    """astream 桩：先吐 THINK token，再吐 JSON 数组（覆盖设计文档 §2.3 的推理模型前缀坑）。"""

    calls: list[list[dict]] = []
    fail: bool = False
    batch_sizes: list[int] = []

    def __init__(self, score: float = -0.6):
        self.score = score

    async def astream(self, messages, **_kw):
        type(self).calls.append(messages)
        if type(self).fail:
            raise RuntimeError("LLM 网关 500")
        user = messages[-1]["content"]
        # 从 prompt 里数出这批有几条（"共 N 条公告"）
        n = int(user.split("共 ", 1)[1].split(" 条", 1)[0])
        type(self).batch_sizes.append(n)
        idx = [int(line.split(".", 1)[0]) for line in user.splitlines() if line[:1].isdigit()]
        yield "\x00THINK\x00让我逐条看一下这些公告…"
        items = ",".join(
            f'{{"i":{i},"event_type":"negative","negative_score":{self.score},"reason":"测试判定"}}'
            for i in idx
        )
        yield f"[{items}]"
        yield {"usage": {"total_tokens": 1}}

    @classmethod
    def reset(cls, fail: bool = False):
        cls.calls = []
        cls.batch_sizes = []
        cls.fail = fail


@pytest.fixture(autouse=True)
def _stub_db_and_llm(monkeypatch):
    """DB 缓存 + LLM 工厂全部桩掉（单测不碰 Postgres / 模型网关）。"""
    _FakeLLM.reset()
    cache_writes: list[list[dict]] = []

    async def _empty_cache(market, symbol, keys=None, llm_ttl_days=None):
        return {}

    async def _put(items):
        cache_writes.append(list(items))
        return len(items)

    monkeypatch.setattr(ann, "get_announcement_cache", _empty_cache)
    monkeypatch.setattr(ann, "put_announcement_cache", _put)
    monkeypatch.setattr(ann, "get_chat_llm", lambda *a, **kw: _FakeLLM())
    monkeypatch.setattr(ns, "get_chat_llm", lambda *a, **kw: _FakeLLM())
    monkeypatch.setattr(ns, "TAVILY_API_KEY", "fake-key")
    return cache_writes


def _notice(title: str, ann_type: str, ann_date: str = "2026-07-01") -> dict:
    """未经 classify 的原始公告（fetch_stock_notices(classify=False) 的形状）。"""
    return {
        "symbol": "000001", "market": "cn_a", "name": "平安银行",
        "title": title, "ann_type": ann_type, "ann_date": ann_date,
        "url": "http://x", "source": "eastmoney_notice",
        "title_hash": f"h-{abs(hash(title))}", "confidence": "inferred",
    }


# ── 1. 三层判定 ───────────────────────────────────────────────────────────────

def test_rule_layer_drops_noise_and_grades_delisting():
    """T-PRED-ANN-01：噪音直接丢；退市风险规则定级 -1.0；全程 0 次 LLM。"""
    raw = [
        _notice("第八届董事会第五次会议决议公告", "董事会决议公告"),
        _notice("独立董事关于对外担保的专项说明", "独立董事意见"),
        _notice("关于公司股票被实施退市风险警示的公告", "实施退市风险警示"),
    ]
    items, stats = asyncio.run(
        ann.judge_announcements("000001", "cn_a", raw, dimension="negative_event")
    )

    assert stats["noise_dropped"] == 2, "常规治理公告必须在规则层丢弃（省 token 的关键）"
    assert stats["candidates"] == 0 and stats["llm_judged"] == 0
    assert not _FakeLLM.calls, "规则层判定不允许调用 LLM"

    assert len(items) == 1
    it = items[0]
    assert it["event_type"] == "negative"
    assert it["negative_score"] == -1.0
    assert it["confidence"] == "structured"      # 规则层 = 结构化字段判定
    assert "退市" in it["reason"]
    # FilingItem 契约字段齐全
    assert {"title", "date", "ann_type", "url", "event_type", "negative_score", "reason",
            "source", "confidence", "dilution_ratio", "discount_rate",
            "reduction_ratio", "decay_weight",
            "lockup_pct", "days_until", "support_ratio"} == set(it)


def test_candidate_pool_batches_at_most_ann_batch():
    """T-PRED-ANN-02：候选池按 PREDICTION_ANN_BATCH 分批（batch60 会飙 reasoning token）。"""
    raw = [_notice(f"关于收到监管问询函的公告（{i}）", "其它风险提示公告") for i in range(70)]
    items, stats = asyncio.run(
        ann.judge_announcements("000001", "cn_a", raw, dimension="negative_event")
    )

    assert stats["candidates"] == 70
    assert stats["llm_judged"] == 70
    assert stats["llm_batches"] == math.ceil(70 / PREDICTION_ANN_BATCH)
    assert max(_FakeLLM.batch_sizes) <= PREDICTION_ANN_BATCH, "批大小超过 30 → reasoning 截断风险"
    assert all(it["confidence"] == "llm_inferred" for it in items)
    assert all(it["negative_score"] == -0.6 for it in items)


def test_candidate_pool_capped_at_max_candidates():
    """候选池上限：超出部分按日期倒序丢弃，且明确写明「未做 LLM 精判」。"""
    raw = [
        _notice(f"关于收到问询函的公告（{i}）", "其它风险提示公告", ann_date=f"2026-0{1 + i % 6}-01")
        for i in range(ann.MAX_CANDIDATES + 15)
    ]
    items, stats = asyncio.run(
        ann.judge_announcements("000001", "cn_a", raw, dimension="negative_event")
    )
    assert stats["candidates"] == ann.MAX_CANDIDATES
    skipped = [it for it in items if "上限" in it["reason"]]
    assert len(skipped) == 15
    assert all(it["negative_score"] == 0.0 for it in skipped)


def test_cache_hit_skips_llm(monkeypatch):
    """T-PRED-ANN-03：命中 announcement_cache → 0 次 LLM。"""
    raw = [_notice("关于收到监管问询函的公告", "其它风险提示公告")]
    key = (raw[0]["ann_date"], raw[0]["title_hash"])

    async def _hit(market, symbol, keys=None, llm_ttl_days=None):
        return {key: {
            "event_type": "negative", "negative_score": -0.7,
            "reason": "缓存：监管问询", "judged_by": "llm",
        }}

    monkeypatch.setattr(ann, "get_announcement_cache", _hit)

    items, stats = asyncio.run(
        ann.judge_announcements("000001", "cn_a", raw, dimension="negative_event")
    )
    assert stats["cache_hits"] == 1 and stats["llm_judged"] == 0
    assert not _FakeLLM.calls, "缓存命中还调 LLM = 白烧钱"
    assert items[0]["negative_score"] == -0.7
    assert items[0]["confidence"] == "llm_inferred"


def test_llm_judgement_writes_cache(_stub_db_and_llm):
    """T-PRED-ANN-04：LLM 判定结果回写缓存（judged_by=llm），THINK 前缀不污染 JSON。"""
    raw = [_notice("关于公司被立案调查的公告", "违法违规")]
    items, stats = asyncio.run(
        ann.judge_announcements("000001", "cn_a", raw, dimension="negative_event")
    )
    assert stats["llm_judged"] == 1
    assert items[0]["confidence"] == "llm_inferred"

    writes = _stub_db_and_llm
    assert writes and writes[0][0]["judged_by"] == "llm"
    assert writes[0][0]["title_hash"] == raw[0]["title_hash"]


def test_llm_failure_marks_estimated_not_neutral():
    """T-PRED-ANN-05：LLM 挂了 → confidence=estimated + reason 写明，不伪装成「无风险」。"""
    _FakeLLM.reset(fail=True)
    raw = [_notice("关于收到监管问询函的公告", "其它风险提示公告")]
    items, stats = asyncio.run(
        ann.judge_announcements("000001", "cn_a", raw, dimension="negative_event")
    )
    assert stats["llm_failed"] == 1 and stats["llm_judged"] == 0
    assert items[0]["confidence"] == "estimated"
    assert "LLM 判定失败" in items[0]["reason"]
    assert items[0]["negative_score"] == 0.0     # 不计入事件分，但 UI 看得到原因


def test_announcements_full_never_calls_llm():
    """T-PRED-ANN-06：全量历史公告维度只做时间线展示 → 0 次 LLM（否则与风险维度重复烧钱）。"""
    raw = [_notice(f"关于收到问询函的公告（{i}）", "其它风险提示公告") for i in range(40)]
    items, stats = asyncio.run(
        ann.judge_announcements("000001", "cn_a", raw, dimension="announcements_full")
    )
    assert stats["candidates"] == 0 and stats["llm_judged"] == 0
    assert not _FakeLLM.calls
    assert len(items) == 40

    score, summary = ann.compute_dimension_score("announcements_full", items)
    assert score == 0.0, "数据底座维度不能单独计分（会与三个风险维度双重计数）"
    assert "40" in summary


# ── 2. 事件分（§5.3）───────────────────────────────────────────────────────────

def test_event_score_saturates_and_decays():
    """T-PRED-ANN-07：一条今天的 -1.0 → S_neg ≈ -0.39（κ=2.0），30 天前的同一条明显更轻。"""
    from datetime import date, timedelta

    today = date.today().isoformat()
    old = (date.today() - timedelta(days=90)).isoformat()

    fresh = [{"event_type": "negative", "negative_score": -1.0, "date": today,
              "decay_weight": ann.decay_weight(today)}]
    stale = [{"event_type": "negative", "negative_score": -1.0, "date": old,
              "decay_weight": ann.decay_weight(old)}]

    s_fresh, _ = ann.compute_dimension_score("negative_event", fresh)
    s_stale, _ = ann.compute_dimension_score("negative_event", stale)

    assert s_fresh == pytest.approx(-0.393, abs=0.01), "设计文档 §5.3 ①：-(1-e^-0.5) = -0.39"
    assert s_stale > s_fresh, "时间衰减：90 天前的爆雷必须比今天的轻"
    assert -1.0 < s_fresh <= 0.0

    f, scores, weights = ann.aggregate_event_score(
        [{"dimension": "negative_event", "score": s_fresh, "items": fresh}]
    )
    assert f == pytest.approx(weights["negative"] * s_fresh, abs=1e-6)
    assert scores["negative"] == s_fresh


def test_event_markers_only_negative():
    """K 线打点只标有负面分的条目。"""
    digest = [{
        "dimension": "negative_event",
        "items": [
            {"date": "2026-07-01", "event_type": "negative", "title": "立案", "negative_score": -0.9},
            {"date": "2026-06-01", "event_type": "neutral", "title": "例行", "negative_score": 0.0},
        ],
    }]
    markers = ann.to_event_markers(digest)
    assert len(markers) == 1
    assert set(markers[0]) == {"date", "event_type", "title", "score", "dimension"}
    assert markers[0]["score"] == -0.9


# ── 3. news_search 分流 ───────────────────────────────────────────────────────

def _run_data_dim(dim: str, **kw):
    """跑单个数据型维度（经 _run_dimension 顶部的 kind 分流）。"""
    traces: list[dict] = []
    cards: list[dict] = []

    async def _on_trace(p):
        traces.append(p)

    async def _on_card(p):
        cards.append(p)

    async def _run():
        return await ns._run_dimension(
            _FakeLLM(), kw.get("symbol", "000001.SZ"), kw.get("market", "cn_a"),
            dim, "pred_x",
            total=1, counter={"done": 0},
            sem=asyncio.Semaphore(5),
            budget_event=asyncio.Event(),
            on_progress=None, on_thinking=None, on_dimension=None,
            on_data_item=_on_card, on_tool_trace=_on_trace,
            siblings=kw.get("siblings", []),
        )

    return asyncio.run(_run()), traces, cards


def test_data_dimension_dispatch_no_tavily_budget(monkeypatch):
    """T-PRED-ANN-08：kind=data 走 _run_data_dimension —— 不调 Tavily、不递增预算计数。"""
    import quant.data as quant_data

    tavily_calls: list[str] = []
    budget_calls: list[str] = []

    async def _tavily(q, pid, max_results=10):
        tavily_calls.append(q)
        return {"answer": "", "results": []}

    async def _incr(pid):
        budget_calls.append(pid)
        return 1

    async def _fake_fetch(symbol, *, market="cn_a", months=24):
        return [
            {**_notice("关于公司被立案调查的公告", "违法违规"),
             "event_type": "negative", "event_subtype": "violation",
             "severity": None, "needs_llm": True, "is_noise": False,
             "reason": "公告类型命中「违法违规」"},
        ]

    monkeypatch.setattr(ns, "tavily_search", _tavily)
    monkeypatch.setattr(ns, "_incr_prediction_count", _incr)
    monkeypatch.setattr(quant_data, "fetch_negative_events", _fake_fetch)

    result, traces, cards = _run_data_dim("negative_event")

    assert result["kind"] == "data" and result["failed"] is False
    assert not tavily_calls, "数据型维度不该碰 Tavily"
    assert not budget_calls, "数据型维度不该消耗 Tavily 预算"
    assert result["total_fetched"] == 1
    assert result["llm_judged"] == 1
    assert result["score"] < 0
    assert "未解析 PDF" in result["source_note"]
    # FilingDimensionResult 契约字段
    assert {"dimension", "kind", "items", "score", "summary", "fetcher", "total_fetched",
            "candidates", "llm_judged", "cache_hits", "elapsed_ms", "source_note",
            "failed", "error"} == set(result)


def test_tool_trace_payload_shape(monkeypatch):
    """T-PRED-ANN-09：取数前后各一条 TOOL_TRACE（用户要的「看到模型调用工具」）。"""
    import quant.data as quant_data

    async def _fake_fetch(symbol, *, market="cn_a", months=24, classify=False):
        return [_notice("2025 年年度报告", "年度报告")]

    monkeypatch.setattr(quant_data, "fetch_stock_notices", _fake_fetch)
    result, traces, cards = _run_data_dim("announcements_full")

    assert len(traces) == 2
    start, end = traces
    assert start["phase"] == "start"
    assert start["tool"] == "fetch_stock_notices"
    assert start["dimension"] == "announcements_full"
    assert start["args"]["symbol"] == "000001.SZ"
    assert start["args"]["months"] == 24          # 反射装参：只传 fetcher 真有的形参
    assert start["args"]["classify"] is True
    assert isinstance(start["ts"], int)

    assert end["phase"] == "end" and end["ok"] is True
    assert end["rows"] == 1
    assert end["id"] == start["id"]               # 前端靠 id 配对 start/end
    assert isinstance(end["elapsed_ms"], int)
    assert "返回 1 条" in end["summary"]
    assert end["error"] == ""


def test_data_item_payload_shape(monkeypatch):
    """T-PRED-ANN-10：每条数据卡片一个 DATA_ITEM（与 NEWS_ITEM 平级）。"""
    import quant.data as quant_data

    async def _fake_fetch(symbol, *, market="cn_a", months=24):
        return [
            {**_notice("关于公司股票被实施退市风险警示的公告", "实施退市风险警示"),
             "event_type": "negative", "event_subtype": "st_delisting_risk",
             "severity": -1.0, "needs_llm": False, "is_noise": False, "reason": "规则定级"},
        ]

    monkeypatch.setattr(quant_data, "fetch_negative_events", _fake_fetch)
    result, traces, cards = _run_data_dim("negative_event")

    assert len(cards) == 1
    card = cards[0]
    assert card["dimension"] == "negative_event"
    assert card["kind"] == "data"
    assert card["fetcher"] == "fetch_negative_events"
    assert card["index"] == 1 and card["total"] == 1
    assert card["source_note"]
    assert card["item"]["negative_score"] == -1.0
    assert card["item"]["confidence"] == "structured"


def test_every_registered_fetcher_actually_exists():
    """T-PRED-ANN-11：注册表里每个数据型维度的 fetcher 都必须在 quant.data 里真实存在。

    ★ 这条不变式当初就该有 —— 它正是本次线上问题的成因：
      dimensions.py（契约层）照设计文档 §2.2 声明了 lockup_buyback + fetch_lockup_buyback，
      而 quant/data/announcement.py（数据层）照 §9 的实现清单只建了另外 4 个 fetcher。
      两个 agent 并行、各自都没错，**接缝没人负责** → 用户勾上该维度就看到
      「取数函数 fetch_lockup_buyback 未实现」。
      注册表与数据层是一份契约的两半，必须锁死。
    """
    import quant.data as quant_data
    from graph.prediction.dimensions import DIMENSION_REGISTRY

    dims = DIMENSION_REGISTRY.values() if isinstance(DIMENSION_REGISTRY, dict) \
        else DIMENSION_REGISTRY
    missing = [
        (d.key, d.fetcher)
        for d in dims
        if d.kind == "data" and not callable(getattr(quant_data, d.fetcher or "", None))
    ]
    assert not missing, f"注册表声明了 fetcher 但数据层没实现：{missing}"


def test_missing_fetcher_degrades_dimension(monkeypatch):
    """T-PRED-ANN-11b：fetcher 真的缺失时 → 该维 failed，不抛（维度级降级仍要成立）。"""
    from graph.prediction import dimensions as dim_mod

    real = dim_mod.get_dimension("stake_reduction")
    ghost = replace(real, fetcher="fetch_does_not_exist")
    monkeypatch.setattr(dim_mod, "get_dimension", lambda k: ghost if k == "stake_reduction" else real)
    monkeypatch.setattr(ns, "get_dimension", lambda k: ghost if k == "stake_reduction" else real)

    result, traces, cards = _run_data_dim("stake_reduction")
    assert result["failed"] is True
    assert "未实现" in result["error"]
    assert result["items"] == [] and result["score"] == 0.0
    assert not traces, "还没开始调用就失败了，不该发 TOOL_TRACE"


def test_fetch_error_marks_failed_not_clean(monkeypatch):
    """T-PRED-ANN-12：数据源挂了 → failed（把「数据源挂了」说成「这票很干净」是最危险的谎）。"""
    import quant.data as quant_data
    from quant.data import AnnouncementDataError

    async def _boom(symbol, *, market="cn_a", months=24):
        raise AnnouncementDataError("eastmoney", "东财 429 限流")

    monkeypatch.setattr(quant_data, "fetch_reductions", _boom)
    result, traces, cards = _run_data_dim("stake_reduction")

    assert result["failed"] is True
    assert "429" in result["error"]
    assert result["items"] == []
    assert len(traces) == 2 and traces[1]["ok"] is False
    assert "调用失败" in traces[1]["summary"]


def test_gather_mixes_search_and_data_dims(monkeypatch):
    """T-PRED-ANN-13：搜索型 + 数据型在同一个 gather 里并发，分别归到两个 digest。"""
    import quant.data as quant_data

    async def _tavily(q, pid, max_results=10):
        return {"answer": "综合答案", "results": [{"title": "t", "url": "u",
                                                   "snippet": "s", "full_content": "f"}]}

    async def _incr(pid):
        return 1

    async def _fake_fetch(symbol, *, market="cn_a", months=24):
        return [{**_notice("关于股东减持股份的公告", "股东减持"),
                 "event_type": "stake_reduction", "event_subtype": "reduction",
                 "severity": None, "needs_llm": False, "is_noise": False,
                 "pct_of_total": 2.0, "reason": "东财减持明细", "source": "eastmoney_ggcg"}]

    monkeypatch.setattr(ns, "tavily_search", _tavily)
    monkeypatch.setattr(ns, "_incr_prediction_count", _incr)
    monkeypatch.setattr(quant_data, "fetch_reductions", _fake_fetch)

    traces: list[dict] = []
    dims_done: list[dict] = []

    async def _on_trace(p):
        traces.append(p)

    async def _on_dim(d):
        dims_done.append(d)

    async def _run():
        return await ns.gather_all_dimensions(
            "000001.SZ", "cn_a", "pred_mix",
            ["macro_policy", "stake_reduction"],
            on_dimension=_on_dim,
            on_tool_trace=_on_trace,
        )

    out = asyncio.run(_run())

    assert [d["dimension"] for d in out["news_digest"]] == ["macro_policy"]
    assert [d["dimension"] for d in out["filing_digest"]] == ["stake_reduction"]
    assert out["filing_digest"][0]["kind"] == "data"
    assert -1.0 <= out["news_sentiment"] <= 1.0
    assert len(dims_done) == 2, "两类维度都要触发 on_dimension（节点按 kind 分流落库）"
    assert len(traces) == 2, "只有数据型维度发 TOOL_TRACE"

    # 减持 2% → x_k = 0.02/0.01 = 2 → S_sr = -(1-e^{-2/3}) ≈ -0.487（衰减权重 ~1，公告是今天的）
    score = out["filing_digest"][0]["score"]
    assert -1.0 < score < 0.0
    item = out["filing_digest"][0]["items"][0]
    assert item["reduction_ratio"] == pytest.approx(0.02), "百分数必须换算成小数（否则强度放大 100 倍）"


# ── lockup_buyback：解禁（抛压）+ 回购（托底）──────────────────────────────────

def test_lockup_scoring_weights_by_imminence():
    """解禁强度必须按「临近度」加权 —— 越近的解禁抛压越重。

    ★ 这里藏着一个方向性陷阱：解禁是**未来**事件，而通用的 decay_weight(ann_date) 是按
      「过去多久」算的（w = exp(-Δt/τ)）。把未来日期喂进去会得到 exp(+Δt/τ) > 1 ——
      越遥远的解禁反而权重越大，完全反了。所以 lockup 通道自己用 exp(-days_until/τ)。
    实测（真实数据）：600925 解禁 0.77% / 79 天后 → 强度 0.13；
                     301303 解禁 0.75% / 39 天后 → 强度 0.32（比例几乎一样，只因更临近就重 2.5 倍）。
    """
    near = {"event_type": "lockup", "lockup_pct": 1.0, "days_until": 10, "date": "2026-08-01"}
    far = {"event_type": "lockup", "lockup_pct": 1.0, "days_until": 85, "date": "2026-10-01"}

    x_near = ann._intensity(near, "lockup")
    x_far = ann._intensity(far, "lockup")

    assert x_near > x_far, "同样规模的解禁，越临近抛压必须越大"
    assert 0 < x_far < 1.0
    assert x_near <= 1.0 + 1e-9


def test_buyback_offsets_lockup_but_never_turns_positive():
    """回购「托底」= 抵消解禁抛压，但不能把预测推成利多。

    事件分 F 被钳在 [-1, 0]（engine.py:540），本模型表达不了正向事件驱动。
    维度描述写的就是「托底」而非「利多」，所以净额抵消是**符合语义**的建模简化。
    """
    lock = {"event_type": "lockup", "lockup_pct": 3.0, "days_until": 0, "date": "2026-07-12"}
    buy_small = {"event_type": "buyback", "support_ratio": 0.5, "date": "2026-06-01"}
    buy_huge = {"event_type": "buyback", "support_ratio": 99.0, "date": "2026-06-01"}

    s_lock_only, _ = ann.compute_dimension_score(ann.DIM_LOCKUP, [lock])
    s_offset, _ = ann.compute_dimension_score(ann.DIM_LOCKUP, [lock, buy_small])
    s_over, _ = ann.compute_dimension_score(ann.DIM_LOCKUP, [lock, buy_huge])

    assert s_lock_only < 0, "有解禁就该有负分"
    assert s_lock_only < s_offset < 0, "回购要把抛压减轻（负分变小），但仍是负的"
    assert s_over == 0.0, "回购再大也只能抵消到 0，不能变成正分（F 钳在 [-1,0]）"


def test_lockup_dimension_is_cn_a_only():
    """akshare 的解禁/回购接口只有 A 股 —— 注册表必须如实声明，别在美股下给用户一个必然失败的选项。"""
    from graph.prediction.dimensions import get_dimension
    assert get_dimension("lockup_buyback").markets == ("cn_a",)


def test_lockup_fetcher_returns_empty_for_us_market():
    """越过注册表直调美股 → 返回 []（不抛，也不伪造数据）。"""
    import quant.data as quant_data
    rows = asyncio.run(quant_data.fetch_lockup_buyback("MU", market="us_stock"))
    assert rows == []


def test_lockup_fields_survive_filing_item_roundtrip():
    """★ 回归：解禁的强度字段必须活着穿过 _filing_item + pydantic 归一化。

    踩过的坑：fetch_lockup_buyback 正确返回了 lockup_pct/days_until，数据卡片也照常显示，
    但 domain.FilingItem 没声明这两个字段 → FilingItem(**d) 把它们**静默丢弃** →
    _intensity 读不到 lockup_pct → 解禁强度恒为 0 → 维度分 0.0 → 事件分公式里
    根本看不到解禁这一路。UI 看着一切正常，模型里却完全没吃到这个风险因子 —— 最阴的一种 bug。
    """
    from graph.prediction.domain import FilingItem

    raw = {
        "title": "限售解禁 2026-08-20（占总市值 0.75%，39 天后）",
        "ann_date": "2026-08-20", "ann_type": "限售解禁", "url": "",
        "source": "eastmoney_lockup", "confidence": "hard",
        "event_type": "lockup", "needs_llm": False, "severity": None,
        "lockup_pct": 0.75, "days_until": 39,
    }
    item = ann._filing_item(
        raw, event_type="lockup", negative_score=0.0, reason="", confidence="structured",
    )
    assert item["lockup_pct"] == 0.75 and item["days_until"] == 39

    # 关键一步：过一遍真正的 pydantic 模型（生产链路就是这么走的）
    revived = FilingItem(**item).model_dump()
    assert revived["lockup_pct"] == 0.75, "pydantic 丢字段 → 解禁强度会静默归零"
    assert revived["days_until"] == 39

    assert ann._intensity(revived, "lockup") > 0, "字段活着，强度就必须 > 0"
    score, _ = ann.compute_dimension_score(ann.DIM_LOCKUP, [revived])
    assert score < 0, "有未来解禁 → 维度分必须是负的，不能是 0"
