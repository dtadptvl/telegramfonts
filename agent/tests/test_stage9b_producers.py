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
    ConsumerEvidenceBundle,
    FidelityReport,
    FidelityThresholds,
)
from fidelity.producers import (
    CandidateArtifact,
    ChromiumEvidenceProducer,
    FontToolsEvidenceProducer,
    FreeTypeEvidenceProducer,
    HarfBuzzEvidenceProducer,
    ProductionConsumerEvidenceProducer,
)
from measurement.browser_session import find_chromium_executable
from measurement.calibration import ObservationCalibrator
from measurement.models import DirectMetrics, ObservationConfig, ObservationRecord
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


# =========================================================================
# 1. Candidate Artifact Validation & Rejection
# =========================================================================

def test_candidate_artifact_validation_success_and_sha_integrity() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        builder = MaxCandidateFontBuilder("TestFont", "Regular", units_per_em=1000)
        reconstructed = {
            65: ReconstructedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, contours=[_make_sample_contour()], bounding_box_upem=(50, 50, 550, 700)),
        }
        build_result = builder.build_candidate_family(reconstructed, tmp_path)

        # OTF from path
        art_otf = CandidateArtifact.from_source(build_result.otf.file_path)
        assert art_otf.format == "OTF"
        assert art_otf.size_bytes > 0
        assert art_otf.sha256_hex == hashlib.sha256(art_otf.raw_bytes).hexdigest()

        # TTF from bytes
        art_ttf = CandidateArtifact.from_source(build_result.ttf.file_path.read_bytes(), format_hint="TTF")
        assert art_ttf.format == "TTF"
        assert art_ttf.size_bytes > 0
        assert art_ttf.sha256_hex == hashlib.sha256(art_ttf.raw_bytes).hexdigest()


def test_candidate_artifact_rejects_corrupt_empty_and_mismatched_inputs() -> None:
    # 1. Non-existent file
    with pytest.raises(FileNotFoundError):
        CandidateArtifact.from_source(Path("non_existent_file.ttf"))

    # 2. Empty bytes
    with pytest.raises(ValueError, match="bytes cannot be empty"):
        CandidateArtifact.from_source(b"")

    # 3. Invalid magic header
    with pytest.raises(ValueError, match="UNSUPPORTED_OR_CORRUPT_FORMAT"):
        CandidateArtifact.from_source(b"INVALID_HEADER_BYTES_1234567890")

    # 4. Format mismatch (declaring TTF when bytes start with OTTO)
    with pytest.raises(ValueError, match="FORMAT_MISMATCH"):
        CandidateArtifact.from_source(b"OTTO\x00\x00\x00\x00", format_hint="TTF")


# =========================================================================
# 2. Individual Producer Execution & Failure Isolation
# =========================================================================

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


def test_freetype_producer_raster_comparison() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        builder = MaxCandidateFontBuilder("TestFont", "Regular", units_per_em=1000)
        reconstructed = {
            65: ReconstructedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, contours=[_make_sample_contour()], bounding_box_upem=(50, 50, 550, 700)),
        }
        build_result = builder.build_candidate_family(reconstructed, tmp_path)
        art = CandidateArtifact.from_source(build_result.ttf.file_path)

        rec, png_bytes = _make_observation_record(code_point=65, resolution=256)
        glyph = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50, 50, 550, 700), [_make_sample_contour()], observation_fingerprints=("a" * 64,))
        model = CanonicalFontModel(
            family_name="TestFont", style_name="Regular", reference_id="test_font", style_id="regular",
            config_hash="a" * 64, browser_version="chromium", fit_observations_count=1,
            calibration_fingerprint="b" * 64, glyphs={65: glyph},
        )

        evidence = FreeTypeEvidenceProducer.produce(art, model, [rec], lambda r: png_bytes)
        assert evidence.candidate_artifact_sha == art.sha256_hex
        assert evidence.result.render_error is None
        assert evidence.result.raster_iou > 0.85
        assert evidence.result.render_size_px == 256


def test_harfbuzz_producer_shaping_validation() -> None:
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

        glyph_A = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50, 50, 550, 700), [_make_sample_contour()], observation_fingerprints=("a" * 64,))
        glyph_B = CalibratedGlyph(66, "B", 600.0, 40.0, 40.0, 750.0, -200.0, (40, 50, 560, 700), [_make_sample_contour(10)], observation_fingerprints=("b" * 64,))
        model = CanonicalFontModel(
            family_name="TestFont", style_name="Regular", reference_id="test_font", style_id="regular",
            config_hash="a" * 64, browser_version="chromium", fit_observations_count=2,
            calibration_fingerprint="b" * 64, glyphs={65: glyph_A, 66: glyph_B},
            kerning_pairs={(65, 66): -20},
        )

        pair = PairKerningObservation(65, 66, "A", "B", 650.0, 600.0, 1230.0, -20, True, provenance="chromium:chromium:canvas_text_metrics")
        evidence = HarfBuzzEvidenceProducer.produce(art, model, [pair])

        assert evidence.candidate_artifact_sha == art.sha256_hex
        assert evidence.result.in_candidate_cmap is True
        assert evidence.result.glyph_sequence_match is True
        assert evidence.result.advance_delta_upem == 0


# =========================================================================
# 3. Production Bundle Assembler Positive Integration Fixture
# =========================================================================

@pytest.mark.asyncio
async def test_production_consumer_bundle_assembler_positive_fixture() -> None:
    """Full positive vertical slice executing real FontTools, FreeType, and HarfBuzz producers without caller booleans."""
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

        # 1. Run ProductionConsumerEvidenceProducer
        bundle = await ProductionConsumerEvidenceProducer.produce_bundle(
            candidate_source=ttf_file,
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
        assert bundle.freetype.result.raster_iou > 0.85
        assert bundle.harfbuzz.result.in_candidate_cmap is True

        # Check Chromium capability on host
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
            if report.overall_status != "PASS":
                print(f"REPORT FAILURE REASONS: {report.failure_reasons}")
            assert report.overall_status == "PASS"
            assert report.consumer_gate.status == "PASS"


# =========================================================================
# 4. Negative Tests & Fail-Closed Behavior
# =========================================================================

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

        ft_1 = FontToolsEvidenceProducer.produce(art1)
        ft_2 = FontToolsEvidenceProducer.produce(art2)

        # Cross-artifact substitution fails constructor / validation
        with pytest.raises(ValueError, match="BoundFontToolsEvidence SHA mismatch"):
            BoundFontToolsEvidence(candidate_artifact_sha=art1.sha256_hex, result=ft_2.result)


def test_corrupt_font_fails_all_producers_closed() -> None:
    corrupt_bytes = b"\x00\x01\x00\x00" + b"\x00" * 500  # valid header, corrupt table data
    art = CandidateArtifact.from_source(corrupt_bytes, format_hint="TTF")

    rec, png_bytes = _make_observation_record(code_point=65)
    model = CanonicalFontModel(
        family_name="F", style_name="R", reference_id="ref", style_id="reg",
        config_hash="a" * 64, browser_version="chromium", fit_observations_count=1,
        calibration_fingerprint="b" * 64,
        glyphs={65: CalibratedGlyph(65, "A", 650, 50, 50, 750, -200, (50, 50, 550, 700), [_make_sample_contour()], ("a" * 64,))},
    )

    ft_ev = FontToolsEvidenceProducer.produce(art)
    assert ft_ev.result.is_direct_loadable_fonttools is False
    assert ft_ev.result.validation_error is not None

    fr_ev = FreeTypeEvidenceProducer.produce(art, model, [rec], lambda r: png_bytes)
    assert fr_ev.result.render_error is not None

    hb_ev = HarfBuzzEvidenceProducer.produce(art, model)
    assert hb_ev.result.in_candidate_cmap is False


def test_zero_held_out_samples_fails_closed() -> None:
    art = CandidateArtifact.from_source(b"\x00\x01\x00\x00" + b"\x00" * 100, format_hint="TTF")
    model = CanonicalFontModel(
        family_name="F", style_name="R", reference_id="ref", style_id="reg",
        config_hash="a" * 64, browser_version="chromium", fit_observations_count=1,
        calibration_fingerprint="b" * 64,
        glyphs={65: CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50.0, 50.0, 550.0, 700.0), [_make_sample_contour()], observation_fingerprints=("a" * 64,))},
    )
    config = ObservationConfig()

    with pytest.raises(ValueError, match="ZERO_HELD_OUT_SAMPLES"):
        asyncio.run(ProductionConsumerEvidenceProducer.produce_bundle(
            candidate_source=art.raw_bytes,
            model=model,
            config=config,
            held_out_records=[],
            raster_provider=lambda r: b"",
        ))


def test_chromium_unavailable_fails_gate_closed() -> None:
    art = CandidateArtifact.from_source(b"\x00\x01\x00\x00" + b"\x00" * 100, format_hint="TTF")
    rec, png_bytes = _make_observation_record(code_point=65)
    glyph = CalibratedGlyph(65, "A", 650.0, 50.0, 50.0, 750.0, -200.0, (50.0, 50.0, 550.0, 700.0), [_make_sample_contour()], observation_fingerprints=("a" * 64,))
    model = CanonicalFontModel(
        family_name="F", style_name="R", reference_id="ref", style_id="reg",
        config_hash="a" * 64, browser_version="chromium", fit_observations_count=1,
        calibration_fingerprint="b" * 64, glyphs={65: glyph},
    )

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
        held_out_fingerprint=FidelityEvaluator._compute_records_fingerprint([rec]),
        candidate_artifact_sha=art.sha256_hex,
        fonttools=BoundFontToolsEvidence(candidate_artifact_sha=art.sha256_hex, result=FormatValidationResult("TTF", "f.ttf", 100, art.sha256_hex, True, True, True, True, True, 1, 1000, True, True, True)),
        freetype=BoundFreeTypeEvidence(candidate_artifact_sha=art.sha256_hex, result=RasterComparisonResult(65, "A", 256, 0.95, 0)),
        harfbuzz=BoundHarfBuzzEvidence(candidate_artifact_sha=art.sha256_hex, result=ShapingTestResult("A", "c", True, True, ["A"], ["A"], 1, 1, 650, 650, 0, 0)),
        chromium=BoundChromiumEvidence(candidate_artifact_sha=art.sha256_hex, result=unavail_chromium),
    )

    report = FidelityEvaluator.evaluate(
        model=model,
        config=ObservationConfig(),
        fit_records=[rec],
        held_out_records=[rec],
        held_out_pairs=[PairKerningObservation(65, 65, "A", "A", 650, 650, 1300, 0, False, provenance="chromium:chromium:canvas_text_metrics")],
        consumer_bundle=bundle,
        raster_provider=lambda r: png_bytes,
    )
    assert report.overall_status == "FAIL"
    assert report.consumer_gate.status == "FAIL"
    assert report.consumer_gate.chromium_passed is False
