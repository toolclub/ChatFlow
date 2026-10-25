"""Concurrency regressions: a timed-out SDK must not freeze chat/DNS."""
import asyncio
import contextvars
import threading
import unittest
from unittest.mock import patch

from quant.execution import BoundedExecutor


class QuantExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = BoundedExecutor(1, "quant-test")
        self.release_worker = threading.Event()
        self.started = threading.Event()

    async def asyncTearDown(self):
        self.release_worker.set()
        self.pool.shutdown()

    def blocking_call(self):
        self.started.set()
        self.release_worker.wait(2)
        return "done"

    async def wait_started(self):
        for _ in range(100):
            if self.started.is_set():
                return
            await asyncio.sleep(0.005)
        self.fail("worker did not start")

    async def test_cancelled_call_keeps_capacity_until_thread_finishes(self):
        with patch.object(self.pool._executor, "submit", wraps=self.pool._executor.submit) as submit:
            first = asyncio.create_task(self.pool.run(self.blocking_call))
            await self.wait_started()
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            # Retrying a timed-out SDK does not accumulate executor work items.
            for _ in range(4):
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(self.pool.run(lambda: "retry"), 0.01)
            self.assertEqual(submit.call_count, 1)
            self.release_worker.set()
            self.assertEqual(await asyncio.wait_for(self.pool.run(lambda: "next"), 1), "next")

    async def test_saturated_quant_pool_does_not_starve_default_executor(self):
        task = asyncio.create_task(self.pool.run(self.blocking_call))
        await self.wait_started()
        # DNS uses this same default executor. It must remain available to chat.
        result = await asyncio.wait_for(asyncio.to_thread(lambda: "dns-ready"), 0.5)
        self.assertEqual(result, "dns-ready")
        self.release_worker.set()
        self.assertEqual(await task, "done")

    async def test_context_and_kwargs_are_forwarded(self):
        marker = contextvars.ContextVar("quant-test-context", default="missing")
        token = marker.set("request")
        try:
            result = await self.pool.run(lambda *, suffix: marker.get() + suffix, suffix="-ok")
            self.assertEqual(result, "request-ok")
        finally:
            marker.reset(token)

    async def test_failure_returns_capacity_and_propagates(self):
        def fail():
            raise ValueError("provider unavailable")
        with self.assertRaisesRegex(ValueError, "provider unavailable"):
            await self.pool.run(fail)
        self.assertEqual(await asyncio.wait_for(self.pool.run(lambda: 42), 1), 42)

    async def test_shutdown_never_joins_a_running_worker(self):
        task = asyncio.create_task(self.pool.run(self.blocking_call))
        await self.wait_started()
        loop = asyncio.get_running_loop()
        before = loop.time()
        self.pool.shutdown()
        self.assertLess(loop.time() - before, 0.1)
        self.release_worker.set()
        self.assertEqual(await task, "done")


if __name__ == "__main__":
    unittest.main()
