"""Tests for A23 Runner lifecycle, state machine, consumer loop, R2 upload, completion, and queue ACK boundary."""
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
async def test_runner_default_live_preview_and_durable_completion(test_settings: Settings):
    preview_bytes = _make_test_image_bytes(20, 60)
    acked_leases = []
    uploaded_keys = []
    completed_jobs = []

    def queue_handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        if "acks" in data:
            acked_leases.extend([a["lease_id"] for a in data["acks"]])
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
        if "artifact" in request.url.path:
            key = f"artifacts/ord_live_1/job_live_1/{request.headers['X-Artifact-SHA256']}.zip"
            uploaded_keys.append(key)
            return httpx.Response(200, json={"success": True, "artifact_key": key, "sha256": request.headers['X-Artifact-SHA256'], "size": len(request.content)})
        if "complete" in request.url.path:
            completed_jobs.append("job_live_1")
            return httpx.Response(200, json={"success": True, "status": "COMPLETED", "queue_action": "ack", "completed_at": int(time.time() * 1000)})
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

        # Full pipeline completed -> uploaded, completed, and ACKed from Queue
        assert res.action == RunnerAction.ACKED
        assert len(uploaded_keys) == 1
        assert len(completed_jobs) == 1
        assert "l_live" in acked_leases


@pytest.mark.asyncio
async def test_runner_does_not_ack_queue_when_upload_alone_succeeds(test_settings: Settings):
    preview_bytes = _make_test_image_bytes(20, 60)
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
        if "claim" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "job_id": "job_no_ack",
                    "order_id": "ord_no_ack",
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
        if "artifact" in request.url.path:
            # Upload succeeds
            return httpx.Response(200, json={"success": True, "artifact_key": "artifacts/ord/job/sha.zip", "sha256": "a"*64, "size": 100})
        if "complete" in request.url.path:
            # Complete fails with 500 / Network Error
            return httpx.Response(500, json={"error": "Database error"})
        return httpx.Response(404)

    def source_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=preview_bytes, headers={"content-type": "image/png"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(queue_handler)) as q_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(worker_handler)) as w_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(source_handler)) as s_http:
        q_client = CloudflareQueueClient(test_settings, client=q_http)
        w_client = WorkerJobClient(test_settings, client=w_http)
        runner = A23Runner(test_settings, q_client, w_client, source_acquirer=SourceAcquirer(client=s_http))

        msg = QueueMessage(id="m1", lease_id="l_no_ack", body_raw='{"job_id":"job_no_ack"}', attempts=1, job_id="job_no_ack")
        res = await runner.process_message(msg)

        # Never ACK Queue because upload alone succeeded (BLOCK 6)
        assert res.action == RunnerAction.RETRIED
        assert "l_no_ack" not in acked_leases
        assert "l_no_ack" in retried_leases


@pytest.mark.asyncio
async def test_runner_ambiguous_completion_failure_does_not_call_fail(test_settings: Settings):
    preview_bytes = _make_test_image_bytes(20, 60)
    failed_called = False
    retried_leases = []

    def queue_handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        if "retries" in data:
            retried_leases.extend([r["lease_id"] for r in data["retries"]])
        return httpx.Response(200, json={"success": True})

    def worker_handler(request: httpx.Request) -> httpx.Response:
        nonlocal failed_called
        if "claim" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "job_id": "job_ambig",
                    "order_id": "ord_ambig",
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
        if "artifact" in request.url.path:
            return httpx.Response(200, json={"success": True, "artifact_key": "artifacts/ord/job/sha.zip", "sha256": "a"*64, "size": 100})
        if "complete" in request.url.path:
            raise httpx.ConnectError("Network dropped during complete response")
        if "fail" in request.url.path:
            failed_called = True
            return httpx.Response(200, json={"success": True})
        return httpx.Response(404)

    def source_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=preview_bytes, headers={"content-type": "image/png"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(queue_handler)) as q_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(worker_handler)) as w_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(source_handler)) as s_http:
        q_client = CloudflareQueueClient(test_settings, client=q_http)
        w_client = WorkerJobClient(test_settings, client=w_http)
        runner = A23Runner(test_settings, q_client, w_client, source_acquirer=SourceAcquirer(client=s_http))

        msg = QueueMessage(id="m1", lease_id="l_ambig", body_raw='{"job_id":"job_ambig"}', attempts=1, job_id="job_ambig")
        res = await runner.process_message(msg)

        # Must not call /fail on ambiguous completion network failure (BLOCK 6)
        assert failed_called is False
        assert res.action == RunnerAction.RETRIED
        assert "l_ambig" in retried_leases


@pytest.mark.asyncio
async def test_runner_completion_409_conflict_terminal_acks_queue(test_settings: Settings):
    preview_bytes = _make_test_image_bytes(20, 60)
    acked_leases = []

    def queue_handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        if "acks" in data:
            acked_leases.extend([a["lease_id"] for a in data["acks"]])
        return httpx.Response(200, json={"success": True})

    def worker_handler(request: httpx.Request) -> httpx.Response:
        if "claim" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "job_id": "job_term_conflict",
                    "order_id": "ord_term_conflict",
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
        if "artifact" in request.url.path:
            return httpx.Response(200, json={"success": True, "artifact_key": "artifacts/ord/job/sha.zip", "sha256": "a"*64, "size": 100})
        if "complete" in request.url.path:
            return httpx.Response(409, json={"status": "CONFLICT", "queue_action": "ack", "reason": "job_already_completed"})
        return httpx.Response(404)

    def source_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=preview_bytes, headers={"content-type": "image/png"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(queue_handler)) as q_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(worker_handler)) as w_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(source_handler)) as s_http:
        q_client = CloudflareQueueClient(test_settings, client=q_http)
        w_client = WorkerJobClient(test_settings, client=w_http)
        runner = A23Runner(test_settings, q_client, w_client, source_acquirer=SourceAcquirer(client=s_http))

        msg = QueueMessage(id="m1", lease_id="l_conf_ack", body_raw='{"job_id":"job_term_conflict"}', attempts=1, job_id="job_term_conflict")
        res = await runner.process_message(msg)

        # 409 with queue_action=ack must ACK queue (Point 4)
        assert res.action == RunnerAction.ACKED
        assert "l_conf_ack" in acked_leases


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

        assert res.action == RunnerAction.FAILED_TERMINAL
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
async def test_runner_fenced_heartbeat_during_build_aborts(test_settings: Settings):
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
        def build_font(self, style_source, family_name, format_type, output_dir):
            time.sleep(1.2)
            return super().build_font(style_source, family_name, format_type, output_dir)

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

        assert res.action == RunnerAction.FENCED_ABORT


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
