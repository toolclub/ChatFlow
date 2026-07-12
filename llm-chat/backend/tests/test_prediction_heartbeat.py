"""预测 SSE 心跳（PredictionSession._heartbeat / drain）—— 单元测试

修复「卡住感」：预测的 SSE 链路原先没有心跳（聊天的 graph/runner/stream.py 有），
实测单次运行有 12~13 个 >15s 的静默空档，最长 198 秒 socket 零字节 —— 用户以为卡死。

  T-PRED-HB-01: session 启动后队列里周期性出现 ping
  T-PRED-HB-02: ★ ping 不写 event_log（append_event 从未被 ping 调用过）—— 关键坑回归测试
  T-PRED-HB-03: ping 不带 id（前端据此把它当"只刷新停滞计时器、不推进游标"的信号）
  T-PRED-HB-04: drain() 里 ping 不被 skip_eid 去重逻辑误吞（eid=0 永远不跳过）
  T-PRED-HB-05: drain() 轮询超时补发 ping（心跳任务万一挂了的双保险）
  T-PRED-HB-06: _finish_session / stop() 取消心跳任务（不留悬挂 task）
  T-PRED-HB-07: 真实事件经 _emit 落 event_log 并带上自增 eid（与 ping 形成对照）

★ T-PRED-HB-02 的由来：ping 若走 _emit()，一次十几分钟的预测会往 event_log 攒下几百条
  垃圾 ping 行，并在**每次重连 replay 时被全量重放**。chat 侧 stream.py:384 就是这么防的。

隔离策略：
  - 无 DB / 无 Redis：monkeypatch pa.append_event + db.redis_state.heartbeat_streaming。
    heartbeat_streaming 必须桩掉 —— 心跳里对它有 wait_for(timeout=2)，不桩会让每个
    用例慢 2 秒（且真去连 Redis）。
  - 心跳间隔 / drain 轮询超时被调到毫秒级（模块级常量，运行时查找 → monkeypatch 生效）。
运行：pytest tests/test_prediction_heartbeat.py -m unit -v
"""
from __future__ import annotations

import asyncio

import pytest

import graph.prediction_agent as pa
from fsm.sse_events import SSEEventType


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    """心跳每拍都会给 Redis 会话续期（wait_for timeout=2）—— 桩掉，否则单测又慢又要连 Redis。"""
    async def _noop(*_a, **_kw):
        return None

    monkeypatch.setattr("db.redis_state.heartbeat_streaming", _noop, raising=False)
    monkeypatch.setattr("db.redis_state.register_streaming", _noop, raising=False)
    monkeypatch.setattr("db.redis_state.unregister_streaming", _noop, raising=False)


@pytest.fixture
def appended(monkeypatch) -> list[dict]:
    """捕获 event_log 写入（append_event），返回自增 eid。"""
    rows: list[dict] = []

    async def _fake_append(conv_id, event_type, event_data=None, sse_string="", message_id=""):
        rows.append({"conv_id": conv_id, "event_type": event_type, "event_data": event_data})
        return len(rows)  # 自增 eid（与真实 event_log 一致）

    monkeypatch.setattr(pa, "append_event", _fake_append)
    return rows


@pytest.fixture(autouse=True)
def _fast_heartbeat(monkeypatch):
    """把 3s 心跳压到 20ms，把 5s drain 轮询超时压到 50ms（单测跑得完）。"""
    monkeypatch.setattr(pa, "HEARTBEAT_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(pa, "_DRAIN_POLL_TIMEOUT_SECONDS", 0.05)


# ════════════════════════════════════════════════════════════════════════════
# T-PRED-HB-01 / 02 / 03: 心跳周期性 ping + 不落库 + 不带 id
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_heartbeat_emits_pings_and_never_touches_event_log(appended):
    """★ 核心：静默期内队列周期性出现 ping，且 append_event 一次都没被 ping 调用。"""

    async def _run():
        session = pa.PredictionSession("pred_hb")

        async def _silent_graph():
            # 模拟一段长静默（LLM 长思考 / Tavily 往返）：图不产任何事件
            await asyncio.sleep(0.12)
            await pa._finish_session(session)

        session.start(_silent_graph())

        drained: list[dict] = []
        async for ev in session.drain(skip_eid=0):
            drained.append(ev)
        if session.graph_task and not session.graph_task.done():
            await session.graph_task
        return drained, session

    drained, session = asyncio.run(_run())

    pings = [e for e in drained if e["event"] == SSEEventType.PING.value]
    # 120ms 静默 / 20ms 心跳 → 至少 2 个 ping（放宽下界，避免调度抖动 flake）
    assert len(pings) >= 2, (
        f"静默期内心跳未按周期 ping（实得 {len(pings)} 个）—— SSE 链路会重现 198s 零字节空档"
    )

    # ★★ 关键坑：ping 绝不能进 event_log ★★
    ping_rows = [r for r in appended if r["event_type"] == SSEEventType.PING.value]
    assert ping_rows == [], (
        f"ping 被写进了 event_log（{len(ping_rows)} 条）—— 一次预测会攒下几百条垃圾行，"
        "且每次重连 replay 都会被全量重放"
    )

    # ping 不带 id：前端据此判定"只刷新停滞计时器、不推进 after_event_id 游标"
    for p in pings:
        assert "id" not in p, f"ping 不能带 id，否则前端游标逻辑和重放都会坏：{p}"
        assert p == {"event": "ping"}

    assert session.terminal is True


@pytest.mark.unit
def test_ping_event_uses_enum_not_literal():
    """铁律 #3：ping 事件类型取自 SSEEventType 枚举，不硬编码字符串。"""
    assert pa._ping_event() == {"event": SSEEventType.PING.value}
    assert SSEEventType.PING.value == "ping"
    assert "id" not in pa._ping_event()


# ════════════════════════════════════════════════════════════════════════════
# T-PRED-HB-04: ping 不被 skip_eid 去重误吞
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_ping_survives_skip_eid_dedup(appended):
    """重连时 drain(skip_eid=N) 会跳过已 replay 的事件；ping 的 eid=0，永远不能被跳过。

    否则重连后的客户端会一个 ping 都收不到 → 停滞检测误判链路已死。
    """
    async def _run():
        session = pa.PredictionSession("pred_skip")
        # 手工塞入：一个"已 replay 过"的旧事件（eid=1）+ 一个 ping（eid=0）
        await session.queue.put((1, {"event": SSEEventType.THINKING.value, "text": "旧", "id": 1}))
        await session.queue.put((0, pa._ping_event()))
        await session.queue.put((2, {"event": SSEEventType.DONE.value, "id": 2}))
        session.terminal = True
        await session.queue.put(None)

        return [ev async for ev in session.drain(skip_eid=1)]

    drained = asyncio.run(_run())
    types = [e["event"] for e in drained]

    assert SSEEventType.THINKING.value not in types, "eid=1 已 replay，应被 skip_eid 跳过"
    assert SSEEventType.PING.value in types, "ping（eid=0）不能被去重逻辑误吞"
    assert SSEEventType.DONE.value in types


# ════════════════════════════════════════════════════════════════════════════
# T-PRED-HB-05: drain 轮询超时补发 ping（双保险）
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_drain_timeout_yields_ping_not_bare_continue(appended):
    """心跳任务万一挂了：drain 的 asyncio.TimeoutError 分支也要 yield ping（原为裸 continue）。"""

    async def _run():
        session = pa.PredictionSession("pred_drain")
        session.graph_task = None       # 不启心跳，只测 drain 自己的兜底

        async def _late_finish():
            await asyncio.sleep(0.13)   # > 2 个 drain 轮询周期（50ms）
            session.terminal = True
            await session.queue.put(None)

        task = asyncio.create_task(_late_finish())
        drained = [ev async for ev in session.drain(skip_eid=0)]
        await task
        return drained

    drained = asyncio.run(_run())
    pings = [e for e in drained if e["event"] == SSEEventType.PING.value]
    assert len(pings) >= 2, f"drain 轮询超时应补发 ping（双保险），实得 {len(pings)}"
    # 兜底 ping 同样不落库
    assert appended == []


# ════════════════════════════════════════════════════════════════════════════
# T-PRED-HB-06: 心跳任务生命周期
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_finish_session_cancels_heartbeat(appended):
    """_finish_session 取消 hb_task —— 否则每个跑完的预测都留一个心跳 task 悬挂。"""

    async def _run():
        session = pa.PredictionSession("pred_fin")

        async def _graph():
            await asyncio.sleep(0.03)

        session.start(_graph())
        assert session.hb_task is not None and not session.hb_task.done()

        await pa._finish_session(session)
        await asyncio.sleep(0.01)       # 让 cancel 生效
        return session

    session = asyncio.run(_run())
    assert session.terminal is True
    assert session.hb_task.cancelled() or session.hb_task.done(), "心跳任务未被取消"


@pytest.mark.unit
def test_stop_cancels_heartbeat_and_graph(appended):
    """stop()（用户点停止）同时取消 graph_task + hb_task。"""

    async def _run():
        session = pa.PredictionSession("pred_stop_hb")

        async def _forever():
            await asyncio.sleep(10)

        session.start(_forever())
        await asyncio.sleep(0.01)
        session.stop()
        await asyncio.sleep(0.01)
        return session

    session = asyncio.run(_run())
    assert session.terminal is True
    assert session.graph_task.cancelled() or session.graph_task.done()
    assert session.hb_task.cancelled() or session.hb_task.done()


# ════════════════════════════════════════════════════════════════════════════
# T-PRED-HB-07: 真实事件走 _emit → 落库 + 带 eid（与 ping 形成对照）
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_real_events_are_persisted_with_incrementing_id(appended):
    """_emit 的事件必须落 event_log 且带自增 id —— 前端靠它推进 after_event_id 游标，
    重连时只拉增量，而不是把 4000+ 条 thinking 全量重放。"""

    async def _run():
        session = pa.PredictionSession("pred_emit")
        await pa._emit(session, {"event": SSEEventType.THINKING.value, "text": "推理"})
        await pa._emit(session, {
            "event": SSEEventType.PREDICTION_PROGRESS.value,
            "phase": "news_searching", "label": "地缘政治", "index": 3, "total": 12,
            "detail": "正在搜索：地缘政治",
        })
        out = []
        while not session.queue.empty():
            out.append(session.queue.get_nowait())
        return out

    out = asyncio.run(_run())

    eids = [eid for eid, _ in out]
    payloads = [ev for _, ev in out]
    assert eids == [1, 2], "每条落库事件都要拿到自增 eid"
    assert payloads[0]["id"] == 1 and payloads[1]["id"] == 2, "eid 必须回填进发给前端的 payload"

    # 对照：这两条进了 event_log，ping 不进
    assert [r["event_type"] for r in appended] == [
        SSEEventType.THINKING.value,
        SSEEventType.PREDICTION_PROGRESS.value,
    ]
    # 进度事件结构符合契约 F
    assert payloads[1]["phase"] == "news_searching"
    assert payloads[1]["index"] == 3 and payloads[1]["total"] == 12


@pytest.mark.unit
def test_emit_survives_event_log_failure(monkeypatch):
    """event_log 写失败不能阻塞图：eid=0 入队（不可被 skip_eid 跳过），铁律 #9 只 warning。"""

    async def _boom(**kw):
        raise RuntimeError("DB down")

    monkeypatch.setattr(pa, "append_event", _boom)

    async def _run():
        session = pa.PredictionSession("pred_dbfail")
        await pa._emit(session, {"event": SSEEventType.THINKING.value, "text": "x"})
        return session.queue.get_nowait()

    eid, ev = asyncio.run(_run())
    assert eid == 0, "写库失败的事件 eid=0 → drain 不会把它当成已 replay 而跳过"
    assert "id" not in ev
    assert ev["text"] == "x"


# ════════════════════════════════════════════════════════════════════════════
# T-PRED-HB-08 (v2): 6 个新事件全部带 eid + 全部进 event_log（ping 仍然不进）
#
# eid 是重连游标的唯一依据：前端 onEventId 记住最大 eid，重连时 after_event_id 传它。
# 新事件若不带 eid，用户刷新后这些卡片/蜡烛要么丢、要么被重复渲染。
# ════════════════════════════════════════════════════════════════════════════

_V2_EVENTS = [
    (SSEEventType.TOOL_TRACE, {"phase": "end", "tool": "fetch_reductions", "rows": 12}),
    (SSEEventType.DATA_ITEM, {"dimension": "negative_event", "item": {"title": "立案调查"}}),
    (SSEEventType.FORMULA_STEP, {"key": "garch", "stage": "garch", "result": 0.3}),
    (SSEEventType.FORECAST_DAY, {"day": 1, "date": "2026-07-13", "o": 10, "h": 11, "l": 9, "c": 10.5}),
    (SSEEventType.EVENT_MARKER, {"date": "2026-07-01", "event_type": "negative", "score": -0.9}),
    (SSEEventType.PAUSED, {"status": "paused", "message": "预测已暂停"}),
]


@pytest.mark.unit
def test_v2_events_all_carry_eid_and_persist(appended):
    """★ 6 个 v2 新事件经 _emit 后都必须带自增 eid 并落 event_log（可重放）。"""

    async def _run():
        session = pa.PredictionSession("pred_v2_eid")
        for etype, body in _V2_EVENTS:
            await pa._emit(session, {"event": etype.value, **body})
        out = []
        while not session.queue.empty():
            out.append(session.queue.get_nowait())
        return out

    items = asyncio.run(_run())

    assert len(items) == len(_V2_EVENTS)
    for (etype, _body), (eid, ev) in zip(_V2_EVENTS, items):
        assert ev["event"] == etype.value
        assert eid > 0, f"{etype.value} 没拿到 eid —— 重连游标会漏掉它"
        assert ev["id"] == eid, f"{etype.value} 的 eid 没回填进 payload（前端读的是 payload.id）"

    # eid 严格自增（前端 after_event_id 靠单调性推进游标）
    eids = [eid for eid, _ in items]
    assert eids == sorted(eids) and len(set(eids)) == len(eids)

    # 6 条全部进了 event_log（刷新重放拿得到）
    assert [r["event_type"] for r in appended] == [e.value for e, _ in _V2_EVENTS]


@pytest.mark.unit
def test_v2_event_types_come_from_enum():
    """铁律 #3：6 个新事件类型必须在 SSEEventType 里注册，不能是字面量。"""
    for etype, _ in _V2_EVENTS:
        assert isinstance(etype, SSEEventType)
        assert SSEEventType(etype.value) is etype


@pytest.mark.unit
def test_ping_still_never_persisted_alongside_v2_events(appended):
    """★ v1 的坑不能被 v2 破坏：新事件走 _emit 落库，ping 依旧只入队、绝不落库。"""

    async def _run():
        session = pa.PredictionSession("pred_v2_ping")
        await pa._emit(session, {"event": SSEEventType.FORECAST_DAY.value, "day": 1, "c": 10.5})
        # 心跳走的是 queue.put((0, ...)) 直塞，不走 _emit
        await session.queue.put((0, pa._ping_event()))
        await pa._emit(session, {"event": SSEEventType.FORECAST_DAY.value, "day": 2, "c": 10.7})
        out = []
        while not session.queue.empty():
            out.append(session.queue.get_nowait())
        return out

    items = asyncio.run(_run())

    kinds = [ev["event"] for _eid, ev in items]
    assert kinds == [
        SSEEventType.FORECAST_DAY.value, SSEEventType.PING.value,
        SSEEventType.FORECAST_DAY.value,
    ]

    ping_eid, ping_ev = items[1]
    assert ping_eid == 0 and "id" not in ping_ev

    logged = [r["event_type"] for r in appended]
    assert SSEEventType.PING.value not in logged, (
        "ping 又被写进 event_log 了 —— 一次预测会攒下几百条垃圾行并在重连时全量重放"
    )
    assert logged == [SSEEventType.FORECAST_DAY.value] * 2
