"""Unit tests for the coordinator's portal lock.

MyMeter keeps the chart range and interval in the session between the
POST /Dashboard/Chart setup and the GET /Dashboard/ChartData read, so an update,
a backfill and a keep-alive must not use the session at the same time.
"""

import asyncio
import os
import sys
import threading
import unittest
from unittest.mock import AsyncMock, MagicMock

# Ensure repo root is on sys.path for direct unittest invocation
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tests.conftest  # noqa: F401  (stubs Home Assistant for direct unittest invocation)

from custom_components.pseg.const import AUTH_TYPE_COOKIE, CONF_AUTH_TYPE, CONF_COOKIE
from custom_components.pseg.coordinator import PsegDataUpdateCoordinator
from custom_components.pseg.pseg_client import PsegAuthError

TIMEOUT = 5
DATA = {"hourly_records": [{"start": "2026-11-01T00:00:00+00:00", "kwh": 1.0, "cost": 0.25}]}
HISTORY = [{"date": "2026-10-31", "kwh": 12.0, "cost": 3.0}]


class BlockingCall:
    """A portal call that runs in a worker thread and blocks until released."""

    def __init__(self, name, events, result=None):
        self.name = name
        self.events = events
        self.result = result
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, *args):
        self.events.append(f"{self.name} start")
        self.started.set()
        self.release.wait(TIMEOUT)
        self.events.append(f"{self.name} end")
        return self.result


async def wait_for_thread_event(event):
    """Wait for a threading.Event without blocking the event loop."""
    for _ in range(TIMEOUT * 100):
        if event.is_set():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("portal call never started")


class TestPortalLock(unittest.IsolatedAsyncioTestCase):
    def coordinator(self):
        hass = MagicMock()
        hass.is_stopping = False
        loop = asyncio.get_running_loop()

        async def run_in_executor(func, *args):
            return await loop.run_in_executor(None, func, *args)

        hass.async_add_executor_job = run_in_executor
        client = MagicMock()
        client.cookie = "MM_SID=abc"
        coordinator = PsegDataUpdateCoordinator(
            hass, client, {CONF_AUTH_TYPE: AUTH_TYPE_COOKIE, CONF_COOKIE: "MM_SID=abc"}
        )
        coordinator._raise_auth_issues = MagicMock()
        coordinator._dismiss_auth_issues = MagicMock()
        coordinator.async_record_statistics = AsyncMock()
        coordinator.async_request_refresh = AsyncMock()
        coordinator.async_set_updated_data = MagicMock()
        return coordinator

    async def test_backfill_waits_for_a_running_update(self):
        coordinator = self.coordinator()
        events = []
        update_fetch = BlockingCall("update", events, result=dict(DATA))
        coordinator.client.fetch_energy_data = update_fetch
        coordinator.client.fetch_history_range = lambda *args: events.append("backfill") or HISTORY

        update = asyncio.create_task(coordinator._async_update_data())
        await wait_for_thread_event(update_fetch.started)
        backfill = asyncio.create_task(coordinator.async_backfill_history("2026-10-01", "2026-10-31"))
        await asyncio.sleep(0.1)
        self.assertEqual(events, ["update start"])

        update_fetch.release.set()
        await asyncio.wait_for(asyncio.gather(update, backfill), TIMEOUT)
        self.assertEqual(events, ["update start", "update end", "backfill"])

    async def test_update_waits_for_a_running_backfill(self):
        coordinator = self.coordinator()
        events = []
        backfill_fetch = BlockingCall("backfill", events, result=HISTORY)
        coordinator.client.fetch_history_range = backfill_fetch
        coordinator.client.fetch_energy_data = lambda *args: events.append("update") or dict(DATA)

        backfill = asyncio.create_task(coordinator.async_backfill_history("2026-10-01", "2026-10-31"))
        await wait_for_thread_event(backfill_fetch.started)
        update = asyncio.create_task(coordinator._async_update_data())
        await asyncio.sleep(0.1)
        self.assertEqual(events, ["backfill start"])

        backfill_fetch.release.set()
        await asyncio.wait_for(asyncio.gather(backfill, update), TIMEOUT)
        self.assertEqual(events, ["backfill start", "backfill end", "update"])

    async def test_statistics_imports_do_not_overlap(self):
        coordinator = self.coordinator()
        coordinator.client.fetch_energy_data = lambda *args: dict(DATA)
        coordinator.client.fetch_history_range = lambda *args: HISTORY
        running = 0
        overlaps = []

        async def record(records, is_backfill=False):
            nonlocal running
            running += 1
            overlaps.append(running > 1)
            await asyncio.sleep(0.05)
            running -= 1

        coordinator.async_record_statistics = record
        await asyncio.wait_for(
            asyncio.gather(
                coordinator._async_update_data(),
                coordinator.async_backfill_history("2026-10-01", "2026-10-31"),
            ),
            TIMEOUT,
        )
        self.assertEqual(overlaps, [False, False])

    async def test_keep_alive_skips_while_the_portal_is_in_use(self):
        coordinator = self.coordinator()
        events = []
        update_fetch = BlockingCall("update", events, result=dict(DATA))
        coordinator.client.fetch_energy_data = update_fetch

        update = asyncio.create_task(coordinator._async_update_data())
        await wait_for_thread_event(update_fetch.started)
        self.assertTrue(await asyncio.wait_for(coordinator.async_keep_alive(), TIMEOUT))
        coordinator.client.keep_alive.assert_not_called()

        update_fetch.release.set()
        await asyncio.wait_for(update, TIMEOUT)
        self.assertTrue(await coordinator.async_keep_alive())
        coordinator.client.keep_alive.assert_called_once()

    async def test_expired_session_refreshes_without_deadlock(self):
        coordinator = self.coordinator()
        coordinator.client.keep_alive.side_effect = PsegAuthError("expired")
        coordinator.client.fetch_energy_data = lambda *args: dict(DATA)

        # Like Home Assistant's debouncer, run the refresh right away
        async def refresh_now():
            await coordinator._async_update_data()

        coordinator.async_request_refresh = AsyncMock(side_effect=refresh_now)
        self.assertTrue(await asyncio.wait_for(coordinator.async_keep_alive(), TIMEOUT))
        coordinator.async_request_refresh.assert_awaited_once()
        self.assertFalse(coordinator._portal_lock.locked())


if __name__ == "__main__":
    unittest.main()
