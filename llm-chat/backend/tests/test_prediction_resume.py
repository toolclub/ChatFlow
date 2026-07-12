"""股票预测 RESUME（后台图 + event_log 回放）—— 单元测试

验证 graph/prediction_agent.py 的 resume 机制（镜像 chat resume 模式）：
  T-PRED-RESUME-01: 终态回放 — snap.status='done' 且 event_log 含 done 事件 →
                    stream_predict(id, 0) 回放全部事件后短路 return，不查快照、不启图。
  T-PRED-RESUME-02: after_event_id 偏移 — 仅回放 id > after_event_id 的事件（不重复 1,2）。
  T-PRED-RESUME-03: 后台启动 — start_prediction_graph 建 session + 后台 task；
                    astream_events 产出经 _emit 落 event_log（append_event）+ 入队，
                    图结束落 done 哨兵并清理 _SESSIONS。

隔离策略（对齐 test_quant_analyze.py / test_prediction_fsm.py）：
  - 无真实 DB/Redis/远程模型：monkeypatch graph.prediction_agent 模块级
    get_events_since / append_event / get_prediction_snapshot / update_prediction_status /
    update_prediction_analysis / predict_graph / db.redis_state.register|unregister_streaming。
  - 全 async（铁律 #4），用 asyncio.run 驱动；事件类型用 SSEEventType.*.value（铁律 #3）。
  - 异常不吞（铁律 #9）：测试只读产出，不验证 warning。
运行：pytest tests/test_prediction_resume.py -m unit -v
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

# 延迟 import：避免 collection 期触发重依赖（与 test_prediction_fsm.py 一致），方法内取。
# graph.prediction_agent 在 venv 内实测可 import（statsmodels/arch 在子模块内 lazy import）。


def _evt_row(eid: int, event_type: str, payload: dict) -> dict:
    """构造一行 event_log（与 db.event_store.get_events_since 返回结构一致）。"""
    return {
        "id": eid,
        "event_type": event_type,
        "sse_string": json.dumps(payload, ensure_ascii=False),
        "event_data": payload,
        "created_at": 0.0,
    }


# ═══════════════════════════════════════════════════════════════════════════
# T-PRED-RESUME-01: 终态回放 — 短路、不启图
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.unit
def test_terminal_replay_yields_events_no_graph(monkeypatch):
    """snap.status='done' + event_log 含 done 事件 → 回放全部事件后短路 return；
    不进入 LIVE 阶段（get_prediction_snapshot / start_prediction_graph 均不调用）。"""
    import graph.prediction_agent as pa
    from fsm.sse_events import SSEEventType

    events = [
        _evt_row(1, SSEEventType.THINKING.value,
                 {"event": SSEEventType.THINKING.value, "text": "推理"}),
        _evt_row(2, SSEEventType.NEWS_ITEM.value,
                 {"event": SSEEventType.NEWS_ITEM.value, "title": "新闻"}),
        _evt_row(3, SSEEventType.DONE.value,
                 {"event": SSEEventType.DONE.value, "analysis": "完成"}),
    ]

    get_events_calls: list[dict] = []

    async def _fake_get_events(conv_id, after_id=0, message_id=""):
        get_events_calls.append({"conv_id": conv_id, "after_id": after_id})
        # 铁律 #1：过滤由 event_log 完成（stream_predict 信任 get_events_since 结果）
        return [e for e in events if e["id"] > after_id]

    monkeypatch.setattr(pa, "get_events_since", _fake_get_events)

    snap_calls = {"n": 0}

    async def _fake_get_snap(_sid):
        snap_calls["n"] += 1
        return SimpleNamespace(status="done")  # 终态快照（即便被读也应判终态）

    monkeypatch.setattr(pa, "get_prediction_snapshot", _fake_get_snap)

    start_calls = {"n": 0}

    async def _fake_start(_sid, _state):
        start_calls["n"] += 1
        return None

    monkeypatch.setattr(pa, "start_prediction_graph", _fake_start)
    monkeypatch.setattr(pa, "_SESSIONS", {})  # 无 live session

    async def _run():
        out: list[dict] = []
        async for ev in pa.stream_predict("pred_done", 0):
            out.append(ev)
        return out

    out = asyncio.run(_run())

    types = [e["event"] for e in out]
    assert types == [
        SSEEventType.THINKING.value,
        SSEEventType.NEWS_ITEM.value,
        SSEEventType.DONE.value,
    ], f"回放事件顺序错误: {types}"
    assert out[-1]["analysis"] == "完成"

    # after_event_id 正确透传给 event_log
    assert get_events_calls and get_events_calls[0]["after_id"] == 0
    assert get_events_calls[0]["conv_id"] == "pred_done"

    # has_terminal 短路：LIVE 阶段未执行（不查快照、不启图）
    assert snap_calls["n"] == 0, "终态回放不应查询快照"
    assert start_calls["n"] == 0, "终态回放不应启动新图"


# ═══════════════════════════════════════════════════════════════════════════
# T-PRED-RESUME-02: after_event_id 偏移 — 仅回放 id > after_event_id
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.unit
def test_after_event_id_replays_only_onward(monkeypatch):
    """events id=1,2,3；stream_predict(id, after_event_id=2) 仅 yield id=3（done），
    不重复 id=1,2（前端刷新恢复时跳过已收到的事件）。"""
    import graph.prediction_agent as pa
    from fsm.sse_events import SSEEventType

    events = [
        _evt_row(1, SSEEventType.THINKING.value,
                 {"event": SSEEventType.THINKING.value, "text": "evt1"}),
        _evt_row(2, SSEEventType.NEWS_ITEM.value,
                 {"event": SSEEventType.NEWS_ITEM.value, "title": "evt2"}),
        _evt_row(3, SSEEventType.DONE.value,
                 {"event": SSEEventType.DONE.value, "analysis": "evt3"}),
    ]

    captured: dict = {}

    async def _fake_get_events(conv_id, after_id=0, message_id=""):
        captured["after_id"] = after_id
        captured["conv_id"] = conv_id
        return [e for e in events if e["id"] > after_id]

    monkeypatch.setattr(pa, "get_events_since", _fake_get_events)

    snap_calls = {"n": 0}

    async def _fake_get_snap(_sid):
        snap_calls["n"] += 1
        return SimpleNamespace(status="done")

    monkeypatch.setattr(pa, "get_prediction_snapshot", _fake_get_snap)
    monkeypatch.setattr(pa, "start_prediction_graph",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("不应启图")))
    monkeypatch.setattr(pa, "_SESSIONS", {})

    async def _run():
        out: list[dict] = []
        async for ev in pa.stream_predict("pred_offset", 2):
            out.append(ev)
        return out

    out = asyncio.run(_run())

    # after_event_id=2 透传到 event_log 查询
    assert captured["after_id"] == 2
    assert captured["conv_id"] == "pred_offset"

    # 仅回放 id=3（done），不含 id=1,2
    assert len(out) == 1, f"应只回放 1 条（id=3），实际 {len(out)}"
    assert out[0]["event"] == SSEEventType.DONE.value
    assert out[0]["analysis"] == "evt3"

    # 不混入 id=1,2 的内容
    blob = json.dumps(out, ensure_ascii=False)
    assert "evt1" not in blob, "after_event_id=2 不应回放 id=1"
    assert "evt2" not in blob, "after_event_id=2 不应回放 id=2"

    # done 短路：未查快照、未启图
    assert snap_calls["n"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# T-PRED-RESUME-03: 后台启动 — session + event_log 增量持久化
# ═══════════════════════════════════════════════════════════════════════════
class _FakePredictGraph:
    """假 predict_graph：astream_events 产出固定 raw 事件序列（thinking/news/final_state）。"""

    async def astream_events(self, state, version="v2", config=None):
        from fsm.sse_events import SSEEventType

        # 1) emit_thinking 派发的 llm_thinking 自定义事件 → 映射 thinking
        yield {
            "event": "on_custom_event",
            "name": "llm_thinking",
            "data": {"phase": "reasoning", "delta": "思考中"},
        }
        # 2) 节点派发的 prediction_event → 原样转发（type 已是枚举值）
        yield {
            "event": "on_custom_event",
            "name": "prediction_event",
            "data": {"type": SSEEventType.NEWS_ITEM.value, "title": "新闻A"},
        }
        # 3) 图结束：捕获 final_state（analysis/risk_notes/forecast）
        yield {
            "event": "on_chain_end",
            "name": "LangGraph",
            "data": {
                "output": {
                    "analysis": "综合分析",
                    "risk_notes": ["风险A"],
                    "forecast": {"price": 10.5},
                }
            },
        }


@pytest.mark.unit
def test_start_prediction_graph_background(monkeypatch):
    """start_prediction_graph 建 session + 后台 task；astream_events 产出经 _emit
    落 event_log（append_event 每事件一次）+ 入队；图结束落 done 哨兵并清理 _SESSIONS。"""
    import graph.prediction_agent as pa
    from fsm.sse_events import SSEEventType

    monkeypatch.setattr(pa, "predict_graph", _FakePredictGraph())

    appended: list[dict] = []

    async def _fake_append(conv_id, event_type, event_data=None, sse_string="", message_id=""):
        eid = len(appended) + 1  # 自增 eid（与真实 event_log 一致）
        appended.append({
            "conv_id": conv_id,
            "event_type": event_type,
            "sse_string": sse_string,
            "message_id": message_id,
        })
        return eid

    monkeypatch.setattr(pa, "append_event", _fake_append)

    async def _fake_update_analysis(_sid, analysis, risk_notes, thinking_segments=None):
        return None

    monkeypatch.setattr(pa, "update_prediction_analysis", _fake_update_analysis)

    async def _fake_get_snap(_sid):
        # _transition_status 从 DB 读当前态：inferring → done 合法转换
        return SimpleNamespace(status="inferring")

    monkeypatch.setattr(pa, "get_prediction_snapshot", _fake_get_snap)

    async def _fake_update_status(_sid, _status, error_message=None):
        return None

    monkeypatch.setattr(pa, "update_prediction_status", _fake_update_status)

    # Redis 活跃会话注册/注销 — no-op（跨 worker，单测不依赖）
    async def _noop(*_a, **_kw):
        return None

    monkeypatch.setattr("db.redis_state.register_streaming", _noop)
    monkeypatch.setattr("db.redis_state.unregister_streaming", _noop)

    state = pa.build_initial_state(
        snapshot_id="pred_bg",
        client_id="admin",
        user_id="",
        criteria={"market": "cn_a", "symbol": "000001", "horizon": 5},
        status="queued",
    )

    async def _run():
        session = await pa.start_prediction_graph("pred_bg", state)
        # session 已注册
        assert pa._SESSIONS.get("pred_bg") is session

        drained: list[dict] = []
        async for ev in session.drain(skip_eid=0):
            drained.append(ev)

        # 图任务应已结束（_finish_session 是最后一步）；await 兜底
        if session.graph_task and not session.graph_task.done():
            await session.graph_task
        return drained, session

    drained, session = asyncio.run(_run())

    types = [e["event"] for e in drained]
    # thinking + news_item + done（按产出顺序）
    assert types == [
        SSEEventType.THINKING.value,
        SSEEventType.NEWS_ITEM.value,
        SSEEventType.DONE.value,
    ], f"drain 事件顺序错误: {types}"
    assert drained[0]["text"] == "思考中"
    assert drained[1]["title"] == "新闻A"
    # done 携带最终结构化字段（铁律 #2：独立字段）
    assert drained[2]["analysis"] == "综合分析"
    assert drained[2]["risk_notes"] == ["风险A"]
    assert drained[2]["forecast"] == {"price": 10.5}

    # append_event 每个事件调用一次（thinking + news_item + done = 3）
    assert len(appended) == 3, f"应 append 3 次，实际 {len(appended)}"
    appended_types = [a["event_type"] for a in appended]
    assert appended_types == [
        SSEEventType.THINKING.value,
        SSEEventType.NEWS_ITEM.value,
        SSEEventType.DONE.value,
    ]
    # conv_id / message_id 绑定到 prediction_id（event_log 真相源）
    for a in appended:
        assert a["conv_id"] == "pred_bg"
        assert a["message_id"] == "pred_bg"

    # 终态哨兵已放、session 已从注册表清理
    assert session.terminal is True
    assert "pred_bg" not in pa._SESSIONS
