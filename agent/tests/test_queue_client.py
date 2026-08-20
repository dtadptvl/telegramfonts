"""Tests for Cloudflare Queues HTTP pull client."""
import json
import httpx
import pytest

from config import Settings
from queue_client import CloudflareQueueClient, QueueMessage


@pytest.mark.asyncio
async def test_queue_pull_messages_success(test_settings: Settings):
    mock_payload = {
        "success": True,
        "result": {
            "messages": [
                {
                    "id": "msg_1",
                    "lease_id": "lease_1",
                    "body": json.dumps({"job_id": "job_100"}),
                    "attempts": 1,
                },
                {
                    "id": "msg_2",
                    "lease_id": "lease_2",
                    "body": "invalid-json",
                    "attempts": 2,
                },
            ]
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {test_settings.CF_QUEUES_TOKEN.get_secret_value()}"
        data = json.loads(request.content)
        assert data["batch_size"] == 2
        assert data["visibility_timeout_ms"] == 300000
        return httpx.Response(200, json=mock_payload)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = CloudflareQueueClient(test_settings, client=http_client)
        messages = await client.pull_messages(batch_size=2, visibility_timeout_ms=300000)

        assert len(messages) == 2
        assert messages[0].id == "msg_1"
        assert messages[0].job_id == "job_100"
        assert messages[1].id == "msg_2"
        assert messages[1].job_id is None


@pytest.mark.asyncio
async def test_queue_ack_and_retry(test_settings: Settings):
    sent_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_requests.append(json.loads(request.content))
        return httpx.Response(200, json={"success": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = CloudflareQueueClient(test_settings, client=http_client)

        # ACK
        ack_res = await client.acknowledge_messages(["lease_1", "lease_2"])
        assert ack_res is True
        assert sent_requests[0] == {"acks": [{"lease_id": "lease_1"}, {"lease_id": "lease_2"}]}

        # RETRY
        retry_res = await client.retry_messages([("lease_3", 60)])
        assert retry_res is True
        assert sent_requests[1] == {"retries": [{"lease_id": "lease_3", "delay_seconds": 60}]}


@pytest.mark.asyncio
async def test_queue_network_error_graceful_handling(test_settings: Settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = CloudflareQueueClient(test_settings, client=http_client)
        messages = await client.pull_messages()
        assert messages == []

        ack_res = await client.acknowledge_messages(["l1"])
        assert ack_res is False
