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


class FixtureSourceAcquirer(SourceAcquirer):
    """Run-loop fixture adapter for the explicit test-only preview input path."""

    def __init__(self, preview_bytes: bytes, **kwargs):
        super().__init__(**kwargs)
        self.preview_bytes = preview_bytes

    async def acquire_source(self, source_url, styles, preview_input=None, allow_web_fallback=False):
        return await super().acquire_source(
            source_url,
            styles,
            preview_input=self.preview_bytes,
            allow_web_fallback=allow_web_fallback,
        )


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
                    "source_url": "https://www.myfonts.com/collections/be-vietnam-pro",
                    "family_name": "Be Vietnam Pro",
                    "styles": [{"id": "regular", "display_name": "Regular"}],
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
        res = await runner.process_message(msg, preview_input=preview_bytes)

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
                    "source_url": "https://www.myfonts.com/collections/be-vietnam-pro",
                    "family_name": "Be Vietnam Pro",
                    "styles": [{"id": "regular", "display_name": "Regular"}],
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
        res = await runner.process_message(msg, preview_input=preview_bytes)

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
                    "source_url": "https://www.myfonts.com/collections/be-vietnam-pro",
                    "family_name": "Be Vietnam Pro",
                    "styles": [{"id": "regular", "display_name": "Regular"}],
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
        res = await runner.process_message(msg, preview_input=preview_bytes)

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
                    "source_url": "https://www.myfonts.com/collections/be-vietnam-pro",
                    "family_name": "Be Vietnam Pro",
                    "styles": [{"id": "regular", "display_name": "Regular"}],
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

        msg = QueueMessage(id="m1", lease_id="l_conf", body_raw='{"job_id":"job_term_conflict"}', attempts=1, job_id="job_term_conflict")
        res = await runner.process_message(msg, preview_input=preview_bytes)

        # 409 with queue_action=ack must ACK queue (Point 4)
        assert res.action == RunnerAction.ACKED
        assert "l_conf" in acked_leases


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
                    "source_url": "https://www.myfonts.com/collections/unknown-font",
                    "family_name": "Unknown Font",
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
        class UnobservableBrowser:
            browser_version = "Chromium/Test"

            async def start(self):
                return None

            async def observe_source_font(self, source_url, style_name, family_name):
                raise ValueError("NO_OBSERVABLE_BROWSER_FONT_FACES")

            def close(self):
                return None

        runner = A23Runner(
            test_settings,
            q_client,
            w_client,
            source_acquirer=SourceAcquirer(
                client=s_http,
                observation_store_dir=test_settings.SCRATCH_DIR / "unobservable",
                browser_session_factory=UnobservableBrowser,
            ),
        )

        msg = QueueMessage(id="m1", lease_id="l1", body_raw='{"job_id":"job_blocked_preview"}', attempts=1, job_id="job_blocked_preview")
        res = await runner.process_message(msg)

        assert res.action == RunnerAction.FAILED_TERMINAL
        assert any("NO_OBSERVABLE_BROWSER_FONT_FACES" in str(code) for code in failed_reason_codes)


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
async def test_runner_unexpected_exception_after_claim_fails_and_retries(test_settings: Settings):
    failed_payloads = []
    retried_leases = []

    def queue_handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        if "retries" in data:
            retried_leases.extend([item["lease_id"] for item in data["retries"]])
        return httpx.Response(200, json={"success": True})

    def worker_handler(request: httpx.Request) -> httpx.Response:
        if "claim" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "job_id": "job_unexpected_exception",
                    "order_id": "ord_unexpected_exception",
                    "lease_token": "12345678-1234-1234-1234-123456789abc",
                    "lease_expires_at": int(time.time() * 1000) + 300000,
                    "source_url": "https://www.myfonts.com/collections/be-vietnam-pro",
                    "styles": [{"id": "regular", "display_name": "Regular"}],
                    "formats": ["TTF"],
                },
            )
        if "heartbeat" in request.url.path:
            return httpx.Response(
                200,
                json={"success": True, "lease_expires_at": int(time.time() * 1000) + 300000},
            )
        if "fail" in request.url.path:
            failed_payloads.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={"success": True, "status": "RETRY", "queue_action": "retry", "delay_seconds": 17},
            )
        return httpx.Response(404)

    class UnexpectedSourceAcquirer(SourceAcquirer):
        async def acquire_source(self, source_url, styles, preview_input=None, allow_web_fallback=False):
            raise TypeError("unexpected source payload containing untrusted details")

    async with httpx.AsyncClient(transport=httpx.MockTransport(queue_handler)) as q_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(worker_handler)) as w_http:
        q_client = CloudflareQueueClient(test_settings, client=q_http)
        w_client = WorkerJobClient(test_settings, client=w_http)
        runner = A23Runner(
            test_settings,
            q_client,
            w_client,
            source_acquirer=UnexpectedSourceAcquirer(),
        )

        msg = QueueMessage(
            id="m_unexpected",
            lease_id="l_unexpected",
            body_raw='{"job_id":"job_unexpected_exception"}',
            attempts=1,
            job_id="job_unexpected_exception",
        )
        result = await runner.process_message(msg)

        assert result.action == RunnerAction.RETRIED
        assert result.reason == "UNEXPECTED_RUNTIME_ERROR"
        assert failed_payloads == [
            {
                "worker_id": test_settings.A23_WORKER_ID,
                "lease_token": "12345678-1234-1234-1234-123456789abc",
                "retryable": True,
                "reason_code": "UNEXPECTED_RUNTIME_ERROR",
            }
        ]
        assert retried_leases == ["l_unexpected"]


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
                    "source_url": "https://www.myfonts.com/collections/be-vietnam-pro",
                    "family_name": "Be Vietnam Pro",
                    "styles": [{"id": "regular", "display_name": "Regular"}],
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
        res = await runner.process_message(msg, preview_input=preview_bytes)

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


@pytest.mark.asyncio
async def test_runner_entrypoint_lifecycle_start_stop_close(test_settings: Settings):
    pull_count = 0

    def queue_handler(request: httpx.Request) -> httpx.Response:
        nonlocal pull_count
        if "pull" in request.url.path:
            pull_count += 1
            return httpx.Response(200, json={"success": True, "messages": []})
        return httpx.Response(200, json={"success": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(queue_handler)) as q_http, \
               httpx.AsyncClient() as w_http, \
               httpx.AsyncClient() as s_http:
        q_client = CloudflareQueueClient(test_settings, client=q_http)
        w_client = WorkerJobClient(test_settings, client=w_http)
        s_acquirer = SourceAcquirer(client=s_http)
        runner = A23Runner(
            test_settings,
            queue_client=q_client,
            worker_client=w_client,
            source_acquirer=s_acquirer,
        )

        stop_event = asyncio.Event()

        async def stop_after_brief_delay():
            await asyncio.sleep(0.05)
            stop_event.set()

        asyncio.create_task(stop_after_brief_delay())

        # Proves runner.run_loop(stop_event=stop_event) terminates cleanly
        await runner.run_loop(stop_event=stop_event)
        assert stop_event.is_set() is True

        # Proves runner.close() executes cleanly and closes owned clients without raising
        await runner.close()


@pytest.mark.asyncio
async def test_multi_consumer_queue_duplicate_and_ack_loss_redelivery_proves_singular_compute(test_settings: Settings):
    """Proves >=2 concurrent consumers handling Queue message duplicate / ACK loss produce singular compute and upload."""
    preview_bytes = _make_test_image_bytes(20, 60)
    uploaded_artifacts = []
    completed_jobs = []
    acked_queue_leases = []
    job_completed_in_d1 = False

    # Simulate Queue state
    queue_messages_worker_1 = [
        {"id": "msg_queue_01", "body": {"job_id": "job_multi_dup"}, "lease_id": "lease_q_worker_1"}
    ]
    # Simulated redelivered message for worker 2
    queue_messages_worker_2 = [
        {"id": "msg_queue_01_redelivery", "body": {"job_id": "job_multi_dup"}, "lease_id": "lease_q_worker_2"}
    ]

    def queue_handler_1(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        if "pull" in request.url.path:
            return httpx.Response(200, json={"success": True, "messages": queue_messages_worker_1})
        if "ack" in request.url.path:
            if "acks" in data:
                acked_queue_leases.extend([a["lease_id"] for a in data["acks"]])
            return httpx.Response(200, json={"success": True})
        return httpx.Response(200, json={"success": True})

    def queue_handler_2(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        if "pull" in request.url.path:
            return httpx.Response(200, json={"success": True, "messages": queue_messages_worker_2})
        if "ack" in request.url.path:
            if "acks" in data:
                acked_queue_leases.extend([a["lease_id"] for a in data["acks"]])
            return httpx.Response(200, json={"success": True})
        return httpx.Response(200, json={"success": True})

    def worker_handler(request: httpx.Request) -> httpx.Response:
        nonlocal job_completed_in_d1
        if "claim" in request.url.path:
            if job_completed_in_d1:
                # After completion, D1 responds 409 TERMINAL + queue_action: ack
                return httpx.Response(
                    409,
                    json={
                        "status": "TERMINAL",
                        "queue_action": "ack",
                        "reason": "job_already_terminal",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "job_id": "job_multi_dup",
                    "order_id": "ord_multi_dup",
                    "lease_token": "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d",
                    "lease_expires_at": int(time.time() * 1000) + 300000,
                    "source_url": "https://www.myfonts.com/collections/be-vietnam-pro",
                    "family_name": "Be Vietnam Pro",
                    "styles": [{"id": "regular", "display_name": "Regular"}],
                    "formats": ["TTF"],
                },
            )
        if "artifact" in request.url.path:
            uploaded_artifacts.append(request.headers.get("X-Artifact-SHA256"))
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "artifact_key": f"artifacts/ord_multi_dup/job_multi_dup/{request.headers.get('X-Artifact-SHA256')}.zip",
                    "size": 1234,
                },
            )
        if "complete" in request.url.path:
            job_completed_in_d1 = True
            completed_jobs.append("job_multi_dup")
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "status": "COMPLETED",
                    "queue_action": "ack",
                    "receipt": {"job_id": "job_multi_dup"},
                },
            )
        return httpx.Response(200, json={"success": True})

    def source_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=preview_bytes, headers={"Content-Type": "image/png"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(queue_handler_1)) as q_http_1, \
               httpx.AsyncClient(transport=httpx.MockTransport(queue_handler_2)) as q_http_2, \
               httpx.AsyncClient(transport=httpx.MockTransport(worker_handler)) as w_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(source_handler)) as s_http:

        # 1. Consumer 1 pulls and processes job
        q_client_1 = CloudflareQueueClient(test_settings, client=q_http_1)
        w_client_1 = WorkerJobClient(test_settings, client=w_http)
        s_acquirer_1 = FixtureSourceAcquirer(preview_bytes, client=s_http)
        runner_1 = A23Runner(
            test_settings,
            queue_client=q_client_1,
            worker_client=w_client_1,
            source_acquirer=s_acquirer_1,
        )

        res_1 = await runner_1.run_once()
        assert len(res_1) == 1
        assert res_1[0].action == RunnerAction.ACKED

        assert len(uploaded_artifacts) == 1
        assert len(completed_jobs) == 1
        assert "lease_q_worker_1" in acked_queue_leases

        # 2. Simulated Queue ACK loss & redelivery: Consumer 2 receives duplicate message
        q_client_2 = CloudflareQueueClient(test_settings, client=q_http_2)
        w_client_2 = WorkerJobClient(test_settings, client=w_http)
        s_acquirer_2 = FixtureSourceAcquirer(preview_bytes, client=s_http)
        runner_2 = A23Runner(
            test_settings,
            queue_client=q_client_2,
            worker_client=w_client_2,
            source_acquirer=s_acquirer_2,
        )

        res_2 = await runner_2.run_once()
        assert len(res_2) == 1
        assert res_2[0].action == RunnerAction.ACKED
        assert "lease_q_worker_2" in acked_queue_leases

        # 3. Verify singular canonical outcome: zero additional uploads, zero duplicate completions
        assert len(uploaded_artifacts) == 1
        assert len(completed_jobs) == 1

        await runner_1.close()
        await runner_2.close()


@pytest.mark.asyncio
async def test_runner_fails_pending_catalog_request_on_error(test_settings: Settings):
    failed_requests = []
    call_order = []

    def queue_handler(request: httpx.Request) -> httpx.Response:
        call_order.append("queue_pull")
        return httpx.Response(200, json={"result": {"messages": []}, "success": True})

    def worker_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "/internal/catalog-requests/pending" in request.url.path:
            call_order.append("catalog_pending")
            return httpx.Response(
                200,
                json={
                    "requests": [
                        {
                            "id": "req_invalid_1",
                            "user_id": "usr_99",
                            "canonical_key": "invalid_font",
                            "source_url": "https://www.myfonts.com/collections/invalid-font",
                            "status": "PENDING",
                            "created_at": 1700000000,
                        }
                    ]
                },
            )
        if request.method == "POST" and "/internal/catalog-requests/req_invalid_1/fail" in request.url.path:
            failed_requests.append("req_invalid_1")
            return httpx.Response(200, json={"success": True, "status": "FAILED"})
        return httpx.Response(404)

    def source_handler(request: httpx.Request) -> httpx.Response:
        # Return 404 for invalid font
        return httpx.Response(404, text="Not Found")

    async with httpx.AsyncClient(transport=httpx.MockTransport(queue_handler)) as q_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(worker_handler)) as w_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(source_handler)) as s_http:

        q_client = CloudflareQueueClient(test_settings, client=q_http)
        w_client = WorkerJobClient(test_settings, client=w_http)
        s_acquirer = SourceAcquirer(client=s_http)
        runner = A23Runner(
            test_settings,
            queue_client=q_client,
            worker_client=w_client,
            source_acquirer=s_acquirer,
        )

        results = await runner.run_once()
        assert results == []

        # Verify queue was polled before catalog processing
        assert call_order == ["queue_pull", "catalog_pending"]

        # Verify failed request was transitioned out of PENDING via fail endpoint
        assert failed_requests == ["req_invalid_1"]

        await runner.close()


@pytest.mark.asyncio
async def test_runner_leaves_catalog_retryable_on_transient_error(test_settings: Settings):
    failed_requests = []

    def queue_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": {"messages": []}, "success": True})

    def worker_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "/internal/catalog-requests/pending" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "requests": [
                        {
                            "id": "req_transient_1",
                            "user_id": "usr_transient",
                            "canonical_key": "transient_font",
                            "source_url": "https://www.myfonts.com/collections/transient-font",
                            "status": "PENDING",
                            "created_at": 1700000000,
                        }
                    ]
                },
            )
        if "/fail" in request.url.path:
            failed_requests.append(request.url.path)
            return httpx.Response(200, json={"success": True, "status": "FAILED"})
        return httpx.Response(404)

    def source_handler(request: httpx.Request) -> httpx.Response:
        # Transient 503 Service Unavailable error
        return httpx.Response(503, text="Service Unavailable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(queue_handler)) as q_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(worker_handler)) as w_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(source_handler)) as s_http:

        q_client = CloudflareQueueClient(test_settings, client=q_http)
        w_client = WorkerJobClient(test_settings, client=w_http)
        s_acquirer = SourceAcquirer(client=s_http)
        runner = A23Runner(
            test_settings,
            queue_client=q_client,
            worker_client=w_client,
            source_acquirer=s_acquirer,
        )

        results = await runner.run_once()
        assert results == []

        # Verify /fail was NOT called for transient error; request remains retryable in D1
        assert failed_requests == []

        await runner.close()


@pytest.mark.asyncio
async def test_runner_bounds_catalog_processing_to_one_per_loop(test_settings: Settings):
    completed_requests = []

    def queue_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": {"messages": []}, "success": True})

    def worker_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "/internal/catalog-requests/pending" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "requests": [
                        {
                            "id": f"req_batch_{i}",
                            "user_id": f"usr_{i}",
                            "canonical_key": f"key_{i}",
                            "source_url": f"https://www.myfonts.com/collections/font-{i}",
                            "status": "PENDING",
                            "created_at": 1700000000 + i,
                        }
                        for i in range(1, 4)
                    ]
                },
            )
        if "/complete" in request.url.path:
            req_id = request.url.path.split("/")[-2]
            completed_requests.append(req_id)
            return httpx.Response(200, json={"success": True, "catalog_id": f"cat_{req_id}"})
        return httpx.Response(404)

    def source_handler(request: httpx.Request) -> httpx.Response:
        html = """
        <html>
          <head>
            <meta property="og:title" content="Test Font - MyFonts">
          </head>
          <body>
            <div data-style-name="Regular"></div>
          </body>
        </html>
        """
        return httpx.Response(200, text=html)

    async with httpx.AsyncClient(transport=httpx.MockTransport(queue_handler)) as q_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(worker_handler)) as w_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(source_handler)) as s_http:

        q_client = CloudflareQueueClient(test_settings, client=q_http)
        w_client = WorkerJobClient(test_settings, client=w_http)
        s_acquirer = SourceAcquirer(client=s_http)
        runner = A23Runner(
            test_settings,
            queue_client=q_client,
            worker_client=w_client,
            source_acquirer=s_acquirer,
        )

        # Run one loop iteration
        await runner.run_once()

        # Bounded to exactly 1 catalog request in this run_once call
        assert len(completed_requests) == 1
        assert completed_requests[0] == "req_batch_1"

        await runner.close()



