"""MAX-exclusive font binary builder consuming source-driven glyph data through MaxCandidateFontBuilder."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from compute.models import GeneratedFontFile, StyleSourceData
from reconstruction.candidate_builder import MaxCandidateFontBuilder
from reconstruction.models import Contour, LineSegment, Point2D, ReconstructedGlyph

logger = logging.getLogger("telegramfonts.agent.font_builder")

UNICODE_MAP = {
    "space": 0x20,
    "A": 0x41,
    "B": 0x42,
    "a": 0x61,
    "b": 0x62,
}


class FontBuilderService:
    """Production MAX font builder service delegating exclusively to MaxCandidateFontBuilder."""

    def build_font(
        self,
        style_source: StyleSourceData,
        family_name: str,
        format_type: str,
        output_dir: Path,
    ) -> GeneratedFontFile:
        clean_format = format_type.strip().upper()
        if clean_format not in ("TTF", "OTF", "WOFF2"):
            raise ValueError(f"UNSUPPORTED_FORMAT: {clean_format}")

        sanitized_family = "".join(c for c in family_name if c.isalnum() or c in (" ", "-", "_")).strip() or "TeleFont"
        sanitized_style = "".join(c for c in style_source.style_name if c.isalnum() or c in (" ", "-", "_")).strip() or "Regular"

        glyph_models: dict[int, ReconstructedGlyph] = {}
        for g_name, g_vec in style_source.glyphs.items():
            cp = ord(g_vec.character[0]) if (g_vec.character and len(g_vec.character) > 0) else UNICODE_MAP.get(g_name, 0x20)
            contours: list[Contour] = []
            for loop in g_vec.contours:
                if len(loop) < 2:
                    continue
                segs = [
                    LineSegment(Point2D(loop[i][0], loop[i][1]), Point2D(loop[i + 1][0], loop[i + 1][1]))
                    for i in range(len(loop) - 1)
                ]
                segs.append(LineSegment(Point2D(loop[-1][0], loop[-1][1]), Point2D(loop[0][0], loop[0][1])))
                contours.append(Contour(segments=segs, is_hole=False))

            glyph_models[cp] = ReconstructedGlyph(
                code_point=cp,
                character=g_vec.character or chr(cp) if 32 <= cp <= 126 else "",
                advance_width_upem=float(g_vec.advance_width),
                lsb_upem=float(g_vec.lsb),
                rsb_upem=float(max(0, g_vec.advance_width - g_vec.lsb - 100)),
                ascent_upem=800.0,
                descent_upem=-200.0,
                contours=contours,
            )

        builder = MaxCandidateFontBuilder(
            family_name=sanitized_family,
            style_name=sanitized_style,
            weight_class=style_source.weight_class,
        )
        family_res = builder.build_candidate_family(glyph_models, output_dir=output_dir)

        if clean_format == "TTF":
            art = family_res.ttf
        elif clean_format == "OTF":
            art = family_res.otf
        elif clean_format == "WOFF2":
            art = family_res.woff2
        else:
            raise ValueError(f"UNSUPPORTED_FORMAT: {clean_format}")

        return GeneratedFontFile(
            style_id=style_source.style_id,
            style_name=style_source.style_name,
            format=clean_format,
            filename=art.filename,
            file_path=art.file_path,
            size_bytes=art.size_bytes,
            sha256_hex=art.sha256_hex,
        )
