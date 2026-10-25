"""预测图拓扑与条件边（graph/prediction_agent.py + preflight/compute/analyze 节点）—— 单元测试

★ 本文件的头号用例是 T-PRED-GRAPH-02：**预检失败时 news_search 一次都不能被调用**。
  修复前图顺序是 START → news_search → compute → analyze：那句 2 秒就能得出的行情判死，
  被排在 11~16 分钟的 12 维资讯搜索之后。用户等十几分钟，最后只收到一句
  「历史行情为空，无法预测」。这条断言就是"不再浪费 10 分钟"的回归保证。

  T-PRED-GRAPH-01: 图拓扑 —— START→preflight→(error?END:news_search)→compute→(error?END:analyze)→END
  T-PRED-GRAPH-02: 预检失败 → state 置 error + error_kind，且 news_search / compute / analyze 全不跑
  T-PRED-GRAPH-03: compute 置了 error → analyze 不被调用（不再白烧一次完整 LLM）
  T-PRED-GRAPH-04: 全链成功 → 四个节点依次跑完，final_state 带 forecast + analysis
  T-PRED-GRAPH-05: 路由函数 _route_after_preflight / _route_after_compute 直接单测
  T-PRED-GRAPH-06: on_chain_end 白名单含 "preflight"（漏了会把预检失败当成功落 done）
  T-PRED-GRAPH-07: _transition_status —— SM 拒绝的终态被强制写入（状态卡死回归测试）
  T-PRED-GRAPH-08: _finalize_progress —— done/stopped 清空 progress；failed 落 error_kind 且不带 phase

隔离策略：
  - predict_graph 在 import 时已用 `_node.execute` 的**绑定方法**编译进图，事后 patch 节点
    实例/类都不生效。所以这里 patch 的是各节点模块里的**叶子依赖**
    （preflight / search_all_dimensions / fetch_bars / forecast / get_chat_llm + 各 store 写函数），
    跑的是真实的 predict_graph.ainvoke —— 测的是真图，不是复刻的假图。
  - 无 DB / 无网络 / 无 LLM。全 async，asyncio.run 驱动。
运行：pytest tests/test_prediction_graph.py -m unit -v
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langgraph.graph import END

import graph.nodes.prediction.analyze_node as analyze_mod
import graph.nodes.prediction.compute_node as compute_mod
import graph.nodes.prediction.news_search_node as news_mod
import graph.nodes.prediction.preflight_node as preflight_mod
import graph.prediction_agent as pa
from graph.prediction.market_data import (
    KIND_BAD_SYMBOL,
    KIND_RATE_LIMITED,
    MarketDataError,
)


# ════════════════════════════════════════════════════════════════════════════
# 探针：记录每个节点的叶子依赖被调了几次
# ════════════════════════════════════════════════════════════════════════════

class _Calls:
    def __init__(self) -> None:
        self.preflight = 0
        self.news = 0
        self.fetch_bars = 0
        self.forecast = 0
        self.llm = 0
        self.status_writes: list[tuple[str, str]] = []
        self.progress_writes: list[dict] = []


class _FakeLLM:
    """analyze_node 的 LLM 替身（astream，铁律 #6）。"""

    def __init__(self, calls: _Calls):
        self._calls = calls

    async def astream(self, messages, *a, **kw):
        self._calls.llm += 1
        for ch in '{"analysis":"综合分析","risk_notes":["风险A"]}':
            yield ch


@pytest.fixture
def calls(monkeypatch) -> _Calls:
    """把四个节点的所有外部依赖（行情/资讯/LLM/DB）换成计数替身。

    预检默认成功；各用例按需覆盖 preflight_mod.preflight。
    """
    c = _Calls()

    # ── preflight 节点 ──────────────────────────────────────────────────────
    async def _fake_preflight(symbol, market="cn_a"):
        c.preflight += 1
        return {
            "ok": True, "symbol": symbol.upper(), "market": market,
            "bars": 17, "last_close": 10.5, "last_date": "2026-07-10",
        }

    monkeypatch.setattr(preflight_mod, "preflight", _fake_preflight)

    # emit_progress 的 DB 落库（preflight/compute/analyze 都经 preflight_mod.emit_progress）
    async def _fake_progress(sid, payload):
        c.progress_writes.append(dict(payload))

    monkeypatch.setattr(preflight_mod, "update_prediction_progress", _fake_progress)
    monkeypatch.setattr(news_mod, "update_prediction_progress", _fake_progress)

    # ── news_search 节点（v2：gather_all_dimensions，搜索型 + 数据型同一 gather）──
    async def _fake_gather(symbol, market, prediction_id, dims=None, **kw):
        c.news += 1
        digest = [{
            "dimension": "macro_policy", "query": "q", "answer": "a", "results": [],
            "sentiment": 0.3, "sentiment_reason": "r", "failed": False, "error": "",
        }]
        return {"news_digest": digest, "news_sentiment": 0.3, "filing_digest": []}

    monkeypatch.setattr(news_mod, "gather_all_dimensions", _fake_gather)

    async def _fake_news_store(sid, digest, sentiment=None):
        return None

    monkeypatch.setattr(news_mod, "update_prediction_news", _fake_news_store)

    # v2 增量落库（每维一跑完就写库 —— 暂停续跑的前置）
    async def _fake_append_dim(sid, result, kind="search"):
        return True

    async def _fake_append_trace(sid, trace):
        return True

    async def _fake_filings_store(sid, digest, event_score=None):
        return None

    async def _fake_get_snap(sid):
        return None

    monkeypatch.setattr(news_mod, "append_prediction_dimension", _fake_append_dim)
    monkeypatch.setattr(news_mod, "append_prediction_tool_trace", _fake_append_trace)
    monkeypatch.setattr(news_mod, "update_prediction_filings", _fake_filings_store)
    monkeypatch.setattr(news_mod, "get_prediction_snapshot", _fake_get_snap)

    # ── compute 节点 ────────────────────────────────────────────────────────
    async def _fake_fetch_bars(symbol, market, lookback, **kw):
        c.fetch_bars += 1
        import pandas as pd
        return pd.DataFrame({
            "symbol": [symbol] * 40,
            "date": pd.date_range("2026-01-05", periods=40).strftime("%Y-%m-%d"),
            "close": [10.0 + i * 0.1 for i in range(40)],
        })

    monkeypatch.setattr(compute_mod, "fetch_bars", _fake_fetch_bars)

    async def _fake_forecast(bars_df, sentiment, horizon, *, filing_digest=None, on_step=None):
        c.forecast += 1
        return {
            "dates": ["2026-07-13"], "point_forecast": [11.0],
            "p10": [10.0], "p50": [11.0], "p90": [12.0],
            "inflection_points": [], "confidence": 0.7, "model_name": "ARIMA+GARCH",
            "technical_indicators": {},
            "model_params": {}, "formula_steps": [], "forecast_candles": [],
        }

    monkeypatch.setattr(compute_mod, "forecast", _fake_forecast)

    async def _fake_forecast_store(sid, **kw):
        return None

    monkeypatch.setattr(compute_mod, "update_prediction_forecast", _fake_forecast_store)

    # v2 产物落库（公式步骤 / 预测蜡烛 / 全参数 / 事件打点）
    async def _noop_store(*a, **kw):
        return None

    for fn in (
        "update_prediction_model_params",
        "update_prediction_formula_steps",
        "update_prediction_candles",
        "update_prediction_event_markers",
        "append_prediction_formula_step",
        "append_prediction_forecast_candle",
    ):
        monkeypatch.setattr(compute_mod, fn, _noop_store)

    # ── 状态写库（两个节点各自 import 了 update_prediction_status）──────────
    async def _fake_status(sid, status, error_message=None):
        c.status_writes.append((sid, status))

    monkeypatch.setattr(news_mod, "update_prediction_status", _fake_status)
    monkeypatch.setattr(compute_mod, "update_prediction_status", _fake_status)

    # ── analyze 节点 ────────────────────────────────────────────────────────
    monkeypatch.setattr(analyze_mod, "get_chat_llm", lambda *a, **kw: _FakeLLM(c))

    return c


def _state(symbol: str = "MU", market: str = "us_stock") -> dict:
    return pa.build_initial_state(
        snapshot_id="pred_graph",
        client_id="admin",
        user_id="",
        criteria={"market": market, "symbol": symbol, "horizon": 5},
        status="queued",
    )


# ════════════════════════════════════════════════════════════════════════════
# T-PRED-GRAPH-01: 图拓扑
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_graph_topology_preflight_is_first():
    """契约 E：START → preflight → (条件) news_search → compute → (条件) analyze → END。"""
    graph = pa.predict_graph.get_graph()
    edges = {(e.source, e.target) for e in graph.edges}

    assert ("__start__", "preflight") in edges, "preflight 必须是图的第一个节点"
    assert ("preflight", "news_search") in edges
    assert ("preflight", "__end__") in edges, "预检失败必须能直接短路到 END"
    assert ("news_search", "compute") in edges
    assert ("compute", "analyze") in edges
    assert ("compute", "__end__") in edges, "compute 失败必须能直接短路到 END"
    assert ("analyze", "__end__") in edges

    # 旧拓扑（news_search 打头）必须已被取代
    assert ("__start__", "news_search") not in edges, (
        "资讯搜索不能再是第一个节点 —— 那会让行情判死排在 11~16 分钟之后"
    )


# ════════════════════════════════════════════════════════════════════════════
# ★ T-PRED-GRAPH-02: 预检失败 → 不跑资讯搜索（本次修复的核心保证）
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_preflight_failure_short_circuits_news_search(calls, monkeypatch):
    """★ 核心：preflight 抛 MarketDataError → 图立刻短路到 END。

    news_search / compute / analyze 一个都不能跑：这是"用户不再白等 10 分钟才看到
    一句误导性红错"的唯一保证。同时 state 必须带上 error + error_kind
    （error_kind 已在 PredictState 里声明，否则 LangGraph 会静默丢弃它）。
    """
    async def _boom(symbol, market="cn_a"):
        calls.preflight += 1
        raise MarketDataError(
            KIND_RATE_LIMITED,
            "数据源限流（雅虎财经 HTTP 429），请稍后重试",
        )

    monkeypatch.setattr(preflight_mod, "preflight", _boom)

    final = asyncio.run(pa.predict_graph.ainvoke(_state()))

    # 预检跑了、且判死了
    assert calls.preflight == 1
    assert final["error"] == "数据源限流（雅虎财经 HTTP 429），请稍后重试"
    assert final["error_kind"] == KIND_RATE_LIMITED, (
        "error_kind 必须能穿过 LangGraph state（PredictState 里要声明该 key），"
        "否则前端无法区分「稍后重试」和「改代码」"
    )

    # ★★ 最重要的三条：下游一个都没跑 ★★
    assert calls.news == 0, "预检失败后仍跑了资讯搜索 —— 用户又要白等 11~16 分钟"
    assert calls.fetch_bars == 0, "预检失败后仍去拉行情"
    assert calls.llm == 0, "预检失败后仍烧了一次 analyze LLM"

    # 没有任何节点做状态机转换（预检阶段 status 保持 queued，契约 D）
    assert calls.status_writes == [], f"预检失败不应有状态转换，实际 {calls.status_writes}"
    assert final.get("status") == "queued"


@pytest.mark.unit
def test_missing_symbol_short_circuits_too(calls):
    """未指定标的 → 同样在预检阶段判死，不跑资讯搜索（连 preflight() 都不用调）。"""
    final = asyncio.run(pa.predict_graph.ainvoke(_state(symbol="")))

    assert final["error"] == "未指定标的代码，无法预测"
    assert final["error_kind"] == KIND_BAD_SYMBOL
    assert calls.preflight == 0, "空 symbol 应零网络开销判死"
    assert calls.news == 0
    assert calls.llm == 0


@pytest.mark.unit
def test_preflight_emits_progress_and_normalizes_symbol(calls):
    """预检成功 → 发 PREDICTION_PROGRESS（phase=preflight）+ 回写归一化 symbol。"""
    final = asyncio.run(pa.predict_graph.ainvoke(_state(symbol="mu")))

    assert final["symbol"] == "MU", "归一化后的代码要回写 state，供下游 news/compute 复用"
    assert final["preflight"]["bars"] == 17

    phases = [p.get("phase") for p in calls.progress_writes]
    assert "preflight" in phases, "预检必须发进度事件（填满开跑瞬间的 SSE 空档）"
    details = [p.get("detail", "") for p in calls.progress_writes if p.get("phase") == "preflight"]
    assert any("行情数据校验通过" in d for d in details)


# ════════════════════════════════════════════════════════════════════════════
# T-PRED-GRAPH-03: compute 失败 → analyze 不跑
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_compute_error_short_circuits_analyze(calls, monkeypatch):
    """compute 拉行情失败 → 条件边短路到 END，analyze 的 LLM 一个 token 都不烧。

    旧行为：compute 已 error，analyze 仍跑一次完整 LLM —— 用户先看到分析哗哗流出来，
    几十秒后才弹一个红色报错。
    """
    async def _boom(symbol, market, lookback, **kw):
        calls.fetch_bars += 1
        raise MarketDataError(KIND_RATE_LIMITED, "数据源限流（雅虎财经 HTTP 429）")

    monkeypatch.setattr(compute_mod, "fetch_bars", _boom)

    final = asyncio.run(pa.predict_graph.ainvoke(_state()))

    assert calls.news == 1, "预检通过 → 资讯搜索照跑"
    assert calls.fetch_bars == 1
    assert final["error"] == "数据源限流（雅虎财经 HTTP 429）"
    assert final["error_kind"] == KIND_RATE_LIMITED

    # ★ analyze 没跑
    assert calls.llm == 0, "compute 已 error，analyze 不应再烧一次完整 LLM"
    assert calls.forecast == 0
    assert not final.get("analysis")


@pytest.mark.unit
def test_news_all_failed_short_circuits_compute_and_analyze(calls, monkeypatch):
    """资讯全维失败（NewsSearchUnavailable）→ error 透传，compute/analyze 都短路。"""
    from graph.prediction.news_search import NewsSearchUnavailable

    async def _boom(symbol, market, prediction_id, dims=None, **kw):
        calls.news += 1
        raise NewsSearchUnavailable(
            "资讯搜索全部失败（网络/Tavily 不可用）：连接失败",
            failures=[{"dimension": "macro_policy", "failed": True, "error": "连接失败"}],
        )

    monkeypatch.setattr(news_mod, "gather_all_dimensions", _boom)

    final = asyncio.run(pa.predict_graph.ainvoke(_state()))

    assert calls.news == 1
    assert "资讯搜索全部失败" in final["error"]
    assert final["error_kind"] == news_mod.ERROR_KIND_NEWS_UNAVAILABLE
    # ComputeNode 入口的 `if state.get("error")` 双保险 + 条件边：都不跑
    assert calls.fetch_bars == 0, "资讯全挂后不应再拉行情"
    assert calls.llm == 0, "资讯全挂后不应烧 analyze LLM"


# ════════════════════════════════════════════════════════════════════════════
# T-PRED-GRAPH-04: 全链成功
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_happy_path_runs_all_four_nodes(calls):
    """四节点依次跑完：preflight → news_search → compute → analyze。"""
    final = asyncio.run(pa.predict_graph.ainvoke(_state()))

    assert calls.preflight == 1
    assert calls.news == 1
    assert calls.fetch_bars == 1
    assert calls.forecast == 1
    assert calls.llm == 1

    assert not final.get("error")
    assert final["news_sentiment"] == pytest.approx(0.3)
    assert final["forecast"]["model_name"] == "ARIMA+GARCH"
    assert final["analysis"] == "综合分析"
    assert final["risk_notes"] == ["风险A"]
    assert final["bars_summary"]["bars_count"] == 40

    # 状态流转（铁律 #6：节点内经 PredictionSM 校验后写库）：queued → news_searching → inferring
    assert [s for _, s in calls.status_writes] == ["news_searching", "inferring"]


# ════════════════════════════════════════════════════════════════════════════
# T-PRED-GRAPH-05 / 06: 路由函数 + on_chain_end 白名单
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_route_functions():
    """条件边函数：有 error 走 END，无 error 走下一节点。"""
    assert pa._route_after_preflight({"error": "限流"}) == END
    assert pa._route_after_preflight({}) == "news_search"
    assert pa._route_after_compute({"error": "限流"}) == END
    assert pa._route_after_compute({}) == "analyze"
    # 空字符串 error（节点未失败）不能被当成失败
    assert pa._route_after_preflight({"error": ""}) == "news_search"


@pytest.mark.unit
def test_preflight_in_on_chain_end_whitelist():
    """_GRAPH_NODE_NAMES 必须含 "preflight" —— 漏了它，预检失败时 _run_prediction_graph
    抓不到 final_state.error，会把失败当成功收尾落 done（最容易漏的一条）。"""
    assert "preflight" in pa._GRAPH_NODE_NAMES
    assert set(pa._GRAPH_NODE_NAMES) == {"preflight", "news_search", "compute", "analyze"}


@pytest.mark.unit
def test_predict_state_declares_error_kind_and_preflight():
    """PredictState 必须声明 error_kind / preflight：LangGraph 只把 TypedDict 里声明过的
    key 写回 channel，未声明的会被**静默丢弃**（不报错、也不生效）。"""
    keys = pa.PredictState.__annotations__
    assert "error_kind" in keys, "error_kind 未声明 → ERROR 事件里永远没有 kind"
    assert "preflight" in keys
    assert "error" in keys


# ════════════════════════════════════════════════════════════════════════════
# T-PRED-GRAPH-07: _transition_status —— 合法补齐遗漏中间态（状态卡死回归）
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_transition_status_reconciles_missing_intermediate_states(monkeypatch):
    """★ 回归：中间态转换丢失（DB 里还停在 queued）时，SM 会拒绝 queued→done。

    PredictionSM.send_event 对非法转换是「logger.warning + 返回当前值」而不是抛异常，
    所以不能直接写 send_event 的返回值 —— 那会把 queued 原样写回，任务永远卡在 queued：
      · /active 一直把它当成进行中
      · stream_predict 把它当"worker 重启遗留"，整单重跑一遍
    图已完成时沿合法 FSM 路径补齐中间态；禁止绕过状态机直接强写终态，
    否则旧图的晚到结果也会覆盖用户刚写入的 paused/stopped。
    """
    written: list[tuple[str, str, str | None]] = []

    async def _fake_get_snap(sid):
        return SimpleNamespace(status="queued")   # 中间态转换丢失

    async def _fake_update(sid, status, error_message=None):
        written.append((sid, status, error_message))

    monkeypatch.setattr(pa, "get_prediction_snapshot", _fake_get_snap)
    monkeypatch.setattr(pa, "update_prediction_status", _fake_update)

    asyncio.run(pa._transition_status("pred_x", "done"))

    assert written == [("pred_x", status, None) for status in
                       ("news_searching", "inferring", "done")]


@pytest.mark.unit
def test_transition_status_normal_path(monkeypatch):
    """合法转换（inferring → done）照常经 SM 校验后写库。"""
    written: list[tuple[str, str]] = []

    async def _fake_get_snap(sid):
        return SimpleNamespace(status="inferring")

    async def _fake_update(sid, status, error_message=None):
        written.append((sid, status))

    monkeypatch.setattr(pa, "get_prediction_snapshot", _fake_get_snap)
    monkeypatch.setattr(pa, "update_prediction_status", _fake_update)

    asyncio.run(pa._transition_status("pred_y", "done"))
    assert written == [("pred_y", "done")]


@pytest.mark.unit
def test_transition_status_forces_failed_from_queued(monkeypatch):
    """预检失败时 DB 还是 queued —— queued→failed 是合法转换，正常写入。"""
    written: list[tuple[str, str, str | None]] = []

    async def _fake_get_snap(sid):
        return SimpleNamespace(status="queued")

    async def _fake_update(sid, status, error_message=None):
        written.append((sid, status, error_message))

    monkeypatch.setattr(pa, "get_prediction_snapshot", _fake_get_snap)
    monkeypatch.setattr(pa, "update_prediction_status", _fake_update)

    asyncio.run(pa._transition_status("pred_z", "failed", error_message="数据源限流"))
    assert written == [("pred_z", "failed", "数据源限流")]


# ════════════════════════════════════════════════════════════════════════════
# T-PRED-GRAPH-08: _finalize_progress
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_finalize_progress_clears_on_success(monkeypatch):
    """done/stopped → progress 置 {}：否则记录列表会一直挂着「正在搜索 3/12」残留标签。"""
    written: list[dict] = []

    async def _fake_progress(sid, payload):
        written.append(payload)

    monkeypatch.setattr(pa, "update_prediction_progress", _fake_progress)

    asyncio.run(pa._finalize_progress("pred_done"))
    assert written == [{}], f"终态必须清空 progress，实际 {written}"


@pytest.mark.unit
def test_finalize_progress_records_error_kind_without_phase(monkeypatch):
    """failed → progress 写 {"error_kind": ...} 且**不带 phase**。

    前端 hasPredictionProgress() 是按「有没有 phase」判"有无进行中的阶段"的，
    带上 phase 会让失败的记录在列表里被渲染成"还在跑"。
    """
    written: list[dict] = []

    async def _fake_progress(sid, payload):
        written.append(payload)

    monkeypatch.setattr(pa, "update_prediction_progress", _fake_progress)

    asyncio.run(pa._finalize_progress("pred_failed", error_kind=KIND_RATE_LIMITED))

    assert written == [{"error_kind": KIND_RATE_LIMITED}]
    assert "phase" not in written[0], "error_kind 载荷不能带 phase（否则前端渲染成进行中）"
