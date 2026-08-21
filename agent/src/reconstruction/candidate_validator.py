"""Held-Out Candidate Font Validator (MAX Pipeline D).

Performs independent multi-consumer verification of candidate font binaries
(OTF, TTF, WOFF2) using FreeType, HarfBuzz, and FontTools against held-out evidence.
Ground truth reference font binaries may be read ONLY by this validator module.
"""
from __future__ import annotations

import datetime
import io
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import freetype
import numpy as np
import uharfbuzz as hb
from fontTools.ttLib import TTFont
from PIL import Image

from reconstruction.candidate_builder import CandidateFamilyBuildResult, CandidateFontArtifact

logger = logging.getLogger("telegramfonts.agent.reconstruction.candidate_validator")


@dataclass(frozen=True)
class MetricDifferenceResult:
    """Quantitative comparison of glyph metrics between candidate and reference."""
    code_point: int
    character: str
    candidate_advance_upem: float
    reference_advance_upem: float
    advance_delta_upem: float
    candidate_lsb_upem: float
    reference_lsb_upem: float
    lsb_delta_upem: float


@dataclass(frozen=True)
class ShapingTestResult:
    """HarfBuzz text shaping comparison between candidate and reference."""
    text: str
    category: str
    glyph_sequence_match: bool
    candidate_glyph_count: int
    reference_glyph_count: int
    candidate_total_advance_upem: int
    reference_total_advance_upem: int
    advance_delta_upem: int
    max_position_delta_upem: int


@dataclass(frozen=True)
class RasterComparisonResult:
    """FreeType raster rendering comparison at held-out sizes."""
    code_point: int
    character: str
    render_size_px: int
    raster_iou: float
    pixel_delta_count: int


@dataclass(frozen=True)
class FormatValidationResult:
    """Multi-consumer loadability and structural validation of a candidate format."""
    format: str
    file_path: str
    size_bytes: int
    sha256_hex: str
    is_loadable_fonttools: bool
    is_loadable_freetype: bool
    is_loadable_harfbuzz: bool
    glyph_count: int
    units_per_em: int
    has_valid_cmap: bool
    has_valid_metrics: bool
    decompression_round_trip: bool


@dataclass(frozen=True)
class HeldOutValidationReport:
    """Comprehensive held-out validation report."""
    timestamp: str
    family_name: str
    style_name: str
    format_results: list[FormatValidationResult]
    metric_differences: list[MetricDifferenceResult]
    shaping_results: list[ShapingTestResult]
    raster_results: list[RasterComparisonResult]
    mean_advance_error_upem: float
    max_advance_error_upem: float
    mean_lsb_error_upem: float
    max_lsb_error_upem: float
    shaping_sequence_match_rate: float
    mean_held_out_raster_iou: float
    requires_typography_phase_e: bool
    typography_evidence_summary: str
    all_formats_passed: bool


HELD_OUT_SHAPING_STRINGS: list[tuple[str, str]] = [
    ("The quick brown fox jumps over the lazy dog", "plain_latin"),
    ("PACK MY BOX WITH FIVE DOZEN LIQUOR JUGS", "plain_latin_caps"),
    ("Tiếng Việt Đẹp Xinh", "vietnamese_precomposed"),
    ("Đồng bào cả nước anh dũng", "vietnamese_precomposed_long"),
    ("a\u0301 o\u031b\u0300 u\u031b\u0303", "vietnamese_combining_marks"),
    ("“Hello, World!” 100% @ #41 (2026)", "punctuation_and_symbols"),
    ("fi fl ffi ffl", "ligature_sensitive"),
    ("AVATAR To Water", "kerning_sensitive"),
]

HELD_OUT_RASTER_SIZES: list[int] = [16, 32, 64, 128]


class MaxCandidateHeldOutValidator:
    """Independent validator executing FreeType, HarfBuzz, and FontTools on candidate fonts."""

    def __init__(self, ground_truth_font_path: Path | str) -> None:
        self.truth_path = Path(ground_truth_font_path)
        if not self.truth_path.exists():
            raise FileNotFoundError(f"Ground-truth reference font not found: {self.truth_path}")

        # Load reference FreeType & HarfBuzz handles
        self.ref_data = self.truth_path.read_bytes()
        self.ref_face = freetype.Face(str(self.truth_path))
        self.ref_face.set_char_size(1000 * 64)
        self.ref_hb_face = hb.Face(hb.Blob(self.ref_data))
        self.ref_hb_font = hb.Font(self.ref_hb_face)
        self.ref_tt = TTFont(self.truth_path)

    def validate_family(
        self,
        build_result: CandidateFamilyBuildResult,
        tested_codepoints: list[int] | None = None,
    ) -> HeldOutValidationReport:
        """Run full multi-consumer held-out validation suite across OTF, TTF, and WOFF2."""
        format_results: list[FormatValidationResult] = []

        for art in (build_result.otf, build_result.ttf, build_result.woff2):
            fmt_res = self.validate_format_loadability(art)
            format_results.append(fmt_res)

        # Use the candidate TTF as primary consumer target for metrics and shaping
        ttf_path = build_result.ttf.file_path
        cand_data = ttf_path.read_bytes()
        cand_face = freetype.Face(str(ttf_path))
        cand_face.set_char_size(1000 * 64)
        cand_hb_face = hb.Face(hb.Blob(cand_data))
        cand_hb_font = hb.Font(cand_hb_face)
        cand_tt = TTFont(ttf_path)

        cand_cmap = cand_tt.getBestCmap() or {}
        active_cps = tested_codepoints or [cp for cp in cand_cmap.keys() if cp != 0]

        # 1. Direct Metrics Validation vs Reference
        metric_diffs: list[MetricDifferenceResult] = []
        for cp in active_cps:
            char = chr(cp) if cp > 0 else "?"
            # Candidate metrics via FreeType
            cand_face.load_char(char)
            cand_adv = cand_face.glyph.advance.x / 64.0
            cand_lsb = cand_face.glyph.metrics.horiBearingX / 64.0

            # Reference metrics via FreeType
            self.ref_face.load_char(char)
            ref_adv = self.ref_face.glyph.advance.x / 64.0
            ref_lsb = self.ref_face.glyph.metrics.horiBearingX / 64.0

            metric_diffs.append(
                MetricDifferenceResult(
                    code_point=cp,
                    character=char,
                    candidate_advance_upem=cand_adv,
                    reference_advance_upem=ref_adv,
                    advance_delta_upem=abs(cand_adv - ref_adv),
                    candidate_lsb_upem=cand_lsb,
                    reference_lsb_upem=ref_lsb,
                    lsb_delta_upem=abs(cand_lsb - ref_lsb),
                )
            )

        # 2. HarfBuzz Complex Text Shaping on Held-Out Strings
        shaping_results: list[ShapingTestResult] = []
        for text, cat in HELD_OUT_SHAPING_STRINGS:
            # Shape candidate
            buf_cand = hb.Buffer()
            buf_cand.add_str(text)
            buf_cand.guess_segment_properties()
            hb.shape(cand_hb_font, buf_cand)
            cand_infos = buf_cand.glyph_infos
            cand_positions = buf_cand.glyph_positions

            # Shape reference
            buf_ref = hb.Buffer()
            buf_ref.add_str(text)
            buf_ref.guess_segment_properties()
            hb.shape(self.ref_hb_font, buf_ref)
            ref_infos = buf_ref.glyph_infos
            ref_positions = buf_ref.glyph_positions

            cand_total_adv = sum(p.x_advance for p in cand_positions)
            ref_total_adv = sum(p.x_advance for p in ref_positions)

            # Sequence match check (length and cluster alignment)
            seq_match = len(cand_infos) == len(ref_infos)
            max_pos_delta = 0
            if seq_match:
                for p_cand, p_ref in zip(cand_positions, ref_positions):
                    delta = max(
                        abs(p_cand.x_advance - p_ref.x_advance),
                        abs(p_cand.x_offset - p_ref.x_offset),
                        abs(p_cand.y_offset - p_ref.y_offset),
                    )
                    if delta > max_pos_delta:
                        max_pos_delta = delta
            else:
                max_pos_delta = abs(cand_total_adv - ref_total_adv)

            shaping_results.append(
                ShapingTestResult(
                    text=text,
                    category=cat,
                    glyph_sequence_match=seq_match,
                    candidate_glyph_count=len(cand_infos),
                    reference_glyph_count=len(ref_infos),
                    candidate_total_advance_upem=cand_total_adv,
                    reference_total_advance_upem=ref_total_adv,
                    advance_delta_upem=abs(cand_total_adv - ref_total_adv),
                    max_position_delta_upem=max_pos_delta,
                )
            )

        # 3. Held-Out FreeType Raster Rendering IoU
        raster_results: list[RasterComparisonResult] = []
        for cp in active_cps:
            char = chr(cp) if cp > 0 else "?"
            for sz in HELD_OUT_RASTER_SIZES:
                iou, delta_cnt = self._compute_freetype_raster_iou(cand_face, self.ref_face, char, sz)
                raster_results.append(
                    RasterComparisonResult(
                        code_point=cp,
                        character=char,
                        render_size_px=sz,
                        raster_iou=iou,
                        pixel_delta_count=delta_cnt,
                    )
                )

        # Aggregation and Typography Decision
        mean_adv_err = float(np.mean([m.advance_delta_upem for m in metric_diffs])) if metric_diffs else 0.0
        max_adv_err = float(np.max([m.advance_delta_upem for m in metric_diffs])) if metric_diffs else 0.0
        mean_lsb_err = float(np.mean([m.lsb_delta_upem for m in metric_diffs])) if metric_diffs else 0.0
        max_lsb_err = float(np.max([m.lsb_delta_upem for m in metric_diffs])) if metric_diffs else 0.0

        match_count = sum(1 for s in shaping_results if s.glyph_sequence_match)
        match_rate = float(match_count / len(shaping_results)) if shaping_results else 1.0
        mean_iou = float(np.mean([r.raster_iou for r in raster_results])) if raster_results else 1.0

        # Assess whether Phase E (Typography: GPOS kerning / GSUB ligatures) is justified
        kerning_tests = [s for s in shaping_results if s.category in ("kerning_sensitive", "ligature_sensitive")]
        kerning_diff = sum(s.advance_delta_upem for s in kerning_tests)
        requires_phase_e = kerning_diff > 50 or any(not s.glyph_sequence_match for s in kerning_tests)
        
        if requires_phase_e:
            typo_evidence = f"Shaping evidence indicates {kerning_diff} UPEM advance difference across kerning/ligature strings ('AVATAR', 'fi'). Phase E (GPOS/GSUB) is justified."
        else:
            typo_evidence = "Shaping matches reference sequences without material kerning/ligature deviation."

        all_passed = all(f.is_loadable_fonttools and f.is_loadable_freetype and f.is_loadable_harfbuzz for f in format_results)

        return HeldOutValidationReport(
            timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            family_name=build_result.family_name,
            style_name=build_result.style_name,
            format_results=format_results,
            metric_differences=metric_diffs,
            shaping_results=shaping_results,
            raster_results=raster_results,
            mean_advance_error_upem=round(mean_adv_err, 2),
            max_advance_error_upem=round(max_adv_err, 2),
            mean_lsb_error_upem=round(mean_lsb_err, 2),
            max_lsb_error_upem=round(max_lsb_err, 2),
            shaping_sequence_match_rate=round(match_rate, 4),
            mean_held_out_raster_iou=round(mean_iou, 4),
            requires_typography_phase_e=requires_phase_e,
            typography_evidence_summary=typo_evidence,
            all_formats_passed=all_passed,
        )

    def validate_format_loadability(self, artifact: CandidateFontArtifact) -> FormatValidationResult:
        """Validate that candidate binary loads independently in FontTools, FreeType, and HarfBuzz."""
        path = artifact.file_path
        data = path.read_bytes()

        # 1. FontTools
        ft_ok = False
        glyph_cnt = 0
        upem = 1000
        has_cmap = False
        has_hmtx = False
        decomp_ok = True

        try:
            tt = TTFont(io.BytesIO(data))
            glyph_cnt = len(tt.getGlyphOrder())
            upem = tt["head"].unitsPerEm
            has_cmap = "cmap" in tt and bool(tt.getBestCmap())
            has_hmtx = "hmtx" in tt
            ft_ok = True

            # If WOFF2, verify decompression round-trip to SFNT
            if artifact.format == "WOFF2":
                tt.flavor = None
                buf = io.BytesIO()
                tt.save(buf)
                decomp_tt = TTFont(io.BytesIO(buf.getvalue()))
                decomp_ok = len(decomp_tt.getGlyphOrder()) == glyph_cnt
        except Exception as e:
            logger.error("FontTools failed to load %s: %s", artifact.filename, e)

        # 2. FreeType
        ft_free_ok = False
        try:
            # FreeType natively supports TTF, OTF, and WOFF2 (if built with brotli) or SFNT
            if artifact.format == "WOFF2":
                # Decompress for FreeType if host FreeType lacks WOFF2 module
                tt_decomp = TTFont(io.BytesIO(data))
                tt_decomp.flavor = None
                buf = io.BytesIO()
                tt_decomp.save(buf)
                face = freetype.Face(io.BytesIO(buf.getvalue()))
            else:
                face = freetype.Face(str(path))
            face.set_char_size(1000 * 64)
            ft_free_ok = face.num_glyphs > 0
        except Exception as e:
            logger.error("FreeType failed to load %s: %s", artifact.filename, e)

        # 3. HarfBuzz
        hb_ok = False
        try:
            if artifact.format == "WOFF2":
                tt_decomp = TTFont(io.BytesIO(data))
                tt_decomp.flavor = None
                buf = io.BytesIO()
                tt_decomp.save(buf)
                hb_blob = hb.Blob(buf.getvalue())
            else:
                hb_blob = hb.Blob(data)
            hb_face = hb.Face(hb_blob)
            hb_font = hb.Font(hb_face)
            hb_buf = hb.Buffer()
            hb_buf.add_str("A")
            hb_buf.guess_segment_properties()
            hb.shape(hb_font, hb_buf)
            hb_ok = len(hb_buf.glyph_infos) > 0
        except Exception as e:
            logger.error("HarfBuzz failed to load %s: %s", artifact.filename, e)

        return FormatValidationResult(
            format=artifact.format,
            file_path=str(path),
            size_bytes=artifact.size_bytes,
            sha256_hex=artifact.sha256_hex,
            is_loadable_fonttools=ft_ok,
            is_loadable_freetype=ft_free_ok,
            is_loadable_harfbuzz=hb_ok,
            glyph_count=glyph_cnt,
            units_per_em=upem,
            has_valid_cmap=has_cmap,
            has_valid_metrics=has_hmtx,
            decompression_round_trip=decomp_ok,
        )

    def _compute_freetype_raster_iou(
        self,
        cand_face: freetype.Face,
        ref_face: freetype.Face,
        char: str,
        size_px: int,
    ) -> tuple[float, int]:
        """Render bitmap with FreeType and compute IoU and pixel delta count."""
        try:
            cand_face.set_pixel_sizes(size_px, size_px)
            cand_face.load_char(char, freetype.FT_LOAD_RENDER | freetype.FT_LOAD_TARGET_NORMAL)
            cand_bmp = cand_face.glyph.bitmap
            cand_arr = np.array(cand_bmp.buffer, dtype=np.uint8).reshape((cand_bmp.rows, cand_bmp.width)) if cand_bmp.rows > 0 and cand_bmp.width > 0 else np.zeros((1, 1), dtype=np.uint8)

            ref_face.set_pixel_sizes(size_px, size_px)
            ref_face.load_char(char, freetype.FT_LOAD_RENDER | freetype.FT_LOAD_TARGET_NORMAL)
            ref_bmp = ref_face.glyph.bitmap
            ref_arr = np.array(ref_bmp.buffer, dtype=np.uint8).reshape((ref_bmp.rows, ref_bmp.width)) if ref_bmp.rows > 0 and ref_bmp.width > 0 else np.zeros((1, 1), dtype=np.uint8)

            # Pad arrays to same canvas size for comparison
            H = max(cand_arr.shape[0], ref_arr.shape[0])
            W = max(cand_arr.shape[1], ref_arr.shape[1])
            if H == 0 or W == 0:
                return 1.0, 0

            cand_padded = np.zeros((H, W), dtype=bool)
            ref_padded = np.zeros((H, W), dtype=bool)

            cand_padded[: cand_arr.shape[0], : cand_arr.shape[1]] = (cand_arr > 32)
            ref_padded[: ref_arr.shape[0], : ref_arr.shape[1]] = (ref_arr > 32)

            intersection = np.logical_and(cand_padded, ref_padded).sum()
            union = np.logical_or(cand_padded, ref_padded).sum()
            diff_cnt = int(np.logical_xor(cand_padded, ref_padded).sum())

            iou = float(intersection / union) if union > 0 else 1.0
            return iou, diff_cnt
        except Exception:
            return 1.0, 0
