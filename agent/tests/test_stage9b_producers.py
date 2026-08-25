"""Comprehensive test suite for Stage 9B production four-consumer evidence producers."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import math
import os
import tempfile
from pathlib import Path
from typing import Sequence
from unittest.mock import patch

import numpy as np
import pytest
from PIL import Image, ImageDraw

import fidelity
from fidelity.evaluator import FidelityEvaluator, validate_consumer_gate
from fidelity.models import (
    BoundChromiumEvidence,
    BoundFontToolsEvidence,
    BoundFreeTypeEvidence,
    BoundHarfBuzzEvidence,
    ChromiumGlyphSampleEvidence,
    ChromiumPairSampleEvidence,
    ConsumerEvidenceBundle,
    FidelityReport,
    FidelityThresholds,
    FreeTypeSampleEvidence,
    HarfBuzzPositionVector,
    HarfBuzzSampleEvidence,
    ProductionProducerError,
)
from fidelity.producers import (
    CandidateArtifact,
    CandidateArtifactDescriptor,
    ChromiumEvidenceProducer,
    FontToolsEvidenceProducer,
    FreeTypeEvidenceProducer,
    HarfBuzzEvidenceProducer,
    ProductionConsumerEvidenceProducer,
)
from measurement.browser_session import ChromiumSession, find_chromium_executable
from measurement.calibration import ObservationCalibrator
from measurement.models import DirectMetrics, ObservationConfig, ObservationRecord
from reconstruction.candidate_builder import MaxCandidateFontBuilder
from reconstruction.candidate_validator import (
    ChromiumPairMetricResult,
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


# =========================================================================
# 1. Merge-Blocking Reproductions
# =========================================================================

def test_architect_reproduction_1_harfbuzz_zero_positions_fails_consumer_gate() -> None:
    """Reproduction 1: HarfBuzz positions (0,0,0,0) vs expected 650 UPEM each must FAIL gate even if caller claims pos_delta=0."""
    art = CandidateArtifact.from_source(b"\x00\x01\x00\x00" + b"\x00" * 100, format_hint="TTF")
    cfg = ObservationConfig()
    cfg_hash = cfg.compute_hash()
    rec, png_bytes = _make_observation_record(code_point=65, config_hash=cfg_hash)
    glyph = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50.0, 50.0, 550.0, 700.0), [_make_sample_contour()], observation_fingerprints=("a" * 64,))
    model = CanonicalFontModel(
        family_name="F", style_name="R", reference_id="ref", style_id="reg",
        config_hash=cfg_hash, browser_version="chromium", fit_observations_count=1,
        calibration_fingerprint="b" * 64, glyphs={65: glyph},
    )
    pair = PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False, provenance="chromium:chromium:canvas_text_metrics")

    ft_res = FormatValidationResult("TTF", "f.ttf", 100, art.sha256_hex, True, False, False, False, False, 1, 1000, True, True, True)
    fr_sample = FreeTypeSampleEvidence(rec.cache_key, 65, "A", 256, rec.raster_sha256, 0.95, 0)
    fr_res = RasterComparisonResult(65, "A", 256, 0.95, 0, samples=(fr_sample,), min_raster_iou=0.95)

    cr_glyph_s = ChromiumGlyphSampleEvidence(65, "A", 650.0, 650.0, 0.0)
    cr_pair_s = ChromiumPairSampleEvidence(65, 65, "AA", 1300.0, 1300.0, 1300.0, 0.0, 0.0, True)
    cr_res = ChromiumValidationResult(True, "cr", True, True, 1, 0.0, [], glyph_samples=(cr_glyph_s,), pair_samples=(cr_pair_s,), fit_pairs_material_improvement=True, held_out_pairs_non_regression=True, rendered_canvas_valid=True)

    # Caller claims max_position_delta_upem=0.0 and advance_delta_upem=0.0, but positions are (0,0,0,0)
    zero_pos = HarfBuzzPositionVector(0.0, 0.0, 0.0, 0.0)
    hb_sample_bad = HarfBuzzSampleEvidence(
        65, 65, "AA", True, True, (1, 1), (0, 1), (zero_pos, zero_pos),
        candidate_total_advance_upem=0.0, expected_total_advance_upem=1300.0,
        advance_delta_upem=0.0, max_position_delta_upem=0.0,
    )
    hb_res = ShapingTestResult(
        "AA", "c", True, True, ["A", "A"], ["A", "A"], 2, 2, 0, 1300, 0, 0,
        samples=(hb_sample_bad,), all_in_cmap=True, all_sequence_match=True,
    )

    bundle = ConsumerEvidenceBundle(
        schema_version="1.0.0",
        model_canonical_hash=model.compute_canonical_hash(),
        config_hash=cfg_hash,
        held_out_fingerprint=FidelityEvaluator._compute_composite_held_out_fingerprint([rec], [pair]),
        held_out_raster_fingerprint=FidelityEvaluator._compute_records_fingerprint([rec]),
        held_out_typography_fingerprint=FidelityEvaluator._compute_typography_fingerprint([pair]),
        candidate_artifact_sha=art.sha256_hex,
        fonttools=BoundFontToolsEvidence(candidate_artifact_sha=art.sha256_hex, result=ft_res),
        freetype=BoundFreeTypeEvidence(candidate_artifact_sha=art.sha256_hex, result=fr_res),
        harfbuzz=BoundHarfBuzzEvidence(candidate_artifact_sha=art.sha256_hex, result=hb_res),
        chromium=BoundChromiumEvidence(candidate_artifact_sha=art.sha256_hex, result=cr_res),
    )

    gate_res, gate_errs = validate_consumer_gate(
        bundle=bundle,
        model=model,
        config=cfg,
        held_out_records=[rec],
        held_out_pairs=[pair],
    )
    assert gate_res.status == "FAIL"
    assert gate_res.harfbuzz_passed is False
    assert any("HARFBUZZ_POSITION_DELTA_EXCEEDED" in err or "HARFBUZZ_SAMPLE_VALIDATION_FAILED" in err for err in gate_errs)


def test_architect_reproduction_2_chromium_pair_candidate_advance_drift_fails_gate() -> None:
    """Reproduction 2: Chromium pair candidate 1650 vs expected 1300 (delta 350) must FAIL gate even if caller claims non_regression=True."""
    art = CandidateArtifact.from_source(b"\x00\x01\x00\x00" + b"\x00" * 100, format_hint="TTF")
    cfg = ObservationConfig()
    cfg_hash = cfg.compute_hash()
    rec, png_bytes = _make_observation_record(code_point=65, config_hash=cfg_hash)
    glyph = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50.0, 50.0, 550.0, 700.0), [_make_sample_contour()], observation_fingerprints=("a" * 64,))
    model = CanonicalFontModel(
        family_name="F", style_name="R", reference_id="ref", style_id="reg",
        config_hash=cfg_hash, browser_version="chromium", fit_observations_count=1,
        calibration_fingerprint="b" * 64, glyphs={65: glyph},
    )
    pair = PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False, provenance="chromium:chromium:canvas_text_metrics")

    ft_res = FormatValidationResult("TTF", "f.ttf", 100, art.sha256_hex, True, False, False, False, False, 1, 1000, True, True, True)
    fr_sample = FreeTypeSampleEvidence(rec.cache_key, 65, "A", 256, rec.raster_sha256, 0.95, 0)
    fr_res = RasterComparisonResult(65, "A", 256, 0.95, 0, samples=(fr_sample,), min_raster_iou=0.95)

    pos_vec = HarfBuzzPositionVector(650.0, 0.0, 0.0, 0.0)
    hb_sample = HarfBuzzSampleEvidence(65, 65, "AA", True, True, (1, 1), (0, 1), (pos_vec, pos_vec), 1300.0, 1300.0, 0.0, 0.0)
    hb_res = ShapingTestResult("AA", "c", True, True, ["A", "A"], ["A", "A"], 2, 2, 1300, 1300, 0, 0, samples=(hb_sample,), all_in_cmap=True, all_sequence_match=True)

    # Candidate pair advance 1650 vs expected 1300 (delta 350), caller claims non_regression=True
    cr_glyph_s = ChromiumGlyphSampleEvidence(65, "A", 650.0, 650.0, 0.0)
    cr_bad_pair_s = ChromiumPairSampleEvidence(
        65, 65, "AA", baseline_single_sum_upem=1300.0, candidate_pair_advance_upem=1650.0,
        expected_pair_advance_upem=1300.0, gpos_applied_adjustment_upem=350.0, advance_delta_upem=350.0,
        non_regression=True,
    )
    cr_res = ChromiumValidationResult(
        True, "cr", True, True, 1, 0.0, [], glyph_samples=(cr_glyph_s,), pair_samples=(cr_bad_pair_s,),
        fit_pairs_material_improvement=True, held_out_pairs_non_regression=True, rendered_canvas_valid=True,
    )

    bundle = ConsumerEvidenceBundle(
        schema_version="1.0.0",
        model_canonical_hash=model.compute_canonical_hash(),
        config_hash=cfg_hash,
        held_out_fingerprint=FidelityEvaluator._compute_composite_held_out_fingerprint([rec], [pair]),
        held_out_raster_fingerprint=FidelityEvaluator._compute_records_fingerprint([rec]),
        held_out_typography_fingerprint=FidelityEvaluator._compute_typography_fingerprint([pair]),
        candidate_artifact_sha=art.sha256_hex,
        fonttools=BoundFontToolsEvidence(candidate_artifact_sha=art.sha256_hex, result=ft_res),
        freetype=BoundFreeTypeEvidence(candidate_artifact_sha=art.sha256_hex, result=fr_res),
        harfbuzz=BoundHarfBuzzEvidence(candidate_artifact_sha=art.sha256_hex, result=hb_res),
        chromium=BoundChromiumEvidence(candidate_artifact_sha=art.sha256_hex, result=cr_res),
    )

    gate_res, gate_errs = validate_consumer_gate(
        bundle=bundle,
        model=model,
        config=cfg,
        held_out_records=[rec],
        held_out_pairs=[pair],
    )
    assert gate_res.status == "FAIL"
    assert gate_res.chromium_passed is False
    assert any("CHROMIUM_PAIR_DELTA_EXCEEDED" in err or "CHROMIUM_PAIR_REGRESSION" in err for err in gate_errs)


def test_architect_reproduction_3_freetype_aggregate_mismatch_fails_consumer_gate() -> None:
    """Reproduction 3: FreeType sample IoU 0.95 with aggregate min/mean 0 or pixel delta 999999 must FAIL gate."""
    art = CandidateArtifact.from_source(b"\x00\x01\x00\x00" + b"\x00" * 100, format_hint="TTF")
    cfg = ObservationConfig()
    cfg_hash = cfg.compute_hash()
    rec, png_bytes = _make_observation_record(code_point=65, config_hash=cfg_hash)
    glyph = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50.0, 50.0, 550.0, 700.0), [_make_sample_contour()], observation_fingerprints=("a" * 64,))
    model = CanonicalFontModel(
        family_name="F", style_name="R", reference_id="ref", style_id="reg",
        config_hash=cfg_hash, browser_version="chromium", fit_observations_count=1,
        calibration_fingerprint="b" * 64, glyphs={65: glyph},
    )
    pair = PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False, provenance="chromium:chromium:canvas_text_metrics")

    ft_res = FormatValidationResult("TTF", "f.ttf", 100, art.sha256_hex, True, False, False, False, False, 1, 1000, True, True, True)

    # Sample IoU is 0.95 with 0 deltas, but aggregate min/mean is 0.0 and pixel_delta_count is 999999
    fr_sample = FreeTypeSampleEvidence(rec.cache_key, 65, "A", 256, rec.raster_sha256, 0.95, 0)
    fr_res_mismatch = RasterComparisonResult(65, "A", 256, 0.0, 999999, samples=(fr_sample,), min_raster_iou=0.0)

    pos_vec = HarfBuzzPositionVector(650.0, 0.0, 0.0, 0.0)
    hb_sample = HarfBuzzSampleEvidence(65, 65, "AA", True, True, (1, 1), (0, 1), (pos_vec, pos_vec), 1300.0, 1300.0, 0.0, 0.0)
    hb_res = ShapingTestResult("AA", "c", True, True, ["A", "A"], ["A", "A"], 2, 2, 1300, 1300, 0, 0, samples=(hb_sample,), all_in_cmap=True, all_sequence_match=True)

    cr_glyph_s = ChromiumGlyphSampleEvidence(65, "A", 650.0, 650.0, 0.0)
    cr_pair_s = ChromiumPairSampleEvidence(65, 65, "AA", 1300.0, 1300.0, 1300.0, 0.0, 0.0, True)
    cr_res = ChromiumValidationResult(True, "cr", True, True, 1, 0.0, [], glyph_samples=(cr_glyph_s,), pair_samples=(cr_pair_s,), fit_pairs_material_improvement=True, held_out_pairs_non_regression=True, rendered_canvas_valid=True)

    bundle = ConsumerEvidenceBundle(
        schema_version="1.0.0",
        model_canonical_hash=model.compute_canonical_hash(),
        config_hash=cfg_hash,
        held_out_fingerprint=FidelityEvaluator._compute_composite_held_out_fingerprint([rec], [pair]),
        held_out_raster_fingerprint=FidelityEvaluator._compute_records_fingerprint([rec]),
        held_out_typography_fingerprint=FidelityEvaluator._compute_typography_fingerprint([pair]),
        candidate_artifact_sha=art.sha256_hex,
        fonttools=BoundFontToolsEvidence(candidate_artifact_sha=art.sha256_hex, result=ft_res),
        freetype=BoundFreeTypeEvidence(candidate_artifact_sha=art.sha256_hex, result=fr_res_mismatch),
        harfbuzz=BoundHarfBuzzEvidence(candidate_artifact_sha=art.sha256_hex, result=hb_res),
        chromium=BoundChromiumEvidence(candidate_artifact_sha=art.sha256_hex, result=cr_res),
    )

    gate_res, gate_errs = validate_consumer_gate(
        bundle=bundle,
        model=model,
        config=cfg,
        held_out_records=[rec],
        held_out_pairs=[pair],
    )
    assert gate_res.status == "FAIL"
    assert gate_res.freetype_passed is False
    assert any("FREETYPE_AGGREGATE_MISMATCH" in err for err in gate_errs)


def test_architect_reproduction_4_raw_freetype_error_sentinel_sanitized() -> None:
    """Reproduction 4: Raw FreeType error sentinels must be absent from ProductionProducerError."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        file_a = tmp_path / "font.ttf"
        font_bytes = b"\x00\x01\x00\x00" + b"\x00" * 100
        file_a.write_bytes(font_bytes)
        desc = CandidateArtifactDescriptor(file_a, "TTF", len(font_bytes), hashlib.sha256(font_bytes).hexdigest(), font_bytes)

        cfg = ObservationConfig()
        cfg_hash = cfg.compute_hash()
        rec, png_bytes = _make_observation_record(code_point=65, config_hash=cfg_hash)
        glyph = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50.0, 50.0, 550.0, 700.0), [_make_sample_contour()], observation_fingerprints=("a" * 64,))
        model = CanonicalFontModel(
            family_name="F", style_name="R", reference_id="ref", style_id="reg",
            config_hash=cfg_hash, browser_version="chromium", fit_observations_count=1,
            calibration_fingerprint="b" * 64, glyphs={65: glyph},
        )
        pair = PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False, provenance="chromium:chromium:canvas_text_metrics")

        ft_res = FormatValidationResult("TTF", str(file_a), len(font_bytes), desc.expected_sha256_hex, True, False, False, False, False, 1, 1000, True, True, True)
        ft_evidence = BoundFontToolsEvidence(desc.expected_sha256_hex, ft_res)

        raw_sentinel = "FreeTypeRenderError: CRASH_RAW_SENTINEL_AT_/tmp/secret/private_font.ttf"
        fr_sample = FreeTypeSampleEvidence(rec.cache_key, 65, "A", 256, rec.raster_sha256, 0.0, 0, render_error=raw_sentinel)
        fr_res = RasterComparisonResult(65, "A", 256, 0.0, 0, render_error=raw_sentinel, samples=(fr_sample,), min_raster_iou=0.0)
        fr_evidence = BoundFreeTypeEvidence(desc.expected_sha256_hex, fr_res)

        pos_vec = HarfBuzzPositionVector(650.0, 0.0, 0.0, 0.0)
        hb_sample = HarfBuzzSampleEvidence(65, 65, "AA", True, True, (1, 1), (0, 1), (pos_vec, pos_vec), 1300.0, 1300.0, 0.0, 0.0)
        hb_res = ShapingTestResult("AA", "c", True, True, ["A", "A"], ["A", "A"], 2, 2, 1300, 1300, 0, 0, samples=(hb_sample,), all_in_cmap=True, all_sequence_match=True)
        hb_evidence = BoundHarfBuzzEvidence(desc.expected_sha256_hex, hb_res)

        cr_glyph_s = ChromiumGlyphSampleEvidence(65, "A", 650.0, 650.0, 0.0)
        cr_pair_s = ChromiumPairSampleEvidence(65, 65, "AA", 1300.0, 1300.0, 1300.0, 0.0, 0.0, True)
        cr_res = ChromiumValidationResult(True, "cr", True, True, 1, 0.0, [], glyph_samples=(cr_glyph_s,), pair_samples=(cr_pair_s,), fit_pairs_material_improvement=True, held_out_pairs_non_regression=True, rendered_canvas_valid=True)
        cr_evidence = BoundChromiumEvidence(desc.expected_sha256_hex, cr_res)

        with patch.object(FontToolsEvidenceProducer, "produce", return_value=ft_evidence):
            with patch.object(FreeTypeEvidenceProducer, "produce", return_value=fr_evidence):
                with patch.object(HarfBuzzEvidenceProducer, "produce", return_value=hb_evidence):
                    with patch.object(ChromiumEvidenceProducer, "produce", return_value=cr_evidence):
                        with pytest.raises(ProductionProducerError) as exc_info:
                            asyncio.run(
                                ProductionConsumerEvidenceProducer.produce_bundle(
                                    descriptor=desc,
                                    model=model,
                                    config=cfg,
                                    held_out_records=[rec],
                                    held_out_pairs=[pair],
                                    raster_provider=lambda r: png_bytes,
                                )
                            )
                        err_msg = str(exc_info.value)
                        assert "CONSUMER_PRODUCER_FAILED" in err_msg
                        assert raw_sentinel not in err_msg
                        assert "CRASH_RAW_SENTINEL" not in err_msg
                        assert "/tmp/secret" not in err_msg
                        assert "FREETYPE_SAMPLE_RENDER_FAILED" in err_msg


def test_builder_file_existence_and_descriptor_validation() -> None:
    """CandidateArtifact.from_descriptor must raise FileNotFoundError if builder file is missing."""
    attested_bytes = b"\x00\x01\x00\x00" + b"\x00" * 100
    sha = hashlib.sha256(attested_bytes).hexdigest()
    desc = CandidateArtifactDescriptor(
        file_path="definitely-missing-builder.ttf",
        expected_format="TTF",
        expected_size_bytes=len(attested_bytes),
        expected_sha256_hex=sha,
        raw_bytes=attested_bytes,
    )
    with pytest.raises(FileNotFoundError, match="ARTIFACT_FILE_NOT_FOUND"):
        CandidateArtifact.from_descriptor(desc)

    with pytest.raises(ValueError, match="INVALID_EXPECTED_SHA256"):
        CandidateArtifactDescriptor(
            file_path="font.ttf",
            expected_format="TTF",
            expected_size_bytes=100,
            expected_sha256_hex=sha.upper(),
        ).validate()

    with pytest.raises(ValueError, match="INVALID_EXPECTED_SHA256"):
        CandidateArtifactDescriptor(
            file_path="font.ttf",
            expected_format="TTF",
            expected_size_bytes=100,
            expected_sha256_hex="z" * 64,
        ).validate()


def test_missing_component_fingerprints_fails_consumer_gate() -> None:
    """Reproduction 3: Missing held_out_raster_fingerprint or held_out_typography_fingerprint must FAIL ConsumerGate."""
    art = CandidateArtifact.from_source(b"\x00\x01\x00\x00" + b"\x00" * 100, format_hint="TTF")
    cfg = ObservationConfig()
    cfg_hash = cfg.compute_hash()
    rec, png_bytes = _make_observation_record(code_point=65, config_hash=cfg_hash)
    glyph = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50.0, 50.0, 550.0, 700.0), [_make_sample_contour()], observation_fingerprints=("a" * 64,))
    model = CanonicalFontModel(
        family_name="F", style_name="R", reference_id="ref", style_id="reg",
        config_hash=cfg_hash, browser_version="chromium", fit_observations_count=1,
        calibration_fingerprint="b" * 64, glyphs={65: glyph},
    )
    pair = PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False, provenance="chromium:chromium:canvas_text_metrics")

    ft_res = FormatValidationResult("TTF", "f.ttf", 100, art.sha256_hex, True, False, False, False, False, 1, 1000, True, True, True)
    fr_sample = FreeTypeSampleEvidence(rec.cache_key, 65, "A", 256, rec.raster_sha256, 0.95, 0)
    fr_res = RasterComparisonResult(65, "A", 256, 0.95, 0, samples=(fr_sample,), min_raster_iou=0.95)

    pos_vec = HarfBuzzPositionVector(650.0, 0.0, 0.0, 0.0)
    hb_sample = HarfBuzzSampleEvidence(65, 65, "AA", True, True, (1, 1), (0, 1), (pos_vec, pos_vec), 1300.0, 1300.0, 0.0, 0.0)
    hb_res = ShapingTestResult("AA", "c", True, True, ["A", "A"], ["A", "A"], 2, 2, 1300, 1300, 0, 0, samples=(hb_sample,), all_in_cmap=True, all_sequence_match=True)

    cr_glyph_s = ChromiumGlyphSampleEvidence(65, "A", 650.0, 650.0, 0.0)
    cr_pair_s = ChromiumPairSampleEvidence(65, 65, "AA", 1300.0, 1300.0, 1300.0, 0.0, 0.0, True)
    cr_res = ChromiumValidationResult(True, "cr", True, True, 1, 0.0, [], glyph_samples=(cr_glyph_s,), pair_samples=(cr_pair_s,), fit_pairs_material_improvement=True, held_out_pairs_non_regression=True, rendered_canvas_valid=True)

    r_fp = FidelityEvaluator._compute_records_fingerprint([rec])
    t_fp = FidelityEvaluator._compute_typography_fingerprint([pair])
    comp_fp = FidelityEvaluator._compute_composite_held_out_fingerprint([rec], [pair])

    # 1. Test missing typography fingerprint
    bundle_no_typo = ConsumerEvidenceBundle(
        schema_version="1.0.0",
        model_canonical_hash=model.compute_canonical_hash(),
        config_hash=cfg_hash,
        held_out_fingerprint=comp_fp,
        held_out_raster_fingerprint=r_fp,
        held_out_typography_fingerprint="",
        candidate_artifact_sha=art.sha256_hex,
        fonttools=BoundFontToolsEvidence(candidate_artifact_sha=art.sha256_hex, result=ft_res),
        freetype=BoundFreeTypeEvidence(candidate_artifact_sha=art.sha256_hex, result=fr_res),
        harfbuzz=BoundHarfBuzzEvidence(candidate_artifact_sha=art.sha256_hex, result=hb_res),
        chromium=BoundChromiumEvidence(candidate_artifact_sha=art.sha256_hex, result=cr_res),
    )
    direct_errs = bundle_no_typo.validate_bindings(
        expected_model_hash=model.compute_canonical_hash(),
        expected_config_hash=cfg_hash,
        expected_held_out_fingerprint=comp_fp,
        expected_raster_fingerprint=r_fp,
        expected_typography_fingerprint=t_fp,
    )
    assert any("MISSING_TYPOGRAPHY_FINGERPRINT" in err for err in direct_errs)

    report = FidelityEvaluator.evaluate(
        model=model,
        config=cfg,
        fit_records=[rec],
        held_out_records=[rec],
        held_out_pairs=[pair],
        consumer_bundle=bundle_no_typo,
        raster_provider=lambda r: png_bytes,
    )
    assert report.overall_status == "FAIL"
    assert report.consumer_gate.status == "FAIL"
    assert any("MISSING_TYPOGRAPHY_FINGERPRINT" in r for r in report.failure_reasons)

    # 2. Test missing raster fingerprint
    bundle_no_raster = ConsumerEvidenceBundle(
        schema_version="1.0.0",
        model_canonical_hash=model.compute_canonical_hash(),
        config_hash=cfg_hash,
        held_out_fingerprint=comp_fp,
        held_out_raster_fingerprint="",
        held_out_typography_fingerprint=t_fp,
        candidate_artifact_sha=art.sha256_hex,
        fonttools=BoundFontToolsEvidence(candidate_artifact_sha=art.sha256_hex, result=ft_res),
        freetype=BoundFreeTypeEvidence(candidate_artifact_sha=art.sha256_hex, result=fr_res),
        harfbuzz=BoundHarfBuzzEvidence(candidate_artifact_sha=art.sha256_hex, result=hb_res),
        chromium=BoundChromiumEvidence(candidate_artifact_sha=art.sha256_hex, result=cr_res),
    )
    report_r = FidelityEvaluator.evaluate(
        model=model,
        config=cfg,
        fit_records=[rec],
        held_out_records=[rec],
        held_out_pairs=[pair],
        consumer_bundle=bundle_no_raster,
        raster_provider=lambda r: png_bytes,
    )
    assert report_r.overall_status == "FAIL"
    assert report_r.consumer_gate.status == "FAIL"
    assert any("MISSING_RASTER_FINGERPRINT" in r for r in report_r.failure_reasons)


# =========================================================================
# 4. Zero Samples & Descriptor Drift Tests
# =========================================================================

def test_zero_sample_bundle_fails_consumer_gate() -> None:
    """Bundle with empty sample collections must FAIL closed even if aggregate fields claim PASS."""
    art = CandidateArtifact.from_source(b"\x00\x01\x00\x00" + b"\x00" * 100, format_hint="TTF")
    cfg = ObservationConfig()
    cfg_hash = cfg.compute_hash()
    rec, png_bytes = _make_observation_record(code_point=65, config_hash=cfg_hash)
    glyph = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50.0, 50.0, 550.0, 700.0), [_make_sample_contour()], observation_fingerprints=("a" * 64,))
    model = CanonicalFontModel(
        family_name="F", style_name="R", reference_id="ref", style_id="reg",
        config_hash=cfg_hash, browser_version="chromium", fit_observations_count=1,
        calibration_fingerprint="b" * 64, glyphs={65: glyph},
    )
    pair = PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False, provenance="chromium:chromium:canvas_text_metrics")

    # Aggregate fields claim PASS, but samples are empty tuple ()
    ft_res = FormatValidationResult("TTF", "f.ttf", 100, art.sha256_hex, True, False, False, False, False, 1, 1000, True, True, True)
    fr_res = RasterComparisonResult(65, "A", 256, 0.95, 0, samples=(), min_raster_iou=0.95)
    hb_res = ShapingTestResult("AA", "c", True, True, ["A", "A"], ["A", "A"], 2, 2, 1300, 1300, 0, 0, samples=(), all_in_cmap=True, all_sequence_match=True)
    cr_res = ChromiumValidationResult(True, "cr", True, True, 1, 0.0, [], glyph_samples=(), pair_samples=(), fit_pairs_material_improvement=True, held_out_pairs_non_regression=True, rendered_canvas_valid=True)

    bundle = ConsumerEvidenceBundle(
        schema_version="1.0.0",
        model_canonical_hash=model.compute_canonical_hash(),
        config_hash=cfg_hash,
        held_out_fingerprint=FidelityEvaluator._compute_composite_held_out_fingerprint([rec], [pair]),
        held_out_raster_fingerprint=FidelityEvaluator._compute_records_fingerprint([rec]),
        held_out_typography_fingerprint=FidelityEvaluator._compute_typography_fingerprint([pair]),
        candidate_artifact_sha=art.sha256_hex,
        fonttools=BoundFontToolsEvidence(candidate_artifact_sha=art.sha256_hex, result=ft_res),
        freetype=BoundFreeTypeEvidence(candidate_artifact_sha=art.sha256_hex, result=fr_res),
        harfbuzz=BoundHarfBuzzEvidence(candidate_artifact_sha=art.sha256_hex, result=hb_res),
        chromium=BoundChromiumEvidence(candidate_artifact_sha=art.sha256_hex, result=cr_res),
    )

    report = FidelityEvaluator.evaluate(
        model=model,
        config=cfg,
        fit_records=[rec],
        held_out_records=[rec],
        held_out_pairs=[pair],
        consumer_bundle=bundle,
        raster_provider=lambda r: png_bytes,
    )
    assert report.overall_status == "FAIL"
    assert report.consumer_gate.status == "FAIL"
    assert report.consumer_gate.freetype_passed is False
    assert report.consumer_gate.harfbuzz_passed is False
    assert report.consumer_gate.chromium_passed is False


# =========================================================================
# 5. Full Positive Integration Vertical Slice
# =========================================================================

@pytest.mark.asyncio
async def test_production_consumer_bundle_assembler_positive_fixture() -> None:
    """Full positive vertical slice executing real FontTools, FreeType, HarfBuzz, and Chromium."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        config = ObservationConfig(resolutions=(128, 256), base_subpixel_phases=((0.0, 0.0),), expanded_subpixel_phases=((0.0, 0.0),))
        cfg_hash = config.compute_hash()

        r1_fit, b1_fit = _make_observation_record(code_point=65, resolution=128, advance_width_upem=650.0, config_hash=cfg_hash)
        r2_fit, b2_fit = _make_observation_record(code_point=65, resolution=256, advance_width_upem=650.0, config_hash=cfg_hash)
        r3_fit, b3_fit = _make_observation_record(code_point=66, resolution=128, advance_width_upem=600.0, config_hash=cfg_hash)
        r4_fit, b4_fit = _make_observation_record(code_point=66, resolution=256, advance_width_upem=600.0, config_hash=cfg_hash)

        fit_records = [r1_fit, r2_fit, r3_fit, r4_fit]

        r1_held, b1_held = _make_observation_record(code_point=65, resolution=256, subpixel_x=0.25, subpixel_y=0.25, advance_width_upem=651.0, config_hash=cfg_hash)
        r2_held, b2_held = _make_observation_record(code_point=66, resolution=256, subpixel_x=0.25, subpixel_y=0.25, advance_width_upem=599.0, config_hash=cfg_hash)

        held_out_records = [r1_held, r2_held]
        held_out_rasters = {r1_held.cache_key: b1_held, r2_held.cache_key: b2_held}

        calibrated_map = ObservationCalibrator.calibrate_all(fit_records, config=config, units_per_em=1000)
        calib_fp = ObservationCalibrator.compute_calibration_fingerprint(fit_records, config=config, units_per_em=1000)

        contour_A = _make_sample_contour()
        contour_B = _make_sample_contour(offset_x=10.0)

        glyph_A = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50, 50, 550, 700), [contour_A], observation_fingerprints=calibrated_map[65].observation_fingerprints)
        glyph_B = CalibratedGlyph(66, "B", 600.0, 40.0, 40.0, 750.0, -200.0, (40, 50, 560, 700), [contour_B], observation_fingerprints=calibrated_map[66].observation_fingerprints)

        fit_pair = PairKerningObservation(65, 66, "A", "B", 650.0, 600.0, 1230.0, -20, True, provenance="chromium:chromium:canvas_text_metrics")
        held_out_pair = PairKerningObservation(66, 65, "B", "A", 600.0, 650.0, 1240.0, -10, True, provenance="chromium:chromium:canvas_text_metrics")

        model = CanonicalFontModel(
            schema_version="1.0.0",
            family_name="E2EFont",
            style_name="Regular",
            reference_id="test_font",
            style_id="regular",
            metrics=GlobalFontMetrics(1000, 800.0, -200.0, 0.0, 700.0, 500.0, 1000.0, 500.0, -100.0, 50.0),
            glyphs={65: glyph_A, 66: glyph_B},
            kerning_pairs={(65, 66): -20, (66, 65): -10},
            config_hash=cfg_hash,
            browser_version="chromium",
            fit_observations_count=len(fit_records),
            calibration_fingerprint=calib_fp,
        )

        builder = MaxCandidateFontBuilder("E2EFont", "Regular", units_per_em=1000)
        reconstructed_glyphs = {
            65: ReconstructedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, contours=[contour_A], bounding_box_upem=(50, 50, 550, 700)),
            66: ReconstructedGlyph(66, "B", 600.0, 40.0, 40.0, 750.0, -200.0, contours=[contour_B], bounding_box_upem=(40, 50, 560, 700)),
        }
        typo_dataset = TypographyDataset("test_font", "regular", kerning_pairs={(65, 66): -20, (66, 65): -10})
        build_result = builder.build_candidate_family(reconstructed_glyphs, tmp_path, typography=typo_dataset)
        ttf_file = build_result.ttf.file_path

        # 1. Run ProductionConsumerEvidenceProducer with CandidateArtifactDescriptor
        desc = CandidateArtifactDescriptor(ttf_file, "TTF", build_result.ttf.size_bytes, build_result.ttf.sha256_hex)
        bundle = await ProductionConsumerEvidenceProducer.produce_bundle(
            descriptor=desc,
            model=model,
            config=config,
            held_out_records=held_out_records,
            held_out_pairs=[held_out_pair],
            raster_provider=lambda r: held_out_rasters[r.cache_key],
        )

        assert bundle.candidate_artifact_sha == build_result.ttf.sha256_hex
        assert bundle.fonttools.candidate_artifact_sha == build_result.ttf.sha256_hex
        assert bundle.freetype.candidate_artifact_sha == build_result.ttf.sha256_hex
        assert bundle.harfbuzz.candidate_artifact_sha == build_result.ttf.sha256_hex
        assert bundle.chromium.candidate_artifact_sha == build_result.ttf.sha256_hex

        assert bundle.fonttools.result.is_direct_loadable_fonttools is True
        assert bundle.freetype.result.min_raster_iou > 0.85
        assert len(bundle.freetype.result.samples) == 2
        assert bundle.harfbuzz.result.all_in_cmap is True
        assert bundle.harfbuzz.result.all_sequence_match is True
        assert len(bundle.harfbuzz.result.samples) == 1
        assert len(bundle.harfbuzz.result.samples[0].positions) == 2

        assert bundle.chromium.result.is_available is True
        assert bundle.chromium.result.is_direct_loadable_chromium is True
        assert bundle.chromium.result.rendered_canvas_valid is True
        assert len(bundle.chromium.result.glyph_samples) == 2
        assert len(bundle.chromium.result.pair_samples) == 1
        assert bundle.chromium.result.pair_samples[0].pair == "BA"

        # Evaluate with FidelityEvaluator
        report = FidelityEvaluator.evaluate(
            model=model,
            config=config,
            fit_records=fit_records,
            held_out_records=held_out_records,
            fit_pairs=[fit_pair],
            held_out_pairs=[held_out_pair],
            consumer_bundle=bundle,
            raster_provider=lambda r: held_out_rasters[r.cache_key],
        )
        assert report.overall_status == "PASS"
        assert report.consumer_gate.status == "PASS"


@pytest.mark.asyncio
async def test_production_bundle_positive_invariant_passes_shared_consumer_gate_immediately() -> None:
    """Positive Invariant: Every bundle returned by produce_bundle passes validate_consumer_gate and FidelityEvaluator immediately."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        config = ObservationConfig(resolutions=(128, 256), base_subpixel_phases=((0.0, 0.0),), expanded_subpixel_phases=((0.0, 0.0),))
        cfg_hash = config.compute_hash()

        r1_fit, b1_fit = _make_observation_record(code_point=65, resolution=128, advance_width_upem=650.0, config_hash=cfg_hash)
        r2_fit, b2_fit = _make_observation_record(code_point=65, resolution=256, advance_width_upem=650.0, config_hash=cfg_hash)
        r3_fit, b3_fit = _make_observation_record(code_point=66, resolution=128, advance_width_upem=600.0, config_hash=cfg_hash)
        r4_fit, b4_fit = _make_observation_record(code_point=66, resolution=256, advance_width_upem=600.0, config_hash=cfg_hash)
        fit_records = [r1_fit, r2_fit, r3_fit, r4_fit]

        r1_held, b1_held = _make_observation_record(code_point=65, resolution=256, subpixel_x=0.25, subpixel_y=0.25, advance_width_upem=651.0, config_hash=cfg_hash)
        r2_held, b2_held = _make_observation_record(code_point=66, resolution=256, subpixel_x=0.25, subpixel_y=0.25, advance_width_upem=599.0, config_hash=cfg_hash)
        held_out_records = [r1_held, r2_held]
        held_out_rasters = {r1_held.cache_key: b1_held, r2_held.cache_key: b2_held}

        calibrated_map = ObservationCalibrator.calibrate_all(fit_records, config=config, units_per_em=1000)
        calib_fp = ObservationCalibrator.compute_calibration_fingerprint(fit_records, config=config, units_per_em=1000)

        contour_A = _make_sample_contour()
        contour_B = _make_sample_contour(offset_x=10.0)

        glyph_A = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50, 50, 550, 700), [contour_A], observation_fingerprints=calibrated_map[65].observation_fingerprints)
        glyph_B = CalibratedGlyph(66, "B", 600.0, 40.0, 40.0, 750.0, -200.0, (40, 50, 560, 700), [contour_B], observation_fingerprints=calibrated_map[66].observation_fingerprints)

        fit_pair = PairKerningObservation(65, 66, "A", "B", 650.0, 600.0, 1230.0, -20, True, provenance="chromium:chromium:canvas_text_metrics")
        held_out_pair = PairKerningObservation(66, 65, "B", "A", 600.0, 650.0, 1240.0, -10, True, provenance="chromium:chromium:canvas_text_metrics")

        model = CanonicalFontModel(
            schema_version="1.0.0",
            family_name="E2EFont",
            style_name="Regular",
            reference_id="test_font",
            style_id="regular",
            metrics=GlobalFontMetrics(1000, 800.0, -200.0, 0.0, 700.0, 500.0, 1000.0, 500.0, -100.0, 50.0),
            glyphs={65: glyph_A, 66: glyph_B},
            kerning_pairs={(65, 66): -20, (66, 65): -10},
            config_hash=cfg_hash,
            browser_version="chromium",
            fit_observations_count=len(fit_records),
            calibration_fingerprint=calib_fp,
        )

        builder = MaxCandidateFontBuilder("E2EFont", "Regular", units_per_em=1000)
        reconstructed_glyphs = {
            65: ReconstructedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, contours=[contour_A], bounding_box_upem=(50, 50, 550, 700)),
            66: ReconstructedGlyph(66, "B", 600.0, 40.0, 40.0, 750.0, -200.0, contours=[contour_B], bounding_box_upem=(40, 50, 560, 700)),
        }
        typo_dataset = TypographyDataset("test_font", "regular", kerning_pairs={(65, 66): -20, (66, 65): -10})
        build_result = builder.build_candidate_family(reconstructed_glyphs, tmp_path, typography=typo_dataset)

        desc = CandidateArtifactDescriptor(build_result.ttf.file_path, "TTF", build_result.ttf.size_bytes, build_result.ttf.sha256_hex)
        thresholds = FidelityThresholds()

        bundle = await ProductionConsumerEvidenceProducer.produce_bundle(
            descriptor=desc,
            model=model,
            config=config,
            held_out_records=held_out_records,
            held_out_pairs=[held_out_pair],
            raster_provider=lambda r: held_out_rasters[r.cache_key],
            thresholds=thresholds,
        )

        # Invariant 1: Direct validate_consumer_gate call returns PASS
        gate_res, gate_errs = validate_consumer_gate(
            bundle=bundle,
            model=model,
            config=config,
            held_out_records=held_out_records,
            held_out_pairs=[held_out_pair],
            thresholds=thresholds,
        )
        assert gate_res.status == "PASS"
        assert not gate_errs

        # Invariant 2: Full FidelityEvaluator report returns PASS
        report = FidelityEvaluator.evaluate(
            model=model,
            config=config,
            fit_records=fit_records,
            held_out_records=held_out_records,
            fit_pairs=[fit_pair],
            held_out_pairs=[held_out_pair],
            consumer_bundle=bundle,
            raster_provider=lambda r: held_out_rasters[r.cache_key],
            thresholds=thresholds,
        )
        assert report.overall_status == "PASS"
        assert report.consumer_gate.status == "PASS"


# =========================================================================
# 6. Typography, Fingerprints & Edge Tests
# =========================================================================

def test_typography_fingerprint_rejects_character_codepoint_drift() -> None:
    bad_pair = PairKerningObservation(65, 66, "Z", "B", 650, 600, 1250, 0, False, provenance="chromium:chromium:canvas_text_metrics")
    with pytest.raises(ValueError, match="TYPOGRAPHY_CHAR_CODEPOINT_MISMATCH"):
        FidelityEvaluator._compute_typography_fingerprint([bad_pair])


def test_public_test_session_adapter_absence() -> None:
    """Verify TestChromiumEvidenceProducerAdapter is absent from fidelity public package exports."""
    assert not hasattr(fidelity, "TestChromiumEvidenceProducerAdapter")
    assert "TestChromiumEvidenceProducerAdapter" not in fidelity.__all__


def test_freetype_sample_level_drift_rejected_in_evaluator() -> None:
    art = CandidateArtifact.from_source(b"\x00\x01\x00\x00" + b"\x00" * 100, format_hint="TTF")
    cfg = ObservationConfig()
    cfg_hash = cfg.compute_hash()
    rec, png_bytes = _make_observation_record(code_point=65, config_hash=cfg_hash)
    glyph = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50.0, 50.0, 550.0, 700.0), [_make_sample_contour()], observation_fingerprints=("a" * 64,))
    model = CanonicalFontModel(
        family_name="F", style_name="R", reference_id="ref", style_id="reg",
        config_hash=cfg_hash, browser_version="chromium", fit_observations_count=1,
        calibration_fingerprint="b" * 64, glyphs={65: glyph},
    )
    pair = PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False, provenance="chromium:chromium:canvas_text_metrics")

    bad_sample = FreeTypeSampleEvidence("drifted_key", 65, "A", 256, rec.raster_sha256, 0.95, 0)
    fr_res = RasterComparisonResult(65, "A", 256, 0.95, 0, samples=(bad_sample,), min_raster_iou=0.95)

    pos_vec = HarfBuzzPositionVector(650.0, 0.0, 0.0, 0.0)
    hb_sample = HarfBuzzSampleEvidence(65, 65, "AA", True, True, (1, 1), (0, 1), (pos_vec, pos_vec), 1300.0, 1300.0, 0.0, 0.0)
    hb_res = ShapingTestResult("AA", "c", True, True, ["A", "A"], [], 2, 2, 1300, 1300, 0, 0, samples=(hb_sample,), all_in_cmap=True, all_sequence_match=True)

    cr_glyph_s = ChromiumGlyphSampleEvidence(65, "A", 650.0, 650.0, 0.0)
    cr_pair_s = ChromiumPairSampleEvidence(65, 65, "AA", 1300.0, 1300.0, 1300.0, 0.0, 0.0, True)
    cr_res = ChromiumValidationResult(True, "cr", True, True, 1, 0.0, [], glyph_samples=(cr_glyph_s,), pair_samples=(cr_pair_s,), fit_pairs_material_improvement=True, held_out_pairs_non_regression=True, rendered_canvas_valid=True)
    ft_res = FormatValidationResult("TTF", "f.ttf", 100, art.sha256_hex, True, False, False, False, False, 1, 1000, True, True, True)

    bundle = ConsumerEvidenceBundle(
        schema_version="1.0.0",
        model_canonical_hash=model.compute_canonical_hash(),
        config_hash=cfg_hash,
        held_out_fingerprint=FidelityEvaluator._compute_composite_held_out_fingerprint([rec], [pair]),
        held_out_raster_fingerprint=FidelityEvaluator._compute_records_fingerprint([rec]),
        held_out_typography_fingerprint=FidelityEvaluator._compute_typography_fingerprint([pair]),
        candidate_artifact_sha=art.sha256_hex,
        fonttools=BoundFontToolsEvidence(candidate_artifact_sha=art.sha256_hex, result=ft_res),
        freetype=BoundFreeTypeEvidence(candidate_artifact_sha=art.sha256_hex, result=fr_res),
        harfbuzz=BoundHarfBuzzEvidence(candidate_artifact_sha=art.sha256_hex, result=hb_res),
        chromium=BoundChromiumEvidence(candidate_artifact_sha=art.sha256_hex, result=cr_res),
    )

    report = FidelityEvaluator.evaluate(
        model=model,
        config=cfg,
        fit_records=[rec],
        held_out_records=[rec],
        held_out_pairs=[pair],
        consumer_bundle=bundle,
        raster_provider=lambda r: png_bytes,
    )
    assert report.consumer_gate.status == "FAIL"
    assert report.consumer_gate.freetype_passed is False


def test_harfbuzz_sample_level_drift_rejected_in_evaluator() -> None:
    art = CandidateArtifact.from_source(b"\x00\x01\x00\x00" + b"\x00" * 100, format_hint="TTF")
    cfg = ObservationConfig()
    cfg_hash = cfg.compute_hash()
    rec, png_bytes = _make_observation_record(code_point=65, config_hash=cfg_hash)
    glyph = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50.0, 50.0, 550.0, 700.0), [_make_sample_contour()], observation_fingerprints=("a" * 64,))
    model = CanonicalFontModel(
        family_name="F", style_name="R", reference_id="ref", style_id="reg",
        config_hash=cfg_hash, browser_version="chromium", fit_observations_count=1,
        calibration_fingerprint="b" * 64, glyphs={65: glyph},
    )
    pair = PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False, provenance="chromium:chromium:canvas_text_metrics")

    fr_sample = FreeTypeSampleEvidence(rec.cache_key, 65, "A", 256, rec.raster_sha256, 0.95, 0)
    fr_res = RasterComparisonResult(65, "A", 256, 0.95, 0, samples=(fr_sample,), min_raster_iou=0.95)

    pos_vec = HarfBuzzPositionVector(650.0, 0.0, 0.0, 0.0)
    hb_sample = HarfBuzzSampleEvidence(65, 65, "AA", True, False, (1, 1), (0, 1), (pos_vec, pos_vec), 1300.0, 1300.0, 0.0, 0.0)
    hb_res = ShapingTestResult("AA", "c", True, False, ["A", "A"], [], 2, 2, 1300, 1300, 0, 0, samples=(hb_sample,), all_in_cmap=True, all_sequence_match=False)

    cr_glyph_s = ChromiumGlyphSampleEvidence(65, "A", 650.0, 650.0, 0.0)
    cr_pair_s = ChromiumPairSampleEvidence(65, 65, "AA", 1300.0, 1300.0, 1300.0, 0.0, 0.0, True)
    cr_res = ChromiumValidationResult(True, "cr", True, True, 1, 0.0, [], glyph_samples=(cr_glyph_s,), pair_samples=(cr_pair_s,), fit_pairs_material_improvement=True, held_out_pairs_non_regression=True, rendered_canvas_valid=True)
    ft_res = FormatValidationResult("TTF", "f.ttf", 100, art.sha256_hex, True, False, False, False, False, 1, 1000, True, True, True)

    bundle = ConsumerEvidenceBundle(
        schema_version="1.0.0",
        model_canonical_hash=model.compute_canonical_hash(),
        config_hash=cfg_hash,
        held_out_fingerprint=FidelityEvaluator._compute_composite_held_out_fingerprint([rec], [pair]),
        held_out_raster_fingerprint=FidelityEvaluator._compute_records_fingerprint([rec]),
        held_out_typography_fingerprint=FidelityEvaluator._compute_typography_fingerprint([pair]),
        candidate_artifact_sha=art.sha256_hex,
        fonttools=BoundFontToolsEvidence(candidate_artifact_sha=art.sha256_hex, result=ft_res),
        freetype=BoundFreeTypeEvidence(candidate_artifact_sha=art.sha256_hex, result=fr_res),
        harfbuzz=BoundHarfBuzzEvidence(candidate_artifact_sha=art.sha256_hex, result=hb_res),
        chromium=BoundChromiumEvidence(candidate_artifact_sha=art.sha256_hex, result=cr_res),
    )

    report = FidelityEvaluator.evaluate(
        model=model,
        config=cfg,
        fit_records=[rec],
        held_out_records=[rec],
        held_out_pairs=[pair],
        consumer_bundle=bundle,
        raster_provider=lambda r: png_bytes,
    )
    assert report.consumer_gate.status == "FAIL"
    assert report.consumer_gate.harfbuzz_passed is False


def test_chromium_sample_level_drift_rejected_in_evaluator() -> None:
    art = CandidateArtifact.from_source(b"\x00\x01\x00\x00" + b"\x00" * 100, format_hint="TTF")
    cfg = ObservationConfig()
    cfg_hash = cfg.compute_hash()
    rec, png_bytes = _make_observation_record(code_point=65, config_hash=cfg_hash)
    glyph = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50.0, 50.0, 550.0, 700.0), [_make_sample_contour()], observation_fingerprints=("a" * 64,))
    model = CanonicalFontModel(
        family_name="F", style_name="R", reference_id="ref", style_id="reg",
        config_hash=cfg_hash, browser_version="chromium", fit_observations_count=1,
        calibration_fingerprint="b" * 64, glyphs={65: glyph},
    )
    pair = PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False, provenance="chromium:chromium:canvas_text_metrics")

    fr_sample = FreeTypeSampleEvidence(rec.cache_key, 65, "A", 256, rec.raster_sha256, 0.95, 0)
    fr_res = RasterComparisonResult(65, "A", 256, 0.95, 0, samples=(fr_sample,), min_raster_iou=0.95)

    pos_vec = HarfBuzzPositionVector(650.0, 0.0, 0.0, 0.0)
    hb_sample = HarfBuzzSampleEvidence(65, 65, "AA", True, True, (1, 1), (0, 1), (pos_vec, pos_vec), 1300.0, 1300.0, 0.0, 0.0)
    hb_res = ShapingTestResult("AA", "c", True, True, ["A", "A"], [], 2, 2, 1300, 1300, 0, 0, samples=(hb_sample,), all_in_cmap=True, all_sequence_match=True)

    cr_glyph_s = ChromiumGlyphSampleEvidence(65, "A", 650.0, 650.0, 0.0)
    cr_bad_s = ChromiumPairSampleEvidence(65, 65, "AA", 1300.0, 1300.0, 1300.0, 0.0, 0.0, False)
    cr_res = ChromiumValidationResult(True, "cr", True, True, 1, 0.0, [], glyph_samples=(cr_glyph_s,), pair_samples=(cr_bad_s,), fit_pairs_material_improvement=False, held_out_pairs_non_regression=False, rendered_canvas_valid=True)
    ft_res = FormatValidationResult("TTF", "f.ttf", 100, art.sha256_hex, True, False, False, False, False, 1, 1000, True, True, True)

    bundle = ConsumerEvidenceBundle(
        schema_version="1.0.0",
        model_canonical_hash=model.compute_canonical_hash(),
        config_hash=cfg_hash,
        held_out_fingerprint=FidelityEvaluator._compute_composite_held_out_fingerprint([rec], [pair]),
        held_out_raster_fingerprint=FidelityEvaluator._compute_records_fingerprint([rec]),
        held_out_typography_fingerprint=FidelityEvaluator._compute_typography_fingerprint([pair]),
        candidate_artifact_sha=art.sha256_hex,
        fonttools=BoundFontToolsEvidence(candidate_artifact_sha=art.sha256_hex, result=ft_res),
        freetype=BoundFreeTypeEvidence(candidate_artifact_sha=art.sha256_hex, result=fr_res),
        harfbuzz=BoundHarfBuzzEvidence(candidate_artifact_sha=art.sha256_hex, result=hb_res),
        chromium=BoundChromiumEvidence(candidate_artifact_sha=art.sha256_hex, result=cr_res),
    )

    report = FidelityEvaluator.evaluate(
        model=model,
        config=cfg,
        fit_records=[rec],
        held_out_records=[rec],
        held_out_pairs=[pair],
        consumer_bundle=bundle,
        raster_provider=lambda r: png_bytes,
    )
    assert report.consumer_gate.status == "FAIL"
    assert report.consumer_gate.chromium_passed is False


def test_zero_held_out_samples_fails_closed_in_producer() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        f_file = tmp_path / "f.ttf"
        font_bytes = b"\x00\x01\x00\x00" + b"\x00" * 100
        f_file.write_bytes(font_bytes)
        desc = CandidateArtifactDescriptor(f_file, "TTF", len(font_bytes), hashlib.sha256(font_bytes).hexdigest(), font_bytes)

        model = CanonicalFontModel(
            family_name="F", style_name="R", reference_id="ref", style_id="reg",
            config_hash="a" * 64, browser_version="chromium", fit_observations_count=1,
            calibration_fingerprint="b" * 64,
            glyphs={65: CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50.0, 50.0, 550.0, 700.0), [_make_sample_contour()], observation_fingerprints=("a" * 64,))},
        )
        rec, _ = _make_observation_record(code_point=65)
        pair = PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False, provenance="chromium:chromium:canvas_text_metrics")

        with pytest.raises(ValueError, match="ZERO_HELD_OUT_RASTER_SAMPLES"):
            asyncio.run(ProductionConsumerEvidenceProducer.produce_bundle(desc, model, ObservationConfig(), [], [pair], lambda r: b""))

        with pytest.raises(ValueError, match="ZERO_HELD_OUT_TYPOGRAPHY_SAMPLES"):
            asyncio.run(ProductionConsumerEvidenceProducer.produce_bundle(desc, model, ObservationConfig(), [rec], [], lambda r: b""))


def test_unknown_held_out_code_point_fails_closed_in_producer() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        f_file = tmp_path / "f.ttf"
        font_bytes = b"\x00\x01\x00\x00" + b"\x00" * 100
        f_file.write_bytes(font_bytes)
        desc = CandidateArtifactDescriptor(f_file, "TTF", len(font_bytes), hashlib.sha256(font_bytes).hexdigest(), font_bytes)

        model = CanonicalFontModel(
            family_name="F", style_name="R", reference_id="ref", style_id="reg",
            config_hash="a" * 64, browser_version="chromium", fit_observations_count=1,
            calibration_fingerprint="b" * 64,
            glyphs={65: CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50.0, 50.0, 550.0, 700.0), [_make_sample_contour()], observation_fingerprints=("a" * 64,))},
        )
        rec_unknown, _ = _make_observation_record(code_point=999)
        pair = PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False, provenance="chromium:chromium:canvas_text_metrics")

        with pytest.raises(ValueError, match="UNKNOWN_HELD_OUT_CODE_POINT"):
            asyncio.run(ProductionConsumerEvidenceProducer.produce_bundle(desc, model, ObservationConfig(), [rec_unknown], [pair], lambda r: b""))


@pytest.mark.asyncio
async def test_test_chromium_custom_session_internal() -> None:
    has_chromium = False
    try:
        find_chromium_executable()
        has_chromium = True
    except Exception:
        has_chromium = False

    if not has_chromium:
        pytest.skip("No Chromium executable on host")

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        builder = MaxCandidateFontBuilder("TestAdapter", "Regular", units_per_em=1000)
        reconstructed = {
            65: ReconstructedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, contours=[_make_sample_contour()], bounding_box_upem=(50, 50, 550, 700)),
        }
        build_result = builder.build_candidate_family(reconstructed, tmp_path)
        art = CandidateArtifact.from_source(build_result.ttf.file_path)

        glyph = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50, 50, 550, 700), [_make_sample_contour()], observation_fingerprints=("a" * 64,))
        model = CanonicalFontModel(
            family_name="TestAdapter", style_name="Regular", reference_id="test_font", style_id="regular",
            config_hash="a" * 64, browser_version="chromium", fit_observations_count=1,
            calibration_fingerprint="b" * 64, glyphs={65: glyph},
        )
        rec, _ = _make_observation_record(code_point=65)

        session = ChromiumSession(timeout_seconds=10.0)
        try:
            evidence = await ChromiumEvidenceProducer._produce_with_session_internal(
                artifact=art,
                model=model,
                held_out_records=[rec],
                held_out_pairs=None,
                custom_session=session,
            )
            assert evidence.result.is_direct_loadable_chromium is True
        finally:
            await session.aclose()
