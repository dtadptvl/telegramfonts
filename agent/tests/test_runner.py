"""Tests for A23 Runner lifecycle, state machine, consumer loop, and lease deadlines."""
import asyncio
import json
import time
import httpx
import pytest
from pathlib import Path

from compute.packager import PackagerService
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
async def test_runner_successful_compute_hold_for_completion_and_no_recompute(test_settings: Settings):
    acked_leases = []
    claim_count = 0

    def queue_handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        if "acks" in data:
            acked_leases.extend([a["lease_id"] for a in data["acks"]])
        return httpx.Response(200, json={"success": True})

    def worker_handler(request: httpx.Request) -> httpx.Response:
        nonlocal claim_count
        if "claim" in request.url.path:
            claim_count += 1
            return httpx.Response(
                200,
                json={
                    "job_id": "job_success_1",
                    "order_id": "ord_success_1",
                    "lease_token": "12345678-1234-1234-1234-123456789abc",
                    "lease_expires_at": int(time.time() * 1000) + 300000,
                    "source_url": "https://www.myfonts.com/collections/roboto-flex",
                    "family_name": "Roboto Flex",
                    "foundry": "Google",
                    "styles": [{"id": "rf_reg", "display_name": "Regular"}],
                    "formats": ["TTF", "WOFF2"],
                },
            )
        if "heartbeat" in request.url.path:
            return httpx.Response(200, json={"success": True, "lease_expires_at": int(time.time() * 1000) + 300000})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(queue_handler)) as q_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(worker_handler)) as w_http:
        q_client = CloudflareQueueClient(test_settings, client=q_http)
        w_client = WorkerJobClient(test_settings, client=w_http)
        runner = A23Runner(test_settings, q_client, w_client)

        msg = QueueMessage(id="m1", lease_id="l_succ", body_raw='{"job_id":"job_success_1"}', attempts=1, job_id="job_success_1")
        res1 = await runner.process_message(msg)

        # Assert successful compute stops at HOLD_FOR_COMPLETION
        assert res1.action == RunnerAction.HOLD_FOR_COMPLETION
        assert res1.manifest is not None
        assert res1.manifest.zip_file_path.exists()
        assert len(res1.manifest.files) == 2
        assert "l_succ" not in acked_leases

        # Subsequent processing of same message does not recompute (BLOCK A)
        res2 = await runner.process_message(msg)
        assert res2.action == RunnerAction.HOLD_FOR_COMPLETION
        assert res2.reason == "already_held"
        assert claim_count == 1


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
async def test_runner_slow_packaging_crossing_expiry_aborts_without_hold(test_settings: Settings):
    def queue_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True})

    def worker_handler(request: httpx.Request) -> httpx.Response:
        if "claim" in request.url.path:
            # 15.5s left on lease at start (< 15s safety margin after 1s delay)
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

    class SlowPackager(PackagerService):
        def package_job_output(self, *args, **kwargs):
            res = super().package_job_output(*args, **kwargs)
            # Simulate packaging delay that crosses the 15s safety margin deadline
            import time
            time.sleep(1.0)
            # Advance time by manipulating system clock or sleeping
            return res

    async with httpx.AsyncClient(transport=httpx.MockTransport(queue_handler)) as q_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(worker_handler)) as w_http:
        q_client = CloudflareQueueClient(test_settings, client=q_http)
        w_client = WorkerJobClient(test_settings, client=w_http)
        runner = A23Runner(test_settings, q_client, w_client, packager=SlowPackager())

        msg = QueueMessage(id="m1", lease_id="l_slow", body_raw='{"job_id":"job_slow_pkg"}', attempts=1, job_id="job_slow_pkg")
        res = await runner.process_message(msg)

        # Lease deadline crossing during slow packaging results in abort, not HOLD (BLOCK D)
        assert res.action != RunnerAction.HOLD_FOR_COMPLETION
