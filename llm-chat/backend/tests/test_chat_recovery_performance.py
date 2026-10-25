"""Recovery efficiency without weakening durable ordering or model isolation."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from graph.runner import stream as runner
from graph.runner.utils import sse
from memory import store
from memory.schema import Conversation, Message
from services import conversation_service as service_module


def parse_frames(chunks):
    return [json.loads(line[5:]) for chunk in chunks for line in chunk.splitlines()
            if line.startswith("data:")]


def rows(count):
    return [{"id": i + 1, "event_type": "content", "sse_string": sse({"content": f"内容 {i}"})}
            for i in range(count)] + [{"id": count + 1, "event_type": "done", "sse_string": sse({"done": True})}]


@pytest.mark.asyncio
async def test_ui_projection_never_hydrates_model_cache_or_queries_duplicate_tools(monkeypatch):
    meta = {"id": "view", "title": "history", "status": "streaming"}
    message = {"message_id": "round", "role": "assistant", "content": "saved", "thinking": "reason",
               "thinking_segments": [{"node": "model", "content": "reason"}],
               "stream_completed": False, "stream_buffer": "partial", "images": [],
               "tool_summary": "summary", "step_summary": "step", "prompt_tokens": 12,
               "completion_tokens": 3, "reasoning_tokens": 2, "clarification_data": {}, "timestamp": 1}
    queries = []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, query):
            queries.append(query)
            value = SimpleNamespace(first=lambda: meta, all=lambda: [message])
            return SimpleNamespace(mappings=lambda: value)

    model_history = Conversation("view", messages=[Message("user", "model history")], status="streaming")
    monkeypatch.setattr(store, "_store", {"view": model_history})
    monkeypatch.setattr(store, "AsyncSessionLocal", Session)
    data = await store.db_get_conversation_view("view", include_tools=False)
    assert data["messages"] == [{**message, "tool_calls": []}]
    assert len(queries) == 2
    assert all("tool_executions" not in str(query) for query in queries)
    assert store.get("view") is model_history
    assert model_history.messages[0].content == "model history"


@pytest.mark.asyncio
async def test_full_state_runs_independent_reads_together_and_reads_tools_once(monkeypatch):
    started = set()
    all_started = asyncio.Event()
    tool = {"id": 8, "message_id": "round", "tool_name": "search", "tool_input": {"q": "test"},
            "sequence_number": 1, "status": "done"}
    data = {"id": "view", "title": "view", "status": "completed", "messages": [
        {"role": "assistant", "message_id": "round", "content": "answer", "tool_calls": []},
    ]}

    def read(name, result):
        async def impl(*args, **kwargs):
            started.add(name)
            if len(started) == 5:
                all_started.set()
            await asyncio.wait_for(all_started.wait(), timeout=0.5)
            return result
        return AsyncMock(side_effect=impl)

    view = read("messages", data)
    tools = read("tools", [tool])
    monkeypatch.setattr(store, "db_get_conversation_view", view)
    legacy_hydrate = AsyncMock(side_effect=AssertionError("UI must not hydrate model cache"))
    monkeypatch.setattr(store, "db_get_conversation", legacy_hydrate)
    monkeypatch.setattr(service_module, "get_tool_executions_for_conv", tools)
    monkeypatch.setattr("db.plan_store.get_all_plans_for_conv", read("plans", []))
    monkeypatch.setattr(service_module, "get_artifact_meta_list", read("artifacts", []))
    monkeypatch.setattr(service_module, "get_latest_event_id", read("cursor", 99))
    result = await service_module.conversation_service.get_full_state("view")
    assert result["messages"][0]["tool_executions"] == [tool]
    assert result["messages"][0]["tool_calls"] == [{"id": "call_8", "name": "search", "args": {"q": "test"}, "type": "tool_call"}]
    view.assert_awaited_once_with("view", include_tools=False)
    tools.assert_awaited_once_with("view")
    legacy_hydrate.assert_not_awaited()


@pytest.mark.asyncio
async def test_replay_batches_full_frames_and_retains_each_durable_cursor(monkeypatch):
    events = rows(1001)

    async def read(conv_id, after_id, message_id=""):
        assert message_id == "round"
        return [event for event in events if event["id"] > after_id][:500]

    monkeypatch.setattr("db.event_store.get_events_since", read)
    chunks = [chunk async for chunk in runner.resume_stream("conv", 0, "round")]
    payloads = parse_frames(chunks)
    assert [p["id"] for p in payloads] == list(range(1, 1003))
    assert [p["content"] for p in payloads[:-1]] == [f"内容 {i}" for i in range(1001)]
    assert all(p["message_id"] == "round" for p in payloads)
    assert payloads[-1]["done"] is True
    assert len(chunks) <= 7  # 1002 frames formerly required 1002 ASGI writes.
    assert all(len(chunk.encode("utf-8")) <= runner._SSE_REPLAY_CHUNK_BYTES for chunk in chunks)
    assert all(chunk.endswith("\n\n") and chunk.startswith("id:") for chunk in chunks)


@pytest.mark.asyncio
async def test_disconnect_mid_page_resumes_after_last_received_event(monkeypatch):
    events = rows(800)

    async def read(conv_id, after_id, message_id=""):
        return [event for event in events if event["id"] > after_id][:500]

    monkeypatch.setattr("db.event_store.get_events_since", read)
    stream = runner.resume_stream("conv", 0, "round")
    first_chunk = await anext(stream)
    received = parse_frames([first_chunk])
    await stream.aclose()
    rest = [chunk async for chunk in runner.resume_stream("conv", received[-1]["id"], "round")]
    assert [p["id"] for p in [*received, *parse_frames(rest)]] == list(range(1, 802))


@pytest.mark.asyncio
async def test_large_event_is_never_split_into_invalid_sse_frames(monkeypatch):
    huge = "界" * runner._SSE_REPLAY_CHUNK_BYTES
    events = [{"id": 1, "event_type": "content", "sse_string": sse({"content": huge})},
              {"id": 2, "event_type": "done", "sse_string": sse({"done": True})}]
    monkeypatch.setattr("db.event_store.get_events_since", AsyncMock(return_value=events))
    chunks = [chunk async for chunk in runner.resume_stream("conv", 0, "round")]
    assert len(chunks) == 2
    assert parse_frames(chunks)[0]["content"] == huge
    assert chunks[0].endswith("\n\n")


@pytest.mark.asyncio
async def test_omitted_round_uses_lightweight_identity_lookup(monkeypatch):
    latest = AsyncMock(return_value="round")
    monkeypatch.setattr(store, "get_latest_assistant_message_id", latest)
    monkeypatch.setattr(store, "db_get_conversation", AsyncMock(side_effect=AssertionError("unexpected full history")))
    monkeypatch.setattr("db.event_store.get_events_since", AsyncMock(return_value=rows(0)))
    chunks = [chunk async for chunk in runner.resume_stream("conv")]
    assert parse_frames(chunks)[0]["message_id"] == "round"
    latest.assert_awaited_once_with("conv")


@pytest.mark.asyncio
async def test_startup_and_invalidation_do_not_fan_out_history_reads(monkeypatch):
    def no_database():
        raise AssertionError("lazy initialization must not read histories")
    monkeypatch.setattr(store, "AsyncSessionLocal", no_database)
    listener = AsyncMock()
    monkeypatch.setattr("db.redis_state.start_cache_invalidation_listener", listener)
    idle = Conversation("idle", status="completed")
    active = Conversation("active", status="streaming")
    monkeypatch.setattr(store, "_store", {"idle": idle, "active": active})
    monkeypatch.setattr(runner, "_active_sessions", {})
    await store.init()
    callback = listener.call_args.args[0]
    await callback("not-cached")
    await callback("idle")
    await callback("active")
    assert store.get("idle") is None
    assert store.get("active") is active


@pytest.mark.asyncio
async def test_missing_identity_cannot_list_cached_or_persisted_conversations(monkeypatch):
    monkeypatch.setattr(store, "_store", {"private": Conversation("private", user_id="owner")})
    monkeypatch.setattr(store, "AsyncSessionLocal", lambda: pytest.fail("must reject before querying"))
    assert store.all_conversations() == []
    assert await store.db_list_conversations() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("artifact", [
    {"id": 7, "path": "existing.html", "content": ""},
    {"path": "metadata-only.html"},
    {"path": "binary.pdf", "content": "", "binary": True},
])
async def test_artifact_metadata_does_not_overwrite_saved_content(monkeypatch, artifact):
    save = AsyncMock()
    monkeypatch.setattr("db.artifact_store.save_artifact", save)
    session = runner.StreamSession("artifact", "hello")
    await session._track_sse_for_db(sse({"file_artifact": artifact}))
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_artifact_fallback_persists_current_message_and_updates_emitted_identity(monkeypatch):
    save = AsyncMock(return_value={"id": 7, "message_id": "assistant"})
    monkeypatch.setattr("db.artifact_store.save_artifact", save)
    session = runner.StreamSession("artifact", "hello")
    updated = await session._track_sse_for_db(sse({"file_artifact": {"name": "test", "path": "test.txt", "content": "real content"}}))
    assert save.call_args.kwargs["message_id"] == session.assistant_message_id
    assert parse_frames([updated])[0]["file_artifact"]["id"] == 7
    assert session.ctx.assistant_message_id == session.assistant_message_id


@pytest.mark.asyncio
async def test_title_edit_updates_only_requested_columns_and_preserves_active_history(monkeypatch):
    statements = []
    commit = AsyncMock()

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, statement):
            statements.append(statement)
            return SimpleNamespace(scalar_one_or_none=lambda: "active")

        async def commit(self):
            await commit()

    cached = Conversation("active", title="old", messages=[Message("assistant", "in progress")], status="streaming")
    monkeypatch.setattr(store, "_store", {"active": cached})
    monkeypatch.setattr(store, "AsyncSessionLocal", Session)
    monkeypatch.setattr(store, "_notify_cache_invalidation", AsyncMock())
    monkeypatch.setattr(store, "db_get_conversation", AsyncMock(side_effect=AssertionError("must not rehydrate")))
    assert await service_module.conversation_service.update_conversation("active", title="new") == {"ok": True}
    assert store.get("active") is cached
    assert cached.title == "new" and cached.status == "streaming"
    assert cached.messages[0].content == "in progress"
    assert len(statements) == 1
    assert set(statements[0].compile().params) == {"title", "updated_at", "id_1"}
    commit.assert_awaited_once()
