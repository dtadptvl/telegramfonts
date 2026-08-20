"""A23 Compute Runner state machine and message lifecycle."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from config import Settings
from compute import (
    FontBuilderService,
    PackagerService,
    SourceAcquirer,
    StagedManifest,
    validate_font_file,
)
from queue_client import CloudflareQueueClient, QueueMessage
from scratch import ScratchManager
from worker_client import ClaimedJob, WorkerJobClient

logger = logging.getLogger("telegramfonts.agent.runner")


class RunnerAction(str, Enum):
    HOLD_FOR_COMPLETION = "HOLD_FOR_COMPLETION"
    ACKED = "ACKED"
    RETRIED = "RETRIED"
    FENCED_ABORT = "FENCED_ABORT"
    ERROR = "ERROR"


@dataclass
class ProcessResult:
    action: RunnerAction
    job_id: str | None = None
    manifest: StagedManifest | None = None
    reason: str | None = None


class A23Runner:
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
        self.source_acquirer = source_acquirer or SourceAcquirer(settings.HTTP_TIMEOUT_SECONDS)
        self.font_builder = font_builder or FontBuilderService()
        self.packager = packager or PackagerService()

    async def _heartbeat_loop(
        self,
        job_id: str,
        lease_token: str,
        fenced_event: asyncio.Event,
        stop_event: asyncio.Event,
    ) -> None:
        """Background heartbeat loop maintaining D1 lease until compute finishes or fences."""
        interval = self.settings.HEARTBEAT_INTERVAL_SECONDS
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                break  # Stop requested
            except asyncio.TimeoutError:
                pass  # Interval reached, send heartbeat

            if stop_event.is_set():
                break

            hb_res = await self.worker_client.heartbeat(job_id, lease_token)
            if hb_res.fenced:
                logger.warning(f"Heartbeat detected lease fencing for job {job_id}")
                fenced_event.set()
                break

    async def process_message(self, msg: QueueMessage) -> ProcessResult:
        logger.info(f"Processing message {msg.id} (attempts: {msg.attempts})")

        # 1. Validate payload contains job_id
        if not msg.job_id:
            logger.warning(f"Message {msg.id} has invalid or missing job_id; acknowledging/discarding")
            await self.queue_client.acknowledge_messages([msg.lease_id])
            return ProcessResult(action=RunnerAction.ACKED, reason="invalid_job_id")

        job_id = msg.job_id

        # 2. Authoritative claim against Worker D1
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

        # 3. Job successfully CLAIMED -> Execute isolated compute pipeline
        job = claim_res.job
        job_dir = self.scratch_manager.get_job_dir(job.job_id, job.lease_token)
        fenced_event = asyncio.Event()
        stop_event = asyncio.Event()

        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(job.job_id, job.lease_token, fenced_event, stop_event)
        )

        try:
            # Step A: Validate and acquire source
            await self.source_acquirer.acquire_source(job.source_url)

            # Step B: Generate requested styles and formats
            generated_files = []
            family_name = job.family_name or "TeleFont"

            for style in job.styles:
                for fmt in job.formats:
                    if fenced_event.is_set():
                        raise RuntimeError("LEASE_FENCED")

                    if fmt not in ("TTF", "OTF", "WOFF2"):
                        raise ValueError(f"UNSUPPORTED_FORMAT_{fmt}")

                    font_file = self.font_builder.build_font(
                        family_name=family_name,
                        style_name=style.display_name,
                        format_type=fmt,
                        output_dir=job_dir,
                    )

                    # Validate generated font binary
                    if not validate_font_file(font_file.file_path, fmt):
                        raise ValueError(f"GENERATED_FONT_INVALID_{fmt}")

                    generated_files.append(font_file)

            if not generated_files:
                raise ValueError("NO_FILES_GENERATED")

            if fenced_event.is_set():
                raise RuntimeError("LEASE_FENCED")

            # Step C: Package outputs into deterministic ZIP and manifest
            manifest = self.packager.package_job_output(
                job_id=job.job_id,
                order_id=job.order_id,
                family_name=family_name,
                files=generated_files,
                output_dir=job_dir,
            )

            logger.info(
                f"Successfully computed job {job.job_id} -> staged {manifest.zip_filename} "
                f"({manifest.zip_size_bytes} bytes, HOLD_FOR_COMPLETION)"
            )

            # Hold for Phase 6 completion without ACK-ing queue message or marking complete
            return ProcessResult(
                action=RunnerAction.HOLD_FOR_COMPLETION,
                job_id=job.job_id,
                manifest=manifest,
            )

        except Exception as exc:
            logger.error(f"Compute error for job {job.job_id}: {exc}")

            if isinstance(exc, RuntimeError) and "LEASE_FENCED" in str(exc):
                return ProcessResult(
                    action=RunnerAction.FENCED_ABORT,
                    job_id=job.job_id,
                    reason="lease_fenced",
                )

            # Classify error: retryable vs terminal
            err_text = str(exc)
            if any(k in err_text for k in ("INVALID_SOURCE_URL", "UNSUPPORTED_FORMAT", "GENERATED_FONT_INVALID", "NO_FILES_GENERATED")):
                retryable = False
                reason_code = err_text.replace(" ", "_").upper()[:64]
            else:
                retryable = True
                reason_code = "COMPUTE_ERROR"

            fail_res = await self.worker_client.fail(
                job_id=job.job_id,
                lease_token=job.lease_token,
                retryable=retryable,
                reason_code=reason_code,
            )

            if fail_res.queue_action == "ack":
                await self.queue_client.acknowledge_messages([msg.lease_id])
                return ProcessResult(action=RunnerAction.ACKED, job_id=job.job_id, reason=reason_code)
            else:
                delay = fail_res.delay_seconds or 30
                await self.queue_client.retry_messages([(msg.lease_id, delay)])
                return ProcessResult(action=RunnerAction.RETRIED, job_id=job.job_id, reason=reason_code)

        finally:
            stop_event.set()
            await heartbeat_task
