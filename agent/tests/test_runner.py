"""Tests for A23 Runner lifecycle, state machine, consumer loop, live preview acquisition, and lease safety."""
import asyncio
import io
import json
import time
import httpx
import pytest
from pathlib import Path
from PIL import Image, ImageDraw

from compute.font_builder import FontBuilderService
from compute.packager import PackagerService
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


@pytest.mark.asyncio
async def test_runner_default_live_preview_acquisition_success(test_settings: Settings):
    preview_bytes = _make_test_image_bytes(20, 60)

    def queue_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True})

    def worker_handler(request: httpx.Request) -> httpx.Response:
        if "claim" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "job_id": "job_live_1",
                    "order_id": "ord_live_1",
                    "lease_token": "12345678-1234-1234-1234-123456789abc",
                    "lease_expires_at": int(time.time() * 1000) + 300000,
                    "source_url": "https://www.myfonts.com/collections/roboto-flex",
                    "family_name": "Roboto Flex",
                    "styles": [{"id": "rf_reg", "display_name": "Regular"}],
                    "formats": ["TTF"],
                },
            )
        if "heartbeat" in request.url.path:
            return httpx.Response(200, json={"success": True, "lease_expires_at": int(time.time() * 1000) + 300000})
        return httpx.Response(404)

    def source_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str == "https://www.myfonts.com/collections/roboto-flex":
            html = '<meta property="og:image" content="https://www.myfonts.com/img/preview.png">'
            return httpx.Response(200, text=html, headers={"content-type": "text/html"})
        if url_str == "https://www.myfonts.com/img/preview.png":
            return httpx.Response(200, content=preview_bytes, headers={"content-type": "image/png"})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(queue_handler)) as q_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(worker_handler)) as w_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(source_handler)) as s_http:
        q_client = CloudflareQueueClient(test_settings, client=q_http)
        w_client = WorkerJobClient(test_settings, client=w_http)
        s_acquirer = SourceAcquirer(client=s_http)
        runner = A23Runner(test_settings, q_client, w_client, source_acquirer=s_acquirer)

        msg = QueueMessage(id="m1", lease_id="l_live", body_raw='{"job_id":"job_live_1"}', attempts=1, job_id="job_live_1")
        res = await runner.process_message(msg)

        # Output produced driven by fetched preview bytes without manual preview_input
        assert res.action == RunnerAction.HOLD_FOR_COMPLETION
        assert res.manifest is not None
        assert res.manifest.zip_file_path.exists()


@pytest.mark.asyncio
async def test_two_different_fetched_previews_produce_distinct_output(test_settings: Settings):
    img_1 = _make_test_image_bytes(10, 30)  # Thin
    img_2 = _make_test_image_bytes(10, 80)  # Thick

    def queue_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True})

    def worker_handler(request: httpx.Request) -> httpx.Response:
        if "claim" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "job_id": "job_distinct",
                    "order_id": "ord_distinct",
                    "lease_token": "12345678-1234-1234-1234-123456789abc",
                    "lease_expires_at": int(time.time() * 1000) + 300000,
                    "source_url": "https://www.myfonts.com/collections/roboto-flex",
                    "family_name": "Roboto Flex",
                    "styles": [{"id": "rf_reg", "display_name": "Regular"}],
                    "formats": ["TTF"],
                },
            )
        if "heartbeat" in request.url.path:
            return httpx.Response(200, json={"success": True, "lease_expires_at": int(time.time() * 1000) + 300000})
        return httpx.Response(404)

    # First run with image 1
    def source_handler_1(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=img_1, headers={"content-type": "image/png"})

    # Second run with image 2
    def source_handler_2(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=img_2, headers={"content-type": "image/png"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(queue_handler)) as q_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(worker_handler)) as w_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(source_handler_1)) as s_http_1, \
               httpx.AsyncClient(transport=httpx.MockTransport(source_handler_2)) as s_http_2:
        q_client = CloudflareQueueClient(test_settings, client=q_http)
        w_client = WorkerJobClient(test_settings, client=w_http)

        # Run 1
        runner1 = A23Runner(test_settings, q_client, w_client, source_acquirer=SourceAcquirer(client=s_http_1))
        msg1 = QueueMessage(id="m1", lease_id="l1", body_raw='{"job_id":"job_distinct"}', attempts=1, job_id="job_distinct")
        res1 = await runner1.process_message(msg1)

        # Run 2
        runner2 = A23Runner(test_settings, q_client, w_client, source_acquirer=SourceAcquirer(client=s_http_2))
        msg2 = QueueMessage(id="m2", lease_id="l2", body_raw='{"job_id":"job_distinct"}', attempts=1, job_id="job_distinct")
        res2 = await runner2.process_message(msg2)

        # Reconstructed output font binary changes between different preview payloads
        assert res1.manifest.files[0].sha256_hex != res2.manifest.files[0].sha256_hex


@pytest.mark.asyncio
async def test_missing_or_blocked_live_preview_fails_without_synthetic_success(test_settings: Settings):
    failed_reason_codes = []

    def queue_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True})

    def worker_handler(request: httpx.Request) -> httpx.Response:
        if "claim" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "job_id": "job_blocked_preview",
                    "order_id": "ord_blocked",
                    "lease_token": "12345678-1234-1234-1234-123456789abc",
                    "lease_expires_at": int(time.time() * 1000) + 300000,
                    "source_url": "https://www.myfonts.com/collections/roboto-flex",
                    "family_name": "Roboto Flex",
                    "styles": [{"id": "reg", "display_name": "Regular"}],
                    "formats": ["TTF"],
                },
            )
        if "fail" in request.url.path:
            data = json.loads(request.content)
            failed_reason_codes.append(data.get("reason_code"))
            return httpx.Response(200, json={"success": True, "status": "FAILED", "queue_action": "ack"})
        return httpx.Response(404)

    def source_handler_blocked(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Forbidden", headers={"content-type": "text/html"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(queue_handler)) as q_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(worker_handler)) as w_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(source_handler_blocked)) as s_http:
        q_client = CloudflareQueueClient(test_settings, client=q_http)
        w_client = WorkerJobClient(test_settings, client=w_http)
        runner = A23Runner(test_settings, q_client, w_client, source_acquirer=SourceAcquirer(client=s_http))

        msg = QueueMessage(id="m1", lease_id="l1", body_raw='{"job_id":"job_blocked_preview"}', attempts=1, job_id="job_blocked_preview")
        res = await runner.process_message(msg)

        # Blocked preview triggers controlled fail, never returns HOLD
        assert res.action == RunnerAction.ACKED
        assert any("SOURCE_ACQUISITION_BLOCKED" in str(code) for code in failed_reason_codes)


@pytest.mark.asyncio
async def test_runner_invalid_message_body(test_settings: Settings):
    acked_leases = []

    def queue_handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        if "acks" in data:
            acked_leases.extend([a["lease_id"] for a in data["acks"]])
        return httpx.Response(200, json={"success": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(queue_handler)) as q_http, \
               httpx.AsyncClient() as w_http:
        q_client = CloudflareQueueClient(test_settings, client=q_http)
        w_client = WorkerJobClient(test_settings, client=w_http)
        runner = A23Runner(test_settings, q_client, w_client)

        msg = QueueMessage(id="m1", lease_id="l1", body_raw="invalid", attempts=1, job_id=None)
        res = await runner.process_message(msg)

        assert res.action == RunnerAction.ACKED
        assert "l1" in acked_leases


@pytest.mark.asyncio
async def test_runner_claim_ack_and_retry_paths(test_settings: Settings):
    acked_leases = []
    retried_leases = []

    def queue_handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        if "acks" in data:
            acked_leases.extend([a["lease_id"] for a in data["acks"]])
        if "retries" in data:
            retried_leases.extend([r["lease_id"] for r in data["retries"]])
        return httpx.Response(200, json={"success": True})

    def worker_handler(request: httpx.Request) -> httpx.Response:
        if "job_term" in request.url.path:
            return httpx.Response(409, json={"status": "TERMINAL", "queue_action": "ack", "reason": "job_completed"})
        if "job_conflict" in request.url.path:
            return httpx.Response(409, json={"status": "LEASED", "queue_action": "retry", "reason": "job_currently_leased"})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(queue_handler)) as q_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(worker_handler)) as w_http:
        q_client = CloudflareQueueClient(test_settings, client=q_http)
        w_client = WorkerJobClient(test_settings, client=w_http)
        runner = A23Runner(test_settings, q_client, w_client)

        # 1. Claim returned ACK
        msg_term = QueueMessage(id="m1", lease_id="l_term", body_raw='{"job_id":"job_term"}', attempts=1, job_id="job_term")
        res1 = await runner.process_message(msg_term)
        assert res1.action == RunnerAction.ACKED
        assert "l_term" in acked_leases

        # 2. Claim returned RETRY
        msg_conf = QueueMessage(id="m2", lease_id="l_conf", body_raw='{"job_id":"job_conflict"}', attempts=1, job_id="job_conflict")
        res2 = await runner.process_message(msg_conf)
        assert res2.action == RunnerAction.RETRIED
        assert "l_conf" in retried_leases


@pytest.mark.asyncio
async def test_runner_heartbeat_runs_concurrently_during_slow_build(test_settings: Settings):
    heartbeat_calls = 0
    preview_bytes = _make_test_image_bytes(20, 50)

    def queue_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True})

    def worker_handler(request: httpx.Request) -> httpx.Response:
        nonlocal heartbeat_calls
        if "claim" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "job_id": "job_slow_build",
                    "order_id": "ord_slow_build",
                    "lease_token": "12345678-1234-1234-1234-123456789abc",
                    "lease_expires_at": int(time.time() * 1000) + 120000,
                    "source_url": "https://www.myfonts.com/collections/roboto-flex",
                    "family_name": "Roboto Flex",
                    "styles": [{"id": "reg", "display_name": "Regular"}],
                    "formats": ["TTF"],
                },
            )
        if "heartbeat" in request.url.path:
            heartbeat_calls += 1
            return httpx.Response(200, json={"success": True, "lease_expires_at": int(time.time() * 1000) + 120000})
        return httpx.Response(404)

    def source_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=preview_bytes, headers={"content-type": "image/png"})

    class SlowBuilder(FontBuilderService):
        def build_font(self, *args, **kwargs):
            time.sleep(1.2)  # Simulates slow CPU font build
            return super().build_font(*args, **kwargs)

    fast_hb_settings = test_settings.model_copy(update={"HEARTBEAT_INTERVAL_SECONDS": 1, "LEASE_DURATION_SECONDS": 60})

    async with httpx.AsyncClient(transport=httpx.MockTransport(queue_handler)) as q_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(worker_handler)) as w_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(source_handler)) as s_http:
        q_client = CloudflareQueueClient(fast_hb_settings, client=q_http)
        w_client = WorkerJobClient(fast_hb_settings, client=w_http)
        runner = A23Runner(
            fast_hb_settings,
            q_client,
            w_client,
            source_acquirer=SourceAcquirer(client=s_http),
            font_builder=SlowBuilder(),
        )

        msg = QueueMessage(id="m1", lease_id="l_hb", body_raw='{"job_id":"job_slow_build"}', attempts=1, job_id="job_slow_build")
        res = await runner.process_message(msg)

        # Heartbeat loop ran concurrently during the CPU build
        assert heartbeat_calls >= 1
        assert res.action == RunnerAction.HOLD_FOR_COMPLETION


@pytest.mark.asyncio
async def test_runner_fenced_heartbeat_during_build_aborts_without_hold(test_settings: Settings):
    preview_bytes = _make_test_image_bytes(20, 50)

    def queue_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True})

    def worker_handler(request: httpx.Request) -> httpx.Response:
        if "claim" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "job_id": "job_fenced_build",
                    "order_id": "ord_fenced_build",
                    "lease_token": "12345678-1234-1234-1234-123456789abc",
                    "lease_expires_at": int(time.time() * 1000) + 120000,
                    "source_url": "https://www.myfonts.com/collections/roboto-flex",
                    "family_name": "Roboto Flex",
                    "styles": [{"id": "reg", "display_name": "Regular"}],
                    "formats": ["TTF"],
                },
            )
        if "heartbeat" in request.url.path:
            return httpx.Response(409, json={"status": "FENCED"})
        if "fail" in request.url.path:
            return httpx.Response(200, json={"success": True, "status": "FAILED", "queue_action": "ack"})
        return httpx.Response(404)

    def source_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=preview_bytes, headers={"content-type": "image/png"})

    class SlowBuilder(FontBuilderService):
        def build_font(self, *args, **kwargs):
            time.sleep(1.2)
            return super().build_font(*args, **kwargs)

    fast_hb_settings = test_settings.model_copy(update={"HEARTBEAT_INTERVAL_SECONDS": 1, "LEASE_DURATION_SECONDS": 60})

    async with httpx.AsyncClient(transport=httpx.MockTransport(queue_handler)) as q_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(worker_handler)) as w_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(source_handler)) as s_http:
        q_client = CloudflareQueueClient(fast_hb_settings, client=q_http)
        w_client = WorkerJobClient(fast_hb_settings, client=w_http)
        runner = A23Runner(
            fast_hb_settings,
            q_client,
            w_client,
            source_acquirer=SourceAcquirer(client=s_http),
            font_builder=SlowBuilder(),
        )

        msg = QueueMessage(id="m1", lease_id="l_fn", body_raw='{"job_id":"job_fenced_build"}', attempts=1, job_id="job_fenced_build")
        res = await runner.process_message(msg)

        # Fenced heartbeat immediately aborts with FENCED_ABORT; no HOLD result
        assert res.action == RunnerAction.FENCED_ABORT


@pytest.mark.asyncio
async def test_runner_slow_packaging_crossing_expiry_aborts_without_hold(test_settings: Settings):
    preview_bytes = _make_test_image_bytes(20, 50)

    def queue_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True})

    def worker_handler(request: httpx.Request) -> httpx.Response:
        if "claim" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "job_id": "job_slow_pkg",
                    "order_id": "ord_slow_pkg",
                    "lease_token": "12345678-1234-1234-1234-123456789abc",
                    "lease_expires_at": int(time.time() * 1000) + 15500,
                    "source_url": "https://www.myfonts.com/collections/roboto-flex",
                    "family_name": "Roboto Flex",
                    "styles": [{"id": "reg", "display_name": "Regular"}],
                    "formats": ["TTF"],
                },
            )
        if "heartbeat" in request.url.path:
            return httpx.Response(500)
        if "fail" in request.url.path:
            return httpx.Response(200, json={"success": True, "status": "FAILED", "queue_action": "ack"})
        return httpx.Response(404)

    def source_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=preview_bytes, headers={"content-type": "image/png"})

    class SlowPackager(PackagerService):
        def package_job_output(self, *args, **kwargs):
            res = super().package_job_output(*args, **kwargs)
            time.sleep(1.0)
            return res

    async with httpx.AsyncClient(transport=httpx.MockTransport(queue_handler)) as q_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(worker_handler)) as w_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(source_handler)) as s_http:
        q_client = CloudflareQueueClient(test_settings, client=q_http)
        w_client = WorkerJobClient(test_settings, client=w_http)
        runner = A23Runner(
            test_settings,
            q_client,
            w_client,
            source_acquirer=SourceAcquirer(client=s_http),
            packager=SlowPackager(),
        )

        msg = QueueMessage(id="m1", lease_id="l_slow", body_raw='{"job_id":"job_slow_pkg"}', attempts=1, job_id="job_slow_pkg")
        res = await runner.process_message(msg)

        # Lease deadline crossing during slow packaging results in abort, not HOLD
        assert res.action != RunnerAction.HOLD_FOR_COMPLETION


@pytest.mark.asyncio
async def test_runner_run_once_and_run_loop(test_settings: Settings):
    pull_count = 0

    def queue_handler(request: httpx.Request) -> httpx.Response:
        nonlocal pull_count
        if "pull" in request.url.path:
            pull_count += 1
            if pull_count == 1:
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "messages": [
                            {"id": "msg_loop_1", "lease_id": "lease_loop_1", "body": json.dumps({"job_id": "job_term"}), "attempts": 1}
                        ],
                    },
                )
            return httpx.Response(200, json={"success": True, "messages": []})
        return httpx.Response(200, json={"success": True})

    def worker_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"status": "TERMINAL", "queue_action": "ack", "reason": "job_completed"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(queue_handler)) as q_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(worker_handler)) as w_http:
        q_client = CloudflareQueueClient(test_settings, client=q_http)
        w_client = WorkerJobClient(test_settings, client=w_http)
        runner = A23Runner(test_settings, q_client, w_client)

        # 1. run_once
        results = await runner.run_once()
        assert len(results) == 1
        assert results[0].action == RunnerAction.ACKED

        # 2. run_loop with bounded max_iterations
        await runner.run_loop(max_iterations=2)
        assert pull_count >= 2
