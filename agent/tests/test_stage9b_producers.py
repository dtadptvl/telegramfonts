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
    HarfBuzzSampleEvidence,
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
# 1. Candidate Artifact & Descriptor Anti-Drift Validation
# =========================================================================

def test_candidate_artifact_descriptor_anti_drift() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        builder = MaxCandidateFontBuilder("TestFont", "Regular", units_per_em=1000)
        reconstructed = {
            65: ReconstructedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, contours=[_make_sample_contour()], bounding_box_upem=(50, 50, 550, 700)),
        }
        build_result = builder.build_candidate_family(reconstructed, tmp_path)
        ttf_file = build_result.ttf.file_path
        ttf_size = build_result.ttf.size_bytes
        ttf_sha = build_result.ttf.sha256_hex

        # 1. Valid descriptor
        desc = CandidateArtifactDescriptor(ttf_file, "TTF", ttf_size, ttf_sha)
        art = CandidateArtifact.from_descriptor(desc)
        assert art.format == "TTF"
        assert art.sha256_hex == ttf_sha

        # 2. Size drift rejection
        desc_bad_size = CandidateArtifactDescriptor(ttf_file, "TTF", ttf_size + 10, ttf_sha)
        with pytest.raises(ValueError, match="ARTIFACT_SIZE_DRIFT"):
            CandidateArtifact.from_descriptor(desc_bad_size)

        # 3. SHA drift rejection
        desc_bad_sha = CandidateArtifactDescriptor(ttf_file, "TTF", ttf_size, "0" * 64)
        with pytest.raises(ValueError, match="ARTIFACT_SHA_DRIFT"):
            CandidateArtifact.from_descriptor(desc_bad_sha)

        # 4. Format drift rejection
        desc_bad_fmt = CandidateArtifactDescriptor(ttf_file, "OTF", ttf_size, ttf_sha)
        with pytest.raises(ValueError, match="ARTIFACT_FORMAT_DRIFT"):
            CandidateArtifact.from_descriptor(desc_bad_fmt)


# =========================================================================
# 2. Architect Reproduction: Missing / Drifted Held-Out Pairs Rejected
# =========================================================================

def test_architect_reproduction_held_out_pair_binding_and_rejection() -> None:
    """Architect Reproduction: empty or omitted held-out pairs must FAIL before evaluation or in FidelityEvaluator."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        builder = MaxCandidateFontBuilder("TestFont", "Regular", units_per_em=1000)
        reconstructed = {
            65: ReconstructedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, contours=[_make_sample_contour()], bounding_box_upem=(50, 50, 550, 700)),
            66: ReconstructedGlyph(66, "B", 600.0, 40.0, 40.0, 750.0, -200.0, contours=[_make_sample_contour(10)], bounding_box_upem=(40, 50, 560, 700)),
        }
        typo = TypographyDataset("test_font", "regular", kerning_pairs={(65, 66): -20})
        build_result = builder.build_candidate_family(reconstructed, tmp_path, typography=typo)
        art = CandidateArtifact.from_source(build_result.ttf.file_path)

        rec, png_bytes = _make_observation_record(code_point=65, resolution=256)
        glyph_A = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50, 50, 550, 700), [_make_sample_contour()], observation_fingerprints=("a" * 64,))
        glyph_B = CalibratedGlyph(66, "B", 600.0, 40.0, 40.0, 750.0, -200.0, (40, 50, 560, 700), [_make_sample_contour(10)], observation_fingerprints=("b" * 64,))
        model = CanonicalFontModel(
            family_name="TestFont", style_name="Regular", reference_id="test_font", style_id="regular",
            config_hash="a" * 64, browser_version="chromium", fit_observations_count=2,
            calibration_fingerprint="b" * 64, glyphs={65: glyph_A, 66: glyph_B},
            kerning_pairs={(65, 66): -20},
        )
        config = ObservationConfig()

        # 1. Calling produce_bundle with empty held_out_pairs raises ValueError
        with pytest.raises(ValueError, match="ZERO_HELD_OUT_TYPOGRAPHY_SAMPLES"):
            asyncio.run(
                ProductionConsumerEvidenceProducer.produce_bundle(
                    candidate_source=art,
                    model=model,
                    config=config,
                    held_out_records=[rec],
                    held_out_pairs=[],
                    raster_provider=lambda r: png_bytes,
                )
            )


# =========================================================================
# 3. Sample-Level FreeType & HarfBuzz Truth Tests
# =========================================================================

def test_freetype_producer_detects_single_bad_sample_among_good() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        builder = MaxCandidateFontBuilder("TestFont", "Regular", units_per_em=1000)
        reconstructed = {
            65: ReconstructedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, contours=[_make_sample_contour()], bounding_box_upem=(50, 50, 550, 700)),
        }
        build_result = builder.build_candidate_family(reconstructed, tmp_path)
        art = CandidateArtifact.from_source(build_result.ttf.file_path)

        rec1, png1 = _make_observation_record(code_point=65, resolution=128)
        rec2, png2 = _make_observation_record(code_point=65, resolution=256)

        glyph = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50, 50, 550, 700), [_make_sample_contour()], observation_fingerprints=("a" * 64,))
        model = CanonicalFontModel(
            family_name="TestFont", style_name="Regular", reference_id="test_font", style_id="regular",
            config_hash="a" * 64, browser_version="chromium", fit_observations_count=1,
            calibration_fingerprint="b" * 64, glyphs={65: glyph},
        )

        # Provider provides corrupt bytes for rec2
        evidence = FreeTypeEvidenceProducer.produce(
            art, model, [rec1, rec2], lambda r: png1 if r.cache_key == rec1.cache_key else b"CORRUPT_BYTES"
        )
        assert evidence.result.render_error is not None
        assert len(evidence.result.samples) == 2
        assert evidence.result.samples[0].render_error is None
        assert evidence.result.samples[1].render_error is not None


def test_harfbuzz_producer_excessive_advance_delta_fails() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        builder = MaxCandidateFontBuilder("TestFont", "Regular", units_per_em=1000)
        reconstructed = {
            65: ReconstructedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, contours=[_make_sample_contour()], bounding_box_upem=(50, 50, 550, 700)),
            66: ReconstructedGlyph(66, "B", 600.0, 40.0, 40.0, 750.0, -200.0, contours=[_make_sample_contour(10)], bounding_box_upem=(40, 50, 560, 700)),
        }
        build_result = builder.build_candidate_family(reconstructed, tmp_path)
        art = CandidateArtifact.from_source(build_result.ttf.file_path)

        glyph_A = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50, 50, 550, 700), [_make_sample_contour()], observation_fingerprints=("a" * 64,))
        glyph_B = CalibratedGlyph(66, "B", 600.0, 40.0, 40.0, 750.0, -200.0, (40, 50, 560, 700), [_make_sample_contour(10)], observation_fingerprints=("b" * 64,))
        model = CanonicalFontModel(
            family_name="TestFont", style_name="Regular", reference_id="test_font", style_id="regular",
            config_hash="a" * 64, browser_version="chromium", fit_observations_count=2,
            calibration_fingerprint="b" * 64, glyphs={65: glyph_A, 66: glyph_B},
        )

        # Declared pair expects 2000 UPEM (actual candidate has 1250) -> huge delta
        bad_pair = PairKerningObservation(65, 66, "A", "B", 650.0, 600.0, 2000.0, 750, True, provenance="chromium:chromium:canvas_text_metrics")
        evidence = HarfBuzzEvidenceProducer.produce(art, model, [bad_pair])

        assert evidence.result.all_sequence_match is False
        assert evidence.result.error_message is not None
        assert evidence.result.samples[0].advance_delta_upem == 750.0


# =========================================================================
# 4. Chromium Special Characters & Zero-Pair Guard
# =========================================================================

def test_chromium_producer_zero_pair_does_not_claim_non_regression() -> None:
    art = CandidateArtifact.from_source(b"\x00\x01\x00\x00" + b"\x00" * 100, format_hint="TTF")
    model = CanonicalFontModel(
        family_name="F", style_name="R", reference_id="ref", style_id="reg",
        config_hash="a" * 64, browser_version="chromium", fit_observations_count=1,
        calibration_fingerprint="b" * 64,
        glyphs={65: CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50.0, 50.0, 550.0, 700.0), [_make_sample_contour()], observation_fingerprints=("a" * 64,))},
    )
    rec, png = _make_observation_record(code_point=65)

    evidence = asyncio.run(ChromiumEvidenceProducer.produce(art, model, [rec], held_out_pairs=None))
    assert evidence.result.held_out_pairs_non_regression is False


@pytest.mark.asyncio
async def test_chromium_producer_safely_handles_special_characters() -> None:
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
        builder = MaxCandidateFontBuilder("TestSpecial", "Regular", units_per_em=1000)
        reconstructed = {
            34: ReconstructedGlyph(34, '"', 500.0, 50.0, 50.0, 750.0, -200.0, contours=[_make_sample_contour()], bounding_box_upem=(50, 50, 450, 700)),
            92: ReconstructedGlyph(92, '\\', 500.0, 50.0, 50.0, 750.0, -200.0, contours=[_make_sample_contour()], bounding_box_upem=(50, 50, 450, 700)),
        }
        build_result = builder.build_candidate_family(reconstructed, tmp_path)
        art = CandidateArtifact.from_source(build_result.ttf.file_path)

        glyph_quote = CalibratedGlyph(34, '"', 500.0, 50.0, 50.0, 750.0, -200.0, (50, 50, 450, 700), [_make_sample_contour()], observation_fingerprints=("a" * 64,))
        glyph_slash = CalibratedGlyph(92, '\\', 500.0, 50.0, 50.0, 750.0, -200.0, (50, 50, 450, 700), [_make_sample_contour()], observation_fingerprints=("b" * 64,))

        model = CanonicalFontModel(
            family_name="TestSpecial", style_name="Regular", reference_id="test_font", style_id="regular",
            config_hash="a" * 64, browser_version="chromium", fit_observations_count=2,
            calibration_fingerprint="b" * 64, glyphs={34: glyph_quote, 92: glyph_slash},
        )
        rec_quote, png_q = _make_observation_record(code_point=34)
        pair_special = PairKerningObservation(34, 92, '"', '\\', 500.0, 500.0, 1000.0, 0, False, provenance="chromium:chromium:canvas_text_metrics")

        evidence = await ChromiumEvidenceProducer.produce(art, model, [rec_quote], [pair_special])
        assert evidence.result.is_direct_loadable_chromium is True
        assert evidence.result.rendered_canvas_valid is True
        assert len(evidence.result.pair_samples) == 1
        assert evidence.result.pair_samples[0].pair == '"\\'


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
            candidate_source=desc,
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

        assert bundle.fonttools.result.is_direct_loadable_fonttools is True
        assert bundle.freetype.result.min_raster_iou > 0.85
        assert len(bundle.freetype.result.samples) == 2
        assert bundle.harfbuzz.result.all_in_cmap is True
        assert bundle.harfbuzz.result.all_sequence_match is True

        has_chromium = False
        try:
            find_chromium_executable()
            has_chromium = True
        except Exception:
            has_chromium = False

        if has_chromium:
            assert bundle.chromium.result.is_available is True
            assert bundle.chromium.result.is_direct_loadable_chromium is True
            assert bundle.chromium.result.rendered_canvas_valid is True
            assert len(bundle.chromium.result.pair_samples) == 1

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
# 6. Additional Negative, Boundary & Isolation Tests
# =========================================================================

def test_candidate_artifact_rejects_corrupt_empty_and_mismatched_inputs() -> None:
    with pytest.raises(FileNotFoundError):
        CandidateArtifact.from_source(Path("non_existent_file.ttf"))

    with pytest.raises(ValueError, match="bytes cannot be empty"):
        CandidateArtifact.from_source(b"")

    with pytest.raises(ValueError, match="UNSUPPORTED_OR_CORRUPT_FORMAT"):
        CandidateArtifact.from_source(b"INVALID_HEADER_BYTES_1234567890")

    with pytest.raises(ValueError, match="FORMAT_MISMATCH"):
        CandidateArtifact.from_source(b"OTTO\x00\x00\x00\x00", format_hint="TTF")


def test_fonttools_producer_validation_and_table_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        builder = MaxCandidateFontBuilder("TestFont", "Regular", units_per_em=1000)
        reconstructed = {
            65: ReconstructedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, contours=[_make_sample_contour()], bounding_box_upem=(50, 50, 550, 700)),
        }
        build_result = builder.build_candidate_family(reconstructed, tmp_path)
        art = CandidateArtifact.from_source(build_result.ttf.file_path)

        evidence = FontToolsEvidenceProducer.produce(art)
        assert evidence.candidate_artifact_sha == art.sha256_hex
        assert evidence.result.is_direct_loadable_fonttools is True
        assert evidence.result.has_valid_cmap is True
        assert evidence.result.has_valid_metrics is True
        assert evidence.result.decompression_round_trip is True
        assert evidence.result.validation_error is None


def test_fonttools_producer_corrupt_font_fails_closed() -> None:
    corrupt_bytes = b"\x00\x01\x00\x00" + b"\x00" * 500
    art = CandidateArtifact.from_source(corrupt_bytes, format_hint="TTF")

    ft_ev = FontToolsEvidenceProducer.produce(art)
    assert ft_ev.result.is_direct_loadable_fonttools is False
    assert ft_ev.result.validation_error is not None


def test_producer_cross_artifact_mix_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        builder1 = MaxCandidateFontBuilder("Font1", "Regular", units_per_em=1000)
        builder2 = MaxCandidateFontBuilder("Font2", "Regular", units_per_em=1000)

        reconstructed1 = {65: ReconstructedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, contours=[_make_sample_contour()], bounding_box_upem=(50, 50, 550, 700))}
        reconstructed2 = {65: ReconstructedGlyph(65, "A", 600.0, 40.0, 40.0, 750.0, -200.0, contours=[_make_sample_contour(10)], bounding_box_upem=(40, 50, 560, 700))}

        b1 = builder1.build_candidate_family(reconstructed1, tmp_path / "1")
        b2 = builder2.build_candidate_family(reconstructed2, tmp_path / "2")

        art1 = CandidateArtifact.from_source(b1.ttf.file_path)
        art2 = CandidateArtifact.from_source(b2.ttf.file_path)

        ft_2 = FontToolsEvidenceProducer.produce(art2)

        with pytest.raises(ValueError, match="BoundFontToolsEvidence SHA mismatch"):
            BoundFontToolsEvidence(candidate_artifact_sha=art1.sha256_hex, result=ft_2.result)


def test_unknown_held_out_code_point_rejected_before_execution() -> None:
    art = CandidateArtifact.from_source(b"\x00\x01\x00\x00" + b"\x00" * 100, format_hint="TTF")
    model = CanonicalFontModel(
        family_name="F", style_name="R", reference_id="ref", style_id="reg",
        config_hash="a" * 64, browser_version="chromium", fit_observations_count=1,
        calibration_fingerprint="b" * 64,
        glyphs={65: CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50.0, 50.0, 550.0, 700.0), [_make_sample_contour()], observation_fingerprints=("a" * 64,))},
    )
    config = ObservationConfig()
    rec_unknown, _ = _make_observation_record(code_point=999)
    pair = PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False, provenance="chromium:chromium:canvas_text_metrics")

    with pytest.raises(ValueError, match="UNKNOWN_HELD_OUT_CODE_POINT"):
        asyncio.run(
            ProductionConsumerEvidenceProducer.produce_bundle(
                candidate_source=art,
                model=model,
                config=config,
                held_out_records=[rec_unknown],
                held_out_pairs=[pair],
                raster_provider=lambda r: b"",
            )
        )


def test_chromium_unavailable_fails_gate_closed() -> None:
    art = CandidateArtifact.from_source(b"\x00\x01\x00\x00" + b"\x00" * 100, format_hint="TTF")
    rec, png_bytes = _make_observation_record(code_point=65)
    glyph = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50.0, 50.0, 550.0, 700.0), [_make_sample_contour()], observation_fingerprints=("a" * 64,))
    model = CanonicalFontModel(
        family_name="F", style_name="R", reference_id="ref", style_id="reg",
        config_hash="a" * 64, browser_version="chromium", fit_observations_count=1,
        calibration_fingerprint="b" * 64, glyphs={65: glyph},
    )
    pair = PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False, provenance="chromium:chromium:canvas_text_metrics")

    unavail_chromium = ChromiumValidationResult(
        is_available=False,
        browser_version="unavailable",
        is_direct_loadable_chromium=False,
        fallback_rejection_verified=False,
        measured_glyph_count=0,
        mean_chromium_advance_error_upem=0.0,
        rendered_canvas_valid=False,
        error_message="CHROMIUM_NOT_AVAILABLE",
        held_out_pairs_non_regression=False,
    )

    bundle = ConsumerEvidenceBundle(
        schema_version="1.0.0",
        model_canonical_hash=model.compute_canonical_hash(),
        config_hash="a" * 64,
        held_out_fingerprint=FidelityEvaluator._compute_composite_held_out_fingerprint([rec], [pair]),
        held_out_raster_fingerprint=FidelityEvaluator._compute_records_fingerprint([rec]),
        held_out_typography_fingerprint=FidelityEvaluator._compute_typography_fingerprint([pair]),
        candidate_artifact_sha=art.sha256_hex,
        fonttools=BoundFontToolsEvidence(candidate_artifact_sha=art.sha256_hex, result=FormatValidationResult("TTF", "f.ttf", 100, art.sha256_hex, True, False, False, False, False, 1, 1000, True, True, True)),
        freetype=BoundFreeTypeEvidence(candidate_artifact_sha=art.sha256_hex, result=RasterComparisonResult(65, "A", 256, 0.95, 0)),
        harfbuzz=BoundHarfBuzzEvidence(candidate_artifact_sha=art.sha256_hex, result=ShapingTestResult("A", "c", True, True, ["A"], ["A"], 1, 1, 650, 650, 0, 0)),
        chromium=BoundChromiumEvidence(candidate_artifact_sha=art.sha256_hex, result=unavail_chromium),
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
    assert report.consumer_gate.chromium_passed is False
