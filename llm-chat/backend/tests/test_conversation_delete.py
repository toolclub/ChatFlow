"""Deletion must not race final persistence or a newly admitted turn."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from graph.runner import stream as runner
from memory import store
from services import conversation_service as services


@pytest.mark.asyncio
async def test_delete_waits_for_consumer_and_keeps_stop_signal(monkeypatch):
    service = services.ConversationService()
    finalize = asyncio.Event()
    stopped = asyncio.Event()
    order = []
    status = {"status": "streaming", "updated_at": time.time()}

    async def graph():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def consumer():
        await stopped.wait()
        await finalize.wait()
        order.append("persist")
        status["status"] = "active"

    graph_task = asyncio.create_task(graph())
    consumer_task = asyncio.create_task(consumer())
    await asyncio.sleep(0)
    local = SimpleNamespace(stop_event=asyncio.Event(), _graph_task=graph_task, _consumer_task=consumer_task)
    monkeypatch.setitem(runner._active_sessions, "deleting", local)
    monkeypatch.setattr(store, "db_get_conversation_meta", AsyncMock(side_effect=lambda _: dict(status)))
    publish = AsyncMock()
    monkeypatch.setattr("db.redis_state.publish_stop", publish)
    monkeypatch.setattr(store, "delete", AsyncMock(side_effect=lambda _: order.append("delete")))
    cleanup = AsyncMock(side_effect=lambda _: order.append("clear_stop"))
    monkeypatch.setattr(service, "_cleanup_redis", cleanup)
    monkeypatch.setattr(service, "_cleanup_sandbox_session", AsyncMock())
    monkeypatch.setattr(services, "LONGTERM_MEMORY_ENABLED", False)
    task = asyncio.create_task(service.delete_conversation("deleting"))
    try:
        await asyncio.wait_for(stopped.wait(), 0.5)
        await asyncio.sleep(0)
        assert not task.done()
        cleanup.assert_not_awaited()
        finalize.set()
        assert await asyncio.wait_for(task, 0.5) == {"ok": True}
        assert order == ["persist", "delete", "clear_stop"]
        assert local.stop_event.is_set()
        publish.assert_awaited_once()
    finally:
        finalize.set()
        await asyncio.gather(task, graph_task, consumer_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_timeout_never_cancels_finalizer_or_deletes_rows(monkeypatch):
    service = services.ConversationService()
    consumer = asyncio.create_task(asyncio.Event().wait())
    local = SimpleNamespace(stop_event=asyncio.Event(), _graph_task=None, _consumer_task=consumer)
    monkeypatch.setitem(runner._active_sessions, "slow", local)
    monkeypatch.setattr(store, "db_get_conversation_meta", AsyncMock(return_value={"status": "streaming", "updated_at": time.time()}))
    monkeypatch.setattr("db.redis_state.publish_stop", AsyncMock())
    original = service._stop_active_stream
    monkeypatch.setattr(service, "_stop_active_stream", lambda cid: original(cid, timeout=0.02))
    delete = AsyncMock()
    clear = AsyncMock()
    monkeypatch.setattr(store, "delete", delete)
    monkeypatch.setattr(service, "_cleanup_redis", clear)
    try:
        with pytest.raises(TimeoutError, match="保存结果"):
            await service.delete_conversation("slow")
        assert not consumer.done()
        delete.assert_not_awaited()
        clear.assert_not_awaited()
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["active", "streaming"])
async def test_store_delete_locks_and_checks_state_before_atomic_cleanup(monkeypatch, status):
    statements = []
    commit = AsyncMock()

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, statement):
            statements.append(str(statement))
            return SimpleNamespace(scalar_one_or_none=lambda: SimpleNamespace(status=status))

        async def commit(self):
            await commit()

    marker = object()
    monkeypatch.setattr(store, "_store", {"conv": marker})
    monkeypatch.setattr(store, "AsyncSessionLocal", Session)
    notify = AsyncMock()
    monkeypatch.setattr(store, "_notify_cache_invalidation", notify)
    if status == "streaming":
        with pytest.raises(TimeoutError):
            await store.delete("conv")
        assert len(statements) == 1
        assert store.get("conv") is marker
        commit.assert_not_awaited()
        notify.assert_not_awaited()
    else:
        await store.delete("conv")
        assert len(statements) == 9
        assert {query.split()[2] for query in statements[1:]} == {
            "event_log", "tool_executions", "message_details", "tool_events",
            "plan_steps", "artifacts", "messages", "conversations",
        }
        commit.assert_awaited_once()
        notify.assert_awaited_once_with("conv")
        assert store.get("conv") is None
    assert "FOR UPDATE" in statements[0]


@pytest.mark.asyncio
async def test_batch_delete_reports_exact_successes_and_deduplicates(monkeypatch):
    service = services.ConversationService()

    async def cleanup(cid):
        if cid == "busy":
            raise TimeoutError("任务正在停止")

    cleanup_mock = AsyncMock(side_effect=cleanup)
    monkeypatch.setattr(service, "_cleanup_conversation", cleanup_mock)
    result = await service.batch_delete_conversations(["done", "busy", "done"])
    assert result == {"ok": False, "deleted": 1, "deleted_ids": ["done"],
                      "errors": [{"conv_id": "busy", "error": "任务正在停止"}]}
    assert cleanup_mock.await_count == 2
