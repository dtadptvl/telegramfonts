"""Tests for A23 Runner lifecycle and state machine."""
import asyncio
import json
import httpx
import pytest
from pathlib import Path

from config import Settings
from queue_client import CloudflareQueueClient, QueueMessage
from runner import A23Runner, RunnerAction
from worker_client import WorkerJobClient


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
async def test_runner_successful_compute_hold_for_completion(test_settings: Settings):
    acked_leases = []
    failed_jobs = []

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
                    "job_id": "job_success_1",
                    "order_id": "ord_success_1",
                    "lease_token": "token_123",
                    "lease_expires_at": 1800000000000,
                    "source_url": "https://www.myfonts.com/collections/roboto-flex",
                    "family_name": "Roboto Flex",
                    "foundry": "Google",
                    "styles": [{"id": "rf_reg", "display_name": "Regular"}],
                    "formats": ["TTF", "WOFF2"],
                },
            )
        if "heartbeat" in request.url.path:
            return httpx.Response(200, json={"success": True, "lease_expires_at": 1800000000000})
        if "fail" in request.url.path:
            failed_jobs.append(request.url.path)
            return httpx.Response(200, json={"success": True})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(queue_handler)) as q_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(worker_handler)) as w_http:
        q_client = CloudflareQueueClient(test_settings, client=q_http)
        w_client = WorkerJobClient(test_settings, client=w_http)
        runner = A23Runner(test_settings, q_client, w_client)

        msg = QueueMessage(id="m1", lease_id="l_succ", body_raw='{"job_id":"job_success_1"}', attempts=1, job_id="job_success_1")
        res = await runner.process_message(msg)

        # Assert successful compute stops at HOLD_FOR_COMPLETION
        assert res.action == RunnerAction.HOLD_FOR_COMPLETION
        assert res.manifest is not None
        assert res.manifest.zip_file_path.exists()
        assert res.manifest.zip_size_bytes > 0
        assert len(res.manifest.files) == 2

        # Assert no Queue ACK and no Worker fail called
        assert "l_succ" not in acked_leases
        assert len(failed_jobs) == 0


@pytest.mark.asyncio
async def test_runner_terminal_compute_error_acks_queue(test_settings: Settings):
    acked_leases = []
    worker_calls = []

    def queue_handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        if "acks" in data:
            acked_leases.extend([a["lease_id"] for a in data["acks"]])
        return httpx.Response(200, json={"success": True})

    def worker_handler(request: httpx.Request) -> httpx.Response:
        worker_calls.append(request.url.path)
        if "claim" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "job_id": "job_bad_fmt",
                    "order_id": "ord_bad_fmt",
                    "lease_token": "token_123",
                    "lease_expires_at": 1800000000000,
                    "source_url": "https://www.myfonts.com/collections/roboto-flex",
                    "family_name": "Roboto Flex",
                    "styles": [{"id": "rf_reg", "display_name": "Regular"}],
                    "formats": ["INVALID_FMT"],  # Non-retryable format error
                },
            )
        if "fail" in request.url.path:
            return httpx.Response(200, json={"success": True, "status": "FAILED", "queue_action": "ack"})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(queue_handler)) as q_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(worker_handler)) as w_http:
        q_client = CloudflareQueueClient(test_settings, client=q_http)
        w_client = WorkerJobClient(test_settings, client=w_http)
        runner = A23Runner(test_settings, q_client, w_client)

        msg = QueueMessage(id="m1", lease_id="l_bad", body_raw='{"job_id":"job_bad_fmt"}', attempts=1, job_id="job_bad_fmt")
        res = await runner.process_message(msg)

        assert res.action == RunnerAction.ACKED
        assert "l_bad" in acked_leases
        assert any("fail" in call for call in worker_calls)


@pytest.mark.asyncio
async def test_runner_retryable_compute_error_retries_queue(test_settings: Settings):
    retried_leases = []

    def queue_handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        if "retries" in data:
            retried_leases.extend([r["lease_id"] for r in data["retries"]])
        return httpx.Response(200, json={"success": True})

    def worker_handler(request: httpx.Request) -> httpx.Response:
        if "claim" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "job_id": "job_retryable_1",
                    "order_id": "ord_retryable_1",
                    "lease_token": "token_retry",
                    "lease_expires_at": 1800000000000,
                    "source_url": "https://www.myfonts.com/collections/roboto-flex",
                    "family_name": "Roboto Flex",
                    "styles": [{"id": "reg", "display_name": "Regular"}],
                    "formats": ["TTF"],
                },
            )
        if "fail" in request.url.path:
            return httpx.Response(200, json={"success": True, "status": "RETRY", "queue_action": "retry", "delay_seconds": 45})
        return httpx.Response(404)

    class FailingSourceAcquirer:
        async def acquire_source(self, url: str):
            raise ConnectionResetError("Network connection reset by peer")

    async with httpx.AsyncClient(transport=httpx.MockTransport(queue_handler)) as q_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(worker_handler)) as w_http:
        q_client = CloudflareQueueClient(test_settings, client=q_http)
        w_client = WorkerJobClient(test_settings, client=w_http)
        runner = A23Runner(test_settings, q_client, w_client, source_acquirer=FailingSourceAcquirer())

        msg = QueueMessage(id="m1", lease_id="l_ret", body_raw='{"job_id":"job_retryable_1"}', attempts=1, job_id="job_retryable_1")
        res = await runner.process_message(msg)

        assert res.action == RunnerAction.RETRIED
        assert "l_ret" in retried_leases


@pytest.mark.asyncio
async def test_runner_heartbeat_fencing_aborts_compute(test_settings: Settings):
    def queue_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True})

    def worker_handler(request: httpx.Request) -> httpx.Response:
        if "claim" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "job_id": "job_fenced_1",
                    "order_id": "ord_fenced_1",
                    "lease_token": "token_fenced",
                    "lease_expires_at": 1800000000000,
                    "source_url": "https://www.myfonts.com/collections/roboto-flex",
                    "family_name": "Roboto Flex",
                    "styles": [{"id": "reg", "display_name": "Regular"}],
                    "formats": ["TTF"],
                },
            )
        if "heartbeat" in request.url.path:
            # Return 409 fenced
            return httpx.Response(409, json={"status": "EXPIRED_OR_FENCED", "queue_action": "ack"})
        return httpx.Response(404)

    class SlowBuilder:
        def build_font(self, *args, **kwargs):
            # Wait for heartbeat to run and fence
            import time
            time.sleep(1.2)
            return None

    # Configure heartbeat interval to 1s
    custom_settings = Settings(
        CF_ACCOUNT_ID=test_settings.CF_ACCOUNT_ID,
        CF_QUEUE_ID=test_settings.CF_QUEUE_ID,
        CF_QUEUES_TOKEN=test_settings.CF_QUEUES_TOKEN,
        EDGE_BASE_URL=test_settings.EDGE_BASE_URL,
        A23_NODE_SECRET=test_settings.A23_NODE_SECRET,
        A23_WORKER_ID="worker-1",
        SCRATCH_DIR=test_settings.SCRATCH_DIR,
        HEARTBEAT_INTERVAL_SECONDS=1,
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(queue_handler)) as q_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(worker_handler)) as w_http:
        q_client = CloudflareQueueClient(custom_settings, client=q_http)
        w_client = WorkerJobClient(custom_settings, client=w_http)
        runner = A23Runner(custom_settings, q_client, w_client)

        # Trigger heartbeat failure
        fenced_ev = asyncio.Event()
        stop_ev = asyncio.Event()
        hb_task = asyncio.create_task(runner._heartbeat_loop("job_fenced_1", "tok", fenced_ev, stop_ev))
        await asyncio.sleep(1.2)
        assert fenced_ev.is_set()
        stop_ev.set()
        await hb_task
