"""Bounded workers for blocking market SDKs and numerical work.

The default asyncio executor also serves DNS and unrelated chat operations. A
slow market API must not occupy it. Cancellation only stops the async waiter;
the worker keeps its capacity permit until the synchronous call actually ends.
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import os
import weakref
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger("quant.execution")


class BoundedExecutor:
    def __init__(self, max_workers: int, name: str):
        self._max_workers = max(1, max_workers)
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_workers, thread_name_prefix=name,
        )
        self._limits = weakref.WeakKeyDictionary()

    async def run(self, func, /, *args, **kwargs):
        loop = asyncio.get_running_loop()
        limit = self._limits.get(loop)
        if limit is None:
            limit = asyncio.Semaphore(self._max_workers)
            self._limits[loop] = limit
        await limit.acquire()
        try:
            call = functools.partial(func, *args, **kwargs)
            future = self._executor.submit(contextvars.copy_context().run, call)
        except BaseException:
            limit.release()
            raise

        def finished(_):
            # Use the concurrent future, not the asyncio wrapper: cancelling
            # that wrapper does not stop an already running SDK call.
            try:
                loop.call_soon_threadsafe(limit.release)
            except RuntimeError:
                # An old request loop may already be closed at app shutdown.
                pass

        future.add_done_callback(finished)
        return await asyncio.wrap_future(future, loop=loop)

    def shutdown(self):
        """Never join worker threads on the event loop, including on timeout."""
        self._executor.shutdown(wait=False, cancel_futures=True)


def _workers(name: str, default: int, maximum: int) -> int:
    try:
        return max(1, min(maximum, int(os.environ.get(name, default))))
    except (TypeError, ValueError):
        logger.warning("Invalid worker count for %s; using %d", name, default)
        return default


_io = BoundedExecutor(_workers("QUANT_IO_WORKERS", 8, 32), "quant-io")
_compute = BoundedExecutor(_workers("QUANT_COMPUTE_WORKERS", 2, 4), "quant-compute")
_disk = BoundedExecutor(2, "quant-cache")
# BaoStock has a process-global, non-thread-safe socket. This permit must also
# survive cancellation and be shared across provider instances.
_baostock = BoundedExecutor(1, "quant-baostock")

run_io = _io.run
run_compute = _compute.run
run_disk = _disk.run
run_baostock = _baostock.run
