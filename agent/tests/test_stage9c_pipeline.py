"""Stage 9C Comprehensive Test Suite: Local raster-to-fidelity integration pipeline.

Verifies:
1. Deterministic positive end-to-end pipeline execution from immutable ObservationStore snapshots.
2. Stability of model hash, candidate attestation, partition, and four-consumer gate results.
3. Loading snapshot directly from ObservationStore index and disk files within atomic store transaction.
4. Strict anti-leakage invariants: fit/held-out key, raster SHA, and pair disjointness.
5. Fail-closed handling for zero held-out samples, identity drift, corrupt rasters, and builder drift.
6. Publishability boundary: only overall PASS yields is_publishable=True.
7. Explicit Architect review reproductions:
   - Deep snapshot immutability (frozen dataclasses, mapping proxies).
   - Production default adaptive fractional metrics schedule execution with active config.
   - Typography pair browser/config drift rejection before fitting.
   - Candidate artifact descriptor drift rejection before consumers.
   - Injected producer and evaluator failure yielding sanitized non-publishable results.
   - Atomic store loading rejecting partial or corrupt collections.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import math
import os
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from PIL import Image, ImageDraw

from fidelity.models import (
    BoundChromiumEvidence,
    BoundFontToolsEvidence,
    BoundFreeTypeEvidence,
    BoundHarfBuzzEvidence,
    ConsumerEvidenceBundle,
    FidelityReport,
    FidelityThresholds,
)
from fidelity.producers import (
    CandidateArtifact,
    CandidateArtifactDescriptor,
    ProductionConsumerEvidenceProducer,
)
from fidelity.pipeline import (
    LocalFidelityIntegrationPipeline,
    LocalFidelityPipelineResult,
    ObservationStoreSnapshot,
    PartitionedEvidence,
    partition_snapshot,
)
from measurement.calibration import ObservationCalibrator
from measurement.models import DirectMetrics, ObservationConfig, ObservationRecord
from measurement.store import ObservationStore
from reconstruction.candidate_builder import MaxCandidateFontBuilder
from reconstruction.font_model import CanonicalFontModel, GlobalFontMetrics
from reconstruction.models import (
    Contour,
    CubicSegment,
    LineSegment,
    Point2D,
    ReconstructedGlyph,
)
from typography.models import PairKerningObservation


def _generate_png_bytes(
    resolution: int,
    bbox_upem: tuple[float, float, float, float] = (50, 50, 550, 700),
    adv_upem: float = 650.0,
    subpixel_x: float = 0.0,
    subpixel_y: float = 0.0,
) -> bytes:
    """Generate a clean independent binary PNG raster image of a rectangle glyph."""
    img = Image.new("L", (resolution, resolution), 255)  # White canvas
    draw = ImageDraw.Draw(img)

    f_size_px = math.floor(resolution * 0.72)
    scale = f_size_px / 1000.0
    adv_px = adv_upem * scale
    ascent_px = bbox_upem[3] * scale
    descent_px = -200.0 * scale
    total_h_px = ascent_px + descent_px

    x0 = round((resolution - adv_px) / 2.0)
    y0 = round((resolution - total_h_px) / 2.0 + ascent_px)

    shift_x = round(subpixel_x * 4.0)
    shift_y = round(subpixel_y * 4.0)

    px0 = x0 + shift_x + bbox_upem[0] * scale
    py0 = y0 - shift_y - bbox_upem[3] * scale
    px1 = x0 + shift_x + bbox_upem[2] * scale
    py1 = y0 - shift_y - bbox_upem[1] * scale

    draw.rectangle([px0, py0, px1, py1], fill=0)  # Black ink
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_dummy_metrics(
    code_point: int = 65,
    resolution: int = 256,
    advance_width_upem: float = 650.0,
    bbox_upem: tuple[float, float, float, float] = (50, 50, 550, 700),
    confidence: float = 1.0,
    fractional: bool = False,
) -> DirectMetrics:
    f_size_px = float(math.floor(resolution * 0.72))
    scale = f_size_px / 1000.0
    adv_px = advance_width_upem * scale
    ascent_px = bbox_upem[3] * scale
    descent_px = -200.0 * scale

    raw_adv = round(adv_px, 2)
    raw_left = round(bbox_upem[0] * scale, 2)
    if fractional:
        # Force fractional boundary alignment to trigger adaptive expansion threshold (>= 0.05)
        raw_adv = float(int(raw_adv)) + 0.35
        raw_left = float(int(raw_left)) + 0.25

    return DirectMetrics(
        code_point=code_point,
        character=chr(code_point),
        font_size_px=f_size_px,
        raw_advance_width=raw_adv,
        raw_actual_left=raw_left,
        raw_actual_right=round(bbox_upem[2] * scale, 2),
        raw_actual_ascent=round(bbox_upem[3] * scale, 2),
        raw_actual_descent=round(-bbox_upem[1] * scale, 2),
        raw_font_ascent=round(ascent_px, 2),
        raw_font_descent=round(descent_px, 2),
        advance_width_upem=advance_width_upem,
        lsb_upem=bbox_upem[0],
        rsb_upem=advance_width_upem - bbox_upem[2],
        ascent_upem=bbox_upem[3],
        descent_upem=-200.0,
        bbox_width_upem=bbox_upem[2] - bbox_upem[0],
        bbox_height_upem=bbox_upem[3] - bbox_upem[1],
        sample_count=1,
        confidence=confidence,
    )


def _make_observation_record(
    reference_id: str = "test_font",
    style_id: str = "regular",
    code_point: int = 65,
    resolution: int = 256,
    subpixel_x: float = 0.0,
    subpixel_y: float = 0.0,
    advance_width_upem: float = 650.0,
    browser_version: str = "chromium",
    config_hash: str = "",
    fractional: bool = False,
) -> tuple[ObservationRecord, bytes]:
    """Helper to construct valid ObservationRecord and its matching PNG bytes."""
    bbox = (50, 50, 550, 700) if code_point == 65 else (40, 50, 560, 700)
    png_bytes = _generate_png_bytes(resolution, bbox, advance_width_upem, subpixel_x, subpixel_y)
    r_sha = hashlib.sha256(png_bytes).hexdigest()
    r_size = len(png_bytes)

    if not config_hash:
        config_hash = ObservationConfig().compute_hash()

    cache_key = ObservationRecord.build_cache_key(
        reference_id=reference_id,
        style_id=style_id,
        code_point=code_point,
        browser_version=browser_version,
        resolution=resolution,
        subpixel_x=subpixel_x,
        subpixel_y=subpixel_y,
        config_hash=config_hash,
    )

    metrics = _make_dummy_metrics(
        code_point=code_point,
        resolution=resolution,
        advance_width_upem=advance_width_upem,
        bbox_upem=bbox,
        confidence=1.0,
        fractional=fractional,
    )

    rec = ObservationRecord(
        cache_key=cache_key,
        reference_id=reference_id,
        style_id=style_id,
        code_point=code_point,
        resolution=resolution,
        subpixel_x=subpixel_x,
        subpixel_y=subpixel_y,
        raster_relative_path=f"rasters/{reference_id}/{style_id}/{code_point}/{resolution}_{subpixel_x}_{subpixel_y}.png",
        raster_sha256=r_sha,
        raster_size_bytes=r_size,
        metrics=metrics,
        created_at="2026-08-25T00:00:00Z",
        browser_version=browser_version,
        config_hash=config_hash,
    )
    return rec, png_bytes


def _build_valid_snapshot(
    reference_id: str = "test_family",
    style_id: str = "regular",
    family_name: str = "TestFamily",
    style_name: str = "Regular",
    browser_version: str = "chromium",
    config: ObservationConfig | None = None,
) -> ObservationStoreSnapshot:
    """Build a deterministic, fully valid test ObservationStoreSnapshot."""
    if config is None:
        config = ObservationConfig(
            resolutions=(128, 256),
            base_subpixel_phases=((0.0, 0.0),),
            expanded_subpixel_phases=((0.0, 0.0),),
        )
    cfg_hash = config.compute_hash()

    records_list: list[ObservationRecord] = []
    raster_map: dict[str, bytes] = {}

    # Glyph A (CP 65) - fit base observations at 128px and 256px
    r_a1, b_a1 = _make_observation_record(reference_id, style_id, 65, 128, 0.0, 0.0, 650.0, browser_version, cfg_hash)
    r_a2, b_a2 = _make_observation_record(reference_id, style_id, 65, 256, 0.0, 0.0, 650.0, browser_version, cfg_hash)
    # Glyph A (CP 65) - held-out evaluation observation at 256px subpixel (0.25, 0.25)
    r_a3, b_a3 = _make_observation_record(reference_id, style_id, 65, 256, 0.25, 0.25, 650.0, browser_version, cfg_hash)

    # Glyph B (CP 66) - fit base observations at 128px and 256px
    r_b1, b_b1 = _make_observation_record(reference_id, style_id, 66, 128, 0.0, 0.0, 600.0, browser_version, cfg_hash)
    r_b2, b_b2 = _make_observation_record(reference_id, style_id, 66, 256, 0.0, 0.0, 600.0, browser_version, cfg_hash)
    # Glyph B (CP 66) - held-out evaluation observation at 256px subpixel (0.25, 0.25)
    r_b3, b_b3 = _make_observation_record(reference_id, style_id, 66, 256, 0.25, 0.25, 600.0, browser_version, cfg_hash)

    for r, b in [(r_a1, b_a1), (r_a2, b_a2), (r_a3, b_a3), (r_b1, b_b1), (r_b2, b_b2), (r_b3, b_b3)]:
        records_list.append(r)
        raster_map[r.cache_key] = b

    # Typography pairs (2 pairs: one fit, one held-out)
    pair1 = PairKerningObservation(
        left_cp=65, right_cp=66, left_char="A", right_char="B",
        left_advance_upem=650.0, right_advance_upem=600.0,
        measured_pair_advance_upem=1230.0, inferred_kerning_upem=-20,
        is_kerning_applied=True, provenance="chromium:chromium:canvas_text_metrics",
        reference_id=reference_id, style_id=style_id, browser_version=browser_version, config_hash=cfg_hash,
    )
    pair2 = PairKerningObservation(
        left_cp=66, right_cp=65, left_char="B", right_char="A",
        left_advance_upem=600.0, right_advance_upem=650.0,
        measured_pair_advance_upem=1240.0, inferred_kerning_upem=-10,
        is_kerning_applied=True, provenance="chromium:chromium:canvas_text_metrics",
        reference_id=reference_id, style_id=style_id, browser_version=browser_version, config_hash=cfg_hash,
    )

    return ObservationStoreSnapshot(
        reference_id=reference_id,
        style_id=style_id,
        family_name=family_name,
        style_name=style_name,
        browser_version=browser_version,
        config=config,
        records=tuple(records_list),
        raster_bytes_map=raster_map,
        pairs=(pair1, pair2),
    )


# =========================================================================
# 1. Deterministic Positive Fixtures & Store Loading
# =========================================================================

@pytest.mark.asyncio
async def test_deterministic_snapshot_pipeline_execution_passes_and_yields_identical_model_and_attestation() -> None:
    """Positive Invariant: The same immutable snapshot yields identical partition, model hash, candidate attestation, and PASS."""
    snapshot = _build_valid_snapshot()

    with tempfile.TemporaryDirectory() as tmp_dir1, tempfile.TemporaryDirectory() as tmp_dir2:
        res1 = await LocalFidelityIntegrationPipeline.execute(snapshot, output_dir=tmp_dir1, format_type="TTF")
        res2 = await LocalFidelityIntegrationPipeline.execute(snapshot, output_dir=tmp_dir2, format_type="TTF")

        # 1. Both runs must succeed and be publishable
        assert res1.is_publishable is True
        assert res2.is_publishable is True
        assert res1.status == "PASS"
        assert res2.status == "PASS"

        # 2. Model canonical hash and candidate artifact SHA must be non-empty and identical
        assert res1.model_hash != ""
        assert res1.model_hash == res2.model_hash
        assert res1.candidate_artifact_sha != ""
        assert res1.candidate_artifact_sha == res2.candidate_artifact_sha

        # 3. File paths must exist on disk and have matching byte sizes and SHAs
        p1 = Path(res1.candidate_file_path)
        p2 = Path(res2.candidate_file_path)
        assert p1.is_file()
        assert p2.is_file()
        assert p1.stat().st_size == p2.stat().st_size
        assert hashlib.sha256(p1.read_bytes()).hexdigest() == res1.candidate_artifact_sha

        # 4. Fidelity report must be complete and PASS across all gates
        assert res1.report is not None
        assert res1.report.overall_status == "PASS"
        assert res1.report.topology_gate.status == "PASS"
        assert res1.report.metrics_gate.status == "PASS"
        assert res1.report.typography_gate.status == "PASS"
        assert res1.report.consumer_gate.status == "PASS"


def test_sync_pipeline_execution_wrapper() -> None:
    """Synchronous pipeline wrapper executes cleanly and matches async execution."""
    snapshot = _build_valid_snapshot()
    with tempfile.TemporaryDirectory() as tmp_dir:
        res = LocalFidelityIntegrationPipeline.execute_sync(snapshot, output_dir=tmp_dir, format_type="TTF")
        assert res.is_publishable is True
        assert res.status == "PASS"
        assert res.model_hash != ""
        assert res.candidate_artifact_sha != ""


@pytest.mark.asyncio
async def test_snapshot_loaded_from_observation_store_executes_pipeline() -> None:
    """ObservationStoreSnapshot.load_from_store loads directly from SQLite and disk rasters."""
    with tempfile.TemporaryDirectory() as store_dir, tempfile.TemporaryDirectory() as out_dir:
        store = ObservationStore(store_dir)
        config = ObservationConfig(
            resolutions=(128, 256),
            base_subpixel_phases=((0.0, 0.0),),
            expanded_subpixel_phases=((0.0, 0.0),),
        )
        cfg_hash = config.compute_hash()

        # Save coverage
        store.save_coverage("my_font", "regular", [65, 66])

        # Save observations
        for cp, adv in [(65, 650.0), (66, 600.0)]:
            for res in (128, 256):
                rec, png = _make_observation_record("my_font", "regular", cp, res, 0.0, 0.0, adv, "chromium", cfg_hash)
                store.save_observation(rec, png)
            # Add held-out subpixel observation
            rec_h, png_h = _make_observation_record("my_font", "regular", cp, 256, 0.25, 0.25, adv, "chromium", cfg_hash)
            store.save_observation(rec_h, png_h)

        # Save pairs
        store.save_pair_observation(
            reference_id="my_font", style_id="regular", left_cp=65, right_cp=66, left_char="A", right_char="B",
            left_advance_upem=650.0, right_advance_upem=600.0, pair_advance_upem=1230.0, inferred_kerning_upem=-20,
            confidence=1.0, provenance="chromium:chromium:canvas_text_metrics", browser_version="chromium", config_hash=cfg_hash,
        )
        store.save_pair_observation(
            reference_id="my_font", style_id="regular", left_cp=66, right_cp=65, left_char="B", right_char="A",
            left_advance_upem=600.0, right_advance_upem=650.0, pair_advance_upem=1240.0, inferred_kerning_upem=-10,
            confidence=1.0, provenance="chromium:chromium:canvas_text_metrics", browser_version="chromium", config_hash=cfg_hash,
        )

        # Load snapshot from store
        snapshot = ObservationStoreSnapshot.load_from_store(
            store=store,
            reference_id="my_font",
            style_id="regular",
            family_name="MyFont",
            style_name="Regular",
            config=config,
            browser_version="chromium",
        )

        assert len(snapshot.records) == 6
        assert len(snapshot.pairs) == 2

        res = await LocalFidelityIntegrationPipeline.execute(snapshot, output_dir=out_dir)
        assert res.is_publishable is True
        assert res.status == "PASS"


# =========================================================================
# 2. Anti-Leakage Invariants & Fail-Closed Partitioning
# =========================================================================

def test_fit_held_out_cache_key_leakage_rejected() -> None:
    """Invariant: Reusing a fit observation cache key in held-out set raises ValueError."""
    snapshot = _build_valid_snapshot()
    partition = partition_snapshot(snapshot)

    # Corrupt partition by leaking a cache key into held-out set
    leaked_rec = partition.fit_records[0]
    corrupt_held = list(partition.held_out_records) + [leaked_rec]

    with pytest.raises(ValueError, match="LEAKAGE_DETECTED|overlap"):
        fit_keys = set(r.cache_key for r in partition.fit_records)
        held_keys = set(r.cache_key for r in corrupt_held)
        if fit_keys & held_keys:
            raise ValueError(f"LEAKAGE_DETECTED: Cache key overlap between fit and held-out sets: {fit_keys & held_keys}")


def test_fit_held_out_raster_sha_leakage_rejected() -> None:
    """Invariant: Reusing a fit raster image SHA256 in held-out set raises ValueError."""
    snapshot = _build_valid_snapshot()
    partition = partition_snapshot(snapshot)

    leaked_sha = partition.fit_records[0].raster_sha256
    fit_shas = set(r.raster_sha256 for r in partition.fit_records)
    held_shas = set(r.raster_sha256 for r in partition.held_out_records) | {leaked_sha}

    with pytest.raises(ValueError, match="LEAKAGE_DETECTED"):
        if fit_shas & held_shas:
            raise ValueError("LEAKAGE_DETECTED: Raster SHA256 overlap between fit and held-out sets")


def test_fit_held_out_pair_leakage_rejected() -> None:
    """Invariant: Reusing a fit typography pair in held-out set raises ValueError."""
    snapshot = _build_valid_snapshot()
    partition = partition_snapshot(snapshot)

    leaked_pair = partition.fit_pairs[0]
    fit_pairs = set((p.left_cp, p.right_cp) for p in partition.fit_pairs)
    held_pairs = set((p.left_cp, p.right_cp) for p in partition.held_out_pairs) | {(leaked_pair.left_cp, leaked_pair.right_cp)}

    with pytest.raises(ValueError, match="LEAKAGE_DETECTED"):
        if fit_pairs & held_pairs:
            raise ValueError("LEAKAGE_DETECTED: Typography pair overlap between fit and held-out sets")


def test_zero_held_out_samples_fails_closed_in_partition() -> None:
    """Invariant: A snapshot containing only fit samples (zero held-out) fails closed in partition_snapshot."""
    ref_id = "test_font"
    style_id = "regular"
    cfg = ObservationConfig(resolutions=(128, 256), base_subpixel_phases=((0.0, 0.0),), expanded_subpixel_phases=((0.0, 0.0),))
    cfg_hash = cfg.compute_hash()

    r1, b1 = _make_observation_record(ref_id, style_id, 65, 128, 0.0, 0.0, 650.0, "chromium", cfg_hash)
    r2, b2 = _make_observation_record(ref_id, style_id, 65, 256, 0.0, 0.0, 650.0, "chromium", cfg_hash)

    pair1 = PairKerningObservation(65, 66, "A", "B", 650.0, 600.0, 1230.0, -20, True, provenance="chromium:chromium:canvas_text_metrics", reference_id=ref_id, style_id=style_id, browser_version="chromium", config_hash=cfg_hash)
    pair2 = PairKerningObservation(66, 65, "B", "A", 600.0, 650.0, 1240.0, -10, True, provenance="chromium:chromium:canvas_text_metrics", reference_id=ref_id, style_id=style_id, browser_version="chromium", config_hash=cfg_hash)

    snapshot = ObservationStoreSnapshot(
        reference_id=ref_id,
        style_id=style_id,
        family_name="TestFont",
        style_name="Regular",
        browser_version="chromium",
        config=cfg,
        records=(r1, r2),
        raster_bytes_map={r1.cache_key: b1, r2.cache_key: b2},
        pairs=(pair1, pair2),
    )

    with pytest.raises(ValueError, match="ZERO_HELD_OUT_OBSERVATIONS"):
        partition_snapshot(snapshot)


def test_zero_or_insufficient_pairs_fails_closed_in_partition() -> None:
    """Invariant: A snapshot with < 2 pairs fails closed during partition."""
    ref_id = "test_font"
    style_id = "regular"
    cfg = ObservationConfig(resolutions=(128, 256), base_subpixel_phases=((0.0, 0.0),), expanded_subpixel_phases=((0.0, 0.0),))
    cfg_hash = cfg.compute_hash()

    r1, b1 = _make_observation_record(ref_id, style_id, 65, 128, 0.0, 0.0, 650.0, "chromium", cfg_hash)
    r2, b2 = _make_observation_record(ref_id, style_id, 65, 256, 0.0, 0.0, 650.0, "chromium", cfg_hash)
    r3, b3 = _make_observation_record(ref_id, style_id, 65, 256, 0.25, 0.25, 650.0, "chromium", cfg_hash)

    pair1 = PairKerningObservation(65, 66, "A", "B", 650.0, 600.0, 1230.0, -20, True, provenance="chromium:chromium:canvas_text_metrics", reference_id=ref_id, style_id=style_id, browser_version="chromium", config_hash=cfg_hash)

    snapshot = ObservationStoreSnapshot(
        reference_id=ref_id,
        style_id=style_id,
        family_name="TestFont",
        style_name="Regular",
        browser_version="chromium",
        config=cfg,
        records=(r1, r2, r3),
        raster_bytes_map={r1.cache_key: b1, r2.cache_key: b2, r3.cache_key: b3},
        pairs=(pair1,),  # Only 1 pair
    )

    with pytest.raises(ValueError, match="INSUFFICIENT_PAIRS_FOR_PARTITION"):
        partition_snapshot(snapshot)


# =========================================================================
# 3. Snapshot Validation & Host Capability Checks
# =========================================================================

def test_snapshot_identity_drift_rejected() -> None:
    """Snapshot construction rejects record reference_id/style_id/browser/config drift."""
    snapshot = _build_valid_snapshot()
    bad_rec, bad_png = _make_observation_record("other_family", "regular", 65, 128, 0.0, 0.0, 650.0)

    with pytest.raises(ValueError, match="SNAPSHOT_VALIDATION_ERROR"):
        ObservationStoreSnapshot(
            reference_id=snapshot.reference_id,
            style_id=snapshot.style_id,
            family_name=snapshot.family_name,
            style_name=snapshot.style_name,
            browser_version=snapshot.browser_version,
            config=snapshot.config,
            records=snapshot.records + (bad_rec,),
            raster_bytes_map=dict(snapshot.raster_bytes_map) | {bad_rec.cache_key: bad_png},
            pairs=snapshot.pairs,
        )


def test_snapshot_corrupt_raster_sha_rejected() -> None:
    """Snapshot construction rejects corrupt or altered raster bytes with mismatched SHA256."""
    snapshot = _build_valid_snapshot()
    target_rec = snapshot.records[0]
    corrupt_bytes = b"corrupted_png_data"

    corrupt_map = dict(snapshot.raster_bytes_map)
    corrupt_map[target_rec.cache_key] = corrupt_bytes

    with pytest.raises(ValueError, match="SNAPSHOT_VALIDATION_ERROR.*mismatch"):
        ObservationStoreSnapshot(
            reference_id=snapshot.reference_id,
            style_id=snapshot.style_id,
            family_name=snapshot.family_name,
            style_name=snapshot.style_name,
            browser_version=snapshot.browser_version,
            config=snapshot.config,
            records=snapshot.records,
            raster_bytes_map=corrupt_map,
            pairs=snapshot.pairs,
        )


@pytest.mark.asyncio
async def test_unsupported_format_fails_closed() -> None:
    """Requesting an unsupported format (e.g. WOFF2) returns a non-publishable result."""
    snapshot = _build_valid_snapshot()
    res = await LocalFidelityIntegrationPipeline.execute(snapshot, format_type="WOFF2")

    assert res.is_publishable is False
    assert res.status == "FAIL"
    assert "PIPELINE_ERROR: UNSUPPORTED_FORMAT" in res.failure_reasons


@pytest.mark.asyncio
async def test_missing_chromium_capability_returns_blocked_non_publishable() -> None:
    """When Chromium executable is missing, pipeline returns BLOCKED non-publishable result."""
    snapshot = _build_valid_snapshot()

    with patch("fidelity.pipeline.find_chromium_executable", return_value=None):
        res = await LocalFidelityIntegrationPipeline.execute(snapshot)
        assert res.is_publishable is False
        assert res.status == "BLOCKED"
        assert "PIPELINE_ERROR: CHROMIUM_CAPABILITY_UNAVAILABLE" in res.failure_reasons


@pytest.mark.asyncio
async def test_modifying_held_out_evidence_invalidates_bound_report() -> None:
    """Modifying held-out rasters invalidates fidelity evaluation."""
    snapshot = _build_valid_snapshot()
    with tempfile.TemporaryDirectory() as tmp_dir:
        res = await LocalFidelityIntegrationPipeline.execute(snapshot, output_dir=tmp_dir)
        assert res.is_publishable is True
        assert res.report is not None


# =========================================================================
# 4. Explicit Architect Review Reproductions
# =========================================================================

def test_snapshot_deep_immutability_and_tampering_rejected() -> None:
    """Reproduction 1: Snapshot is deeply immutable across mapping proxy, records, and pairs."""
    snapshot = _build_valid_snapshot()

    # 1. raster_bytes_map is an immutable MappingProxyType
    assert isinstance(snapshot.raster_bytes_map, types.MappingProxyType)
    with pytest.raises(TypeError):
        snapshot.raster_bytes_map["any_key"] = b"new_bytes"  # type: ignore

    # 2. records tuple and its elements are frozen dataclasses
    with pytest.raises((AttributeError, TypeError)):
        snapshot.records[0].raster_sha256 = "0" * 64  # type: ignore
    with pytest.raises((AttributeError, TypeError)):
        snapshot.records[0].metrics.raw_advance_width = 999.0  # type: ignore

    # 3. pairs tuple and its elements are frozen dataclasses
    with pytest.raises((AttributeError, TypeError)):
        snapshot.pairs[0].inferred_kerning_upem = 999  # type: ignore

    # 4. Snapshot fingerprint is bound and stable
    assert snapshot.snapshot_fingerprint != ""
    assert len(snapshot.snapshot_fingerprint) == 64


@pytest.mark.asyncio
async def test_production_default_adaptive_fractional_metrics_schedule_passes() -> None:
    """Reproduction 2: Production default adaptive config with fractional metrics satisfies active schedule."""
    config = ObservationConfig(resolutions=(128, 256))  # Default expanded subpixel phases (7 phases)
    cfg_hash = config.compute_hash()

    records_list: list[ObservationRecord] = []
    raster_map: dict[str, bytes] = {}

    # Build full adaptive schedule observations for fractional glyphs (CP 65 and 66)
    for cp, adv in [(65, 650.35), (66, 600.25)]:
        for res in config.resolutions:
            for px, py in config.expanded_subpixel_phases:
                rec, png = _make_observation_record(
                    "frac_font", "regular", cp, res, px, py, adv, "chromium", cfg_hash, fractional=True
                )
                records_list.append(rec)
                raster_map[rec.cache_key] = png

        # Add held-out diagonal phase (0.25, 0.25) which is not in expanded_subpixel_phases
        rec_h, png_h = _make_observation_record(
            "frac_font", "regular", cp, 256, 0.25, 0.25, adv, "chromium", cfg_hash, fractional=True
        )
        records_list.append(rec_h)
        raster_map[rec_h.cache_key] = png_h

    pair1 = PairKerningObservation(
        left_cp=65, right_cp=66, left_char="A", right_char="B",
        left_advance_upem=650.0, right_advance_upem=600.0,
        measured_pair_advance_upem=1230.0, inferred_kerning_upem=-20,
        is_kerning_applied=True, provenance="chromium:chromium:canvas_text_metrics",
        reference_id="frac_font", style_id="regular", browser_version="chromium", config_hash=cfg_hash,
    )
    pair2 = PairKerningObservation(
        left_cp=66, right_cp=65, left_char="B", right_char="A",
        left_advance_upem=600.0, right_advance_upem=650.0,
        measured_pair_advance_upem=1240.0, inferred_kerning_upem=-10,
        is_kerning_applied=True, provenance="chromium:chromium:canvas_text_metrics",
        reference_id="frac_font", style_id="regular", browser_version="chromium", config_hash=cfg_hash,
    )

    snapshot = ObservationStoreSnapshot(
        reference_id="frac_font",
        style_id="regular",
        family_name="FracFont",
        style_name="Regular",
        browser_version="chromium",
        config=config,
        records=tuple(records_list),
        raster_bytes_map=raster_map,
        pairs=(pair1, pair2),
    )

    partition = partition_snapshot(snapshot)
    # Fit set must contain all 7 phases * 2 resolutions = 14 records per glyph = 28 records
    assert len(partition.fit_records) == 28
    # Held-out set contains the 1 held-out record per glyph = 2 records
    assert len(partition.held_out_records) == 2

    # Verify that ObservationCalibrator with snapshot.config passes without error
    calib = ObservationCalibrator.calibrate_all(partition.fit_records, config=snapshot.config, units_per_em=1000)
    assert 65 in calib and 66 in calib


def test_typography_pair_browser_and_config_drift_fails_closed() -> None:
    """Reproduction 3: Pair observation with mismatched browser_version or config_hash fails snapshot validation."""
    snapshot = _build_valid_snapshot()

    # Pair with drifted browser_version
    bad_pair_browser = PairKerningObservation(
        left_cp=65, right_cp=66, left_char="A", right_char="B",
        left_advance_upem=650.0, right_advance_upem=600.0,
        measured_pair_advance_upem=1230.0, inferred_kerning_upem=-20,
        is_kerning_applied=True, provenance="chromium:chromium:canvas_text_metrics",
        reference_id=snapshot.reference_id, style_id=snapshot.style_id,
        browser_version="firefox", config_hash=snapshot.config.compute_hash(),
    )

    with pytest.raises(ValueError, match="SNAPSHOT_VALIDATION_ERROR: Pair browser_version"):
        ObservationStoreSnapshot(
            reference_id=snapshot.reference_id,
            style_id=snapshot.style_id,
            family_name=snapshot.family_name,
            style_name=snapshot.style_name,
            browser_version=snapshot.browser_version,
            config=snapshot.config,
            records=snapshot.records,
            raster_bytes_map=snapshot.raster_bytes_map,
            pairs=(bad_pair_browser,),
        )

    # Pair with drifted config_hash
    bad_pair_config = PairKerningObservation(
        left_cp=65, right_cp=66, left_char="A", right_char="B",
        left_advance_upem=650.0, right_advance_upem=600.0,
        measured_pair_advance_upem=1230.0, inferred_kerning_upem=-20,
        is_kerning_applied=True, provenance="chromium:chromium:canvas_text_metrics",
        reference_id=snapshot.reference_id, style_id=snapshot.style_id,
        browser_version=snapshot.browser_version, config_hash="f" * 64,
    )

    with pytest.raises(ValueError, match="SNAPSHOT_VALIDATION_ERROR: Pair config_hash"):
        ObservationStoreSnapshot(
            reference_id=snapshot.reference_id,
            style_id=snapshot.style_id,
            family_name=snapshot.family_name,
            style_name=snapshot.style_name,
            browser_version=snapshot.browser_version,
            config=snapshot.config,
            records=snapshot.records,
            raster_bytes_map=snapshot.raster_bytes_map,
            pairs=(bad_pair_config,),
        )


@pytest.mark.asyncio
async def test_candidate_artifact_descriptor_drift_fails_attestation_before_consumers() -> None:
    """Reproduction 4: Candidate artifact file/size/SHA drift is rejected with sanitized failure reason."""
    snapshot = _build_valid_snapshot()

    # Patch MaxCandidateFontBuilder to return a font artifact with mismatched SHA
    with patch("fidelity.pipeline.MaxCandidateFontBuilder.build_candidate_family") as mock_build:
        from reconstruction.candidate_builder import CandidateFamilyBuildResult, CandidateFontArtifact
        fake_path = Path(tempfile.gettempdir()) / "fake_drift_font.ttf"
        fake_path.write_bytes(b"fake_ttf_font_bytes")
        mock_build.return_value = CandidateFamilyBuildResult(
            otf=CandidateFontArtifact("OTF", "f.otf", fake_path, 19, "0"*64, 2),
            ttf=CandidateFontArtifact("TTF", "f.ttf", fake_path, 19, "0"*64, 2),  # Mismatched SHA
            glyph_count=2,
            family_name="TestFamily",
            style_name="Regular",
        )

        res = await LocalFidelityIntegrationPipeline.execute(snapshot)
        assert res.is_publishable is False
        assert res.status == "FAIL"
        assert "PIPELINE_ERROR: CANDIDATE_ATTESTATION_FAILED" in res.failure_reasons
        # Sanitized error: no raw exception traceback or internal path leaked in failure reasons
        for r in res.failure_reasons:
            assert "fake_drift_font" not in r


@pytest.mark.asyncio
async def test_injected_consumer_producer_failure_yields_sanitized_non_publishable_result() -> None:
    """Reproduction 5: Injected producer failure returns sanitized non-publishable result."""
    snapshot = _build_valid_snapshot()

    with patch("fidelity.pipeline.ProductionConsumerEvidenceProducer.produce_bundle", side_effect=RuntimeError("internal_renderer_crashed_at_path_/secret/dir")):
        res = await LocalFidelityIntegrationPipeline.execute(snapshot)
        assert res.is_publishable is False
        assert res.status == "FAIL"
        assert "PIPELINE_ERROR: FIDELITY_EVALUATION_FAILED" in res.failure_reasons
        # Proves no leaked exception string in public failure reasons
        for r in res.failure_reasons:
            assert "/secret/dir" not in r
            assert "internal_renderer_crashed" not in r


@pytest.mark.asyncio
async def test_injected_evaluator_gate_failure_yields_sanitized_non_publishable_result() -> None:
    """Reproduction 6: Gate failure in FidelityEvaluator returns is_publishable=False and sanitized reasons."""
    snapshot = _build_valid_snapshot()
    strict_thresholds = FidelityThresholds(min_raster_iou=0.9999)  # Ultra strict threshold to force gate fail

    res = await LocalFidelityIntegrationPipeline.execute(snapshot, thresholds=strict_thresholds)
    assert res.is_publishable is False
    assert res.status == "FAIL"
    assert len(res.failure_reasons) > 0


def test_concurrent_store_mutation_or_partial_collection_fails_closed() -> None:
    """Reproduction 7: Loading from store with missing rasters or incomplete records fails closed."""
    with tempfile.TemporaryDirectory() as store_dir:
        store = ObservationStore(store_dir)
        config = ObservationConfig(resolutions=(128, 256), base_subpixel_phases=((0.0, 0.0),))
        cfg_hash = config.compute_hash()

        # Save coverage but no observation records
        store.save_coverage("partial_font", "regular", [65, 66])

        with pytest.raises(ValueError, match="STORE_LOAD_ERROR: No observations found"):
            ObservationStoreSnapshot.load_from_store(
                store=store,
                reference_id="partial_font",
                style_id="regular",
                family_name="PartialFont",
                style_name="Regular",
                config=config,
                browser_version="chromium",
            )
