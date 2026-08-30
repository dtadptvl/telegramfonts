"""A23 Compute Runner pipeline: Queue polling, source fetch, build, upload, and completion."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from acquisition.models import (
    AcquisitionOutcome,
    AcquiredBinary,
    BINARY_PROVENANCE_PROBE_ORDER,
    BINARY_STAGE_AUTHORIZED_SESSION,
    BINARY_STAGE_DUMP_DOM,
)
from acquisition.pipeline import AcquisitionPipeline
from acquisition.capability import PROVIDER_MONOTYPE_RENDER, ProviderRasterCapability, resolve_raster_provider
from acquisition.raster_ingest import (
    RASTER_FALLBACK_PROVENANCE,
    collect_browser_measurement,
    ingest_raster_pages,
    page_slice_attestation,
)
from compute.archive import (
    ARCHIVEABLE_FORMATS,
    PROVENANCE_BINARY_DUMP_DOM,
    PROVENANCE_BINARY_SESSION,
    PROVENANCE_PROBE_ORDER,
    PROVENANCE_STAGE9D_RASTER,
    PROVENANCE_VIETNAMESE_AI,
    PROVENANCE_VIETNAMESE_PRESERVED,
    ArchiveIdentity,
    ArchiveModeResolution,
    FinalFontArchive,
    canonical_source_identity,
    resolve_archive_mode,
)
from compute.binary_cache import AuthorizedBinaryCache, BinaryCacheIdentity
from compute.binary_gate import BINARY_PIPELINE_VERSION, BinaryConsumerValidator, BinaryGateReport, prepare_binary_artifact
from compute.font_builder import FontBuilderService
from compute.models import ArchiveSourceContext, GeneratedFontFile, JobPackageManifest, ManifestPart, SourcePayload
from compute.model_cache import CanonicalFontModelCache, FontModelCacheIdentity
from compute.packager import PackagerService
from compute.source import SourceAcquirer
from measurement.models import ObservationConfig
from compute.validator import validate_font_file
from config import Settings
from fidelity.profiles import FAST_30_PROFILE
from fidelity.release_gate import STAGE9D_ATTESTATION_SCHEMA_VERSION, Stage9DAttestation, Stage9DReleaseGate
from queue_client import CloudflareQueueClient, QueueMessage
from scratch import ScratchManager
from worker_client import ClaimedJob, WorkerJobClient

QueueClient = CloudflareQueueClient
logger = logging.getLogger("telegramfonts.agent.runner")

# T-FAST30-A23-FIX F1: safety margin applied to the job-level monotonic wall
# (claim -> ACK). Guards keep enough budget for package/upload/complete/ACK
# and fail closed (all-or-nothing) instead of starting work they cannot
# finish inside the wall.
WALL_SAFETY_MARGIN_MS = 15000


class JobWallLimitExceeded(RuntimeError):
    """Job-level monotonic wall breached: terminal FAST30_FAILED.

    The message is the exact bounded terminal code so the sanitized error
    mapping surfaces FAST30_FAILED (no fallback/escalation, no
    upload/archive after the breach). Independent of the heartbeat-moved
    lease expiry by construction.
    """

    def __init__(self) -> None:
        super().__init__("FAST30_FAILED")


def touch_progress_beacon(path: Path | str | None, stage: str = "") -> None:
    """T-FAST30-A23-FIX F5: touch the supervisor progress beacon file.

    Process-lifecycle signal only (stage transitions / heartbeat beats);
    never part of pipeline semantics. Absent path disables the beacon.
    Failures are logged at debug and never affect the pipeline.
    """
    if path is None:
        return
    try:
        Path(path).touch(exist_ok=True)
    except OSError as exc:
        logger.debug("progress beacon touch failed (stage=%s): %s", stage, type(exc).__name__)

FENCED_ERROR_CODE = "LEASE_FENCED_OR_EXPIRED"
UNEXPECTED_ERROR_CODE = "UNEXPECTED_RUNTIME_ERROR"

TERMINAL_ERROR_CODES = frozenset({
    "INVALID_SOURCE_URL",
    "NO_PUBLIC_PREVIEW_FOUND",
    "SOURCE_ACQUISITION_BLOCKED_403",
    "SOURCE_ACQUISITION_BLOCKED_429",
    "SOURCE_PREVIEW_PARSE_FAILED",
    "MALFORMED_SOURCE_INPUT",
    "CORRUPT_SOURCE_IMAGE",
    "UNSUPPORTED_FORMAT",
    "MISSING_MODE",
    "UNSUPPORTED_MODE",
    "NO_FILES_GENERATED",
    "STAGE9D_GATE_FAILED",
    "ACQUISITION_BINARY_INTEGRITY_FAILED",
    "ACQUISITION_INSUFFICIENT",
    "VIETNAMESE_EXTENSION_FAILED",
    "FAST30_FAILED",
})

KNOWN_ERROR_CODES = frozenset(
    TERMINAL_ERROR_CODES
    | {
        FENCED_ERROR_CODE,
        "MISSING_SOURCE_PAYLOAD",
        "FINAL_FONT_ARCHIVE_WRITE_FAILED",
        "NO_OBSERVABLE_BROWSER_FONT_FACES",
    }
)

KNOWN_ERROR_PREFIXES = (
    ("STYLE_MISSING_IN_SOURCE_", "STYLE_MISSING_IN_SOURCE"),
    ("GENERATED_FONT_INVALID_", "GENERATED_FONT_INVALID"),
    ("SOURCE_HTTP_ERROR_", "SOURCE_HTTP_ERROR"),
    ("MALFORMED_SOURCE_INPUT_", "MALFORMED_SOURCE_INPUT"),
    ("MALFORMED_GLYPH_DATA:", "MALFORMED_GLYPH_DATA"),
    ("NO_OBSERVABLE_GLYPHS_FOR_", "NO_OBSERVABLE_GLYPHS"),
    ("NO_MAX_RECONSTRUCTION_FOR_", "NO_MAX_RECONSTRUCTION"),
    ("NO_MAX_STYLES_COMPILED_FOR_", "NO_MAX_STYLES_COMPILED"),
    ("NO_MAX_OBSERVATIONS_FOUND_FOR_", "NO_MAX_OBSERVATIONS_FOUND"),
    ("NO_MAX_RECONSTRUCTED_GLYPHS_AVAILABLE_FOR_", "NO_MAX_RECONSTRUCTED_GLYPHS_AVAILABLE"),
    ("COMPLETED_MAX_COLLECTION_HAS_NO_COVERAGE_", "COMPLETED_MAX_COLLECTION_HAS_NO_COVERAGE"),
    ("FAILED_BUILDING_FORMAT_", "FAILED_BUILDING_FORMAT"),
    ("UNSUPPORTED_ARCHIVE_FORMAT:", "UNSUPPORTED_ARCHIVE_FORMAT"),
    ("UNSUPPORTED_ARCHIVE_MODE:", "UNSUPPORTED_ARCHIVE_MODE"),
    ("UNSUPPORTED_FORMAT:", "UNSUPPORTED_FORMAT"),
    ("EMPTY_ARCHIVE_IDENTITY_", "EMPTY_ARCHIVE_IDENTITY"),
    ("ARTIFACT_PART_EXCEEDS_CAP:", "ARTIFACT_PART_EXCEEDS_CAP"),
    ("STAGE9D_GATE_FAILED_", "STAGE9D_GATE_FAILED"),
    ("ACQUISITION_BINARY_INTEGRITY_FAILED:", "ACQUISITION_BINARY_INTEGRITY_FAILED"),
    ("VI_", "VIETNAMESE_EXTENSION_FAILED"),
)


def _safe_error_code(exc: Exception) -> str:
    """Map only known internal errors to bounded codes; never expose exception text."""
    raw = str(exc)
    if raw in KNOWN_ERROR_CODES:
        return raw
    for prefix, canonical in KNOWN_ERROR_PREFIXES:
        if raw.startswith(prefix):
            return canonical
    return UNEXPECTED_ERROR_CODE


SUPPORTED_JOB_MODES = frozenset({"ORIGINAL", "VIETNAMESE"})


def _require_job_mode(job: ClaimedJob) -> str:
    """Fail-closed mode binding (T-PRICE-01).

    A job with an absent or unsupported mode never defaults to ORIGINAL:
    the run fails with a clear terminal reason code (MISSING_MODE or
    UNSUPPORTED_MODE). Defense-in-depth under the required-mode claim
    validation in ClaimedJob.from_dict.
    """
    raw = job.mode if isinstance(job.mode, str) else ""
    if not raw.strip():
        raise ValueError("MISSING_MODE")
    mode = raw.strip().upper()
    if mode not in SUPPORTED_JOB_MODES:
        raise ValueError("UNSUPPORTED_MODE")
    return mode


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
        archive: FinalFontArchive | None = None,
        acquisition_pipeline: AcquisitionPipeline | None = None,
        model_cache: CanonicalFontModelCache | None = None,
        binary_cache: AuthorizedBinaryCache | None = None,
        vietnamese_ai_provider: Any = None,
    ) -> None:
        self.settings = settings
        self.queue_client = queue_client
        self.worker_client = worker_client
        self.scratch_manager = scratch_manager or ScratchManager(settings.SCRATCH_DIR)
        self.source_acquirer = source_acquirer or SourceAcquirer(
            timeout=settings.HTTP_TIMEOUT_SECONDS,
            cache_dir=self.scratch_manager.root / "source_cache",
            # Production observation schedule: the exact canonical MAX
            # observation schedule (measurement.max_profile), never legacy.
            # FAST_30 is the sole reconstruction profile (ADR-0001).
            observation_config=ObservationConfig.max_profile(),
        )
        self.font_builder = font_builder or FontBuilderService(
            observation_store_dir=getattr(self.source_acquirer, "store_dir", None)
        )
        self.packager = packager or PackagerService()
        # D21 safe archive mode (Issue #90): explicit, versioned, fail-closed.
        # NO_LOCAL_ARCHIVE disables the local L1 archive entirely (repeat
        # orders recompute; delivery unchanged); injecting an archive into a
        # worker explicitly configured for NO_LOCAL_ARCHIVE is a forbidden
        # fake-archive path and fails closed at construction.
        self.archive_mode: ArchiveModeResolution = resolve_archive_mode(
            settings, archive_present=archive is not None
        )
        if archive is not None and not self.archive_mode.archive_enabled:
            raise ValueError("ARCHIVE_FORBIDDEN_IN_NO_LOCAL_ARCHIVE_MODE")
        if self.archive_mode.archive_enabled:
            self.archive = archive if archive is not None else FinalFontArchive.from_settings(settings)
        else:
            self.archive = None
        self.acquisition_pipeline = acquisition_pipeline
        self.model_cache = model_cache
        self.binary_cache = binary_cache
        self.vietnamese_ai_provider = vietnamese_ai_provider
        # Deterministic per-job acquisition/reuse call trace (sanitized).
        self.last_reuse_trace: dict[str, Any] = {}

    def _heartbeat_thread_main(
        self,
        job_id: str,
        lease_token: str,
        expiry_holder: list[int],
        fenced_event: threading.Event,
        stop_event: threading.Event,
    ) -> None:
        """Lease heartbeat loop on a dedicated thread (independent scheduler).

        The heartbeat MUST keep extending the lease for the full duration of
        acquisition/reconstruction/optimization/delivery. Running it as an
        asyncio task on the compute loop was proven to starve it whenever
        synchronous compute blocked the loop (Issue #90 attempt 5: zero
        heartbeat requests and zero D1 lease extensions over ~21 minutes of
        healthy compute; the cron reaper then correctly fenced the run). A
        dedicated thread keeps beating regardless of event-loop blocking.
        Cadence, endpoint payload, and fenced/transient reactions are
        unchanged; reaper/lease semantics live on the edge and are untouched.
        Renewals log at INFO so lease extensions are observable in
        production logs.
        """
        interval = self.settings.HEARTBEAT_INTERVAL_SECONDS
        while not stop_event.wait(timeout=interval):
            if stop_event.is_set():
                break

            # T-FAST30-A23-FIX F5: a beating heartbeat IS progress; the
            # supervisor watchdog kills only when this goes stale.
            self._touch_progress_beacon("heartbeat")

            try:
                hb_res = self.worker_client.heartbeat_sync(job_id, lease_token)
            except Exception as exc:
                logger.warning(
                    "Heartbeat exception for %s; class=%s",
                    job_id,
                    type(exc).__name__,
                )
                continue
            if hb_res.success and hb_res.lease_expires_at:
                expiry_holder[0] = hb_res.lease_expires_at
                logger.info(f"Heartbeat renewed for job {job_id}, new expiry={hb_res.lease_expires_at}")
            elif hb_res.fenced:
                logger.warning(f"Job {job_id} lease was fenced or expired during execution")
                fenced_event.set()
                break
            else:
                logger.warning(f"Heartbeat transient failure for job {job_id}")

    def _touch_progress_beacon(self, stage: str = "") -> None:
        touch_progress_beacon(
            getattr(self.settings, "PROGRESS_BEACON_FILE", None), stage
        )

    def _durable_checkpoint_root(self, job_id: str) -> Path:
        """T-FAST30-A23-FIX F6: durable checkpoint root scoped to the job.

        Lives in the durable scratch-root cache namespace (analogous to
        font_model_cache), NOT in the lease-token-bound job dir, so a
        re-claimed attempt of the same job resumes from persisted
        identity-bound checkpoints instead of restarting. Distinct jobs are
        isolated by construction; identity-hash validation stays fail-closed.
        """
        return self.scratch_manager.get_durable_job_cache_dir(job_id, "glyph_checkpoints")

    @staticmethod
    def _family_name_from_url(source_url: str) -> str:
        parsed = urlparse(source_url.strip())
        path_parts = [p for p in parsed.path.split("/") if p]
        return path_parts[-1].replace("-", " ").title() if path_parts else "TeleFont"

    def _get_archive_context(self, job: ClaimedJob) -> ArchiveSourceContext | None:
        context_getter = getattr(self.source_acquirer, "get_archive_context", None)
        if not callable(context_getter):
            return None
        return context_getter(job.source_url, job.styles)

    def _make_archive_identity(
        self,
        job: ClaimedJob,
        family_name: str,
        style_id: str,
        style_name: str,
        format_type: str,
        context: ArchiveSourceContext,
        provenance: str = PROVENANCE_STAGE9D_RASTER,
        ai_binding: str = "",
    ) -> ArchiveIdentity:
        return ArchiveIdentity(
            source_identity=context.source_identity,
            family_name=family_name,
            style_id=style_id,
            style_name=style_name,
            mode=job.mode,
            format=format_type,
            observation_identity=context.observation_identity_for(style_id),
            config_version=context.config_version,
            provenance=provenance,
            ai_binding=ai_binding,
        )

    def _get_archive_hit(
        self,
        job: ClaimedJob,
        family_name: str,
        context: ArchiveSourceContext | None,
    ) -> list[GeneratedFontFile] | None:
        """Return all requested files only when every requested format is a verified
        attested hit. Legacy/unattested/tampered entries are cache misses."""
        if self.archive is None or context is None:
            return None
        if not job.formats or any(fmt not in ARCHIVEABLE_FORMATS for fmt in job.formats):
            return None

        cached_files: list[GeneratedFontFile] = []
        for style in job.styles:
            for fmt in job.formats:
                hit = self._probe_l1(job, family_name, style.id, style.display_name, fmt, context)
                if hit is None:
                    return None
                cached_files.append(hit)
        return cached_files or None

    def _probe_l1(
        self,
        job: ClaimedJob,
        family_name: str,
        style_id: str,
        style_name: str,
        fmt: str,
        context: ArchiveSourceContext,
    ) -> GeneratedFontFile | None:
        """L1 exact final-artifact reuse across the deterministic provenance order."""
        if self.archive is None:
            return None
        for provenance in PROVENANCE_PROBE_ORDER:
            identity = self._make_archive_identity(
                job, family_name, style_id, style_name, fmt, context,
                provenance=provenance, ai_binding="",
            )
            found = self.archive.get_attested_any_binding(identity)
            if found is None:
                continue
            entry, ai_binding = found
            if ai_binding:
                # Re-resolve with the exact binding so identity stays complete.
                bound_identity = self._make_archive_identity(
                    job, family_name, style_id, style_name, fmt, context,
                    provenance=provenance, ai_binding=ai_binding,
                )
                if self.archive.get_attested(bound_identity) is None:
                    continue
            self._trace_record(f"L1_{fmt}", "HIT", provenance=provenance)
            return entry.to_generated_font_file()
        return None

    def _trace_record(self, key: str, event: str, **fields: Any) -> None:
        trace = self.last_reuse_trace.setdefault("events", [])
        trace.append({"key": key, "event": event, **fields})

    def _stage9d_gate_artifact(
        self,
        style_data: Any,
        family_name: str,
        style_id: str,
        style_name: str,
        fmt: str,
        build_dir: Path,
        mode: str = "ORIGINAL",
        vietnamese_service: Any = None,
    ) -> tuple[GeneratedFontFile, Any]:
        """Run the Stage 9D release gate for one style+format; fail-closed.

        Returns the exact PASS-gated artifact as a GeneratedFontFile plus the
        attestation payload. Raises a sanitized error on any non-publishable outcome.
        """
        gate_store = getattr(self.source_acquirer, "store", None)
        gate_config = getattr(self.source_acquirer, "observation_config", None)
        if gate_store is None or gate_config is None:
            raise ValueError(f"STAGE9D_GATE_FAILED_{fmt}")

        result = Stage9DReleaseGate.execute_sync(
            store=gate_store,
            config=gate_config,
            reference_id=style_data.observation_reference_id,
            style_id=style_data.observation_style_id or style_id,
            family_name=family_name,
            style_name=style_name,
            browser_version=style_data.observation_browser_version,
            format_type=fmt,
            output_dir=build_dir / "stage9d" / fmt.lower(),
            mode=mode,
            vietnamese_service=vietnamese_service,
            provider_capability=self._sealed_provider_capability(
                gate_store,
                style_data.observation_reference_id,
                style_data.observation_style_id or style_id,
                style_data.observation_browser_version,
                gate_config.compute_hash(),
            ),
            # Production flow under FAST_30, the sole reconstruction
            # profile (ADR-0001): deterministic confidence gate +
            # unchanged final gates + 30-minute wall limit; no
            # fallback/escalation of any trigger type.
            reconstruction_profile=FAST_30_PROFILE,
        )
        if not result.is_publishable or result.attestation is None:
            # FAST_30 regime: deadline or quality failure returns
            # FAST30_FAILED and stops (no fallback/escalation).
            raise ValueError("FAST30_FAILED")

        artifact_path = Path(result.candidate_file_path)
        font_file = GeneratedFontFile(
            style_id=style_id,
            style_name=style_name,
            format=fmt,
            filename=artifact_path.name,
            file_path=artifact_path,
            size_bytes=result.candidate_size_bytes,
            sha256_hex=result.candidate_artifact_sha,
        )
        return font_file, result.attestation, result

    # ------------------------------------------------------------------
    # Tiered reuse coordinator: L1 final artifact -> L2 FontModel ->
    # L3 authorized binary -> L4 exact raster observations -> acquisition.
    # A tier skips only causally replaced work; all gates stay fail-closed.
    # ------------------------------------------------------------------

    def _observation_keys(self, source_url: str, style_id: str) -> tuple[str, str]:
        family_key = (
            self._family_name_from_url(source_url).lower().replace(" ", "_").replace("-", "_")
        )
        style_key = style_id.lower().replace(" ", "_").replace("-", "_")
        return family_key, style_key

    @staticmethod
    def _sealed_provider_capability(store, family_key, style_key, browser_version, config_hash):
        """Recover the sealed provider capability of a completed collection.

        Direct-browser collections carry none (returns None, preserving the
        phase-held-out partition). Forged or drifted sealed descriptors fail
        closed at snapshot load.
        """
        capability_json, capability_hash = store.get_source_collection_capability(
            family_key, style_key, browser_version=browser_version, config_hash=config_hash
        )
        if not capability_json:
            return None
        capability = ProviderRasterCapability.from_json(capability_json)
        if capability.compute_hash() != capability_hash:
            raise ValueError("CAPABILITY_FORGED: sealed capability hash drift")
        return capability

    @staticmethod
    def _reference_fingerprint(
        archive_context: ArchiveSourceContext,
        style_id: str,
        browser_version: str,
        config_hash: str,
        coverage_fingerprint: str,
    ) -> str:
        payload = {
            "source_identity": archive_context.source_identity,
            "observation_identity": archive_context.observation_identity_for(style_id),
            "config_version": archive_context.config_version,
            "browser_version": browser_version,
            "config_hash": config_hash,
            "coverage_fingerprint": coverage_fingerprint,
        }
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _vietnamese_service(self, mode: str, config_hash: str, source_hash: str):
        """ORIGINAL mode never receives an AI service (zero AI work)."""
        if mode.strip().upper() != "VIETNAMESE":
            return None
        from compute.vietnamese import VietnameseExtensionService

        return VietnameseExtensionService(
            ai_provider=self.vietnamese_ai_provider,
            config_hash=config_hash,
            source_hash=source_hash,
        )

    @staticmethod
    def _coverage_fingerprint(coverage: list[int]) -> str:
        return hashlib.sha256(
            json.dumps(sorted(coverage), separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _completed_identities(self, store: Any, family_key: str, style_key: str, cfg_h: str):
        try:
            identities = [
                (b, c)
                for b, c in store.get_completed_collection_identities(family_key, style_key)
                if c == cfg_h and store.is_source_collection_completed(family_key, style_key, c, b)
            ]
        except Exception:
            return []
        return sorted(identities)

    def _binary_attestation(
        self,
        binary: Any,
        report: BinaryGateReport,
        family_key: str,
        style_key: str,
        config_hash: str,
    ) -> Stage9DAttestation:
        provenance = (
            PROVENANCE_BINARY_DUMP_DOM
            if binary.provenance == BINARY_STAGE_DUMP_DOM
            else PROVENANCE_BINARY_SESSION
        )
        return Stage9DAttestation(
            schema_version=STAGE9D_ATTESTATION_SCHEMA_VERSION,
            format=report.format,
            artifact_sha256=report.artifact_sha256,
            artifact_size_bytes=report.artifact_size_bytes,
            reference_id=family_key,
            style_id=style_key,
            browser_version="authorized_binary",
            config_hash=config_hash,
            snapshot_fingerprint=binary.sha256_hex,
            fit_set_fingerprint="",
            held_out_set_fingerprint="",
            model_hash="",
            policy_hash=BINARY_PIPELINE_VERSION,
            report_id=f"bin_{report.compute_report_hash()[:16]}",
            report_hash=report.compute_report_hash(),
            consumer_bundle_hash="",
            optimizer_trace_hash="",
            optimizer_converged=True,
            overall_status=report.overall_status,
            provenance=provenance,
            ai_binding="",
        )

    def _tiered_resolve_artifact(
        self,
        job: ClaimedJob,
        style: Any,
        family_name: str,
        archive_context: ArchiveSourceContext | None,
        fmt: str,
        build_dir: Path,
        reuse_state: dict[str, Any],
        job_deadline: float | None = None,
    ) -> tuple[GeneratedFontFile, Any, str, str]:
        """Resolve one style+format through the reuse tiers; fail-closed."""
        # T-FAST30-A23-FIX F1: clamp the gate wall to the remaining job-level
        # monotonic budget (never exceeding the FAST_30 profile limit). The
        # gate wall is no longer born at gate entry with the full profile
        # limit; it inherits the claim->ACK job wall.
        wall_budget: float | None = None
        if job_deadline is not None:
            wall_budget = job_deadline - time.monotonic()
            if wall_budget <= 0:
                raise JobWallLimitExceeded()
            wall_budget = min(wall_budget, float(FAST_30_PROFILE.wall_limit_seconds))
        mode = _require_job_mode(job)
        store = getattr(self.source_acquirer, "store", None)
        config = getattr(self.source_acquirer, "observation_config", None)

        # L1: exact final artifact (per-item; supports mixed hit/miss orders).
        if archive_context is not None and self.archive is not None:
            hit = self._probe_l1(job, family_name, style.id, style.display_name, fmt, archive_context)
            if hit is not None:
                return hit, None, "", ""

        if store is None or config is None:
            raise ValueError(f"STAGE9D_GATE_FAILED_{fmt}")
        cfg_h = config.compute_hash()
        family_key, style_key = self._observation_keys(job.source_url, style.id)
        completed = self._completed_identities(store, family_key, style_key, cfg_h)

        # L2: canonical FontModel reuse (skips acquisition + reconstruction +
        # optimization; candidate build and held-out consumer gating still run).
        if self.model_cache is not None and archive_context is not None:
            for bv, cfg in completed:
                coverage = store.get_coverage(family_key, style_key, browser_version=bv, config_hash=cfg)
                if not coverage:
                    continue
                cov_fp = self._coverage_fingerprint(coverage)
                ref_fp = self._reference_fingerprint(archive_context, style.id, bv, cfg, cov_fp)
                for provenance in PROVENANCE_PROBE_ORDER:
                    mc_identity = FontModelCacheIdentity(
                        reference_fingerprint=ref_fp,
                        family_name=family_name,
                        style_id=style.id,
                        mode=mode,
                        coverage_fingerprint=cov_fp,
                        provenance=provenance,
                    )
                    model = self.model_cache.get(mc_identity)
                    if model is None:
                        continue
                    metadata = self.model_cache.get_metadata(mc_identity) or {}
                    self._trace_record(f"L2_{style.id}_{fmt}", "HIT", provenance=provenance)
                    result = Stage9DReleaseGate.execute_sync_with_model(
                        store=store,
                        config=config,
                        reference_id=family_key,
                        style_id=style_key,
                        family_name=family_name,
                        style_name=style.display_name,
                        browser_version=bv,
                        format_type=fmt,
                        model=model,
                        cached_snapshot_fingerprint=str(metadata.get("snapshot_fingerprint", "")),
                        cached_trace_hash=str(metadata.get("trace_hash", "")),
                        cached_provenance=provenance,
                        cached_ai_binding=str(metadata.get("ai_binding", "")),
                        output_dir=build_dir / "stage9d_l2" / fmt.lower(),
                        provider_capability=self._sealed_provider_capability(
                            store, family_key, style_key, bv, cfg
                        ),
                        wall_limit_seconds=wall_budget,
                    )
                    if result.is_publishable and result.attestation is not None:
                        artifact_path = Path(result.candidate_file_path)
                        font_file = GeneratedFontFile(
                            style_id=style.id,
                            style_name=style.display_name,
                            format=fmt,
                            filename=artifact_path.name,
                            file_path=artifact_path,
                            size_bytes=result.candidate_size_bytes,
                            sha256_hex=result.candidate_artifact_sha,
                        )
                        return font_file, result.attestation, provenance, str(metadata.get("ai_binding", ""))
                    self._trace_record(f"L2_{style.id}_{fmt}", "GATE_FAIL", provenance=provenance)

        # L3: authorized binary win (zero geometry reconstruction).
        # T-PRICE-01: BinaryCacheIdentity does not bind mode, so the L3
        # binary shortcut is ORIGINAL-only (ADR-0002 exact-binary-wins
        # semantics); VIETNAMESE must fall through to observable
        # reconstruction and never consume an ORIGINAL binary.
        binary = reuse_state.get("binaries", {}).get(style.id)
        if binary is not None and mode != "ORIGINAL":
            self._trace_record(f"L3_{style.id}_{fmt}", "BINARY_REUSE_REFUSED_MODE", mode=mode)
            binary = None
        if binary is not None:
            self._trace_record(f"L3_{style.id}_{fmt}", "BINARY_REUSE", provenance=binary.provenance)
            font_file = prepare_binary_artifact(
                binary, fmt, build_dir / "stage9d_binary" / fmt.lower(), family_name, style.display_name
            )
            report = BinaryConsumerValidator().validate(
                font_file, provenance=binary.provenance
            )
            if report.overall_status != "PASS":
                raise ValueError(f"STAGE9D_GATE_FAILED_{fmt}")
            attestation = self._binary_attestation(binary, report, family_key, style_key, cfg_h)
            provenance = (
                PROVENANCE_BINARY_DUMP_DOM
                if binary.provenance == BINARY_STAGE_DUMP_DOM
                else PROVENANCE_BINARY_SESSION
            )
            return font_file, attestation, provenance, ""

        # L4: exact raster observations -> full Stage 9D gate (fit-only
        # optimization + four consumers + held-out evaluation).
        if completed:
            bv, cfg = completed[0]
            coverage = store.get_coverage(family_key, style_key, browser_version=bv, config_hash=cfg)
            if coverage:
                self._trace_record(f"L4_{style.id}_{fmt}", "GATE", mode=mode)
                source_hash = self._reference_fingerprint(
                    archive_context, style.id, bv, cfg, self._coverage_fingerprint(coverage)
                ) if archive_context is not None else cfg_h
                result = Stage9DReleaseGate.execute_sync(
                    store=store,
                    config=config,
                    reference_id=family_key,
                    style_id=style_key,
                    family_name=family_name,
                    style_name=style.display_name,
                    browser_version=bv,
                    format_type=fmt,
                    output_dir=build_dir / "stage9d" / fmt.lower(),
                    mode=mode,
                    vietnamese_service=self._vietnamese_service(mode, cfg_h, source_hash),
                    provider_capability=self._sealed_provider_capability(
                        store, family_key, style_key, bv, cfg
                    ),
                    # Production flow under FAST_30, the sole
                    # reconstruction profile (ADR-0001); no
                    # fallback/escalation of any trigger type.
                    reconstruction_profile=FAST_30_PROFILE,
                    wall_limit_seconds=wall_budget,
                    # T-FAST30-A23-FIX F6: durable, stable-identity
                    # checkpoint placement (job-scoped root + snapshot
                    # identity) so an interrupted attempt resumes instead
                    # of restarting on re-claim.
                    checkpoint_root=self._durable_checkpoint_root(job.job_id),
                )
                if not result.is_publishable or result.attestation is None:
                    # FAST_30 regime: deadline or quality failure returns
                    # FAST30_FAILED and stops (no fallback/escalation).
                    raise ValueError("FAST30_FAILED")
                attestation = result.attestation
                artifact_path = Path(result.candidate_file_path)
                font_file = GeneratedFontFile(
                    style_id=style.id,
                    style_name=style.display_name,
                    format=fmt,
                    filename=artifact_path.name,
                    file_path=artifact_path,
                    size_bytes=result.candidate_size_bytes,
                    sha256_hex=result.candidate_artifact_sha,
                )
                # Promote the converged model to the L2 cache for exact reuse.
                if self.model_cache is not None and archive_context is not None and result.model is not None:
                    cov_fp = self._coverage_fingerprint(coverage)
                    ref_fp = self._reference_fingerprint(archive_context, style.id, bv, cfg, cov_fp)
                    mc_identity = FontModelCacheIdentity(
                        reference_fingerprint=ref_fp,
                        family_name=family_name,
                        style_id=style.id,
                        mode=mode,
                        coverage_fingerprint=cov_fp,
                        provenance=attestation.provenance,
                    )
                    try:
                        self.model_cache.put(
                            mc_identity,
                            result.model,
                            metadata={
                                "snapshot_fingerprint": result.snapshot_fingerprint,
                                "trace_hash": result.trace.compute_trace_hash() if result.trace else "",
                                "provenance": attestation.provenance,
                                "ai_binding": attestation.ai_binding,
                            },
                        )
                        self._trace_record(f"L2WRITE_{style.id}_{fmt}", "STORED")
                    except Exception as exc:
                        logger.warning("L2 model cache write skipped: %s", type(exc).__name__)
                return font_file, attestation, attestation.provenance, attestation.ai_binding

        raise ValueError(f"STAGE9D_GATE_FAILED_{fmt}")

    def _sync_build_validate_and_package(
        self,
        source_payload: SourcePayload | None,
        job: ClaimedJob,
        job_dir: Path,
        fenced_event: threading.Event,
        expiry_holder: list[int],
        archive_context: ArchiveSourceContext | None = None,
        cached_files: list[GeneratedFontFile] | None = None,
        reuse_state: dict[str, Any] | None = None,
        job_deadline: float | None = None,
    ) -> JobPackageManifest:
        """Build/validate/archive on a miss, or package verified archive files on a hit.

        T-FAST30-A23-FIX F1: the wall guards below are monotonic job-deadline
        checks (claim -> ACK), independent of the heartbeat-moved
        ``expiry_holder`` (kept for interface stability; no longer the wall
        source). A breach raises JobWallLimitExceeded (terminal FAST30_FAILED)
        before any further archive/package work: all-or-nothing holds.
        """
        reuse_state = reuse_state if reuse_state is not None else {}
        family_name = job.family_name or (
            source_payload.family_name if source_payload is not None else self._family_name_from_url(job.source_url)
        )
        build_dir = job_dir / "build"
        build_dir.mkdir(parents=True, exist_ok=True)

        if cached_files is not None:
            generated_files = list(cached_files)
        else:
            gated = bool(reuse_state.get("gated"))
            if not gated and source_payload is None:
                raise RuntimeError("MISSING_SOURCE_PAYLOAD")
            generated_files = []
            # Stage 9D: archive writes are deferred until every requested
            # style+format gate has PASSed, so partial PASS archives nothing.
            pending_attested: list[tuple[ArchiveIdentity, GeneratedFontFile, Any]] = []
            for style in job.styles:
                style_data = source_payload.styles.get(style.id) if source_payload is not None else None
                if not gated and not style_data:
                    raise ValueError(f"STYLE_MISSING_IN_SOURCE_{style.id}")

                for fmt in job.formats:
                    if fenced_event.is_set():
                        raise RuntimeError("LEASE_FENCED_OR_EXPIRED")
                    if (
                        job_deadline is not None
                        and time.monotonic() + WALL_SAFETY_MARGIN_MS / 1000.0 >= job_deadline
                    ):
                        raise JobWallLimitExceeded()

                    if gated:
                        font_file, attestation, provenance, ai_binding = self._tiered_resolve_artifact(
                            job=job,
                            style=style,
                            family_name=family_name,
                            archive_context=archive_context,
                            fmt=fmt,
                            build_dir=build_dir,
                            reuse_state=reuse_state,
                            job_deadline=job_deadline,
                        )
                        if not validate_font_file(font_file.file_path, fmt):
                            raise ValueError(f"GENERATED_FONT_INVALID_{fmt}")
                        if (
                            attestation is not None
                            and self.archive is not None
                            and archive_context is not None
                            and fmt in ARCHIVEABLE_FORMATS
                        ):
                            pending_attested.append(
                                (
                                    self._make_archive_identity(
                                        job,
                                        family_name,
                                        style.id,
                                        style.display_name,
                                        fmt,
                                        archive_context,
                                        provenance=provenance,
                                        ai_binding=ai_binding,
                                    ),
                                    font_file,
                                    attestation,
                                )
                            )
                        generated_files.append(font_file)
                        continue

                    font_file = self.font_builder.build_font(
                        style_data,
                        family_name,
                        fmt,
                        build_dir,
                    )

                    if not validate_font_file(font_file.file_path, fmt):
                        raise ValueError(f"GENERATED_FONT_INVALID_{fmt}")

                    if self.archive is not None and archive_context is not None and fmt in ARCHIVEABLE_FORMATS:
                        identity = self._make_archive_identity(
                            job,
                            family_name,
                            style.id,
                            style.display_name,
                            fmt,
                            archive_context,
                        )
                        try:
                            self.archive.put(identity, font_file)
                        except (OSError, sqlite3.Error) as exc:
                            raise RuntimeError("FINAL_FONT_ARCHIVE_WRITE_FAILED") from exc

                    generated_files.append(font_file)

            for identity, font_file, attestation in pending_attested:
                try:
                    self.archive.put_attested(
                        identity,
                        font_file,
                        attestation_json=json.dumps(
                            attestation.to_dict(), sort_keys=True, separators=(",", ":")
                        ),
                        attestation_hash=attestation.compute_hash(),
                    )
                except (OSError, sqlite3.Error) as exc:
                    raise RuntimeError("FINAL_FONT_ARCHIVE_WRITE_FAILED") from exc

        if not generated_files:
            raise ValueError("NO_FILES_GENERATED")

        if fenced_event.is_set():
            raise RuntimeError("LEASE_FENCED_OR_EXPIRED")
        if (
            job_deadline is not None
            and time.monotonic() + WALL_SAFETY_MARGIN_MS / 1000.0 >= job_deadline
        ):
            raise JobWallLimitExceeded()

        manifest = self.packager.package_job_output(
            job_id=job.job_id,
            order_id=job.order_id,
            family_name=family_name,
            files=generated_files,
            output_dir=job_dir,
        )

        if fenced_event.is_set():
            self.scratch_manager.cleanup_job_dir(job_dir)
            raise RuntimeError("LEASE_FENCED_OR_EXPIRED")
        if (
            job_deadline is not None
            and time.monotonic() + WALL_SAFETY_MARGIN_MS / 1000.0 >= job_deadline
        ):
            self.scratch_manager.cleanup_job_dir(job_dir)
            raise JobWallLimitExceeded()

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
        fenced_event = threading.Event()
        stop_event = threading.Event()
        expiry_holder = [job.lease_expires_at]

        # T-FAST30-A23-FIX F1: hard monotonic job wall born at claim
        # (JOB_WALL_SECONDS, default 1800s = the 30-minute production wall).
        job_deadline = time.monotonic() + float(self.settings.JOB_WALL_SECONDS)
        self._touch_progress_beacon(f"job_start:{job.job_id}")

        # Lease liveness runs on a dedicated thread so blocking compute on
        # the event loop can never starve lease extensions (Issue #90).
        heartbeat_thread = threading.Thread(
            target=self._heartbeat_thread_main,
            args=(job.job_id, job.lease_token, expiry_holder, fenced_event, stop_event),
            name=f"lease-heartbeat-{job.job_id}",
            daemon=True,
        )
        heartbeat_thread.start()

        try:
            try:
                # T-FAST30-A23-FIX F1: job-level monotonic deadline, claim ->
                # ACK. The timeout is born at claim from JOB_WALL_SECONDS and
                # is independent of the heartbeat-moved expiry_holder.
                async with asyncio.timeout(max(0.0, job_deadline - time.monotonic())):
                    return await self._execute_claimed_job(
                        msg=msg,
                        job=job,
                        job_dir=job_dir,
                        fenced_event=fenced_event,
                        stop_event=stop_event,
                        expiry_holder=expiry_holder,
                        job_deadline=job_deadline,
                        preview_input=preview_input,
                    )
            except TimeoutError:
                # Wall breach: stop the heartbeat FIRST so the edge observes
                # the fence/expiry instead of further lease extensions.
                stop_event.set()
                raise JobWallLimitExceeded() from None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            err_code = _safe_error_code(exc)
            logger.warning(
                "Error during compute execution for %s: class=%s code=%s",
                job.job_id,
                type(exc).__name__,
                err_code,
                exc_info=True,
            )

            if err_code == FENCED_ERROR_CODE:
                self.scratch_manager.cleanup_job_dir(job_dir)
                return ProcessResult(action=RunnerAction.FENCED_ABORT, job_id=job.job_id, reason="fenced")

            is_terminal = err_code in TERMINAL_ERROR_CODES

            # T-FAST30-A23-FIX F1: on a terminal FAST30_FAILED wall breach,
            # stop the heartbeat BEFORE failing so the edge observes the
            # fence/expiry instead of further lease extensions.
            if err_code == "FAST30_FAILED":
                stop_event.set()

            fail_res = await self.worker_client.fail(
                job_id=job.job_id,
                lease_token=job.lease_token,
                retryable=not is_terminal,
                reason_code=err_code,
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
            await asyncio.to_thread(
                heartbeat_thread.join, self.settings.HTTP_TIMEOUT_SECONDS + 5.0
            )
            if heartbeat_thread.is_alive():
                logger.warning(
                    "Heartbeat thread for job %s did not exit promptly after stop",
                    job.job_id,
                )

    async def _execute_claimed_job(
        self,
        msg: QueueMessage,
        job: ClaimedJob,
        job_dir: Path,
        fenced_event: threading.Event,
        stop_event: threading.Event,
        expiry_holder: list[int],
        job_deadline: float,
        preview_input: bytes | dict[str, Any] | None = None,
    ) -> ProcessResult:
        """Run Steps A-F for one claimed job inside the job-level wall.

        T-FAST30-A23-FIX F1: this entire claim->ACK body executes under the
        caller's ``asyncio.timeout`` born from the monotonic job deadline.
        Every inner guard additionally checks the same deadline so a breach
        surfaces as a deterministic JobWallLimitExceeded (terminal
        FAST30_FAILED) rather than a mid-flight cancellation whenever
        possible. All-or-nothing: after a breach nothing is uploaded,
        archived, or completed.
        """
        # Step A: tiered reuse. L1 exact archive hit -> package directly.
        # Otherwise resolve L2/L3 capabilities before any browser acquisition.
        family_name = job.family_name or self._family_name_from_url(job.source_url)
        archive_context = self._get_archive_context(job)
        self.last_reuse_trace = {"events": [], "acquisition_traces": {}}
        # D21: archive-mode truth is observable in every job report trace.
        self.last_reuse_trace["archive_mode"] = self.archive_mode.to_dict()
        cached_files = self._get_archive_hit(job, family_name, archive_context)
        reuse_state: dict[str, Any] = {"gated": False, "binaries": {}}
        if cached_files is not None:
            logger.info("Final-font archive hit for job %s (%d files)", job.job_id, len(cached_files))
            source_payload = None
        else:
            gate_store = getattr(self.source_acquirer, "store", None)
            gate_config = getattr(self.source_acquirer, "observation_config", None)
            gated = preview_input is None and gate_store is not None and gate_config is not None
            reuse_state["gated"] = gated
            if gated:
                cfg_h = gate_config.compute_hash()
                job_mode = _require_job_mode(job)
                family_envelope = None
                needs_acquisition = False
                for style in job.styles:
                    family_key, style_key = self._observation_keys(job.source_url, style.id)
                    completed = self._completed_identities(gate_store, family_key, style_key, cfg_h)
                    l2_candidate = False
                    if self.model_cache is not None and completed and archive_context is not None:
                        for bv, cfg in completed:
                            coverage = gate_store.get_coverage(
                                family_key, style_key, browser_version=bv, config_hash=cfg
                            )
                            if not coverage:
                                continue
                            cov_fp = self._coverage_fingerprint(coverage)
                            ref_fp = self._reference_fingerprint(
                                archive_context, style.id, bv, cfg, cov_fp
                            )
                            for provenance in PROVENANCE_PROBE_ORDER:
                                probe = FontModelCacheIdentity(
                                    reference_fingerprint=ref_fp,
                                    family_name=family_name,
                                    style_id=style.id,
                                    mode=job_mode,
                                    coverage_fingerprint=cov_fp,
                                    provenance=provenance,
                                )
                                if self.model_cache.get(probe) is not None:
                                    l2_candidate = True
                                    break
                            if l2_candidate:
                                break
                    if completed or l2_candidate:
                        self._trace_record(f"PREACQ_{style.id}", "SKIP_BROWSER", l2=l2_candidate)
                        continue
                    # L3 durable authorized-binary cache probe before any
                    # provider/network call. Identity binds the actual
                    # acquisition stage provenance; the compatible-reuse
                    # rule probes the deterministic provenance order.
                    # T-PRICE-01: the L3 binary shortcut is ORIGINAL-only
                    # (BinaryCacheIdentity does not bind mode).
                    l3_ref_fp = hashlib.sha256(
                        canonical_source_identity(job.source_url).encode("utf-8")
                    ).hexdigest()
                    l3_hit = None
                    if job_mode == "ORIGINAL" and self.binary_cache is not None:
                        for prov in BINARY_PROVENANCE_PROBE_ORDER:
                            l3_identity = BinaryCacheIdentity(
                                reference_fingerprint=l3_ref_fp,
                                family_name=family_name,
                                style_id=style.id,
                                provenance=prov,
                            )
                            cached_raw, cached_fmt, cached_prov, cache_status = self.binary_cache.get(
                                l3_identity
                            )
                            if cache_status == "CORRUPT":
                                raise ValueError(
                                    "ACQUISITION_BINARY_INTEGRITY_FAILED:L3_CACHE_CORRUPT"
                                )
                            if cache_status == "HIT" and cached_raw is not None:
                                l3_hit = (cached_raw, cached_fmt, cached_prov or prov)
                                break
                    if job_mode == "ORIGINAL" and l3_hit is not None:
                        cached_raw, cached_fmt, cached_prov = l3_hit
                        reuse_state["binaries"][style.id] = AcquiredBinary(
                            raw_bytes=cached_raw,
                            format=cached_fmt,
                            family_name=family_name,
                            style_name=style.display_name,
                            provenance=cached_prov,
                        )
                        self._trace_record(
                            f"PREACQ_{style.id}", "L3_CACHE_HIT", provenance=cached_prov
                        )
                        continue
                    if self.acquisition_pipeline is not None:
                        if family_envelope is None:
                            family_envelope = await self.acquisition_pipeline.acquire_family_preflight(
                                job.source_url,
                                expected_family=family_name,
                                expected_styles=job.styles,
                            )
                        outcome = await self.acquisition_pipeline.acquire(
                            job.source_url,
                            family_name,
                            style.display_name,
                            raster_request={
                                # Observable render-size passes: the closed
                                # capability's disjoint fit+held-out sizes.
                                "acs_pts": [
                                    int(r)
                                    for r in ProviderRasterCapability.deterministic_size_schedule(
                                        PROVIDER_MONOTYPE_RENDER, gate_config.resolutions
                                    ).all_sizes()
                                ]
                            },
                            family_envelope=family_envelope,
                        )
                        self.last_reuse_trace["acquisition_traces"][style.id] = (
                            outcome.trace.to_sanitized_dict()
                        )
                        if outcome.kind == "binary" and outcome.binary is not None and job_mode != "ORIGINAL":
                            # T-PRICE-01: exact-binary-wins-immediately is ORIGINAL-only
                            # (ADR-0002); VIETNAMESE must reconstruct through observable
                            # evidence and never take the binary shortcut.
                            self._trace_record(
                                f"PREACQ_{style.id}", "BINARY_WIN_REFUSED_MODE", mode=job_mode
                            )
                        if outcome.kind == "binary" and outcome.binary is not None and job_mode == "ORIGINAL":
                            if self.binary_cache is not None:
                                self.binary_cache.put(
                                    BinaryCacheIdentity(
                                        reference_fingerprint=l3_ref_fp,
                                        family_name=family_name,
                                        style_id=style.id,
                                        provenance=outcome.binary.provenance,
                                    ),
                                    outcome.binary.raw_bytes,
                                    outcome.binary.format,
                                    stage_provenance=outcome.binary.provenance,
                                )
                            reuse_state["binaries"][style.id] = outcome.binary
                            self._trace_record(
                                f"PREACQ_{style.id}", "BINARY_WIN", provenance=outcome.binary.provenance
                            )
                            continue
                        if outcome.kind == "raster_authorized" and outcome.raster_pages:
                            # Raster evidence is never discarded: the
                            # bounds-checked CDN sprite slices ARE the
                            # reconstruction pixels and are persisted
                            # directly as observations. The browser path
                            # supplements observable metrics/pairs/
                            # features only; it never recaptures rasters
                            # from the source page.
                            raster_cps = sorted({
                                int(g["code_point"])
                                for page in outcome.raster_pages
                                for g in (page.payload or {}).get("glyphs", [])
                            })
                            if not raster_cps:
                                raise ValueError("ACQUISITION_RASTER_IDENTITY_MISSING")
                            family_key, style_key = self._observation_keys(
                                job.source_url, style.id
                            )
                            supplement = await collect_browser_measurement(
                                job.source_url,
                                family_name,
                                style.display_name,
                                raster_cps,
                                gate_config,
                            )
                            # Closed capability descriptor: size axis only,
                            # deterministic disjoint fit/held-out render
                            # sizes, sealed into the collection identity.
                            # Provider identity is derived from the pages
                            # that actually produced the raster; dump/
                            # Playwright evidence is never relabeled as
                            # the Monotype CDN provider. Unknown/absent/
                            # mixed provenance fails closed (no default).
                            try:
                                raster_provider_id = resolve_raster_provider(outcome.raster_pages)
                            except ValueError:
                                raise ValueError("ACQUISITION_RASTER_PROVIDER_IDENTITY_FAILED")
                            raster_capability = ProviderRasterCapability.deterministic_size_schedule(
                                raster_provider_id, gate_config.resolutions
                            )
                            attestation = page_slice_attestation(outcome.raster_pages)
                            ingested = ingest_raster_pages(
                                gate_store,
                                gate_config,
                                family_key,
                                style_key,
                                supplement,
                                outcome.raster_pages,
                                raster_capability,
                                source_url=job.source_url,
                            )
                            raster_trace_prov = (
                                RASTER_FALLBACK_PROVENANCE
                                if raster_provider_id == PROVIDER_MONOTYPE_RENDER
                                else str((outcome.raster_pages[0].payload or {}).get("provenance", ""))
                            )
                            self._trace_record(
                                f"PREACQ_{style.id}",
                                "RASTER_HANDOFF",
                                glyphs=ingested,
                                browser_version=supplement.browser_version,
                                raster_provenance=raster_trace_prov,
                                capability_hash=raster_capability.compute_hash(),
                                capability_fit_sizes=list(raster_capability.fit_sizes),
                                capability_held_out_sizes=list(raster_capability.held_out_sizes),
                                sprite_sha256=attestation["sprite_sha256"],
                                slice_bindings=attestation["bindings"],
                            )
                            continue
                        if outcome.kind == "insufficient" and outcome.terminal_reason_code.startswith(
                            "ACQUISITION_BINARY_INTEGRITY_FAILED"
                        ):
                            raise ValueError(outcome.terminal_reason_code)
                    needs_acquisition = True
                if needs_acquisition:
                    source_payload = await self.source_acquirer.acquire_source(
                        source_url=job.source_url,
                        styles=job.styles,
                        preview_input=preview_input,
                    )
                    archive_context = source_payload.archive_context or archive_context
                else:
                    source_payload = None
                    if archive_context is None:
                        archive_context = self.source_acquirer.get_archive_context(
                            job.source_url, job.styles
                        )
            else:
                source_payload = await self.source_acquirer.acquire_source(
                    source_url=job.source_url,
                    styles=job.styles,
                    preview_input=preview_input,
                )
                archive_context = source_payload.archive_context or archive_context

        self._touch_progress_beacon("acquisition_done")

        # Step B & C: Build fonts, validate, and package in a worker thread off the event loop
        manifest = await asyncio.to_thread(
            self._sync_build_validate_and_package,
            source_payload,
            job,
            job_dir,
            fenced_event,
            expiry_holder,
            archive_context,
            cached_files,
            reuse_state,
            job_deadline,
        )
        self._touch_progress_beacon("build_done")

        # Step D: Upload ZIP artifact(s) to private R2 storage endpoint
        if fenced_event.is_set():
            raise RuntimeError("LEASE_FENCED_OR_EXPIRED")
        # T-FAST30-A23-FIX F1: never upload after the monotonic wall breach
        # (all-or-nothing).
        if time.monotonic() >= job_deadline:
            raise JobWallLimitExceeded()

        uploaded_parts: list[dict[str, Any]] = []
        parts_to_upload = manifest.parts if manifest.parts else [
            ManifestPart(
                part_index=1,
                total_parts=1,
                filename=manifest.zip_filename,
                file_path=manifest.zip_file_path,
                size_bytes=manifest.zip_size_bytes,
                sha256_hex=manifest.zip_sha256_hex,
                file_count=len(manifest.files),
            )
        ]

        for part in parts_to_upload:
            if fenced_event.is_set():
                raise RuntimeError("LEASE_FENCED_OR_EXPIRED")
            if time.monotonic() >= job_deadline:
                raise JobWallLimitExceeded()

            upload_res = await self.worker_client.upload_artifact(
                job_id=job.job_id,
                lease_token=job.lease_token,
                zip_path=part.file_path,
                sha256_hex=part.sha256_hex,
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

            uploaded_parts.append({
                "part_index": part.part_index,
                "total_parts": part.total_parts,
                "filename": part.filename,
                "artifact_key": upload_res.artifact_key,
                "artifact_size_bytes": part.size_bytes,
                "artifact_sha256": part.sha256_hex,
            })

        self._touch_progress_beacon("upload_done")

        # Step E: Fenced atomic D1 completion
        if fenced_event.is_set():
            raise RuntimeError("LEASE_FENCED_OR_EXPIRED")
        # T-FAST30-A23-FIX F1: never complete after the monotonic wall
        # breach (all-or-nothing).
        if time.monotonic() >= job_deadline:
            raise JobWallLimitExceeded()

        complete_res = await self.worker_client.complete(
            job_id=job.job_id,
            lease_token=job.lease_token,
            artifact_key=uploaded_parts[0]["artifact_key"],
            sha256_hex=uploaded_parts[0]["artifact_sha256"],
            size=uploaded_parts[0]["artifact_size_bytes"],
            parts=uploaded_parts,
        )

        # Step F: Finalize and ACK Queue boundary (BLOCK 6)
        if complete_res.success:
            # Durable completion committed to D1 -> Acknowledge Queue message
            await self.queue_client.acknowledge_messages([msg.lease_id])
            self.scratch_manager.cleanup_job_dir(job_dir)
            self._touch_progress_beacon("completed")
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

    async def process_pending_catalogs(self, max_requests: int = 1) -> int:
        """Resolve at most max_requests pending catalog request(s) per loop to protect Queue latency."""
        try:
            reqs = await self.worker_client.get_pending_catalog_requests()
        except Exception as exc:
            safe_code = _safe_error_code(exc)
            logger.warning(
                "Error checking pending catalog requests: class=%s code=%s",
                type(exc).__name__,
                safe_code,
            )
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
                safe_code = _safe_error_code(exc)
                logger.warning(
                    "Terminal failure processing catalog request %s: class=%s code=%s",
                    req.id,
                    type(exc).__name__,
                    safe_code,
                )
                await self.worker_client.fail_catalog_request(req.id, safe_code)
            except Exception as exc:
                # Transient network, 5xx, or transport error: leave retryable in D1
                safe_code = _safe_error_code(exc)
                logger.warning(
                    "Transient error processing catalog request %s: class=%s code=%s",
                    req.id,
                    type(exc).__name__,
                    safe_code,
                )
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
            # T-FAST30-A23-FIX F5: an iterating (even idle) worker IS
            # progress; the watchdog kills only when this goes stale.
            self._touch_progress_beacon("loop")
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
                safe_code = _safe_error_code(exc)
                logger.error(
                    "Error in runner loop iteration %s: class=%s code=%s",
                    iterations,
                    type(exc).__name__,
                    safe_code,
                )
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

