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
    TestChromiumEvidenceProducerAdapter,
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
# 1. Architect Reproduction 1: Zero-Sample Aggregate Bundle Fails Gate
# =========================================================================

def test_architect_reproduction_1_zero_sample_bundle_fails_consumer_gate() -> None:
    """Reproduction 1: Bundle with empty sample collections must FAIL closed even if aggregate fields claim PASS."""
    art = CandidateArtifact.from_source(b"\x00\x01\x00\x00" + b"\x00" * 100, format_hint="TTF")
    rec, png_bytes = _make_observation_record(code_point=65)
    glyph = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50.0, 50.0, 550.0, 700.0), [_make_sample_contour()], observation_fingerprints=("a" * 64,))
    model = CanonicalFontModel(
        family_name="F", style_name="R", reference_id="ref", style_id="reg",
        config_hash="a" * 64, browser_version="chromium", fit_observations_count=1,
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
        config_hash="a" * 64,
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
        config=ObservationConfig(),
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
# 2. Architect Reproduction 2: Descriptor Path vs Bytes Drift Rejected
# =========================================================================

def test_architect_reproduction_2_descriptor_path_bytes_drift_rejected() -> None:
    """Reproduction 2: Descriptor with mismatched path on disk vs raw_bytes must be rejected."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        file_b = tmp_path / "font_b.ttf"
        bytes_b = b"\x00\x01\x00\x00" + b"\xBB" * 100
        file_b.write_bytes(bytes_b)

        bytes_a = b"\x00\x01\x00\x00" + b"\xAA" * 100
        size_a = len(bytes_a)
        sha_a = hashlib.sha256(bytes_a).hexdigest()

        # Attesting size and SHA of A, but pointing file_path to B with disk bytes B != A
        desc = CandidateArtifactDescriptor(file_path=file_b, expected_format="TTF", expected_size_bytes=size_a, expected_sha256_hex=sha_a, raw_bytes=bytes_a)

        with pytest.raises(ValueError, match="ARTIFACT_PATH_BYTES_DRIFT"):
            CandidateArtifact.from_descriptor(desc)


# =========================================================================
# 3. Production Outcome Fail-Closed & Source Verification
# =========================================================================

def test_production_producer_requires_attested_descriptor() -> None:
    rec, _ = _make_observation_record(code_point=65)
    pair = PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False, provenance="chromium:chromium:canvas_text_metrics")
    model = CanonicalFontModel(
        family_name="F", style_name="R", reference_id="ref", style_id="reg",
        config_hash="a" * 64, browser_version="chromium", fit_observations_count=1,
        calibration_fingerprint="b" * 64,
        glyphs={65: CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50.0, 50.0, 550.0, 700.0), [_make_sample_contour()], observation_fingerprints=("a" * 64,))},
    )

    with pytest.raises(TypeError, match="ProductionConsumerEvidenceProducer requires a CandidateArtifactDescriptor"):
        asyncio.run(
            ProductionConsumerEvidenceProducer.produce_bundle(
                descriptor="raw_unattested_path.ttf",  # type: ignore
                model=model,
                config=ObservationConfig(),
                held_out_records=[rec],
                held_out_pairs=[pair],
                raster_provider=lambda r: b"",
            )
        )


def test_production_producer_corrupt_font_raises_production_error() -> None:
    corrupt_bytes = b"\x00\x01\x00\x00" + b"\x00" * 100
    desc = CandidateArtifactDescriptor(
        file_path="corrupt.ttf",
        expected_format="TTF",
        expected_size_bytes=len(corrupt_bytes),
        expected_sha256_hex=hashlib.sha256(corrupt_bytes).hexdigest(),
        raw_bytes=corrupt_bytes,
    )
    rec, png_bytes = _make_observation_record(code_point=65)
    pair = PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False, provenance="chromium:chromium:canvas_text_metrics")
    model = CanonicalFontModel(
        family_name="F", style_name="R", reference_id="ref", style_id="reg",
        config_hash="a" * 64, browser_version="chromium", fit_observations_count=1,
        calibration_fingerprint="b" * 64,
        glyphs={65: CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50.0, 50.0, 550.0, 700.0), [_make_sample_contour()], observation_fingerprints=("a" * 64,))},
    )

    # Corrupt font must raise ProductionProducerError (no usable PASS bundle)
    with pytest.raises(ProductionProducerError, match="CONSUMER_PRODUCER_FAILED"):
        asyncio.run(
            ProductionConsumerEvidenceProducer.produce_bundle(
                descriptor=desc,
                model=model,
                config=ObservationConfig(),
                held_out_records=[rec],
                held_out_pairs=[pair],
                raster_provider=lambda r: png_bytes,
            )
        )


# =========================================================================
# 4. Typography / Character-Codepoint Drift & Exact Sample Coverage
# =========================================================================

def test_typography_fingerprint_rejects_character_codepoint_drift() -> None:
    bad_pair = PairKerningObservation(65, 66, "Z", "B", 650, 600, 1250, 0, False, provenance="chromium:chromium:canvas_text_metrics")
    with pytest.raises(ValueError, match="TYPOGRAPHY_CHAR_CODEPOINT_MISMATCH"):
        FidelityEvaluator._compute_typography_fingerprint([bad_pair])


def test_freetype_sample_level_drift_rejected_in_evaluator() -> None:
    art = CandidateArtifact.from_source(b"\x00\x01\x00\x00" + b"\x00" * 100, format_hint="TTF")
    rec, png_bytes = _make_observation_record(code_point=65)
    glyph = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50.0, 50.0, 550.0, 700.0), [_make_sample_contour()], observation_fingerprints=("a" * 64,))
    model = CanonicalFontModel(
        family_name="F", style_name="R", reference_id="ref", style_id="reg",
        config_hash="a" * 64, browser_version="chromium", fit_observations_count=1,
        calibration_fingerprint="b" * 64, glyphs={65: glyph},
    )
    pair = PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False, provenance="chromium:chromium:canvas_text_metrics")

    # Sample with drifted cache_key
    bad_sample = FreeTypeSampleEvidence("drifted_key", 65, "A", 256, rec.raster_sha256, 0.95, 0)
    fr_res = RasterComparisonResult(65, "A", 256, 0.95, 0, samples=(bad_sample,), min_raster_iou=0.95)

    pos_vec = HarfBuzzPositionVector(650.0, 0.0, 0.0, 0.0)
    hb_sample = HarfBuzzSampleEvidence(65, 65, "AA", True, True, (1, 1), (0, 1), (pos_vec, pos_vec), 1300.0, 1300.0, 0.0, 0.0)
    hb_res = ShapingTestResult("AA", "c", True, True, ["A", "A"], [], 2, 2, 1300, 1300, 0, 0, samples=(hb_sample,), all_in_cmap=True, all_sequence_match=True)

    cr_pair_s = ChromiumPairSampleEvidence(65, 65, "AA", 1300.0, 1300.0, 1300.0, 0.0, 0.0, True)
    cr_res = ChromiumValidationResult(True, "cr", True, True, 1, 0.0, [], glyph_samples=(), pair_samples=(cr_pair_s,), fit_pairs_material_improvement=True, held_out_pairs_non_regression=True, rendered_canvas_valid=True)
    ft_res = FormatValidationResult("TTF", "f.ttf", 100, art.sha256_hex, True, False, False, False, False, 1, 1000, True, True, True)

    bundle = ConsumerEvidenceBundle(
        schema_version="1.0.0",
        model_canonical_hash=model.compute_canonical_hash(),
        config_hash="a" * 64,
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
        config=ObservationConfig(),
        fit_records=[rec],
        held_out_records=[rec],
        held_out_pairs=[pair],
        consumer_bundle=bundle,
        raster_provider=lambda r: png_bytes,
    )
    assert report.consumer_gate.status == "FAIL"
    assert report.consumer_gate.freetype_passed is False


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


# =========================================================================
# 6. Sample Drift, Adapter & Boundary Tests
# =========================================================================

def test_harfbuzz_sample_level_drift_rejected_in_evaluator() -> None:
    art = CandidateArtifact.from_source(b"\x00\x01\x00\x00" + b"\x00" * 100, format_hint="TTF")
    rec, png_bytes = _make_observation_record(code_point=65)
    glyph = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50.0, 50.0, 550.0, 700.0), [_make_sample_contour()], observation_fingerprints=("a" * 64,))
    model = CanonicalFontModel(
        family_name="F", style_name="R", reference_id="ref", style_id="reg",
        config_hash="a" * 64, browser_version="chromium", fit_observations_count=1,
        calibration_fingerprint="b" * 64, glyphs={65: glyph},
    )
    pair = PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False, provenance="chromium:chromium:canvas_text_metrics")

    fr_sample = FreeTypeSampleEvidence(rec.cache_key, 65, "A", 256, rec.raster_sha256, 0.95, 0)
    fr_res = RasterComparisonResult(65, "A", 256, 0.95, 0, samples=(fr_sample,), min_raster_iou=0.95)

    # HarfBuzz sample with drifted text "AB" instead of "AA"
    pos_vec = HarfBuzzPositionVector(650.0, 0.0, 0.0, 0.0)
    bad_hb_sample = HarfBuzzSampleEvidence(65, 65, "AB", True, True, (1, 1), (0, 1), (pos_vec, pos_vec), 1300.0, 1300.0, 0.0, 0.0)
    hb_res = ShapingTestResult("AA", "c", True, True, ["A", "A"], [], 2, 2, 1300, 1300, 0, 0, samples=(bad_hb_sample,), all_in_cmap=True, all_sequence_match=True)

    cr_pair_s = ChromiumPairSampleEvidence(65, 65, "AA", 1300.0, 1300.0, 1300.0, 0.0, 0.0, True)
    cr_res = ChromiumValidationResult(True, "cr", True, True, 1, 0.0, [], glyph_samples=(), pair_samples=(cr_pair_s,), fit_pairs_material_improvement=True, held_out_pairs_non_regression=True, rendered_canvas_valid=True)
    ft_res = FormatValidationResult("TTF", "f.ttf", 100, art.sha256_hex, True, False, False, False, False, 1, 1000, True, True, True)

    bundle = ConsumerEvidenceBundle(
        schema_version="1.0.0",
        model_canonical_hash=model.compute_canonical_hash(),
        config_hash="a" * 64,
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
        config=ObservationConfig(),
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
    rec, png_bytes = _make_observation_record(code_point=65)
    glyph = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50.0, 50.0, 550.0, 700.0), [_make_sample_contour()], observation_fingerprints=("a" * 64,))
    model = CanonicalFontModel(
        family_name="F", style_name="R", reference_id="ref", style_id="reg",
        config_hash="a" * 64, browser_version="chromium", fit_observations_count=1,
        calibration_fingerprint="b" * 64, glyphs={65: glyph},
    )
    pair = PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False, provenance="chromium:chromium:canvas_text_metrics")

    fr_sample = FreeTypeSampleEvidence(rec.cache_key, 65, "A", 256, rec.raster_sha256, 0.95, 0)
    fr_res = RasterComparisonResult(65, "A", 256, 0.95, 0, samples=(fr_sample,), min_raster_iou=0.95)

    pos_vec = HarfBuzzPositionVector(650.0, 0.0, 0.0, 0.0)
    hb_sample = HarfBuzzSampleEvidence(65, 65, "AA", True, True, (1, 1), (0, 1), (pos_vec, pos_vec), 1300.0, 1300.0, 0.0, 0.0)
    hb_res = ShapingTestResult("AA", "c", True, True, ["A", "A"], [], 2, 2, 1300, 1300, 0, 0, samples=(hb_sample,), all_in_cmap=True, all_sequence_match=True)

    # Chromium pair sample with non_regression=False
    cr_bad_s = ChromiumPairSampleEvidence(65, 65, "AA", 1300.0, 1300.0, 1300.0, 0.0, 0.0, False)
    cr_res = ChromiumValidationResult(True, "cr", True, True, 1, 0.0, [], glyph_samples=(), pair_samples=(cr_bad_s,), fit_pairs_material_improvement=False, held_out_pairs_non_regression=False, rendered_canvas_valid=True)
    ft_res = FormatValidationResult("TTF", "f.ttf", 100, art.sha256_hex, True, False, False, False, False, 1, 1000, True, True, True)

    bundle = ConsumerEvidenceBundle(
        schema_version="1.0.0",
        model_canonical_hash=model.compute_canonical_hash(),
        config_hash="a" * 64,
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
        config=ObservationConfig(),
        fit_records=[rec],
        held_out_records=[rec],
        held_out_pairs=[pair],
        consumer_bundle=bundle,
        raster_provider=lambda r: png_bytes,
    )
    assert report.consumer_gate.status == "FAIL"
    assert report.consumer_gate.chromium_passed is False


def test_zero_held_out_samples_fails_closed_in_producer() -> None:
    art = CandidateArtifact.from_source(b"\x00\x01\x00\x00" + b"\x00" * 100, format_hint="TTF")
    model = CanonicalFontModel(
        family_name="F", style_name="R", reference_id="ref", style_id="reg",
        config_hash="a" * 64, browser_version="chromium", fit_observations_count=1,
        calibration_fingerprint="b" * 64,
        glyphs={65: CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50.0, 50.0, 550.0, 700.0), [_make_sample_contour()], observation_fingerprints=("a" * 64,))},
    )
    desc = CandidateArtifactDescriptor("f.ttf", "TTF", 104, art.sha256_hex, art.raw_bytes)
    rec, _ = _make_observation_record(code_point=65)
    pair = PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False, provenance="chromium:chromium:canvas_text_metrics")

    with pytest.raises(ValueError, match="ZERO_HELD_OUT_RASTER_SAMPLES"):
        asyncio.run(ProductionConsumerEvidenceProducer.produce_bundle(desc, model, ObservationConfig(), [], [pair], lambda r: b""))

    with pytest.raises(ValueError, match="ZERO_HELD_OUT_TYPOGRAPHY_SAMPLES"):
        asyncio.run(ProductionConsumerEvidenceProducer.produce_bundle(desc, model, ObservationConfig(), [rec], [], lambda r: b""))


def test_unknown_held_out_code_point_fails_closed_in_producer() -> None:
    art = CandidateArtifact.from_source(b"\x00\x01\x00\x00" + b"\x00" * 100, format_hint="TTF")
    model = CanonicalFontModel(
        family_name="F", style_name="R", reference_id="ref", style_id="reg",
        config_hash="a" * 64, browser_version="chromium", fit_observations_count=1,
        calibration_fingerprint="b" * 64,
        glyphs={65: CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50.0, 50.0, 550.0, 700.0), [_make_sample_contour()], observation_fingerprints=("a" * 64,))},
    )
    desc = CandidateArtifactDescriptor("f.ttf", "TTF", 104, art.sha256_hex, art.raw_bytes)
    rec_unknown, _ = _make_observation_record(code_point=999)
    pair = PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False, provenance="chromium:chromium:canvas_text_metrics")

    with pytest.raises(ValueError, match="UNKNOWN_HELD_OUT_CODE_POINT"):
        asyncio.run(ProductionConsumerEvidenceProducer.produce_bundle(desc, model, ObservationConfig(), [rec_unknown], [pair], lambda r: b""))


@pytest.mark.asyncio
async def test_test_chromium_adapter_custom_session() -> None:
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
            evidence = await TestChromiumEvidenceProducerAdapter.produce_with_session(
                art, model, [rec], None, session
            )
            assert evidence.result.is_direct_loadable_chromium is True
        finally:
            session.close()
