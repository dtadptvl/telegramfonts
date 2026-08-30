"""Stage 9D: Runner release gate.

Fail-closed integration boundary for the runner archive-miss path. For one exact
observation 4-tuple and one requested format, it:

1. Loads the immutable verified snapshot (rejecting incomplete/stale/mixed collections).
2. Partitions deterministic disjoint fit/held-out evidence.
3. Reconstructs glyphs from fit evidence only.
4. Runs the deterministic bounded fit-only optimizer (fail-closed non-convergence).
5. Assembles the canonical model, builds the candidate artifact, and attests it.
6. Produces four-consumer evidence and the authoritative fidelity report over
   held-out evidence.
7. Re-verifies on-disk artifact bytes against the attested SHA (drift guard).

Only an authentic PASS with converged optimization and matching attestation is
publishable. validate_font_file() alone is never sufficient.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from fidelity.balanced_search import SEARCH_VERSION as FAST30_SEARCH_VERSION
from fidelity.balanced_search import (
    BalancedMaxSearch,
    Fast30WallLimitError,
    GlyphCheckpointStore,
)
from fidelity.evaluator import FidelityEvaluator
from fidelity.models import FidelityReport, FidelityThresholds
from fidelity.optimizer import (
    FitOnlyGlyphOptimizer,
    OptimizationTrace,
    OptimizerNonConvergenceError,
    OptimizerNonFiniteObjectiveError,
    OptimizerPolicy,
)
from fidelity.pipeline import ObservationStoreSnapshot, partition_snapshot
from fidelity.profiles import (
    CONFIDENCE_LOW,
    CONFIDENCE_PASS,
    FAST_30_PROFILE,
    PROFILE_FAST_30,
    RETIRED_PROFILES,
    compute_profile_confidence,
    select_production_profile,
)
from fidelity.producers import (
    CandidateArtifact,
    CandidateArtifactDescriptor,
    ProductionConsumerEvidenceProducer,
)
from measurement.browser_session import find_chromium_executable
from measurement.calibration import (
    ObservationCalibrator,
    derive_multisize_derived_metrics,
)
from measurement.collector import (
    derive_multisize_kerning,
    validate_pair_size_schedule,
)
from measurement.models import ObservationConfig
from measurement.store import ObservationStore
from reconstruction.candidate_builder import MaxCandidateFontBuilder
from reconstruction.font_model import CalibratedGlyph, CanonicalFontModel, GlobalFontMetrics
from reconstruction.models import ReconstructedGlyph
from reconstruction.solver import MaxReconstructionSolver
from typography.models import TypographyDataset

logger = logging.getLogger("telegramfonts.agent.fidelity.release_gate")

STAGE9D_ATTESTATION_SCHEMA_VERSION = 1

PROVENANCE_STAGE9D_RASTER = "stage9d_raster_v1"
PROVENANCE_VIETNAMESE_AI = "vietnamese_ai_v1"
PROVENANCE_VIETNAMESE_PRESERVED = "vietnamese_preserved_v1"

# Issue #82: bounded identity-bound memo of gate model formation. The gate
# re-runs snapshot->reconstruction->optimization->calibration->assembly per
# style x format even on byte-identical verified evidence and identical
# optimizer policy; this memo reuses the formed model across such calls.
# Hits are drift-guarded fail-closed and bounded (LRU).
_MODEL_FORMATION_MEMO_MAX = 2


@dataclass(frozen=True)
class _ModelFormationEntry:
    """One memoized model-formation result (pre-Vietnamese-extension base)."""

    model: Any
    sealed: Any
    trace: OptimizationTrace
    calibrated_glyphs: tuple[tuple[int, CalibratedGlyph], ...]
    kerning_map: tuple[tuple[tuple[int, int], int], ...]
    # The versioned reconstruction profile that formed this model and
    # its deterministic confidence outcome, so memo hits never re-derive
    # confidence from anything but sealed formation truth.
    profile_name: str = PROFILE_FAST_30
    confidence_status: str = ""
    confidence_score: float = -1.0
    confidence_reasons: tuple[str, ...] = ()
    search_summary: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True)
class Stage9DAttestation:
    """Immutable Stage 9 attestation bound to one exact artifact and evidence set."""

    schema_version: int
    format: str
    artifact_sha256: str
    artifact_size_bytes: int
    reference_id: str
    style_id: str
    browser_version: str
    config_hash: str
    snapshot_fingerprint: str
    fit_set_fingerprint: str
    held_out_set_fingerprint: str
    model_hash: str
    policy_hash: str
    report_id: str
    report_hash: str
    consumer_bundle_hash: str
    optimizer_trace_hash: str
    optimizer_converged: bool
    overall_status: str
    provenance: str = PROVENANCE_STAGE9D_RASTER
    ai_binding: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "format": self.format,
            "artifact_sha256": self.artifact_sha256,
            "artifact_size_bytes": self.artifact_size_bytes,
            "reference_id": self.reference_id,
            "style_id": self.style_id,
            "browser_version": self.browser_version,
            "config_hash": self.config_hash,
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "fit_set_fingerprint": self.fit_set_fingerprint,
            "held_out_set_fingerprint": self.held_out_set_fingerprint,
            "model_hash": self.model_hash,
            "policy_hash": self.policy_hash,
            "report_id": self.report_id,
            "report_hash": self.report_hash,
            "consumer_bundle_hash": self.consumer_bundle_hash,
            "optimizer_trace_hash": self.optimizer_trace_hash,
            "optimizer_converged": self.optimizer_converged,
            "overall_status": self.overall_status,
            "provenance": self.provenance,
            "ai_binding": self.ai_binding,
        }

    @staticmethod
    def canonical_hash(payload: dict[str, Any]) -> str:
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def compute_hash(self) -> str:
        return self.canonical_hash(self.to_dict())

    @classmethod
    def from_json(cls, raw_json: str) -> Stage9DAttestation | None:
        try:
            payload = json.loads(raw_json)
            if not isinstance(payload, dict):
                return None
            return cls(
                schema_version=int(payload["schema_version"]),
                format=str(payload["format"]),
                artifact_sha256=str(payload["artifact_sha256"]),
                artifact_size_bytes=int(payload["artifact_size_bytes"]),
                reference_id=str(payload["reference_id"]),
                style_id=str(payload["style_id"]),
                browser_version=str(payload["browser_version"]),
                config_hash=str(payload["config_hash"]),
                snapshot_fingerprint=str(payload["snapshot_fingerprint"]),
                fit_set_fingerprint=str(payload["fit_set_fingerprint"]),
                held_out_set_fingerprint=str(payload["held_out_set_fingerprint"]),
                model_hash=str(payload["model_hash"]),
                policy_hash=str(payload["policy_hash"]),
                report_id=str(payload["report_id"]),
                report_hash=str(payload["report_hash"]),
                consumer_bundle_hash=str(payload["consumer_bundle_hash"]),
                optimizer_trace_hash=str(payload["optimizer_trace_hash"]),
                optimizer_converged=bool(payload["optimizer_converged"]),
                overall_status=str(payload["overall_status"]),
                provenance=str(payload.get("provenance", PROVENANCE_STAGE9D_RASTER)),
                ai_binding=str(payload.get("ai_binding", "")),
            )
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None


@dataclass(frozen=True)
class ReleaseGateResult:
    """Authoritative Stage 9D gate outcome for one style+format combination."""

    is_publishable: bool
    status: str
    family_name: str
    style_name: str
    reference_id: str
    style_id: str
    format: str
    model_hash: str
    candidate_file_path: str
    candidate_size_bytes: int
    candidate_artifact_sha: str
    snapshot_fingerprint: str = ""
    fit_set_fingerprint: str = ""
    held_out_set_fingerprint: str = ""
    report: FidelityReport | None = None
    report_hash: str = ""
    attestation: Stage9DAttestation | None = None
    trace: OptimizationTrace | None = None
    failure_reasons: tuple[str, ...] = field(default_factory=tuple)
    # Reconstruction-profile record: which versioned profile produced
    # this result and its deterministic confidence gate outcome. No
    # escalation record exists: FAST_30 has no fallback path (ADR-0001).
    reconstruction_profile: str = PROFILE_FAST_30
    confidence_status: str = ""
    confidence_score: float = -1.0
    confidence_reasons: tuple[str, ...] = field(default_factory=tuple)
    search_summary: tuple[tuple[str, Any], ...] = field(default_factory=tuple)
    model: Any = field(default=None, repr=False, compare=False)
    _temp_dir: Any = field(default=None, repr=False, compare=False)

    def cleanup(self) -> None:
        if self._temp_dir is not None:
            if hasattr(self._temp_dir, "cleanup"):
                self._temp_dir.cleanup()
            elif isinstance(self._temp_dir, (str, Path)):
                import shutil

                shutil.rmtree(str(self._temp_dir), ignore_errors=True)

    def __enter__(self) -> ReleaseGateResult:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.cleanup()


def _fast30_search_summary(formation: Any) -> tuple[tuple[str, Any], ...]:
    """Deterministic, sanitized summary of one FAST_30 ladder formation."""
    easy = sum(1 for r in formation.search_records if r.classification == "EASY")
    hard = sum(1 for r in formation.search_records if r.classification == "HARD")
    resumed = sum(1 for r in formation.search_records if r.checkpoint_resumed)
    return (
        ("search_version", FAST30_SEARCH_VERSION),
        ("glyph_count", len(formation.search_records)),
        ("easy_glyphs", easy),
        ("hard_glyphs", hard),
        ("checkpoint_resumed_glyphs", resumed),
        ("worker_count", formation.worker_count),
        ("cache_stats", tuple(sorted(formation.cache_stats.items()))),
        ("checkpoint_stats", tuple(sorted(formation.checkpoint_stats.items()))),
        ("per_glyph", tuple(
            (r.code_point, r.classification, r.tiers_executed, r.pool_size,
             repr(r.full_objective), r.stop_reason, r.checkpoint_resumed)
            for r in formation.search_records
        )),
    )


def _fail_result(
    status: str,
    snapshot: ObservationStoreSnapshot | None,
    reason: str,
    fmt: str,
    model_hash: str = "",
    trace: OptimizationTrace | None = None,
    temp_dir: Any = None,
) -> ReleaseGateResult:
    if temp_dir is not None and hasattr(temp_dir, "cleanup"):
        temp_dir.cleanup()
    return ReleaseGateResult(
        is_publishable=False,
        status=status,
        family_name=snapshot.family_name if snapshot else "",
        style_name=snapshot.style_name if snapshot else "",
        reference_id=snapshot.reference_id if snapshot else "",
        style_id=snapshot.style_id if snapshot else "",
        format=fmt,
        model_hash=model_hash,
        candidate_file_path="",
        candidate_size_bytes=0,
        candidate_artifact_sha="",
        snapshot_fingerprint=snapshot.snapshot_fingerprint if snapshot else "",
        trace=trace,
        failure_reasons=(reason,),
    )


class Stage9DReleaseGate:
    """Fail-closed Stage 9 release gate for the runner archive-miss path."""

    # Identity-bound model-formation memo (Issue #82): process-wide, bounded.
    _formation_memo_lock = threading.Lock()
    _formation_memo: "OrderedDict[Any, _ModelFormationEntry]" = OrderedDict()

    @classmethod
    def _formation_memo_get(cls, key: Any) -> "_ModelFormationEntry | None":
        with cls._formation_memo_lock:
            entry = cls._formation_memo.get(key)
            if entry is not None:
                cls._formation_memo.move_to_end(key)
            return entry

    @classmethod
    def _formation_memo_put(cls, key: Any, entry: "_ModelFormationEntry") -> None:
        with cls._formation_memo_lock:
            cls._formation_memo[key] = entry
            cls._formation_memo.move_to_end(key)
            while len(cls._formation_memo) > _MODEL_FORMATION_MEMO_MAX:
                cls._formation_memo.popitem(last=False)

    @classmethod
    def _formation_memo_drop(cls, key: Any) -> None:
        with cls._formation_memo_lock:
            cls._formation_memo.pop(key, None)

    @classmethod
    def _formation_memo_clear(cls) -> None:
        with cls._formation_memo_lock:
            cls._formation_memo.clear()


    @classmethod
    async def _execute_profiled(
        cls,
        store: ObservationStore,
        config: ObservationConfig,
        reference_id: str,
        style_id: str,
        family_name: str,
        style_name: str,
        browser_version: str,
        format_type: str,
        output_dir: str | Path | None = None,
        thresholds: FidelityThresholds | None = None,
        optimizer_policy: OptimizerPolicy | None = None,
        mode: str = "ORIGINAL",
        vietnamese_service: Any = None,
        provider_capability: Any = None,
        profile: Any = None,
        deadline: float | None = None,
        checkpoint_root: str | Path | None = None,
    ) -> ReleaseGateResult:
        if profile is None:
            profile = FAST_30_PROFILE
        clean_format = format_type.strip().upper()
        if clean_format not in ("TTF", "OTF"):
            return _fail_result("FAIL", None, "PIPELINE_ERROR: UNSUPPORTED_FORMAT", clean_format)
        # Fail-closed profile boundary: FAST_30 is the sole production
        # profile (ADR-0001). Retired/unknown profiles never enter the
        # pipeline; no fallback/escalation exists.
        if profile.name != PROFILE_FAST_30:
            reason = (
                f"PROFILE_RETIRED: {profile.name}"
                if profile.name in RETIRED_PROFILES
                else f"PROFILE_UNKNOWN: {profile.name}"
            )
            return _fail_result("FAIL", None, reason, clean_format)
        if deadline is not None and time.monotonic() > deadline:
            return _fail_result(
                "FAIL", None, "FAST30_FAILED: WALL_LIMIT_EXCEEDED", clean_format
            )

        # Host capability check (fail-closed BLOCKED, identical to Stage 9C).
        try:
            chromium_exe = find_chromium_executable()
            if not chromium_exe or not os.path.exists(chromium_exe):
                raise RuntimeError("Chromium executable unavailable")
        except Exception:
            logger.warning("Chromium capability unavailable on host; returning BLOCKED non-publishable result")
            return _fail_result("BLOCKED", None, "PIPELINE_ERROR: CHROMIUM_CAPABILITY_UNAVAILABLE", clean_format)

        # 1. Immutable verified snapshot (rejects incomplete/stale/mixed collections).
        try:
            snapshot = ObservationStoreSnapshot.load_from_store(
                store=store,
                reference_id=reference_id,
                style_id=style_id,
                family_name=family_name,
                style_name=style_name,
                config=config,
                browser_version=browser_version,
                expected_capability=provider_capability,
            )
        except Exception as exc:
            logger.error("Stage 9D snapshot load failed: %s", type(exc).__name__)
            return _fail_result("FAIL", None, "PIPELINE_ERROR: SNAPSHOT_LOAD_FAILED", clean_format)

        # 2. Deterministic disjoint fit/held-out partition.
        try:
            partition = partition_snapshot(snapshot)
        except Exception as exc:
            logger.error("Stage 9D partition failed: %s", type(exc).__name__)
            return _fail_result("FAIL", snapshot, "PIPELINE_ERROR: SNAPSHOT_PARTITION_FAILED", clean_format)

        # Provider-capability collections calibrate against the sealed fit
        # sizes only; held-out sizes stay sealed from fitting.
        capability_fit_sizes = (
            tuple(snapshot.provider_capability.fit_sizes)
            if snapshot.provider_capability is not None
            else None
        )

        # 3.-5. Fit-only reconstruction + bounded deterministic optimization +
        # calibration + canonical model assembly, under an identity-bound
        # model-formation memo (Issue #82). An exact
        # (snapshot_fingerprint, optimizer_policy) hit reuses the LIVE formed
        # model, its sealed handle and its frozen convergence trace, skipping
        # ONLY reconstruction/optimization/calibration/assembly: snapshot load,
        # partition, candidate build, four-consumer evidence, held-out
        # evaluation and attestation still run fail-closed on every call.
        effective_policy = optimizer_policy if optimizer_policy is not None else OptimizerPolicy()
        # The memo key binds the versioned reconstruction profile: models
        # formed under different profile policies are different
        # reconstruction identities and can never cross-reuse.
        memo_key = (
            snapshot.snapshot_fingerprint,
            tuple(sorted(effective_policy.to_dict().items())),
            profile.policy_hash(),
        )
        cached = cls._formation_memo_get(memo_key)

        trace: OptimizationTrace | None = None
        calibrated_glyphs: dict[int, CalibratedGlyph] = {}
        kerning_map: dict[tuple[int, int], int] = {}
        model_hash = ""
        confidence_status = ""
        confidence_score = -1.0
        confidence_reasons: tuple[str, ...] = ()
        search_summary: tuple[tuple[str, Any], ...] = ()
        if cached is not None:
            try:
                # Fail-closed drift guard on hit: the live model must still
                # hash to the sealed model hash and the sealed handle must
                # verify; any drift discards the entry and recomputes.
                if cached.model.compute_canonical_hash() != cached.sealed.model_hash:
                    raise ValueError("MEMO_MODEL_DRIFT")
                cached.sealed.verify()
            except Exception:
                logger.warning(
                    "Stage 9D model formation memo drift detected; "
                    "entry discarded, recomputing fail-closed"
                )
                cls._formation_memo_drop(memo_key)
                cached = None

        if cached is not None:
            model = cached.model
            sealed = cached.sealed
            trace = cached.trace
            calibrated_glyphs = dict(cached.calibrated_glyphs)
            kerning_map = dict(cached.kerning_map)
            model_hash = sealed.model_hash
            confidence_status = cached.confidence_status
            confidence_score = cached.confidence_score
            confidence_reasons = cached.confidence_reasons
            search_summary = cached.search_summary
            logger.info(
                "Stage 9D model formation memo hit: "
                "reconstruction/optimization/calibration/assembly skipped"
            )

        if cached is None:
            # 3. Fit-only reconstruction + 4. bounded deterministic
            # optimization under the FAST_30 profile: the deterministic
            # coarse-to-fine search ladder over the shared solver/
            # optimizer interfaces, then the confidence gate. The ladder
            # reuses compatible exact-identity intermediates (decode/
            # prepare artifacts derived purely from the sealed fit
            # observation bytes); every fit record is still prepared,
            # optimized, and gated.
            try:
                ckpt_store = None
                if checkpoint_root is not None:
                    # T-FAST30-A23-FIX F6: durable, stable-identity checkpoint
                    # placement. The caller-supplied root is scoped to the job
                    # (durable cache, not the lease-token-bound scratch dir)
                    # and this segment binds the snapshot identity, so a
                    # re-claimed attempt of the same job over identical
                    # evidence resumes instead of restarting. Load-time
                    # identity-hash validation stays fail-closed; distinct
                    # jobs never share a root.
                    ckpt_store = GlyphCheckpointStore(
                        Path(checkpoint_root) / snapshot.snapshot_fingerprint[:16],
                        profile,
                    )
                elif output_dir is not None:
                    ckpt_store = GlyphCheckpointStore(Path(output_dir), profile)
                search = BalancedMaxSearch(profile, checkpoint_store=ckpt_store)
                formation = search.form_model(
                    snapshot=snapshot,
                    partition=partition,
                    config=snapshot.config,
                    raster_provider=lambda r: snapshot.get_raster_bytes(r.cache_key),
                    units_per_em=1000,
                    deadline=deadline,
                )
                optimized_glyphs = dict(formation.optimized_glyphs)
                trace = formation.trace
                confidence = compute_profile_confidence(
                    profile, formation.confidence_evidence
                )
                confidence_status = confidence.status
                confidence_score = confidence.score
                confidence_reasons = confidence.reasons
                search_summary = _fast30_search_summary(formation)
                # Deterministic confidence gate: LOW rejects the FAST_30
                # candidate before any build/consumer work. Held-out/
                # consumer evidence never feeds this decision (fit
                # evidence only). LOW is a quality failure: the
                # production flow returns FAST30_FAILED and stops; there
                # is no fallback/escalation of any trigger type.
                if confidence.status != CONFIDENCE_PASS:
                    logger.warning(
                        "FAST_30 confidence LOW: %s",
                        ";".join(confidence.reasons) or "UNKNOWN",
                    )
                    return _fail_result(
                        "FAIL", snapshot,
                        "PIPELINE_ERROR: FAST30_CONFIDENCE_LOW",
                        clean_format, model_hash="", trace=trace,
                    )
            except Fast30WallLimitError:
                return _fail_result(
                    "FAIL", snapshot, "FAST30_FAILED: WALL_LIMIT_EXCEEDED",
                    clean_format, trace=trace,
                )
            except OptimizerNonConvergenceError:
                return _fail_result("FAIL", snapshot, "PIPELINE_ERROR: OPTIMIZER_NON_CONVERGENCE", clean_format, trace=trace)
            except OptimizerNonFiniteObjectiveError:
                return _fail_result("FAIL", snapshot, "PIPELINE_ERROR: OPTIMIZER_NON_FINITE_OBJECTIVE", clean_format, trace=trace)
            except Exception as exc:
                logger.error("Stage 9D reconstruction/optimization failed: %s", type(exc).__name__)
                return _fail_result("FAIL", snapshot, "PIPELINE_ERROR: MODEL_FITTING_FAILED", clean_format, trace=trace)
            # 5. Calibration and canonical model assembly from fit evidence only.
            try:
                calibrated_metrics = ObservationCalibrator.calibrate_all(
                    records=partition.fit_records,
                    config=snapshot.config,
                    units_per_em=1000,
                    required_resolutions=capability_fit_sizes,
                )
                calib_fp = ObservationCalibrator.compute_calibration_fingerprint(
                    records=partition.fit_records,
                    config=snapshot.config,
                    units_per_em=1000,
                    required_resolutions=capability_fit_sizes,
                )

                # Robust multi-size GLYPH metrics: derived from the sealed raw
                # per-size metric_observations (lower median across the closed
                # metric schedule). Caller-authored aggregates or absent raw
                # evidence cannot substitute; structural recomputation is the
                # only accepted path.
                expected_metric_sizes = tuple(
                    float(s) for s in snapshot.config.metric_sizes_px
                )
                multisize_derived: dict[int, dict[str, float]] = {}
                for cp in sorted(calibrated_metrics.keys()):
                    multisize_derived[cp] = derive_multisize_derived_metrics(
                        metric_observations=snapshot.metric_observations,
                        code_point=cp,
                        reference_id=snapshot.reference_id,
                        style_id=snapshot.style_id,
                        browser_version=snapshot.browser_version,
                        config_hash=snapshot.config.compute_hash(),
                        expected_sizes=expected_metric_sizes,
                    )

                calibrated_glyphs: dict[int, CalibratedGlyph] = {}
                for cp, rec_g in optimized_glyphs.items():
                    m = calibrated_metrics[cp]
                    # Override the single-anchor metrics with the robust
                    # multi-size derivation, so glyph/global/vertical truth is
                    # causally bound to the sealed raw evidence.
                    ms = multisize_derived.get(cp)
                    if ms is None:
                        raise ValueError(
                            f"MULTISIZE_METRIC_CP_MISSING: cp={cp:04X}"
                        )
                    calibrated_glyphs[cp] = CalibratedGlyph(
                        code_point=cp,
                        character=chr(cp),
                        advance_width_upem=ms["advance_width_upem"],
                        lsb_upem=ms["lsb_upem"],
                        rsb_upem=ms["rsb_upem"],
                        ascent_upem=ms["ascent_upem"],
                        descent_upem=ms["descent_upem"],
                        bounding_box_upem=rec_g.bounding_box_upem,
                        contours=rec_g.contours,
                        confidence=m.confidence,
                        observation_fingerprints=m.observation_fingerprints,
                    )

                # Robust multi-size GLOBAL and VERTICAL metrics derived from the
                # sealed raw per-size evidence (max ascent / min descent /
                # max advance / mean advance / cap from H / x from x), via the
                # per-glyph multi-size derivations above. Any drift in the sealed
                # raw rows changes these values and the sealed snapshot identity.
                glyph_advances = [g.advance_width_upem for g in calibrated_glyphs.values()]
                glyph_ascents = [g.ascent_upem for g in calibrated_glyphs.values()]
                glyph_descents = [g.descent_upem for g in calibrated_glyphs.values()]
                ascent = max(glyph_ascents) if glyph_ascents else 0.0
                descent = min(glyph_descents) if glyph_descents else 0.0
                max_adv = max(glyph_advances) if glyph_advances else 0.0
                avg_adv = float(np.mean(glyph_advances)) if glyph_advances else 0.0
                cap_h = calibrated_glyphs.get(
                    ord("H"), next(iter(calibrated_glyphs.values()))
                ).ascent_upem
                x_h = calibrated_glyphs.get(
                    ord("x"), next(iter(calibrated_glyphs.values()))
                ).ascent_upem

                global_metrics = GlobalFontMetrics(
                    units_per_em=1000,
                    ascent_upem=ascent,
                    descent_upem=descent,
                    line_gap_upem=0.0,
                    cap_height_upem=cap_h,
                    x_height_upem=x_h,
                    max_advance_width_upem=max_adv,
                    avg_char_width_upem=avg_adv,
                    underline_position_upem=-100.0,
                    underline_thickness_upem=50.0,
                )

                # Kerning truth consumed by the model: recomputed fresh from the
                # sealed raw per-size pair evidence under the exact collection
                # identity (lower-median across the closed metric schedule), the
                # same causal path as glyph/global/vertical above. Absent raw
                # rows, schedule mismatch, duplicate sizes, or drift against the
                # stored derived pair value fail closed. Caller-authored
                # aggregate values are never consumed.
                expected_pair_sizes = tuple(
                    float(s) for s in snapshot.config.metric_sizes_px
                )
                raw_pair_rows: dict[tuple[int, int], list[dict[str, Any]]] = {}
                for ps in snapshot.pair_size_observations:
                    raw_pair_rows.setdefault(
                        (int(ps["left_cp"]), int(ps["right_cp"])), []
                    ).append(dict(ps))
                kerning_map: dict[tuple[int, int], int] = {}
                for p in partition.fit_pairs:
                    if not (p.is_kerning_applied or p.inferred_kerning_upem != 0):
                        continue
                    pair_key = (int(p.left_cp), int(p.right_cp))
                    rows = raw_pair_rows.get(pair_key)
                    if not rows:
                        raise ValueError(
                            f"MULTISIZE_KERNING_NO_EVIDENCE: ({pair_key[0]},{pair_key[1]}) "
                            f"in {snapshot.reference_id}:{snapshot.style_id}"
                        )
                    validate_pair_size_schedule(rows, expected_pair_sizes)
                    recomputed = derive_multisize_kerning(rows)
                    if recomputed != int(p.inferred_kerning_upem):
                        raise ValueError(
                            f"MULTISIZE_KERNING_DRIFT: ({pair_key[0]},{pair_key[1]}) "
                            f"stored={int(p.inferred_kerning_upem)} "
                            f"recomputed={recomputed} in "
                            f"{snapshot.reference_id}:{snapshot.style_id}"
                        )
                    kerning_map[pair_key] = recomputed

                model = CanonicalFontModel(
                    schema_version="1.0.0",
                    family_name=snapshot.family_name,
                    style_name=snapshot.style_name,
                    reference_id=snapshot.reference_id,
                    style_id=snapshot.style_id,
                    metrics=global_metrics,
                    glyphs=calibrated_glyphs,
                    config_hash=snapshot.config.compute_hash(),
                    browser_version=snapshot.browser_version,
                    fit_observations_count=len(partition.fit_records),
                    calibration_fingerprint=calib_fp,
                    kerning_pairs=kerning_map,
                )
                # Deep-immutability seal BEFORE the Vietnamese/build stages:
                # the canonical model is sealed into a deeply immutable handle
                # (frozen canonical bytes + SHA-256 seal). The build, consumer
                # evidence and attestation bind this sealed model hash; any
                # post-seal mutation of the model graph is detected fail-closed
                # before use (drift guard below). TTF and OTF runs bind the
                # identical sealed hash.
                sealed = model.seal()
                model_hash = sealed.model_hash

            except Exception as exc:
                logger.error("Stage 9D model assembly failed: %s", type(exc).__name__)
                reason_code = "PIPELINE_ERROR: MODEL_FITTING_FAILED"
                message = str(exc)
                if message.startswith("VI_"):
                    reason_code = f"PIPELINE_ERROR: {message}"
                return _fail_result("FAIL", snapshot, reason_code, clean_format, model_hash=model_hash, trace=trace)
            cls._formation_memo_put(
                memo_key,
                _ModelFormationEntry(
                    model=model,
                    sealed=sealed,
                    trace=trace,
                    calibrated_glyphs=tuple(sorted(calibrated_glyphs.items())),
                    kerning_map=tuple(sorted(kerning_map.items())),
                    profile_name=profile.name,
                    confidence_status=confidence_status,
                    confidence_score=confidence_score,
                    confidence_reasons=confidence_reasons,
                    search_summary=search_summary,
                ),
            )


        if deadline is not None and time.monotonic() > deadline:
            return _fail_result(
                "FAIL", snapshot, "FAST30_FAILED: WALL_LIMIT_EXCEEDED",
                clean_format, model_hash=model_hash, trace=trace,
            )

        # VIETNAMESE extension boundary: ORIGINAL never invokes AI work.
        # The extension runs on EVERY VIETNAMESE gate call, including memo
        # hits; memoized entries always hold the pre-extension base model.
        gate_provenance = PROVENANCE_STAGE9D_RASTER
        gate_ai_binding = ""
        # Code points constructed by the provenance-attested extension
        # (deterministic + AI): they carry no fit observations by
        # construction and are exempt from fit-evidence binding only.
        extension_cps: frozenset[int] = frozenset()
        if mode.strip().upper() == "VIETNAMESE":
            try:

                if vietnamese_service is None:
                    from compute.vietnamese import VietnameseExtensionService, missing_vietnamese_codepoints

                    if missing_vietnamese_codepoints(model):
                        raise ValueError("VI_PROVIDER_REQUIRED_FOR_MISSING_COVERAGE")
                    gate_provenance = PROVENANCE_VIETNAMESE_PRESERVED
                else:
                    model, vi_binding = await vietnamese_service.extend(model)
                    from compute.vietnamese import validate_nfc_nfd_coverage

                    nfc_failures = validate_nfc_nfd_coverage(model)
                    if nfc_failures:
                        raise ValueError("VI_NFC_NFD_VALIDATION_FAILED")
                    if vi_binding.extended_codepoints:
                        gate_provenance = PROVENANCE_VIETNAMESE_AI
                        gate_ai_binding = vi_binding.compute_binding_hash()
                        extension_cps = frozenset(vi_binding.extended_codepoints)
                    else:
                        gate_provenance = PROVENANCE_VIETNAMESE_PRESERVED
                    sealed = model.seal()
                    model_hash = sealed.model_hash
            except Exception as exc:
                logger.error("Stage 9D Vietnamese extension failed: %s", type(exc).__name__)
                reason_code = "PIPELINE_ERROR: MODEL_FITTING_FAILED"
                message = str(exc)
                if message.startswith("VI_"):
                    reason_code = f"PIPELINE_ERROR: {message}"
                return _fail_result("FAIL", snapshot, reason_code, clean_format, model_hash=model_hash, trace=trace)


        # Post-attestation drift guard: the model was sealed above; any
        # mutation of the model graph between seal and use fails closed
        # before build/consumer evidence. The sealed handle itself is
        # deeply immutable (frozen canonical bytes + hash), and TTF/OTF
        # attestations bind the identical sealed model hash.
        if model.compute_canonical_hash() != sealed.model_hash:
            return _fail_result(
                "FAIL", snapshot, "PIPELINE_ERROR: SEALED_FONT_MODEL_DRIFT", clean_format, model_hash=model_hash, trace=trace
            )

        if deadline is not None and time.monotonic() > deadline:
            return _fail_result(
                "FAIL", snapshot, "FAST30_FAILED: WALL_LIMIT_EXCEEDED",
                clean_format, model_hash=model_hash, trace=trace,
            )

        # 6. Candidate build + attestation + on-disk drift re-verification.
        temp_dir_obj: Any = None
        cand_sha = ""
        cand_path = ""
        cand_size = 0
        try:
            if output_dir is not None:
                work_dir = Path(output_dir)
                work_dir.mkdir(parents=True, exist_ok=True)
            else:
                temp_dir_obj = tempfile.TemporaryDirectory(prefix="telefont_stage9d_")
                work_dir = Path(temp_dir_obj.name)

            builder = MaxCandidateFontBuilder(
                family_name=snapshot.family_name,
                style_name=snapshot.style_name,
                units_per_em=1000,
            )
            if mode.strip().upper() == "VIETNAMESE":
                # The VIETNAMESE candidate must be built from the exact
                # extended canonical model (base + deterministic + AI
                # glyphs); building from the raw optimized base glyphs
                # would leave the extension out of the cmap.
                build_glyphs: dict[int, ReconstructedGlyph] = {
                    cp: ReconstructedGlyph(
                        code_point=g.code_point,
                        character=g.character,
                        advance_width_upem=g.advance_width_upem,
                        lsb_upem=g.lsb_upem,
                        rsb_upem=g.rsb_upem,
                        ascent_upem=g.ascent_upem,
                        descent_upem=g.descent_upem,
                        contours=list(g.contours),
                        bounding_box_upem=tuple(g.bounding_box_upem),
                        reconstruction_time_ms=0.0,
                    )
                    for cp, g in model.glyphs.items()
                }
            else:
                # ORIGINAL path must serialize the identical sealed canonical
                # model graph as VIETNAMESE/L2 paths: build from
                # calibrated_glyphs (multi-size derived metrics, identical
                # geometry) so hmtx/hhea/OS2 match model.glyphs under the
                # same model hash.
                build_glyphs = {
                    cp: ReconstructedGlyph(
                        code_point=g.code_point,
                        character=g.character,
                        advance_width_upem=g.advance_width_upem,
                        lsb_upem=g.lsb_upem,
                        rsb_upem=g.rsb_upem,
                        ascent_upem=g.ascent_upem,
                        descent_upem=g.descent_upem,
                        contours=list(g.contours),
                        bounding_box_upem=tuple(g.bounding_box_upem),
                        reconstruction_time_ms=0.0,
                    )
                    for cp, g in calibrated_glyphs.items()
                }
            family_build = builder.build_candidate_family(
                glyphs=build_glyphs,
                output_dir=work_dir,
                typography=TypographyDataset(
                    family_name=snapshot.family_name,
                    style_name=snapshot.style_name,
                    units_per_em=1000,
                    kerning_pairs=kerning_map,
                    observations=list(partition.fit_pairs),
                ),
                formats=(clean_format,),
            )

            art_file = family_build.ttf if clean_format == "TTF" else family_build.otf
            if not art_file or not art_file.file_path or not Path(art_file.file_path).is_file():
                raise FileNotFoundError("Candidate font file not built successfully")

            raw_font_bytes = Path(art_file.file_path).read_bytes()
            if mode.strip().upper() == "VIETNAMESE":
                from compute.vietnamese import validate_candidate_font_bytes

                vi_build_failures = validate_candidate_font_bytes(raw_font_bytes, model)
                if vi_build_failures:
                    raise ValueError("VI_CANDIDATE_VALIDATION_FAILED")
            descriptor = CandidateArtifactDescriptor(
                file_path=str(art_file.file_path),
                expected_format=clean_format,
                expected_size_bytes=art_file.size_bytes,
                expected_sha256_hex=art_file.sha256_hex,
                raw_bytes=raw_font_bytes,
            )
            descriptor.validate()
            candidate_art = CandidateArtifact.from_descriptor(descriptor)
            cand_sha = candidate_art.sha256_hex
            cand_path = candidate_art.file_path
            cand_size = art_file.size_bytes

            # Drift guard: the exact attested bytes must still be on disk.
            reread = Path(cand_path).read_bytes()
            if len(reread) != cand_size or hashlib.sha256(reread).hexdigest() != cand_sha:
                raise ValueError("ARTIFACT_DRIFT_DETECTED")
        except Exception as exc:
            logger.error("Stage 9D candidate build/attestation failed: %s: %s", type(exc).__name__, exc)
            reason = (
                "PIPELINE_ERROR: ARTIFACT_DRIFT_DETECTED"
                if isinstance(exc, ValueError) and str(exc) == "ARTIFACT_DRIFT_DETECTED"
                else "PIPELINE_ERROR: CANDIDATE_ATTESTATION_FAILED"
            )
            return _fail_result(
                "FAIL", snapshot, reason, clean_format, model_hash=model_hash, trace=trace, temp_dir=temp_dir_obj
            )

        if deadline is not None and time.monotonic() > deadline:
            return _fail_result(
                "FAIL", snapshot, "FAST30_FAILED: WALL_LIMIT_EXCEEDED",
                clean_format, model_hash=model_hash, trace=trace,
                temp_dir=temp_dir_obj,
            )

        # 7. Four-consumer evidence + authoritative held-out gating.
        try:
            bundle = await ProductionConsumerEvidenceProducer.produce_bundle(
                descriptor=descriptor,
                model=model,
                config=snapshot.config,
                held_out_records=partition.held_out_records,
                held_out_pairs=partition.held_out_pairs,
                raster_provider=lambda r: snapshot.get_raster_bytes(r.cache_key),
                thresholds=thresholds,
            )

            report = FidelityEvaluator.evaluate(
                model=model,
                config=snapshot.config,
                fit_records=partition.fit_records,
                held_out_records=partition.held_out_records,
                fit_pairs=partition.fit_pairs,
                held_out_pairs=partition.held_out_pairs,
                consumer_bundle=bundle,
                thresholds=thresholds,
                raster_provider=lambda r: snapshot.get_raster_bytes(r.cache_key),
                required_resolutions=capability_fit_sizes,
                extension_codepoints=extension_cps,
            )
        except Exception as exc:
            logger.error(
                "Stage 9D consumer evidence/evaluation failed: %s: %s",
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            if temp_dir_obj is not None:
                temp_dir_obj.cleanup()
            return ReleaseGateResult(
                is_publishable=False,
                status="FAIL",
                family_name=snapshot.family_name,
                style_name=snapshot.style_name,
                reference_id=snapshot.reference_id,
                style_id=snapshot.style_id,
                format=clean_format,
                model_hash=model_hash,
                candidate_file_path=cand_path,
                candidate_size_bytes=cand_size,
                candidate_artifact_sha=cand_sha,
                snapshot_fingerprint=snapshot.snapshot_fingerprint,
                fit_set_fingerprint=partition.fit_set_fingerprint,
                held_out_set_fingerprint=partition.held_out_set_fingerprint,
                trace=trace,
                failure_reasons=("PIPELINE_ERROR: FIDELITY_EVALUATION_FAILED",),
                reconstruction_profile=profile.name,
                confidence_status=confidence_status,
                confidence_score=confidence_score,
                confidence_reasons=confidence_reasons,
                search_summary=search_summary,
            )

        is_pass = report.overall_status == "PASS" and trace is not None and trace.converged
        sanitized_reasons: tuple[str, ...] = ()
        if not is_pass:
            if report.overall_status != "PASS":
                sanitized_reasons = tuple(
                    r.split(":")[0] if ":" in r else r for r in report.failure_reasons
                ) or ("PIPELINE_ERROR: FIDELITY_GATE_FAILED",)
            else:
                sanitized_reasons = ("PIPELINE_ERROR: OPTIMIZER_NON_CONVERGENCE",)

        report_hash = report.compute_report_hash()
        attestation = Stage9DAttestation(
            schema_version=STAGE9D_ATTESTATION_SCHEMA_VERSION,
            format=clean_format,
            artifact_sha256=cand_sha,
            artifact_size_bytes=cand_size,
            reference_id=snapshot.reference_id,
            style_id=snapshot.style_id,
            browser_version=snapshot.browser_version,
            config_hash=snapshot.config.compute_hash(),
            snapshot_fingerprint=snapshot.snapshot_fingerprint,
            fit_set_fingerprint=partition.fit_set_fingerprint,
            held_out_set_fingerprint=partition.held_out_set_fingerprint,
            model_hash=model_hash,
            policy_hash=report.policy_hash,
            report_id=report.report_id,
            report_hash=report_hash,
            consumer_bundle_hash=report.consumer_gate.consumer_bundle_hash,
            optimizer_trace_hash=trace.compute_trace_hash() if trace else "",
            optimizer_converged=bool(trace and trace.converged),
            overall_status=report.overall_status,
            provenance=gate_provenance,
            ai_binding=gate_ai_binding,
        )

        # Publication is bounded by the wall limit: a font that crosses
        # the deadline before publication fails closed (FAST30_FAILED).
        if deadline is not None and time.monotonic() > deadline:
            return _fail_result(
                "FAIL", snapshot, "FAST30_FAILED: WALL_LIMIT_EXCEEDED",
                clean_format, model_hash=model_hash, trace=trace,
                temp_dir=temp_dir_obj,
            )

        return ReleaseGateResult(
            is_publishable=is_pass,
            status=report.overall_status,
            family_name=snapshot.family_name,
            style_name=snapshot.style_name,
            reference_id=snapshot.reference_id,
            style_id=snapshot.style_id,
            format=clean_format,
            model_hash=model_hash,
            candidate_file_path=cand_path,
            candidate_size_bytes=cand_size,
            candidate_artifact_sha=cand_sha,
            snapshot_fingerprint=snapshot.snapshot_fingerprint,
            fit_set_fingerprint=partition.fit_set_fingerprint,
            held_out_set_fingerprint=partition.held_out_set_fingerprint,
            report=report,
            report_hash=report_hash,
            attestation=attestation,
            trace=trace,
            failure_reasons=sanitized_reasons,
            reconstruction_profile=profile.name,
            confidence_status=confidence_status,
            confidence_score=confidence_score,
            confidence_reasons=confidence_reasons,
            search_summary=search_summary,
            model=model,
            _temp_dir=temp_dir_obj,
        )

    @classmethod
    async def execute(
        cls,
        store: ObservationStore,
        config: ObservationConfig,
        reference_id: str,
        style_id: str,
        family_name: str,
        style_name: str,
        browser_version: str,
        format_type: str,
        output_dir: str | Path | None = None,
        thresholds: FidelityThresholds | None = None,
        optimizer_policy: OptimizerPolicy | None = None,
        mode: str = "ORIGINAL",
        vietnamese_service: Any = None,
        provider_capability: Any = None,
        reconstruction_profile: Any = None,
        wall_limit_seconds: float | None = None,
        checkpoint_root: str | Path | None = None,
    ) -> ReleaseGateResult:
        """Stage 9D production flow under FAST_30 (ADR-0001).

        FAST_30 is the sole selectable production profile: reuse ->
        FAST_30 ladder formation -> deterministic confidence gate ->
        complete unchanged held-out + four-consumer gates -> publish.
        Low/missing/invalid confidence, any failed held-out/consumer/
        artifact/attestation/coverage/deterministic-identity gate, or
        a wall-limit overrun (30 minutes per font by default) fails
        closed and stops; the production flow surfaces FAST30_FAILED.
        No fallback/escalation of any trigger type, no profile
        bouncing, no retry, no threshold adjustment, no iterative
        fitting against held-out failures. Selection of a retired
        profile (BALANCED_MAX, FULL_MAX) fails closed with
        PROFILE_RETIRED before any work.
        """
        try:
            profile = select_production_profile(reconstruction_profile)
        except ValueError as exc:
            return _fail_result(
                "FAIL", None, str(exc), format_type.strip().upper()
            )
        limit = (
            float(wall_limit_seconds)
            if wall_limit_seconds is not None
            else float(profile.wall_limit_seconds)
        )
        deadline = time.monotonic() + max(0.0, limit)
        common: dict[str, Any] = dict(
            store=store,
            config=config,
            reference_id=reference_id,
            style_id=style_id,
            family_name=family_name,
            style_name=style_name,
            browser_version=browser_version,
            format_type=format_type,
            thresholds=thresholds,
            optimizer_policy=optimizer_policy,
            mode=mode,
            vietnamese_service=vietnamese_service,
            provider_capability=provider_capability,
        )
        return await cls._execute_profiled(
            output_dir=output_dir,
            profile=profile,
            deadline=deadline,
            checkpoint_root=checkpoint_root,
            **common,
        )

    @classmethod
    def execute_sync(
        cls,
        store: ObservationStore,
        config: ObservationConfig,
        reference_id: str,
        style_id: str,
        family_name: str,
        style_name: str,
        browser_version: str,
        format_type: str,
        output_dir: str | Path | None = None,
        thresholds: FidelityThresholds | None = None,
        optimizer_policy: OptimizerPolicy | None = None,
        mode: str = "ORIGINAL",
        vietnamese_service: Any = None,
        provider_capability: Any = None,
        reconstruction_profile: Any = None,
        wall_limit_seconds: float | None = None,
        checkpoint_root: str | Path | None = None,
    ) -> ReleaseGateResult:
        kwargs = dict(
            store=store,
            config=config,
            reference_id=reference_id,
            style_id=style_id,
            family_name=family_name,
            style_name=style_name,
            browser_version=browser_version,
            format_type=format_type,
            output_dir=output_dir,
            thresholds=thresholds,
            optimizer_policy=optimizer_policy,
            mode=mode,
            vietnamese_service=vietnamese_service,
            provider_capability=provider_capability,
            reconstruction_profile=reconstruction_profile,
            wall_limit_seconds=wall_limit_seconds,
            checkpoint_root=checkpoint_root,
        )
        return cls._run_bounded(cls.execute, kwargs, format_type, wall_limit_seconds)

    @classmethod
    def _run_bounded(
        cls,
        coro_fn: Any,
        kwargs: dict[str, Any],
        format_type: str,
        wall_limit_seconds: float | None,
    ) -> ReleaseGateResult:
        """T-FAST30-A23-FIX F2: preemptive wall at the sync gate boundary.

        The gate always runs on a dedicated executor future; when a wall
        limit is supplied the boundary is HARD: ``future.result(timeout=
        remaining_budget)`` returns FAST30_FAILED WALL_LIMIT_EXCEEDED (never
        publishable) even if the cooperative in-gate checkpoints have not
        fired (those checkpoints remain unchanged). The worker thread cannot
        be force-killed in Python; it terminates at its own cooperative
        deadline, while the caller is unblocked at the hard bound.
        """
        import concurrent.futures

        clean_format = str(format_type).strip().upper()
        limit = float(wall_limit_seconds) if wall_limit_seconds is not None else None
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(lambda: asyncio.run(coro_fn(**kwargs)))
            if limit is None:
                return future.result()
            try:
                return future.result(timeout=max(0.0, limit))
            except concurrent.futures.TimeoutError:
                return _fail_result(
                    "FAIL", None, "FAST30_FAILED: WALL_LIMIT_EXCEEDED", clean_format
                )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    @classmethod
    async def execute_with_model(
        cls,
        store: ObservationStore,
        config: ObservationConfig,
        reference_id: str,
        style_id: str,
        family_name: str,
        style_name: str,
        browser_version: str,
        format_type: str,
        model: Any,
        cached_snapshot_fingerprint: str,
        cached_trace_hash: str,
        cached_provenance: str,
        cached_ai_binding: str = "",
        output_dir: str | Path | None = None,
        thresholds: FidelityThresholds | None = None,
        provider_capability: Any = None,
        wall_limit_seconds: float | None = None,
    ) -> ReleaseGateResult:
        """L2 reuse tier: a cached canonical FontModel replaces acquisition,
        reconstruction, and optimization. Only causally replaced work is
        skipped: snapshot verification, candidate build, consumer evidence,
        held-out evaluation, and attestation still run fail-closed. The
        FAST_30 wall limit still bounds the tier (reuse never exempts a
        font from the deadline)."""
        clean_format = format_type.strip().upper()
        if clean_format not in ("TTF", "OTF"):
            return _fail_result("FAIL", None, "PIPELINE_ERROR: UNSUPPORTED_FORMAT", clean_format)
        limit = (
            float(wall_limit_seconds)
            if wall_limit_seconds is not None
            else float(FAST_30_PROFILE.wall_limit_seconds)
        )
        deadline = time.monotonic() + max(0.0, limit)

        try:
            snapshot = ObservationStoreSnapshot.load_from_store(
                store=store,
                reference_id=reference_id,
                style_id=style_id,
                family_name=family_name,
                style_name=style_name,
                config=config,
                browser_version=browser_version,
                expected_capability=provider_capability,
            )
        except Exception as exc:
            logger.error("Stage 9D L2 snapshot load failed: %s", type(exc).__name__)
            return _fail_result("FAIL", None, "PIPELINE_ERROR: SNAPSHOT_LOAD_FAILED", clean_format)

        if time.monotonic() > deadline:
            return _fail_result(
                "FAIL", snapshot, "FAST30_FAILED: WALL_LIMIT_EXCEEDED", clean_format
            )

        # The cached model is only reusable when the current evidence snapshot
        # is byte-identical to the one it was built from (fail-closed drift).
        if snapshot.snapshot_fingerprint != cached_snapshot_fingerprint:
            return _fail_result("FAIL", snapshot, "PIPELINE_ERROR: L2_SNAPSHOT_DRIFT", clean_format)
        if model.config_hash != config.compute_hash():
            return _fail_result("FAIL", snapshot, "PIPELINE_ERROR: L2_CONFIG_DRIFT", clean_format)
        if model.reference_id != reference_id or model.style_id != style_id:
            return _fail_result("FAIL", snapshot, "PIPELINE_ERROR: L2_IDENTITY_DRIFT", clean_format)

        try:
            partition = partition_snapshot(snapshot)
        except Exception as exc:
            logger.error("Stage 9D L2 partition failed: %s", type(exc).__name__)
            return _fail_result("FAIL", snapshot, "PIPELINE_ERROR: SNAPSHOT_PARTITION_FAILED", clean_format)

        model_hash = ""
        sealed = None
        try:
            # L2 reuse binds the identical sealed model identity: the cached
            # model is sealed into a deeply immutable handle; build/consumer
            # stages bind the sealed hash and any post-seal model drift
            # fails closed before use.
            sealed = model.seal()
            model_hash = sealed.model_hash
        except Exception as exc:
            logger.error("Stage 9D L2 cached model invalid: %s", type(exc).__name__)
            return _fail_result("FAIL", snapshot, "PIPELINE_ERROR: L2_MODEL_INVALID", clean_format)

        if model.compute_canonical_hash() != sealed.model_hash:
            return _fail_result(
                "FAIL", snapshot, "PIPELINE_ERROR: SEALED_FONT_MODEL_DRIFT", clean_format, model_hash=model_hash
            )

        if time.monotonic() > deadline:
            return _fail_result(
                "FAIL", snapshot, "FAST30_FAILED: WALL_LIMIT_EXCEEDED",
                clean_format, model_hash=model_hash,
            )

        temp_dir_obj: Any = None
        cand_sha = ""
        cand_path = ""
        cand_size = 0
        try:
            if output_dir is not None:
                work_dir = Path(output_dir)
                work_dir.mkdir(parents=True, exist_ok=True)
            else:
                temp_dir_obj = tempfile.TemporaryDirectory(prefix="telefont_stage9d_l2_")
                work_dir = Path(temp_dir_obj.name)

            glyphs = {
                cp: ReconstructedGlyph(
                    code_point=cp,
                    character=g.character,
                    advance_width_upem=g.advance_width_upem,
                    lsb_upem=g.lsb_upem,
                    rsb_upem=g.rsb_upem,
                    ascent_upem=g.ascent_upem,
                    descent_upem=g.descent_upem,
                    contours=list(g.contours),
                    bounding_box_upem=g.bounding_box_upem,
                )
                for cp, g in model.glyphs.items()
            }
            builder = MaxCandidateFontBuilder(
                family_name=model.family_name,
                style_name=model.style_name,
                units_per_em=model.metrics.units_per_em,
            )
            from typography.models import TypographyDataset

            family_build = builder.build_candidate_family(
                glyphs=glyphs,
                output_dir=work_dir,
                typography=TypographyDataset(
                    family_name=model.family_name,
                    style_name=model.style_name,
                    units_per_em=model.metrics.units_per_em,
                    kerning_pairs=dict(model.kerning_pairs),
                    observations=list(partition.fit_pairs),
                ),
                formats=(clean_format,),
            )
            art_file = family_build.ttf if clean_format == "TTF" else family_build.otf
            if not art_file or not art_file.file_path or not Path(art_file.file_path).is_file():
                raise FileNotFoundError("Candidate font file not built successfully")

            raw_font_bytes = Path(art_file.file_path).read_bytes()
            if cached_provenance in (PROVENANCE_VIETNAMESE_AI, PROVENANCE_VIETNAMESE_PRESERVED):
                from compute.vietnamese import validate_candidate_font_bytes

                vi_build_failures = validate_candidate_font_bytes(raw_font_bytes, model)
                if vi_build_failures:
                    raise ValueError("VI_CANDIDATE_VALIDATION_FAILED")
            descriptor = CandidateArtifactDescriptor(
                file_path=str(art_file.file_path),
                expected_format=clean_format,
                expected_size_bytes=art_file.size_bytes,
                expected_sha256_hex=art_file.sha256_hex,
                raw_bytes=raw_font_bytes,
            )
            descriptor.validate()
            candidate_art = CandidateArtifact.from_descriptor(descriptor)
            cand_sha = candidate_art.sha256_hex
            cand_path = candidate_art.file_path
            cand_size = art_file.size_bytes

            reread = Path(cand_path).read_bytes()
            if len(reread) != cand_size or hashlib.sha256(reread).hexdigest() != cand_sha:
                raise ValueError("ARTIFACT_DRIFT_DETECTED")
        except Exception as exc:
            logger.error("Stage 9D L2 candidate build failed: %s", type(exc).__name__)
            if temp_dir_obj is not None:
                temp_dir_obj.cleanup()
            return _fail_result(
                "FAIL", snapshot, "PIPELINE_ERROR: CANDIDATE_ATTESTATION_FAILED", clean_format, model_hash=model_hash
            )

        if time.monotonic() > deadline:
            return _fail_result(
                "FAIL", snapshot, "FAST30_FAILED: WALL_LIMIT_EXCEEDED",
                clean_format, model_hash=model_hash, temp_dir=temp_dir_obj,
            )

        capability_fit_sizes = (
            tuple(snapshot.provider_capability.fit_sizes)
            if snapshot.provider_capability is not None
            else None
        )
        try:
            bundle = await ProductionConsumerEvidenceProducer.produce_bundle(
                descriptor=descriptor,
                model=model,
                config=snapshot.config,
                held_out_records=partition.held_out_records,
                held_out_pairs=partition.held_out_pairs,
                raster_provider=lambda r: snapshot.get_raster_bytes(r.cache_key),
                thresholds=thresholds,
            )
            report = FidelityEvaluator.evaluate(
                model=model,
                config=snapshot.config,
                fit_records=partition.fit_records,
                held_out_records=partition.held_out_records,
                fit_pairs=partition.fit_pairs,
                held_out_pairs=partition.held_out_pairs,
                consumer_bundle=bundle,
                thresholds=thresholds,
                raster_provider=lambda r: snapshot.get_raster_bytes(r.cache_key),
                required_resolutions=capability_fit_sizes,
            )
        except Exception as exc:
            logger.error("Stage 9D L2 evaluation failed: %s", type(exc).__name__)
            if temp_dir_obj is not None:
                temp_dir_obj.cleanup()
            return _fail_result(
                "FAIL", snapshot, "PIPELINE_ERROR: FIDELITY_EVALUATION_FAILED", clean_format, model_hash=model_hash
            )

        is_pass = report.overall_status == "PASS"
        sanitized_reasons: tuple[str, ...] = ()
        if not is_pass:
            sanitized_reasons = tuple(
                r.split(":")[0] if ":" in r else r for r in report.failure_reasons
            ) or ("PIPELINE_ERROR: FIDELITY_GATE_FAILED",)

        report_hash = report.compute_report_hash()
        attestation = Stage9DAttestation(
            schema_version=STAGE9D_ATTESTATION_SCHEMA_VERSION,
            format=clean_format,
            artifact_sha256=cand_sha,
            artifact_size_bytes=cand_size,
            reference_id=snapshot.reference_id,
            style_id=snapshot.style_id,
            browser_version=snapshot.browser_version,
            config_hash=snapshot.config.compute_hash(),
            snapshot_fingerprint=snapshot.snapshot_fingerprint,
            fit_set_fingerprint=partition.fit_set_fingerprint,
            held_out_set_fingerprint=partition.held_out_set_fingerprint,
            model_hash=model_hash,
            policy_hash=report.policy_hash,
            report_id=report.report_id,
            report_hash=report_hash,
            consumer_bundle_hash=report.consumer_gate.consumer_bundle_hash,
            optimizer_trace_hash=cached_trace_hash,
            optimizer_converged=True,
            overall_status=report.overall_status,
            provenance=cached_provenance,
            ai_binding=cached_ai_binding,
        )

        if time.monotonic() > deadline:
            return _fail_result(
                "FAIL", snapshot, "FAST30_FAILED: WALL_LIMIT_EXCEEDED",
                clean_format, model_hash=model_hash, temp_dir=temp_dir_obj,
            )

        return ReleaseGateResult(
            is_publishable=is_pass,
            status=report.overall_status,
            family_name=snapshot.family_name,
            style_name=snapshot.style_name,
            reference_id=snapshot.reference_id,
            style_id=snapshot.style_id,
            format=clean_format,
            model_hash=model_hash,
            candidate_file_path=cand_path,
            candidate_size_bytes=cand_size,
            candidate_artifact_sha=cand_sha,
            snapshot_fingerprint=snapshot.snapshot_fingerprint,
            fit_set_fingerprint=partition.fit_set_fingerprint,
            held_out_set_fingerprint=partition.held_out_set_fingerprint,
            report=report,
            report_hash=report_hash,
            attestation=attestation,
            trace=None,
            failure_reasons=sanitized_reasons,
            model=model,
            _temp_dir=temp_dir_obj,
        )

    @classmethod
    def execute_sync_with_model(cls, **kwargs) -> ReleaseGateResult:
        """Synchronous wrapper around execute_with_model for runner threads.

        T-FAST30-A23-FIX F2: same preemptive hard wall as ``execute_sync``:
        the remaining job budget bounds the executor future; on timeout the
        L2 tier returns FAST30_FAILED WALL_LIMIT_EXCEEDED without publishing.
        """
        return cls._run_bounded(
            cls.execute_with_model,
            kwargs,
            str(kwargs.get("format_type", "")),
            kwargs.get("wall_limit_seconds"),
        )
