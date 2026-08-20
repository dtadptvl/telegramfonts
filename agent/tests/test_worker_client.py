"""Tests for Worker Job API client and fail-closed claim validation."""
import json
import httpx
import pytest

from config import Settings
from worker_client import ClaimedJob, WorkerJobClient


@pytest.mark.asyncio
async def test_worker_claim_success(test_settings: Settings):
    mock_claim_payload = {
        "job_id": "job_100",
        "order_id": "ord_100",
        "lease_token": "12345678-1234-1234-1234-123456789abc",
        "lease_expires_at": 1800000000000,
        "source_url": "https://www.myfonts.com/collections/roboto-flex",
        "family_name": "Roboto Flex",
        "foundry": "Google Fonts",
        "styles": [{"id": "regular", "display_name": "Regular"}],
        "formats": ["TTF", "WOFF2"],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {test_settings.A23_NODE_SECRET.get_secret_value()}"
        assert request.url.path == "/internal/jobs/job_100/claim"
        return httpx.Response(200, json=mock_claim_payload)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = WorkerJobClient(test_settings, client=http_client)
        res = await client.claim("job_100")

        assert res.status == "CLAIMED"
        assert res.queue_action == "claimed"
        assert res.job is not None
        assert res.job.job_id == "job_100"
        assert res.job.formats == ["TTF", "WOFF2"]


def test_claimed_job_fail_closed_validation():
    base_valid = {
        "job_id": "job_100",
        "order_id": "ord_100",
        "lease_token": "12345678-1234-1234-1234-123456789abc",
        "lease_expires_at": 1800000000000,
        "source_url": "https://www.myfonts.com/collections/roboto-flex",
        "styles": [{"id": "s1", "display_name": "Regular"}],
        "formats": ["TTF"],
    }

    # 1. Mixed valid + invalid styles fails closed (BLOCK C)
    with pytest.raises(ValueError, match="INVALID_STYLE_ID"):
        ClaimedJob.from_dict({
            **base_valid,
            "styles": [{"id": "s1", "display_name": "Regular"}, {"id": "bad style with spaces!", "display_name": "Bad"}],
        })

    # 2. Mixed valid + invalid formats fails closed (BLOCK C)
    with pytest.raises(ValueError, match="UNSUPPORTED_FORMAT"):
        ClaimedJob.from_dict({
            **base_valid,
            "formats": ["TTF", "EXE"],
        })

    # 3. Empty styles or formats fails closed
    with pytest.raises(ValueError, match="MISSING_OR_EMPTY_STYLES"):
        ClaimedJob.from_dict({**base_valid, "styles": []})

    with pytest.raises(ValueError, match="MISSING_OR_EMPTY_FORMATS"):
        ClaimedJob.from_dict({**base_valid, "formats": []})

    # 4. Off-domain source url fails closed
    with pytest.raises(ValueError, match="MALFORMED_SOURCE_URL"):
        ClaimedJob.from_dict({**base_valid, "source_url": "https://evil.com/font"})


@pytest.mark.asyncio
async def test_worker_claim_terminal_and_conflict(test_settings: Settings):
    # Terminal case (409 with queue_action=ack)
    def handler_terminal(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"status": "TERMINAL", "queue_action": "ack", "reason": "job_completed"})

    transport1 = httpx.MockTransport(handler_terminal)
    async with httpx.AsyncClient(transport=transport1) as http_client:
        client = WorkerJobClient(test_settings, client=http_client)
        res = await client.claim("job_term")
        assert res.queue_action == "ack"
        assert res.status == "TERMINAL"

    # Not found case (404)
    def handler_404(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "Not Found", "queue_action": "ack"})

    transport2 = httpx.MockTransport(handler_404)
    async with httpx.AsyncClient(transport=transport2) as http_client:
        client = WorkerJobClient(test_settings, client=http_client)
        res = await client.claim("job_missing")
        assert res.queue_action == "ack"
        assert res.status == "NOT_FOUND"


@pytest.mark.asyncio
async def test_worker_heartbeat_and_fail(test_settings: Settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if "heartbeat" in request.url.path:
            return httpx.Response(200, json={"success": True, "lease_expires_at": 12345})
        if "fail" in request.url.path:
            return httpx.Response(200, json={"success": True, "status": "RETRY", "queue_action": "retry", "delay_seconds": 20})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = WorkerJobClient(test_settings, client=http_client)

        hb_res = await client.heartbeat("job_1", "tok_1")
        assert hb_res.success is True
        assert hb_res.fenced is False
        assert hb_res.lease_expires_at == 12345

        fail_res = await client.fail("job_1", "tok_1", retryable=True, reason_code="ERR")
        assert fail_res.success is True
        assert fail_res.status == "RETRY"
        assert fail_res.queue_action == "retry"
        assert fail_res.delay_seconds == 20
