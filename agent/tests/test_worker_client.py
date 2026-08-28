"""Tests for Worker Job API client and strict fail-closed claim validation."""
import json
import httpx
import pytest

pytestmark = pytest.mark.integration

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
        "formats": ["TTF", "OTF"],
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
        assert res.job.formats == ["TTF", "OTF"]


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

    # 1. Sibling/substring host fails closed (BLOCK C)
    with pytest.raises(ValueError, match="MALFORMED_SOURCE_URL"):
        ClaimedJob.from_dict({**base_valid, "source_url": "https://evilmyfonts.com/font"})

    with pytest.raises(ValueError, match="MALFORMED_SOURCE_URL"):
        ClaimedJob.from_dict({**base_valid, "source_url": "https://myfonts.com.evil.com/font"})

    # 2. Non-string display_name fails whole payload (BLOCK C)
    with pytest.raises(ValueError, match="INVALID_STYLE_DISPLAY_NAME"):
        ClaimedJob.from_dict({
            **base_valid,
            "styles": [{"id": "s1", "display_name": 12345}],
        })

    with pytest.raises(ValueError, match="INVALID_STYLE_DISPLAY_NAME"):
        ClaimedJob.from_dict({
            **base_valid,
            "styles": [{"id": "s1", "display_name": True}],
        })

    # 3. Mixed valid + invalid styles fails closed (BLOCK C)
    with pytest.raises(ValueError, match="INVALID_STYLE_ID"):
        ClaimedJob.from_dict({
            **base_valid,
            "styles": [{"id": "s1", "display_name": "Regular"}, {"id": "bad style with spaces!", "display_name": "Bad"}],
        })

    # 4. Removed WOFF2 format fails closed.
    with pytest.raises(ValueError, match="UNSUPPORTED_FORMAT"):
        ClaimedJob.from_dict({
            **base_valid,
            "formats": ["TTF", "WOFF2"],
        })

    # 5. Empty styles or formats fails closed
    with pytest.raises(ValueError, match="MISSING_OR_EMPTY_STYLES"):
        ClaimedJob.from_dict({**base_valid, "styles": []})

    with pytest.raises(ValueError, match="MISSING_OR_EMPTY_FORMATS"):
        ClaimedJob.from_dict({**base_valid, "formats": []})


@pytest.mark.asyncio
async def test_worker_claim_terminal_and_conflict(test_settings: Settings):
    def handler_terminal(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"status": "TERMINAL", "queue_action": "ack", "reason": "job_completed"})

    transport1 = httpx.MockTransport(handler_terminal)
    async with httpx.AsyncClient(transport=transport1) as http_client:
        client = WorkerJobClient(test_settings, client=http_client)
        res = await client.claim("job_term")
        assert res.queue_action == "ack"
        assert res.status == "TERMINAL"

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


@pytest.mark.asyncio
async def test_worker_upload_artifact_and_complete(tmp_path, test_settings: Settings):
    dummy_zip = tmp_path / "test.zip"
    dummy_zip.write_bytes(b"PK\x03\x04dummy_zip_content")
    sha256_hex = "a" * 64

    def handler(request: httpx.Request) -> httpx.Response:
        if "artifact" in request.url.path:
            assert request.method == "PUT"
            assert request.headers["Content-Type"] == "application/zip"
            assert request.headers["X-Worker-Id"] == test_settings.A23_WORKER_ID
            assert request.headers["X-Lease-Token"] == "tok_123"
            assert request.headers["X-Artifact-SHA256"] == sha256_hex
            return httpx.Response(200, json={
                "success": True,
                "artifact_key": f"artifacts/ord_1/job_1/{sha256_hex}.zip",
                "sha256": sha256_hex,
                "size": len(b"PK\x03\x04dummy_zip_content"),
            })

        if "complete" in request.url.path:
            assert request.method == "POST"
            body = json.loads(request.content)
            assert body["worker_id"] == test_settings.A23_WORKER_ID
            assert body["lease_token"] == "tok_123"
            assert body["sha256"] == sha256_hex
            return httpx.Response(200, json={
                "success": True,
                "status": "COMPLETED",
                "queue_action": "ack",
                "completed_at": 1700000000000,
                "artifact_key": f"artifacts/ord_1/job_1/{sha256_hex}.zip",
            })

        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = WorkerJobClient(test_settings, client=http_client)

        upload_res = await client.upload_artifact("job_1", "tok_123", dummy_zip, sha256_hex)
        assert upload_res.success is True
        assert upload_res.fenced is False
        assert upload_res.artifact_key == f"artifacts/ord_1/job_1/{sha256_hex}.zip"

        complete_res = await client.complete(
            "job_1",
            "tok_123",
            upload_res.artifact_key,
            sha256_hex,
            upload_res.size,
        )
        assert complete_res.success is True
        assert complete_res.status == "COMPLETED"
        assert complete_res.queue_action == "ack"


@pytest.mark.asyncio
async def test_worker_upload_artifact_streams_without_read_bytes(tmp_path, test_settings: Settings, monkeypatch):
    dummy_zip = tmp_path / "streamed_test.zip"
    payload_content = b"PK\x03\x04" + b"X" * 150000  # > 64KB to ensure multiple chunks
    dummy_zip.write_bytes(payload_content)
    sha256_hex = "b" * 64

    # Disallow calling read_bytes on Path
    def fail_read_bytes(self, *args, **kwargs):
        raise AssertionError("Path.read_bytes() must not be called during streamed upload (BLOCK 4)")

    monkeypatch.setattr("pathlib.Path.read_bytes", fail_read_bytes)

    received_chunks = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Content-Type"] == "application/zip"
        assert request.headers["Content-Length"] == str(len(payload_content))
        received_chunks.append(request.content)
        return httpx.Response(200, json={
            "success": True,
            "artifact_key": f"artifacts/ord_stream/job_stream/{sha256_hex}.zip",
            "sha256": sha256_hex,
            "size": len(payload_content),
        })

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = WorkerJobClient(test_settings, client=http_client)
        upload_res = await client.upload_artifact("job_stream", "tok_stream", dummy_zip, sha256_hex)

        assert upload_res.success is True
        assert upload_res.size == len(payload_content)
        assert len(received_chunks) == 1
        assert received_chunks[0] == payload_content


@pytest.mark.asyncio
async def test_worker_complete_409_preserves_status_and_queue_action(test_settings: Settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={
            "status": "EXPIRED_OR_FENCED",
            "queue_action": "retry",
            "reason": "lease_superseded_or_expired",
        })

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = WorkerJobClient(test_settings, client=http_client)
        res = await client.complete("job_1", "tok_1", "artifacts/k.zip", "a"*64, 100)
        assert res.success is False
        assert res.status == "EXPIRED_OR_FENCED"
        assert res.queue_action == "retry"
        assert res.reason == "lease_superseded_or_expired"


@pytest.mark.asyncio
async def test_worker_get_and_complete_catalog_requests(test_settings: Settings):
    pending_list = [
        {
            "id": "req_101",
            "user_id": "usr_202",
            "canonical_key": "helvetica_now",
            "source_url": "https://www.myfonts.com/collections/helvetica-now-font",
            "status": "PENDING",
            "created_at": 1700000000,
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {test_settings.A23_NODE_SECRET.get_secret_value()}"
        if request.method == "GET" and request.url.path == "/internal/catalog-requests/pending":
            return httpx.Response(200, json={"requests": pending_list})
        if request.method == "POST" and request.url.path == "/internal/catalog-requests/req_101/complete":
            return httpx.Response(200, json={"success": True, "catalog_id": "cat_101"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = WorkerJobClient(test_settings, client=http_client)

        reqs = await client.get_pending_catalog_requests()
        assert len(reqs) == 1
        assert reqs[0].id == "req_101"
        assert reqs[0].canonical_key == "helvetica_now"

        payload = {
            "canonical_key": "helvetica_now",
            "source_url": "https://www.myfonts.com/collections/helvetica-now-font",
            "family_name": "Helvetica Now",
            "foundry": "Monotype",
            "styles": [{"id": "regular", "display_name": "Regular", "price": 5000}],
        }
        success = await client.complete_catalog_request("req_101", payload)
        assert success is True


@pytest.mark.asyncio
async def test_worker_fail_catalog_request(test_settings: Settings):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {test_settings.A23_NODE_SECRET.get_secret_value()}"
        if request.method == "POST" and request.url.path == "/internal/catalog-requests/req_fail_1/fail":
            return httpx.Response(200, json={"success": True, "status": "FAILED"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = WorkerJobClient(test_settings, client=http_client)
        res = await client.fail_catalog_request("req_fail_1", "NO_CATALOG_STYLES_FOUND")
        assert res is True



