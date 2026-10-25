"""Worker ownership and durable multi-subscriber recovery regressions."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import graph.prediction_agent as pa
from fsm.sse_events import SSEEventType
from graph.prediction import runtime


def snapshot(status="inferring"):
    return SimpleNamespace(status=status, client_id="test", user_id="", criteria={})


def row(eid, kind):
    return {"id": eid, "event_type": kind, "event_data": {"event": kind, "text": str(eid)}}


@pytest.mark.asyncio
async def test_remote_owner_stream_never_starts_duplicate_graph(monkeypatch):
    calls = 0
    async def events(*args, **kwargs):
        nonlocal calls
        calls += 1
        return [] if calls == 1 else [row(1, SSEEventType.DONE.value)]
    monkeypatch.setattr(pa, "get_events_since", events)
    monkeypatch.setattr(pa, "get_prediction_snapshot", AsyncMock(return_value=snapshot()))
    monkeypatch.setattr(pa, "get_prediction_runtime_snapshot", AsyncMock(return_value=snapshot()))
    monkeypatch.setattr(runtime, "owned", AsyncMock(return_value=True))
    start = AsyncMock()
    monkeypatch.setattr(pa, "start_prediction_graph", start)
    result = [ev async for ev in pa.stream_predict("remote")]
    assert result[-1]["event"] == SSEEventType.DONE.value
    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_each_subscriber_gets_all_pages_without_stealing_events(monkeypatch):
    rows = [row(1, SSEEventType.THINKING.value), row(2, SSEEventType.CONTENT.value),
            row(3, SSEEventType.DONE.value)]
    async def events(_sid, after_id=0, **kwargs):
        await asyncio.sleep(0)
        return [r for r in rows if r["id"] > after_id][:1]
    monkeypatch.setattr(pa, "get_events_since", events)
    # Snapshot is already done; all pages must still be delivered to each tab.
    monkeypatch.setattr(pa, "get_prediction_snapshot", AsyncMock(return_value=snapshot("done")))
    monkeypatch.setattr(pa, "get_prediction_runtime_snapshot", AsyncMock(return_value=snapshot("done")))
    async def collect():
        return [ev["id"] async for ev in pa.stream_predict("shared")]
    assert await asyncio.gather(collect(), collect()) == [[1, 2, 3], [1, 2, 3]]


@pytest.mark.asyncio
async def test_old_pause_does_not_close_resumed_stream(monkeypatch):
    rows = [row(1, SSEEventType.PAUSED.value), row(2, SSEEventType.CONTENT.value),
            row(3, SSEEventType.DONE.value)]
    async def events(_sid, after_id=0, **kwargs):
        return [r for r in rows if r["id"] > after_id]
    monkeypatch.setattr(pa, "get_events_since", events)
    monkeypatch.setattr(pa, "get_prediction_snapshot", AsyncMock(return_value=snapshot()))
    monkeypatch.setattr(pa, "get_prediction_runtime_snapshot", AsyncMock(return_value=snapshot()))
    result = [ev["id"] async for ev in pa.stream_predict("resumed")]
    assert result == [2, 3]


@pytest.mark.asyncio
async def test_start_with_occupied_lease_does_not_run_or_change_status(monkeypatch):
    monkeypatch.setattr(runtime, "acquire", AsyncMock(return_value=None))
    read = AsyncMock()
    monkeypatch.setattr(pa, "get_prediction_snapshot", read)
    monkeypatch.setattr(pa, "get_prediction_runtime_snapshot", read)
    assert await pa.start_prediction_graph("occupied", {"resume": True}) is None
    read.assert_not_awaited()


@pytest.mark.asyncio
async def test_lease_unavailable_fails_closed(monkeypatch):
    monkeypatch.setattr(runtime, "acquire", AsyncMock(side_effect=ConnectionError("offline")))
    with pytest.raises(ConnectionError):
        await pa.start_prediction_graph("offline", {})
    assert "offline" not in pa._SESSIONS


@pytest.mark.asyncio
async def test_detached_local_buffer_is_bounded(monkeypatch):
    session = pa.PredictionSession("detached")
    for i in range(1000):
        session.enqueue((i, {"event": SSEEventType.CONTENT.value, "id": i}))
    assert session.queue.qsize() == 256
    assert session.queue.get_nowait()[0] == 744


@pytest.mark.asyncio
async def test_expired_owner_cannot_release_or_renew_new_owner(monkeypatch):
    class Redis:
        token = None
        async def set(self, key, value, **kwargs):
            if kwargs.get("nx") and self.token:
                return False
            self.token = value
            return True
        async def eval(self, script, n, key, token, *args):
            if self.token != token:
                return 0
            if "'del'" in script:
                self.token = None
            return 1
    redis = Redis()
    monkeypatch.setattr("db.redis_state._get_redis", lambda: redis)
    first = await runtime.acquire("lease")
    assert await runtime.acquire("lease") is None
    redis.token = None  # first lease expires while its worker is unreachable
    second = await runtime.acquire("lease")
    assert second and first != second
    assert not await runtime.release("lease", first)
    assert not await runtime.renew("lease", first)
    assert redis.token == second
    assert await runtime.renew("lease", second)
    assert await runtime.release("lease", second)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["paused", "stopped", "failed"])
async def test_late_completion_cannot_override_user_control(monkeypatch, status):
    monkeypatch.setattr(pa, "get_prediction_snapshot", AsyncMock(return_value=snapshot(status)))
    monkeypatch.setattr(pa, "get_prediction_runtime_snapshot", AsyncMock(return_value=snapshot(status)))
    update = AsyncMock()
    monkeypatch.setattr(pa, "update_prediction_status", update)
    assert not await pa._transition_status("controlled", "done")
    update.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_resume,expected", [(False, False), (True, True)])
async def test_db_pause_requires_explicit_resume_under_row_lock(monkeypatch, allow_resume, expected):
    from db import prediction_store as store
    snap = snapshot("paused")
    class Session:
        committed = False
        locked = False
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def execute(self, statement):
            self.locked = statement._for_update_arg is not None
            return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: snap))
        async def commit(self):
            self.committed = True
    session = Session()
    monkeypatch.setattr(store, "AsyncSessionLocal", lambda: session)
    changed = await store.update_prediction_status("controlled", "inferring", allow_resume=allow_resume)
    assert changed is expected
    assert session.locked
    assert session.committed is expected
    assert snap.status == ("inferring" if expected else "paused")


@pytest.mark.asyncio
async def test_competing_resume_requests_start_exactly_one_graph(monkeypatch):
    snap = snapshot("paused")
    snap.market = "cn_a"
    snap.news_digest = []
    snap.filing_digest = []
    monkeypatch.setattr(pa, "get_prediction_snapshot", AsyncMock(return_value=snap))
    claimed = False
    async def acquire(_sid):
        nonlocal claimed
        await asyncio.sleep(0)
        if claimed:
            return None
        claimed = True
        return "resume-winner"
    async def update(_sid, target, **kwargs):
        snap.status = target
        return True
    monkeypatch.setattr(runtime, "acquire", acquire)
    release = AsyncMock()
    monkeypatch.setattr(runtime, "release", release)
    monkeypatch.setattr(pa, "update_prediction_status", update)
    monkeypatch.setattr("db.redis_state.clear_stop", AsyncMock())
    start = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(pa, "start_prediction_graph", start)
    results = await asyncio.gather(pa.resume_prediction("race"), pa.resume_prediction("race"))
    assert sum(result["ok"] for result in results) == 1
    assert start.await_count == 1
    assert start.call_args.kwargs["lease_token"] == "resume-winner"
    release.assert_not_awaited()  # ownership is handed to the background graph


@pytest.mark.asyncio
async def test_resume_losing_lease_does_not_touch_new_session(monkeypatch):
    from unittest.mock import Mock
    monkeypatch.setattr(pa, "get_prediction_snapshot", AsyncMock(return_value=snapshot("paused")))
    monkeypatch.setattr(runtime, "acquire", AsyncMock(return_value=None))
    running = SimpleNamespace(request_pause=Mock(), graph_task=Mock())
    monkeypatch.setitem(pa._SESSIONS, "winner", running)
    result = await pa.resume_prediction("winner")
    assert not result["ok"]
    running.request_pause.assert_not_called()
    running.graph_task.cancel.assert_not_called()
