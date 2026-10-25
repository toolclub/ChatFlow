"""Inference regression tests: connection lifetime, partial failures and rotation."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from llm.client import LLMClient

pytestmark = pytest.mark.unit


def chunk(content=None, reasoning=None, usage=None, tool_calls=None):
    return SimpleNamespace(
        usage=usage,
        choices=[SimpleNamespace(delta=SimpleNamespace(
            content=content, reasoning_content=reasoning, tool_calls=tool_calls,
        ))],
    )


class FakeStream:
    def __init__(self, items=(), delay=0, error=None):
        self.items = list(items)
        self.delay = delay
        self.error = error
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.items:
            return self.items.pop(0)
        if self.error:
            raise self.error
        raise StopAsyncIteration

    async def close(self):
        self.closed = True


def client_for(stream, create=None):
    create = create or AsyncMock(return_value=stream)
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return LLMClient(sdk, model="test-model")


def stream_for(client, with_tools, **kwargs):
    if with_tools:
        return client.astream_with_tools([], tools=[], **kwargs)
    return client.astream([], **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_tools", [False, True])
async def test_usage_chunk_keeps_text_reasoning_and_tools(with_tools):
    usage = SimpleNamespace(prompt_tokens=1, completion_tokens=2, total_tokens=3)
    tc = SimpleNamespace(index=0, id="call-1", function=SimpleNamespace(name="echo", arguments='{"text":"ok"}'))
    stream = FakeStream([chunk("answer", "reason", usage, [tc])])
    result = [item async for item in stream_for(client_for(stream), with_tools)]
    assert stream.closed
    if with_tools:
        done = json.loads(result[-1][1])
        assert done["content"] == "answer"
        assert done["thinking"] == "reason"
        assert done["tool_calls"][0]["id"] == "call-1"
        assert done["usage"]["total_tokens"] == 3
    else:
        assert "answer" in result
        assert "\x00THINK\x00reason" in result
        assert result[0]["usage"]["total_tokens"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("with_tools", [False, True])
async def test_stream_connection_closes_after_transport_error(with_tools):
    stream = FakeStream([chunk("partial")], error=ConnectionError("disconnected"))
    with pytest.raises(ConnectionError):
        _ = [item async for item in stream_for(client_for(stream), with_tools)]
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("with_tools", [False, True])
async def test_stream_total_deadline_closes_slow_connection(with_tools):
    stream = FakeStream([chunk("x")] * 100, delay=.01)
    with pytest.raises(TimeoutError):
        _ = [item async for item in stream_for(client_for(stream), with_tools, timeout=.025)]
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("with_tools", [False, True])
async def test_request_setup_is_included_in_deadline(with_tools):
    async def slow_create(**kwargs):
        await asyncio.sleep(10)
    with pytest.raises(TimeoutError):
        _ = [item async for item in stream_for(client_for(None, slow_create), with_tools, timeout=.01)]


@pytest.mark.asyncio
@pytest.mark.parametrize("with_tools", [False, True])
async def test_cancel_closes_stream_and_propagates(with_tools):
    stream = FakeStream([chunk("partial")], delay=10)
    async def consume():
        return [item async for item in stream_for(client_for(stream), with_tools)]
    task = asyncio.create_task(consume())
    await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed


@pytest.mark.asyncio
async def test_partial_reply_failure_propagates_to_runner(monkeypatch):
    from graph.nodes.base import BaseNode
    emitted = AsyncMock()
    monkeypatch.setattr("langchain_core.callbacks.manager.adispatch_custom_event", emitted)
    stream = FakeStream([chunk("partial")], error=ConnectionError("lost"))
    with pytest.raises(ConnectionError):
        await BaseNode._stream_tokens(client_for(stream), [], .7, "conv", "call_model")
    assert emitted.call_args.args[1]["content"] == "partial"
    assert stream.closed


def test_rotating_key_creates_new_client_without_disrupting_existing(monkeypatch):
    import llm.chat as factory
    monkeypatch.setattr(factory, "_client_cache", {})
    monkeypatch.setattr(factory, "_get_http_client", lambda base, key: SimpleNamespace(api_key=key))
    old = factory._make_client("https://same.invalid/v1", "old-key", "same-model", .7)
    new = factory._make_client("https://same.invalid/v1", "new-key", "same-model", .7)
    assert new is not old
    assert new._client.api_key == "new-key"
    assert old._client.api_key == "old-key"
    assert factory._make_client("https://same.invalid/v1", "new-key", "same-model", .7) is new


@pytest.mark.asyncio
@pytest.mark.parametrize("with_tools", [False, True])
async def test_role_sampling_and_budget_reach_both_stream_apis(with_tools):
    stream = FakeStream([])
    create = AsyncMock(return_value=stream)
    client = client_for(stream, create)
    client.request_options = {"temperature": .2, "top_p": .8, "max_completion_tokens": 1024}
    _ = [item async for item in stream_for(client, with_tools)]
    assert create.call_args.kwargs["temperature"] == .2
    assert create.call_args.kwargs["top_p"] == .8
    assert create.call_args.kwargs["max_completion_tokens"] == 1024
    assert "max_tokens" not in create.call_args.kwargs


def test_deepseek_router_avoids_forced_high_reasoning():
    from llm.providers import get_provider_for_model
    assert get_provider_for_model("deepseek-v4", role="router").get_extra_body(0) == {
        "thinking": {"type": "disabled"},
    }
    assert get_provider_for_model("deepseek-v4").get_extra_body(.7)["reasoning_effort"] == "high"


def test_role_options_are_isolated_in_factory_cache(monkeypatch):
    import llm.chat as factory
    from config import settings
    monkeypatch.setattr(factory, "_client_cache", {})
    monkeypatch.setattr(factory, "_get_http_client", lambda *a: SimpleNamespace())
    monkeypatch.setattr(settings, "llm_role_options", {"router": {"max_tokens": 256}})
    router = factory._make_client("url", "key", "same", 0, role="router")
    chat = factory._make_client("url", "key", "same", 0)
    assert router is not chat
    assert router.request_options == {"max_tokens": 256}
    assert chat.request_options == {}


@pytest.mark.parametrize("options", [
    {"max_tokens": -1}, {"max_tokens": 0}, {"max_tokens": True},
    {"max_tokens": 10, "max_completion_tokens": 10}, {"top_p": 2},
    {"temperature": 3}, {"unsupported_sampling": 1},
])
def test_invalid_role_options_are_rejected(options):
    with pytest.raises(ValueError):
        LLMClient(None, "test", request_options=options)


@pytest.mark.asyncio
async def test_tool_failure_uses_current_message_and_parallel_batch_status(monkeypatch):
    from graph.nodes.base import BaseNode
    from langchain_core.messages import AIMessage, ToolMessage
    calls = [{"name": "search", "args": {}, "id": str(n)} for n in range(2)]
    monkeypatch.setattr("db.tool_store.get_tool_executions_for_message", AsyncMock(return_value=[
        {"status": "timeout"}, {"status": "done"},
    ]))
    state = {"assistant_message_id": "current", "messages": [
        AIMessage(content="", tool_calls=calls),
        ToolMessage(content="results", tool_call_id="0"),
        ToolMessage(content="results", tool_call_id="1"),
    ]}
    assert await BaseNode._latest_tool_batch_failed(state)


@pytest.mark.asyncio
async def test_error_words_in_successful_tool_output_do_not_change_state():
    from graph.nodes.base import BaseNode
    from langchain_core.messages import ToolMessage
    assert not await BaseNode._latest_tool_batch_failed({"messages": [
        ToolMessage(content="Exception error failed not found invalid", tool_call_id="0", status="success"),
    ]})
    assert await BaseNode._latest_tool_batch_failed({"messages": [
        ToolMessage(content="", tool_call_id="0", status="error"),
    ]})


@pytest.mark.asyncio
async def test_parallel_tool_custom_events_keep_their_tool_run_identity():
    from langchain_core.callbacks.manager import adispatch_custom_event
    from langchain_core.messages import AIMessage
    from langchain_core.tools import tool
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode

    @tool
    async def emit_output(marker: str) -> str:
        """Emit a distinct output from a parallel tool call."""
        await asyncio.sleep(0)
        await adispatch_custom_event("sandbox_output", {"text": marker})
        return marker

    graph = StateGraph(MessagesState)
    graph.add_node("tools", ToolNode([emit_output]))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    calls = [{"name": "emit_output", "args": {"marker": marker}, "id": marker} for marker in ("a", "b")]
    events = [event async for event in graph.compile().astream_events(
        {"messages": [AIMessage(content="", tool_calls=calls)]}, version="v2",
    )]
    tool_runs = {e["data"]["input"]["marker"]: e["run_id"] for e in events if e["event"] == "on_tool_start"}
    custom_runs = {e["data"]["text"]: e["run_id"] for e in events if e["event"] == "on_custom_event"}
    assert len(tool_runs) == 2
    assert tool_runs["a"] != tool_runs["b"]
    assert custom_runs == tool_runs


@pytest.mark.asyncio
@pytest.mark.parametrize("with_tools", [False, True])
async def test_output_budget_exhaustion_is_not_reported_as_success(with_tools):
    from llm.client import LLMOutputTruncatedError
    truncated = chunk("partial")
    truncated.choices[0].finish_reason = "length"
    stream = FakeStream([truncated])
    with pytest.raises(LLMOutputTruncatedError):
        _ = [item async for item in stream_for(client_for(stream), with_tools)]
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("db_status", ["running", "done"])
async def test_current_tool_error_is_not_hidden_by_lagging_database(monkeypatch, db_status):
    from graph.nodes.base import BaseNode
    from langchain_core.messages import AIMessage, ToolMessage
    monkeypatch.setattr("db.tool_store.get_tool_executions_for_message", AsyncMock(return_value=[
        {"status": db_status},
    ]))
    state = {"assistant_message_id": "current", "messages": [
        AIMessage(content="", tool_calls=[{"name": "search", "args": {}, "id": "current-call"}]),
        ToolMessage(content="", tool_call_id="current-call", status="error"),
        AIMessage(content="Tool execution finished"),
    ]}
    assert await BaseNode._latest_tool_batch_failed(state)


@pytest.mark.asyncio
async def test_previous_batch_structured_error_does_not_taint_current_success(monkeypatch):
    from graph.nodes.base import BaseNode
    from langchain_core.messages import AIMessage, ToolMessage
    monkeypatch.setattr("db.tool_store.get_tool_executions_for_message", AsyncMock(return_value=[
        {"status": "error"}, {"status": "done"},
    ]))
    state = {"assistant_message_id": "current", "messages": [
        AIMessage(content="", tool_calls=[{"name": "search", "args": {}, "id": "old"}]),
        ToolMessage(content="", tool_call_id="old", status="error"),
        AIMessage(content="", tool_calls=[{"name": "search", "args": {}, "id": "new"}]),
        ToolMessage(content="", tool_call_id="new", status="success"),
    ]}
    assert not await BaseNode._latest_tool_batch_failed(state)
