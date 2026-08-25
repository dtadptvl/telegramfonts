"""Stage 9C: Local raster-to-fidelity integration pipeline.

Provides a unified, fail-closed integration boundary connecting immutable observation
snapshots, deterministic fit/held-out partitioning, master glyph reconstruction,
canonical font model assembly, candidate artifact attestation, four-consumer evidence
production, and authoritative fidelity gating.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

from fidelity.evaluator import FidelityEvaluator, validate_consumer_gate
from fidelity.models import (
    BoundChromiumEvidence,
    BoundFontToolsEvidence,
    BoundFreeTypeEvidence,
    BoundHarfBuzzEvidence,
    ConsumerEvidenceBundle,
    FidelityReport,
    FidelityThresholds,
    ProductionProducerError,
)
from fidelity.producers import (
    CandidateArtifact,
    CandidateArtifactDescriptor,
    ProductionConsumerEvidenceProducer,
)
from measurement.browser_session import find_chromium_executable
from measurement.calibration import CalibratedGlyphMetrics, ObservationCalibrator
from measurement.models import DirectMetrics, ObservationConfig, ObservationRecord
from measurement.store import ObservationStore
from reconstruction.candidate_builder import MaxCandidateFontBuilder
from reconstruction.font_model import CalibratedGlyph, CanonicalFontModel, GlobalFontMetrics
from reconstruction.models import Contour, ReconstructedGlyph
from reconstruction.solver import MaxReconstructionSolver
from typography.models import PairKerningObservation, TypographyDataset

logger = logging.getLogger("telegramfonts.agent.fidelity.pipeline")


@dataclass(frozen=True)
class ObservationStoreSnapshot:
    """Immutable, fully identified snapshot of observations for a single font style."""

    reference_id: str
    style_id: str
    family_name: str
    style_name: str
    browser_version: str
    config: ObservationConfig
    records: tuple[ObservationRecord, ...]
    raster_bytes_map: dict[str, bytes]
    pairs: tuple[PairKerningObservation, ...]

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Enforce strict identity, non-emptiness, byte integrity, and hash stability."""
        if not self.reference_id or not isinstance(self.reference_id, str):
            raise ValueError("SNAPSHOT_VALIDATION_ERROR: reference_id cannot be empty")
        if not self.style_id or not isinstance(self.style_id, str):
            raise ValueError("SNAPSHOT_VALIDATION_ERROR: style_id cannot be empty")
        if not self.family_name or not isinstance(self.family_name, str):
            raise ValueError("SNAPSHOT_VALIDATION_ERROR: family_name cannot be empty")
        if not self.style_name or not isinstance(self.style_name, str):
            raise ValueError("SNAPSHOT_VALIDATION_ERROR: style_name cannot be empty")
        if not self.browser_version or not isinstance(self.browser_version, str):
            raise ValueError("SNAPSHOT_VALIDATION_ERROR: browser_version cannot be empty")
        if not isinstance(self.config, ObservationConfig):
            raise ValueError("SNAPSHOT_VALIDATION_ERROR: config must be an ObservationConfig instance")
        if not self.config.resolutions or self.config.upem <= 0:
            raise ValueError("SNAPSHOT_VALIDATION_ERROR: Invalid ObservationConfig parameters")

        if not self.records:
            raise ValueError("SNAPSHOT_VALIDATION_ERROR: records tuple cannot be empty")

        cfg_hash = self.config.compute_hash()
        seen_cache_keys: set[str] = set()

        for r in self.records:
            if not isinstance(r, ObservationRecord):
                raise ValueError("SNAPSHOT_VALIDATION_ERROR: records must contain ObservationRecord instances")
            if r.reference_id != self.reference_id:
                raise ValueError(
                    f"SNAPSHOT_VALIDATION_ERROR: Record reference_id '{r.reference_id}' != snapshot '{self.reference_id}'"
                )
            if r.style_id != self.style_id:
                raise ValueError(
                    f"SNAPSHOT_VALIDATION_ERROR: Record style_id '{r.style_id}' != snapshot '{self.style_id}'"
                )
            if r.browser_version != self.browser_version:
                raise ValueError(
                    f"SNAPSHOT_VALIDATION_ERROR: Record browser_version '{r.browser_version}' != snapshot '{self.browser_version}'"
                )
            if r.config_hash and r.config_hash != cfg_hash:
                raise ValueError(
                    f"SNAPSHOT_VALIDATION_ERROR: Record config_hash '{r.config_hash}' != snapshot config '{cfg_hash}'"
                )
            if r.cache_key in seen_cache_keys:
                raise ValueError(f"SNAPSHOT_VALIDATION_ERROR: Duplicate record cache_key: {r.cache_key}")
            seen_cache_keys.add(r.cache_key)

            if r.cache_key not in self.raster_bytes_map:
                raise ValueError(f"SNAPSHOT_VALIDATION_ERROR: Missing raster bytes for record: {r.cache_key}")
            png_bytes = self.raster_bytes_map[r.cache_key]
            if len(png_bytes) != r.raster_size_bytes:
                raise ValueError(
                    f"SNAPSHOT_VALIDATION_ERROR: Byte size mismatch for {r.cache_key}: {len(png_bytes)} != {r.raster_size_bytes}"
                )
            actual_sha = hashlib.sha256(png_bytes).hexdigest()
            if actual_sha != r.raster_sha256:
                raise ValueError(
                    f"SNAPSHOT_VALIDATION_ERROR: SHA-256 mismatch for {r.cache_key}: {actual_sha} != {r.raster_sha256}"
                )

        seen_pairs: set[tuple[int, int]] = set()
        for p in self.pairs:
            if not isinstance(p, PairKerningObservation):
                raise ValueError("SNAPSHOT_VALIDATION_ERROR: pairs must contain PairKerningObservation instances")
            if p.left_cp <= 0 or p.right_cp <= 0:
                raise ValueError(f"SNAPSHOT_VALIDATION_ERROR: Invalid pair code points: ({p.left_cp}, {p.right_cp})")
            if p.left_char != chr(p.left_cp) or p.right_char != chr(p.right_cp):
                raise ValueError(f"SNAPSHOT_VALIDATION_ERROR: Character drift in pair: '{p.left_char}{p.right_char}'")
            pair_key = (p.left_cp, p.right_cp)
            if pair_key in seen_pairs:
                raise ValueError(f"SNAPSHOT_VALIDATION_ERROR: Duplicate pair observation for ({p.left_cp}, {p.right_cp})")
            seen_pairs.add(pair_key)

    @classmethod
    def load_from_store(
        cls,
        store: ObservationStore,
        reference_id: str,
        style_id: str,
        family_name: str,
        style_name: str,
        config: ObservationConfig,
        browser_version: str = "chromium",
    ) -> ObservationStoreSnapshot:
        """Load and strictly validate an immutable snapshot from an ObservationStore."""
        coverage = store.get_coverage(reference_id, style_id)
        if not coverage:
            with store._get_connection() as conn:
                cur = conn.execute(
                    "SELECT DISTINCT code_point FROM observations WHERE reference_id = ? AND style_id = ? ORDER BY code_point ASC",
                    (reference_id, style_id),
                )
                coverage = [row["code_point"] for row in cur.fetchall()]

        if not coverage:
            raise ValueError(f"SNAPSHOT_LOAD_ERROR: No observations or coverage found for {reference_id}/{style_id}")

        records: list[ObservationRecord] = []
        raster_map: dict[str, bytes] = {}

        for cp in coverage:
            glyph_obs = store.get_glyph_observations(reference_id, style_id, cp)
            for rec, png_bytes in glyph_obs:
                if rec.browser_version == browser_version and (not rec.config_hash or rec.config_hash == config.compute_hash()):
                    records.append(rec)
                    raster_map[rec.cache_key] = png_bytes

        if not records:
            raise ValueError(
                f"SNAPSHOT_LOAD_ERROR: No matching observations for {reference_id}/{style_id} (browser={browser_version})"
            )

        raw_pairs = store.get_pair_observations(reference_id, style_id)
        pairs: list[PairKerningObservation] = []
        for row in raw_pairs:
            p = PairKerningObservation(
                left_cp=int(row["left_cp"]),
                right_cp=int(row["right_cp"]),
                left_char=str(row["left_char"]),
                right_char=str(row["right_char"]),
                left_advance_upem=float(row["left_advance_upem"]),
                right_advance_upem=float(row["right_advance_upem"]),
                measured_pair_advance_upem=float(row["pair_advance_upem"]),
                inferred_kerning_upem=int(row.get("inferred_kerning_upem", 0)),
                is_kerning_applied=bool(int(row.get("inferred_kerning_upem", 0)) != 0),
                provenance=str(row.get("provenance", "untrusted")),
            )
            pairs.append(p)

        return cls(
            reference_id=reference_id,
            style_id=style_id,
            family_name=family_name,
            style_name=style_name,
            browser_version=browser_version,
            config=config,
            records=tuple(records),
            raster_bytes_map=raster_map,
            pairs=tuple(pairs),
        )


@dataclass(frozen=True)
class PartitionedEvidence:
    """Disjoint, non-empty partition of fit and held-out observations and pairs."""

    fit_records: tuple[ObservationRecord, ...]
    held_out_records: tuple[ObservationRecord, ...]
    fit_pairs: tuple[PairKerningObservation, ...]
    held_out_pairs: tuple[PairKerningObservation, ...]

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Enforce strict non-emptiness, disjointness, and anti-leakage invariants."""
        if not self.fit_records:
            raise ValueError("PARTITION_ERROR: fit_records cannot be empty")
        if not self.held_out_records:
            raise ValueError("PARTITION_ERROR: held_out_records cannot be empty")

        fit_keys = {r.cache_key for r in self.fit_records}
        held_keys = {r.cache_key for r in self.held_out_records}
        key_overlap = fit_keys & held_keys
        if key_overlap:
            raise ValueError(f"PARTITION_LEAKAGE: Fit and held-out cache keys overlap: {key_overlap}")

        fit_shas = {r.raster_sha256 for r in self.fit_records}
        held_shas = {r.raster_sha256 for r in self.held_out_records}
        sha_overlap = fit_shas & held_shas
        if sha_overlap:
            raise ValueError(f"PARTITION_LEAKAGE: Fit and held-out raster SHA-256 digests overlap: {sha_overlap}")

        if not self.fit_pairs:
            raise ValueError("PARTITION_ERROR: fit_pairs cannot be empty")
        if not self.held_out_pairs:
            raise ValueError("PARTITION_ERROR: held_out_pairs cannot be empty")

        fit_pair_keys = {(p.left_cp, p.right_cp) for p in self.fit_pairs}
        held_pair_keys = {(p.left_cp, p.right_cp) for p in self.held_out_pairs}
        pair_overlap = fit_pair_keys & held_pair_keys
        if pair_overlap:
            raise ValueError(f"PARTITION_LEAKAGE: Fit and held-out typography pairs overlap: {pair_overlap}")


def partition_snapshot(snapshot: ObservationStoreSnapshot) -> PartitionedEvidence:
    """Deterministically partition snapshot observations and pairs into disjoint fit and held-out sets."""
    snapshot.validate()

    grouped: dict[int, list[ObservationRecord]] = {}
    for r in snapshot.records:
        grouped.setdefault(r.code_point, []).append(r)

    fit_recs: list[ObservationRecord] = []
    held_recs: list[ObservationRecord] = []

    for cp in sorted(grouped.keys()):
        recs = sorted(grouped[cp], key=lambda r: (r.resolution, r.subpixel_x, r.subpixel_y, r.cache_key))
        if len(recs) < 2:
            raise ValueError(
                f"PARTITION_ERROR: INSUFFICIENT_OBSERVATIONS_FOR_GLYPH_{cp}: Glyph requires at least 2 distinct observations for fit/held-out split, got {len(recs)}"
            )

        zero_phase = [r for r in recs if r.subpixel_x == 0.0 and r.subpixel_y == 0.0]
        non_zero_phase = [r for r in recs if r.subpixel_x != 0.0 or r.subpixel_y != 0.0]

        if zero_phase and non_zero_phase:
            fit_recs.extend(zero_phase)
            held_recs.extend(non_zero_phase)
        else:
            fit_recs.extend(recs[:-1])
            held_recs.append(recs[-1])

    if not snapshot.pairs or len(snapshot.pairs) < 2:
        raise ValueError(
            f"PARTITION_ERROR: INSUFFICIENT_TYPOGRAPHY_PAIRS: Snapshot requires at least 2 distinct pair observations, got {len(snapshot.pairs)}"
        )

    sorted_pairs = sorted(snapshot.pairs, key=lambda p: (p.left_cp, p.right_cp, p.provenance))
    fit_pairs = [p for i, p in enumerate(sorted_pairs) if i % 2 == 0]
    held_pairs = [p for i, p in enumerate(sorted_pairs) if i % 2 == 1]

    partition = PartitionedEvidence(
        fit_records=tuple(fit_recs),
        held_out_records=tuple(held_recs),
        fit_pairs=tuple(fit_pairs),
        held_out_pairs=tuple(held_pairs),
    )
    return partition


@dataclass(frozen=True)
class LocalFidelityPipelineResult:
    """Typed publishability decision and authoritative outcome of local integration pipeline."""

    is_publishable: bool
    status: str  # "PASS" | "FAIL" | "BLOCKED"
    family_name: str
    style_name: str
    reference_id: str
    style_id: str
    model_hash: str
    candidate_artifact_sha: str
    candidate_file_path: str
    report: FidelityReport | None
    failure_reasons: tuple[str, ...] = ()


class LocalFidelityIntegrationPipeline:
    """Authoritative local integration pipeline executing raster snapshot to verified fidelity report."""

    @classmethod
    async def execute(
        cls,
        snapshot: ObservationStoreSnapshot,
        output_dir: Path | str,
        format_type: str = "TTF",
        thresholds: FidelityThresholds | None = None,
    ) -> LocalFidelityPipelineResult:
        """Run the complete end-to-end local integration pipeline in fail-closed mode."""
        if thresholds is None:
            thresholds = FidelityThresholds()
        thresholds.validate()

        clean_format = format_type.strip().upper()
        if clean_format not in ("TTF", "OTF"):
            return LocalFidelityPipelineResult(
                is_publishable=False,
                status="FAIL",
                family_name=snapshot.family_name,
                style_name=snapshot.style_name,
                reference_id=snapshot.reference_id,
                style_id=snapshot.style_id,
                model_hash="",
                candidate_artifact_sha="",
                candidate_file_path="",
                report=None,
                failure_reasons=(f"UNSUPPORTED_FORMAT: '{format_type}'",),
            )

        # 1. Capability Preflight
        try:
            find_chromium_executable()
        except Exception as exc:
            return LocalFidelityPipelineResult(
                is_publishable=False,
                status="BLOCKED",
                family_name=snapshot.family_name,
                style_name=snapshot.style_name,
                reference_id=snapshot.reference_id,
                style_id=snapshot.style_id,
                model_hash="",
                candidate_artifact_sha="",
                candidate_file_path="",
                report=None,
                failure_reasons=(f"CHROMIUM_CAPABILITY_UNAVAILABLE: {exc}",),
            )

        # 2. Snapshot Validation & Partitioning
        try:
            snapshot.validate()
            partition = partition_snapshot(snapshot)
            partition.validate()
        except Exception as exc:
            return LocalFidelityPipelineResult(
                is_publishable=False,
                status="FAIL",
                family_name=snapshot.family_name,
                style_name=snapshot.style_name,
                reference_id=snapshot.reference_id,
                style_id=snapshot.style_id,
                model_hash="",
                candidate_artifact_sha="",
                candidate_file_path="",
                report=None,
                failure_reasons=(f"SNAPSHOT_PARTITION_FAILED: {exc}",),
            )

        # 3. Master Outline Reconstruction & Model Fitting (FIT SET ONLY)
        try:
            solver = MaxReconstructionSolver()
            reconstructed_glyphs: dict[int, ReconstructedGlyph] = {}

            grouped_fit: dict[int, list[ObservationRecord]] = {}
            for r in partition.fit_records:
                grouped_fit.setdefault(r.code_point, []).append(r)

            for cp in sorted(grouped_fit.keys()):
                glyph_fit_obs = [
                    (r, snapshot.raster_bytes_map[r.cache_key])
                    for r in grouped_fit[cp]
                ]
                reconstructed_glyphs[cp] = solver.reconstruct_glyph(glyph_fit_obs)

            calibrated_metrics = ObservationCalibrator.calibrate_all(
                records=partition.fit_records,
                config=None,
                units_per_em=1000,
            )
            calib_fp = ObservationCalibrator.compute_calibration_fingerprint(
                records=partition.fit_records,
                config=None,
                units_per_em=1000,
            )

            calibrated_glyphs: dict[int, CalibratedGlyph] = {}
            for cp, rec_g in reconstructed_glyphs.items():
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
                kerning_pairs=kerning_map,
                config_hash=snapshot.config.compute_hash(),
                browser_version=snapshot.browser_version,
                fit_observations_count=len(partition.fit_records),
                calibration_fingerprint=calib_fp,
                fit_provenance="browser_observed_multi_res",
            )
            model.validate()
            model_hash = model.compute_canonical_hash()

        except Exception as exc:
            return LocalFidelityPipelineResult(
                is_publishable=False,
                status="FAIL",
                family_name=snapshot.family_name,
                style_name=snapshot.style_name,
                reference_id=snapshot.reference_id,
                style_id=snapshot.style_id,
                model_hash="",
                candidate_artifact_sha="",
                candidate_file_path="",
                report=None,
                failure_reasons=(f"MODEL_FITTING_FAILED: {exc}",),
            )

        # 4. Candidate Font Binary Building & Attestation
        try:
            out_p = Path(output_dir)
            out_p.mkdir(parents=True, exist_ok=True)

            typo_dataset = TypographyDataset(
                family_name=snapshot.family_name,
                style_name=snapshot.style_name,
                units_per_em=1000,
                kerning_pairs=kerning_map,
                observations=list(partition.fit_pairs),
                active_kerning_pairs_count=len(kerning_map),
                total_pairs_probed=len(partition.fit_pairs),
            )
            builder = MaxCandidateFontBuilder(
                family_name=snapshot.family_name,
                style_name=snapshot.style_name,
                units_per_em=1000,
            )
            family_build = builder.build_candidate_family(
                glyphs=reconstructed_glyphs,
                output_dir=out_p,
                typography=typo_dataset,
            )

            art_file = family_build.ttf if clean_format == "TTF" else family_build.otf
            if not art_file or not art_file.file_path or not Path(art_file.file_path).is_file():
                raise FileNotFoundError(f"Candidate font file {clean_format} not built successfully")

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

        except Exception as exc:
            return LocalFidelityPipelineResult(
                is_publishable=False,
                status="FAIL",
                family_name=snapshot.family_name,
                style_name=snapshot.style_name,
                reference_id=snapshot.reference_id,
                style_id=snapshot.style_id,
                model_hash=model_hash,
                candidate_artifact_sha="",
                candidate_file_path="",
                report=None,
                failure_reasons=(f"CANDIDATE_BUILD_ATTESTATION_FAILED: {exc}",),
            )

        # 5. Production Consumer Evidence Production & Fidelity Evaluation (HELD-OUT SET ONLY)
        try:
            bundle = await ProductionConsumerEvidenceProducer.produce_bundle(
                descriptor=descriptor,
                model=model,
                config=snapshot.config,
                held_out_records=list(partition.held_out_records),
                held_out_pairs=list(partition.held_out_pairs),
                raster_provider=lambda r: snapshot.raster_bytes_map[r.cache_key],
                thresholds=thresholds,
            )

            report = FidelityEvaluator.evaluate(
                model=model,
                config=snapshot.config,
                fit_records=list(partition.fit_records),
                held_out_records=list(partition.held_out_records),
                fit_pairs=list(partition.fit_pairs),
                held_out_pairs=list(partition.held_out_pairs),
                consumer_bundle=bundle,
                raster_provider=lambda r: snapshot.raster_bytes_map[r.cache_key],
                thresholds=thresholds,
            )

        except Exception as exc:
            return LocalFidelityPipelineResult(
                is_publishable=False,
                status="FAIL",
                family_name=snapshot.family_name,
                style_name=snapshot.style_name,
                reference_id=snapshot.reference_id,
                style_id=snapshot.style_id,
                model_hash=model_hash,
                candidate_artifact_sha=candidate_art.sha256_hex,
                candidate_file_path=candidate_art.file_path,
                report=None,
                failure_reasons=(f"EVIDENCE_OR_EVALUATION_FAILED: {exc}",),
            )

        # 6. Publishability Decision
        is_pass = report.overall_status == "PASS"
        return LocalFidelityPipelineResult(
            is_publishable=is_pass,
            status=report.overall_status,
            family_name=snapshot.family_name,
            style_name=snapshot.style_name,
            reference_id=snapshot.reference_id,
            style_id=snapshot.style_id,
            model_hash=model_hash,
            candidate_artifact_sha=candidate_art.sha256_hex,
            candidate_file_path=candidate_art.file_path,
            report=report,
            failure_reasons=tuple(report.failure_reasons) if not is_pass else (),
        )

    @classmethod
    def execute_sync(
        cls,
        snapshot: ObservationStoreSnapshot,
        output_dir: Path | str,
        format_type: str = "TTF",
        thresholds: FidelityThresholds | None = None,
    ) -> LocalFidelityPipelineResult:
        """Synchronous wrapper for execute."""
        return asyncio.run(
            cls.execute(
                snapshot=snapshot,
                output_dir=output_dir,
                format_type=format_type,
                thresholds=thresholds,
            )
        )
