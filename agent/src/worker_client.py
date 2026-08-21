"""Client for Cloudflare Worker internal job APIs."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import httpx

from compute.models import ClaimStyle
from compute.source import validate_myfonts_url
from config import Settings

logger = logging.getLogger("telegramfonts.agent.worker")


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
        if not isinstance(data, dict):
            raise ValueError("MALFORMED_CLAIM_PAYLOAD_NOT_DICT")

        # 1. Scalar identifier validation (BLOCK C: no loose type coercion)
        raw_job_id = data.get("job_id")
        if not isinstance(raw_job_id, str) or not raw_job_id.strip() or not all(c.isalnum() or c in ("-", "_") for c in raw_job_id.strip()) or len(raw_job_id.strip()) > 64:
            raise ValueError("MALFORMED_JOB_ID")
        job_id = raw_job_id.strip()

        raw_order_id = data.get("order_id")
        if not isinstance(raw_order_id, str) or not raw_order_id.strip() or not all(c.isalnum() or c in ("-", "_") for c in raw_order_id.strip()) or len(raw_order_id.strip()) > 64:
            raise ValueError("MALFORMED_ORDER_ID")
        order_id = raw_order_id.strip()

        raw_lease_token = data.get("lease_token")
        if not isinstance(raw_lease_token, str) or len(raw_lease_token.strip()) != 36:
            raise ValueError("MALFORMED_LEASE_TOKEN")
        lease_token = raw_lease_token.strip()

        raw_lease_expires = data.get("lease_expires_at")
        if not isinstance(raw_lease_expires, int) or raw_lease_expires <= 0:
            raise ValueError("MALFORMED_LEASE_EXPIRY")
        lease_expires_at = raw_lease_expires

        raw_source_url = data.get("source_url")
        if not isinstance(raw_source_url, str) or not validate_myfonts_url(raw_source_url):
            raise ValueError("MALFORMED_SOURCE_URL")
        source_url = raw_source_url.strip()

        # 2. Atomic styles validation (BLOCK C: non-string or malformed style fails whole payload)
        raw_styles = data.get("styles")
        if not isinstance(raw_styles, list) or len(raw_styles) == 0:
            raise ValueError("MISSING_OR_EMPTY_STYLES")

        styles: list[ClaimStyle] = []
        for s in raw_styles:
            if not isinstance(s, dict):
                raise ValueError("INVALID_STYLE_ENTRY")

            s_id = s.get("id")
            if not isinstance(s_id, str) or not s_id.strip() or not all(c.isalnum() or c in ("-", "_") for c in s_id.strip()) or len(s_id.strip()) > 64:
                raise ValueError(f"INVALID_STYLE_ID: {s_id}")

            s_name = s.get("display_name")
            if not isinstance(s_name, str) or not s_name.strip() or len(s_name.strip()) > 128:
                raise ValueError(f"INVALID_STYLE_DISPLAY_NAME: {s_name}")

            styles.append(ClaimStyle(id=s_id.strip(), display_name=s_name.strip()))

        # 3. Atomic formats validation (BLOCK C: exact allowed formats)
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

        raw_family = data.get("family_name")
        family_name = str(raw_family).strip() if (isinstance(raw_family, str) and raw_family.strip()) else None

        raw_foundry = data.get("foundry")
        foundry = str(raw_foundry).strip() if (isinstance(raw_foundry, str) and raw_foundry.strip()) else None

        return cls(
            job_id=job_id,
            order_id=order_id,
            lease_token=lease_token,
            lease_expires_at=lease_expires_at,
            source_url=source_url,
            family_name=family_name,
            foundry=foundry,
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


@dataclass(frozen=True)
class UploadResult:
    success: bool
    fenced: bool
    artifact_key: str | None = None
    sha256: str | None = None
    size: int = 0
    reason: str | None = None


@dataclass(frozen=True)
class CompleteResult:
    success: bool
    status: str
    queue_action: str  # "ack" | "retry"
    completed_at: int | None = None
    artifact_key: str | None = None
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

    async def upload_artifact(
        self,
        job_id: str,
        lease_token: str,
        zip_path: Path,
        sha256_hex: str,
    ) -> UploadResult:
        """Stream computed ZIP artifact to private Edge R2 upload endpoint."""
        url = f"{self.base_url}/{job_id}/artifact"
        file_size = zip_path.stat().st_size

        async def _chunk_stream():
            with open(zip_path, "rb") as f:
                while chunk := f.read(65536):
                    yield chunk

        headers = {
            "Authorization": f"Bearer {self.settings.A23_NODE_SECRET.get_secret_value()}",
            "X-Worker-Id": self.settings.A23_WORKER_ID,
            "X-Lease-Token": lease_token,
            "X-Artifact-SHA256": sha256_hex,
            "Content-Type": "application/zip",
            "Content-Length": str(file_size),
        }

        try:
            resp = await self._client.put(url, headers=headers, content=_chunk_stream())
            data = resp.json() if resp.content else {}

            if resp.status_code == 200:
                return UploadResult(
                    success=True,
                    fenced=False,
                    artifact_key=data.get("artifact_key"),
                    sha256=data.get("sha256"),
                    size=data.get("size", file_size),
                )

            if resp.status_code == 409:
                return UploadResult(
                    success=False,
                    fenced=True,
                    reason=data.get("reason", "lease_expired_or_fenced"),
                )

            return UploadResult(
                success=False,
                fenced=False,
                reason=data.get("reason", f"upload_error_{resp.status_code}"),
            )
        except httpx.RequestError as exc:
            logger.warning(f"Network error on artifact upload ({job_id}): {type(exc).__name__}")
            return UploadResult(success=False, fenced=False, reason="network_error")

    async def complete(
        self,
        job_id: str,
        lease_token: str,
        artifact_key: str,
        sha256_hex: str,
        size: int,
    ) -> CompleteResult:
        """Commit canonical job and order completion to D1."""
        url = f"{self.base_url}/{job_id}/complete"
        payload = {
            "worker_id": self.settings.A23_WORKER_ID,
            "lease_token": lease_token,
            "artifact_key": artifact_key,
            "sha256": sha256_hex,
            "size": size,
        }

        try:
            resp = await self._client.post(url, headers=self.headers, json=payload)
            data = resp.json() if resp.content else {}

            if resp.status_code == 200:
                return CompleteResult(
                    success=True,
                    status="COMPLETED",
                    queue_action=data.get("queue_action", "ack"),
                    completed_at=data.get("completed_at"),
                    artifact_key=data.get("artifact_key", artifact_key),
                )

            if resp.status_code == 409:
                queue_action = data.get("queue_action", "ack")
                status = data.get("status", "CONFLICT")
                reason = data.get("reason") or data.get("error", "conflict")
                return CompleteResult(
                    success=False,
                    status=status,
                    queue_action=queue_action,
                    reason=reason,
                )

            return CompleteResult(
                success=False,
                status="ERROR",
                queue_action="retry",
                reason=data.get("reason", f"complete_error_{resp.status_code}"),
            )
        except httpx.RequestError as exc:
            logger.warning(f"Network error on complete ({job_id}): {type(exc).__name__}")
            return CompleteResult(
                success=False,
                status="AMBIGUOUS_ERROR",
                queue_action="retry",
                reason="network_error",
            )

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

    async def get_pending_catalog_requests(self) -> list[PendingCatalogRequest]:
        """Fetch pending catalog requests awaiting metadata acquisition."""
        url = f"{self.settings.EDGE_BASE_URL}/internal/catalog-requests/pending"
        try:
            resp = await self._client.get(url, headers=self.headers)
            if resp.status_code == 200:
                data = resp.json()
                reqs = data.get("requests", [])
                return [
                    PendingCatalogRequest(
                        id=str(r.get("id", "")),
                        user_id=str(r.get("user_id", "")),
                        canonical_key=str(r.get("canonical_key", "")),
                        source_url=str(r.get("source_url", "")),
                        status=str(r.get("status", "PENDING")),
                        created_at=int(r.get("created_at", 0)),
                    )
                    for r in reqs
                    if r.get("id") and r.get("source_url")
                ]
            return []
        except Exception as exc:
            logger.warning(f"Error fetching pending catalog requests: {exc}")
            return []

    async def complete_catalog_request(
        self,
        request_id: str,
        payload: dict[str, Any],
    ) -> bool:
        """Post completed font catalog metadata back to Edge."""
        url = f"{self.settings.EDGE_BASE_URL}/internal/catalog-requests/{request_id}/complete"
        try:
            resp = await self._client.post(url, headers=self.headers, json=payload)
            return resp.status_code == 200
        except Exception as exc:
            logger.warning(f"Error completing catalog request ({request_id}): {exc}")
            return False

    async def fail_catalog_request(
        self,
        request_id: str,
        reason: str = "catalog_acquisition_failed",
    ) -> bool:
        """Post catalog failure notification back to Edge."""
        url = f"{self.settings.EDGE_BASE_URL}/internal/catalog-requests/{request_id}/fail"
        try:
            resp = await self._client.post(
                url,
                headers=self.headers,
                json={"reason": reason[:128], "error_code": reason[:64]},
            )
            return resp.status_code == 200
        except Exception as exc:
            logger.warning(f"Error failing catalog request ({request_id}): {exc}")
            return False


@dataclass(frozen=True)
class PendingCatalogRequest:
    id: str
    user_id: str
    canonical_key: str
    source_url: str
    status: str
    created_at: int
