"""Comprehensive test suite for Stage 9A calibration, Canonical FontModel, consumer bundles, and fail-closed Fidelity Report."""
from __future__ import annotations

import copy
import hashlib
import io
import math
import tempfile
from pathlib import Path
from typing import Sequence

import numpy as np
import pytest
from PIL import Image, ImageDraw

from fidelity.evaluator import FidelityEvaluator
from fidelity.models import (
    BoundChromiumEvidence,
    BoundFontToolsEvidence,
    BoundFreeTypeEvidence,
    BoundHarfBuzzEvidence,
    ChromiumGlyphSampleEvidence,
    ChromiumPairSampleEvidence,
    ConsumerEvidenceBundle,
    ConsumerGateResult,
    FidelityReport,
    FidelityThresholds,
    FreeTypeSampleEvidence,
    HarfBuzzPositionVector,
    HarfBuzzSampleEvidence,
)
from measurement.calibration import (
    CalibratedGlyphMetrics,
    CalibrationTransform,
    ObservationCalibrator,
)
from measurement.models import DirectMetrics, ObservationConfig, ObservationRecord
from measurement.store import ObservationStore
from fontTools.ttLib import TTFont
from reconstruction.candidate_builder import MaxCandidateFontBuilder
from reconstruction.candidate_validator import (
    ChromiumValidationResult,
    FormatValidationResult,
    RasterComparisonResult,
    ShapingTestResult,
)
from reconstruction.font_model import (
    CalibratedGlyph,
    CanonicalFontModel,
    GlobalFontMetrics,
)
from reconstruction.models import (
    Contour,
    CubicSegment,
    LineSegment,
    Point2D,
    ReconstructedGlyph,
)
from typography.models import PairKerningObservation, TypographyDataset


def _generate_png_bytes(
    resolution: int,
    bbox_upem: tuple[float, float, float, float] = (50, 50, 550, 700),
    subpixel_x: float = 0.0,
    subpixel_y: float = 0.0,
) -> bytes:
    """Generate a clean independent binary PNG raster image of a rectangle glyph."""
    img = Image.new("L", (resolution, resolution), 255)  # White canvas
    draw = ImageDraw.Draw(img)

    f_size_px = math.floor(resolution * 0.72)
    scale = f_size_px / 1000.0
    adv_px = 650.0 * scale
    ascent_px = 750.0 * scale
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
    advance_width_upem: float = 650.0,
    lsb_upem: float = 50.0,
    rsb_upem: float = 50.0,
    ascent_upem: float = 750.0,
    descent_upem: float = -200.0,
    bbox_width_upem: float = 500.0,
    bbox_height_upem: float = 650.0,
    confidence: float = 1.0,
) -> DirectMetrics:
    return DirectMetrics(
        code_point=code_point,
        character=chr(code_point),
        font_size_px=144.0,
        raw_advance_width=93.6,
        raw_actual_left=-7.2,
        raw_actual_right=86.4,
        raw_actual_ascent=108.0,
        raw_actual_descent=-28.8,
        raw_font_ascent=115.2,
        raw_font_descent=-28.8,
        advance_width_upem=advance_width_upem,
        lsb_upem=lsb_upem,
        rsb_upem=rsb_upem,
        ascent_upem=ascent_upem,
        descent_upem=descent_upem,
        bbox_width_upem=bbox_width_upem,
        bbox_height_upem=bbox_height_upem,
        sample_count=1,
        confidence=confidence,
    )


def _make_observation_record(
    code_point: int = 65,
    resolution: int = 256,
    subpixel_x: float = 0.0,
    subpixel_y: float = 0.0,
    reference_id: str = "test_font",
    style_id: str = "regular",
    browser_version: str = "chromium",
    config_hash: str = "a" * 64,
    advance_width_upem: float = 650.0,
    bbox_width_upem: float = 500.0,
    bbox_height_upem: float = 650.0,
    confidence: float = 1.0,
    raster_bytes: bytes | None = None,
) -> tuple[ObservationRecord, bytes]:
    metrics = _make_dummy_metrics(
        code_point=code_point,
        advance_width_upem=advance_width_upem,
        bbox_width_upem=bbox_width_upem,
        bbox_height_upem=bbox_height_upem,
        confidence=confidence,
    )
    if raster_bytes is None:
        raster_bytes = _generate_png_bytes(
            resolution,
            (50, 50, 550, 700) if code_point == 65 else (40, 50, 560, 700),
            subpixel_x,
            subpixel_y,
        )

    sha = hashlib.sha256(raster_bytes).hexdigest()
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
    rec = ObservationRecord(
        cache_key=cache_key,
        reference_id=reference_id,
        style_id=style_id,
        code_point=code_point,
        resolution=resolution,
        subpixel_x=subpixel_x,
        subpixel_y=subpixel_y,
        raster_relative_path=f"{reference_id}/{style_id}/{code_point:04X}/{resolution}px_{subpixel_x:.2f}_{subpixel_y:.2f}.png",
        raster_sha256=sha,
        raster_size_bytes=len(raster_bytes),
        metrics=metrics,
        created_at="2026-08-25T00:00:00Z",
        browser_version=browser_version,
        config_hash=config_hash,
    )
    return rec, raster_bytes


def _make_sample_contour(offset_x: float = 0.0, offset_y: float = 0.0) -> Contour:
    """Create a closed square contour in UPEM coordinates."""
    p0 = Point2D(50.0 + offset_x, 50.0 + offset_y)
    p1 = Point2D(550.0 + offset_x, 50.0 + offset_y)
    p2 = Point2D(550.0 + offset_x, 700.0 + offset_y)
    p3 = Point2D(50.0 + offset_x, 700.0 + offset_y)

    segments = [
        LineSegment(p0, p1),
        LineSegment(p1, p2),
        LineSegment(p2, p3),
        LineSegment(p3, p0),
    ]
    return Contour(segments=segments, is_hole=False, area_upem=325000.0)


def _make_valid_consumer_bundle(
    model_hash: str,
    config_hash: str,
    held_out_fp: str,
    artifact_sha: str = "d" * 64,
    file_path: str = "font.ttf",
    records: Sequence[ObservationRecord] | None = None,
    pairs: Sequence[PairKerningObservation] | None = None,
) -> ConsumerEvidenceBundle:
    fmt = FormatValidationResult(
        format="ttf", file_path=file_path, size_bytes=1024, sha256_hex=artifact_sha,
        is_direct_loadable_fonttools=True, is_direct_loadable_freetype=True,
        is_roundtrip_loadable_freetype=True, is_direct_loadable_harfbuzz=True,
        is_direct_loadable_chromium=True, glyph_count=2, units_per_em=1000,
        has_valid_cmap=True, has_valid_metrics=True, decompression_round_trip=True,
    )
    if records:
        samples = tuple(
            FreeTypeSampleEvidence(
                cache_key=r.cache_key, code_point=r.code_point, character=chr(r.code_point),
                resolution=r.resolution, raster_sha256=r.raster_sha256, raster_iou=0.95, pixel_delta_count=5,
            )
            for r in records
        )
        total_px_deltas = sum(s.pixel_delta_count for s in samples)
        raster_res = RasterComparisonResult(
            code_point=records[0].code_point, character=chr(records[0].code_point), render_size_px=records[0].resolution,
            raster_iou=0.95, pixel_delta_count=total_px_deltas, render_error=None, samples=samples, min_raster_iou=0.95,
        )
        unique_cps = sorted(list({r.code_point for r in records}))
        cr_glyph_samples = tuple(
            ChromiumGlyphSampleEvidence(
                code_point=cp,
                character=chr(cp),
                candidate_advance_upem=650.0 if cp == 65 else 600.0,
                expected_advance_upem=650.0 if cp == 65 else 600.0,
                advance_delta_upem=0.0,
            )
            for cp in unique_cps
        )
    else:
        cr_glyph_samples = ()
        raster_res = RasterComparisonResult(
            code_point=65, character="A", render_size_px=256,
            raster_iou=0.95, pixel_delta_count=5, render_error=None,
        )

    if pairs:
        hb_samples = tuple(
            HarfBuzzSampleEvidence(
                left_cp=p.left_cp, right_cp=p.right_cp, text=f"{p.left_char}{p.right_char}",
                in_candidate_cmap=True, glyph_sequence_match=True, glyph_ids=(1, 2), clusters=(0, 1),
                positions=(
                    HarfBuzzPositionVector(p.left_advance_upem + float(p.inferred_kerning_upem), 0, 0, 0),
                    HarfBuzzPositionVector(p.right_advance_upem, 0, 0, 0),
                ),
                candidate_total_advance_upem=p.measured_pair_advance_upem, expected_total_advance_upem=p.measured_pair_advance_upem,
                advance_delta_upem=0.0, max_position_delta_upem=0.0,
            )
            for p in pairs
        )
        shaping_res = ShapingTestResult(
            text=f"{pairs[0].left_char}{pairs[0].right_char}", category="basic", in_candidate_cmap=True,
            glyph_sequence_match=True, candidate_glyph_names=["A", "B"],
            reference_glyph_names=["A", "B"], candidate_glyph_count=2,
            reference_glyph_count=2, candidate_total_advance_upem=int(pairs[0].measured_pair_advance_upem),
            reference_total_advance_upem=int(pairs[0].measured_pair_advance_upem), advance_delta_upem=0,
            max_position_delta_upem=0, samples=hb_samples, all_in_cmap=True, all_sequence_match=True,
        )
        cr_pair_samples = tuple(
            ChromiumPairSampleEvidence(
                left_cp=p.left_cp, right_cp=p.right_cp, pair=f"{p.left_char}{p.right_char}",
                baseline_single_sum_upem=p.left_advance_upem + p.right_advance_upem,
                candidate_pair_advance_upem=p.measured_pair_advance_upem,
                expected_pair_advance_upem=p.measured_pair_advance_upem,
                gpos_applied_adjustment_upem=float(p.inferred_kerning_upem),
                advance_delta_upem=0.0, non_regression=True,
            )
            for p in pairs
        )
        chromium_res = ChromiumValidationResult(
            is_available=True, browser_version="chromium",
            is_direct_loadable_chromium=True, fallback_rejection_verified=True,
            measured_glyph_count=len(cr_glyph_samples) if cr_glyph_samples else 2,
            mean_chromium_advance_error_upem=0.0,
            rendered_canvas_valid=True, error_message=None, held_out_pairs_non_regression=True,
            glyph_samples=cr_glyph_samples,
            pair_samples=cr_pair_samples,
        )
    else:
        shaping_res = ShapingTestResult(
            text="AB", category="basic", in_candidate_cmap=True,
            glyph_sequence_match=True, candidate_glyph_names=["A", "B"],
            reference_glyph_names=["A", "B"], candidate_glyph_count=2,
            reference_glyph_count=2, candidate_total_advance_upem=1250,
            reference_total_advance_upem=1250, advance_delta_upem=0,
            max_position_delta_upem=0,
        )
        chromium_res = ChromiumValidationResult(
            is_available=True, browser_version="chromium",
            is_direct_loadable_chromium=True, fallback_rejection_verified=True,
            measured_glyph_count=2, mean_chromium_advance_error_upem=0.5,
            rendered_canvas_valid=True, error_message=None, held_out_pairs_non_regression=True,
        )

    r_fp = FidelityEvaluator._compute_records_fingerprint(records) if records else held_out_fp
    t_fp = FidelityEvaluator._compute_typography_fingerprint(pairs) if pairs else "empty"
    comp_fp = FidelityEvaluator._compute_composite_held_out_fingerprint(records, pairs) if (records and pairs) else held_out_fp

    return ConsumerEvidenceBundle(
        schema_version="1.0.0",
        model_canonical_hash=model_hash,
        config_hash=config_hash,
        held_out_fingerprint=comp_fp,
        candidate_artifact_sha=artifact_sha,
        fonttools=BoundFontToolsEvidence(candidate_artifact_sha=artifact_sha, result=fmt),
        freetype=BoundFreeTypeEvidence(candidate_artifact_sha=artifact_sha, result=raster_res),
        harfbuzz=BoundHarfBuzzEvidence(candidate_artifact_sha=artifact_sha, result=shaping_res),
        chromium=BoundChromiumEvidence(candidate_artifact_sha=artifact_sha, result=chromium_res),
        held_out_raster_fingerprint=r_fp,
        held_out_typography_fingerprint=t_fp,
    )


# =========================================================================
# 1. Calibration Transform & Order Independence
# =========================================================================

def test_calibration_transform_forward_and_inverse() -> None:
    metrics = _make_dummy_metrics(advance_width_upem=600.0, ascent_upem=700.0, descent_upem=-200.0)
    transform = CalibrationTransform.from_observation(
        resolution=256,
        metrics=metrics,
        subpixel_x=0.25,
        subpixel_y=0.5,
        units_per_em=1000,
    )

    test_points = [(0.0, 0.0), (300.0, 450.0), (600.0, 700.0), (50.0, -150.0)]
    for upem_x, upem_y in test_points:
        px_x, px_y = transform.inverse(upem_x, upem_y)
        rec_x, rec_y = transform.forward(px_x, px_y)
        assert abs(rec_x - upem_x) < 1e-6
        assert abs(rec_y - upem_y) < 1e-6


def test_calibration_transform_rejects_degenerate_inputs() -> None:
    metrics = _make_dummy_metrics()
    with pytest.raises(ValueError, match="Invalid non-positive resolution"):
        CalibrationTransform.from_observation(resolution=0, metrics=metrics)

    with pytest.raises(ValueError, match="Invalid non-positive UPEM"):
        CalibrationTransform.from_observation(resolution=256, metrics=metrics, units_per_em=-100)

    transform = CalibrationTransform.from_observation(resolution=256, metrics=metrics)
    with pytest.raises(ValueError, match="Non-finite pixel coordinates"):
        transform.forward(float("nan"), 10.0)

    with pytest.raises(ValueError, match="Non-finite UPEM coordinates"):
        transform.inverse(float("inf"), 10.0)


def test_observation_calibrator_order_independence() -> None:
    r1, _ = _make_observation_record(code_point=65, resolution=128, advance_width_upem=650.0)
    r2, _ = _make_observation_record(code_point=65, resolution=256, advance_width_upem=652.0)
    r3, _ = _make_observation_record(code_point=65, resolution=512, advance_width_upem=648.0)

    calib1 = ObservationCalibrator.calibrate_glyph_observations([r1, r2, r3])
    calib2 = ObservationCalibrator.calibrate_glyph_observations([r3, r1, r2])
    calib3 = ObservationCalibrator.calibrate_glyph_observations([r2, r3, r1])

    assert calib1.to_dict() == calib2.to_dict() == calib3.to_dict()
    assert calib1.advance_width_upem == 650.0
    assert calib1.sample_count == 3


def test_observation_calibrator_rejects_mismatched_and_corrupt_records() -> None:
    r1, _ = _make_observation_record(code_point=65, reference_id="font_A")
    r2, _ = _make_observation_record(code_point=65, reference_id="font_B")

    with pytest.raises(ValueError, match="Reference ID mismatch"):
        ObservationCalibrator.calibrate_glyph_observations([r1, r2])

    r_diff_cp, _ = _make_observation_record(code_point=66, reference_id="font_A")
    with pytest.raises(ValueError, match="Code point mismatch"):
        ObservationCalibrator.calibrate_glyph_observations([r1, r_diff_cp])


def test_observation_calibrator_rejects_duplicate_and_missing_phases() -> None:
    config = ObservationConfig(
        resolutions=(128, 256),
        base_subpixel_phases=((0.0, 0.0), (0.5, 0.5)),
        expanded_subpixel_phases=((0.0, 0.0), (0.5, 0.5)),
    )
    cfg_hash = config.compute_hash()

    r1, _ = _make_observation_record(code_point=65, resolution=128, subpixel_x=0.0, subpixel_y=0.0, config_hash=cfg_hash)
    r2, _ = _make_observation_record(code_point=65, resolution=128, subpixel_x=0.0, subpixel_y=0.0, config_hash=cfg_hash)

    with pytest.raises(ValueError, match="Duplicate adaptive phase observation"):
        ObservationCalibrator.calibrate_glyph_observations([r1, r2], config=config)

    # Missing phase (0.5, 0.5) at 128
    with pytest.raises(ValueError, match="Missing required adaptive subpixel phase"):
        ObservationCalibrator.calibrate_glyph_observations([r1], config=config)


# =========================================================================
# 2. Observation Store Round-Trip & Total Fail-Closed Resume
# =========================================================================

def test_observation_record_non_hex_digest_rejected_at_construction() -> None:
    """Architect defect 1: non-hex config_hash, raster_sha256, or cache_key fails at construction."""
    metrics = _make_dummy_metrics()
    with pytest.raises(ValueError, match="must be a 64-char hexadecimal"):
        ObservationRecord(
            cache_key="0" * 64, reference_id="r", style_id="s", code_point=65, resolution=128,
            subpixel_x=0.0, subpixel_y=0.0, raster_relative_path="p.png", raster_sha256="0" * 64,
            raster_size_bytes=100, metrics=metrics, created_at="2026", browser_version="chr",
            config_hash="g" * 64,  # Non-hex character 'g'
        )

    with pytest.raises(ValueError, match="must be a 64-char hexadecimal"):
        ObservationRecord(
            cache_key="0" * 64, reference_id="r", style_id="s", code_point=65, resolution=128,
            subpixel_x=0.0, subpixel_y=0.0, raster_relative_path="p.png", raster_sha256="z" * 64,  # Non-hex 'z'
            raster_size_bytes=100, metrics=metrics, created_at="2026", browser_version="chr",
            config_hash="0" * 64,
        )


def test_collector_store_roundtrip_identity_preservation() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = ObservationStore(Path(tmp_dir))
        config = ObservationConfig(resolutions=(128, 256), base_subpixel_phases=((0.0, 0.0),), expanded_subpixel_phases=((0.0, 0.0),))
        cfg_hash = config.compute_hash()

        rec, png_bytes = _make_observation_record(
            code_point=65, resolution=128, browser_version="chromium-headless-shell", config_hash=cfg_hash
        )
        store.save_observation(rec, png_bytes)

        assert store.has_observation(rec.cache_key) is True
        loaded_rec = store.get_observation(rec.cache_key)
        assert loaded_rec is not None
        assert loaded_rec.browser_version == "chromium-headless-shell"
        assert loaded_rec.config_hash == cfg_hash
        assert loaded_rec.validate_cache_key() is True

        glyph_obs = store.get_glyph_observations("test_font", "regular", 65)
        assert len(glyph_obs) == 1
        g_rec, g_bytes = glyph_obs[0]
        assert g_rec.browser_version == "chromium-headless-shell"
        assert g_rec.config_hash == cfg_hash
        assert g_bytes == png_bytes


def test_store_has_observation_returns_false_for_tampered_non_hex_or_missing_or_corrupt_files() -> None:
    """Architect defect 1: malformed identity, missing file, wrong size, or wrong SHA returns False safely."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = ObservationStore(Path(tmp_dir))
        config = ObservationConfig(resolutions=(128, 256), base_subpixel_phases=((0.0, 0.0),), expanded_subpixel_phases=((0.0, 0.0),))
        cfg_hash = config.compute_hash()

        # 1. Row with malformed non-hex config_hash in DB -> has_observation must return False without throwing!
        with store._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO observations (
                    cache_key, reference_id, style_id, code_point, resolution,
                    subpixel_x, subpixel_y, raster_relative_path, raster_sha256,
                    raster_size_bytes, advance_width_px, advance_width_upem,
                    lsb_px, lsb_upem, rsb_px, rsb_upem, ascent_px, ascent_upem,
                    descent_px, descent_upem, bbox_width_upem, bbox_height_upem,
                    sample_count, confidence, created_at, browser_version, config_hash
                ) VALUES ('0'*64, 'ref', 'style', 65, 128, 0, 0, 'path.png', '0'*64, 100, 50, 500, 0, 0, 0, 0, 50, 500, 0, 0, 500, 500, 1, 1.0, '2026', 'chr', 'malformed_short_non_hex')
                """
            )
            conn.commit()

        assert store.get_observation("0" * 64) is None
        assert store.has_observation("0" * 64) is False

        # 2. Valid save followed by missing file
        rec, png_bytes = _make_observation_record(code_point=65, resolution=128, config_hash=cfg_hash)
        store.save_observation(rec, png_bytes)
        assert store.has_observation(rec.cache_key) is True

        png_file = Path(tmp_dir) / rec.raster_relative_path
        png_file.unlink()  # delete raster
        assert store.has_observation(rec.cache_key) is False

        # 3. Corrupt raster file (wrong SHA/size)
        png_file.write_bytes(b"CORRUPT_BYTES")
        assert store.has_observation(rec.cache_key) is False

        # 4. Recollection / replacement converges cleanly
        store.save_observation(rec, png_bytes)
        assert store.has_observation(rec.cache_key) is True


def test_save_observation_rejects_mismatched_and_corrupt_bytes() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = ObservationStore(Path(tmp_dir))
        config = ObservationConfig(resolutions=(128, 256), base_subpixel_phases=((0.0, 0.0),), expanded_subpixel_phases=((0.0, 0.0),))
        cfg_hash = config.compute_hash()

        rec, png_bytes = _make_observation_record(code_point=65, resolution=128, config_hash=cfg_hash)

        # 1. Byte length mismatch
        with pytest.raises(ValueError, match="Raster byte size mismatch"):
            store.save_observation(rec, png_bytes + b"extra")

        # 2. SHA256 mismatch
        bad_rec = copy.deepcopy(rec)
        bad_rec.raster_sha256 = "b" * 64
        with pytest.raises(ValueError, match="Raster SHA256 mismatch"):
            store.save_observation(bad_rec, png_bytes)


# =========================================================================
# 3. Strict Canonical Model & Metrics Parsing
# =========================================================================

def test_canonical_font_model_strict_validation_and_parsing() -> None:
    contour = _make_sample_contour()
    glyph_A = CalibratedGlyph(
        code_point=65, character="A", advance_width_upem=650.0, lsb_upem=50.0, rsb_upem=50.0,
        ascent_upem=750.0, descent_upem=-200.0, bounding_box_upem=(50.0, 50.0, 550.0, 700.0),
        contours=[contour], confidence=1.0, observation_fingerprints=("a" * 64,),
    )
    model = CanonicalFontModel(
        schema_version="1.0.0", family_name="TestFamily", style_name="Regular",
        reference_id="test_family", style_id="regular",
        metrics=GlobalFontMetrics(
            units_per_em=1000, ascent_upem=800.0, descent_upem=-200.0, line_gap_upem=0.0,
            cap_height_upem=700.0, x_height_upem=500.0, max_advance_width_upem=1000.0,
            avg_char_width_upem=500.0, underline_position_upem=-100.0, underline_thickness_upem=50.0,
        ),
        glyphs={65: glyph_A}, kerning_pairs={(65, 65): 0},
        config_hash="a" * 64, browser_version="chromium",
        fit_observations_count=1, calibration_fingerprint="b" * 64,
    )
    json_str = model.to_canonical_json()
    restored = CanonicalFontModel.from_canonical_json(json_str)
    assert restored.compute_canonical_hash() == model.compute_canonical_hash()

    # Reject duplicate glyph entries
    d = model.to_canonical_dict()
    d["glyphs"].append(glyph_A.to_canonical_dict())
    with pytest.raises(ValueError, match="Duplicate glyph entry"):
        CanonicalFontModel.from_canonical_dict(d)

    # Reject duplicate kerning pairs
    d_kern = model.to_canonical_dict()
    d_kern["kerning_pairs"].append({"left_cp": 65, "right_cp": 65, "kerning_upem": 0})
    with pytest.raises(ValueError, match="Duplicate kerning pair entry"):
        CanonicalFontModel.from_canonical_dict(d_kern)

    # Reject non-hex hashes
    with pytest.raises(ValueError, match="must be a 64-char hex digest"):
        bad_model = copy.deepcopy(model)
        bad_model.config_hash = "g" * 64
        bad_model.validate()


def test_canonical_font_model_hash_stability_under_reordering() -> None:
    contour_A = _make_sample_contour()
    contour_B = _make_sample_contour(offset_x=10.0)

    glyph_A = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50.0, 50.0, 550.0, 700.0), [contour_A], observation_fingerprints=("a" * 64,))
    glyph_B = CalibratedGlyph(66, "B", 600.0, 40.0, 40.0, 750.0, -200.0, (40.0, 50.0, 560.0, 700.0), [contour_B], observation_fingerprints=("b" * 64,))

    metrics = GlobalFontMetrics(1000, 800.0, -200.0, 0.0, 700.0, 500.0, 1000.0, 500.0, -100.0, 50.0)

    model_1 = CanonicalFontModel(
        family_name="ReorderFont", style_name="Regular", reference_id="ref", style_id="reg",
        metrics=metrics, config_hash="a" * 64, browser_version="chromium", fit_observations_count=2,
        calibration_fingerprint="c" * 64, glyphs={65: glyph_A, 66: glyph_B}, kerning_pairs={(65, 66): -20, (66, 65): -10},
    )

    model_2 = CanonicalFontModel(
        family_name="ReorderFont", style_name="Regular", reference_id="ref", style_id="reg",
        metrics=metrics, config_hash="a" * 64, browser_version="chromium", fit_observations_count=2,
        calibration_fingerprint="c" * 64, glyphs={66: glyph_B, 65: glyph_A}, kerning_pairs={(66, 65): -10, (65, 66): -20},
    )

    assert model_1.compute_canonical_hash() == model_2.compute_canonical_hash()


# =========================================================================
# 4. Architect Adversarial Reproductions & Fail-Closed Tests
# =========================================================================

def test_cross_artifact_consumer_reuse_rejected() -> None:
    """Architect defect 2: reusing consumer results across different candidate artifacts fails binding."""
    # Bundle A for artifact SHA 'd'*64
    bundle_a = _make_valid_consumer_bundle(
        model_hash="a" * 64, config_hash="b" * 64, held_out_fp="c" * 64, artifact_sha="d" * 64
    )
    assert len(bundle_a.validate_bindings("a" * 64, "b" * 64, "c" * 64)) == 0

    # Bundle B for artifact SHA 'e'*64 but reuses freetype / harfbuzz from 'd'*64
    fmt_e = FormatValidationResult(
        format="ttf", file_path="font.ttf", size_bytes=1024, sha256_hex="e" * 64,
        is_direct_loadable_fonttools=True, is_direct_loadable_freetype=True,
        is_roundtrip_loadable_freetype=True, is_direct_loadable_harfbuzz=True,
        is_direct_loadable_chromium=True, glyph_count=2, units_per_em=1000,
        has_valid_cmap=True, has_valid_metrics=True, decompression_round_trip=True,
    )
    bundle_b = ConsumerEvidenceBundle(
        schema_version="1.0.0",
        model_canonical_hash="a" * 64,
        config_hash="b" * 64,
        held_out_fingerprint="c" * 64,
        candidate_artifact_sha="e" * 64,
        fonttools=BoundFontToolsEvidence(candidate_artifact_sha="e" * 64, result=fmt_e),
        freetype=bundle_a.freetype,  # REUSED from artifact 'd'*64!
        harfbuzz=bundle_a.harfbuzz,  # REUSED from artifact 'd'*64!
        chromium=bundle_a.chromium,  # REUSED from artifact 'd'*64!
    )
    errors = bundle_b.validate_bindings("a" * 64, "b" * 64, "c" * 64)
    assert any("CROSS_ARTIFACT_CONSUMER_EVIDENCE: freetype" in e for e in errors)
    assert any("CROSS_ARTIFACT_CONSUMER_EVIDENCE: harfbuzz" in e for e in errors)
    assert any("CROSS_ARTIFACT_CONSUMER_EVIDENCE: chromium" in e for e in errors)


def test_nondeterministic_bundle_identity_host_path_invariance() -> None:
    """Architect reproduction: changing only host path in fonttools result yields identical bundle hash."""
    bundle1 = _make_valid_consumer_bundle(
        model_hash="a" * 64, config_hash="b" * 64, held_out_fp="c" * 64, file_path="/tmp/host1/font.ttf"
    )
    bundle2 = _make_valid_consumer_bundle(
        model_hash="a" * 64, config_hash="b" * 64, held_out_fp="c" * 64, file_path="/opt/data/host2/another_dir/font.ttf"
    )
    assert bundle1.compute_bundle_hash() == bundle2.compute_bundle_hash()


def test_incomplete_chromium_gate_fails_closed() -> None:
    """Architect reproduction: incomplete or failing Chromium fields fail the consumer gate."""
    config = ObservationConfig(resolutions=(128, 256), base_subpixel_phases=((0.0, 0.0),), expanded_subpixel_phases=((0.0, 0.0),))
    cfg_hash = config.compute_hash()

    r_fit, b_fit = _make_observation_record(code_point=65, resolution=128, config_hash=cfg_hash)
    r_fit2, b_fit2 = _make_observation_record(code_point=65, resolution=256, config_hash=cfg_hash)
    r_held, b_held = _make_observation_record(code_point=65, resolution=256, subpixel_x=0.25, subpixel_y=0.25, config_hash=cfg_hash)

    fit_records = [r_fit, r_fit2]
    calib_map = ObservationCalibrator.calibrate_all(fit_records, config=config)
    calib_fp = ObservationCalibrator.compute_calibration_fingerprint(fit_records, config=config)

    glyph = CalibratedGlyph(
        65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50, 50, 550, 700),
        [_make_sample_contour()], observation_fingerprints=calib_map[65].observation_fingerprints,
    )
    model = CanonicalFontModel(
        family_name="F", style_name="R", reference_id="test_font", style_id="regular",
        config_hash=cfg_hash, browser_version="chromium", fit_observations_count=2,
        calibration_fingerprint=calib_fp, glyphs={65: glyph},
    )
    model_hash = model.compute_canonical_hash()
    held_fp = FidelityEvaluator._compute_records_fingerprint([r_held])

    # 1. Invalid canvas -> FAIL
    bad_chromium = ChromiumValidationResult(
        is_available=True, browser_version="chromium", is_direct_loadable_chromium=True,
        fallback_rejection_verified=True, measured_glyph_count=2, mean_chromium_advance_error_upem=0.5,
        rendered_canvas_valid=False, error_message=None, held_out_pairs_non_regression=True,
    )
    bundle1 = _make_valid_consumer_bundle(model_hash=model_hash, config_hash=cfg_hash, held_out_fp=held_fp)
    bundle1 = ConsumerEvidenceBundle(
        schema_version="1.0.0", model_canonical_hash=model_hash, config_hash=cfg_hash,
        held_out_fingerprint=held_fp, candidate_artifact_sha="d" * 64,
        fonttools=bundle1.fonttools, freetype=bundle1.freetype, harfbuzz=bundle1.harfbuzz,
        chromium=BoundChromiumEvidence(candidate_artifact_sha="d" * 64, result=bad_chromium),
    )

    rep1 = FidelityEvaluator.evaluate(
        model=model, config=config, fit_records=fit_records, held_out_records=[r_held],
        held_out_pairs=[PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False, provenance="chromium:chromium:canvas_text_metrics")],
        consumer_bundle=bundle1, raster_provider=lambda r: b_held,
    )
    assert rep1.overall_status == "FAIL"
    assert rep1.consumer_gate.status == "FAIL"
    assert not rep1.consumer_gate.chromium_passed

    # 2. Non-empty error message -> FAIL
    bad_chromium2 = ChromiumValidationResult(
        is_available=True, browser_version="chromium", is_direct_loadable_chromium=True,
        fallback_rejection_verified=True, measured_glyph_count=2, mean_chromium_advance_error_upem=0.5,
        rendered_canvas_valid=True, error_message="SHADING_ERROR", held_out_pairs_non_regression=True,
    )
    bundle2 = ConsumerEvidenceBundle(
        schema_version="1.0.0", model_canonical_hash=model_hash, config_hash=cfg_hash,
        held_out_fingerprint=held_fp, candidate_artifact_sha="d" * 64,
        fonttools=bundle1.fonttools, freetype=bundle1.freetype, harfbuzz=bundle1.harfbuzz,
        chromium=BoundChromiumEvidence(candidate_artifact_sha="d" * 64, result=bad_chromium2),
    )

    rep2 = FidelityEvaluator.evaluate(
        model=model, config=config, fit_records=fit_records, held_out_records=[r_held],
        held_out_pairs=[PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False, provenance="chromium:chromium:canvas_text_metrics")],
        consumer_bundle=bundle2, raster_provider=lambda r: b_held,
    )
    assert rep2.overall_status == "FAIL"
    assert not rep2.consumer_gate.chromium_passed

    # 3. Held-out pairs regression -> FAIL
    bad_chromium3 = ChromiumValidationResult(
        is_available=True, browser_version="chromium", is_direct_loadable_chromium=True,
        fallback_rejection_verified=True, measured_glyph_count=2, mean_chromium_advance_error_upem=0.5,
        rendered_canvas_valid=True, error_message=None, held_out_pairs_non_regression=False,
    )
    bundle3 = ConsumerEvidenceBundle(
        schema_version="1.0.0", model_canonical_hash=model_hash, config_hash=cfg_hash,
        held_out_fingerprint=held_fp, candidate_artifact_sha="d" * 64,
        fonttools=bundle1.fonttools, freetype=bundle1.freetype, harfbuzz=bundle1.harfbuzz,
        chromium=BoundChromiumEvidence(candidate_artifact_sha="d" * 64, result=bad_chromium3),
    )

    rep3 = FidelityEvaluator.evaluate(
        model=model, config=config, fit_records=fit_records, held_out_records=[r_held],
        held_out_pairs=[PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False, provenance="chromium:chromium:canvas_text_metrics")],
        consumer_bundle=bundle3, raster_provider=lambda r: b_held,
    )
    assert rep3.overall_status == "FAIL"
    assert not rep3.consumer_gate.chromium_passed


def test_real_typography_provenance_and_browser_drift() -> None:
    config = ObservationConfig(resolutions=(128, 256), base_subpixel_phases=((0.0, 0.0),), expanded_subpixel_phases=((0.0, 0.0),))
    cfg_hash = config.compute_hash()

    r_fit, b_fit = _make_observation_record(code_point=65, resolution=128, config_hash=cfg_hash)
    r_fit2, b_fit2 = _make_observation_record(code_point=65, resolution=256, config_hash=cfg_hash)
    r_held, b_held = _make_observation_record(code_point=65, resolution=256, subpixel_x=0.25, subpixel_y=0.25, config_hash=cfg_hash)

    fit_records = [r_fit, r_fit2]
    calib_map = ObservationCalibrator.calibrate_all(fit_records, config=config)
    calib_fp = ObservationCalibrator.compute_calibration_fingerprint(fit_records, config=config)

    glyph = CalibratedGlyph(
        65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50, 50, 550, 700),
        [_make_sample_contour()], observation_fingerprints=calib_map[65].observation_fingerprints,
    )
    model = CanonicalFontModel(
        family_name="F", style_name="R", reference_id="test_font", style_id="regular",
        config_hash=cfg_hash, browser_version="chromium", fit_observations_count=2,
        calibration_fingerprint=calib_fp, glyphs={65: glyph},
    )

    valid_pair = PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False, provenance="chromium:chromium:canvas_text_metrics")

    bundle = _make_valid_consumer_bundle(
        model_hash=model.compute_canonical_hash(),
        config_hash=cfg_hash,
        held_out_fp=FidelityEvaluator._compute_records_fingerprint([r_held]),
        records=[r_held],
        pairs=[valid_pair],
    )

    # 1. Synthetic label -> FAIL
    synthetic_pair = PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False, provenance="synthetic_label")
    rep1 = FidelityEvaluator.evaluate(
        model=model, config=config, fit_records=fit_records, held_out_records=[r_held],
        held_out_pairs=[synthetic_pair], consumer_bundle=bundle, raster_provider=lambda r: b_held,
    )
    assert rep1.overall_status == "FAIL"
    assert any("UNTRUSTED_TYPOGRAPHY" in r for r in rep1.failure_reasons)

    # 2. Browser version drift in provenance -> FAIL
    drift_pair = PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False, provenance="chromium:drifted_version:canvas_text_metrics")
    rep2 = FidelityEvaluator.evaluate(
        model=model, config=config, fit_records=fit_records, held_out_records=[r_held],
        held_out_pairs=[drift_pair], consumer_bundle=bundle, raster_provider=lambda r: b_held,
    )
    assert rep2.overall_status == "FAIL"
    assert any("UNTRUSTED_TYPOGRAPHY" in r for r in rep2.failure_reasons)

    # 3. Matching real production provenance -> PASS
    valid_pair = PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False, provenance="chromium:chromium:canvas_text_metrics")
    rep3 = FidelityEvaluator.evaluate(
        model=model, config=config, fit_records=fit_records, held_out_records=[r_held],
        held_out_pairs=[valid_pair], consumer_bundle=bundle, raster_provider=lambda r: b_held,
    )
    assert rep3.overall_status == "PASS"


def test_consumer_gate_mandatory_and_bypass_fails_closed() -> None:
    config = ObservationConfig(resolutions=(128, 256), base_subpixel_phases=((0.0, 0.0),), expanded_subpixel_phases=((0.0, 0.0),))
    cfg_hash = config.compute_hash()

    r_fit, b_fit = _make_observation_record(code_point=65, resolution=128, config_hash=cfg_hash)
    r_fit2, b_fit2 = _make_observation_record(code_point=65, resolution=256, config_hash=cfg_hash)
    r_held, b_held = _make_observation_record(code_point=65, resolution=256, subpixel_x=0.25, subpixel_y=0.25, config_hash=cfg_hash)

    fit_records = [r_fit, r_fit2]
    calib_map = ObservationCalibrator.calibrate_all(fit_records, config=config)
    calib_fp = ObservationCalibrator.compute_calibration_fingerprint(fit_records, config=config)

    glyph = CalibratedGlyph(
        65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50, 50, 550, 700),
        [_make_sample_contour()], observation_fingerprints=calib_map[65].observation_fingerprints,
    )
    model = CanonicalFontModel(
        family_name="F", style_name="R", reference_id="test_font", style_id="regular",
        config_hash=cfg_hash, browser_version="chromium", fit_observations_count=2,
        calibration_fingerprint=calib_fp, glyphs={65: glyph},
    )
    model_hash = model.compute_canonical_hash()
    held_fp = FidelityEvaluator._compute_records_fingerprint([r_held])

    # 1. Missing bundle -> FAIL
    rep1 = FidelityEvaluator.evaluate(
        model=model, config=config, fit_records=fit_records, held_out_records=[r_held],
        held_out_pairs=[PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False, provenance="chromium:chromium:canvas_text_metrics")],
        consumer_bundle=None, raster_provider=lambda r: b_held,
    )
    assert rep1.overall_status == "FAIL"
    assert rep1.consumer_gate.status == "FAIL"
    assert any("MISSING_CONSUMER_BUNDLE" in r for r in rep1.failure_reasons)

    # 2. Stale model hash in bundle -> FAIL
    stale_bundle = _make_valid_consumer_bundle(model_hash="e" * 64, config_hash=cfg_hash, held_out_fp=held_fp)
    rep2 = FidelityEvaluator.evaluate(
        model=model, config=config, fit_records=fit_records, held_out_records=[r_held],
        held_out_pairs=[PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False, provenance="chromium:chromium:canvas_text_metrics")],
        consumer_bundle=stale_bundle, raster_provider=lambda r: b_held,
    )
    assert rep2.overall_status == "FAIL"
    assert rep2.consumer_gate.status == "FAIL"
    assert any("BUNDLE_MODEL_HASH_MISMATCH" in r for r in rep2.failure_reasons)

    # 3. Failing consumer (e.g. FreeType render error) -> FAIL
    bad_bundle = _make_valid_consumer_bundle(model_hash=model_hash, config_hash=cfg_hash, held_out_fp=held_fp)
    bad_bundle = ConsumerEvidenceBundle(
        schema_version="1.0.0", model_canonical_hash=model_hash, config_hash=cfg_hash,
        held_out_fingerprint=held_fp, candidate_artifact_sha="d" * 64,
        fonttools=bad_bundle.fonttools,
        freetype=BoundFreeTypeEvidence(candidate_artifact_sha="d" * 64, result=RasterComparisonResult(65, "A", 256, 0.5, 50, render_error="CORRUPT_TABLE")),
        harfbuzz=bad_bundle.harfbuzz,
        chromium=bad_bundle.chromium,
    )
    rep3 = FidelityEvaluator.evaluate(
        model=model, config=config, fit_records=fit_records, held_out_records=[r_held],
        held_out_pairs=[PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False, provenance="chromium:chromium:canvas_text_metrics")],
        consumer_bundle=bad_bundle, raster_provider=lambda r: b_held,
    )
    assert rep3.overall_status == "FAIL"
    assert rep3.consumer_gate.status == "FAIL"
    assert not rep3.consumer_gate.freetype_passed


def test_fit_evidence_fingerprint_binding_fails_closed() -> None:
    config = ObservationConfig(resolutions=(128, 256), base_subpixel_phases=((0.0, 0.0),), expanded_subpixel_phases=((0.0, 0.0),))
    cfg_hash = config.compute_hash()

    r_fit, b_fit = _make_observation_record(code_point=65, resolution=128, config_hash=cfg_hash)
    r_fit2, b_fit2 = _make_observation_record(code_point=65, resolution=256, config_hash=cfg_hash)
    r_held, b_held = _make_observation_record(code_point=65, resolution=256, subpixel_x=0.25, subpixel_y=0.25, config_hash=cfg_hash)

    fit_records = [r_fit, r_fit2]
    # Model with arbitrary/tampered glyph observation fingerprints
    glyph = CalibratedGlyph(
        65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50, 50, 550, 700),
        [_make_sample_contour()], observation_fingerprints=("f" * 64,),  # Arbitrary hash!
    )
    calib_fp = ObservationCalibrator.compute_calibration_fingerprint(fit_records, config=config)
    model = CanonicalFontModel(
        family_name="F", style_name="R", reference_id="test_font", style_id="regular",
        config_hash=cfg_hash, browser_version="chromium", fit_observations_count=2,
        calibration_fingerprint=calib_fp, glyphs={65: glyph},
    )

    bundle = _make_valid_consumer_bundle(
        model_hash=model.compute_canonical_hash(),
        config_hash=cfg_hash,
        held_out_fp=FidelityEvaluator._compute_records_fingerprint([r_held]),
    )

    report = FidelityEvaluator.evaluate(
        model=model, config=config, fit_records=fit_records, held_out_records=[r_held],
        held_out_pairs=[PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False, provenance="chromium:chromium:canvas_text_metrics")],
        consumer_bundle=bundle, raster_provider=lambda r: b_held,
    )
    assert report.overall_status == "FAIL"
    assert any("GLYPH_FINGERPRINT_MISMATCH" in r for r in report.failure_reasons)


def test_policy_hash_and_max_chamfer_gating() -> None:
    config = ObservationConfig(resolutions=(128, 256), base_subpixel_phases=((0.0, 0.0),), expanded_subpixel_phases=((0.0, 0.0),))
    cfg_hash = config.compute_hash()

    r_fit, b_fit = _make_observation_record(code_point=65, resolution=128, config_hash=cfg_hash)
    r_fit2, b_fit2 = _make_observation_record(code_point=65, resolution=256, config_hash=cfg_hash)
    r_held, b_held = _make_observation_record(code_point=65, resolution=256, subpixel_x=0.25, subpixel_y=0.25, config_hash=cfg_hash)

    fit_records = [r_fit, r_fit2]
    calib_map = ObservationCalibrator.calibrate_all(fit_records, config=config)
    calib_fp = ObservationCalibrator.compute_calibration_fingerprint(fit_records, config=config)

    glyph = CalibratedGlyph(
        65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50, 50, 550, 700),
        [_make_sample_contour()], observation_fingerprints=calib_map[65].observation_fingerprints,
    )
    model = CanonicalFontModel(
        family_name="F", style_name="R", reference_id="test_font", style_id="regular",
        config_hash=cfg_hash, browser_version="chromium", fit_observations_count=2,
        calibration_fingerprint=calib_fp, glyphs={65: glyph},
    )

    bundle = _make_valid_consumer_bundle(
        model_hash=model.compute_canonical_hash(),
        config_hash=cfg_hash,
        held_out_fp=FidelityEvaluator._compute_records_fingerprint([r_held]),
    )
    held_pair = PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False, provenance="chromium:chromium:canvas_text_metrics")

    pol1 = FidelityThresholds(max_chamfer_distance_upem=30.0)
    pol2 = FidelityThresholds(max_chamfer_distance_upem=0.001)

    assert pol1.compute_policy_hash() != pol2.compute_policy_hash()

    rep_pass = FidelityEvaluator.evaluate(
        model=model, config=config, fit_records=fit_records, held_out_records=[r_held],
        held_out_pairs=[held_pair], consumer_bundle=bundle, raster_provider=lambda r: b_held,
        thresholds=pol1,
    )
    rep_fail = FidelityEvaluator.evaluate(
        model=model, config=config, fit_records=fit_records, held_out_records=[r_held],
        held_out_pairs=[held_pair], consumer_bundle=bundle, raster_provider=lambda r: b_held,
        thresholds=pol2,
    )

    assert rep_pass.geometry_raster_gate.status == "PASS"
    assert rep_fail.geometry_raster_gate.status == "FAIL"
    assert rep_pass.report_id != rep_fail.report_id


def test_fidelity_rejects_corrupt_and_mismatched_raster_bytes() -> None:
    config = ObservationConfig(resolutions=(128, 256), base_subpixel_phases=((0.0, 0.0),), expanded_subpixel_phases=((0.0, 0.0),))
    cfg_hash = config.compute_hash()

    r_fit, b_fit = _make_observation_record(code_point=65, resolution=128, config_hash=cfg_hash)
    r_fit2, b_fit2 = _make_observation_record(code_point=65, resolution=256, config_hash=cfg_hash)
    r_held, b_held = _make_observation_record(code_point=65, resolution=256, subpixel_x=0.25, subpixel_y=0.25, config_hash=cfg_hash)

    fit_records = [r_fit, r_fit2]
    calib_map = ObservationCalibrator.calibrate_all(fit_records, config=config)
    calib_fp = ObservationCalibrator.compute_calibration_fingerprint(fit_records, config=config)

    glyph = CalibratedGlyph(
        65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50, 50, 550, 700),
        [_make_sample_contour()], observation_fingerprints=calib_map[65].observation_fingerprints,
    )
    model = CanonicalFontModel(
        family_name="F", style_name="R", reference_id="test_font", style_id="regular",
        config_hash=cfg_hash, browser_version="chromium", fit_observations_count=2,
        calibration_fingerprint=calib_fp, glyphs={65: glyph},
    )

    bundle = _make_valid_consumer_bundle(
        model_hash=model.compute_canonical_hash(),
        config_hash=cfg_hash,
        held_out_fp=FidelityEvaluator._compute_records_fingerprint([r_held]),
    )
    held_pair = PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False, provenance="chromium:chromium:canvas_text_metrics")

    corrupt_bytes = b"X" * len(b_held)

    report = FidelityEvaluator.evaluate(
        model=model, config=config, fit_records=fit_records, held_out_records=[r_held],
        held_out_pairs=[held_pair], consumer_bundle=bundle,
        raster_provider=lambda r: corrupt_bytes,
    )
    assert report.overall_status == "FAIL"
    assert report.geometry_raster_gate.status == "FAIL"
    assert any("CORRUPT_RASTER_EVIDENCE" in r for r in report.failure_reasons)


def test_fidelity_rejects_same_raster_sha_under_different_cache_keys() -> None:
    config = ObservationConfig(resolutions=(128, 256), base_subpixel_phases=((0.0, 0.0),), expanded_subpixel_phases=((0.0, 0.0),))
    cfg_hash = config.compute_hash()

    r_fit, b_fit = _make_observation_record(code_point=65, resolution=128, config_hash=cfg_hash)
    r_fit2, b_fit2 = _make_observation_record(code_point=65, resolution=256, config_hash=cfg_hash)
    r_held, _ = _make_observation_record(code_point=65, resolution=256, subpixel_x=0.25, subpixel_y=0.25, config_hash=cfg_hash, raster_bytes=b_fit)

    fit_records = [r_fit, r_fit2]
    calib_map = ObservationCalibrator.calibrate_all(fit_records, config=config)
    calib_fp = ObservationCalibrator.compute_calibration_fingerprint(fit_records, config=config)

    glyph = CalibratedGlyph(
        65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50, 50, 550, 700),
        [_make_sample_contour()], observation_fingerprints=calib_map[65].observation_fingerprints,
    )
    model = CanonicalFontModel(
        family_name="F", style_name="R", reference_id="test_font", style_id="regular",
        config_hash=cfg_hash, browser_version="chromium", fit_observations_count=2,
        calibration_fingerprint=calib_fp, glyphs={65: glyph},
    )

    bundle = _make_valid_consumer_bundle(
        model_hash=model.compute_canonical_hash(),
        config_hash=cfg_hash,
        held_out_fp=FidelityEvaluator._compute_records_fingerprint([r_held]),
    )
    held_pair = PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False, provenance="chromium:chromium:canvas_text_metrics")

    report = FidelityEvaluator.evaluate(
        model=model, config=config, fit_records=fit_records, held_out_records=[r_held],
        held_out_pairs=[held_pair], consumer_bundle=bundle,
        raster_provider=lambda r: b_fit,
    )
    assert report.overall_status == "FAIL"
    assert any("LEAKAGE_DETECTED: Fit and held-out sets share" in r for r in report.failure_reasons)


# =========================================================================
# 5. Positive End-to-End Vertical Slice with Candidate Builder & Validator
# =========================================================================

def test_fidelity_report_e2e_positive_fixture() -> None:
    """Full vertical slice: observations -> calibration -> CanonicalFontModel -> real CandidateFontBuilder artifact -> bound ConsumerEvidenceBundle -> PASS."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        config = ObservationConfig(
            resolutions=(128, 256),
            base_subpixel_phases=((0.0, 0.0),),
            expanded_subpixel_phases=((0.0, 0.0),),
        )
        cfg_hash = config.compute_hash()

        r1_fit, b1_fit = _make_observation_record(code_point=65, resolution=128, advance_width_upem=650.0, config_hash=cfg_hash)
        r2_fit, b2_fit = _make_observation_record(code_point=65, resolution=256, advance_width_upem=650.0, config_hash=cfg_hash)
        r3_fit, b3_fit = _make_observation_record(code_point=66, resolution=128, advance_width_upem=600.0, config_hash=cfg_hash)
        r4_fit, b4_fit = _make_observation_record(code_point=66, resolution=256, advance_width_upem=600.0, config_hash=cfg_hash)

        fit_records = [r1_fit, r2_fit, r3_fit, r4_fit]

        # Separate held-out records at subpixel phase (0.25, 0.25)
        r1_held, b1_held = _make_observation_record(code_point=65, resolution=256, subpixel_x=0.25, subpixel_y=0.25, advance_width_upem=651.0, config_hash=cfg_hash)
        r2_held, b2_held = _make_observation_record(code_point=66, resolution=256, subpixel_x=0.25, subpixel_y=0.25, advance_width_upem=599.0, config_hash=cfg_hash)

        held_out_records = [r1_held, r2_held]
        held_out_rasters = {r1_held.cache_key: b1_held, r2_held.cache_key: b2_held}

        # Calibration step using config
        calibrated_map = ObservationCalibrator.calibrate_all(fit_records, config=config, units_per_em=1000)
        assert 65 in calibrated_map and 66 in calibrated_map

        contour_A = _make_sample_contour()
        contour_B = _make_sample_contour(offset_x=10.0)

        glyph_A = CalibratedGlyph(
            code_point=65,
            character="A",
            advance_width_upem=calibrated_map[65].advance_width_upem,
            lsb_upem=calibrated_map[65].lsb_upem,
            rsb_upem=calibrated_map[65].rsb_upem,
            ascent_upem=calibrated_map[65].ascent_upem,
            descent_upem=calibrated_map[65].descent_upem,
            bounding_box_upem=(50.0, 50.0, 550.0, 700.0),
            contours=[contour_A],
            confidence=calibrated_map[65].confidence,
            observation_fingerprints=calibrated_map[65].observation_fingerprints,
        )
        glyph_B = CalibratedGlyph(
            code_point=66,
            character="B",
            advance_width_upem=calibrated_map[66].advance_width_upem,
            lsb_upem=calibrated_map[66].lsb_upem,
            rsb_upem=calibrated_map[66].rsb_upem,
            ascent_upem=calibrated_map[66].ascent_upem,
            descent_upem=calibrated_map[66].descent_upem,
            bounding_box_upem=(40.0, 50.0, 560.0, 700.0),
            contours=[contour_B],
            confidence=calibrated_map[66].confidence,
            observation_fingerprints=calibrated_map[66].observation_fingerprints,
        )

        fit_pair = PairKerningObservation(
            left_cp=65, right_cp=66, left_char="A", right_char="B",
            left_advance_upem=650.0, right_advance_upem=600.0,
            measured_pair_advance_upem=1230.0, inferred_kerning_upem=-20,
            is_kerning_applied=True, provenance="chromium:chromium:canvas_text_metrics",
        )
        held_out_pair = PairKerningObservation(
            left_cp=66, right_cp=65, left_char="B", right_char="A",
            left_advance_upem=600.0, right_advance_upem=650.0,
            measured_pair_advance_upem=1240.0, inferred_kerning_upem=-10,
            is_kerning_applied=True, provenance="chromium:chromium:canvas_text_metrics",
        )

        calib_fp = ObservationCalibrator.compute_calibration_fingerprint(fit_records, config=config, units_per_em=1000)

        model = CanonicalFontModel(
            schema_version="1.0.0",
            family_name="E2EFont",
            style_name="Regular",
            reference_id="test_font",
            style_id="regular",
            metrics=GlobalFontMetrics(
                units_per_em=1000, ascent_upem=800.0, descent_upem=-200.0, line_gap_upem=0.0,
                cap_height_upem=700.0, x_height_upem=500.0, max_advance_width_upem=1000.0,
                avg_char_width_upem=500.0, underline_position_upem=-100.0, underline_thickness_upem=50.0,
            ),
            glyphs={65: glyph_A, 66: glyph_B},
            kerning_pairs={(65, 66): -20, (66, 65): -10},
            config_hash=cfg_hash,
            browser_version="chromium",
            fit_observations_count=len(fit_records),
            calibration_fingerprint=calib_fp,
        )

        model_hash = model.compute_canonical_hash()
        held_fp = FidelityEvaluator._compute_records_fingerprint(held_out_records)

        # Build real candidate font binaries using MaxCandidateFontBuilder
        builder = MaxCandidateFontBuilder("E2EFont", "Regular", units_per_em=1000)
        reconstructed_glyphs = {
            65: ReconstructedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, contours=[contour_A], bounding_box_upem=(50.0, 50.0, 550.0, 700.0)),
            66: ReconstructedGlyph(66, "B", 600.0, 40.0, 40.0, 750.0, -200.0, contours=[contour_B], bounding_box_upem=(40.0, 50.0, 560.0, 700.0)),
        }
        typo_dataset = TypographyDataset("test_font", "regular", kerning_pairs={(65, 66): -20, (66, 65): -10})
        build_result = builder.build_candidate_family(reconstructed_glyphs, tmp_path, typography=typo_dataset)
        ttf_artifact = build_result.ttf

        # Validate the real candidate artifact with FontTools
        tt = TTFont(ttf_artifact.file_path)
        fmt_result = FormatValidationResult(
            format="ttf",
            file_path=str(ttf_artifact.file_path),
            size_bytes=ttf_artifact.size_bytes,
            sha256_hex=ttf_artifact.sha256_hex,
            is_direct_loadable_fonttools=True,
            is_direct_loadable_freetype=True,
            is_roundtrip_loadable_freetype=True,
            is_direct_loadable_harfbuzz=True,
            is_direct_loadable_chromium=True,
            glyph_count=len(tt.getGlyphOrder()),
            units_per_em=tt["head"].unitsPerEm,
            has_valid_cmap="cmap" in tt,
            has_valid_metrics=("hhea" in tt and "hmtx" in tt),
            decompression_round_trip=True,
            validation_error=None,
        )

        bundle = _make_valid_consumer_bundle(
            model_hash=model_hash,
            config_hash=cfg_hash,
            held_out_fp=held_fp,
            artifact_sha=ttf_artifact.sha256_hex,
            file_path=str(ttf_artifact.file_path),
            records=held_out_records,
            pairs=[held_out_pair],
        )
        bundle = ConsumerEvidenceBundle(
            schema_version="1.0.0",
            model_canonical_hash=model_hash,
            config_hash=cfg_hash,
            held_out_fingerprint=bundle.held_out_fingerprint,
            candidate_artifact_sha=ttf_artifact.sha256_hex,
            fonttools=BoundFontToolsEvidence(candidate_artifact_sha=ttf_artifact.sha256_hex, result=fmt_result),
            freetype=bundle.freetype,
            harfbuzz=bundle.harfbuzz,
            chromium=bundle.chromium,
            held_out_raster_fingerprint=bundle.held_out_raster_fingerprint,
            held_out_typography_fingerprint=bundle.held_out_typography_fingerprint,
        )

        report = FidelityEvaluator.evaluate(
            model=model,
            config=config,
            fit_records=fit_records,
            held_out_records=held_out_records,
            fit_pairs=[fit_pair],
            held_out_pairs=[held_out_pair],
            consumer_bundle=bundle,
            raster_provider=lambda r: held_out_rasters[r.cache_key],
            thresholds=FidelityThresholds(
                min_core_coverage_rate=1.0,
                min_raster_iou=0.85,
                max_chamfer_distance_upem=30.0,
                max_advance_width_delta_upem=5.0,
                max_kerning_delta_upem=5.0,
            ),
        )

        assert report.overall_status == "PASS"
        assert report.coverage_gate.status == "PASS"
        assert report.topology_gate.status == "PASS"
        assert report.geometry_raster_gate.status == "PASS"
        assert report.metrics_gate.status == "PASS"
        assert report.typography_gate.status == "PASS"
        assert report.consumer_gate.status == "PASS"
        assert len(report.failure_reasons) == 0

        assert report.compute_report_hash() == report.compute_report_hash()
