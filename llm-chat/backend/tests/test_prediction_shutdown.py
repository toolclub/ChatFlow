"""Service restarts pause predictions instead of permanently stopping them."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import graph.prediction_agent as pa
from fsm.sse_events import SSEEventType


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_status", ["inferring", "done"])
async def test_shutdown_persists_pause_and_waits_for_local_graph_cleanup(monkeypatch, initial_status):
    snapshot = SimpleNamespace(status=initial_status)
    updated = []
    events = []
    started = asyncio.Event()

    async def update(sid, status, **kwargs):
        updated.append((sid, status))
        snapshot.status = status
        return True

    async def graph_events(*args, **kwargs):
        started.set()
        await asyncio.Future()
        yield {}

    async def append(**kwargs):
        events.append(kwargs["event_type"])
        return len(events)

    monkeypatch.setattr(pa, "get_prediction_snapshot", AsyncMock(return_value=snapshot))
    monkeypatch.setattr(pa, "update_prediction_status", update)
    monkeypatch.setattr(pa, "append_event", append)
    monkeypatch.setattr(pa, "predict_graph", SimpleNamespace(astream_events=graph_events))
    release = AsyncMock()
    monkeypatch.setattr(pa.prediction_runtime, "release", release)
    monkeypatch.setattr("db.redis_state.unregister_streaming", AsyncMock())
    session = pa.PredictionSession("local-only")
    session.lease_token = "local-owner"
    monkeypatch.setattr(pa, "_SESSIONS", {"local-only": session})
    session.start(pa._run_prediction_graph(session, {"snapshot_id": "local-only"}))
    await started.wait()

    should_pause = initial_status == "inferring"
    assert await pa.shutdown_prediction_sessions(timeout=1) == int(should_pause)
    assert updated == ([("local-only", "paused")] if should_pause else [])
    assert events == ([SSEEventType.PAUSED.value] if should_pause else [])
    assert SSEEventType.STOPPED.value not in events
    assert session.graph_task.done() and session.terminal
    assert not pa._SESSIONS
    release.assert_awaited_once_with("local-only", "local-owner")


@pytest.mark.asyncio
async def test_shutdown_is_bounded_when_database_pause_hangs(monkeypatch):
    async def stuck_pause(*args, **kwargs):
        await asyncio.Future()
    monkeypatch.setattr(pa, "_transition_status", stuck_pause)
    release = AsyncMock()
    monkeypatch.setattr(pa.prediction_runtime, "release", release)
    session = pa.PredictionSession("slow-db")
    session.lease_token = "owned"
    session.graph_task = asyncio.create_task(asyncio.sleep(30))
    monkeypatch.setattr(pa, "_SESSIONS", {"slow-db": session})
    started = asyncio.get_running_loop().time()
    assert await pa.shutdown_prediction_sessions(timeout=0.02) == 0
    assert asyncio.get_running_loop().time() - started < 0.3
    assert session.cancel_reason == pa._CANCEL_PAUSED
    with pytest.raises(asyncio.CancelledError):
        await session.graph_task
    release.assert_not_awaited()  # lease expiry handles unacknowledged cleanup


@pytest.mark.asyncio
async def test_shutdown_does_not_touch_terminal_or_other_worker_sessions(monkeypatch):
    completed = pa.PredictionSession("done")
    completed.terminal = True
    monkeypatch.setattr(pa, "_SESSIONS", {"done": completed})
    transition = AsyncMock()
    monkeypatch.setattr(pa, "_transition_status", transition)
    assert await pa.shutdown_prediction_sessions(timeout=0.01) == 0
    transition.assert_not_awaited()


@pytest.mark.asyncio
async def test_delayed_quant_init_cannot_start_after_shutdown():
    from main import _cancel_delayed_quant_init

    started = asyncio.Event()
    initialize = AsyncMock()
    async def delayed_start():
        started.set()
        await asyncio.sleep(15)
        await initialize()
    task = asyncio.create_task(delayed_start())
    await started.wait()
    await _cancel_delayed_quant_init(task)
    assert task.cancelled()
    initialize.assert_not_awaited()
