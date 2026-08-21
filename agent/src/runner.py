"""A23 Compute Runner pipeline: Queue polling, source fetch, build, upload, and completion."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from compute.font_builder import FontBuilderService
from compute.models import GeneratedFontFile, JobPackageManifest, SourcePayload
from compute.packager import PackagerService
from compute.source import SourceAcquirer
from compute.validator import validate_font_file
from config import Settings
from queue_client import CloudflareQueueClient, QueueMessage
from scratch import ScratchManager
from worker_client import ClaimedJob, WorkerJobClient

QueueClient = CloudflareQueueClient
logger = logging.getLogger("telegramfonts.agent.runner")

LEASE_SAFETY_MARGIN_MS = 15000  # 15s deadline safety margin


class RunnerAction(str, Enum):
    ACKED = "acked"
    RETRIED = "retried"
    FAILED_TERMINAL = "failed_terminal"
    FENCED_ABORT = "fenced_abort"
    HOLD_FOR_COMPLETION = "hold_for_completion"


@dataclass
class ProcessResult:
    action: RunnerAction
    job_id: str | None = None
    reason: str | None = None
    manifest: JobPackageManifest | None = None


class JobRunner:
    def __init__(
        self,
        settings: Settings,
        queue_client: CloudflareQueueClient,
        worker_client: WorkerJobClient,
        scratch_manager: ScratchManager | None = None,
        source_acquirer: SourceAcquirer | None = None,
        font_builder: FontBuilderService | None = None,
        packager: PackagerService | None = None,
    ) -> None:
        self.settings = settings
        self.queue_client = queue_client
        self.worker_client = worker_client
        self.scratch_manager = scratch_manager or ScratchManager(settings.SCRATCH_DIR)
        self.source_acquirer = source_acquirer or SourceAcquirer(timeout=settings.HTTP_TIMEOUT_SECONDS)
        self.font_builder = font_builder or FontBuilderService()
        self.packager = packager or PackagerService()

    async def _heartbeat_loop(
        self,
        job_id: str,
        lease_token: str,
        expiry_holder: list[int],
        fenced_event: asyncio.Event,
        stop_event: asyncio.Event,
    ) -> None:
        """Background concurrent heartbeat loop."""
        interval = self.settings.HEARTBEAT_INTERVAL_SECONDS
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                pass

            if stop_event.is_set():
                break

            hb_res = await self.worker_client.heartbeat(job_id, lease_token)
            if hb_res.success and hb_res.lease_expires_at:
                expiry_holder[0] = hb_res.lease_expires_at
                logger.debug(f"Heartbeat renewed for job {job_id}, new expiry={hb_res.lease_expires_at}")
            elif hb_res.fenced:
                logger.warning(f"Job {job_id} lease was fenced or expired during execution")
                fenced_event.set()
                break
            else:
                logger.warning(f"Heartbeat transient failure for job {job_id}")

    def _sync_build_validate_and_package(
        self,
        source_payload: SourcePayload,
        job: ClaimedJob,
        job_dir: Path,
        fenced_event: asyncio.Event,
        expiry_holder: list[int],
    ) -> JobPackageManifest:
        """Synchronous CPU-bound pipeline running in a worker thread."""
        family_name = job.family_name or source_payload.family_name
        build_dir = job_dir / "build"
        build_dir.mkdir(parents=True, exist_ok=True)

        generated_files: list[GeneratedFontFile] = []

        for style in job.styles:
            style_data = source_payload.styles.get(style.id)
            if not style_data:
                raise ValueError(f"STYLE_MISSING_IN_SOURCE_{style.id}")

            for fmt in job.formats:
                now_ms = int(time.time() * 1000)
                if fenced_event.is_set() or (now_ms + LEASE_SAFETY_MARGIN_MS >= expiry_holder[0]):
                    raise RuntimeError("LEASE_FENCED_OR_EXPIRED")

                font_file = self.font_builder.build_font(
                    style_data,
                    family_name,
                    fmt,
                    build_dir,
                )

                if not validate_font_file(font_file.file_path, fmt):
                    raise ValueError(f"GENERATED_FONT_INVALID_{fmt}")

                generated_files.append(font_file)

        if not generated_files:
            raise ValueError("NO_FILES_GENERATED")

        now_ms_pre_pkg = int(time.time() * 1000)
        if fenced_event.is_set() or (now_ms_pre_pkg + LEASE_SAFETY_MARGIN_MS >= expiry_holder[0]):
            raise RuntimeError("LEASE_FENCED_OR_EXPIRED")

        manifest = self.packager.package_job_output(
            job_id=job.job_id,
            order_id=job.order_id,
            family_name=family_name,
            files=generated_files,
            output_dir=job_dir,
        )

        now_ms_post_pkg = int(time.time() * 1000)
        if fenced_event.is_set() or (now_ms_post_pkg + LEASE_SAFETY_MARGIN_MS >= expiry_holder[0]):
            self.scratch_manager.cleanup_job_dir(job_dir)
            raise RuntimeError("LEASE_FENCED_OR_EXPIRED")

        return manifest

    async def process_message(
        self,
        msg: QueueMessage,
        preview_input: bytes | dict[str, Any] | None = None,
    ) -> ProcessResult:
        logger.info(f"Processing message {msg.id} (attempts: {msg.attempts})")

        if not msg.job_id:
            logger.warning(f"Message {msg.id} has invalid or missing job_id; acknowledging/discarding")
            await self.queue_client.acknowledge_messages([msg.lease_id])
            return ProcessResult(action=RunnerAction.ACKED, reason="invalid_job_id")

        job_id = msg.job_id

        # 1. Authoritative claim against Worker D1
        claim_res = await self.worker_client.claim(job_id)

        if claim_res.queue_action == "ack":
            logger.info(f"Claim for job {job_id} returned ACK ({claim_res.reason})")
            await self.queue_client.acknowledge_messages([msg.lease_id])
            return ProcessResult(action=RunnerAction.ACKED, job_id=job_id, reason=claim_res.reason)

        if claim_res.queue_action == "retry":
            logger.info(f"Claim for job {job_id} returned RETRY ({claim_res.reason})")
            await self.queue_client.retry_messages([(msg.lease_id, 30)])
            return ProcessResult(action=RunnerAction.RETRIED, job_id=job_id, reason=claim_res.reason)

        if not claim_res.job:
            logger.warning(f"Unexpected claim state for {job_id}")
            await self.queue_client.retry_messages([(msg.lease_id, 30)])
            return ProcessResult(action=RunnerAction.RETRIED, job_id=job_id, reason="claim_no_job")

        # 2. Job successfully CLAIMED -> Execute isolated compute pipeline
        job = claim_res.job
        job_dir = self.scratch_manager.get_job_dir(job.job_id, job.lease_token)
        fenced_event = asyncio.Event()
        stop_event = asyncio.Event()
        expiry_holder = [job.lease_expires_at]

        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(job.job_id, job.lease_token, expiry_holder, fenced_event, stop_event)
        )

        try:
            # Step A: Validate and acquire source payload from real preview content
            source_payload = await self.source_acquirer.acquire_source(
                source_url=job.source_url,
                styles=job.styles,
                preview_input=preview_input,
            )

            # Step B & C: Build fonts, validate, and package in a worker thread off the event loop
            manifest = await asyncio.to_thread(
                self._sync_build_validate_and_package,
                source_payload,
                job,
                job_dir,
                fenced_event,
                expiry_holder,
            )

            # Step D: Upload ZIP artifact to private R2 storage endpoint
            if fenced_event.is_set():
                raise RuntimeError("LEASE_FENCED_OR_EXPIRED")

            upload_res = await self.worker_client.upload_artifact(
                job_id=job.job_id,
                lease_token=job.lease_token,
                zip_path=manifest.zip_file_path,
                sha256_hex=manifest.zip_sha256_hex,
            )

            if upload_res.fenced:
                logger.warning(f"Upload for job {job.job_id} was fenced")
                self.scratch_manager.cleanup_job_dir(job_dir)
                return ProcessResult(action=RunnerAction.FENCED_ABORT, job_id=job.job_id, reason="upload_fenced")

            if not upload_res.success or not upload_res.artifact_key:
                logger.warning(f"Upload failed for job {job.job_id}: {upload_res.reason}")
                await self.worker_client.fail(
                    job.job_id,
                    job.lease_token,
                    retryable=True,
                    reason_code="UPLOAD_FAILED",
                )
                await self.queue_client.retry_messages([(msg.lease_id, 30)])
                self.scratch_manager.cleanup_job_dir(job_dir)
                return ProcessResult(action=RunnerAction.RETRIED, job_id=job.job_id, reason="upload_failed")

            # Step E: Fenced atomic D1 completion
            if fenced_event.is_set():
                raise RuntimeError("LEASE_FENCED_OR_EXPIRED")

            complete_res = await self.worker_client.complete(
                job_id=job.job_id,
                lease_token=job.lease_token,
                artifact_key=upload_res.artifact_key,
                sha256_hex=manifest.zip_sha256_hex,
                size=manifest.zip_size_bytes,
            )

            # Step F: Finalize and ACK Queue boundary (BLOCK 6)
            if complete_res.success:
                # Durable completion committed to D1 -> Acknowledge Queue message
                await self.queue_client.acknowledge_messages([msg.lease_id])
                self.scratch_manager.cleanup_job_dir(job_dir)
                logger.info(f"Job {job.job_id} durably completed and ACKed from queue")
                return ProcessResult(action=RunnerAction.ACKED, job_id=job.job_id, manifest=manifest)

            if complete_res.status == "EXPIRED_OR_FENCED":
                logger.warning(f"Completion for job {job.job_id} was fenced")
                self.scratch_manager.cleanup_job_dir(job_dir)
                return ProcessResult(action=RunnerAction.FENCED_ABORT, job_id=job.job_id, reason="completion_fenced")

            if complete_res.status == "AMBIGUOUS_ERROR":
                logger.warning(f"Ambiguous network failure on complete for {job.job_id}; retrying message")
                await self.queue_client.retry_messages([(msg.lease_id, 30)])
                return ProcessResult(action=RunnerAction.RETRIED, job_id=job.job_id, reason="ambiguous_completion_network_error")

            self.scratch_manager.cleanup_job_dir(job_dir)
            if complete_res.queue_action == "ack":
                await self.queue_client.acknowledge_messages([msg.lease_id])
                return ProcessResult(action=RunnerAction.ACKED, job_id=job.job_id, reason=complete_res.reason)
            else:
                await self.queue_client.retry_messages([(msg.lease_id, 30)])
                return ProcessResult(action=RunnerAction.RETRIED, job_id=job.job_id, reason=complete_res.reason)

        except (ValueError, RuntimeError) as exc:
            err_code = str(exc)
            logger.warning(f"Error during compute execution for {job.job_id}: {err_code}")

            if "LEASE_FENCED" in err_code:
                self.scratch_manager.cleanup_job_dir(job_dir)
                return ProcessResult(action=RunnerAction.FENCED_ABORT, job_id=job.job_id, reason="fenced")

            terminal_codes = {
                "INVALID_SOURCE_URL",
                "NO_PUBLIC_PREVIEW_FOUND",
                "SOURCE_ACQUISITION_BLOCKED_403",
                "SOURCE_ACQUISITION_BLOCKED_429",
                "SOURCE_PREVIEW_PARSE_FAILED",
                "MALFORMED_SOURCE_INPUT",
                "CORRUPT_SOURCE_IMAGE",
                "UNSUPPORTED_FORMAT",
                "NO_FILES_GENERATED",
            }
            is_terminal = any(tc in err_code for tc in terminal_codes)

            fail_res = await self.worker_client.fail(
                job_id=job.job_id,
                lease_token=job.lease_token,
                retryable=not is_terminal,
                reason_code=err_code[:64],
            )

            self.scratch_manager.cleanup_job_dir(job_dir)

            if fail_res.queue_action == "ack":
                await self.queue_client.acknowledge_messages([msg.lease_id])
                return ProcessResult(action=RunnerAction.FAILED_TERMINAL, job_id=job.job_id, reason=err_code)
            else:
                delay = fail_res.delay_seconds or 30
                await self.queue_client.retry_messages([(msg.lease_id, delay)])
                return ProcessResult(action=RunnerAction.RETRIED, job_id=job.job_id, reason=err_code)

        finally:
            stop_event.set()
            await heartbeat_task

    async def process_pending_catalogs(self, max_requests: int = 1) -> int:
        """Resolve at most max_requests pending catalog request(s) per loop to protect Queue latency."""
        try:
            reqs = await self.worker_client.get_pending_catalog_requests()
        except Exception as exc:
            logger.warning(f"Error checking pending catalog requests: {exc}")
            return 0

        if not reqs:
            return 0

        processed = 0
        for req in reqs[:max_requests]:
            try:
                # Acquire authentic metadata from source layer; fails closed if no styles found
                metadata = await self.source_acquirer.acquire_catalog_metadata(req.source_url)
                metadata["canonical_key"] = req.canonical_key
                metadata["source_url"] = req.source_url

                success = await self.worker_client.complete_catalog_request(req.id, metadata)
                if success:
                    processed += 1
                    logger.info(f"Catalog request {req.id} resolved with authentic styles for {metadata.get('family_name')}")
                else:
                    logger.warning(f"Transient error completing catalog request {req.id}; leaving retryable")
            except ValueError as exc:
                # Terminal source/parser/validation failure: fail request out of PENDING and notify user
                logger.warning(f"Terminal failure processing catalog request {req.id}: {exc}")
                await self.worker_client.fail_catalog_request(req.id, str(exc))
            except Exception as exc:
                # Transient network, 5xx, or transport error: leave retryable in D1
                logger.warning(f"Transient error processing catalog request {req.id}: {exc}")
        return processed

    async def close(self) -> None:
        """Close client connections and release resources."""
        await self.queue_client.close()
        await self.worker_client.close()
        if hasattr(self.source_acquirer, "close"):
            await self.source_acquirer.close()

    async def run_once(self) -> list[ProcessResult]:
        """Pull a batch of messages from Queue and process each, then check pending catalog requests."""
        # 1. Prioritize paid fulfillment queue polling first (BLOCK 5)
        messages = await self.queue_client.pull_messages(
            batch_size=self.settings.PULL_BATCH_SIZE,
            visibility_timeout_ms=self.settings.VISIBILITY_TIMEOUT_MS,
        )

        results: list[ProcessResult] = []
        for msg in messages:
            res = await self.process_message(msg)
            results.append(res)

        # 2. Process background catalog requests only after queue messages
        await self.process_pending_catalogs()

        return results

    async def run_loop(
        self,
        stop_event: asyncio.Event | None = None,
        max_iterations: int | None = None,
    ) -> None:
        """Continuous long-polling runner loop with graceful stop support."""
        iterations = 0
        while (stop_event is None or not stop_event.is_set()) and (
            max_iterations is None or iterations < max_iterations
        ):
            iterations += 1
            try:
                results = await self.run_once()
                if not results:
                    if stop_event is not None:
                        try:
                            await asyncio.wait_for(
                                stop_event.wait(), timeout=self.settings.IDLE_BACKOFF_SECONDS
                            )
                            break
                        except asyncio.TimeoutError:
                            pass
                    else:
                        await asyncio.sleep(self.settings.IDLE_BACKOFF_SECONDS)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"Error in runner loop iteration {iterations}: {exc}")
                if stop_event is not None:
                    try:
                        await asyncio.wait_for(
                            stop_event.wait(), timeout=self.settings.ERROR_BACKOFF_SECONDS
                        )
                        break
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(self.settings.ERROR_BACKOFF_SECONDS)


A23Runner = JobRunner

