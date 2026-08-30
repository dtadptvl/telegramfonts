"""Focused tests for heartbeat loop-progress guarantee and compute-side liveness fence (Incident E-00035 F1)."""
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
from worker_client import HeartbeatResult, WorkerJobClient


def _start_beater(runner: A23Runner, job_id: str, expiry: list[int], last_beat_holder: list[float] | None = None):
    fenced = threading.Event()
    stop = threading.Event()
    last_beat_holder = last_beat_holder if last_beat_holder is not None else [time.monotonic()]
    t = threading.Thread(
        target=runner._heartbeat_thread_main,
        args=(job_id, "12345678-1234-1234-1234-123456789abc", expiry, fenced, stop, last_beat_holder),
        daemon=True,
    )
    t.start()
    return t, fenced, stop, last_beat_holder


@pytest.mark.asyncio
async def test_hanging_beat_loop_progress_guarantee(test_settings: Settings, caplog):
    """(1) Hanging beat: beat callable sleeps 5s, timeout is 0.1s, interval is tiny.
    Proves:
    - loop completes >= 2 iterations within a bounded wall (never blocks on stuck thread);
    - warning is logged (with class name);
    - fenced event is NOT set.
    """
    call_count = {"n": 0}

    def hanging_beat(job_id: str, lease_token: str, timeout: float | None = None):
        call_count["n"] += 1
        # Block thread for 5 seconds (much longer than 0.1s timeout)
        time.sleep(5.0)
        return HeartbeatResult(success=True, fenced=False, lease_expires_at=9999999999999)

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
        w_client.heartbeat_sync = MagicMock(side_effect=hanging_beat)
        q_client = CloudflareQueueClient(hb_settings, client=q_http)
        runner = A23Runner(hb_settings, q_client, w_client)

        initial_expiry = int(time.time() * 1000) + 300000
        expiry = [initial_expiry]
        last_beat_holder = [time.monotonic()]

        t0 = time.monotonic()
        with caplog.at_level(logging.WARNING):
            t, fenced, stop, holder = _start_beater(runner, "job_hb_hang_loop", expiry, last_beat_holder)
            try:
                # Wait for at least 2 beat attempts (cadence is 1s + 0.1s timeout -> ~2.5s)
                deadline = time.monotonic() + 3.5
                while call_count["n"] < 2 and time.monotonic() < deadline:
                    await asyncio.sleep(0.1)

                wall_elapsed = time.monotonic() - t0
                assert call_count["n"] >= 2, f"Expected >= 2 beat attempts, got {call_count['n']}"
                assert wall_elapsed < 4.5, f"Loop took too long ({wall_elapsed:.2f}s) - should not block on hanging thread"
                assert not fenced.is_set(), "Timed out heartbeat must NOT set fenced"

                warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
                assert any("Heartbeat timed out" in m or "TimeoutError" in m for m in warnings), (
                    f"Expected timeout warning in logs: {warnings}"
                )
            finally:
                stop.set()
                await asyncio.to_thread(t.join, 2.0)


@pytest.mark.asyncio
async def test_successful_beats_update_holders(test_settings: Settings):
    """(2) Successful beats update expiry_holder and last_successful_beat holder."""
    beats = []

    def successful_beat(job_id: str, lease_token: str, timeout: float | None = None):
        beats.append(time.monotonic())
        new_expiry = int(time.time() * 1000) + 300000 + len(beats) * 1000
        return HeartbeatResult(success=True, fenced=False, lease_expires_at=new_expiry)

    hb_settings = test_settings.model_copy(
        update={
            "HEARTBEAT_INTERVAL_SECONDS": 1,
            "HEARTBEAT_TIMEOUT_SECONDS": 0.5,
            "LEASE_DURATION_SECONDS": 300,
        }
    )

    sync_http = httpx.Client()
    async with httpx.AsyncClient() as w_http, httpx.AsyncClient() as q_http:
        w_client = WorkerJobClient(hb_settings, client=w_http, sync_client=sync_http)
        w_client.heartbeat_sync = MagicMock(side_effect=successful_beat)
        q_client = CloudflareQueueClient(hb_settings, client=q_http)
        runner = A23Runner(hb_settings, q_client, w_client)

        initial_expiry = int(time.time() * 1000) + 300000
        expiry = [initial_expiry]
        last_beat_holder = [0.0]

        t, fenced, stop, holder = _start_beater(runner, "job_hb_success", expiry, last_beat_holder)
        try:
            deadline = time.monotonic() + 3.0
            while len(beats) < 2 and time.monotonic() < deadline:
                await asyncio.sleep(0.1)

            assert len(beats) >= 2
            assert not fenced.is_set()
            assert expiry[0] > initial_expiry
            assert holder[0] > 0.0
            assert holder[0] <= time.monotonic()
        finally:
            stop.set()
            await asyncio.to_thread(t.join, 2.0)


@pytest.mark.asyncio
async def test_fenced_beat_breaks_loop_and_sets_event(test_settings: Settings):
    """(3) Fenced beat (409) breaks the heartbeat loop and sets fenced_event."""
    beats = []

    def fenced_beat(job_id: str, lease_token: str, timeout: float | None = None):
        beats.append(time.monotonic())
        return HeartbeatResult(success=False, fenced=True)

    hb_settings = test_settings.model_copy(
        update={
            "HEARTBEAT_INTERVAL_SECONDS": 1,
            "HEARTBEAT_TIMEOUT_SECONDS": 0.5,
            "LEASE_DURATION_SECONDS": 300,
        }
    )

    sync_http = httpx.Client()
    async with httpx.AsyncClient() as w_http, httpx.AsyncClient() as q_http:
        w_client = WorkerJobClient(hb_settings, client=w_http, sync_client=sync_http)
        w_client.heartbeat_sync = MagicMock(side_effect=fenced_beat)
        q_client = CloudflareQueueClient(hb_settings, client=q_http)
        runner = A23Runner(hb_settings, q_client, w_client)

        expiry = [int(time.time() * 1000) + 300000]
        last_beat_holder = [time.monotonic()]

        t, fenced, stop, holder = _start_beater(runner, "job_hb_fenced", expiry, last_beat_holder)
        try:
            deadline = time.monotonic() + 3.0
            while not fenced.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.1)

            assert fenced.is_set(), "Fenced beat must set fenced_event"
            # Give thread time to exit
            await asyncio.to_thread(t.join, 2.0)
            assert not t.is_alive(), "Heartbeat thread must exit on fenced"
        finally:
            stop.set()


def test_compute_side_liveness_fence_guard(test_settings: Settings, caplog):
    """(4) Compute-side liveness fence guard:
    - Stale holder (> 3 * interval + 15s margin) raises RuntimeError('LEASE_FENCED_OR_EXPIRED')
      and logs WARNING 'heartbeat liveness lost';
    - Fresh holder passes without error.
    """
    hb_settings = test_settings.model_copy(
        update={
            "HEARTBEAT_INTERVAL_SECONDS": 60,
            "LEASE_DURATION_SECONDS": 300,
        }
    )
    sync_http = httpx.Client()
    w_client = WorkerJobClient(hb_settings, sync_client=sync_http)
    q_client = CloudflareQueueClient(hb_settings)
    runner = A23Runner(hb_settings, q_client, w_client)

    # 1. Fresh holder: elapsed = 10s (limit = 3*60 + 15 = 195s) -> passes
    fresh_fenced = threading.Event()
    fresh_holder = [time.monotonic() - 10.0]
    runner._check_liveness_fence(fresh_fenced, fresh_holder)
    assert not fresh_fenced.is_set()

    # 2. Stale holder: elapsed = 200s (> 195s) -> raises and sets fenced
    stale_fenced = threading.Event()
    stale_holder = [time.monotonic() - 200.0]
    with caplog.at_level(logging.WARNING):
        with pytest.raises(RuntimeError, match="LEASE_FENCED_OR_EXPIRED"):
            runner._check_liveness_fence(stale_fenced, stale_holder)

    assert stale_fenced.is_set()
    warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("heartbeat liveness lost" in m.lower() for m in warnings)
