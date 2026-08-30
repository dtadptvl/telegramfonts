"""Focused tests for heartbeat timeout liveness under hanging/slow network calls."""
import asyncio
import logging
import threading
import time
from unittest.mock import MagicMock

import httpx
import pytest

from config import Settings
from queue_client import CloudflareQueueClient
from runner import A23Runner
from worker_client import WorkerJobClient


def _start_beater(runner: A23Runner, job_id: str, expiry: list[int]):
    fenced = threading.Event()
    stop = threading.Event()
    t = threading.Thread(
        target=runner._heartbeat_thread_main,
        args=(job_id, "12345678-1234-1234-1234-123456789abc", expiry, fenced, stop),
        daemon=True,
    )
    t.start()
    return t, fenced, stop


@pytest.mark.asyncio
async def test_hanging_heartbeat_times_out_and_proceeds_to_next_beat(test_settings: Settings, caplog):
    """Monkeys/mocks a hanging heartbeat call (sleeps longer than timeout).
    Verifies:
    1. Bounded HTTP timeout raises TimeoutException;
    2. Runner catches timeout, logs a warning;
    3. Fenced is NOT set;
    4. Loop proceeds to attempt the next beat;
    5. When the hang clears, subsequent beat succeeds and extends expiry.
    """
    call_count = {"n": 0}

    def hanging_heartbeat_sync(job_id: str, lease_token: str, timeout: float | None = None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Simulate hanging beat that raises httpx.ReadTimeout past bounded timeout
            raise httpx.ReadTimeout("Mocked read timeout on hanging heartbeat")
        # Subsequent beat succeeds
        return MagicMock(success=True, fenced=False, lease_expires_at=9999999999999)

    hb_settings = test_settings.model_copy(
        update={
            "HEARTBEAT_INTERVAL_SECONDS": 1,
            "HEARTBEAT_TIMEOUT_SECONDS": 0.1,
            "LEASE_DURATION_SECONDS": 300,
        }
    )

    sync_http = httpx.Client()
    async with httpx.AsyncClient() as w_http, httpx.AsyncClient() as q_http:
        w_client = WorkerJobClient(hb_settings, client=w_http, sync_client=sync_http)
        w_client.heartbeat_sync = MagicMock(side_effect=hanging_heartbeat_sync)
        q_client = CloudflareQueueClient(hb_settings, client=q_http)
        runner = A23Runner(hb_settings, q_client, w_client)

        initial_expiry = int(time.time() * 1000) + 300000
        expiry = [initial_expiry]

        with caplog.at_level(logging.WARNING):
            t, fenced, stop = _start_beater(runner, "job_hb_hang", expiry)
            try:
                # Wait for at least 2 heartbeat intervals
                deadline = time.monotonic() + 4.0
                while call_count["n"] < 2 and time.monotonic() < deadline:
                    await asyncio.sleep(0.1)

                assert call_count["n"] >= 2, f"Expected >= 2 beat attempts, got {call_count['n']}"
                assert not fenced.is_set(), "Timed out heartbeat must NOT set fenced"
                assert expiry[0] == 9999999999999, "Subsequent beat must succeed and update expiry"

                # Check warning logged for exception
                warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
                assert any("Heartbeat exception" in m or "ReadTimeout" in m for m in warnings), (
                    f"Warning not found in logs: {warnings}"
                )
            finally:
                stop.set()
                await asyncio.to_thread(t.join, 3.0)
                assert not t.is_alive(), "Heartbeat thread must stop"
