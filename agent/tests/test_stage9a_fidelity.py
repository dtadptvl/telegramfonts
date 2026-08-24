"""Comprehensive test suite for Stage 9A calibration, Canonical FontModel, and fail-closed Fidelity Report."""
from __future__ import annotations

import copy
import io
import math
from typing import Sequence

import numpy as np
import pytest
from PIL import Image, ImageDraw

from fidelity.evaluator import FidelityEvaluator
from fidelity.models import FidelityReport, FidelityThresholds
from measurement.calibration import (
    CalibratedGlyphMetrics,
    CalibrationTransform,
    ObservationCalibrator,
)
from measurement.models import DirectMetrics, ObservationConfig, ObservationRecord
from reconstruction.font_model import (
    CalibratedGlyph,
    CanonicalFontModel,
    GlobalFontMetrics,
)
from reconstruction.models import Contour, CubicSegment, LineSegment, Point2D
from typography.models import PairKerningObservation


def _make_dummy_metrics(
    code_point: int = 65,
    advance_width_upem: float = 650.0,
    lsb_upem: float = 50.0,
    rsb_upem: float = 50.0,
    ascent_upem: float = 750.0,
    descent_upem: float = -200.0,
    bbox_width_upem: float = 500.0,
    bbox_height_upem: float = 650.0,
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
        confidence=1.0,
    )


def _make_observation_record(
    code_point: int = 65,
    resolution: int = 256,
    subpixel_x: float = 0.0,
    subpixel_y: float = 0.0,
    reference_id: str = "test_font",
    style_id: str = "regular",
    config_hash: str = "hash_cfg_123",
    advance_width_upem: float = 650.0,
    bbox_width_upem: float = 500.0,
    bbox_height_upem: float = 650.0,
) -> ObservationRecord:
    metrics = _make_dummy_metrics(
        code_point=code_point,
        advance_width_upem=advance_width_upem,
        bbox_width_upem=bbox_width_upem,
        bbox_height_upem=bbox_height_upem,
    )
    cache_key = ObservationRecord.build_cache_key(
        reference_id=reference_id,
        style_id=style_id,
        code_point=code_point,
        browser_version="chromium",
        resolution=resolution,
        subpixel_x=subpixel_x,
        subpixel_y=subpixel_y,
        config_hash=config_hash,
    )
    return ObservationRecord(
        cache_key=cache_key,
        reference_id=reference_id,
        style_id=style_id,
        code_point=code_point,
        resolution=resolution,
        subpixel_x=subpixel_x,
        subpixel_y=subpixel_y,
        raster_relative_path=f"rasters/{cache_key}.png",
        raster_sha256="a" * 64,
        raster_size_bytes=1024,
        metrics=metrics,
        created_at="2026-08-25T00:00:00Z",
    )


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
    r1 = _make_observation_record(code_point=65, resolution=128, advance_width_upem=650.0)
    r2 = _make_observation_record(code_point=65, resolution=256, advance_width_upem=652.0)
    r3 = _make_observation_record(code_point=65, resolution=512, advance_width_upem=648.0)

    calib1 = ObservationCalibrator.calibrate_glyph_observations([r1, r2, r3])
    calib2 = ObservationCalibrator.calibrate_glyph_observations([r3, r1, r2])
    calib3 = ObservationCalibrator.calibrate_glyph_observations([r2, r3, r1])

    assert calib1.to_dict() == calib2.to_dict() == calib3.to_dict()
    assert calib1.advance_width_upem == 650.0
    assert calib1.sample_count == 3


def test_observation_calibrator_rejects_mismatched_and_corrupt_records() -> None:
    r1 = _make_observation_record(code_point=65, reference_id="font_A")
    r2 = _make_observation_record(code_point=65, reference_id="font_B")

    with pytest.raises(ValueError, match="Reference ID mismatch"):
        ObservationCalibrator.calibrate_glyph_observations([r1, r2])

    r_diff_cp = _make_observation_record(code_point=66, reference_id="font_A")
    with pytest.raises(ValueError, match="Code point mismatch"):
        ObservationCalibrator.calibrate_glyph_observations([r1, r_diff_cp])

    r_corrupt = copy.deepcopy(r1)
    r_corrupt.raster_sha256 = "invalid_short_hash"
    with pytest.raises(ValueError, match="Corrupt or missing raster SHA256"):
        ObservationCalibrator.calibrate_glyph_observations([r_corrupt])


def test_observation_calibrator_missing_required_resolutions() -> None:
    config = ObservationConfig(resolutions=(128, 256, 512))
    r1 = _make_observation_record(code_point=65, resolution=128)
    r2 = _make_observation_record(code_point=65, resolution=256)

    with pytest.raises(ValueError, match="Missing required observation resolutions"):
        ObservationCalibrator.calibrate_glyph_observations([r1, r2], config=config)


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
        config_hash="abc" * 20,
    )

    hash_1 = model.compute_canonical_hash()
    assert len(hash_1) == 64

    json_str = model.to_canonical_json()
    model_restored = CanonicalFontModel.from_canonical_json(json_str)
    assert model_restored.compute_canonical_hash() == hash_1
    assert model_restored.family_name == "TestFamily"
    assert len(model_restored.glyphs) == 2


def test_canonical_font_model_hash_stability_under_reordering() -> None:
    contour_A = _make_sample_contour()
    contour_B = _make_sample_contour(offset_x=10.0)

    glyph_A = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50.0, 50.0, 550.0, 700.0), [contour_A])
    glyph_B = CalibratedGlyph(66, "B", 600.0, 40.0, 40.0, 750.0, -200.0, (40.0, 50.0, 560.0, 700.0), [contour_B])

    model_1 = CanonicalFontModel(
        family_name="ReorderFont",
        style_name="Regular",
        glyphs={65: glyph_A, 66: glyph_B},
        kerning_pairs={(65, 66): -20, (66, 65): -10},
    )

    model_2 = CanonicalFontModel(
        family_name="ReorderFont",
        style_name="Regular",
        glyphs={66: glyph_B, 65: glyph_A},
        kerning_pairs={(66, 65): -10, (65, 66): -20},
    )

    assert model_1.compute_canonical_hash() == model_2.compute_canonical_hash()


def test_canonical_font_model_rejects_unclosed_and_invalid_topology() -> None:
    unclosed_contour = Contour(
        segments=[
            LineSegment(Point2D(0, 0), Point2D(100, 0)),
            LineSegment(Point2D(100, 0), Point2D(100, 100)),
        ]
    )
    glyph_invalid = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (0, 0, 100, 100), [unclosed_contour])
    model = CanonicalFontModel(
        family_name="BadFont",
        style_name="Regular",
        glyphs={65: glyph_invalid},
    )
    report = FidelityEvaluator.evaluate(
        model=model,
        config=ObservationConfig(),
        fit_records=[_make_observation_record(code_point=65, resolution=128)],
        held_out_records=[_make_observation_record(code_point=65, resolution=256)],
    )
    assert report.overall_status == "FAIL"
    assert report.topology_gate.status == "FAIL"
    assert "TOPOLOGY_GATE_FAIL: 1 unclosed contours detected" in report.failure_reasons


# =========================================================================
# 3. Fidelity Gate & Leakage Evaluation Tests
# =========================================================================

def test_fidelity_gate_0_rejects_fit_held_out_leakage() -> None:
    r1 = _make_observation_record(code_point=65, resolution=128)
    r2 = _make_observation_record(code_point=65, resolution=256)

    fit_records = [r1, r2]
    held_out_records = [r1]

    glyph = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50, 50, 550, 700), [_make_sample_contour()])
    model = CanonicalFontModel(family_name="F", style_name="R", glyphs={65: glyph})

    report = FidelityEvaluator.evaluate(
        model=model,
        config=ObservationConfig(),
        fit_records=fit_records,
        held_out_records=held_out_records,
    )
    assert report.overall_status == "FAIL"
    assert any("LEAKAGE_DETECTED" in r for r in report.failure_reasons)


def test_fidelity_coverage_gate_fail() -> None:
    fit_records = [_make_observation_record(code_point=65, resolution=128)]
    held_out_records = [_make_observation_record(code_point=66, resolution=256)]

    glyph = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50, 50, 550, 700), [_make_sample_contour()])
    model = CanonicalFontModel(family_name="F", style_name="R", glyphs={65: glyph})

    report = FidelityEvaluator.evaluate(
        model=model,
        config=ObservationConfig(),
        fit_records=fit_records,
        held_out_records=held_out_records,
        thresholds=FidelityThresholds(min_core_coverage_rate=1.0),
    )
    assert report.overall_status == "FAIL"
    assert report.coverage_gate.status == "FAIL"
    assert report.coverage_gate.missing_core_glyphs == (66,)


def test_fidelity_metrics_gate_bounds() -> None:
    fit_records = [_make_observation_record(code_point=65, resolution=128, advance_width_upem=650.0)]
    held_out_records = [_make_observation_record(code_point=65, resolution=256, advance_width_upem=680.0)]

    glyph = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50, 50, 550, 700), [_make_sample_contour()])
    model = CanonicalFontModel(family_name="F", style_name="R", glyphs={65: glyph})

    report = FidelityEvaluator.evaluate(
        model=model,
        config=ObservationConfig(),
        fit_records=fit_records,
        held_out_records=held_out_records,
        thresholds=FidelityThresholds(max_advance_width_delta_upem=10.0),
    )
    assert report.overall_status == "FAIL"
    assert report.metrics_gate.status == "FAIL"
    assert any("METRICS_GATE_FAIL" in r for r in report.failure_reasons)


def test_fidelity_typography_gate_bounds() -> None:
    fit_records = [_make_observation_record(code_point=65, resolution=128)]
    held_out_records = [_make_observation_record(code_point=65, resolution=256)]

    held_out_pair = PairKerningObservation(
        left_cp=65,
        right_cp=79,
        left_char="A",
        right_char="O",
        left_advance_upem=650.0,
        right_advance_upem=700.0,
        measured_pair_advance_upem=1310.0,
        inferred_kerning_upem=-40,
        is_kerning_applied=True,
    )

    glyph = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50, 50, 550, 700), [_make_sample_contour()])
    model = CanonicalFontModel(family_name="F", style_name="R", glyphs={65: glyph}, kerning_pairs={})

    report = FidelityEvaluator.evaluate(
        model=model,
        config=ObservationConfig(),
        fit_records=fit_records,
        held_out_records=held_out_records,
        held_out_pairs=[held_out_pair],
        thresholds=FidelityThresholds(max_kerning_delta_upem=15.0),
    )
    assert report.overall_status == "FAIL"
    assert report.typography_gate.status == "FAIL"
    assert any("TYPOGRAPHY_GATE_FAIL" in r for r in report.failure_reasons)


# =========================================================================
# 4. End-to-End Vertical Slice Fixture (Positive & Negative)
# =========================================================================

def test_fidelity_report_e2e_positive_fixture() -> None:
    """Full vertical slice: observations -> calibration -> CanonicalFontModel -> FidelityReport PASS."""
    config = ObservationConfig(resolutions=(128, 256, 512))

    # Create fit records at 128 and 256
    fit_records = [
        _make_observation_record(code_point=65, resolution=128, advance_width_upem=650.0, bbox_width_upem=500.0, bbox_height_upem=650.0),
        _make_observation_record(code_point=65, resolution=256, advance_width_upem=650.0, bbox_width_upem=500.0, bbox_height_upem=650.0),
        _make_observation_record(code_point=66, resolution=128, advance_width_upem=600.0, bbox_width_upem=520.0, bbox_height_upem=650.0),
        _make_observation_record(code_point=66, resolution=256, advance_width_upem=600.0, bbox_width_upem=520.0, bbox_height_upem=650.0),
    ]

    # Create separate held-out records at 512
    held_out_records = [
        _make_observation_record(code_point=65, resolution=512, advance_width_upem=651.0, bbox_width_upem=500.0, bbox_height_upem=650.0),
        _make_observation_record(code_point=66, resolution=512, advance_width_upem=599.0, bbox_width_upem=520.0, bbox_height_upem=650.0),
    ]

    # Calibration step
    calibrated_map = ObservationCalibrator.calibrate_all(fit_records, units_per_em=1000)
    assert 65 in calibrated_map and 66 in calibrated_map

    # Build CanonicalFontModel
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
        bounding_box_upem=(40.0, 50.0, 560.0, 700.0),
        contours=[_make_sample_contour(offset_x=10.0)],
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

    model = CanonicalFontModel(
        schema_version="1.0.0",
        family_name="E2EFont",
        style_name="Regular",
        reference_id="test_font",
        style_id="regular",
        metrics=GlobalFontMetrics(units_per_em=1000, ascent_upem=800.0, descent_upem=-200.0),
        glyphs={65: glyph_A, 66: glyph_B},
        kerning_pairs={(65, 66): -20, (66, 65): -10},
        config_hash=config.compute_hash(),
        fit_observations_count=len(fit_records),
    )

    # Provide a simple raster provider for held-out raster comparison
    def mock_raster_provider(r: ObservationRecord) -> np.ndarray:
        transform = CalibrationTransform.from_observation(r.resolution, r.metrics, r.subpixel_x, r.subpixel_y)
        glyph = model.glyphs[r.code_point]
        return FidelityEvaluator._rasterize_glyph_contours(glyph, transform, r.resolution)

    # Evaluate with non-leakage held-out records and pairs
    report = FidelityEvaluator.evaluate(
        model=model,
        config=config,
        fit_records=fit_records,
        held_out_records=held_out_records,
        fit_pairs=[fit_pair],
        held_out_pairs=[held_out_pair],
        raster_provider=mock_raster_provider,
        thresholds=FidelityThresholds(
            min_core_coverage_rate=1.0,
            min_raster_iou=0.90,
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
    assert len(report.failure_reasons) == 0

    # Deterministic report hash check
    rep_hash1 = report.compute_report_hash()
    rep_hash2 = report.compute_report_hash()
    assert rep_hash1 == rep_hash2
    assert len(rep_hash1) == 64


def test_fidelity_report_negative_fixture_missing_gate_fails() -> None:
    """Negative fixture: when required evidence is missing, report MUST fail closed."""
    model = CanonicalFontModel(
        family_name="IncompleteFont",
        style_name="Regular",
        glyphs={},
    )
    report = FidelityEvaluator.evaluate(
        model=model,
        config=ObservationConfig(),
        fit_records=[],
        held_out_records=[],
    )
    assert report.overall_status == "FAIL"
    assert "MISSING_HELD_OUT_EVIDENCE: No held-out records or pairs provided" in report.failure_reasons
    assert report.coverage_gate.status == "FAIL"
