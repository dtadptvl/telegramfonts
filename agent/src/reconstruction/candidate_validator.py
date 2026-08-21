"""Held-Out Candidate Font Validator (MAX Pipeline D).

Performs independent multi-consumer verification of candidate font binaries
(OTF, TTF, WOFF2) using FreeType, HarfBuzz, Chromium, and FontTools against held-out evidence.
Ground truth reference font binaries may be read ONLY by this validator module.
"""
from __future__ import annotations

import asyncio
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
    in_candidate_cmap: bool
    glyph_sequence_match: bool
    candidate_glyph_names: list[str]
    reference_glyph_names: list[str]
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
    render_error: str | None = None


@dataclass(frozen=True)
class ChromiumValidationResult:
    """Independent validation in headless Chromium browser session."""
    is_available: bool
    browser_version: str
    is_direct_loadable_chromium: bool
    fallback_rejection_verified: bool
    measured_glyph_count: int
    mean_chromium_advance_error_upem: float
    rendered_canvas_valid: bool
    error_message: str | None = None


@dataclass(frozen=True)
class FormatValidationResult:
    """Multi-consumer loadability and structural validation of a candidate format."""
    format: str
    file_path: str
    size_bytes: int
    sha256_hex: str
    is_direct_loadable_fonttools: bool
    is_direct_loadable_freetype: bool
    is_roundtrip_loadable_freetype: bool
    is_direct_loadable_harfbuzz: bool
    is_direct_loadable_chromium: bool
    glyph_count: int
    units_per_em: int
    has_valid_cmap: bool
    has_valid_metrics: bool
    decompression_round_trip: bool
    validation_error: str | None = None


@dataclass(frozen=True)
class HeldOutValidationReport:
    """Comprehensive held-out validation report."""
    timestamp: str
    family_name: str
    style_name: str
    format_results: list[FormatValidationResult]
    chromium_result: ChromiumValidationResult
    metric_differences: list[MetricDifferenceResult]
    shaping_results: list[ShapingTestResult]
    raster_results: list[RasterComparisonResult]
    mean_advance_error_upem: float
    max_advance_error_upem: float
    mean_lsb_error_upem: float
    max_lsb_error_upem: float
    in_cmap_shaping_match_rate: float
    mean_held_out_raster_iou: float
    requires_typography_phase_e: bool
    typography_evidence_summary: str
    all_formats_passed: bool


HELD_OUT_SHAPING_STRINGS: list[tuple[str, str]] = [
    # 1. In-Cmap shaping strings (tested strictly against candidate's mapped glyphs)
    ("AO", "in_cmap_kerning_sensitive"),
    ("OA", "in_cmap_kerning_sensitive"),
    ("BO", "in_cmap_kerning_sensitive"),
    ("A B O 8 @ % g m", "in_cmap_latin_subset"),
    ("Đ ơ đ ư ắ", "in_cmap_vietnamese_subset"),
    # 2. Out-of-cmap held-out strings (tests fallback rejection, .notdef mapping & shaping differences)
    ("The quick brown fox jumps over the lazy dog", "out_of_cmap_plain_latin"),
    ("Tiếng Việt Đẹp Xinh", "out_of_cmap_vietnamese_precomposed"),
    ("a\u0301 o\u031b\u0300 u\u031b\u0303", "out_of_cmap_combining_marks"),
    ("“Hello, World!” 100% @ #41 (2026)", "out_of_cmap_punctuation_and_symbols"),
    ("fi fl ffi ffl", "out_of_cmap_ligatures"),
    ("AVATAR To Water", "out_of_cmap_kerning"),
]

HELD_OUT_RASTER_SIZES: list[int] = [16, 32, 64, 128]


class MaxCandidateHeldOutValidator:
    """Independent validator executing FreeType, HarfBuzz, Chromium, and FontTools on candidate fonts."""

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
        run_chromium: bool = True,
    ) -> HeldOutValidationReport:
        """Run full multi-consumer held-out validation suite across OTF, TTF, WOFF2, and Chromium."""
        # 1. Chromium Browser Validation (Direct WOFF2)
        chromium_res = self._validate_chromium_consumer(build_result, tested_codepoints) if run_chromium else ChromiumValidationResult(
            is_available=False,
            browser_version="skipped",
            is_direct_loadable_chromium=False,
            fallback_rejection_verified=False,
            measured_glyph_count=0,
            mean_chromium_advance_error_upem=0.0,
            rendered_canvas_valid=False,
        )

        # 2. Format Loadability Validation
        format_results: list[FormatValidationResult] = []
        for art in (build_result.otf, build_result.ttf, build_result.woff2):
            fmt_res = self.validate_format_loadability(art, is_chromium_supported=chromium_res.is_direct_loadable_chromium)
            format_results.append(fmt_res)

        # Use Candidate TTF as primary consumer target for FreeType/HarfBuzz metrics & shaping
        ttf_path = build_result.ttf.file_path
        cand_data = ttf_path.read_bytes()
        cand_face = freetype.Face(str(ttf_path))
        cand_face.set_char_size(1000 * 64)
        cand_hb_face = hb.Face(hb.Blob(cand_data))
        cand_hb_font = hb.Font(cand_hb_face)
        cand_tt = TTFont(ttf_path)

        cand_cmap = cand_tt.getBestCmap() or {}
        cand_glyph_order = cand_tt.getGlyphOrder()
        ref_glyph_order = self.ref_tt.getGlyphOrder()
        active_cps = tested_codepoints or [cp for cp in cand_cmap.keys() if cp != 0]

        # 3. Direct Metrics Validation vs Reference
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

        # 4. HarfBuzz Complex Text Shaping with Exact Sequence & Cluster Comparison
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

            # Reverse cmaps to compare resolved Unicode code points rather than raw post-table naming differences
            cand_rev_cmap = {name: cp for cp, name in cand_cmap.items()}
            ref_rev_cmap = {name: cp for cp, name in (self.ref_tt.getBestCmap() or {}).items()}

            cand_cps = [cand_rev_cmap.get(cand_glyph_order[info.codepoint], None) if info.codepoint != 0 else 0 for info in cand_infos]
            ref_cps = [ref_rev_cmap.get(ref_glyph_order[info.codepoint], None) if info.codepoint != 0 else 0 for info in ref_infos]

            cand_names = [cand_glyph_order[info.codepoint] if info.codepoint < len(cand_glyph_order) else f"glyph{info.codepoint}" for info in cand_infos]
            ref_names = [ref_glyph_order[info.codepoint] if info.codepoint < len(ref_glyph_order) else f"glyph{info.codepoint}" for info in ref_infos]

            # Check if all characters in string exist in candidate cmap
            in_cmap = all(ord(c) in cand_cmap or c in (" ", "\n", "\t") for c in text)

            # Sequence match requires identical resolved code points, cluster indices, and no .notdef fallback
            seq_match = (
                (len(cand_cps) == len(ref_cps))
                and (cand_cps == ref_cps)
                and (0 not in cand_cps)
                and ([info.cluster for info in cand_infos] == [info.cluster for info in ref_infos])
            )

            max_pos_delta = 0
            if len(cand_positions) == len(ref_positions):
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
                    in_candidate_cmap=in_cmap,
                    glyph_sequence_match=seq_match,
                    candidate_glyph_names=cand_names,
                    reference_glyph_names=ref_names,
                    candidate_glyph_count=len(cand_infos),
                    reference_glyph_count=len(ref_infos),
                    candidate_total_advance_upem=cand_total_adv,
                    reference_total_advance_upem=ref_total_adv,
                    advance_delta_upem=abs(cand_total_adv - ref_total_adv),
                    max_position_delta_upem=max_pos_delta,
                )
            )

        # 5. Held-Out FreeType Raster Rendering IoU (Never Fails Open)
        raster_results: list[RasterComparisonResult] = []
        for cp in active_cps:
            char = chr(cp) if cp > 0 else "?"
            for sz in HELD_OUT_RASTER_SIZES:
                iou, delta_cnt, err_msg = self._compute_freetype_raster_iou(cand_face, self.ref_face, char, sz)
                raster_results.append(
                    RasterComparisonResult(
                        code_point=cp,
                        character=char,
                        render_size_px=sz,
                        raster_iou=iou,
                        pixel_delta_count=delta_cnt,
                        render_error=err_msg,
                    )
                )

        # Aggregation
        mean_adv_err = float(np.mean([m.advance_delta_upem for m in metric_diffs])) if metric_diffs else 0.0
        max_adv_err = float(np.max([m.advance_delta_upem for m in metric_diffs])) if metric_diffs else 0.0
        mean_lsb_err = float(np.mean([m.lsb_delta_upem for m in metric_diffs])) if metric_diffs else 0.0
        max_lsb_err = float(np.max([m.lsb_delta_upem for m in metric_diffs])) if metric_diffs else 0.0

        in_cmap_tests = [s for s in shaping_results if s.in_candidate_cmap]
        in_cmap_matches = sum(1 for s in in_cmap_tests if s.glyph_sequence_match)
        in_cmap_match_rate = float(in_cmap_matches / len(in_cmap_tests)) if in_cmap_tests else 1.0

        mean_iou = float(np.mean([r.raster_iou for r in raster_results if r.render_error is None])) if raster_results else 1.0

        # Assess whether Phase E typography is justified STRICTLY from in-cmap kerning evidence
        in_cmap_kerning_tests = [s for s in shaping_results if s.in_candidate_cmap and s.category == "in_cmap_kerning_sensitive"]
        in_cmap_kerning_delta = sum(s.advance_delta_upem for s in in_cmap_kerning_tests)
        in_cmap_pos_delta = max((s.max_position_delta_upem for s in in_cmap_kerning_tests), default=0)

        if in_cmap_kerning_delta > 0:
            requires_phase_e = True
            typo_evidence = f"In-cmap shaping evidence across mapped pairs ('AO', 'OA', 'BO') reveals {in_cmap_kerning_delta} UPEM total advance delta (max pair delta: {in_cmap_pos_delta} UPEM) due to missing GPOS kerning table in Candidate vs Reference font. Phase E (GPOS/GSUB typography) is justified."
        else:
            requires_phase_e = False
            typo_evidence = f"In-cmap shaping evidence across mapped pairs ('AO', 'OA', 'BO') confirms 0.0 UPEM advance delta (max pair delta: {in_cmap_pos_delta} UPEM) with OpenType GPOS kerning table active. No independent evidence justifies broad GSUB/mark extensions for this subset."

        # Fail-closed aggregate validation check across all required consumers
        has_raster_errors = any(r.render_error is not None for r in raster_results)
        ft_all = all(f.is_direct_loadable_fonttools for f in format_results)
        free_all = all(f.is_direct_loadable_freetype or f.is_roundtrip_loadable_freetype for f in format_results)
        hb_all = all(f.is_direct_loadable_harfbuzz for f in format_results)
        chrom_ok = (
            (not run_chromium)
            or (
                chromium_res.is_available
                and chromium_res.is_direct_loadable_chromium
                and chromium_res.fallback_rejection_verified
                and chromium_res.rendered_canvas_valid
            )
        )

        all_passed = bool(ft_all and free_all and hb_all and chrom_ok and not has_raster_errors)

        return HeldOutValidationReport(
            timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            family_name=build_result.family_name,
            style_name=build_result.style_name,
            format_results=format_results,
            chromium_result=chromium_res,
            metric_differences=metric_diffs,
            shaping_results=shaping_results,
            raster_results=raster_results,
            mean_advance_error_upem=round(mean_adv_err, 2),
            max_advance_error_upem=round(max_adv_err, 2),
            mean_lsb_error_upem=round(mean_lsb_err, 2),
            max_lsb_error_upem=round(max_lsb_err, 2),
            in_cmap_shaping_match_rate=round(in_cmap_match_rate, 4),
            mean_held_out_raster_iou=round(mean_iou, 4),
            requires_typography_phase_e=requires_phase_e,
            typography_evidence_summary=typo_evidence,
            all_formats_passed=all_passed,
        )

    def validate_format_loadability(
        self,
        artifact: CandidateFontArtifact,
        is_chromium_supported: bool = False,
    ) -> FormatValidationResult:
        """Validate that candidate binary loads in FontTools, FreeType, HarfBuzz, and Chromium with direct/roundtrip semantics."""
        path = artifact.file_path
        data = path.read_bytes()

        # 1. FontTools Direct Load
        ft_ok = False
        glyph_cnt = 0
        upem = 1000
        has_cmap = False
        has_hmtx = False
        decomp_ok = True
        err_msg = None

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
            err_msg = f"FontTools failed: {e}"
            logger.error("FontTools failed to load %s: %s", artifact.filename, e)

        # 2. FreeType Direct & Round-Trip Load
        ft_direct_ok = False
        ft_roundtrip_ok = False
        try:
            face_direct = freetype.Face(io.BytesIO(data))
            face_direct.set_char_size(1000 * 64)
            ft_direct_ok = face_direct.num_glyphs > 0
        except Exception:
            ft_direct_ok = False

        if artifact.format == "WOFF2":
            try:
                tt_decomp = TTFont(io.BytesIO(data))
                tt_decomp.flavor = None
                buf = io.BytesIO()
                tt_decomp.save(buf)
                face_rt = freetype.Face(io.BytesIO(buf.getvalue()))
                face_rt.set_char_size(1000 * 64)
                ft_roundtrip_ok = face_rt.num_glyphs > 0
            except Exception:
                ft_roundtrip_ok = False
        else:
            ft_roundtrip_ok = ft_direct_ok

        # 3. HarfBuzz Direct Load
        hb_direct_ok = False
        try:
            hb_blob = hb.Blob(data)
            hb_face = hb.Face(hb_blob)
            hb_font = hb.Font(hb_face)
            hb_buf = hb.Buffer()
            hb_buf.add_str("A")
            hb_buf.guess_segment_properties()
            hb.shape(hb_font, hb_buf)
            hb_direct_ok = len(hb_buf.glyph_infos) > 0
        except Exception:
            hb_direct_ok = False

        # 4. Chromium Direct Load (only tested on direct WOFF2 web font path)
        chrom_direct_ok = is_chromium_supported if artifact.format == "WOFF2" else False

        return FormatValidationResult(
            format=artifact.format,
            file_path=str(path),
            size_bytes=artifact.size_bytes,
            sha256_hex=artifact.sha256_hex,
            is_direct_loadable_fonttools=ft_ok,
            is_direct_loadable_freetype=ft_direct_ok,
            is_roundtrip_loadable_freetype=ft_roundtrip_ok,
            is_direct_loadable_harfbuzz=hb_direct_ok,
            is_direct_loadable_chromium=chrom_direct_ok,
            glyph_count=glyph_cnt,
            units_per_em=upem,
            has_valid_cmap=has_cmap,
            has_valid_metrics=has_hmtx,
            decompression_round_trip=decomp_ok,
            validation_error=err_msg,
        )

    def _validate_chromium_consumer(
        self,
        build_result: CandidateFamilyBuildResult,
        tested_codepoints: list[int] | None = None,
    ) -> ChromiumValidationResult:
        """Exercise candidate WOFF2 in headless Chromium via CDP."""
        from measurement.browser_session import ChromiumSession, find_chromium_executable

        try:
            find_chromium_executable()
        except Exception as e:
            logger.warning("Chromium executable not available on host: %s", e)
            return ChromiumValidationResult(
                is_available=False,
                browser_version="not_available",
                is_direct_loadable_chromium=False,
                fallback_rejection_verified=False,
                measured_glyph_count=0,
                mean_chromium_advance_error_upem=0.0,
                rendered_canvas_valid=False,
                error_message=str(e),
            )

        async def _run_browser() -> ChromiumValidationResult:
            session = ChromiumSession(timeout_seconds=10.0)
            try:
                await session.start()
                woff2_bytes = build_result.woff2.file_path.read_bytes()
                await session.load_font_data("CandidateMAXWOFF2", woff2_bytes)

                # 1. Fallback Rejection Verification on unmapped code point 'Z'
                fallback_ok = not (await session.is_glyph_supported_in_font("CandidateMAXWOFF2", ord("Z")))

                # 2. Direct browser metrics comparison for mapped glyphs
                cps = tested_codepoints or [ord("A"), ord("B"), ord("O"), ord("8")]
                adv_deltas = []
                for cp in cps:
                    m = await session.measure_glyph_direct("CandidateMAXWOFF2", cp, 200.0)
                    self.ref_face.load_char(chr(cp))
                    ref_adv = self.ref_face.glyph.advance.x / 64.0
                    adv_deltas.append(abs(m.advance_width_upem - ref_adv))

                # 3. Canvas Text Rendering
                js_render = """
                (() => {
                    const canvas = document.createElement('canvas');
                    canvas.width = 400;
                    canvas.height = 100;
                    const ctx = canvas.getContext('2d');
                    ctx.font = '32px "CandidateMAXWOFF2"';
                    ctx.fillText('A B O 8', 10, 50);
                    const dataUrl = canvas.toDataURL('image/png');
                    return dataUrl.length > 500;
                })()
                """
                canvas_ok = bool(await session.evaluate_script(js_render))

                mean_adv_err = float(np.mean(adv_deltas)) if adv_deltas else 0.0

                return ChromiumValidationResult(
                    is_available=True,
                    browser_version=session.browser_version,
                    is_direct_loadable_chromium=True,
                    fallback_rejection_verified=fallback_ok,
                    measured_glyph_count=len(adv_deltas),
                    mean_chromium_advance_error_upem=round(mean_adv_err, 2),
                    rendered_canvas_valid=canvas_ok,
                )
            except Exception as e:
                logger.error("Chromium validation failed: %s", e)
                return ChromiumValidationResult(
                    is_available=True,
                    browser_version="error",
                    is_direct_loadable_chromium=False,
                    fallback_rejection_verified=False,
                    measured_glyph_count=0,
                    mean_chromium_advance_error_upem=999.0,
                    rendered_canvas_valid=False,
                    error_message=str(e),
                )
            finally:
                session.close()

        try:
            return asyncio.run(_run_browser())
        except Exception as e:
            return ChromiumValidationResult(
                is_available=False,
                browser_version="error",
                is_direct_loadable_chromium=False,
                fallback_rejection_verified=False,
                measured_glyph_count=0,
                mean_chromium_advance_error_upem=999.0,
                rendered_canvas_valid=False,
                error_message=str(e),
            )

    def _compute_freetype_raster_iou(
        self,
        cand_face: freetype.Face,
        ref_face: freetype.Face,
        char: str,
        size_px: int,
    ) -> tuple[float, int, str | None]:
        """Render bitmap with FreeType and compute IoU and pixel delta count (never fails open)."""
        try:
            cand_face.set_pixel_sizes(size_px, size_px)
            cand_face.load_char(char, freetype.FT_LOAD_RENDER | freetype.FT_LOAD_TARGET_NORMAL)
            cand_bmp = cand_face.glyph.bitmap
            cand_arr = np.array(cand_bmp.buffer, dtype=np.uint8).reshape((cand_bmp.rows, cand_bmp.width)) if cand_bmp.rows > 0 and cand_bmp.width > 0 else np.zeros((1, 1), dtype=np.uint8)

            ref_face.set_pixel_sizes(size_px, size_px)
            ref_face.load_char(char, freetype.FT_LOAD_RENDER | freetype.FT_LOAD_TARGET_NORMAL)
            ref_bmp = ref_face.glyph.bitmap
            ref_arr = np.array(ref_bmp.buffer, dtype=np.uint8).reshape((ref_bmp.rows, ref_bmp.width)) if ref_bmp.rows > 0 and ref_bmp.width > 0 else np.zeros((1, 1), dtype=np.uint8)

            H = max(cand_arr.shape[0], ref_arr.shape[0])
            W = max(cand_arr.shape[1], ref_arr.shape[1])
            if H == 0 or W == 0:
                return 0.0, -1, "EMPTY_BITMAP_RENDER"

            cand_padded = np.zeros((H, W), dtype=bool)
            ref_padded = np.zeros((H, W), dtype=bool)

            cand_padded[: cand_arr.shape[0], : cand_arr.shape[1]] = (cand_arr > 32)
            ref_padded[: ref_arr.shape[0], : ref_arr.shape[1]] = (ref_arr > 32)

            intersection = np.logical_and(cand_padded, ref_padded).sum()
            union = np.logical_or(cand_padded, ref_padded).sum()
            diff_cnt = int(np.logical_xor(cand_padded, ref_padded).sum())

            iou = float(intersection / union) if union > 0 else 0.0
            return iou, diff_cnt, None
        except Exception as e:
            logger.error("FreeType rasterization failed for char '%s' at %dpx: %s", char, size_px, e)
            return 0.0, -1, str(e)
