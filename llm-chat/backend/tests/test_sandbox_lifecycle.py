"""Stopping a sandbox stream must release all helper tasks and its process."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sandbox.worker import SSHWorker

pytestmark = pytest.mark.unit


class Reader:
    def __init__(self, first=""):
        self.first = first

    async def read(self, size):
        if self.first:
            result, self.first = self.first, ""
            return result
        await asyncio.Event().wait()


class Process:
    def __init__(self, first="ready"):
        self.stdout = Reader(first)
        self.stderr = Reader()
        self.exit_status = None
        self.killed = False
        self.closed = False

    def kill(self):
        self.killed = True
        self.exit_status = -1

    def is_closing(self):
        return False

    async def wait(self):
        return self.exit_status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True


def worker_for(process):
    worker = SSHWorker("test", "localhost")
    worker._get_conn = AsyncMock(return_value=SimpleNamespace(create_process=lambda *a, **kw: process))
    return worker


@pytest.mark.asyncio
async def test_closing_stream_reaps_readers_and_watchdog():
    process = Process()
    before = asyncio.all_tasks()
    stream = worker_for(process).exec_streaming("test", timeout=100)
    assert await anext(stream) == ("stdout", "ready")
    await stream.aclose()
    assert process.killed and process.closed
    assert not (asyncio.all_tasks() - before)


@pytest.mark.asyncio
async def test_cancelling_silent_process_reaps_all_helpers():
    process = Process(first="")
    before = asyncio.all_tasks()
    async def consume():
        return [item async for item in worker_for(process).exec_streaming("test", timeout=100)]
    task = asyncio.create_task(consume())
    await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed and process.closed
    assert not (asyncio.all_tasks() - before)


@pytest.mark.asyncio
async def test_timeout_reaps_process_without_waiting_for_eof():
    process = Process(first="")
    before = asyncio.all_tasks()
    result = [item async for item in worker_for(process).exec_streaming("test", timeout=.01)]
    assert process.killed and process.closed
    assert result[-1][0] == "__done__"
    assert '"exit_code":-1' in result[-1][1]
    assert not (asyncio.all_tasks() - before)


@pytest.mark.asyncio
async def test_total_output_is_capped_but_process_completion_is_preserved(monkeypatch):
    import sandbox.worker as module
    monkeypatch.setattr(module, "_MAX_STREAM_OUTPUT_BYTES", 12)
    process = Process()
    class FiniteReader:
        def __init__(self, blocks):
            self.blocks = iter(blocks)
        async def read(self, size):
            return next(self.blocks, "")
    process.stdout = FiniteReader(["中文" * 10, "x" * 1000])
    process.stderr = FiniteReader([])
    process.exit_status = 0
    result = [item async for item in worker_for(process).exec_streaming("test")]
    stdout = "".join(text for kind, text in result if kind == "stdout")
    assert len(stdout.encode("utf-8")) <= 12
    assert stdout == "中文中文"
    assert sum("终端输出已达上限" in text for _, text in result) == 1
    assert '"exit_code":0' in result[-1][1]
    assert process.closed
