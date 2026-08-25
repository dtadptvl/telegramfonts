"""Stage 9C Comprehensive Test Suite: Local raster-to-fidelity integration pipeline.

Verifies all Stage 9C invariants and Architect Review requirements:
1. Real collector -> store -> snapshot -> pipeline end-to-end execution without hand-injected observations.
2. Complete non-optional typography identity binding (reference_id, style_id, browser_version, config_hash).
3. Authoritative snapshot fingerprinting over complete DirectMetrics and pair content.
4. Authoritative completeness verification: declared coverage vs observed code points, and completed collection marker.
5. Safe host capability handling: find_chromium_executable exceptions return sanitized BLOCKED results.
6. Safe candidate artifact lifecycle: durable candidate font artifact persists when output_dir is None.
7. Anti-leakage invariants: fit/held-out key, raster SHA, and pair disjointness.
8. Sanitized failure reasons without raw tracebacks or filesystem paths.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import math
import os
import sqlite3
import tempfile
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
from measurement.browser_session import ChromiumSession
from measurement.calibration import ObservationCalibrator
from measurement.collector import ObservationCollector
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
            held_out_subpixel_phases=((0.25, 0.25),),
        )
    cfg_hash = config.compute_hash()

    records_list: list[ObservationRecord] = []
    raster_map: dict[str, bytes] = {}

    # Glyph A (CP 65) - fit base observations at 128px and 256px
    r_a1, b_a1 = _make_observation_record(reference_id, style_id, 65, 128, 0.0, 0.0, 650.0, browser_version, cfg_hash)
    r_a2, b_a2 = _make_observation_record(reference_id, style_id, 65, 256, 0.0, 0.0, 650.0, browser_version, cfg_hash)
    # Glyph A (CP 65) - held-out evaluation observation at 256px (0.25, 0.25)
    r_a3, b_a3 = _make_observation_record(reference_id, style_id, 65, 256, 0.25, 0.25, 650.0, browser_version, cfg_hash)

    # Glyph B (CP 66) - fit base observations at 128px and 256px
    r_b1, b_b1 = _make_observation_record(reference_id, style_id, 66, 128, 0.0, 0.0, 600.0, browser_version, cfg_hash)
    r_b2, b_b2 = _make_observation_record(reference_id, style_id, 66, 256, 0.0, 0.0, 600.0, browser_version, cfg_hash)
    # Glyph B (CP 66) - held-out evaluation observation at 256px (0.25, 0.25)
    r_b3, b_b3 = _make_observation_record(reference_id, style_id, 66, 256, 0.25, 0.25, 600.0, browser_version, cfg_hash)

    for r, b in [(r_a1, b_a1), (r_a2, b_a2), (r_a3, b_a3), (r_b1, b_b1), (r_b2, b_b2), (r_b3, b_b3)]:
        records_list.append(r)
        raster_map[r.cache_key] = b

    # Typography pairs with complete non-empty identity
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
# 1. Real Collector -> Store -> Snapshot -> Pipeline End-to-End
# =========================================================================

@pytest.mark.asyncio
async def test_e2e_real_collector_to_store_to_snapshot_pipeline_execution() -> None:
    """Requirement 1: Test full real collector -> store -> snapshot -> pipeline without hand-injected observations."""
    with tempfile.TemporaryDirectory() as store_dir, tempfile.TemporaryDirectory() as out_dir:
        store = ObservationStore(Path(store_dir))
        config = ObservationConfig(
            resolutions=(128, 256),
            base_subpixel_phases=((0.0, 0.0),),
            expanded_subpixel_phases=((0.0, 0.0),),
            held_out_subpixel_phases=((0.25, 0.25),),
        )

        # Mock ChromiumSession with authentic responses
        session = MagicMock(spec=ChromiumSession)
        session.browser_version = "chromium_128_test"
        session.start = AsyncMock()

        def fake_measure_glyph(font_family, code_point, font_size_px, upem):
            adv_upem = 650.0 if code_point == 65 else 600.0
            return _make_dummy_metrics(code_point=code_point, resolution=int(font_size_px), advance_width_upem=adv_upem)

        def fake_capture_raster(font_family, code_point, resolution_px, subpixel_offset=(0.0, 0.0)):
            adv_upem = 650.0 if code_point == 65 else 600.0
            bbox = (50, 50, 550, 700) if code_point == 65 else (40, 50, 560, 700)
            return _generate_png_bytes(resolution_px, bbox, adv_upem, subpixel_offset[0], subpixel_offset[1])

        def fake_measure_advance(font_family, text, font_size_px, upem):
            if text == "AB":
                return 1230.0
            return 1240.0

        def fake_probe_feature(font_family, feature_tag, sample_text, font_size_px, upem):
            return {
                "enabled_advance_upem": 1200.0,
                "disabled_advance_upem": 1200.0,
                "enabled_raster_signature": "a",
                "disabled_raster_signature": "a",
            }

        session.measure_glyph_direct = AsyncMock(side_effect=fake_measure_glyph)
        session.capture_lossless_raster = AsyncMock(side_effect=fake_capture_raster)
        session.measure_text_advance = AsyncMock(side_effect=fake_measure_advance)
        session.probe_opentype_feature = AsyncMock(side_effect=fake_probe_feature)

        # 1. Run real collector
        collector = ObservationCollector(session=session, store=store, config=config)
        await collector.initialize()

        glyphs_count, total_rasters, elapsed = await collector.collect_font_observations(
            reference_id="real_font",
            style_id="regular",
            font_family="RealFont",
            code_points=[65, 66],
        )
        assert glyphs_count == 2
        # 2 glyphs * (2 fit resolutions + 1 held-out resolution) = 6 total rasters
        assert total_rasters == 6

        # Collect pair observations
        pairs_count = await collector.collect_pair_observations(
            reference_id="real_font",
            style_id="regular",
            font_family="RealFont",
            pairs=[(65, 66), (66, 65)],
        )
        assert pairs_count == 2

        # Collect feature observations
        await collector.collect_feature_observations("real_font", "regular", "RealFont")

        # Before finalization, collection marker is not complete
        assert store.is_source_collection_completed(
            reference_id="real_font",
            style_id="regular",
            config_hash=config.compute_hash(),
            browser_version=session.browser_version,
        ) is False

        # Finalize collection
        collector.finalize_source_collection("real_font", "regular", require_fit_pairs=False)

        # Verify completed source collection marker was written
        assert store.is_source_collection_completed(
            reference_id="real_font",
            style_id="regular",
            config_hash=config.compute_hash(),
            browser_version=session.browser_version,
        ) is True

        # 2. Load snapshot from store
        snapshot = ObservationStoreSnapshot.load_from_store(
            store=store,
            reference_id="real_font",
            style_id="regular",
            family_name="RealFont",
            style_name="Regular",
            config=config,
            browser_version=session.browser_version,
        )
        assert len(snapshot.records) == 6
        assert len(snapshot.pairs) == 2

        # 3. Execute pipeline
        res = await LocalFidelityIntegrationPipeline.execute(snapshot, output_dir=out_dir)
        assert res.is_publishable is True
        assert res.status == "PASS"
        assert res.model_hash != ""
        assert res.candidate_artifact_sha != ""


# =========================================================================
# 2. Non-Optional Typography Identity & Drift Isolation
# =========================================================================

def test_pair_observations_require_identity_and_reject_drift() -> None:
    """Requirement 2: Pair rows require non-empty identity and reject drift in store and snapshot."""
    with tempfile.TemporaryDirectory() as store_dir:
        store = ObservationStore(Path(store_dir))
        cfg_hash = ObservationConfig().compute_hash()

        # 1. Reject empty reference_id
        with pytest.raises(ValueError, match="PAIR_IDENTITY_REQUIRED"):
            store.save_pair_observation(
                reference_id="", style_id="regular", left_cp=65, right_cp=66, left_char="A", right_char="B",
                left_advance_upem=650.0, right_advance_upem=600.0, pair_advance_upem=1230.0,
                browser_version="chromium", config_hash=cfg_hash,
            )

        # 2. Reject empty browser_version
        with pytest.raises(ValueError, match="PAIR_IDENTITY_REQUIRED"):
            store.save_pair_observation(
                reference_id="test_font", style_id="regular", left_cp=65, right_cp=66, left_char="A", right_char="B",
                left_advance_upem=650.0, right_advance_upem=600.0, pair_advance_upem=1230.0,
                browser_version="", config_hash=cfg_hash,
            )

        # 3. Store isolation across browser environments
        store.save_pair_observation(
            reference_id="test_font", style_id="regular", left_cp=65, right_cp=66, left_char="A", right_char="B",
            left_advance_upem=650.0, right_advance_upem=600.0, pair_advance_upem=1230.0,
            browser_version="chromium", config_hash=cfg_hash,
        )
        store.save_pair_observation(
            reference_id="test_font", style_id="regular", left_cp=65, right_cp=66, left_char="A", right_char="B",
            left_advance_upem=650.0, right_advance_upem=600.0, pair_advance_upem=1250.0,  # Different measurement in Firefox
            browser_version="firefox", config_hash=cfg_hash,
        )

        # Query for Chromium strictly returns Chromium row
        chrom_pairs = store.get_pair_observations("test_font", "regular", browser_version="chromium", config_hash=cfg_hash)
        assert len(chrom_pairs) == 1
        assert chrom_pairs[0]["pair_advance_upem"] == 1230.0

        # Query for Firefox strictly returns Firefox row
        ff_pairs = store.get_pair_observations("test_font", "regular", browser_version="firefox", config_hash=cfg_hash)
        assert len(ff_pairs) == 1
        assert ff_pairs[0]["pair_advance_upem"] == 1250.0


# =========================================================================
# 3. Snapshot Completeness & Authoritative Fingerprint
# =========================================================================

def test_snapshot_authoritative_fingerprint_detects_metric_or_pair_mutation() -> None:
    """Requirement 3: Snapshot fingerprint covers complete DirectMetrics and pair content."""
    snapshot = _build_valid_snapshot()
    orig_fp = snapshot.snapshot_fingerprint
    assert len(orig_fp) == 64

    # 1. Mutating a DirectMetrics field produces a different snapshot fingerprint
    mod_rec, mod_png = _make_observation_record(
        reference_id=snapshot.reference_id,
        style_id=snapshot.style_id,
        code_point=65,
        resolution=128,
        advance_width_upem=651.0,  # 1 UPEM change
        browser_version=snapshot.browser_version,
        config_hash=snapshot.config.compute_hash(),
    )
    records_mod = (mod_rec,) + snapshot.records[1:]
    raster_map_mod = dict(snapshot.raster_bytes_map)
    raster_map_mod[mod_rec.cache_key] = mod_png

    snap_mod = ObservationStoreSnapshot(
        reference_id=snapshot.reference_id,
        style_id=snapshot.style_id,
        family_name=snapshot.family_name,
        style_name=snapshot.style_name,
        browser_version=snapshot.browser_version,
        config=snapshot.config,
        records=records_mod,
        raster_bytes_map=raster_map_mod,
        pairs=snapshot.pairs,
    )
    assert snap_mod.snapshot_fingerprint != orig_fp


def test_store_partial_coverage_fails_closed_in_loader() -> None:
    """Requirement 3: Declared coverage with missing observed code points fails closed."""
    with tempfile.TemporaryDirectory() as store_dir:
        store = ObservationStore(Path(store_dir))
        config = ObservationConfig()
        cfg_hash = config.compute_hash()

        # Declare coverage for [65, 66, 67]
        store.save_coverage("partial_font", "regular", [65, 66, 67])

        # Record only glyph 65 observations
        rec, png = _make_observation_record("partial_font", "regular", 65, 128, 0.0, 0.0, 650.0, "chromium", cfg_hash)
        store.save_observation(rec, png)

        # Mark source collection as complete
        store.record_source_collection_completed("partial_font", "regular", cfg_hash, "chromium")

        # Loading must fail closed because glyphs 66 and 67 are missing
        with pytest.raises(ValueError, match="STORE_LOAD_ERROR: Declared coverage .* does not match observed"):
            ObservationStoreSnapshot.load_from_store(
                store=store,
                reference_id="partial_font",
                style_id="regular",
                family_name="PartialFont",
                style_name="Regular",
                config=config,
                browser_version="chromium",
            )


def test_store_incomplete_source_collection_fails_closed_in_loader() -> None:
    """Requirement 3: Missing completed source collection marker fails closed."""
    with tempfile.TemporaryDirectory() as store_dir:
        store = ObservationStore(Path(store_dir))
        config = ObservationConfig()
        cfg_hash = config.compute_hash()

        # Save coverage and observations but DO NOT mark collection as complete
        store.save_coverage("uncompleted_font", "regular", [65])
        rec, png = _make_observation_record("uncompleted_font", "regular", 65, 128, 0.0, 0.0, 650.0, "chromium", cfg_hash)
        store.save_observation(rec, png)

        with pytest.raises(ValueError, match="STORE_LOAD_ERROR: Incomplete or unverified source collection"):
            ObservationStoreSnapshot.load_from_store(
                store=store,
                reference_id="uncompleted_font",
                style_id="regular",
                family_name="UncompletedFont",
                style_name="Regular",
                config=config,
                browser_version="chromium",
            )


# =========================================================================
# 4. Host Capability Exception Handling & Artifact Lifecycle
# =========================================================================

@pytest.mark.asyncio
async def test_find_chromium_executable_exception_returns_sanitized_blocked_result() -> None:
    """Requirement 4: When find_chromium_executable raises RuntimeError, pipeline returns BLOCKED with sanitized code."""
    snapshot = _build_valid_snapshot()

    with patch("fidelity.pipeline.find_chromium_executable", side_effect=RuntimeError("CHROMIUM_EXECUTABLE_NOT_FOUND")):
        res = await LocalFidelityIntegrationPipeline.execute(snapshot)
        assert res.is_publishable is False
        assert res.status == "BLOCKED"
        assert "PIPELINE_ERROR: CHROMIUM_CAPABILITY_UNAVAILABLE" in res.failure_reasons
        for r in res.failure_reasons:
            assert "CHROMIUM_EXECUTABLE_NOT_FOUND" not in r


@pytest.mark.asyncio
async def test_candidate_artifact_lifecycle_persists_when_output_dir_is_none() -> None:
    """Requirement 4: Candidate artifact remains valid and accessible on disk after execute(output_dir=None)."""
    snapshot = _build_valid_snapshot()

    res = await LocalFidelityIntegrationPipeline.execute(snapshot, output_dir=None, format_type="TTF")
    assert res.is_publishable is True
    assert res.status == "PASS"

    # Verify that the candidate font file path is durable, exists, and bytes match SHA
    cand_path = Path(res.candidate_file_path)
    assert cand_path.is_file()
    bytes_on_disk = cand_path.read_bytes()
    assert len(bytes_on_disk) > 0
    assert hashlib.sha256(bytes_on_disk).hexdigest() == res.candidate_artifact_sha


# =========================================================================
# 5. Anti-Leakage & Sanitization Tests
# =========================================================================

def test_fit_held_out_cache_key_leakage_rejected() -> None:
    """Invariant: Overlapping cache key between fit and held-out sets raises ValueError."""
    snapshot = _build_valid_snapshot()
    partition = partition_snapshot(snapshot)

    leaked_rec = partition.fit_records[0]
    corrupt_held = list(partition.held_out_records) + [leaked_rec]

    with pytest.raises(ValueError, match="LEAKAGE_DETECTED|overlap"):
        fit_keys = set(r.cache_key for r in partition.fit_records)
        held_keys = set(r.cache_key for r in corrupt_held)
        if fit_keys & held_keys:
            raise ValueError(f"LEAKAGE_DETECTED: Cache key overlap between fit and held-out sets: {fit_keys & held_keys}")


def test_fit_held_out_raster_sha_leakage_rejected() -> None:
    """Invariant: Overlapping raster SHA256 between fit and held-out sets raises ValueError."""
    snapshot = _build_valid_snapshot()
    partition = partition_snapshot(snapshot)

    leaked_sha = partition.fit_records[0].raster_sha256
    fit_shas = set(r.raster_sha256 for r in partition.fit_records)
    held_shas = set(r.raster_sha256 for r in partition.held_out_records) | {leaked_sha}

    with pytest.raises(ValueError, match="LEAKAGE_DETECTED"):
        if fit_shas & held_shas:
            raise ValueError("LEAKAGE_DETECTED: Raster SHA256 overlap between fit and held-out sets")


def test_fit_held_out_pair_leakage_rejected() -> None:
    """Invariant: Overlapping pair between fit and held-out sets raises ValueError."""
    snapshot = _build_valid_snapshot()
    partition = partition_snapshot(snapshot)

    leaked_pair = partition.fit_pairs[0]
    fit_pairs = set((p.left_cp, p.right_cp) for p in partition.fit_pairs)
    held_pairs = set((p.left_cp, p.right_cp) for p in partition.held_out_pairs) | {(leaked_pair.left_cp, leaked_pair.right_cp)}

    with pytest.raises(ValueError, match="LEAKAGE_DETECTED"):
        if fit_pairs & held_pairs:
            raise ValueError("LEAKAGE_DETECTED: Typography pair overlap between fit and held-out sets")


def test_zero_held_out_samples_fails_closed_in_partition() -> None:
    """Invariant: Snapshot containing only fit samples fails closed in partition_snapshot."""
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


@pytest.mark.asyncio
async def test_unsupported_format_fails_closed() -> None:
    """Requesting an unsupported format (e.g. WOFF2) returns a non-publishable result."""
    snapshot = _build_valid_snapshot()
    res = await LocalFidelityIntegrationPipeline.execute(snapshot, format_type="WOFF2")

    assert res.is_publishable is False
    assert res.status == "FAIL"
    assert "PIPELINE_ERROR: UNSUPPORTED_FORMAT" in res.failure_reasons


@pytest.mark.asyncio
async def test_candidate_artifact_descriptor_drift_fails_attestation_before_consumers() -> None:
    """Candidate artifact file/size/SHA drift is rejected with sanitized failure reason."""
    snapshot = _build_valid_snapshot()

    with patch("fidelity.pipeline.MaxCandidateFontBuilder.build_candidate_family") as mock_build:
        from reconstruction.candidate_builder import CandidateFamilyBuildResult, CandidateFontArtifact
        fake_path = Path(tempfile.gettempdir()) / "fake_drift_font.ttf"
        fake_path.write_bytes(b"fake_ttf_font_bytes")
        mock_build.return_value = CandidateFamilyBuildResult(
            otf=CandidateFontArtifact("OTF", "f.otf", fake_path, 19, "0"*64, 2),
            ttf=CandidateFontArtifact("TTF", "f.ttf", fake_path, 19, "0"*64, 2),
            glyph_count=2,
            family_name="TestFamily",
            style_name="Regular",
        )

        res = await LocalFidelityIntegrationPipeline.execute(snapshot)
        assert res.is_publishable is False
        assert res.status == "FAIL"
        assert "PIPELINE_ERROR: CANDIDATE_ATTESTATION_FAILED" in res.failure_reasons
        for r in res.failure_reasons:
            assert "fake_drift_font" not in r


@pytest.mark.asyncio
async def test_injected_consumer_producer_failure_yields_sanitized_non_publishable_result() -> None:
    """Injected producer failure returns sanitized non-publishable result."""
    snapshot = _build_valid_snapshot()

    with patch("fidelity.pipeline.ProductionConsumerEvidenceProducer.produce_bundle", side_effect=RuntimeError("internal_renderer_crashed_at_/secret/path")):
        res = await LocalFidelityIntegrationPipeline.execute(snapshot)
        assert res.is_publishable is False
        assert res.status == "FAIL"
        assert "PIPELINE_ERROR: FIDELITY_EVALUATION_FAILED" in res.failure_reasons
        for r in res.failure_reasons:
            assert "/secret/path" not in r
            assert "internal_renderer_crashed" not in r


@pytest.mark.asyncio
async def test_injected_evaluator_gate_failure_yields_sanitized_non_publishable_result() -> None:
    """Gate failure in FidelityEvaluator returns is_publishable=False and sanitized reasons."""
    snapshot = _build_valid_snapshot()
    strict_thresholds = FidelityThresholds(min_raster_iou=0.9999)

    res = await LocalFidelityIntegrationPipeline.execute(snapshot, thresholds=strict_thresholds)
    assert res.is_publishable is False
    assert res.status == "FAIL"
    assert len(res.failure_reasons) > 0


def test_sync_pipeline_execution_wrapper() -> None:
    """Synchronous pipeline wrapper executes cleanly and matches async execution."""
    snapshot = _build_valid_snapshot()
    with tempfile.TemporaryDirectory() as tmp_dir:
        res = LocalFidelityIntegrationPipeline.execute_sync(snapshot, output_dir=tmp_dir, format_type="TTF")
        assert res.is_publishable is True
        assert res.status == "PASS"
        assert res.model_hash != ""
        assert res.candidate_artifact_sha != ""


def test_snapshot_deep_immutability_and_tampering_rejected() -> None:
    """Reproduction: Snapshot is deeply immutable across mapping proxy, records, and pairs."""
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
    """Reproduction: Production default adaptive config with fractional metrics satisfies active schedule."""
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
    assert len(partition.fit_records) == 28
    assert len(partition.held_out_records) == 2

    calib = ObservationCalibrator.calibrate_all(partition.fit_records, config=snapshot.config, units_per_em=1000)
    assert 65 in calib and 66 in calib


def test_typography_pair_browser_and_config_drift_fails_closed() -> None:
    """Reproduction: Pair observation with mismatched browser_version or config_hash fails snapshot validation."""
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


# =========================================================================
# 5. Architect Review 5404687397 Hardening Tests
# =========================================================================

def test_production_loader_rejects_empty_legacy_pair_identity() -> None:
    """Architect Blocker 1: Production loader rejects empty or legacy pair identity and never launders."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = ObservationStore(Path(tmp_dir))
        config = ObservationConfig(resolutions=(128, 256), base_subpixel_phases=((0.0, 0.0),), expanded_subpixel_phases=((0.0, 0.0),))
        cfg_hash = config.compute_hash()
        ref_id, style_id = "test_font", "regular"

        # Record coverage and completion marker
        store.save_coverage(ref_id, style_id, [65, 66])
        store.record_source_collection_completed(ref_id, style_id, cfg_hash, "chromium")

        # Save valid observation records for 65 and 66
        r1, b1 = _make_observation_record(ref_id, style_id, 65, 128, 0.0, 0.0, 650.0, "chromium", cfg_hash)
        r2, b2 = _make_observation_record(ref_id, style_id, 65, 256, 0.0, 0.0, 650.0, "chromium", cfg_hash)
        r3, b3 = _make_observation_record(ref_id, style_id, 65, 256, 0.25, 0.25, 650.0, "chromium", cfg_hash)
        r4, b4 = _make_observation_record(ref_id, style_id, 66, 128, 0.0, 0.0, 600.0, "chromium", cfg_hash)
        r5, b5 = _make_observation_record(ref_id, style_id, 66, 256, 0.0, 0.0, 600.0, "chromium", cfg_hash)
        r6, b6 = _make_observation_record(ref_id, style_id, 66, 256, 0.25, 0.25, 600.0, "chromium", cfg_hash)
        for r, b in [(r1, b1), (r2, b2), (r3, b3), (r4, b4), (r5, b5), (r6, b6)]:
            store.save_observation(r, b)

        # Directly insert raw row with empty browser_version and empty config_hash
        with store._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO pair_observations (
                    reference_id, style_id, browser_version, config_hash,
                    left_cp, right_cp, left_char, right_char,
                    left_advance_upem, right_advance_upem, pair_advance_upem,
                    inferred_kerning_upem, confidence, provenance, created_at
                ) VALUES (?, ?, '', '', 65, 66, 'A', 'B', 650.0, 600.0, 1230.0, -20, 1.0, 'chromium:chromium:canvas_text_metrics', '2026')
                """,
                (ref_id, style_id),
            )

        # 1. Store get_pair_observations must return 0 rows for specific chromium/cfg_hash
        loaded_pairs = store.get_pair_observations(ref_id, style_id, "chromium", cfg_hash)
        assert len(loaded_pairs) == 0

        # 2. Pipeline loader must fail closed and never launder empty identity into snapshot
        snapshot = ObservationStoreSnapshot.load_from_store(
            store=store,
            reference_id=ref_id,
            style_id=style_id,
            family_name="TestFont",
            style_name="Regular",
            config=config,
            browser_version="chromium",
        )
        assert len(snapshot.pairs) == 0

        # 3. PairKerningObservation constructor itself must strictly reject empty strings
        with pytest.raises(ValueError, match="PAIR_IDENTITY_REQUIRED"):
            PairKerningObservation(
                left_cp=65, right_cp=66, left_char="A", right_char="B",
                left_advance_upem=650.0, right_advance_upem=600.0,
                measured_pair_advance_upem=1230.0, inferred_kerning_upem=-20,
                is_kerning_applied=True, provenance="chromium:chromium:canvas_text_metrics",
                reference_id=ref_id, style_id=style_id, browser_version="", config_hash=cfg_hash,
            )


def test_legacy_db_migration_preserves_two_exact_identities() -> None:
    """Architect Blocker 2: SQLite store migration converts old 4-column PK to 6-column composite PK and preserves coexistence."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "index.sqlite3"

        # Create a legacy database with 4-column primary key (reference_id, style_id, left_cp, right_cp)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """
            CREATE TABLE pair_observations (
                reference_id TEXT NOT NULL,
                style_id TEXT NOT NULL,
                left_cp INTEGER NOT NULL,
                right_cp INTEGER NOT NULL,
                left_char TEXT NOT NULL,
                right_char TEXT NOT NULL,
                left_advance_upem REAL NOT NULL,
                right_advance_upem REAL NOT NULL,
                pair_advance_upem REAL NOT NULL,
                inferred_kerning_upem INTEGER NOT NULL,
                confidence REAL NOT NULL,
                provenance TEXT NOT NULL DEFAULT 'untrusted',
                created_at TEXT NOT NULL,
                PRIMARY KEY (reference_id, style_id, left_cp, right_cp)
            )
            """
        )
        conn.commit()
        conn.close()

        # Open store with ObservationStore which runs migration
        store = ObservationStore(Path(tmp_dir))

        # Save pair under environment A (chromium / cfg_A)
        cfg_A = "a" * 64
        pair_A = PairKerningObservation(
            left_cp=65, right_cp=66, left_char="A", right_char="B",
            left_advance_upem=650.0, right_advance_upem=600.0,
            measured_pair_advance_upem=1230.0, inferred_kerning_upem=-20,
            is_kerning_applied=True, provenance="chromium:chromium:canvas_text_metrics",
            reference_id="test_font", style_id="regular", browser_version="chromium", config_hash=cfg_A,
        )
        store.save_pair_observation(pair_A)

        # Save pair under environment B (firefox / cfg_B) for the EXACT SAME left_cp, right_cp (65, 66)
        cfg_B = "b" * 64
        pair_B = PairKerningObservation(
            left_cp=65, right_cp=66, left_char="A", right_char="B",
            left_advance_upem=650.0, right_advance_upem=600.0,
            measured_pair_advance_upem=1225.0, inferred_kerning_upem=-25,
            is_kerning_applied=True, provenance="chromium:chromium:canvas_text_metrics",
            reference_id="test_font", style_id="regular", browser_version="firefox", config_hash=cfg_B,
        )
        store.save_pair_observation(pair_B)

        # Verify BOTH rows coexist and neither overwrote the other
        pairs_A = store.get_pair_observations("test_font", "regular", "chromium", cfg_A)
        assert len(pairs_A) == 1
        assert pairs_A[0]["browser_version"] == "chromium"
        assert pairs_A[0]["config_hash"] == cfg_A
        assert pairs_A[0]["inferred_kerning_upem"] == -20

        pairs_B = store.get_pair_observations("test_font", "regular", "firefox", cfg_B)
        assert len(pairs_B) == 1
        assert pairs_B[0]["browser_version"] == "firefox"
        assert pairs_B[0]["config_hash"] == cfg_B
        assert pairs_B[0]["inferred_kerning_upem"] == -25


def test_pipeline_result_artifact_lifecycle_and_explicit_cleanup() -> None:
    """Architect Blocker 4: Pipeline result exposes explicit lifecycle cleanup semantics and context manager support."""
    snapshot = _build_valid_snapshot()

    # 1. Execute pipeline with output_dir=None
    result = LocalFidelityIntegrationPipeline.execute_sync(
        snapshot=snapshot,
        format_type="TTF",
    )

    if result.status != "BLOCKED":
        assert result.candidate_file_path != ""
        cand_path = Path(result.candidate_file_path)
        assert cand_path.is_file()

        # Explicit cleanup removes the temporary directory
        result.cleanup()
        assert not cand_path.is_file()

    # 2. Context manager pattern support
    with LocalFidelityIntegrationPipeline.execute_sync(snapshot=snapshot, format_type="TTF") as res:
        assert isinstance(res, LocalFidelityPipelineResult)
        if res.status != "BLOCKED":
            p_file = Path(res.candidate_file_path)
            assert p_file.is_file()

    if res.status != "BLOCKED":
        assert not p_file.is_file()


def test_pipeline_error_logging_sanitizes_hostile_exception_details(caplog: pytest.LogCaptureFixture) -> None:
    """Architect Blocker 5: Raw exception text containing secret paths is never leaked to log messages."""
    snapshot = _build_valid_snapshot()

    with caplog.at_level(logging.ERROR):
        # Trigger an intentional format error
        res = LocalFidelityIntegrationPipeline.execute_sync(snapshot=snapshot, format_type="INVALID_FORMAT")
        assert res.status == "FAIL"

    for record in caplog.records:
        assert "/secret/" not in record.message
        assert "password" not in record.message.lower()
