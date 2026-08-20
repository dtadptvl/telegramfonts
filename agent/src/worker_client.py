"""Client for Cloudflare Worker internal job APIs."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
import httpx

from config import Settings

logger = logging.getLogger("telegramfonts.agent.worker")


@dataclass(frozen=True)
class ClaimStyle:
    id: str
    display_name: str


@dataclass(frozen=True)
class ClaimedJob:
    job_id: str
    order_id: str
    lease_token: str
    lease_expires_at: int
    source_url: str
    family_name: str | None
    foundry: str | None
    styles: list[ClaimStyle]
    formats: list[str]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClaimedJob:
        # 1. Scalar identifier validation
        job_id = str(data.get("job_id", "")).strip()
        if not job_id or not all(c.isalnum() or c in ("-", "_") for c in job_id) or len(job_id) > 64:
            raise ValueError("MALFORMED_JOB_ID")

        order_id = str(data.get("order_id", "")).strip()
        if not order_id or not all(c.isalnum() or c in ("-", "_") for c in order_id) or len(order_id) > 64:
            raise ValueError("MALFORMED_ORDER_ID")

        lease_token = str(data.get("lease_token", "")).strip()
        if not lease_token or len(lease_token) != 36:
            raise ValueError("MALFORMED_LEASE_TOKEN")

        try:
            lease_expires_at = int(data.get("lease_expires_at", 0))
            if lease_expires_at <= 0:
                raise ValueError("MALFORMED_LEASE_EXPIRY")
        except (ValueError, TypeError):
            raise ValueError("MALFORMED_LEASE_EXPIRY")

        source_url = str(data.get("source_url", "")).strip()
        if not source_url.startswith("https://") or ("myfonts.com" not in source_url):
            raise ValueError("MALFORMED_SOURCE_URL")

        # 2. Atomic styles validation (no silent dropping)
        raw_styles = data.get("styles")
        if not isinstance(raw_styles, list) or len(raw_styles) == 0:
            raise ValueError("MISSING_OR_EMPTY_STYLES")

        styles: list[ClaimStyle] = []
        for s in raw_styles:
            if not isinstance(s, dict):
                raise ValueError("INVALID_STYLE_ENTRY")
            s_id = str(s.get("id", "")).strip()
            s_name = str(s.get("display_name", s_id)).strip()
            if not s_id or not all(c.isalnum() or c in ("-", "_") for c in s_id) or len(s_id) > 64:
                raise ValueError(f"INVALID_STYLE_ID: {s_id}")
            if not s_name or len(s_name) > 128:
                raise ValueError(f"INVALID_STYLE_NAME: {s_name}")
            styles.append(ClaimStyle(id=s_id, display_name=s_name))

        # 3. Atomic formats validation (no silent dropping)
        raw_formats = data.get("formats")
        if not isinstance(raw_formats, list) or len(raw_formats) == 0:
            raise ValueError("MISSING_OR_EMPTY_FORMATS")

        allowed_formats = {"TTF", "OTF", "WOFF2"}
        formats: list[str] = []
        for f in raw_formats:
            if not isinstance(f, str):
                raise ValueError(f"NON_STRING_FORMAT: {f}")
            clean_f = f.strip().upper()
            if clean_f not in allowed_formats:
                raise ValueError(f"UNSUPPORTED_FORMAT: {clean_f}")
            if clean_f not in formats:
                formats.append(clean_f)

        return cls(
            job_id=job_id,
            order_id=order_id,
            lease_token=lease_token,
            lease_expires_at=lease_expires_at,
            source_url=source_url,
            family_name=str(data["family_name"]).strip() if data.get("family_name") else None,
            foundry=str(data["foundry"]).strip() if data.get("foundry") else None,
            styles=styles,
            formats=formats,
        )


@dataclass(frozen=True)
class ClaimResult:
    status: str
    queue_action: str  # "ack" | "retry" | "claimed"
    job: ClaimedJob | None = None
    reason: str | None = None


@dataclass(frozen=True)
class HeartbeatResult:
    success: bool
    fenced: bool
    lease_expires_at: int | None = None


@dataclass(frozen=True)
class FailResult:
    success: bool
    fenced: bool
    status: str
    queue_action: str  # "ack" | "retry"
    delay_seconds: int = 0
    reason: str | None = None


class WorkerJobClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.base_url = f"{settings.EDGE_BASE_URL}/internal/jobs"
        self.headers = {
            "Authorization": f"Bearer {settings.A23_NODE_SECRET.get_secret_value()}",
            "Content-Type": "application/json",
        }
        self._external_client = client is not None
        self._client = client or httpx.AsyncClient(timeout=settings.HTTP_TIMEOUT_SECONDS)

    async def close(self) -> None:
        if not self._external_client:
            await self._client.aclose()

    async def claim(self, job_id: str) -> ClaimResult:
        url = f"{self.base_url}/{job_id}/claim"
        payload = {
            "worker_id": self.settings.A23_WORKER_ID,
            "lease_seconds": self.settings.LEASE_DURATION_SECONDS,
        }

        try:
            resp = await self._client.post(url, headers=self.headers, json=payload)
            data = resp.json() if resp.content else {}

            if resp.status_code == 200:
                try:
                    job = ClaimedJob.from_dict(data)
                    return ClaimResult(status="CLAIMED", queue_action="claimed", job=job)
                except ValueError as val_err:
                    logger.warning(f"Malformed claim payload for job {job_id}: {val_err}")
                    return ClaimResult(
                        status="MALFORMED_PAYLOAD",
                        queue_action="retry",
                        reason=f"malformed_claim_payload_{val_err}",
                    )

            if resp.status_code == 404:
                return ClaimResult(
                    status="NOT_FOUND",
                    queue_action="ack",
                    reason="job_not_found",
                )

            # 409 or other controlled conflict
            queue_action = data.get("queue_action", "retry")
            return ClaimResult(
                status=data.get("status", "CONFLICT"),
                queue_action=queue_action,
                reason=data.get("reason"),
            )
        except httpx.RequestError as exc:
            logger.warning(f"Network error on job claim ({job_id}): {type(exc).__name__}")
            # Transient error: do not drop, retry later
            return ClaimResult(status="NETWORK_ERROR", queue_action="retry", reason="network_error")

    async def heartbeat(self, job_id: str, lease_token: str) -> HeartbeatResult:
        url = f"{self.base_url}/{job_id}/heartbeat"
        payload = {
            "worker_id": self.settings.A23_WORKER_ID,
            "lease_token": lease_token,
            "extend_seconds": self.settings.LEASE_DURATION_SECONDS,
        }

        try:
            resp = await self._client.post(url, headers=self.headers, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                return HeartbeatResult(
                    success=True,
                    fenced=False,
                    lease_expires_at=data.get("lease_expires_at"),
                )

            if resp.status_code == 409:
                return HeartbeatResult(success=False, fenced=True)

            return HeartbeatResult(success=False, fenced=False)
        except httpx.RequestError as exc:
            logger.warning(f"Network error on heartbeat ({job_id}): {type(exc).__name__}")
            return HeartbeatResult(success=False, fenced=False)

    async def fail(
        self,
        job_id: str,
        lease_token: str,
        retryable: bool,
        reason_code: str = "UNSPECIFIED_FAILURE",
    ) -> FailResult:
        url = f"{self.base_url}/{job_id}/fail"
        payload = {
            "worker_id": self.settings.A23_WORKER_ID,
            "lease_token": lease_token,
            "retryable": retryable,
            "reason_code": reason_code[:64],
        }

        try:
            resp = await self._client.post(url, headers=self.headers, json=payload)
            data = resp.json() if resp.content else {}

            if resp.status_code == 200:
                return FailResult(
                    success=True,
                    fenced=False,
                    status=data.get("status", "FAILED"),
                    queue_action=data.get("queue_action", "ack"),
                    delay_seconds=int(data.get("delay_seconds", 0)),
                    reason=data.get("reason"),
                )

            if resp.status_code == 409:
                return FailResult(
                    success=False,
                    fenced=True,
                    status="EXPIRED_OR_FENCED",
                    queue_action="ack",
                )

            return FailResult(
                success=False,
                fenced=False,
                status="ERROR",
                queue_action="retry",
            )
        except httpx.RequestError as exc:
            logger.warning(f"Network error on job fail ({job_id}): {type(exc).__name__}")
            return FailResult(
                success=False,
                fenced=False,
                status="NETWORK_ERROR",
                queue_action="retry",
            )
