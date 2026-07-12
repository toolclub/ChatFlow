"""暂停 / 续跑 / 删除（graph/prediction_agent.py + news_search_node）—— 单元测试

  T-PRED-PAUSE-01: remaining_dimensions —— 已完成/失败/未跑 的判据（失败维度必须重跑）
  T-PRED-PAUSE-02: pause_prediction —— cancel 图任务 + 置 paused（非终态），终态任务拒绝暂停
  T-PRED-PAUSE-03: 图被 cancel 且 reason=paused → 发 PAUSED 事件（不是 STOPPED）
  T-PRED-PAUSE-04: resume_prediction —— 按剩余工作量选转换事件 + state.resume=True
  T-PRED-PAUSE-05: resume 只能从 paused 出发（其它状态拒绝）
  T-PRED-PAUSE-06: stream_predict 遇到 paused 快照**不重跑图**（否则一刷新暂停就被自动撤销）
  T-PRED-PAUSE-07: news_search 节点续跑跳过已完成维度，只把剩余维度交给 gather
  T-PRED-PAUSE-08: 失败的维度算"未完成"，续跑时会重跑

隔离：无 DB / 无网络 / 无 LLM。全部 patch 模块级叶子依赖。
运行：pytest tests/test_prediction_pause_resume.py -m unit -v
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import graph.nodes.prediction.news_search_node as news_mod
import graph.prediction_agent as pa
from fsm.sse_events import SSEEventType


def _snap(status: str = "news_searching", news=None, filings=None, dims=None):
    return SimpleNamespace(
        id="pred_p1",
        client_id="admin",
        user_id="",
        market="cn_a",
        symbol="000001.SZ",
        status=status,
        criteria={"market": "cn_a", "symbol": "000001.SZ", "horizon": 7,
                  "dimensions": dims or ["macro_policy", "negative_event", "supply_chain"]},
        news_digest=news or [],
        filing_digest=filings or [],
        progress={},
    )


@pytest.fixture
def store(monkeypatch):
    """把 prediction_agent 的 DB / Redis 依赖换成内存替身。"""
    state = {"snap": _snap(), "statuses": [], "started": []}

    async def _get_snap(sid):
        return state["snap"]

    async def _update_status(sid, status, error_message=None):
        state["statuses"].append(status)
        state["snap"].status = status

    async def _update_progress(sid, payload):
        return None

    async def _start_graph(sid, st):
        state["started"].append(dict(st))
        return pa.PredictionSession(sid)

    monkeypatch.setattr(pa, "get_prediction_snapshot", _get_snap)
    monkeypatch.setattr(pa, "update_prediction_status", _update_status)
    monkeypatch.setattr(pa, "update_prediction_progress", _update_progress)
    monkeypatch.setattr(pa, "start_prediction_graph", _start_graph)
    return state


# ════════════════════════════════════════════════════════════════════════════
# T-PRED-PAUSE-01: 哪些维度算"已完成"
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_remaining_dimensions_failed_counts_as_not_done():
    """失败的维度算未完成 —— 否则一次 Tavily 抖动会被永久固化成「该维无资讯」。"""
    snap = _snap(
        news=[
            {"dimension": "macro_policy", "failed": False},
            {"dimension": "supply_chain", "failed": True, "error": "连接失败"},
        ],
        filings=[{"dimension": "negative_event", "kind": "data", "failed": False}],
    )
    completed, remaining = pa.remaining_dimensions(snap)
    assert completed == ["macro_policy", "negative_event"]
    assert remaining == ["supply_chain"], "失败维度必须重跑"


@pytest.mark.unit
def test_remaining_dimensions_keeps_user_order():
    snap = _snap(news=[{"dimension": "supply_chain", "failed": False}])
    completed, remaining = pa.remaining_dimensions(snap)
    assert remaining == ["macro_policy", "negative_event"]  # 跟随 criteria.dimensions 顺序


# ════════════════════════════════════════════════════════════════════════════
# T-PRED-PAUSE-02/03: 暂停
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_pause_cancels_graph_and_marks_paused(store, monkeypatch):
    async def _run():
        session = pa.PredictionSession("pred_p1")

        async def _forever():
            await asyncio.sleep(30)

        session.graph_task = asyncio.create_task(_forever())
        pa._SESSIONS["pred_p1"] = session
        try:
            res = await pa.pause_prediction("pred_p1")
        finally:
            pa._SESSIONS.pop("pred_p1", None)
        await asyncio.sleep(0)
        return res, session

    res, session = asyncio.run(_run())
    assert res["ok"] is True
    assert res["status"] == "paused"
    assert store["statuses"][-1] == "paused"
    assert session.cancel_reason == pa._CANCEL_PAUSED
    assert session.graph_task.cancelled() or session.graph_task.done()
    # ★ pause 不能像 stop 那样立刻置 terminal：否则 PAUSED 事件赶不上这条 SSE 流
    assert session.terminal is False


@pytest.mark.unit
def test_pause_rejected_on_terminal_status(store):
    store["snap"] = _snap(status="done")
    res = asyncio.run(pa.pause_prediction("pred_p1"))
    assert res["ok"] is False
    assert "done" in res["error"]
    assert store["statuses"] == [], "终态任务不该再写状态"


@pytest.mark.unit
def test_cancelled_graph_emits_paused_not_stopped(monkeypatch):
    """图被 cancel 且 reason=paused → PAUSED 事件（铁律 #3 枚举），不是 STOPPED。"""
    emitted: list[dict] = []

    async def _fake_append(**kw):
        emitted.append(kw["event_data"])
        return len(emitted)

    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(pa, "append_event", _fake_append)
    monkeypatch.setattr(pa, "_finalize_progress", _noop)
    monkeypatch.setattr("db.redis_state.unregister_streaming", _noop, raising=False)

    async def _run():
        session = pa.PredictionSession("pred_p2")
        session.cancel_reason = pa._CANCEL_PAUSED

        async def _boom_graph():
            raise asyncio.CancelledError()

        # 直接驱动 _run_prediction_graph 的取消分支（astream 内部抛 CancelledError）
        class _FakeGraph:
            def astream_events(self, *a, **kw):
                async def _gen():
                    raise asyncio.CancelledError()
                    yield  # pragma: no cover
                return _gen()

        monkeypatch.setattr(pa, "predict_graph", _FakeGraph())
        await pa._run_prediction_graph(session, {"snapshot_id": "pred_p2"})
        return session

    session = asyncio.run(_run())
    kinds = [e.get("event") for e in emitted]
    assert SSEEventType.PAUSED.value in kinds
    assert SSEEventType.STOPPED.value not in kinds
    assert session.terminal is True     # _finish_session 收口


# ════════════════════════════════════════════════════════════════════════════
# T-PRED-PAUSE-04/05: 续跑
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.parametrize(
    "news,filings,expect",
    [
        # 一维都没跑完 → 整单重来（resume → queued）
        ([], [], "queued"),
        # 还有维度没跑完 → resume_news → news_searching
        ([{"dimension": "macro_policy", "failed": False}], [], "news_searching"),
        # 维度全跑完 → resume_infer → inferring
        (
            [{"dimension": "macro_policy", "failed": False},
             {"dimension": "supply_chain", "failed": False}],
            [{"dimension": "negative_event", "kind": "data", "failed": False}],
            "inferring",
        ),
    ],
)
def test_resume_picks_transition_by_remaining_work(store, news, filings, expect):
    store["snap"] = _snap(status="paused", news=news, filings=filings)
    res = asyncio.run(pa.resume_prediction("pred_p1"))
    assert res["ok"] is True
    assert res["status"] == expect
    assert store["statuses"][-1] == expect
    # 图必须以 resume=True 重启（news_search 节点据此读回已完成维度）
    assert store["started"] and store["started"][-1]["resume"] is True
    # dimensions 始终传完整勾选集：跑哪些由节点按 DB 做差决定（单一判据）
    assert store["started"][-1]["dimensions"] == [
        "macro_policy", "negative_event", "supply_chain",
    ]


@pytest.mark.unit
def test_resume_rejected_when_not_paused(store):
    store["snap"] = _snap(status="news_searching")
    res = asyncio.run(pa.resume_prediction("pred_p1"))
    assert res["ok"] is False
    assert store["started"] == [], "非 paused 不得重启图"


# ════════════════════════════════════════════════════════════════════════════
# T-PRED-PAUSE-06: paused 快照不得被 /stream 自动重跑
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_stream_predict_does_not_rerun_paused(store, monkeypatch):
    store["snap"] = _snap(status="paused")
    pa._SESSIONS.pop("pred_p1", None)

    async def _no_events(*a, **kw):
        return []

    monkeypatch.setattr(pa, "get_events_since", _no_events)

    async def _run():
        return [ev async for ev in pa.stream_predict("pred_p1", 0)]

    events = asyncio.run(_run())
    assert store["started"] == [], "paused 被 /stream 自动重跑 = 用户点的暂停被撤销"
    assert events and events[-1]["event"] == SSEEventType.PAUSED.value


# ════════════════════════════════════════════════════════════════════════════
# T-PRED-PAUSE-07/08: 节点续跑跳过已完成维度
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_news_node_skips_completed_dimensions(monkeypatch):
    seen: dict = {}

    async def _fake_gather(symbol, market, pid, dims=None, **kw):
        seen["dims"] = list(dims or [])
        return {
            "news_digest": [{"dimension": "supply_chain", "sentiment": -0.5, "failed": False}],
            "news_sentiment": -0.5,
            "filing_digest": [],
        }

    async def _fake_get_snap(sid):
        return _snap(
            status="paused",
            news=[
                {"dimension": "macro_policy", "sentiment": 0.5, "failed": False},
                {"dimension": "supply_chain", "failed": True, "error": "超时"},
            ],
            filings=[{"dimension": "negative_event", "kind": "data",
                      "score": -0.3, "failed": False}],
        )

    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(news_mod, "gather_all_dimensions", _fake_gather)
    monkeypatch.setattr(news_mod, "get_prediction_snapshot", _fake_get_snap)
    for fn in ("update_prediction_status", "update_prediction_progress",
               "update_prediction_news", "update_prediction_filings",
               "append_prediction_dimension", "append_prediction_tool_trace"):
        monkeypatch.setattr(news_mod, fn, _noop)

    state = pa.build_initial_state(
        "pred_p1", "admin", "",
        {"market": "cn_a", "symbol": "000001.SZ", "horizon": 7,
         "dimensions": ["macro_policy", "negative_event", "supply_chain"]},
        status="news_searching",
        resume=True,
    )
    out = asyncio.run(news_mod.NewsSearchNode().execute(state))

    # 只有失败的 supply_chain 重跑；已完成的两维一次外部调用都不发
    assert seen["dims"] == ["supply_chain"]
    dims_out = [d["dimension"] for d in out["news_digest"]]
    assert dims_out == ["macro_policy", "supply_chain"], "seed 与新结果合并且按勾选顺序"
    assert [d["dimension"] for d in out["filing_digest"]] == ["negative_event"]
    # 情绪必须按「seed + 新结果」重算：gather 只看得见本轮跑过的那一维（-0.5）
    assert out["news_sentiment"] == pytest.approx(0.0)


@pytest.mark.unit
def test_news_node_all_done_skips_gather_entirely(monkeypatch):
    called = {"gather": 0}

    async def _fake_gather(*a, **kw):
        called["gather"] += 1
        return {"news_digest": [], "news_sentiment": 0.0, "filing_digest": []}

    async def _fake_get_snap(sid):
        return _snap(
            status="paused",
            news=[{"dimension": "macro_policy", "sentiment": 0.2, "failed": False},
                  {"dimension": "supply_chain", "sentiment": 0.4, "failed": False}],
            filings=[{"dimension": "negative_event", "kind": "data", "failed": False}],
        )

    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(news_mod, "gather_all_dimensions", _fake_gather)
    monkeypatch.setattr(news_mod, "get_prediction_snapshot", _fake_get_snap)
    for fn in ("update_prediction_status", "update_prediction_progress",
               "update_prediction_news", "update_prediction_filings",
               "append_prediction_dimension", "append_prediction_tool_trace"):
        monkeypatch.setattr(news_mod, fn, _noop)

    state = pa.build_initial_state(
        "pred_p1", "admin", "",
        {"market": "cn_a", "symbol": "000001.SZ", "horizon": 7,
         "dimensions": ["macro_policy", "negative_event", "supply_chain"]},
        status="inferring",
        resume=True,
    )
    out = asyncio.run(news_mod.NewsSearchNode().execute(state))
    assert called["gather"] == 0, "全部维度已完成时不该再打一次外部接口"
    assert out["news_sentiment"] == pytest.approx(0.3)


# ════════════════════════════════════════════════════════════════════════════
# T-PRED-DEL-01: 删除时被 cancel 的图不得再写 event_log（否则留下孤儿行）
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_discard_session_cancelled_graph_writes_nothing(monkeypatch):
    emitted: list[dict] = []

    async def _fake_append(**kw):
        emitted.append(kw["event_data"])
        return len(emitted)

    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(pa, "append_event", _fake_append)
    monkeypatch.setattr("db.redis_state.unregister_streaming", _noop, raising=False)

    async def _run():
        session = pa.PredictionSession("pred_del")
        pa._SESSIONS["pred_del"] = session
        await pa.discard_session("pred_del")

        class _FakeGraph:
            def astream_events(self, *a, **kw):
                async def _gen():
                    raise asyncio.CancelledError()
                    yield  # pragma: no cover
                return _gen()

        monkeypatch.setattr(pa, "predict_graph", _FakeGraph())
        await pa._run_prediction_graph(session, {"snapshot_id": "pred_del"})
        return session

    session = asyncio.run(_run())
    assert session.cancel_reason == pa._CANCEL_DELETED
    assert emitted == [], "快照行马上要被删，再写 event_log 就是永久孤儿行"
    assert "pred_del" not in pa._SESSIONS


@pytest.mark.unit
def test_resolve_dimensions_drops_invalid_and_cross_market():
    """A 股勾了美股专属维度（sec_filings）→ 静默丢弃，不是整单失败。"""
    dims = news_mod.resolve_dimensions(["macro_policy", "sec_filings", "不存在的维度"], "cn_a")
    assert dims == ["macro_policy"]
    # 全非法 → 回落该市场默认集（而不是跑 0 维）
    assert news_mod.resolve_dimensions(["垃圾"], "cn_a")
