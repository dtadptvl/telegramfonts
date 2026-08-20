"""Cloudflare Queue HTTP-Pull REST Client."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
import httpx

from config import Settings

logger = logging.getLogger("telegramfonts.agent.queue")


@dataclass(frozen=True)
class QueueMessage:
    id: str
    lease_id: str
    body_raw: str
    attempts: int
    job_id: str | None = None

    @classmethod
    def from_api_dict(cls, data: dict[str, Any]) -> QueueMessage:
        msg_id = str(data.get("id", ""))
        lease_id = str(data.get("lease_id", ""))
        raw_body = data.get("body", "")
        if isinstance(raw_body, dict):
            raw_body_str = json.dumps(raw_body)
            body_dict = raw_body
        elif isinstance(raw_body, str):
            raw_body_str = raw_body
            try:
                body_dict = json.loads(raw_body)
            except Exception:
                body_dict = None
        else:
            raw_body_str = str(raw_body)
            body_dict = None

        attempts = int(data.get("attempts", 1))

        # Extract and validate job_id
        job_id = None
        if isinstance(body_dict, dict):
            candidate = body_dict.get("job_id")
            if isinstance(candidate, str) and candidate.strip():
                clean_id = candidate.strip()
                if len(clean_id) <= 64 and all(c.isalnum() or c in ("-", "_") for c in clean_id):
                    job_id = clean_id

        return cls(
            id=msg_id,
            lease_id=lease_id,
            body_raw=raw_body_str,
            attempts=attempts,
            job_id=job_id,
        )


class CloudflareQueueClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.base_url = (
            f"https://api.cloudflare.com/client/v4/accounts/"
            f"{settings.CF_ACCOUNT_ID}/queues/{settings.CF_QUEUE_ID}/messages"
        )
        self.headers = {
            "Authorization": f"Bearer {settings.CF_QUEUES_TOKEN.get_secret_value()}",
            "Content-Type": "application/json",
        }
        self._external_client = client is not None
        self._client = client or httpx.AsyncClient(timeout=settings.HTTP_TIMEOUT_SECONDS)

    async def close(self) -> None:
        if not self._external_client:
            await self._client.aclose()

    async def pull_messages(
        self,
        batch_size: int | None = None,
        visibility_timeout_ms: int | None = None,
    ) -> list[QueueMessage]:
        url = f"{self.base_url}/pull"
        payload = {
            "batch_size": batch_size or self.settings.PULL_BATCH_SIZE,
            "visibility_timeout_ms": visibility_timeout_ms or self.settings.VISIBILITY_TIMEOUT_MS,
        }

        try:
            resp = await self._client.post(url, headers=self.headers, json=payload)
            if resp.status_code != 200:
                logger.error(f"Failed to pull messages: HTTP {resp.status_code}")
                return []

            data = resp.json()
            raw_messages = (
                data.get("result", {}).get("messages", [])
                if "result" in data
                else data.get("messages", [])
            )

            messages = [QueueMessage.from_api_dict(m) for m in raw_messages if isinstance(m, dict)]
            logger.info(f"Pulled {len(messages)} messages from queue")
            return messages
        except httpx.RequestError as exc:
            logger.warning(f"Network error pulling messages: {type(exc).__name__}")
            return []

    async def acknowledge_messages(self, lease_ids: list[str]) -> bool:
        if not lease_ids:
            return True
        url = f"{self.base_url}/ack"
        payload = {"acks": [{"lease_id": lid} for lid in lease_ids]}

        try:
            resp = await self._client.post(url, headers=self.headers, json=payload)
            return resp.status_code == 200
        except httpx.RequestError as exc:
            logger.warning(f"Network error acknowledging messages: {type(exc).__name__}")
            return False

    async def retry_messages(self, retries: list[tuple[str, int]]) -> bool:
        """Send retry acknowledgements with optional delay_seconds: list of (lease_id, delay_seconds)."""
        if not retries:
            return True
        url = f"{self.base_url}/ack"
        payload = {
            "retries": [
                {"lease_id": lid, "delay_seconds": max(0, min(delay, 900))}
                for lid, delay in retries
            ]
        }

        try:
            resp = await self._client.post(url, headers=self.headers, json=payload)
            return resp.status_code == 200
        except httpx.RequestError as exc:
            logger.warning(f"Network error requesting message retries: {type(exc).__name__}")
            return False
