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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

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
from fidelity.producers import (
    CandidateArtifact,
    CandidateArtifactDescriptor,
    ProductionConsumerEvidenceProducer,
)
from measurement.browser_session import find_chromium_executable
from measurement.calibration import ObservationCalibrator
from measurement.models import ObservationConfig
from measurement.store import ObservationStore
from reconstruction.candidate_builder import MaxCandidateFontBuilder
from reconstruction.font_model import CalibratedGlyph, CanonicalFontModel, GlobalFontMetrics
from reconstruction.models import ReconstructedGlyph
from reconstruction.solver import MaxReconstructionSolver
from typography.models import TypographyDataset

logger = logging.getLogger("telegramfonts.agent.fidelity.release_gate")

STAGE9D_ATTESTATION_SCHEMA_VERSION = 1


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
    ) -> ReleaseGateResult:
        clean_format = format_type.strip().upper()
        if clean_format not in ("TTF", "OTF"):
            return _fail_result("FAIL", None, "PIPELINE_ERROR: UNSUPPORTED_FORMAT", clean_format)

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

        # 3. Fit-only reconstruction + 4. bounded deterministic optimization.
        trace: OptimizationTrace | None = None
        try:
            solver = MaxReconstructionSolver()
            fit_by_cp: dict[int, list] = {}
            for r in partition.fit_records:
                fit_by_cp.setdefault(r.code_point, []).append(r)

            reconstructed_glyphs: dict[int, ReconstructedGlyph] = {}
            for cp, cp_fit_recs in fit_by_cp.items():
                glyph_fit_obs = [(r, snapshot.get_raster_bytes(r.cache_key)) for r in cp_fit_recs]
                reconstructed_glyphs[cp] = solver.reconstruct_glyph(glyph_fit_obs)

            optimizer = FitOnlyGlyphOptimizer(policy=optimizer_policy)
            optimized_glyphs, trace = optimizer.optimize(
                glyphs=reconstructed_glyphs,
                fit_records=partition.fit_records,
                raster_provider=lambda r: snapshot.get_raster_bytes(r.cache_key),
                units_per_em=1000,
            )
        except OptimizerNonConvergenceError:
            return _fail_result("FAIL", snapshot, "PIPELINE_ERROR: OPTIMIZER_NON_CONVERGENCE", clean_format, trace=trace)
        except OptimizerNonFiniteObjectiveError:
            return _fail_result("FAIL", snapshot, "PIPELINE_ERROR: OPTIMIZER_NON_FINITE_OBJECTIVE", clean_format, trace=trace)
        except Exception as exc:
            logger.error("Stage 9D reconstruction/optimization failed: %s", type(exc).__name__)
            return _fail_result("FAIL", snapshot, "PIPELINE_ERROR: MODEL_FITTING_FAILED", clean_format, trace=trace)

        # 5. Calibration and canonical model assembly from fit evidence only.
        model_hash = ""
        try:
            calibrated_metrics = ObservationCalibrator.calibrate_all(
                records=partition.fit_records,
                config=snapshot.config,
                units_per_em=1000,
            )
            calib_fp = ObservationCalibrator.compute_calibration_fingerprint(
                records=partition.fit_records,
                config=snapshot.config,
                units_per_em=1000,
            )

            calibrated_glyphs: dict[int, CalibratedGlyph] = {}
            for cp, rec_g in optimized_glyphs.items():
                m = calibrated_metrics[cp]
                calibrated_glyphs[cp] = CalibratedGlyph(
                    code_point=cp,
                    character=chr(cp),
                    advance_width_upem=m.advance_width_upem,
                    lsb_upem=m.lsb_upem,
                    rsb_upem=m.rsb_upem,
                    ascent_upem=m.ascent_upem,
                    descent_upem=m.descent_upem,
                    bounding_box_upem=rec_g.bounding_box_upem,
                    contours=rec_g.contours,
                    confidence=m.confidence,
                    observation_fingerprints=m.observation_fingerprints,
                )

            ascent = float(max(g.ascent_upem for g in calibrated_glyphs.values()))
            descent = float(min(g.descent_upem for g in calibrated_glyphs.values()))
            max_adv = float(max(g.advance_width_upem for g in calibrated_glyphs.values()))
            avg_adv = float(np.mean([g.advance_width_upem for g in calibrated_glyphs.values()]))
            cap_h = calibrated_glyphs.get(ord("H"), next(iter(calibrated_glyphs.values()))).ascent_upem
            x_h = calibrated_glyphs.get(ord("x"), next(iter(calibrated_glyphs.values()))).ascent_upem

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

            kerning_map = {
                (p.left_cp, p.right_cp): int(p.inferred_kerning_upem)
                for p in partition.fit_pairs
                if p.is_kerning_applied or p.inferred_kerning_upem != 0
            }

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
            model_hash = model.compute_canonical_hash()
        except Exception as exc:
            logger.error("Stage 9D model assembly failed: %s", type(exc).__name__)
            return _fail_result("FAIL", snapshot, "PIPELINE_ERROR: MODEL_FITTING_FAILED", clean_format, model_hash=model_hash, trace=trace)

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
            family_build = builder.build_candidate_family(
                glyphs=optimized_glyphs,
                output_dir=work_dir,
                typography=TypographyDataset(
                    family_name=snapshot.family_name,
                    style_name=snapshot.style_name,
                    units_per_em=1000,
                    kerning_pairs=kerning_map,
                    observations=list(partition.fit_pairs),
                ),
            )

            art_file = family_build.ttf if clean_format == "TTF" else family_build.otf
            if not art_file or not art_file.file_path or not Path(art_file.file_path).is_file():
                raise FileNotFoundError("Candidate font file not built successfully")

            raw_font_bytes = Path(art_file.file_path).read_bytes()
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
            logger.error("Stage 9D candidate build/attestation failed: %s", type(exc).__name__)
            reason = (
                "PIPELINE_ERROR: ARTIFACT_DRIFT_DETECTED"
                if isinstance(exc, ValueError) and str(exc) == "ARTIFACT_DRIFT_DETECTED"
                else "PIPELINE_ERROR: CANDIDATE_ATTESTATION_FAILED"
            )
            return _fail_result(
                "FAIL", snapshot, reason, clean_format, model_hash=model_hash, trace=trace, temp_dir=temp_dir_obj
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
            )
        except Exception as exc:
            logger.error("Stage 9D consumer evidence/evaluation failed: %s", type(exc).__name__)
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
            _temp_dir=temp_dir_obj,
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
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(lambda: asyncio.run(cls.execute(**kwargs)))
                return future.result()
        return asyncio.run(cls.execute(**kwargs))
