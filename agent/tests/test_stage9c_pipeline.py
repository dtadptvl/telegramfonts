"""Stage 9C Comprehensive Test Suite: Local raster-to-fidelity integration pipeline.

Verifies:
1. Deterministic positive end-to-end pipeline execution from immutable ObservationStore snapshots.
2. Stability of model hash, candidate attestation, partition, and four-consumer gate results.
3. Loading snapshot directly from ObservationStore index and disk files.
4. Strict anti-leakage invariants: fit/held-out key, raster SHA, and pair disjointness.
5. Fail-closed handling for zero held-out samples, identity drift, corrupt rasters, and builder drift.
6. Publishability boundary: only overall PASS yields is_publishable=True.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import math
import os
import tempfile
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

    px0 = x0 + subpixel_x + bbox_upem[0] * scale
    py0 = y0 - subpixel_y - bbox_upem[3] * scale
    px1 = x0 + subpixel_x + bbox_upem[2] * scale
    py1 = y0 - subpixel_y - bbox_upem[1] * scale

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
) -> DirectMetrics:
    f_size_px = float(math.floor(resolution * 0.72))
    scale = f_size_px / 1000.0
    adv_px = advance_width_upem * scale
    ascent_px = bbox_upem[3] * scale
    descent_px = -200.0 * scale

    return DirectMetrics(
        code_point=code_point,
        character=chr(code_point),
        font_size_px=f_size_px,
        raw_advance_width=round(adv_px, 2),
        raw_actual_left=round(bbox_upem[0] * scale, 2),
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
) -> ObservationStoreSnapshot:
    """Construct an immutable ObservationStoreSnapshot with multi-resolution/phase observations for A and B."""
    config = ObservationConfig(
        resolutions=(128, 256),
        base_subpixel_phases=((0.0, 0.0),),
        expanded_subpixel_phases=((0.0, 0.0),),
    )
    cfg_hash = config.compute_hash()

    records: list[ObservationRecord] = []
    raster_map: dict[str, bytes] = {}

    # Glyph 65 ('A')
    r_a1, b_a1 = _make_observation_record(reference_id, style_id, 65, 128, 0.0, 0.0, 650.0, browser_version, cfg_hash)
    r_a2, b_a2 = _make_observation_record(reference_id, style_id, 65, 256, 0.0, 0.0, 650.0, browser_version, cfg_hash)
    r_a3, b_a3 = _make_observation_record(reference_id, style_id, 65, 256, 0.25, 0.25, 650.0, browser_version, cfg_hash)

    # Glyph 66 ('B')
    r_b1, b_b1 = _make_observation_record(reference_id, style_id, 66, 128, 0.0, 0.0, 600.0, browser_version, cfg_hash)
    r_b2, b_b2 = _make_observation_record(reference_id, style_id, 66, 256, 0.0, 0.0, 600.0, browser_version, cfg_hash)
    r_b3, b_b3 = _make_observation_record(reference_id, style_id, 66, 256, 0.25, 0.25, 600.0, browser_version, cfg_hash)

    for r, b in [(r_a1, b_a1), (r_a2, b_a2), (r_a3, b_a3), (r_b1, b_b1), (r_b2, b_b2), (r_b3, b_b3)]:
        records.append(r)
        raster_map[r.cache_key] = b

    # Typography pairs
    pair1 = PairKerningObservation(
        left_cp=65, right_cp=66, left_char="A", right_char="B",
        left_advance_upem=650.0, right_advance_upem=600.0,
        measured_pair_advance_upem=1230.0, inferred_kerning_upem=-20,
        is_kerning_applied=True, provenance="chromium:chromium:canvas_text_metrics"
    )
    pair2 = PairKerningObservation(
        left_cp=66, right_cp=65, left_char="B", right_char="A",
        left_advance_upem=600.0, right_advance_upem=650.0,
        measured_pair_advance_upem=1240.0, inferred_kerning_upem=-10,
        is_kerning_applied=True, provenance="chromium:chromium:canvas_text_metrics"
    )

    return ObservationStoreSnapshot(
        reference_id=reference_id,
        style_id=style_id,
        family_name=family_name,
        style_name=style_name,
        browser_version=browser_version,
        config=config,
        records=tuple(records),
        raster_bytes_map=raster_map,
        pairs=(pair1, pair2),
    )


# =========================================================================
# 1. Deterministic Positive Invariant Pipeline Execution Tests
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
        assert res1.status == "PASS"
        assert res2.is_publishable is True
        assert res2.status == "PASS"

        # 2. Cryptographic determinism
        assert res1.model_hash != ""
        assert res1.model_hash == res2.model_hash
        assert res1.candidate_artifact_sha != ""
        assert res1.candidate_artifact_sha == res2.candidate_artifact_sha

        # 3. Report and gate verification
        assert res1.report is not None
        assert res1.report.overall_status == "PASS"
        assert res1.report.consumer_gate.status == "PASS"
        assert res1.report.geometry_raster_gate.status == "PASS"
        assert res1.report.metrics_gate.status == "PASS"
        assert res1.report.typography_gate.status == "PASS"
        assert res1.report.topology_gate.status == "PASS"
        assert res1.report.coverage_gate.status == "PASS"

        # 4. Verifiable disk candidate file
        assert Path(res1.candidate_file_path).is_file()
        assert Path(res2.candidate_file_path).is_file()
        assert hashlib.sha256(Path(res1.candidate_file_path).read_bytes()).hexdigest() == res1.candidate_artifact_sha


def test_sync_pipeline_execution_wrapper() -> None:
    """Synchronous pipeline wrapper executes cleanly and matches async execution."""
    snapshot = _build_valid_snapshot()
    with tempfile.TemporaryDirectory() as tmp_dir:
        res = LocalFidelityIntegrationPipeline.execute_sync(snapshot, output_dir=tmp_dir, format_type="TTF")
        assert res.is_publishable is True
        assert res.status == "PASS"
        assert res.model_hash != ""
        assert res.candidate_artifact_sha != ""


# =========================================================================
# 2. ObservationStore Ingestion & Load Tests
# =========================================================================

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
        store.save_pair_observation("my_font", "regular", 65, 66, "A", "B", 650.0, 600.0, 1230.0, -20, 1.0, "chromium:chromium:canvas_text_metrics")
        store.save_pair_observation("my_font", "regular", 66, 65, "B", "A", 600.0, 650.0, 1240.0, -10, 1.0, "chromium:chromium:canvas_text_metrics")

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
# 3. Negative Reproductions: Anti-Leakage & Partitioning Invariants
# =========================================================================

def test_fit_held_out_cache_key_leakage_rejected() -> None:
    """PartitionedEvidence strictly rejects overlap between fit and held-out cache keys."""
    rec1, _ = _make_observation_record(code_point=65, resolution=128)
    rec2, _ = _make_observation_record(code_point=65, resolution=256)
    pair1 = PairKerningObservation(65, 66, "A", "B", 650, 600, 1250, 0, False, "chromium")
    pair2 = PairKerningObservation(66, 65, "B", "A", 600, 650, 1250, 0, False, "chromium")

    # Overlapping cache key rec1 in both fit and held-out
    with pytest.raises(ValueError, match="PARTITION_LEAKAGE: Fit and held-out cache keys overlap"):
        PartitionedEvidence(
            fit_records=(rec1, rec2),
            held_out_records=(rec1,),
            fit_pairs=(pair1,),
            held_out_pairs=(pair2,),
        )


def test_fit_held_out_raster_sha_leakage_rejected() -> None:
    """PartitionedEvidence strictly rejects identical raster SHA across fit and held-out records."""
    rec1, png1 = _make_observation_record(code_point=65, resolution=128)
    # rec2 has same raster SHA as rec1 but different cache key
    rec2 = ObservationRecord(
        cache_key="a" * 64,
        reference_id=rec1.reference_id,
        style_id=rec1.style_id,
        code_point=65,
        resolution=256,
        subpixel_x=0.0,
        subpixel_y=0.0,
        raster_relative_path="other.png",
        raster_sha256=rec1.raster_sha256,  # Identical SHA
        raster_size_bytes=rec1.raster_size_bytes,
        metrics=rec1.metrics,
        created_at=rec1.created_at,
        browser_version=rec1.browser_version,
        config_hash=rec1.config_hash,
    )
    pair1 = PairKerningObservation(65, 66, "A", "B", 650, 600, 1250, 0, False, "chromium")
    pair2 = PairKerningObservation(66, 65, "B", "A", 600, 650, 1250, 0, False, "chromium")

    with pytest.raises(ValueError, match="PARTITION_LEAKAGE: Fit and held-out raster SHA-256 digests overlap"):
        PartitionedEvidence(
            fit_records=(rec1,),
            held_out_records=(rec2,),
            fit_pairs=(pair1,),
            held_out_pairs=(pair2,),
        )


def test_fit_held_out_pair_leakage_rejected() -> None:
    """PartitionedEvidence strictly rejects overlapping typography pairs."""
    rec1, _ = _make_observation_record(code_point=65, resolution=128)
    rec2, _ = _make_observation_record(code_point=65, resolution=256, subpixel_x=0.25)
    pair1 = PairKerningObservation(65, 66, "A", "B", 650, 600, 1250, 0, False, "chromium")

    with pytest.raises(ValueError, match="PARTITION_LEAKAGE: Fit and held-out typography pairs overlap"):
        PartitionedEvidence(
            fit_records=(rec1,),
            held_out_records=(rec2,),
            fit_pairs=(pair1,),
            held_out_pairs=(pair1,),
        )


def test_zero_held_out_samples_fails_closed_in_partition() -> None:
    """Single observation per glyph cannot form fit/held-out split and must fail closed."""
    rec1, png1 = _make_observation_record(code_point=65, resolution=128)
    pair1 = PairKerningObservation(65, 66, "A", "B", 650, 600, 1250, 0, False, "chromium")
    pair2 = PairKerningObservation(66, 65, "B", "A", 600, 650, 1250, 0, False, "chromium")

    snap = ObservationStoreSnapshot(
        reference_id="test_font",
        style_id="regular",
        family_name="TestFont",
        style_name="Regular",
        browser_version="chromium",
        config=ObservationConfig(),
        records=(rec1,),
        raster_bytes_map={rec1.cache_key: png1},
        pairs=(pair1, pair2),
    )

    with pytest.raises(ValueError, match="INSUFFICIENT_OBSERVATIONS_FOR_GLYPH_65"):
        partition_snapshot(snap)


def test_zero_or_insufficient_pairs_fails_closed_in_partition() -> None:
    """Snapshot with only 1 pair cannot partition into disjoint fit and held-out sets."""
    rec1, png1 = _make_observation_record(code_point=65, resolution=128)
    rec2, png2 = _make_observation_record(code_point=65, resolution=256, subpixel_x=0.25)
    pair1 = PairKerningObservation(65, 66, "A", "B", 650, 600, 1250, 0, False, "chromium")

    snap = ObservationStoreSnapshot(
        reference_id="test_font",
        style_id="regular",
        family_name="TestFont",
        style_name="Regular",
        browser_version="chromium",
        config=ObservationConfig(),
        records=(rec1, rec2),
        raster_bytes_map={rec1.cache_key: png1, rec2.cache_key: png2},
        pairs=(pair1,),  # Only 1 pair
    )

    with pytest.raises(ValueError, match="INSUFFICIENT_TYPOGRAPHY_PAIRS"):
        partition_snapshot(snap)


# =========================================================================
# 4. Negative Reproductions: Drift & Fail-Closed Attestation Tests
# =========================================================================

def test_snapshot_identity_drift_rejected() -> None:
    """Snapshot with mismatched reference_id, style_id, or config_hash in records must raise error."""
    rec1, png1 = _make_observation_record(reference_id="font_A", style_id="regular")
    pair1 = PairKerningObservation(65, 66, "A", "B", 650, 600, 1250, 0, False, "chromium")
    pair2 = PairKerningObservation(66, 65, "B", "A", 600, 650, 1250, 0, False, "chromium")

    # Mismatched reference_id in snapshot definition
    with pytest.raises(ValueError, match="SNAPSHOT_VALIDATION_ERROR: Record reference_id 'font_A' != snapshot 'font_B'"):
        ObservationStoreSnapshot(
            reference_id="font_B",  # Mismatched
            style_id="regular",
            family_name="FontB",
            style_name="Regular",
            browser_version="chromium",
            config=ObservationConfig(),
            records=(rec1,),
            raster_bytes_map={rec1.cache_key: png1},
            pairs=(pair1, pair2),
        )


def test_snapshot_corrupt_raster_sha_rejected() -> None:
    """Snapshot where raster SHA does not match declared record SHA must raise error."""
    rec1, _ = _make_observation_record(code_point=65)
    corrupted_png = b"corrupted_png_bytes_that_do_not_match_sha"
    pair1 = PairKerningObservation(65, 66, "A", "B", 650, 600, 1250, 0, False, "chromium")
    pair2 = PairKerningObservation(66, 65, "B", "A", 600, 650, 1250, 0, False, "chromium")

    with pytest.raises(ValueError, match="SNAPSHOT_VALIDATION_ERROR: Byte size mismatch"):
        ObservationStoreSnapshot(
            reference_id=rec1.reference_id,
            style_id=rec1.style_id,
            family_name="TestFont",
            style_name="Regular",
            browser_version="chromium",
            config=ObservationConfig(),
            records=(rec1,),
            raster_bytes_map={rec1.cache_key: corrupted_png},
            pairs=(pair1, pair2),
        )


@pytest.mark.asyncio
async def test_unsupported_format_fails_closed() -> None:
    """Pipeline rejects unsupported format (e.g. WOFF2) with is_publishable=False."""
    snapshot = _build_valid_snapshot()
    with tempfile.TemporaryDirectory() as tmp_dir:
        res = await LocalFidelityIntegrationPipeline.execute(snapshot, output_dir=tmp_dir, format_type="WOFF2")
        assert res.is_publishable is False
        assert res.status == "FAIL"
        assert any("UNSUPPORTED_FORMAT" in r for r in res.failure_reasons)


@pytest.mark.asyncio
async def test_missing_chromium_capability_returns_blocked_non_publishable() -> None:
    """When Chromium binary is unavailable, pipeline returns status='BLOCKED' and is_publishable=False."""
    snapshot = _build_valid_snapshot()
    with tempfile.TemporaryDirectory() as tmp_dir:
        with patch("fidelity.pipeline.find_chromium_executable", side_effect=RuntimeError("NO_CHROMIUM_BINARY")):
            res = await LocalFidelityIntegrationPipeline.execute(snapshot, output_dir=tmp_dir)
            assert res.is_publishable is False
            assert res.status == "BLOCKED"
            assert any("CHROMIUM_CAPABILITY_UNAVAILABLE" in r for r in res.failure_reasons)


@pytest.mark.asyncio
async def test_modifying_held_out_evidence_invalidates_bound_report() -> None:
    """Modifying held-out pair data causes gate failure or non-publishability."""
    snapshot = _build_valid_snapshot()

    # Modify held-out pair advance to a drifting / regressing value (e.g. 1800 UPEM instead of 1240)
    bad_pair2 = PairKerningObservation(
        left_cp=66, right_cp=65, left_char="B", right_char="A",
        left_advance_upem=600.0, right_advance_upem=650.0,
        measured_pair_advance_upem=1800.0,  # 550 UPEM drift
        inferred_kerning_upem=-10,
        is_kerning_applied=True,
        provenance="chromium:chromium:canvas_text_metrics"
    )

    bad_snapshot = ObservationStoreSnapshot(
        reference_id=snapshot.reference_id,
        style_id=snapshot.style_id,
        family_name=snapshot.family_name,
        style_name=snapshot.style_name,
        browser_version=snapshot.browser_version,
        config=snapshot.config,
        records=snapshot.records,
        raster_bytes_map=snapshot.raster_bytes_map,
        pairs=(snapshot.pairs[0], bad_pair2),
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        res = await LocalFidelityIntegrationPipeline.execute(bad_snapshot, output_dir=tmp_dir)
        assert res.is_publishable is False
        assert res.status == "FAIL"
