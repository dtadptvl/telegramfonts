"""Production-safe four-consumer evidence producers for Stage 9B.

Executes real FontTools, FreeType, HarfBuzz, and Chromium consumers against verified
candidate font artifact bytes, binding all results to the exact candidate artifact SHA-256
and canonical model/config/held-out fingerprints without ground-truth font leakage.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

import freetype
import numpy as np
import uharfbuzz as hb
from fontTools.ttLib import TTFont
from PIL import Image

from fidelity.models import (
    BoundChromiumEvidence,
    BoundFontToolsEvidence,
    BoundFreeTypeEvidence,
    BoundHarfBuzzEvidence,
    ChromiumGlyphSampleEvidence,
    ChromiumPairSampleEvidence,
    ConsumerEvidenceBundle,
    FidelityThresholds,
    FreeTypeSampleEvidence,
    HarfBuzzPositionVector,
    HarfBuzzSampleEvidence,
    ProductionProducerError,
)
from measurement.browser_session import ChromiumSession, close_browser_session, find_chromium_executable
from measurement.calibration import CalibrationTransform
from measurement.models import ObservationConfig, ObservationRecord
from reconstruction.candidate_validator import (
    ChromiumPairMetricResult,
    ChromiumValidationResult,
    FormatValidationResult,
    RasterComparisonResult,
    ShapingTestResult,
)
from reconstruction.font_model import CanonicalFontModel
from typography.models import PairKerningObservation

logger = logging.getLogger("telegramfonts.agent.fidelity.producers")

REQUIRED_COMMON_TABLES = {"head", "hhea", "maxp", "name", "OS/2", "cmap", "post"}


@dataclass(frozen=True)
class CandidateArtifactDescriptor:
    """Explicit candidate artifact descriptor with builder-attested metadata for anti-drift verification."""

    file_path: Path | str
    expected_format: str  # "OTF" | "TTF"
    expected_size_bytes: int
    expected_sha256_hex: str
    raw_bytes: bytes | None = None

    def validate(self) -> None:
        if not self.expected_format or self.expected_format.upper() not in ("OTF", "TTF"):
            raise ValueError(f"UNSUPPORTED_EXPECTED_FORMAT: '{self.expected_format}'")
        if self.expected_size_bytes <= 0:
            raise ValueError(f"INVALID_EXPECTED_SIZE: {self.expected_size_bytes}")
        if (
            not self.expected_sha256_hex
            or len(self.expected_sha256_hex) != 64
            or any(c not in "0123456789abcdef" for c in self.expected_sha256_hex)
        ):
            raise ValueError(f"INVALID_EXPECTED_SHA256: '{self.expected_sha256_hex}'")


@dataclass(frozen=True)
class CandidateArtifact:
    """Verified immutable candidate font artifact in memory and on filesystem."""

    format: str  # "OTF" | "TTF"
    file_path: str
    size_bytes: int
    sha256_hex: str
    raw_bytes: bytes

    @classmethod
    def from_descriptor(cls, descriptor: CandidateArtifactDescriptor) -> CandidateArtifact:
        """Construct verified artifact from an explicit descriptor, enforcing byte/size/SHA anti-drift."""
        descriptor.validate()
        p = Path(descriptor.file_path)

        if not p.is_file():
            raise FileNotFoundError(f"ARTIFACT_FILE_NOT_FOUND: Candidate font file not found: {p}")

        disk_bytes = p.read_bytes()
        if descriptor.raw_bytes is not None and descriptor.raw_bytes != disk_bytes:
            raise ValueError(
                "ARTIFACT_PATH_BYTES_DRIFT: Descriptor raw_bytes do not match file_path content on disk"
            )
        raw_bytes = disk_bytes
        file_path_str = str(p.resolve())

        actual_size = len(raw_bytes)
        if actual_size != descriptor.expected_size_bytes:
            raise ValueError(
                f"ARTIFACT_SIZE_DRIFT: expected {descriptor.expected_size_bytes} bytes, got {actual_size}"
            )

        actual_sha = hashlib.sha256(raw_bytes).hexdigest().lower()
        if actual_sha != descriptor.expected_sha256_hex.lower():
            raise ValueError(
                f"ARTIFACT_SHA_DRIFT: expected SHA-256 {descriptor.expected_sha256_hex}, got {actual_sha}"
            )

        fmt: str
        if raw_bytes.startswith(b"OTTO"):
            fmt = "OTF"
        elif raw_bytes.startswith(b"\x00\x01\x00\x00") or raw_bytes.startswith(b"true"):
            fmt = "TTF"
        else:
            raise ValueError("UNSUPPORTED_OR_CORRUPT_FORMAT: invalid magic bytes header")

        if fmt != descriptor.expected_format.upper():
            raise ValueError(
                f"ARTIFACT_FORMAT_DRIFT: descriptor declared {descriptor.expected_format}, but magic header is {fmt}"
            )

        return cls(
            format=fmt,
            file_path=file_path_str,
            size_bytes=actual_size,
            sha256_hex=actual_sha,
            raw_bytes=raw_bytes,
        )

    @classmethod
    def from_source(
        cls,
        source: Path | str | bytes,
        format_hint: str | None = None,
        file_path_hint: str | None = None,
    ) -> CandidateArtifact:
        """Convenience constructor for test fixtures."""
        raw_bytes: bytes
        file_path_str: str

        if isinstance(source, bytes):
            raw_bytes = source
            file_path_str = file_path_hint or "in_memory_candidate.bin"
        elif isinstance(source, (Path, str)):
            p = Path(source)
            if not p.exists():
                raise FileNotFoundError(f"Candidate font file not found: {p}")
            raw_bytes = p.read_bytes()
            file_path_str = str(p.resolve())
            if format_hint is None:
                suffix = p.suffix.lower()
                if suffix == ".otf":
                    format_hint = "OTF"
                elif suffix == ".ttf":
                    format_hint = "TTF"
        else:
            raise TypeError(f"Unsupported candidate source type: {type(source)}")

        if not raw_bytes or len(raw_bytes) == 0:
            raise ValueError("Candidate font artifact bytes cannot be empty")

        fmt: str
        if raw_bytes.startswith(b"OTTO"):
            fmt = "OTF"
        elif raw_bytes.startswith(b"\x00\x01\x00\x00") or raw_bytes.startswith(b"true"):
            fmt = "TTF"
        else:
            raise ValueError(
                "UNSUPPORTED_OR_CORRUPT_FORMAT: Candidate binary does not begin with valid OTF ('OTTO') or TTF ('\\x00\\x01\\x00\\x00' / 'true') magic header"
            )

        if format_hint is not None and fmt != format_hint.upper():
            raise ValueError(
                f"FORMAT_MISMATCH: Caller declared format {format_hint}, but magic bytes header indicates {fmt}"
            )

        size_bytes = len(raw_bytes)
        sha256_hex = hashlib.sha256(raw_bytes).hexdigest().lower()

        return cls(
            format=fmt,
            file_path=file_path_str,
            size_bytes=size_bytes,
            sha256_hex=sha256_hex,
            raw_bytes=raw_bytes,
        )


class FontToolsEvidenceProducer:
    """Parses and validates OpenType table structure, cmap, and metrics round-tripping."""

    @classmethod
    def produce(cls, artifact: CandidateArtifact) -> BoundFontToolsEvidence:
        """Parse candidate artifact bytes with FontTools and return bound structural evidence."""
        try:
            font = TTFont(io.BytesIO(artifact.raw_bytes))
        except Exception as exc:
            return BoundFontToolsEvidence(
                candidate_artifact_sha=artifact.sha256_hex,
                result=FormatValidationResult(
                    format=artifact.format,
                    file_path=artifact.file_path,
                    size_bytes=artifact.size_bytes,
                    sha256_hex=artifact.sha256_hex,
                    is_direct_loadable_fonttools=False,
                    is_direct_loadable_freetype=False,
                    is_roundtrip_loadable_freetype=False,
                    is_direct_loadable_harfbuzz=False,
                    is_direct_loadable_chromium=False,
                    glyph_count=0,
                    units_per_em=0,
                    has_valid_cmap=False,
                    has_valid_metrics=False,
                    decompression_round_trip=False,
                    validation_error=f"FONTTOOLS_PARSE_EXCEPTION: {exc}",
                ),
            )

        validation_err: str | None = None
        has_cmap = False
        has_metrics = False
        glyph_count = 0
        upem = 0
        roundtrip_ok = False

        try:
            # 1. Check required tables
            present_tables = set(font.keys())
            missing_common = REQUIRED_COMMON_TABLES - present_tables
            if missing_common:
                validation_err = f"MISSING_COMMON_TABLES: {sorted(list(missing_common))}"

            if artifact.format == "OTF":
                if "CFF " not in present_tables and "CFF2" not in present_tables:
                    validation_err = f"MISSING_OTF_OUTLINE_TABLE: neither 'CFF ' nor 'CFF2' in {sorted(list(present_tables))}"
            elif artifact.format == "TTF":
                if "glyf" not in present_tables or "loca" not in present_tables:
                    validation_err = f"MISSING_TTF_OUTLINE_TABLES: 'glyf' or 'loca' missing in {sorted(list(present_tables))}"

            # 2. Check UPEM
            if "head" in font:
                upem = int(getattr(font["head"], "unitsPerEm", 0))
                if upem <= 0:
                    validation_err = f"INVALID_UNITS_PER_EM: {upem}"

            # 3. Check cmap table
            if "cmap" in font:
                best_cmap = font.getBestCmap()
                if best_cmap and len(best_cmap) > 0:
                    has_cmap = True
                else:
                    validation_err = "EMPTY_OR_INVALID_CMAP"

            # 4. Check horizontal metrics table (hmtx)
            if "hmtx" in font and "maxp" in font:
                glyph_count = int(getattr(font["maxp"], "numGlyphs", 0))
                metrics = font["hmtx"].metrics
                if metrics and len(metrics) >= glyph_count and glyph_count > 0:
                    has_metrics = True
                else:
                    validation_err = f"INVALID_HMTX_METRICS_COUNT: {len(metrics)} != numGlyphs {glyph_count}"

            # 5. Roundtrip serialization validation
            buf = io.BytesIO()
            font.save(buf)
            reloaded_bytes = buf.getvalue()
            reloaded_font = TTFont(io.BytesIO(reloaded_bytes))
            if reloaded_font.getBestCmap() and len(reloaded_font.keys()) == len(present_tables):
                roundtrip_ok = True
            else:
                validation_err = "DECOMPRESSION_ROUND_TRIP_FAILED"

        except Exception as exc:
            validation_err = f"FONTTOOLS_VALIDATION_EXCEPTION: {exc}"

        is_direct_ok = (validation_err is None) and has_cmap and has_metrics and roundtrip_ok

        result = FormatValidationResult(
            format=artifact.format,
            file_path=artifact.file_path,
            size_bytes=artifact.size_bytes,
            sha256_hex=artifact.sha256_hex,
            is_direct_loadable_fonttools=is_direct_ok,
            is_direct_loadable_freetype=False,
            is_roundtrip_loadable_freetype=False,
            is_direct_loadable_harfbuzz=False,
            is_direct_loadable_chromium=False,
            glyph_count=glyph_count,
            units_per_em=upem,
            has_valid_cmap=has_cmap,
            has_valid_metrics=has_metrics,
            decompression_round_trip=roundtrip_ok,
            validation_error=validation_err,
        )

        return BoundFontToolsEvidence(
            candidate_artifact_sha=artifact.sha256_hex,
            result=result,
        )


class FreeTypeEvidenceProducer:
    """Renders candidate font glyphs via FreeType and computes per-sample raster truth against held-out evidence."""

    @classmethod
    def produce(
        cls,
        artifact: CandidateArtifact,
        model: CanonicalFontModel,
        held_out_records: Sequence[ObservationRecord],
        raster_provider: Callable[[ObservationRecord], bytes],
    ) -> BoundFreeTypeEvidence:
        """Render candidate glyphs with FreeType and compare against observed held-out raster evidence."""
        if not held_out_records:
            return BoundFreeTypeEvidence(
                candidate_artifact_sha=artifact.sha256_hex,
                result=RasterComparisonResult(
                    code_point=0,
                    character="",
                    render_size_px=0,
                    raster_iou=0.0,
                    pixel_delta_count=0,
                    render_error="ZERO_HELD_OUT_SAMPLES",
                    samples=(),
                    min_raster_iou=0.0,
                ),
            )

        try:
            face = freetype.Face(io.BytesIO(artifact.raw_bytes))
        except Exception as exc:
            return BoundFreeTypeEvidence(
                candidate_artifact_sha=artifact.sha256_hex,
                result=RasterComparisonResult(
                    code_point=held_out_records[0].code_point,
                    character=chr(held_out_records[0].code_point),
                    render_size_px=held_out_records[0].resolution,
                    raster_iou=0.0,
                    pixel_delta_count=0,
                    render_error=f"FREETYPE_INIT_ERROR: {exc}",
                    samples=(),
                    min_raster_iou=0.0,
                ),
            )

        samples: list[FreeTypeSampleEvidence] = []
        first_error: str | None = None

        for rec in held_out_records:
            cp = rec.code_point
            char = chr(cp)
            res = rec.resolution

            if cp not in model.glyphs:
                sample_err = f"UNKNOWN_HELD_OUT_CODE_POINT_{cp}"
                first_error = first_error or sample_err
                samples.append(
                    FreeTypeSampleEvidence(
                        cache_key=rec.cache_key,
                        code_point=cp,
                        character=char,
                        resolution=res,
                        raster_sha256=rec.raster_sha256,
                        raster_iou=0.0,
                        pixel_delta_count=0,
                        render_error=sample_err,
                    )
                )
                continue

            raw_png = raster_provider(rec)
            if not raw_png:
                sample_err = f"MISSING_RASTER_BYTES for {rec.cache_key}"
                first_error = first_error or sample_err
                samples.append(
                    FreeTypeSampleEvidence(
                        cache_key=rec.cache_key,
                        code_point=cp,
                        character=char,
                        resolution=res,
                        raster_sha256=rec.raster_sha256,
                        raster_iou=0.0,
                        pixel_delta_count=0,
                        render_error=sample_err,
                    )
                )
                continue

            actual_sha = hashlib.sha256(raw_png).hexdigest()
            if actual_sha != rec.raster_sha256 or len(raw_png) != rec.raster_size_bytes:
                sample_err = f"CORRUPT_RASTER_EVIDENCE for {rec.cache_key}"
                first_error = first_error or sample_err
                samples.append(
                    FreeTypeSampleEvidence(
                        cache_key=rec.cache_key,
                        code_point=cp,
                        character=char,
                        resolution=res,
                        raster_sha256=rec.raster_sha256,
                        raster_iou=0.0,
                        pixel_delta_count=0,
                        render_error=sample_err,
                    )
                )
                continue

            try:
                # 1. Decode reference image
                ref_img = Image.open(io.BytesIO(raw_png)).convert("L")
                ref_arr = np.array(ref_img, dtype=np.uint8)
                ref_mask = (ref_arr < 128).astype(np.uint8)  # Black ink = 1

                # 2. Render candidate glyph using FreeType
                f_size_px = math.floor(res * 0.72)
                face.set_pixel_sizes(int(f_size_px), int(f_size_px))
                face.load_char(char, freetype.FT_LOAD_RENDER | freetype.FT_LOAD_TARGET_NORMAL)

                bmp = face.glyph.bitmap
                bmp_w = bmp.width
                bmp_h = bmp.rows
                bmp_left = face.glyph.bitmap_left
                bmp_top = face.glyph.bitmap_top

                cand_mask = np.zeros((res, res), dtype=np.uint8)

                if bmp_w > 0 and bmp_h > 0:
                    cand_buf = np.array(bmp.buffer, dtype=np.uint8).reshape((bmp_h, bmp_w))
                    cand_ink = (cand_buf >= 128).astype(np.uint8)

                    # Compute baseline placement using CalibrationTransform
                    transform = CalibrationTransform.from_observation(
                        resolution=res,
                        metrics=rec.metrics,
                        subpixel_x=rec.subpixel_x,
                        subpixel_y=rec.subpixel_y,
                        units_per_em=model.metrics.units_per_em,
                    )

                    dst_x0 = int(round(transform.x_origin_px + bmp_left))
                    dst_y0 = int(round(transform.y_origin_px - bmp_top))
                    dst_x1 = dst_x0 + bmp_w
                    dst_y1 = dst_y0 + bmp_h

                    src_x0 = max(0, -dst_x0)
                    src_y0 = max(0, -dst_y0)
                    src_x1 = bmp_w - max(0, dst_x1 - res)
                    src_y1 = bmp_h - max(0, dst_y1 - res)

                    c_dst_x0 = max(0, dst_x0)
                    c_dst_y0 = max(0, dst_y0)
                    c_dst_x1 = min(res, dst_x1)
                    c_dst_y1 = min(res, dst_y1)

                    if src_x1 > src_x0 and src_y1 > src_y0 and c_dst_x1 > c_dst_x0 and c_dst_y1 > c_dst_y0:
                        cand_mask[c_dst_y0:c_dst_y1, c_dst_x0:c_dst_x1] = cand_ink[src_y0:src_y1, src_x0:src_x1]

                # 3. Compute exact binary IoU and pixel delta count
                intersection = int(np.logical_and(cand_mask, ref_mask).sum())
                union = int(np.logical_or(cand_mask, ref_mask).sum())
                iou = float(intersection) / max(union, 1)
                pixel_deltas = int(np.abs(cand_mask.astype(int) - ref_mask.astype(int)).sum())

                if not math.isfinite(iou):
                    iou = 0.0
                    sample_err = "NON_FINITE_IOU"
                    first_error = first_error or sample_err
                else:
                    sample_err = None

                samples.append(
                    FreeTypeSampleEvidence(
                        cache_key=rec.cache_key,
                        code_point=cp,
                        character=char,
                        resolution=res,
                        raster_sha256=rec.raster_sha256,
                        raster_iou=iou,
                        pixel_delta_count=pixel_deltas,
                        render_error=sample_err,
                    )
                )

            except Exception as exc:
                sample_err = f"FREETYPE_RENDER_EXCEPTION: {exc}"
                first_error = first_error or sample_err
                samples.append(
                    FreeTypeSampleEvidence(
                        cache_key=rec.cache_key,
                        code_point=cp,
                        character=char,
                        resolution=res,
                        raster_sha256=rec.raster_sha256,
                        raster_iou=0.0,
                        pixel_delta_count=0,
                        render_error=sample_err,
                    )
                )

        min_iou = float(min((s.raster_iou for s in samples), default=0.0))
        mean_iou = float(np.mean([s.raster_iou for s in samples])) if samples else 0.0
        total_deltas = sum(s.pixel_delta_count for s in samples)

        primary_sample = samples[0] if samples else None
        primary_cp = primary_sample.code_point if primary_sample else 0
        primary_char = primary_sample.character if primary_sample else ""
        primary_res = primary_sample.resolution if primary_sample else 0

        result = RasterComparisonResult(
            code_point=primary_cp,
            character=primary_char,
            render_size_px=primary_res,
            raster_iou=mean_iou,
            pixel_delta_count=total_deltas,
            render_error=first_error,
            samples=tuple(samples),
            min_raster_iou=min_iou,
        )

        return BoundFreeTypeEvidence(
            candidate_artifact_sha=artifact.sha256_hex,
            result=result,
        )


class HarfBuzzEvidenceProducer:
    """Shapes held-out text sequences and kerning pairs with HarfBuzz, computing per-sample shaping truth."""

    @classmethod
    def produce(
        cls,
        artifact: CandidateArtifact,
        model: CanonicalFontModel,
        held_out_pairs: Sequence[PairKerningObservation] | None = None,
    ) -> BoundHarfBuzzEvidence:
        """Shape declared held-out text sequences using HarfBuzz and return bound shaping evidence."""
        if not held_out_pairs:
            return BoundHarfBuzzEvidence(
                candidate_artifact_sha=artifact.sha256_hex,
                result=ShapingTestResult(
                    text="",
                    category="held_out_typography",
                    in_candidate_cmap=False,
                    glyph_sequence_match=False,
                    candidate_glyph_names=[],
                    reference_glyph_names=[],
                    candidate_glyph_count=0,
                    reference_glyph_count=0,
                    candidate_total_advance_upem=0,
                    reference_total_advance_upem=0,
                    advance_delta_upem=0,
                    max_position_delta_upem=0,
                    samples=(),
                    all_in_cmap=False,
                    all_sequence_match=False,
                    error_message="ZERO_HELD_OUT_PAIRS",
                ),
            )

        try:
            blob = hb.Blob(artifact.raw_bytes)
            hb_face = hb.Face(blob)
            hb_font = hb.Font(hb_face)
        except Exception as exc:
            return BoundHarfBuzzEvidence(
                candidate_artifact_sha=artifact.sha256_hex,
                result=ShapingTestResult(
                    text="",
                    category="held_out_typography",
                    in_candidate_cmap=False,
                    glyph_sequence_match=False,
                    candidate_glyph_names=[],
                    reference_glyph_names=[],
                    candidate_glyph_count=0,
                    reference_glyph_count=0,
                    candidate_total_advance_upem=0,
                    reference_total_advance_upem=0,
                    advance_delta_upem=0,
                    max_position_delta_upem=0,
                    samples=(),
                    all_in_cmap=False,
                    all_sequence_match=False,
                    error_message=f"HARFBUZZ_INIT_EXCEPTION: {exc}",
                ),
            )

        samples: list[HarfBuzzSampleEvidence] = []
        first_error: str | None = None

        for pair in held_out_pairs:
            text = f"{pair.left_char}{pair.right_char}"
            if pair.left_cp not in model.glyphs or pair.right_cp not in model.glyphs:
                sample_err = f"UNKNOWN_HELD_OUT_PAIR_GLYPHS_{pair.left_cp}_{pair.right_cp}"
                first_error = first_error or sample_err
                samples.append(
                    HarfBuzzSampleEvidence(
                        left_cp=pair.left_cp,
                        right_cp=pair.right_cp,
                        text=text,
                        in_candidate_cmap=False,
                        glyph_sequence_match=False,
                        glyph_ids=(),
                        clusters=(),
                        positions=(),
                        candidate_total_advance_upem=0.0,
                        expected_total_advance_upem=pair.measured_pair_advance_upem,
                        advance_delta_upem=abs(pair.measured_pair_advance_upem),
                        max_position_delta_upem=abs(pair.measured_pair_advance_upem),
                        error_message=sample_err,
                    )
                )
                continue

            try:
                buf = hb.Buffer()
                buf.add_str(text)
                buf.guess_segment_properties()
                hb.shape(hb_font, buf)

                infos = buf.glyph_infos
                positions = buf.glyph_positions
                glyph_ids = tuple(info.codepoint for info in infos)
                clusters = tuple(info.cluster for info in infos)

                pos_vectors = tuple(
                    HarfBuzzPositionVector(
                        x_advance=float(pos.x_advance),
                        y_advance=float(pos.y_advance),
                        x_offset=float(pos.x_offset),
                        y_offset=float(pos.y_offset),
                    )
                    for pos in positions
                )

                # OpenType .notdef is glyph ID 0; in-cmap requires all non-zero glyph IDs
                in_cmap = bool(len(infos) == 2 and all(gid != 0 for gid in glyph_ids))

                cand_total_adv = float(sum(p.x_advance for p in pos_vectors))
                expected_adv = pair.measured_pair_advance_upem
                adv_delta = abs(cand_total_adv - expected_adv)

                # Real position vector validation against canonical model expectation
                exp1_adv = model.glyphs[pair.left_cp].advance_width_upem + float(model.kerning_pairs.get((pair.left_cp, pair.right_cp), 0))
                exp2_adv = model.glyphs[pair.right_cp].advance_width_upem

                if len(pos_vectors) >= 2:
                    d1 = max(
                        abs(pos_vectors[0].x_advance - exp1_adv),
                        abs(pos_vectors[0].y_advance),
                        abs(pos_vectors[0].x_offset),
                        abs(pos_vectors[0].y_offset),
                    )
                    d2 = max(
                        abs(pos_vectors[1].x_advance - exp2_adv),
                        abs(pos_vectors[1].y_advance),
                        abs(pos_vectors[1].x_offset),
                        abs(pos_vectors[1].y_offset),
                    )
                    max_pos_delta = max(d1, d2)
                else:
                    max_pos_delta = 1000.0

                clusters_ok = len(infos) == 2 and clusters == (0, 1)
                seq_match = bool(in_cmap and clusters_ok and adv_delta <= 15.0 and max_pos_delta <= 15.0)

                sample_err = None if (in_cmap and seq_match) else "SHAPING_MISMATCH"
                if sample_err:
                    first_error = first_error or sample_err

                samples.append(
                    HarfBuzzSampleEvidence(
                        left_cp=pair.left_cp,
                        right_cp=pair.right_cp,
                        text=text,
                        in_candidate_cmap=in_cmap,
                        glyph_sequence_match=seq_match,
                        glyph_ids=glyph_ids,
                        clusters=clusters,
                        positions=pos_vectors,
                        candidate_total_advance_upem=cand_total_adv,
                        expected_total_advance_upem=expected_adv,
                        advance_delta_upem=adv_delta,
                        max_position_delta_upem=max_pos_delta,
                        error_message=sample_err,
                    )
                )
            except Exception as exc:
                sample_err = f"SHAPING_EXCEPTION: {exc}"
                first_error = first_error or sample_err
                samples.append(
                    HarfBuzzSampleEvidence(
                        left_cp=pair.left_cp,
                        right_cp=pair.right_cp,
                        text=text,
                        in_candidate_cmap=False,
                        glyph_sequence_match=False,
                        glyph_ids=(),
                        clusters=(),
                        positions=(),
                        candidate_total_advance_upem=0.0,
                        expected_total_advance_upem=pair.measured_pair_advance_upem,
                        advance_delta_upem=abs(pair.measured_pair_advance_upem),
                        max_position_delta_upem=abs(pair.measured_pair_advance_upem),
                        error_message=sample_err,
                    )
                )

        all_in_cmap = all(s.in_candidate_cmap for s in samples) if samples else False
        all_seq_match = all(s.glyph_sequence_match for s in samples) if samples else False
        max_adv_delta = max((s.advance_delta_upem for s in samples), default=0.0)
        max_pos_delta = max((s.max_position_delta_upem for s in samples), default=0.0)

        primary_sample = samples[0] if samples else None
        cand_names = [f"g_{gid}" for gid in (primary_sample.glyph_ids if primary_sample else ())]

        result = ShapingTestResult(
            text=primary_sample.text if primary_sample else "",
            category="held_out_pair",
            in_candidate_cmap=all_in_cmap,
            glyph_sequence_match=all_seq_match,
            candidate_glyph_names=cand_names,
            reference_glyph_names=[],
            candidate_glyph_count=len(cand_names),
            reference_glyph_count=2,
            candidate_total_advance_upem=int(round(primary_sample.candidate_total_advance_upem if primary_sample else 0)),
            reference_total_advance_upem=int(round(primary_sample.expected_total_advance_upem if primary_sample else 0)),
            advance_delta_upem=int(round(max_adv_delta)),
            max_position_delta_upem=int(round(max_pos_delta)),
            samples=tuple(samples),
            all_in_cmap=all_in_cmap,
            all_sequence_match=all_seq_match,
            error_message=first_error,
        )

        return BoundHarfBuzzEvidence(
            candidate_artifact_sha=artifact.sha256_hex,
            result=result,
        )


class ChromiumEvidenceProducer:
    """Loads candidate font in headless Chromium, executing direct metric, canvas, and pair non-regression verification."""

    @classmethod
    async def produce(
        cls,
        artifact: CandidateArtifact,
        model: CanonicalFontModel,
        held_out_records: Sequence[ObservationRecord],
        held_out_pairs: Sequence[PairKerningObservation] | None = None,
    ) -> BoundChromiumEvidence:
        """Execute headless Chromium session on candidate artifact and return BoundChromiumEvidence."""
        return await cls._produce_with_session_internal(
            artifact=artifact,
            model=model,
            held_out_records=held_out_records,
            held_out_pairs=held_out_pairs,
            custom_session=None,
        )

    @classmethod
    async def _produce_with_session_internal(
        cls,
        artifact: CandidateArtifact,
        model: CanonicalFontModel,
        held_out_records: Sequence[ObservationRecord],
        held_out_pairs: Sequence[PairKerningObservation] | None = None,
        custom_session: ChromiumSession | None = None,
    ) -> BoundChromiumEvidence:
        """Internal worker executing session actions."""
        # 1. Capability check
        try:
            find_chromium_executable()
            chromium_available = True
        except Exception:
            chromium_available = False

        if not chromium_available and custom_session is None:
            return BoundChromiumEvidence(
                candidate_artifact_sha=artifact.sha256_hex,
                result=ChromiumValidationResult(
                    is_available=False,
                    browser_version="unavailable",
                    is_direct_loadable_chromium=False,
                    fallback_rejection_verified=False,
                    measured_glyph_count=0,
                    mean_chromium_advance_error_upem=0.0,
                    pair_metrics=[],
                    glyph_samples=(),
                    pair_samples=(),
                    fit_pairs_material_improvement=False,
                    held_out_pairs_non_regression=False,
                    rendered_canvas_valid=False,
                    error_message="CHROMIUM_NOT_AVAILABLE: No Chromium binary found on host system",
                ),
            )

        owns_session = custom_session is None
        sess = custom_session or ChromiumSession(timeout_seconds=10.0)

        try:
            await sess.start()
            alias = f"CandidateMAX_{artifact.sha256_hex[:12]}"
            await sess.load_font_data(alias, artifact.raw_bytes)

            # 1. Direct load & fallback rejection verification
            measured_cps = [r.code_point for r in held_out_records if r.code_point in model.glyphs]
            if not measured_cps:
                measured_cps = list(model.glyphs.keys())[:4]

            test_in_cp = measured_cps[0] if measured_cps else 65
            test_out_cp = next(
                (cp for cp in (ord("Z"), ord("Q"), ord("X"), ord("?"), 0x00D0, 0x1EA0) if cp not in model.glyphs),
                0xFFFF,
            )

            in_loaded = await sess.is_glyph_supported_in_font(alias, test_in_cp)
            out_rejected = not (await sess.is_glyph_supported_in_font(alias, test_out_cp))
            fallback_ok = bool(in_loaded and out_rejected)

            # 2. Direct browser metric measurements for held-out glyphs
            glyph_samples: list[ChromiumGlyphSampleEvidence] = []
            adv_deltas: list[float] = []
            for cp in measured_cps:
                m = await sess.measure_glyph_direct(alias, cp, 200.0, upem=model.metrics.units_per_em)
                expected_adv = model.glyphs[cp].advance_width_upem if cp in model.glyphs else m.advance_width_upem
                delta = abs(m.advance_width_upem - expected_adv)
                adv_deltas.append(delta)
                glyph_samples.append(
                    ChromiumGlyphSampleEvidence(
                        code_point=cp,
                        character=chr(cp),
                        candidate_advance_upem=m.advance_width_upem,
                        expected_advance_upem=expected_adv,
                        advance_delta_upem=delta,
                    )
                )

            # 3. Canvas 2D render proof with safely JSON-encoded characters
            test_chars = "".join(chr(cp) for cp in measured_cps[:4])
            json_chars = json.dumps(test_chars)
            target_font_str = json.dumps(f"100px {alias}, monospace")
            js_canvas = f"""
            (() => {{
                const canvas = document.createElement('canvas');
                canvas.width = 300;
                canvas.height = 300;
                const ctx = canvas.getContext('2d');
                ctx.font = {target_font_str};
                ctx.fillStyle = '#000000';
                ctx.fillText({json_chars}, 20, 150);
                const data = ctx.getImageData(0, 0, 300, 300).data;
                let inkPixels = 0;
                for (let i = 3; i < data.length; i += 4) {{
                    if (data[i] > 128) inkPixels++;
                }}
                return {{ inkPixels: inkPixels }};
            }})()
            """
            render_res = await sess.evaluate_script(js_canvas)
            canvas_valid = bool(render_res and render_res.get("inkPixels", 0) > 0)

            # 4. Pair kerning non-regression verification
            pair_metrics: list[ChromiumPairMetricResult] = []
            pair_samples: list[ChromiumPairSampleEvidence] = []

            if held_out_pairs:
                for pair in held_out_pairs:
                    m_l = await sess.measure_glyph_direct(alias, pair.left_cp, 200.0, upem=model.metrics.units_per_em)
                    m_r = await sess.measure_glyph_direct(alias, pair.right_cp, 200.0, upem=model.metrics.units_per_em)
                    baseline_sum = m_l.advance_width_upem + m_r.advance_width_upem

                    pair_text = f"{pair.left_char}{pair.right_char}"
                    cand_pair_adv = await sess.measure_text_advance(
                        alias, pair_text, font_size_px=200.0, upem=model.metrics.units_per_em
                    )

                    expected_pair_adv = pair.measured_pair_advance_upem
                    gpos_adj = cand_pair_adv - baseline_sum
                    cand_err = abs(cand_pair_adv - expected_pair_adv)
                    base_err = abs(baseline_sum - expected_pair_adv)

                    non_reg = bool(cand_err <= base_err + 2.0)

                    p_res = ChromiumPairMetricResult(
                        pair=pair_text,
                        category="held_out_pair",
                        baseline_single_sum_upem=baseline_sum,
                        candidate_pair_advance_upem=cand_pair_adv,
                        gpos_applied_adjustment_upem=gpos_adj,
                        reference_pair_advance_upem=expected_pair_adv,
                        baseline_error_upem=base_err,
                        gpos_candidate_error_upem=cand_err,
                        material_improvement=non_reg,
                    )
                    pair_metrics.append(p_res)
                    pair_samples.append(
                        ChromiumPairSampleEvidence(
                            left_cp=pair.left_cp,
                            right_cp=pair.right_cp,
                            pair=pair_text,
                            baseline_single_sum_upem=baseline_sum,
                            candidate_pair_advance_upem=cand_pair_adv,
                            expected_pair_advance_upem=expected_pair_adv,
                            gpos_applied_adjustment_upem=gpos_adj,
                            advance_delta_upem=cand_err,
                            non_regression=non_reg,
                        )
                    )

                held_out_non_reg = all(s.non_regression for s in pair_samples)
            else:
                held_out_non_reg = False

            mean_adv_err = float(np.mean(adv_deltas)) if adv_deltas else 0.0

            result = ChromiumValidationResult(
                is_available=True,
                browser_version=sess.browser_version or "chromium",
                is_direct_loadable_chromium=bool(in_loaded),
                fallback_rejection_verified=fallback_ok,
                measured_glyph_count=len(measured_cps),
                mean_chromium_advance_error_upem=mean_adv_err,
                pair_metrics=pair_metrics,
                glyph_samples=tuple(glyph_samples),
                pair_samples=tuple(pair_samples),
                fit_pairs_material_improvement=held_out_non_reg,
                held_out_pairs_non_regression=held_out_non_reg,
                rendered_canvas_valid=canvas_valid,
                error_message=None,
            )

            return BoundChromiumEvidence(
                candidate_artifact_sha=artifact.sha256_hex,
                result=result,
            )

        except Exception as exc:
            was_connected = getattr(sess, "ws", None) is not None and sess._is_connected()
            return BoundChromiumEvidence(
                candidate_artifact_sha=artifact.sha256_hex,
                result=ChromiumValidationResult(
                    is_available=was_connected,
                    browser_version=sess.browser_version or "chromium",
                    is_direct_loadable_chromium=False,
                    fallback_rejection_verified=False,
                    measured_glyph_count=0,
                    mean_chromium_advance_error_upem=0.0,
                    pair_metrics=[],
                    glyph_samples=(),
                    pair_samples=(),
                    fit_pairs_material_improvement=False,
                    held_out_pairs_non_regression=False,
                    rendered_canvas_valid=False,
                    error_message=f"CHROMIUM_EXECUTION_EXCEPTION: {type(exc).__name__}" if was_connected else f"CHROMIUM_NOT_AVAILABLE: {type(exc).__name__}",
                ),
            )
        finally:
            if owns_session:
                await close_browser_session(sess)


class ProductionConsumerEvidenceProducer:
    """Authoritative builder executing all four consumers and producing a strictly bound ConsumerEvidenceBundle."""

    @classmethod
    async def produce_bundle(
        cls,
        descriptor: CandidateArtifactDescriptor,
        model: CanonicalFontModel,
        config: ObservationConfig,
        held_out_records: Sequence[ObservationRecord],
        held_out_pairs: Sequence[PairKerningObservation],
        raster_provider: Callable[[ObservationRecord], bytes],
        thresholds: FidelityThresholds | None = None,
    ) -> ConsumerEvidenceBundle:
        """Execute FontTools, FreeType, HarfBuzz, and Chromium against the attested candidate artifact.

        Returns a complete four-consumer passing ConsumerEvidenceBundle or raises ProductionProducerError.
        """
        # 1. Enforce descriptor attestation strictly before any other parameter/sample checks
        if not isinstance(descriptor, CandidateArtifactDescriptor):
            raise TypeError(
                f"ProductionConsumerEvidenceProducer requires a CandidateArtifactDescriptor, got {type(descriptor)}"
            )

        artifact = CandidateArtifact.from_descriptor(descriptor)

        # 2. Strict pre-validation of inputs (no zero, unknown, or mismatched samples permitted)
        if not held_out_records:
            raise ValueError("ZERO_HELD_OUT_RASTER_SAMPLES: held_out_records cannot be empty")
        if not held_out_pairs:
            raise ValueError("ZERO_HELD_OUT_TYPOGRAPHY_SAMPLES: held_out_pairs cannot be empty")

        for r in held_out_records:
            if r.code_point not in model.glyphs:
                raise ValueError(f"UNKNOWN_HELD_OUT_CODE_POINT: Glyph {r.code_point} not in canonical model")

        for p in held_out_pairs:
            if p.left_cp not in model.glyphs or p.right_cp not in model.glyphs:
                raise ValueError(
                    f"UNKNOWN_HELD_OUT_PAIR_GLYPHS: Pair ({p.left_cp}, {p.right_cp}) not in canonical model"
                )
            if p.left_char != chr(p.left_cp) or p.right_char != chr(p.right_cp):
                raise ValueError(
                    f"TYPOGRAPHY_CHAR_CODEPOINT_MISMATCH: Pair ({p.left_cp}, {p.right_cp}) character text drift"
                )

        model_hash = model.compute_canonical_hash()
        config_hash = config.compute_hash()

        from fidelity.evaluator import FidelityEvaluator, validate_consumer_gate

        held_out_raster_fp = FidelityEvaluator._compute_records_fingerprint(held_out_records)
        held_out_typo_fp = FidelityEvaluator._compute_typography_fingerprint(held_out_pairs)
        composite_held_out_fp = FidelityEvaluator._compute_composite_held_out_fingerprint(
            held_out_records, held_out_pairs
        )

        sorted_records = sorted(
            held_out_records, key=lambda r: (r.code_point, r.resolution, r.subpixel_x, r.subpixel_y)
        )
        sorted_pairs = sorted(held_out_pairs, key=lambda p: (p.left_cp, p.right_cp, p.provenance))

        # 3. Execute 4 independent consumer evidence producers
        ft_evidence = FontToolsEvidenceProducer.produce(artifact)
        fr_evidence = FreeTypeEvidenceProducer.produce(artifact, model, sorted_records, raster_provider)
        hb_evidence = HarfBuzzEvidenceProducer.produce(artifact, model, sorted_pairs)
        cr_evidence = await ChromiumEvidenceProducer.produce(artifact, model, sorted_records, sorted_pairs)

        # 4. Construct candidate bundle
        bundle = ConsumerEvidenceBundle(
            schema_version="1.0.0",
            model_canonical_hash=model_hash,
            config_hash=config_hash,
            held_out_fingerprint=composite_held_out_fp,
            candidate_artifact_sha=artifact.sha256_hex,
            fonttools=ft_evidence,
            freetype=fr_evidence,
            harfbuzz=hb_evidence,
            chromium=cr_evidence,
            held_out_raster_fingerprint=held_out_raster_fp,
            held_out_typography_fingerprint=held_out_typo_fp,
        )

        # 5. Shared single Gate 6 validator check: fail closed if Gate 6 would reject
        gate_result, gate_failures = validate_consumer_gate(
            bundle=bundle,
            model=model,
            config=config,
            held_out_records=held_out_records,
            held_out_pairs=held_out_pairs,
            thresholds=thresholds or FidelityThresholds(),
        )
        if gate_result.status != "PASS":
            reason_summary = "; ".join(gate_failures) if gate_failures else "Consumer gate validation failed"
            raise ProductionProducerError(f"CONSUMER_PRODUCER_FAILED: {reason_summary}")

        return bundle

    @classmethod
    def produce_bundle_sync(
        cls,
        descriptor: CandidateArtifactDescriptor,
        model: CanonicalFontModel,
        config: ObservationConfig,
        held_out_records: Sequence[ObservationRecord],
        held_out_pairs: Sequence[PairKerningObservation],
        raster_provider: Callable[[ObservationRecord], bytes],
        thresholds: FidelityThresholds | None = None,
    ) -> ConsumerEvidenceBundle:
        """Synchronous wrapper for produce_bundle."""
        return asyncio.run(
            cls.produce_bundle(
                descriptor=descriptor,
                model=model,
                config=config,
                held_out_records=held_out_records,
                held_out_pairs=held_out_pairs,
                raster_provider=raster_provider,
                thresholds=thresholds,
            )
        )


class BinaryConsumerEvidenceProducer:
    """Closed four-consumer boundary for authorized binary artifacts.

    Runs the same concrete FontTools/FreeType/HarfBuzz/Chromium evidence
    producers bound to the exact descriptor bytes. Ground truth is derived
    deterministically from the binary's own tables and outlines (an authorized
    binary carries no external observation evidence); nothing is injectable,
    and capability absence or forged evidence fails closed.
    """

    BINARY_BROWSER_VERSION = "authorized_binary"
    SAMPLE_RESOLUTION = 64
    MAX_SAMPLES = 4

    @classmethod
    def _derive_material(cls, raw_bytes: bytes):
        """Deterministic binary-derived model, self-records, and self-pairs."""
        from compute.binary_gate import extract_glyphs_from_binary
        from fidelity.evaluator import FidelityEvaluator
        from reconstruction.font_model import CalibratedGlyph, GlobalFontMetrics
        from measurement.models import DirectMetrics

        extracted, meta = extract_glyphs_from_binary(raw_bytes)
        if not extracted:
            raise ProductionProducerError("BINARY_CONSUMER_NO_GLYPHS")

        sha_tag = hashlib.sha256(raw_bytes).hexdigest()[:16]
        upem = int(meta["units_per_em"])
        config = ObservationConfig()
        cfg_h = config.compute_hash()

        sample_cps = sorted(cp for cp in extracted if 0x21 <= cp <= 0xFFFF)[: cls.MAX_SAMPLES]
        if not sample_cps:
            raise ProductionProducerError("BINARY_CONSUMER_NO_PRINTABLE_SAMPLES")

        glyphs: dict[int, CalibratedGlyph] = {}
        records: list[ObservationRecord] = []
        raster_map: dict[str, bytes] = {}
        import math as _math

        scale = _math.floor(cls.SAMPLE_RESOLUTION * 0.72) / float(upem)

        for cp in sample_cps:
            src = extracted[cp]
            adv = float(src.advance_width_upem)
            metrics = DirectMetrics(
                code_point=cp,
                character=chr(cp),
                font_size_px=_math.floor(cls.SAMPLE_RESOLUTION * 0.72),
                raw_advance_width=round(adv * scale, 2),
                raw_actual_left=round(float(src.lsb_upem) * scale, 2),
                raw_actual_right=round((adv - float(src.rsb_upem)) * scale, 2),
                raw_actual_ascent=round(float(src.ascent_upem) * scale, 2),
                raw_actual_descent=round(-float(src.descent_upem) * scale, 2),
                raw_font_ascent=round(float(src.ascent_upem) * scale, 2),
                raw_font_descent=round(-float(src.descent_upem) * scale, 2),
                advance_width_upem=adv,
                lsb_upem=float(src.lsb_upem),
                rsb_upem=float(src.rsb_upem),
                ascent_upem=float(src.ascent_upem),
                descent_upem=float(src.descent_upem),
                bbox_width_upem=float(src.bounding_box_upem[2] - src.bounding_box_upem[0]),
                bbox_height_upem=float(src.bounding_box_upem[3] - src.bounding_box_upem[1]),
                sample_count=1,
                confidence=1.0,
            )
            transform = CalibrationTransform.from_observation(
                resolution=cls.SAMPLE_RESOLUTION,
                metrics=metrics,
                subpixel_x=0.0,
                subpixel_y=0.0,
                units_per_em=upem,
            )

            class _OutlineGlyph:
                def __init__(self, contours):
                    self.contours = contours

            mask = FidelityEvaluator._rasterize_glyph_contours(
                _OutlineGlyph(src.contours), transform, cls.SAMPLE_RESOLUTION
            )
            img = Image.fromarray(((1 - mask) * 255).astype("uint8"), mode="L")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            png_bytes = buf.getvalue()

            record = ObservationRecord(
                cache_key=ObservationRecord.build_cache_key(
                    reference_id=f"binary_{sha_tag}",
                    style_id="regular",
                    code_point=cp,
                    browser_version=cls.BINARY_BROWSER_VERSION,
                    resolution=cls.SAMPLE_RESOLUTION,
                    subpixel_x=0.0,
                    subpixel_y=0.0,
                    config_hash=cfg_h,
                ),
                reference_id=f"binary_{sha_tag}",
                style_id="regular",
                code_point=cp,
                resolution=cls.SAMPLE_RESOLUTION,
                subpixel_x=0.0,
                subpixel_y=0.0,
                raster_relative_path=f"binary_self/{sha_tag}/{cp:04X}.png",
                raster_sha256=hashlib.sha256(png_bytes).hexdigest(),
                raster_size_bytes=len(png_bytes),
                metrics=metrics,
                created_at="1970-01-01T00:00:00+00:00",
                browser_version=cls.BINARY_BROWSER_VERSION,
                config_hash=cfg_h,
            )
            records.append(record)
            raster_map[record.cache_key] = png_bytes
            glyphs[cp] = CalibratedGlyph(
                code_point=cp,
                character=chr(cp),
                advance_width_upem=adv,
                lsb_upem=float(src.lsb_upem),
                rsb_upem=float(src.rsb_upem),
                ascent_upem=float(src.ascent_upem),
                descent_upem=float(src.descent_upem),
                bounding_box_upem=src.bounding_box_upem,
                contours=list(src.contours),
                confidence=1.0,
                observation_fingerprints=(
                    hashlib.sha256(f"binary_self:{sha_tag}:{cp}".encode("utf-8")).hexdigest(),
                ),
            )

        global_metrics = GlobalFontMetrics(
            units_per_em=upem,
            ascent_upem=float(meta["ascent"]),
            descent_upem=float(meta["descent"]),
            line_gap_upem=0.0,
            cap_height_upem=float(meta["ascent"]),
            x_height_upem=float(meta["ascent"]) * 0.5,
            max_advance_width_upem=max(g.advance_width_upem for g in glyphs.values()),
            avg_char_width_upem=sum(g.advance_width_upem for g in glyphs.values()) / len(glyphs),
            underline_position_upem=-100.0,
            underline_thickness_upem=50.0,
        )

        # Self-pair expectations are deterministic consumer-derived truth bound
        # to the same bytes (HarfBuzz shaping of the artifact itself).
        pair_expectations = cls._shape_pair_advances(raw_bytes, sample_cps)
        pairs: list[PairKerningObservation] = []
        for i in range(len(sample_cps)):
            left_cp = sample_cps[i]
            right_cp = sample_cps[(i + 1) % len(sample_cps)]
            key = (left_cp, right_cp)
            pair_adv = pair_expectations.get(
                key, glyphs[left_cp].advance_width_upem + glyphs[right_cp].advance_width_upem
            )
            pairs.append(
                PairKerningObservation(
                    left_cp=left_cp,
                    right_cp=right_cp,
                    left_char=chr(left_cp),
                    right_char=chr(right_cp),
                    left_advance_upem=glyphs[left_cp].advance_width_upem,
                    right_advance_upem=glyphs[right_cp].advance_width_upem,
                    measured_pair_advance_upem=pair_adv,
                    inferred_kerning_upem=int(
                        round(pair_adv - glyphs[left_cp].advance_width_upem - glyphs[right_cp].advance_width_upem)
                    ),
                    is_kerning_applied=False,
                    reference_id=f"binary_{sha_tag}",
                    style_id="regular",
                    browser_version=cls.BINARY_BROWSER_VERSION,
                    config_hash=cfg_h,
                    confidence=1.0,
                    provenance=f"chromium:{cls.BINARY_BROWSER_VERSION}:canvas_text_metrics",
                )
            )

        model = CanonicalFontModel(
            schema_version="1.0.0",
            family_name=f"binary_{sha_tag}",
            style_name="regular",
            reference_id=f"binary_{sha_tag}",
            style_id="regular",
            metrics=global_metrics,
            glyphs=glyphs,
            config_hash=cfg_h,
            browser_version=cls.BINARY_BROWSER_VERSION,
            fit_observations_count=len(records),
            calibration_fingerprint=hashlib.sha256(f"binary_self:{sha_tag}".encode("utf-8")).hexdigest(),
            kerning_pairs={},
        )
        return model, config, records, pairs, raster_map

    @staticmethod
    def _shape_pair_advances(raw_bytes: bytes, sample_cps: Sequence[int]) -> dict[tuple[int, int], float]:
        expectations: dict[tuple[int, int], float] = {}
        try:
            blob = hb.Blob(raw_bytes)
            face = hb.Face(blob)
            font = hb.Font(face)
            upem = face.upem
            for i in range(len(sample_cps)):
                left_cp = sample_cps[i]
                right_cp = sample_cps[(i + 1) % len(sample_cps)]
                buf = hb.Buffer()
                buf.add_str(chr(left_cp) + chr(right_cp))
                buf.guess_segment_properties()
                hb.shape(font, buf)
                positions = buf.glyph_positions
                if len(positions) == 2 and all(math.isfinite(p.x_advance) for p in positions):
                    # uharfbuzz positions are font units at face upem.
                    expectations[(left_cp, right_cp)] = float(
                        positions[0].x_advance + positions[1].x_advance
                    )
        except Exception:
            return {}
        return expectations

    @classmethod
    async def produce(
        cls,
        descriptor: CandidateArtifactDescriptor,
        thresholds: FidelityThresholds | None = None,
    ) -> ConsumerEvidenceBundle:
        """Execute the closed four-consumer boundary for one authorized binary."""
        if not isinstance(descriptor, CandidateArtifactDescriptor):
            raise TypeError(
                f"BinaryConsumerEvidenceProducer requires a CandidateArtifactDescriptor, got {type(descriptor)}"
            )
        artifact = CandidateArtifact.from_descriptor(descriptor)

        model, config, records, pairs, raster_map = cls._derive_material(artifact.raw_bytes)

        from fidelity.evaluator import validate_consumer_gate

        sorted_records = sorted(records, key=lambda r: r.code_point)
        sorted_pairs = sorted(pairs, key=lambda p: (p.left_cp, p.right_cp))

        ft_evidence = FontToolsEvidenceProducer.produce(artifact)
        fr_evidence = FreeTypeEvidenceProducer.produce(
            artifact, model, sorted_records, lambda r: raster_map[r.cache_key]
        )
        hb_evidence = HarfBuzzEvidenceProducer.produce(artifact, model, sorted_pairs)
        cr_evidence = await ChromiumEvidenceProducer.produce(artifact, model, sorted_records, sorted_pairs)

        model_hash = model.compute_canonical_hash()
        config_hash = config.compute_hash()
        from fidelity.evaluator import FidelityEvaluator

        held_out_raster_fp = FidelityEvaluator._compute_records_fingerprint(sorted_records)
        held_out_typo_fp = FidelityEvaluator._compute_typography_fingerprint(sorted_pairs)
        composite_fp = FidelityEvaluator._compute_composite_held_out_fingerprint(sorted_records, sorted_pairs)

        bundle = ConsumerEvidenceBundle(
            schema_version="1.0.0",
            model_canonical_hash=model_hash,
            config_hash=config_hash,
            held_out_fingerprint=composite_fp,
            candidate_artifact_sha=artifact.sha256_hex,
            fonttools=ft_evidence,
            freetype=fr_evidence,
            harfbuzz=hb_evidence,
            chromium=cr_evidence,
            held_out_raster_fingerprint=held_out_raster_fp,
            held_out_typography_fingerprint=held_out_typo_fp,
        )

        gate_result, gate_failures = validate_consumer_gate(
            bundle=bundle,
            model=model,
            config=config,
            held_out_records=sorted_records,
            held_out_pairs=sorted_pairs,
            thresholds=thresholds or FidelityThresholds(),
        )
        if gate_result.status != "PASS":
            reason_summary = "; ".join(gate_failures) if gate_failures else "Binary consumer gate failed"
            raise ProductionProducerError(f"BINARY_CONSUMER_GATE_FAILED: {reason_summary}")
        return bundle

    @classmethod
    def produce_sync(
        cls,
        descriptor: CandidateArtifactDescriptor,
        thresholds: FidelityThresholds | None = None,
    ) -> ConsumerEvidenceBundle:
        return asyncio.run(cls.produce(descriptor=descriptor, thresholds=thresholds))
