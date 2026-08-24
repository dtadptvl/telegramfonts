"""Comprehensive test suite for Stage 9A calibration, Canonical FontModel, and fail-closed Fidelity Report."""
from __future__ import annotations

import copy
import hashlib
import io
import math
from typing import Sequence

import numpy as np
import pytest
from PIL import Image, ImageDraw

from fidelity.evaluator import FidelityEvaluator
from fidelity.models import (
    ConsumerGateResult,
    FidelityReport,
    FidelityThresholds,
)
from measurement.calibration import (
    CalibratedGlyphMetrics,
    CalibrationTransform,
    ObservationCalibrator,
)
from measurement.models import DirectMetrics, ObservationConfig, ObservationRecord
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
from reconstruction.models import Contour, CubicSegment, LineSegment, Point2D
from typography.models import PairKerningObservation


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
        raster_bytes = _generate_png_bytes(resolution, (50, 50, 550, 700) if code_point == 65 else (40, 50, 560, 700), subpixel_x, subpixel_y)

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
        raster_relative_path=f"rasters/{cache_key}.png",
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


# =========================================================================
# 1. Calibration & Transform Tests
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

    r_corrupt = copy.deepcopy(r1)
    r_corrupt.raster_sha256 = "invalid_short_hash"
    with pytest.raises(ValueError, match="Corrupt or missing raster SHA256"):
        ObservationCalibrator.calibrate_glyph_observations([r_corrupt])


def test_observation_calibrator_rejects_duplicate_and_missing_phases() -> None:
    config = ObservationConfig(
        resolutions=(128, 256, 512),
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
# 2. Canonical FontModel Tests
# =========================================================================

def test_canonical_font_model_validation_and_hashing() -> None:
    contour = _make_sample_contour()
    glyph_A = CalibratedGlyph(
        code_point=65,
        character="A",
        advance_width_upem=650.0,
        lsb_upem=50.0,
        rsb_upem=50.0,
        ascent_upem=750.0,
        descent_upem=-200.0,
        bounding_box_upem=(50.0, 50.0, 550.0, 700.0),
        contours=[contour],
        confidence=1.0,
        observation_fingerprints=("a" * 64, "b" * 64),
    )
    glyph_B = CalibratedGlyph(
        code_point=66,
        character="B",
        advance_width_upem=600.0,
        lsb_upem=40.0,
        rsb_upem=40.0,
        ascent_upem=750.0,
        descent_upem=-200.0,
        bounding_box_upem=(40.0, 50.0, 560.0, 700.0),
        contours=[_make_sample_contour(offset_x=10.0)],
        confidence=0.98,
        observation_fingerprints=("c" * 64,),
    )

    model = CanonicalFontModel(
        schema_version="1.0.0",
        family_name="TestFamily",
        style_name="Regular",
        reference_id="test_family",
        style_id="regular",
        metrics=GlobalFontMetrics(units_per_em=1000, ascent_upem=800.0, descent_upem=-200.0),
        glyphs={65: glyph_A, 66: glyph_B},
        kerning_pairs={(65, 66): -20},
        config_hash="a" * 64,
        browser_version="chromium",
        fit_observations_count=4,
        calibration_fingerprint="b" * 64,
    )

    hash_1 = model.compute_canonical_hash()
    assert len(hash_1) == 64

    json_str = model.to_canonical_json()
    model_restored = CanonicalFontModel.from_canonical_json(json_str)
    assert model_restored.compute_canonical_hash() == hash_1
    assert model_restored.family_name == "TestFamily"
    assert len(model_restored.glyphs) == 2


def test_canonical_font_model_rejects_empty_and_corrupt_models() -> None:
    empty_model = CanonicalFontModel()
    with pytest.raises(ValueError, match="FontModel family_name cannot be empty"):
        empty_model.validate()

    with pytest.raises(ValueError, match="Missing required field in CanonicalFontModel"):
        CanonicalFontModel.from_canonical_dict({})


def test_canonical_font_model_hash_stability_under_reordering() -> None:
    contour_A = _make_sample_contour()
    contour_B = _make_sample_contour(offset_x=10.0)

    glyph_A = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50.0, 50.0, 550.0, 700.0), [contour_A], observation_fingerprints=("a" * 64,))
    glyph_B = CalibratedGlyph(66, "B", 600.0, 40.0, 40.0, 750.0, -200.0, (40.0, 50.0, 560.0, 700.0), [contour_B], observation_fingerprints=("b" * 64,))

    model_1 = CanonicalFontModel(
        family_name="ReorderFont",
        style_name="Regular",
        reference_id="ref",
        style_id="reg",
        config_hash="a" * 64,
        browser_version="chromium",
        fit_observations_count=2,
        calibration_fingerprint="c" * 64,
        glyphs={65: glyph_A, 66: glyph_B},
        kerning_pairs={(65, 66): -20, (66, 65): -10},
    )

    model_2 = CanonicalFontModel(
        family_name="ReorderFont",
        style_name="Regular",
        reference_id="ref",
        style_id="reg",
        config_hash="a" * 64,
        browser_version="chromium",
        fit_observations_count=2,
        calibration_fingerprint="c" * 64,
        glyphs={66: glyph_B, 65: glyph_A},
        kerning_pairs={(66, 65): -10, (65, 66): -20},
    )

    assert model_1.compute_canonical_hash() == model_2.compute_canonical_hash()


# =========================================================================
# 3. Architect Adversarial Reproduction & Fail-Closed Tests
# =========================================================================

def test_adversarial_architect_reproduction_fails_closed() -> None:
    """Exact reproduction from Architect review: config mismatch, no raster provider, no pairs -> must FAIL."""
    config = ObservationConfig(resolutions=(128, 256))
    active_cfg_hash = config.compute_hash()

    r_fit, _ = _make_observation_record(code_point=65, resolution=128, config_hash=active_cfg_hash)
    r_held, _ = _make_observation_record(code_point=65, resolution=256, config_hash=active_cfg_hash)

    glyph = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50, 50, 550, 700), [_make_sample_contour()], observation_fingerprints=("a" * 64,))
    model = CanonicalFontModel(
        family_name="F",
        style_name="R",
        reference_id="test_font",
        style_id="regular",
        config_hash="f" * 64,  # Mismatched config hash!
        browser_version="chromium",
        fit_observations_count=1,
        calibration_fingerprint="c" * 64,
        glyphs={65: glyph},
    )

    report = FidelityEvaluator.evaluate(
        model=model,
        config=config,
        fit_records=[r_fit],
        held_out_records=[r_held],
        raster_provider=None,
        held_out_pairs=(),
    )

    assert report.overall_status == "FAIL"
    assert report.geometry_raster_gate.status == "FAIL"
    assert report.typography_gate.status == "FAIL"
    assert any("CONFIG_HASH_MISMATCH" in r for r in report.failure_reasons)
    assert any("MISSING_RASTER_PROVIDER" in r for r in report.failure_reasons)
    assert any("ZERO_HELD_OUT_TYPOGRAPHY" in r for r in report.failure_reasons)


def test_fidelity_rejects_corrupt_and_mismatched_raster_bytes() -> None:
    config = ObservationConfig(resolutions=(128, 256))
    cfg_hash = config.compute_hash()

    r_fit, b_fit = _make_observation_record(code_point=65, resolution=128, config_hash=cfg_hash)
    r_held, b_held = _make_observation_record(code_point=65, resolution=256, config_hash=cfg_hash)

    glyph = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50, 50, 550, 700), [_make_sample_contour()], observation_fingerprints=("a" * 64,))
    model = CanonicalFontModel(
        family_name="F",
        style_name="R",
        reference_id="test_font",
        style_id="regular",
        config_hash=cfg_hash,
        browser_version="chromium",
        fit_observations_count=1,
        calibration_fingerprint="c" * 64,
        glyphs={65: glyph},
    )

    corrupt_bytes = b"X" * len(b_held)

    def corrupt_provider(r: ObservationRecord) -> bytes:
        return corrupt_bytes

    held_pair = PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False)

    report = FidelityEvaluator.evaluate(
        model=model,
        config=config,
        fit_records=[r_fit],
        held_out_records=[r_held],
        held_out_pairs=[held_pair],
        raster_provider=corrupt_provider,
    )
    assert report.overall_status == "FAIL"
    assert report.geometry_raster_gate.status == "FAIL"
    assert any("CORRUPT_RASTER_EVIDENCE" in r for r in report.failure_reasons)


def test_fidelity_rejects_same_raster_sha_under_different_cache_keys() -> None:
    config = ObservationConfig()
    cfg_hash = config.compute_hash()

    r_fit, b_fit = _make_observation_record(code_point=65, resolution=128, config_hash=cfg_hash)
    r_held, _ = _make_observation_record(code_point=65, resolution=256, config_hash=cfg_hash, raster_bytes=b_fit)

    glyph = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50, 50, 550, 700), [_make_sample_contour()], observation_fingerprints=("a" * 64,))
    model = CanonicalFontModel(
        family_name="F",
        style_name="R",
        reference_id="test_font",
        style_id="regular",
        config_hash=cfg_hash,
        browser_version="chromium",
        fit_observations_count=1,
        calibration_fingerprint="c" * 64,
        glyphs={65: glyph},
    )

    held_pair = PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False)
    report = FidelityEvaluator.evaluate(
        model=model,
        config=config,
        fit_records=[r_fit],
        held_out_records=[r_held],
        held_out_pairs=[held_pair],
        raster_provider=lambda r: b_fit,
    )
    assert report.overall_status == "FAIL"
    assert any("LEAKAGE_DETECTED: Fit and held-out sets share" in r for r in report.failure_reasons)


def test_threshold_non_inertness() -> None:
    """Verify that every advertised threshold actively affects the gate outcome."""
    config = ObservationConfig()
    cfg_hash = config.compute_hash()

    r_fit, b_fit = _make_observation_record(code_point=65, resolution=128, config_hash=cfg_hash)
    r_held, b_held = _make_observation_record(code_point=65, resolution=256, config_hash=cfg_hash, advance_width_upem=658.0)

    glyph = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50, 50, 550, 700), [_make_sample_contour()], confidence=0.85, observation_fingerprints=("a" * 64,))
    model = CanonicalFontModel(
        family_name="F",
        style_name="R",
        reference_id="test_font",
        style_id="regular",
        config_hash=cfg_hash,
        browser_version="chromium",
        fit_observations_count=1,
        calibration_fingerprint="c" * 64,
        glyphs={65: glyph},
    )

    held_pair = PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False)

    # 1. min_confidence: 0.85 >= 0.80 passes, but >= 0.90 fails
    rep_pass = FidelityEvaluator.evaluate(
        model=model, config=config, fit_records=[r_fit], held_out_records=[r_held], held_out_pairs=[held_pair],
        raster_provider=lambda r: b_held, thresholds=FidelityThresholds(min_confidence=0.80, max_advance_width_delta_upem=10.0),
    )
    rep_fail = FidelityEvaluator.evaluate(
        model=model, config=config, fit_records=[r_fit], held_out_records=[r_held], held_out_pairs=[held_pair],
        raster_provider=lambda r: b_held, thresholds=FidelityThresholds(min_confidence=0.90, max_advance_width_delta_upem=10.0),
    )
    assert rep_pass.metrics_gate.status == "PASS"
    assert rep_fail.metrics_gate.status == "FAIL"

    # 2. max_chamfer_distance_upem: generous passes, strict fails
    rep_chamfer_fail = FidelityEvaluator.evaluate(
        model=model, config=config, fit_records=[r_fit], held_out_records=[r_held], held_out_pairs=[held_pair],
        raster_provider=lambda r: b_held, thresholds=FidelityThresholds(max_chamfer_distance_upem=0.001),
    )
    assert rep_chamfer_fail.geometry_raster_gate.status == "FAIL"

    # 3. require_consumers: True with empty consumers fails
    rep_consumer_fail = FidelityEvaluator.evaluate(
        model=model, config=config, fit_records=[r_fit], held_out_records=[r_held], held_out_pairs=[held_pair],
        raster_provider=lambda r: b_held, thresholds=FidelityThresholds(require_consumers=True),
    )
    assert rep_consumer_fail.consumer_gate.status == "FAIL"


def test_independent_consumer_integration() -> None:
    config = ObservationConfig()
    cfg_hash = config.compute_hash()

    r_fit, b_fit = _make_observation_record(code_point=65, resolution=128, config_hash=cfg_hash)
    r_held, b_held = _make_observation_record(code_point=65, resolution=256, config_hash=cfg_hash)

    glyph = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50, 50, 550, 700), [_make_sample_contour()], observation_fingerprints=("a" * 64,))
    model = CanonicalFontModel(
        family_name="F",
        style_name="R",
        reference_id="test_font",
        style_id="regular",
        config_hash=cfg_hash,
        browser_version="chromium",
        fit_observations_count=1,
        calibration_fingerprint="c" * 64,
        glyphs={65: glyph},
    )

    held_pair = PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False)

    bad_fmt = FormatValidationResult(
        format="ttf", file_path="font.ttf", size_bytes=100, sha256_hex="a" * 64,
        is_direct_loadable_fonttools=False,
        is_direct_loadable_freetype=True, is_roundtrip_loadable_freetype=True,
        is_direct_loadable_harfbuzz=True, is_direct_loadable_chromium=True,
        glyph_count=1, units_per_em=1000, has_valid_cmap=True, has_valid_metrics=True,
        decompression_round_trip=True,
    )

    report_fail = FidelityEvaluator.evaluate(
        model=model, config=config, fit_records=[r_fit], held_out_records=[r_held], held_out_pairs=[held_pair],
        consumer_results=[bad_fmt], raster_provider=lambda r: b_held,
    )
    assert report_fail.overall_status == "FAIL"
    assert report_fail.consumer_gate.status == "FAIL"
    assert not report_fail.consumer_gate.fonttools_passed


# =========================================================================
# 4. Positive End-to-End Vertical Slice Fixture
# =========================================================================

def test_fidelity_report_e2e_positive_fixture() -> None:
    """Full vertical slice: observations -> calibration -> CanonicalFontModel -> held-out consumers -> FidelityReport PASS."""
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

    glyph_A = CalibratedGlyph(
        code_point=65,
        character="A",
        advance_width_upem=calibrated_map[65].advance_width_upem,
        lsb_upem=calibrated_map[65].lsb_upem,
        rsb_upem=calibrated_map[65].rsb_upem,
        ascent_upem=calibrated_map[65].ascent_upem,
        descent_upem=calibrated_map[65].descent_upem,
        bounding_box_upem=(50.0, 50.0, 550.0, 700.0),
        contours=[_make_sample_contour()],
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
        bounding_box_upem=(50.0, 50.0, 550.0, 700.0),
        contours=[_make_sample_contour()],
        confidence=calibrated_map[66].confidence,
        observation_fingerprints=calibrated_map[66].observation_fingerprints,
    )

    fit_pair = PairKerningObservation(
        left_cp=65, right_cp=66, left_char="A", right_char="B",
        left_advance_upem=650.0, right_advance_upem=600.0,
        measured_pair_advance_upem=1230.0, inferred_kerning_upem=-20,
        is_kerning_applied=True,
    )
    held_out_pair = PairKerningObservation(
        left_cp=66, right_cp=65, left_char="B", right_char="A",
        left_advance_upem=600.0, right_advance_upem=650.0,
        measured_pair_advance_upem=1240.0, inferred_kerning_upem=-10,
        is_kerning_applied=True,
    )

    calib_fp = hashlib.sha256((calibrated_map[65].calibration_fingerprint + calibrated_map[66].calibration_fingerprint).encode("utf-8")).hexdigest()

    model = CanonicalFontModel(
        schema_version="1.0.0",
        family_name="E2EFont",
        style_name="Regular",
        reference_id="test_font",
        style_id="regular",
        metrics=GlobalFontMetrics(units_per_em=1000, ascent_upem=800.0, descent_upem=-200.0),
        glyphs={65: glyph_A, 66: glyph_B},
        kerning_pairs={(65, 66): -20, (66, 65): -10},
        config_hash=cfg_hash,
        browser_version="chromium",
        fit_observations_count=len(fit_records),
        calibration_fingerprint=calib_fp,
    )

    valid_fmt = FormatValidationResult(
        format="ttf", file_path="font.ttf", size_bytes=1024, sha256_hex="a" * 64,
        is_direct_loadable_fonttools=True, is_direct_loadable_freetype=True,
        is_roundtrip_loadable_freetype=True, is_direct_loadable_harfbuzz=True,
        is_direct_loadable_chromium=True, glyph_count=2, units_per_em=1000,
        has_valid_cmap=True, has_valid_metrics=True, decompression_round_trip=True,
    )

    report = FidelityEvaluator.evaluate(
        model=model,
        config=config,
        fit_records=fit_records,
        held_out_records=held_out_records,
        fit_pairs=[fit_pair],
        held_out_pairs=[held_out_pair],
        consumer_results=[valid_fmt],
        raster_provider=lambda r: held_out_rasters[r.cache_key],
        thresholds=FidelityThresholds(
            min_core_coverage_rate=1.0,
            min_raster_iou=0.85,
            max_chamfer_distance_upem=30.0,
            max_advance_width_delta_upem=5.0,
            max_kerning_delta_upem=5.0,
            require_consumers=True,
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
