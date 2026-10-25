"""Provider cancellation must not join threads or corrupt a shared SDK socket."""
import asyncio
import threading
from types import SimpleNamespace

import pandas as pd
import pytest

from quant.providers.baostock_provider import BaoStockProvider
from quant.providers.tushare_provider import TushareProvider


async def wait_for_thread(event):
    for _ in range(100):
        if event.is_set():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("provider thread did not start")


@pytest.mark.asyncio
async def test_tushare_bulk_cancels_without_blocking_event_loop():
    started, release = threading.Event(), threading.Event()
    def daily(**kwargs):
        started.set()
        release.wait(2)
        return pd.DataFrame()
    provider = TushareProvider("test-token")
    provider._pro = SimpleNamespace(daily=daily)
    task = asyncio.create_task(provider.daily_bars_bulk("2026-10-19", "2026-10-19"))
    await wait_for_thread(started)
    # A watchdog keeps this regression finite with the old blocking shutdown.
    watchdog = threading.Timer(0.75, release.set)
    watchdog.start()
    try:
        start = asyncio.get_running_loop().time()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert asyncio.get_running_loop().time() - start < 0.3
    finally:
        release.set()
        watchdog.cancel()


@pytest.mark.asyncio
async def test_baostock_cancelled_call_keeps_sdk_serial_across_instances():
    first = BaoStockProvider()
    second = BaoStockProvider()
    first._logged_in = second._logged_in = True
    started, release, second_started = threading.Event(), threading.Event(), threading.Event()
    def blocked():
        started.set()
        release.wait(2)
    task = asyncio.create_task(first._call_sync(blocked))
    await wait_for_thread(started)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    next_task = asyncio.create_task(second._call_sync(second_started.set))
    try:
        await asyncio.sleep(0.05)
        assert not second_started.is_set(), "second request reused a socket still in use"
    finally:
        release.set()
    await asyncio.wait_for(next_task, 1)
    assert second_started.is_set()
