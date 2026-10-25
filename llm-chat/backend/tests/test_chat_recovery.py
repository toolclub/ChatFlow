"""Recovery contracts: durable cursors, isolated turns, stop and orphan cleanup."""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from graph.runner import stream as runner
from graph.runner.utils import sse
from fsm.sse_events import SSEEventType
from routers import chat_router
from services.conversation_service import conversation_service


def payload(chunk):
    return json.loads(next(line[5:].strip() for line in chunk.splitlines() if line.startswith("data:")))


def event(eid, kind, value):
    return {"id": eid, "event_type": kind, "sse_string": sse({kind: value})}


@pytest.mark.asyncio
async def test_durable_events_publish_only_after_commit_and_retry_without_loss(monkeypatch):
    session = runner.StreamSession("durable", "hello")
    session._emit_sse(sse({"content": "one"}))
    session._emit_sse(sse({"content": "two"}))
    assert session._sse_events == []
    write = AsyncMock(side_effect=[RuntimeError("temporary outage"), [41, 42]])
    monkeypatch.setattr("db.event_store.append_events_batch", write)
    with pytest.raises(RuntimeError):
        await session._flush_to_db()
    assert len(session._event_batch) == 2
    assert session._sse_events == []
    await session._flush_to_db()
    assert [payload(c)["content"] for c in session._sse_events] == ["one", "two"]
    assert [payload(c)["id"] for c in session._sse_events] == [41, 42]
    assert all(payload(c)["message_id"] == session.assistant_message_id for c in session._sse_events)
    assert not session._event_batch
    session._emit_sse(sse({"ping": True}), SSEEventType.PING.value)
    assert "id" not in payload(session._sse_events[-1])
    assert not session._event_batch


@pytest.mark.asyncio
async def test_resume_drains_late_commit_without_switching_to_memory_tail(monkeypatch):
    reads = AsyncMock(side_effect=[[event(1, "content", "A")], [event(2, "content", "B"), event(3, "done", True)]])
    monkeypatch.setattr("db.event_store.get_events_since", reads)
    monkeypatch.setitem(runner._active_sessions, "resume-gap", SimpleNamespace(_sse_events=["already in memory"]))
    chunks = [payload(frame) async for chunk in runner.resume_stream("resume-gap", 0, "turn-2")
              for frame in chunk.split("\n\n") if frame.strip()]
    assert [c.get("content") for c in chunks if "content" in c] == ["A", "B"]
    assert [c["id"] for c in chunks] == [1, 2, 3]
    assert all(call.kwargs["message_id"] == "turn-2" for call in reads.call_args_list)
    assert reads.call_args_list[1].args[1] == 1


@pytest.mark.asyncio
async def test_resume_after_terminal_cursor_closes_instead_of_waiting_five_minutes(monkeypatch):
    monkeypatch.setattr("db.event_store.get_events_since", AsyncMock(return_value=[]))
    monkeypatch.setattr("memory.store.get_message_stream_state", AsyncMock(return_value={"stream_completed": True}))
    chunks = [payload(c) async for c in runner.resume_stream("finished", 100, "turn")]
    assert chunks == [{"done": True, "compressed": False}]


@pytest.mark.asyncio
async def test_graph_initialization_failure_reaches_consumer(monkeypatch):
    def fail(*args):
        raise RuntimeError("graph unavailable")
    monkeypatch.setattr(runner, "get_graph", fail)
    session = runner.StreamSession("initialization", "hello")
    await session._run_graph()
    kind, data = session.queue.get_nowait()
    assert kind == "error"
    assert str(data["exc"]) == "graph unavailable"


@pytest.mark.asyncio
async def test_parallel_same_named_tools_keep_outputs_and_completion_separate(monkeypatch):
    create = AsyncMock(side_effect=[101, 102])
    complete = AsyncMock()
    monkeypatch.setattr("db.tool_store.create_tool_execution", create)
    monkeypatch.setattr("db.tool_store.complete_tool_execution", complete)
    session = runner.StreamSession("parallel", "hello")
    for run in ("run-a", "run-b"):
        await session._track_sse_for_db(sse({"tool_call": {"name": "sandbox_execute", "tool_run_id": run}}))
    await session._track_sse_for_db(sse({"sandbox_output": {"tool_run_id": "run-a", "text": "A output"}}))
    await session._track_sse_for_db(sse({"sandbox_output": {"tool_run_id": "run-b", "text": "B output"}}))
    await session._track_sse_for_db(sse({"tool_result": {"tool_run_id": "run-a", "status": "done"}}))
    assert complete.call_args.args == (101,)
    assert complete.call_args.kwargs["output"] == "A output"
    assert session._tool_exec_map == {2: 102}
    await session._finish_pending_tools()
    assert complete.call_args.args == (102,)
    assert complete.call_args.kwargs["output"] == "B output"
    assert complete.call_args.kwargs["status"] == "error"


@pytest.mark.asyncio
async def test_stop_cancels_waiting_graph_and_confirms_after_persistence(monkeypatch):
    state = {"user_id": "owner", "status": "streaming", "messages": []}
    released = asyncio.Event()

    async def graph():
        try:
            await asyncio.Event().wait()
        finally:
            released.set()

    async def consumer():
        await released.wait()
        state.update(status="active", messages=[{"role": "assistant", "content": "partial"}])

    graph_task = asyncio.create_task(graph())
    consumer_task = asyncio.create_task(consumer())
    await asyncio.sleep(0)
    session = SimpleNamespace(stop_event=asyncio.Event(), _graph_task=graph_task, _consumer_task=consumer_task)
    monkeypatch.setitem(runner._active_sessions, "stop", session)
    monkeypatch.setattr("memory.store.db_get_conversation_view", AsyncMock(side_effect=lambda *args, **kwargs: state.copy()))
    monkeypatch.setattr("memory.store.db_get_conversation_meta", AsyncMock(side_effect=lambda _: state.copy()))
    publish = AsyncMock()
    monkeypatch.setattr("db.redis_state.publish_stop", publish)
    request = Request({"type": "http", "headers": []})
    response = await chat_router.stop_chat("stop", {"id": "owner"}, request, stop_token="token", timeout_ms=1000)
    chunks = [payload(c) async for c in response.body_iterator]
    await asyncio.gather(graph_task, consumer_task, return_exceptions=True)
    assert chunks == [{"stop_confirmed": True, "can_continue": True, "reason": "stopped", "stop_token": "token"}]
    assert session.stop_event.is_set()
    publish.assert_awaited_once_with("stop")


def test_access_rejects_cross_user_and_accepts_anonymous_owner():
    chat_router._assert_access({"user_id": "", "client_id": "browser"}, {"client_id": "browser"})
    with pytest.raises(HTTPException) as error:
        chat_router._assert_access({"user_id": "other"}, {"id": "attacker"})
    assert error.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("alive", [True, False])
async def test_orphan_recovery_requires_lost_lease(monkeypatch, alive):
    stale = {"status": "streaming", "updated_at": time.time() - 90}
    monkeypatch.setattr("db.redis_state.is_streaming", AsyncMock(return_value=alive))
    recover = AsyncMock(return_value=True)
    monkeypatch.setattr("memory.store.recover_interrupted_stream", recover)
    monkeypatch.setattr("memory.store.db_get_conversation", AsyncMock(return_value={"status": "error"}))
    result = await conversation_service.reconcile_stream_state("orphan", stale)
    assert result["status"] == ("streaming" if alive else "error")
    assert recover.await_count == (0 if alive else 1)


@pytest.mark.asyncio
async def test_redis_outage_is_not_evidence_of_dead_worker(monkeypatch):
    monkeypatch.setattr("db.redis_state.is_streaming", AsyncMock(side_effect=RuntimeError("offline")))
    recover = AsyncMock()
    monkeypatch.setattr("memory.store.recover_interrupted_stream", recover)
    result = await conversation_service.reconcile_stream_state("unknown", {"status": "streaming", "updated_at": 0})
    assert result["status"] == "streaming"
    recover.assert_not_awaited()


@pytest.mark.asyncio
async def test_error_before_first_content_closes_assistant_row(monkeypatch):
    session = runner.StreamSession("error-empty", "hello")
    session.assistant_db_id = 77
    complete = AsyncMock()
    monkeypatch.setattr("memory.store.update_message_streaming", complete)
    monkeypatch.setattr("memory.store.update_status", AsyncMock())
    monkeypatch.setattr("db.redis_state.unregister_streaming", AsyncMock())
    monkeypatch.setattr("db.event_store.append_events_batch", AsyncMock(return_value=[1, 2]))
    session._conv_sm.send_event("streaming")
    await session.queue.put(("error", {"exc": RuntimeError("provider failed"), "can_continue": False}))
    await session._consume_events()
    assert complete.call_args.kwargs["stream_completed"] is True
    assert session._finalized and session._sse_done
    assert session._conv_sm.current_value == "error"


@pytest.mark.asyncio
async def test_db_conversation_preserves_anonymous_ownership(monkeypatch):
    from memory import store
    row = SimpleNamespace(id="anon", title="test", system_prompt="", core_memory={},
                          mid_term_summary="", mid_term_cursor=0, created_at=1,
                          updated_at=2, client_id="browser", user_id="", status="active")

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, *args):
            return row

        async def execute(self, *args):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    monkeypatch.setattr(store, "AsyncSessionLocal", Session)
    monkeypatch.setattr(store, "_store", {})
    restored = await store.db_get_conversation("anon")
    assert restored["client_id"] == "browser"
    assert restored["updated_at"] == 2
    chat_router._assert_access(restored, {"client_id": "browser"})


@pytest.mark.asyncio
async def test_orphan_recovery_does_not_close_a_newer_turn(monkeypatch):
    from memory import store
    commit = AsyncMock()

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def scalar(self, *args):
            # The lease check observed the previous turn at timestamp 10, but
            # another worker acquired a new turn before we obtained the row lock.
            return SimpleNamespace(status="streaming", updated_at=20)

    Session.commit = commit
    monkeypatch.setattr(store, "AsyncSessionLocal", Session)
    assert not await store.recover_interrupted_stream("newer", expected_updated_at=10)
    commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_delivery_waits_for_status_and_lease_cleanup(monkeypatch):
    session = runner.StreamSession("terminal-order", "hello")
    session._conv_sm.send_event("streaming")
    session._emit_sse(sse({"done": True}))
    monkeypatch.setattr("db.event_store.append_events_batch", AsyncMock(return_value=[9]))

    async def assert_not_delivered(*args):
        assert session._sse_events == []
        assert session._sse_done is False

    monkeypatch.setattr("memory.store.update_status", assert_not_delivered)
    monkeypatch.setattr("db.redis_state.unregister_streaming", assert_not_delivered)
    await session._set_done("completed")
    assert payload(session._sse_events[0])["done"] is True
    assert session._sse_done


@pytest.mark.asyncio
async def test_idle_flush_does_not_rewrite_large_buffers_and_preserves_concurrent_tokens(monkeypatch):
    session = runner.StreamSession("flush-version", "hello")
    session.assistant_db_id = 99
    await session._track_sse_for_db(sse({"content": "first"}))
    writes = []

    async def update(*args, **kwargs):
        writes.append(kwargs["stream_buffer"])
        if len(writes) == 1:
            # A token arrives while the first database write is awaiting IO.
            await session._track_sse_for_db(sse({"content": " second"}))

    monkeypatch.setattr("memory.store.update_message_streaming", update)
    await session._flush_to_db()
    await session._flush_to_db()
    await session._flush_to_db()
    assert writes == ["first", "first second"]


@pytest.mark.asyncio
async def test_shutdown_waits_for_real_consumer_to_save_partial_and_release_local_lease(monkeypatch):
    session = runner.StreamSession("shutdown-local", "hello")
    session.assistant_db_id = 123
    session._streaming = "partial answer"
    session._conv_sm.send_event("streaming")
    started = asyncio.Event()

    class WaitingGraph:
        async def astream_events(self, *args, **kwargs):
            started.set()
            await asyncio.Event().wait()
            yield {}

    monkeypatch.setattr(runner, "get_graph", lambda *args: WaitingGraph())
    save = AsyncMock()
    release = AsyncMock()
    publish_stop = AsyncMock()
    monkeypatch.setattr("memory.store.finalize_message", save)
    monkeypatch.setattr("memory.store.update_message_streaming", AsyncMock())
    monkeypatch.setattr("memory.store.update_status", AsyncMock())
    monkeypatch.setattr("db.event_store.append_events_batch", AsyncMock(return_value=[1, 2]))
    monkeypatch.setattr("db.redis_state.unregister_streaming", release)
    monkeypatch.setattr("db.redis_state.publish_stop", publish_stop)
    monkeypatch.setattr(runner, "_active_sessions", {session.conv_id: session})
    session._graph_task = asyncio.create_task(session._run_graph())
    session._consumer_task = asyncio.create_task(session._consume_events())
    session._hb_task = asyncio.create_task(asyncio.Event().wait())
    session._flush_task = asyncio.create_task(asyncio.Event().wait())
    await started.wait()

    await runner.shutdown_chat_sessions(timeout=1)

    assert session._consumer_task.done()
    assert session._graph_task.done()
    assert session._finalized and session._sse_done
    assert session._conv_sm.current_value == "active"
    assert "partial answer" in save.call_args.kwargs["content"]
    release.assert_awaited_once_with("shutdown-local")
    publish_stop.assert_not_awaited()
    assert not runner._active_sessions


@pytest.mark.asyncio
async def test_shutdown_is_bounded_when_consumer_cannot_finish(monkeypatch):
    session = runner.StreamSession("shutdown-stuck", "hello")
    session._graph_task = asyncio.create_task(asyncio.Event().wait())
    session._consumer_task = asyncio.create_task(asyncio.Event().wait())
    monkeypatch.setattr(runner, "_active_sessions", {session.conv_id: session})
    started = time.monotonic()
    await runner.shutdown_chat_sessions(timeout=0.02)
    assert time.monotonic() - started < 0.5
    await asyncio.gather(session._graph_task, session._consumer_task, return_exceptions=True)
    assert session.stop_event.is_set()
    assert session._consumer_task.cancelled()


@pytest.mark.asyncio
@pytest.mark.parametrize("admitted", [False, True])
async def test_chat_hydrates_only_after_exclusive_turn_admission(monkeypatch, admitted):
    from models import ChatRequest
    calls = []
    meta = {"user_id": "owner", "status": "error"}
    monkeypatch.setattr("memory.store.db_get_conversation_meta", AsyncMock(return_value=meta))
    monkeypatch.setattr("db.redis_state.is_streaming", AsyncMock(return_value=False))

    async def reconcile(cid, observed, *, refresh_cache):
        assert refresh_cache is False
        calls.append("reconcile")
        return observed

    async def claim(cid):
        calls.append("claim")
        return admitted

    async def hydrate(cid):
        assert calls == ["reconcile", "claim"]
        calls.append("hydrate")

    async def stream(**kwargs):
        yield sse({SSEEventType.DONE.value: True})

    monkeypatch.setattr(conversation_service, "reconcile_stream_state", reconcile)
    monkeypatch.setattr("memory.store.try_start_stream", claim)
    monkeypatch.setattr("memory.store.db_get_conversation", hydrate)
    monkeypatch.setattr(chat_router.graph_runner, "stream_response", stream)
    monkeypatch.setattr(chat_router, "_stop_events", {})
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    req = ChatRequest(conversation_id="admission", message="hello", model="test")
    if not admitted:
        with pytest.raises(HTTPException) as error:
            await chat_router.chat(req, {"id": "owner"}, request)
        assert error.value.status_code == 409
        assert calls == ["reconcile", "claim"]
    else:
        response = await chat_router.chat(req, {"id": "owner"}, request)
        assert [payload(c) async for c in response.body_iterator] == [{"done": True}]
        assert calls == ["reconcile", "claim", "hydrate"]
        assert not chat_router._stop_events


@pytest.mark.asyncio
async def test_stop_confirmation_read_cannot_replace_new_turn_cache(monkeypatch):
    from memory import store
    from memory.schema import Conversation, Message
    newer = Conversation("stop-race", messages=[Message("user", "new turn")], status="streaming")
    monkeypatch.setattr(store, "_store", {})
    monkeypatch.setattr(store, "db_get_conversation_meta", AsyncMock(return_value={"user_id": "owner", "status": "active"}))
    monkeypatch.setattr("db.redis_state.publish_stop", AsyncMock())
    legacy = AsyncMock(side_effect=AssertionError("Stop confirmation must never hydrate model history"))
    monkeypatch.setattr(store, "db_get_conversation", legacy)

    async def view(cid, *, include_tools):
        assert include_tools is False
        # A second tab admits its next turn after the terminal-state check.
        store._store[cid] = newer
        return {"messages": [{"role": "assistant", "content": "saved reply"}]}

    monkeypatch.setattr(store, "db_get_conversation_view", view)
    response = await chat_router.stop_chat("stop-race", {"id": "owner"},
        Request({"type": "http", "headers": []}), stop_token="old-stop", timeout_ms=1000)
    chunks = [payload(c) async for c in response.body_iterator]
    assert chunks == [{"stop_confirmed": True, "can_continue": True, "reason": "stopped", "stop_token": "old-stop"}]
    assert store.get("stop-race") is newer
    assert newer.messages[0].content == "new turn"
    legacy.assert_not_awaited()
