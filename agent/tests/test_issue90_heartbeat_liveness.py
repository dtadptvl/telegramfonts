"""Issue #90: causal tests for lease-heartbeat liveness during long compute.

Proven defect (A23 attempt 5, evidence 5462933057): ~21 minutes of healthy
compute starved the asyncio heartbeat task (0 heartbeat requests, 0 D1 lease
extensions) because synchronous compute blocked the event loop; the cron
reaper then correctly fenced the healthy run at lease expiry. Fix: the lease
heartbeat runs on a dedicated thread (independent scheduler) that loop
blocking cannot starve.

These tests prove:
  T1 heartbeats continue (lease extensions observed) while the event loop is
     blocked by synchronous compute — the exact causal repro;
  T2 a fenced/expired lease is still detected and heartbeats stop (fencing
     semantics unchanged — a truly dead/fenced worker is still reaped by the
     unchanged edge reaper);
  T3 wire-level heartbeat semantics unchanged (endpoint, payload incl.
     extend_seconds == LEASE_DURATION_SECONDS, bearer auth header present);
  T4 heartbeats stop promptly at job end (no leaked beaters);
  T5 end-to-end process_message: blocking acquisition cannot starve the
     heartbeat and the job still completes.
"""
import asyncio
import io
import json
import threading
import time

import httpx
import pytest

pytestmark = pytest.mark.integration

from PIL import Image, ImageDraw

from compute.source import SourceAcquirer
from config import Settings
from queue_client import CloudflareQueueClient, QueueMessage
from runner import A23Runner, RunnerAction
from worker_client import WorkerJobClient


def _make_test_image_bytes(stroke_x0: int, stroke_x1: int) -> bytes:
    img = Image.new("L", (100, 100), color=255)
    draw = ImageDraw.Draw(img)
    draw.rectangle([stroke_x0, 10, stroke_x1, 90], fill=0)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _claim_payload(job_id: str, order_id: str, lease_ms_ahead: int = 300000) -> dict:
    return {
        "job_id": job_id,
        "order_id": order_id,
        "lease_token": "12345678-1234-1234-1234-123456789abc",
        "lease_expires_at": int(time.time() * 1000) + lease_ms_ahead,
        "source_url": "https://www.myfonts.com/collections/be-vietnam-pro",
        "family_name": "Be Vietnam Pro",
        "styles": [{"id": "regular", "display_name": "Regular"}],
        "formats": ["TTF"],
    }


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
async def test_heartbeats_continue_while_event_loop_blocked(test_settings: Settings):
    """CAUSAL REPRO of the attempt-5 defect: a blocked event loop must not
    starve lease extensions. With the old asyncio heartbeat task this test
    observes zero beats during the freeze; the dedicated thread keeps the
    lease alive."""
    beats = []
    expiries_issued = []

    def worker_handler(request: httpx.Request) -> httpx.Response:
        if "heartbeat" in request.url.path:
            beats.append(time.monotonic())
            new_expiry = int(time.time() * 1000) + 300000 + len(beats) * 1000
            expiries_issued.append(new_expiry)
            return httpx.Response(200, json={"success": True, "lease_expires_at": new_expiry})
        return httpx.Response(404)

    hb_settings = test_settings.model_copy(
        update={"HEARTBEAT_INTERVAL_SECONDS": 1, "LEASE_DURATION_SECONDS": 300}
    )
    sync_http = httpx.Client(transport=httpx.MockTransport(worker_handler))
    async with httpx.AsyncClient(transport=httpx.MockTransport(worker_handler)) as w_http, \
               httpx.AsyncClient() as q_http:
        w_client = WorkerJobClient(hb_settings, client=w_http, sync_client=sync_http)
        q_client = CloudflareQueueClient(hb_settings, client=q_http)
        runner = A23Runner(hb_settings, q_client, w_client)

        initial_expiry = int(time.time() * 1000) + 300000
        expiry = [initial_expiry]
        t, fenced, stop = _start_beater(runner, "job_hb_freeze", expiry)
        try:
            # Exact defect repro: synchronous compute freezes the event loop.
            # No coroutine/task on this loop can run during this window.
            time.sleep(3.5)

            assert not fenced.is_set(), "healthy lease must not be fenced"
            assert len(beats) >= 2, (
                "heartbeats must continue while the event loop is blocked "
                f"(observed {len(beats)} beats in 3.5s at 1s cadence)"
            )
            # Lease extensions observed: expiry advanced to the last issued value.
            assert expiry[0] == expiries_issued[-1]
            assert expiry[0] > initial_expiry
        finally:
            stop.set()
            await asyncio.to_thread(t.join, 5.0)
            assert not t.is_alive(), "heartbeat thread must exit after stop"

        # T4 (part): no further beats after stop.
        beats_at_stop = len(beats)
        await asyncio.sleep(1.6)
        assert len(beats) == beats_at_stop, "heartbeats must stop after stop_event"


@pytest.mark.asyncio
async def test_fenced_lease_still_detected_and_beater_stops(test_settings: Settings):
    """Fencing semantics unchanged: an expired/fenced lease (409 from edge)
    is detected, the beater marks fenced and stops beating; the unchanged
    edge reaper remains the authority that fences dead workers."""
    beats = []

    def worker_handler(request: httpx.Request) -> httpx.Response:
        if "heartbeat" in request.url.path:
            beats.append(time.monotonic())
            return httpx.Response(409, json={"status": "FENCED"})
        return httpx.Response(404)

    hb_settings = test_settings.model_copy(
        update={"HEARTBEAT_INTERVAL_SECONDS": 1, "LEASE_DURATION_SECONDS": 300}
    )
    sync_http = httpx.Client(transport=httpx.MockTransport(worker_handler))
    async with httpx.AsyncClient(transport=httpx.MockTransport(worker_handler)) as w_http, \
               httpx.AsyncClient() as q_http:
        w_client = WorkerJobClient(hb_settings, client=w_http, sync_client=sync_http)
        q_client = CloudflareQueueClient(hb_settings, client=q_http)
        runner = A23Runner(hb_settings, q_client, w_client)

        expiry = [int(time.time() * 1000) + 300000]
        t, fenced, stop = _start_beater(runner, "job_hb_dead", expiry)
        try:
            deadline = time.monotonic() + 5.0
            while not fenced.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
            assert fenced.is_set(), "409 heartbeat must set the fenced event"

            beats_at_fence = len(beats)
            assert beats_at_fence >= 1
            await asyncio.sleep(2.2)
            assert len(beats) == beats_at_fence, (
                "no further heartbeats after fence (beater must stop)"
            )
        finally:
            stop.set()
            await asyncio.to_thread(t.join, 5.0)


@pytest.mark.asyncio
async def test_heartbeat_wire_semantics_unchanged(test_settings: Settings):
    """Endpoint, auth header, and payload (incl. extend_seconds ==
    LEASE_DURATION_SECONDS) are byte-for-byte the canonical heartbeat shape:
    edge-side cadence/lease/reaper semantics see an unchanged request."""
    captured = []

    def worker_handler(request: httpx.Request) -> httpx.Response:
        if "heartbeat" in request.url.path:
            captured.append(request)
            return httpx.Response(200, json={"success": True, "lease_expires_at": 9999999999999})
        return httpx.Response(404)

    hb_settings = test_settings.model_copy(
        update={"HEARTBEAT_INTERVAL_SECONDS": 1, "LEASE_DURATION_SECONDS": 300}
    )
    sync_http = httpx.Client(transport=httpx.MockTransport(worker_handler))
    async with httpx.AsyncClient(transport=httpx.MockTransport(worker_handler)) as w_http, \
               httpx.AsyncClient() as q_http:
        w_client = WorkerJobClient(hb_settings, client=w_http, sync_client=sync_http)
        q_client = CloudflareQueueClient(hb_settings, client=q_http)
        runner = A23Runner(hb_settings, q_client, w_client)

        expiry = [int(time.time() * 1000) + 300000]
        t, fenced, stop = _start_beater(runner, "job_hb_wire", expiry)
        try:
            deadline = time.monotonic() + 5.0
            while not captured and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
        finally:
            stop.set()
            await asyncio.to_thread(t.join, 5.0)

        assert captured, "heartbeat request must be observed"
        req = captured[0]
        assert req.url.path == "/internal/jobs/job_hb_wire/heartbeat"
        assert req.method == "POST"
        assert "Authorization" in req.headers and req.headers["Authorization"], (
            "bearer auth header must be present (value never asserted/printed)"
        )
        payload = json.loads(req.content)
        assert set(payload.keys()) == {"worker_id", "lease_token", "extend_seconds"}
        assert payload["worker_id"] == hb_settings.A23_WORKER_ID
        assert payload["lease_token"] == "12345678-1234-1234-1234-123456789abc"
        assert payload["extend_seconds"] == hb_settings.LEASE_DURATION_SECONDS


class BlockingFixtureSourceAcquirer(SourceAcquirer):
    """Test adapter: blocks the event loop synchronously inside acquisition
    (reproducing the attempt-5 freeze) before delegating to the fixture path."""

    def __init__(self, preview_bytes: bytes, block_seconds: float, **kwargs):
        super().__init__(**kwargs)
        self.preview_bytes = preview_bytes
        self.block_seconds = block_seconds
        self.store_dir = None
        self.store = None

    async def acquire_source(self, source_url, styles, preview_input=None, allow_web_fallback=False):
        time.sleep(self.block_seconds)  # synchronous compute freezing the loop
        return await super().acquire_source(
            source_url,
            styles,
            preview_input=self.preview_bytes,
            allow_web_fallback=allow_web_fallback,
        )


@pytest.mark.asyncio
async def test_process_message_blocking_compute_keeps_heartbeat_and_completes(test_settings: Settings):
    """End-to-end wiring: process_message with a blocking acquisition
    phase still emits heartbeats (lease extensions) and completes the job."""
    preview_bytes = _make_test_image_bytes(20, 60)
    beats = []
    acked_leases = []

    def queue_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.content:
            try:
                data = json.loads(request.content)
            except ValueError:
                data = {}
            if "acks" in data:
                acked_leases.extend([a["lease_id"] for a in data["acks"]])
        return httpx.Response(200, json={"success": True})

    def worker_handler(request: httpx.Request) -> httpx.Response:
        if "claim" in request.url.path:
            return httpx.Response(200, json=_claim_payload("job_hb_e2e", "ord_hb_e2e"))
        if "heartbeat" in request.url.path:
            beats.append(time.monotonic())
            return httpx.Response(
                200, json={"success": True, "lease_expires_at": int(time.time() * 1000) + 300000}
            )
        if "artifact" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "artifact_key": f"artifacts/ord_hb_e2e/job_hb_e2e/{request.headers['X-Artifact-SHA256']}.zip",
                    "sha256": request.headers["X-Artifact-SHA256"],
                    "size": int(request.headers["Content-Length"]),
                },
            )
        if "complete" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "status": "COMPLETED",
                    "queue_action": "ack",
                    "completed_at": int(time.time() * 1000),
                    "artifact_key": "artifacts/ord_hb_e2e/job_hb_e2e/artifact.zip",
                },
            )
        if "fail" in request.url.path:
            return httpx.Response(200, json={"success": True, "status": "FAILED", "queue_action": "ack"})
        return httpx.Response(404)

    hb_settings = test_settings.model_copy(
        update={"HEARTBEAT_INTERVAL_SECONDS": 1, "LEASE_DURATION_SECONDS": 300}
    )
    sync_http = httpx.Client(transport=httpx.MockTransport(worker_handler))
    async with httpx.AsyncClient(transport=httpx.MockTransport(queue_handler)) as q_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(worker_handler)) as w_http, \
               httpx.AsyncClient() as s_http:
        q_client = CloudflareQueueClient(hb_settings, client=q_http)
        w_client = WorkerJobClient(hb_settings, client=w_http, sync_client=sync_http)
        runner = A23Runner(
            hb_settings,
            q_client,
            w_client,
            source_acquirer=BlockingFixtureSourceAcquirer(preview_bytes, 2.6, client=s_http),
        )

        msg = QueueMessage(
            id="m_hb_e2e",
            lease_id="l_hb_e2e",
            body_raw='{"job_id":"job_hb_e2e"}',
            attempts=1,
            job_id="job_hb_e2e",
        )
        res = await runner.process_message(msg)

        assert res.action == RunnerAction.ACKED
        assert len(beats) >= 2, (
            "heartbeats must continue during the blocking acquisition phase "
            f"(observed {len(beats)} beats)"
        )
        assert acked_leases == ["l_hb_e2e"]
