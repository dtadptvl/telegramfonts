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
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

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
    """Immutable, cryptographic snapshot of observations, pairs, and raster bytes."""

    reference_id: str
    style_id: str
    family_name: str
    style_name: str
    browser_version: str
    config: ObservationConfig
    records: tuple[ObservationRecord, ...]
    raster_bytes_map: Mapping[str, bytes]
    pairs: tuple[PairKerningObservation, ...]
    snapshot_fingerprint: str = ""

    def __post_init__(self) -> None:
        # Wrap raster_bytes_map in read-only mapping proxy to guarantee deep immutability
        if not isinstance(self.raster_bytes_map, types.MappingProxyType):
            object.__setattr__(
                self,
                "raster_bytes_map",
                types.MappingProxyType(dict(self.raster_bytes_map)),
            )

        # Validate structural integrity and cryptographic identity
        self.validate()

        # Compute and bind snapshot fingerprint
        computed_fp = self._compute_fingerprint()
        if self.snapshot_fingerprint and self.snapshot_fingerprint != computed_fp:
            raise ValueError(
                f"SNAPSHOT_VALIDATION_ERROR: Supplied snapshot_fingerprint '{self.snapshot_fingerprint}' != computed '{computed_fp}'"
            )
        if not self.snapshot_fingerprint:
            object.__setattr__(self, "snapshot_fingerprint", computed_fp)

    def _compute_fingerprint(self) -> str:
        """Compute authoritative deterministic SHA-256 fingerprint for this snapshot."""
        cfg_hash = self.config.compute_hash() if self.config else ""
        payload = {
            "reference_id": self.reference_id,
            "style_id": self.style_id,
            "family_name": self.family_name,
            "style_name": self.style_name,
            "browser_version": self.browser_version,
            "config_hash": cfg_hash,
            "records": sorted([r.to_dict() for r in self.records], key=lambda x: x["cache_key"]),
            "pairs": sorted([p.to_dict() for p in self.pairs], key=lambda x: (x["left_cp"], x["right_cp"])),
            "raster_hashes": sorted(
                [(k, hashlib.sha256(v).hexdigest()) for k, v in self.raster_bytes_map.items()]
            ),
        }
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def get_raster_bytes(self, cache_key: str) -> bytes:
        """Safe read-only accessor for raster image bytes."""
        if cache_key not in self.raster_bytes_map:
            raise KeyError(f"Cache key '{cache_key}' not found in snapshot raster map")
        return self.raster_bytes_map[cache_key]

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
            if r.config_hash != cfg_hash:
                raise ValueError(
                    f"SNAPSHOT_VALIDATION_ERROR: Record config_hash '{r.config_hash}' != snapshot config '{cfg_hash}'"
                )
            if not r.validate_cache_key():
                raise ValueError(
                    f"SNAPSHOT_VALIDATION_ERROR: Record cache_key '{r.cache_key}' failed deterministic validation"
                )
            if r.cache_key in seen_cache_keys:
                raise ValueError(
                    f"SNAPSHOT_VALIDATION_ERROR: Duplicate record cache_key detected: '{r.cache_key}'"
                )
            seen_cache_keys.add(r.cache_key)

            # Validate raster presence, byte count, and SHA256
            if r.cache_key not in self.raster_bytes_map:
                raise ValueError(
                    f"SNAPSHOT_VALIDATION_ERROR: Missing raster bytes for record cache_key '{r.cache_key}'"
                )
            png_bytes = self.raster_bytes_map[r.cache_key]
            if not isinstance(png_bytes, (bytes, bytearray)):
                raise ValueError(
                    f"SNAPSHOT_VALIDATION_ERROR: Raster for '{r.cache_key}' is not bytes"
                )
            if len(png_bytes) != r.raster_size_bytes:
                raise ValueError(
                    f"SNAPSHOT_VALIDATION_ERROR: Raster size mismatch for '{r.cache_key}': expected {r.raster_size_bytes}, got {len(png_bytes)}"
                )
            actual_sha = hashlib.sha256(png_bytes).hexdigest()
            if actual_sha != r.raster_sha256:
                raise ValueError(
                    f"SNAPSHOT_VALIDATION_ERROR: Raster SHA256 mismatch for '{r.cache_key}': expected {r.raster_sha256}, got {actual_sha}"
                )

        if len(self.raster_bytes_map) != len(seen_cache_keys):
            raise ValueError(
                f"SNAPSHOT_VALIDATION_ERROR: raster_bytes_map contains {len(self.raster_bytes_map)} entries but records have {len(seen_cache_keys)}"
            )

        # Validate typography pair observations
        seen_pairs: set[tuple[int, int]] = set()
        for p in self.pairs:
            if not isinstance(p, PairKerningObservation):
                raise ValueError("SNAPSHOT_VALIDATION_ERROR: pairs must contain PairKerningObservation instances")
            if p.reference_id != self.reference_id:
                raise ValueError(
                    f"SNAPSHOT_VALIDATION_ERROR: Pair reference_id '{p.reference_id}' != snapshot '{self.reference_id}'"
                )
            if p.style_id != self.style_id:
                raise ValueError(
                    f"SNAPSHOT_VALIDATION_ERROR: Pair style_id '{p.style_id}' != snapshot '{self.style_id}'"
                )
            if p.browser_version != self.browser_version:
                raise ValueError(
                    f"SNAPSHOT_VALIDATION_ERROR: Pair browser_version '{p.browser_version}' != snapshot '{self.browser_version}'"
                )
            if p.config_hash != cfg_hash:
                raise ValueError(
                    f"SNAPSHOT_VALIDATION_ERROR: Pair config_hash '{p.config_hash}' != snapshot '{cfg_hash}'"
                )
            pair_key = (p.left_cp, p.right_cp)
            if pair_key in seen_pairs:
                raise ValueError(f"SNAPSHOT_VALIDATION_ERROR: Duplicate pair observation: {pair_key}")
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
        browser_version: str,
    ) -> ObservationStoreSnapshot:
        """Atomically load an immutable snapshot from ObservationStore SQLite index and disk rasters."""
        cfg_hash = config.compute_hash()

        with store._get_connection() as conn:
            # 1. Require completed and verified source collection marker
            if not store.is_source_collection_completed(
                reference_id=reference_id,
                style_id=style_id,
                config_hash=cfg_hash,
                browser_version=browser_version,
            ):
                raise ValueError(
                    f"STORE_LOAD_ERROR: Incomplete or unverified source collection for {reference_id}/{style_id} "
                    f"matching browser '{browser_version}' and config '{cfg_hash}'"
                )

            # 2. Query Unicode coverage
            cov_rows = conn.execute(
                """
                SELECT code_point FROM unicode_coverage
                WHERE reference_id = ? AND style_id = ?
                ORDER BY code_point ASC
                """,
                (reference_id, style_id),
            ).fetchall()
            coverage_cps = [int(r["code_point"]) for r in cov_rows]
            if not coverage_cps:
                raise ValueError(
                    f"STORE_LOAD_ERROR: No Unicode coverage found for {reference_id}/{style_id}"
                )

            # 3. Query all matching observation records
            obs_rows = conn.execute(
                """
                SELECT * FROM observations
                WHERE reference_id = ? AND style_id = ? AND browser_version = ? AND config_hash = ?
                ORDER BY code_point ASC, resolution ASC, subpixel_x ASC, subpixel_y ASC
                """,
                (reference_id, style_id, browser_version, cfg_hash),
            ).fetchall()

            if not obs_rows:
                raise ValueError(
                    f"STORE_LOAD_ERROR: No observations found for {reference_id}/{style_id} matching browser {browser_version} and config {cfg_hash}"
                )

            observed_cps = sorted(set(int(r["code_point"]) for r in obs_rows))
            if set(coverage_cps) != set(observed_cps):
                raise ValueError(
                    f"STORE_LOAD_ERROR: Declared coverage ({len(coverage_cps)} glyphs) does not match observed glyphs ({len(observed_cps)} glyphs)"
                )

            # 3. Read rasters from disk within store boundary
            records: list[ObservationRecord] = []
            raster_map: dict[str, bytes] = {}

            for row in obs_rows:
                r_dict = dict(row)
                m = DirectMetrics(
                    code_point=r_dict["code_point"],
                    character=chr(r_dict["code_point"]),
                    font_size_px=float(r_dict["resolution"]) * 0.72,
                    raw_advance_width=float(r_dict["advance_width_px"]),
                    raw_actual_left=float(r_dict["lsb_px"]),
                    raw_actual_right=float(r_dict["advance_width_px"]) - float(r_dict["rsb_px"]),
                    raw_actual_ascent=float(r_dict["ascent_px"]),
                    raw_actual_descent=-float(r_dict["descent_px"]),
                    raw_font_ascent=float(r_dict["ascent_px"]),
                    raw_font_descent=-float(r_dict["descent_px"]),
                    advance_width_upem=float(r_dict["advance_width_upem"]),
                    lsb_upem=float(r_dict["lsb_upem"]),
                    rsb_upem=float(r_dict["rsb_upem"]),
                    ascent_upem=float(r_dict["ascent_upem"]),
                    descent_upem=float(r_dict["descent_upem"]),
                    bbox_width_upem=float(r_dict["bbox_width_upem"]),
                    bbox_height_upem=float(r_dict["bbox_height_upem"]),
                    sample_count=int(r_dict["sample_count"]),
                    confidence=float(r_dict["confidence"]),
                )
                rec = ObservationRecord(
                    cache_key=r_dict["cache_key"],
                    reference_id=r_dict["reference_id"],
                    style_id=r_dict["style_id"],
                    code_point=r_dict["code_point"],
                    resolution=r_dict["resolution"],
                    subpixel_x=float(r_dict["subpixel_x"]),
                    subpixel_y=float(r_dict["subpixel_y"]),
                    raster_relative_path=r_dict["raster_relative_path"],
                    raster_sha256=r_dict["raster_sha256"],
                    raster_size_bytes=int(r_dict["raster_size_bytes"]),
                    metrics=m,
                    created_at=r_dict["created_at"],
                    browser_version=r_dict["browser_version"],
                    config_hash=r_dict["config_hash"],
                )
                records.append(rec)

                # Read raster file from disk
                png_path = store.base_dir / rec.raster_relative_path
                if not png_path.is_file():
                    raise ValueError(
                        f"STORE_LOAD_ERROR: Missing raster file on disk: {png_path}"
                    )
                png_bytes = png_path.read_bytes()
                if len(png_bytes) != rec.raster_size_bytes:
                    raise ValueError(
                        f"STORE_LOAD_ERROR: Disk raster size mismatch for {rec.cache_key}: expected {rec.raster_size_bytes}, got {len(png_bytes)}"
                    )
                actual_sha = hashlib.sha256(png_bytes).hexdigest()
                if actual_sha != rec.raster_sha256:
                    raise ValueError(
                        f"STORE_LOAD_ERROR: Disk raster SHA256 mismatch for {rec.cache_key}: expected {rec.raster_sha256}, got {actual_sha}"
                    )
                raster_map[rec.cache_key] = png_bytes

            # 4. Query pair observations matching snapshot identity
            pair_rows = conn.execute(
                """
                SELECT * FROM pair_observations
                WHERE reference_id = ? AND style_id = ?
                  AND (browser_version = ? OR browser_version = '')
                  AND (config_hash = ? OR config_hash = '')
                ORDER BY left_cp ASC, right_cp ASC
                """,
                (reference_id, style_id, browser_version, cfg_hash),
            ).fetchall()

            pairs: list[PairKerningObservation] = []
            for p_row in pair_rows:
                p_dict = dict(p_row)
                pair_obs = PairKerningObservation(
                    left_cp=int(p_dict["left_cp"]),
                    right_cp=int(p_dict["right_cp"]),
                    left_char=str(p_dict["left_char"]),
                    right_char=str(p_dict["right_char"]),
                    left_advance_upem=float(p_dict["left_advance_upem"]),
                    right_advance_upem=float(p_dict["right_advance_upem"]),
                    measured_pair_advance_upem=float(p_dict["pair_advance_upem"]),
                    inferred_kerning_upem=int(p_dict["inferred_kerning_upem"]),
                    is_kerning_applied=(int(p_dict["inferred_kerning_upem"]) != 0),
                    confidence=float(p_dict.get("confidence", 1.0)),
                    provenance=str(p_dict.get("provenance", "untrusted")),
                    reference_id=reference_id,
                    style_id=style_id,
                    browser_version=browser_version,
                    config_hash=cfg_hash,
                )
                pairs.append(pair_obs)

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
    """Deterministically partitioned fit and held-out evidence sets."""

    fit_records: tuple[ObservationRecord, ...]
    held_out_records: tuple[ObservationRecord, ...]
    fit_pairs: tuple[PairKerningObservation, ...]
    held_out_pairs: tuple[PairKerningObservation, ...]
    fit_set_fingerprint: str
    held_out_set_fingerprint: str


def partition_snapshot(snapshot: ObservationStoreSnapshot) -> PartitionedEvidence:
    """Deterministically partition snapshot observations into strictly disjoint fit and held-out sets.
    
    The fit set satisfies the exact active adaptive schedule for snapshot.config.
    Held-out evidence remains strictly disjoint, non-empty, and untouched by model fitting.
    """
    snapshot.validate()

    # Group records by code point
    by_cp: dict[int, list[ObservationRecord]] = {}
    for r in snapshot.records:
        by_cp.setdefault(r.code_point, []).append(r)

    fit_records_list: list[ObservationRecord] = []
    held_out_records_list: list[ObservationRecord] = []

    for cp, cp_recs in sorted(by_cp.items()):
        # Determine the exact active schedule required for fitting
        first_m = cp_recs[0].metrics
        expected_phases = set(snapshot.config.get_phases_for_metrics(first_m))

        required_fit_keys = {
            (res, round(px, 4), round(py, 4))
            for res in snapshot.config.resolutions
            for px, py in expected_phases
        }

        cp_fit: list[ObservationRecord] = []
        cp_held_out: list[ObservationRecord] = []

        for r in cp_recs:
            r_key = (r.resolution, round(r.subpixel_x, 4), round(r.subpixel_y, 4))
            if r_key in required_fit_keys:
                cp_fit.append(r)
            else:
                cp_held_out.append(r)

        # Verify that all required fit schedule keys are present
        present_fit_keys = {
            (r.resolution, round(r.subpixel_x, 4), round(r.subpixel_y, 4))
            for r in cp_fit
        }
        missing_fit_keys = required_fit_keys - present_fit_keys
        if missing_fit_keys:
            sample_missing = next(iter(missing_fit_keys))
            raise ValueError(
                f"INSUFFICIENT_FIT_OBSERVATIONS: Missing required adaptive phase ({sample_missing[1]:.4f}, {sample_missing[2]:.4f}) at resolution {sample_missing[0]} for CP {cp}"
            )

        # Verify that held-out observations exist and are non-empty for this glyph
        if not cp_held_out:
            raise ValueError(
                f"ZERO_HELD_OUT_OBSERVATIONS: CP {cp} has no disjoint held-out observations for evaluation"
            )

        fit_records_list.extend(cp_fit)
        held_out_records_list.extend(cp_held_out)

    # Partition typography pair observations deterministically
    sorted_pairs = sorted(snapshot.pairs, key=lambda p: (p.left_cp, p.right_cp))
    if len(sorted_pairs) < 2:
        raise ValueError(
            f"INSUFFICIENT_PAIRS_FOR_PARTITION: Snapshot must contain at least 2 pair observations to form disjoint fit/held-out sets, got {len(sorted_pairs)}"
        )

    # Alternating assignment gives deterministic, balanced fit and held-out pairs
    fit_pairs = tuple(sorted_pairs[0::2])
    held_out_pairs = tuple(sorted_pairs[1::2])

    if not fit_pairs or not held_out_pairs:
        raise ValueError("PARTITION_ERROR: Pair partition yielded an empty fit or held-out set")

    # Strict Anti-Leakage Verification
    fit_keys = set(r.cache_key for r in fit_records_list)
    held_out_keys = set(r.cache_key for r in held_out_records_list)
    key_overlap = fit_keys & held_out_keys
    if key_overlap:
        raise ValueError(f"LEAKAGE_DETECTED: Cache key overlap between fit and held-out sets: {key_overlap}")

    fit_shas = set(r.raster_sha256 for r in fit_records_list)
    held_out_shas = set(r.raster_sha256 for r in held_out_records_list)
    sha_overlap = fit_shas & held_out_shas
    if sha_overlap:
        raise ValueError(f"LEAKAGE_DETECTED: Raster SHA256 overlap between fit and held-out sets: {sha_overlap}")

    fit_pair_tuples = set((p.left_cp, p.right_cp) for p in fit_pairs)
    held_out_pair_tuples = set((p.left_cp, p.right_cp) for p in held_out_pairs)
    pair_overlap = fit_pair_tuples & held_out_pair_tuples
    if pair_overlap:
        raise ValueError(f"LEAKAGE_DETECTED: Typography pair overlap between fit and held-out sets: {pair_overlap}")

    # Compute deterministic fingerprints for fit and held-out partitions
    fit_fp_payload = sorted(list(fit_keys))
    fit_fp = hashlib.sha256(json.dumps(fit_fp_payload).encode("utf-8")).hexdigest()

    held_fp_payload = sorted(list(held_out_keys))
    held_fp = hashlib.sha256(json.dumps(held_fp_payload).encode("utf-8")).hexdigest()

    return PartitionedEvidence(
        fit_records=tuple(fit_records_list),
        held_out_records=tuple(held_out_records_list),
        fit_pairs=fit_pairs,
        held_out_pairs=held_out_pairs,
        fit_set_fingerprint=fit_fp,
        held_out_set_fingerprint=held_fp,
    )


@dataclass(frozen=True)
class LocalFidelityPipelineResult:
    """Authoritative result returned by the Local Fidelity Integration Pipeline."""

    is_publishable: bool
    status: str
    family_name: str
    style_name: str
    reference_id: str
    style_id: str
    model_hash: str
    candidate_artifact_sha: str
    candidate_file_path: str
    report: FidelityReport | None = None
    failure_reasons: tuple[str, ...] = field(default_factory=tuple)


class LocalFidelityIntegrationPipeline:
    """Local library boundary orchestrating raster-to-fidelity model fitting and authoritative gating."""

    @classmethod
    async def execute(
        cls,
        snapshot: ObservationStoreSnapshot,
        thresholds: FidelityThresholds | None = None,
        output_dir: str | Path | None = None,
        format_type: str = "TTF",
    ) -> LocalFidelityPipelineResult:
        """Asynchronously execute the Stage 9C local raster-to-fidelity pipeline."""
        clean_format = format_type.strip().upper()
        if clean_format not in ("TTF", "OTF"):
            return LocalFidelityPipelineResult(
                is_publishable=False,
                status="FAIL",
                family_name=snapshot.family_name if snapshot else "",
                style_name=snapshot.style_name if snapshot else "",
                reference_id=snapshot.reference_id if snapshot else "",
                style_id=snapshot.style_id if snapshot else "",
                model_hash="",
                candidate_artifact_sha="",
                candidate_file_path="",
                report=None,
                failure_reasons=("PIPELINE_ERROR: UNSUPPORTED_FORMAT",),
            )

        # Verify host capabilities (Chromium, FreeType, HarfBuzz, FontTools)
        try:
            chromium_exe = find_chromium_executable()
            if not chromium_exe or not os.path.exists(chromium_exe):
                raise RuntimeError("Chromium executable unavailable")
        except Exception:
            logger.warning("Chromium capability unavailable on host; returning BLOCKED non-publishable result")
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
                failure_reasons=("PIPELINE_ERROR: CHROMIUM_CAPABILITY_UNAVAILABLE",),
            )

        # 1. Deterministic Fit / Held-Out Partitioning
        try:
            partition = partition_snapshot(snapshot)
        except Exception as exc:
            logger.error("Snapshot partitioning failed: %s", exc)
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
                failure_reasons=("PIPELINE_ERROR: SNAPSHOT_PARTITION_FAILED",),
            )

        # 2. Master Glyph Reconstruction & Canonical Model Assembly
        try:
            solver = MaxReconstructionSolver()
            fit_by_cp: dict[int, list[ObservationRecord]] = {}
            for r in partition.fit_records:
                fit_by_cp.setdefault(r.code_point, []).append(r)

            reconstructed_glyphs: dict[int, ReconstructedGlyph] = {}
            for cp, cp_fit_recs in fit_by_cp.items():
                glyph_fit_obs = [
                    (r, snapshot.get_raster_bytes(r.cache_key))
                    for r in cp_fit_recs
                ]
                reconstructed_glyphs[cp] = solver.reconstruct_glyph(glyph_fit_obs)

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
                config_hash=snapshot.config.compute_hash(),
                browser_version=snapshot.browser_version,
                fit_observations_count=len(partition.fit_records),
                calibration_fingerprint=calib_fp,
                kerning_pairs=kerning_map,
            )
            model_hash = model.compute_canonical_hash()

        except Exception as exc:
            logger.error("Model fitting failed: %s", exc)
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
                failure_reasons=("PIPELINE_ERROR: MODEL_FITTING_FAILED",),
            )

        # 3. Candidate Font Building & Artifact Attestation
        try:
            if output_dir is not None:
                work_dir = Path(output_dir)
                work_dir.mkdir(parents=True, exist_ok=True)
            else:
                work_dir = Path(tempfile.mkdtemp(prefix="telefont_candidate_"))

            builder = MaxCandidateFontBuilder(
                family_name=snapshot.family_name,
                style_name=snapshot.style_name,
                units_per_em=1000,
            )
            family_build = builder.build_candidate_family(
                glyphs=reconstructed_glyphs,
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
            cand_sha = candidate_art.sha256_hex
            cand_path = candidate_art.file_path

        except Exception as exc:
            logger.error("Candidate font build/attestation failed: %s", type(exc).__name__)
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
                failure_reasons=("PIPELINE_ERROR: CANDIDATE_ATTESTATION_FAILED",),
            )

        # 4. Production Consumer Evidence Production & Authoritative Gating
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

            is_pass = (report.overall_status == "PASS")
            sanitized_reasons: tuple[str, ...] = ()
            if not is_pass:
                sanitized_reasons = tuple(
                    r.split(":")[0] if ":" in r else r
                    for r in report.failure_reasons
                ) or ("PIPELINE_ERROR: FIDELITY_GATE_FAILED",)

            return LocalFidelityPipelineResult(
                is_publishable=is_pass,
                status=report.overall_status,
                family_name=snapshot.family_name,
                style_name=snapshot.style_name,
                reference_id=snapshot.reference_id,
                style_id=snapshot.style_id,
                model_hash=model_hash,
                candidate_artifact_sha=cand_sha,
                candidate_file_path=cand_path,
                report=report,
                failure_reasons=sanitized_reasons,
            )

        except Exception as exc:
            logger.error("Consumer evidence production or fidelity evaluation failed: %s", exc)
            return LocalFidelityPipelineResult(
                is_publishable=False,
                status="FAIL",
                family_name=snapshot.family_name,
                style_name=snapshot.style_name,
                reference_id=snapshot.reference_id,
                style_id=snapshot.style_id,
                model_hash=model_hash,
                candidate_artifact_sha=cand_sha,
                candidate_file_path=cand_path,
                report=None,
                failure_reasons=("PIPELINE_ERROR: FIDELITY_EVALUATION_FAILED",),
            )

    @classmethod
    def execute_sync(
        cls,
        snapshot: ObservationStoreSnapshot,
        thresholds: FidelityThresholds | None = None,
        output_dir: str | Path | None = None,
        format_type: str = "TTF",
    ) -> LocalFidelityPipelineResult:
        """Synchronous wrapper for executing the pipeline."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    lambda: asyncio.run(
                        cls.execute(
                            snapshot=snapshot,
                            thresholds=thresholds,
                            output_dir=output_dir,
                            format_type=format_type,
                        )
                    )
                )
                return future.result()
        else:
            return asyncio.run(
                cls.execute(
                    snapshot=snapshot,
                    thresholds=thresholds,
                    output_dir=output_dir,
                    format_type=format_type,
                )
            )
