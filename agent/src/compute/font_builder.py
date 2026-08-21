"""Deterministic font binary builder using FontTools consuming source-driven glyph data."""
from __future__ import annotations

import hashlib
import io
import logging
from pathlib import Path
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.ttGlyphPen import TTGlyphPen

from compute.models import GeneratedFontFile, StyleSourceData

logger = logging.getLogger("telegramfonts.agent.font_builder")

UNICODE_MAP = {
    "space": 0x20,
    "A": 0x41,
    "B": 0x42,
    "a": 0x61,
    "b": 0x62,
}


class FontBuilderService:
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

        ext_map = {"TTF": "ttf", "OTF": "otf", "WOFF2": "woff2"}
        ext = ext_map[clean_format]

        sanitized_family = "".join(c for c in family_name if c.isalnum() or c in (" ", "-", "_")).strip()
        sanitized_style = "".join(c for c in style_source.style_name if c.isalnum() or c in (" ", "-", "_")).strip()
        ps_family = sanitized_family.replace(" ", "")
        ps_style = sanitized_style.replace(" ", "")
        ps_name = f"{ps_family}-{ps_style}"

        filename = f"{ps_name}.{ext}"
        output_path = (output_dir / filename).resolve()

        # Path traversal guard
        try:
            output_path.relative_to(output_dir.resolve())
        except ValueError:
            raise ValueError(f"Output path traversal detected: {filename}")

        glyph_order = list(style_source.glyphs.keys())
        if ".notdef" not in glyph_order:
            glyph_order.insert(0, ".notdef")

        cmap: dict[int, str] = {}
        for g_name in glyph_order:
            if g_name in UNICODE_MAP:
                cmap[UNICODE_MAP[g_name]] = g_name

        metrics_dict: dict[str, tuple[int, int]] = {}
        for g_name in glyph_order:
            g_vec = style_source.glyphs.get(g_name)
            adv = g_vec.advance_width if g_vec else 600
            lsb = g_vec.lsb if g_vec else 50
            metrics_dict[g_name] = (adv, lsb)

        family = sanitized_family or "TeleFont"
        style = sanitized_style or "Regular"
        full_name = f"{family} {style}".strip()
        ps = ps_name or f"{family.replace(' ', '')}-{style.replace(' ', '')}"
        unique_id = f"1.000;TeleFont;{ps}"

        name_strings = {
            "familyName": family,
            "styleName": style,
            "uniqueFontIdentifier": unique_id,
            "fullName": full_name,
            "version": "Version 1.000",
            "psName": ps,
        }

        # Determine fsSelection flags based on style name
        style_lower = style.lower()
        is_italic = "italic" in style_lower or "oblique" in style_lower or "slanted" in style_lower
        is_bold = "bold" in style_lower or style_source.weight_class >= 700
        fs_selection = 0
        if is_italic:
            fs_selection |= 0x01
        if is_bold:
            fs_selection |= 0x20
        if not is_italic and not is_bold:
            fs_selection |= 0x40  # REGULAR

        ascent = 800
        descent = -200
        win_ascent = max(ascent, abs(descent))
        win_descent = abs(descent)

        # Build format-specific font representation (BLOCK G)
        if clean_format == "OTF":
            # Real OpenType font with PostScript CFF outlines (isTTF=False, magic=OTTO)
            fb = FontBuilder(unitsPerEm=1024, isTTF=False)
            fb.setupGlyphOrder(glyph_order)
            fb.setupCharacterMap(cmap)

            charstrings: dict[str, object] = {}
            for g_name in glyph_order:
                g_vec = style_source.glyphs.get(g_name)
                adv = metrics_dict[g_name][0]
                pen = T2CharStringPen(adv, None)
                if g_vec and g_vec.contours:
                    for contour in g_vec.contours:
                        if len(contour) > 0:
                            pen.moveTo(contour[0])
                            for pt in contour[1:]:
                                pen.lineTo(pt)
                            pen.closePath()
                charstrings[g_name] = pen.getCharString()

            fb.setupCFF(
                psName=ps,
                fontInfo={"FullName": full_name, "FamilyName": family},
                charStringsDict=charstrings,
                privateDict={},
            )
            fb.setupHorizontalMetrics(metrics_dict)
            fb.setupHorizontalHeader(ascent=ascent, descent=descent)
            fb.setupNameTable(name_strings)
            fb.setupOS2(
                sTypoAscender=ascent,
                sTypoDescender=descent,
                sTypoLineGap=200,
                usWinAscent=win_ascent,
                usWinDescent=win_descent,
                sxHeight=500,
                sCapHeight=700,
                usWeightClass=style_source.weight_class,
                usWidthClass=5,
                fsSelection=fs_selection,
                ulCodePageRange1=1,  # Latin 1 / 1252
                ulUnicodeRange1=1,  # Basic Latin
            )
            fb.setupPost()

        else:
            # TrueType outlines for TTF and WOFF2 (isTTF=True, magic=\x00\x01\x00\x00 or wOF2)
            fb = FontBuilder(unitsPerEm=1024, isTTF=True)
            fb.setupGlyphOrder(glyph_order)
            fb.setupCharacterMap(cmap)

            glyphs_dict: dict[str, object] = {}
            for g_name in glyph_order:
                g_vec = style_source.glyphs.get(g_name)
                pen = TTGlyphPen(None)
                if g_vec and g_vec.contours:
                    for contour in g_vec.contours:
                        if len(contour) > 0:
                            pen.moveTo(contour[0])
                            for pt in contour[1:]:
                                pen.lineTo(pt)
                            pen.closePath()
                glyphs_dict[g_name] = pen.glyph()

            fb.setupGlyf(glyphs_dict)
            fb.setupHorizontalMetrics(metrics_dict)
            fb.setupHorizontalHeader(ascent=ascent, descent=descent)
            fb.setupNameTable(name_strings)
            fb.setupOS2(
                sTypoAscender=ascent,
                sTypoDescender=descent,
                sTypoLineGap=200,
                usWinAscent=win_ascent,
                usWinDescent=win_descent,
                sxHeight=500,
                sCapHeight=700,
                usWeightClass=style_source.weight_class,
                usWidthClass=5,
                fsSelection=fs_selection,
                ulCodePageRange1=1,  # Latin 1 / 1252
                ulUnicodeRange1=1,  # Basic Latin
            )
            fb.setupPost()

            if clean_format == "WOFF2":
                fb.font.flavor = "woff2"

        buffer = io.BytesIO()
        fb.save(buffer)
        raw_bytes = buffer.getvalue()

        output_path.write_bytes(raw_bytes)
        sha256_hex = hashlib.sha256(raw_bytes).hexdigest()

        return GeneratedFontFile(
            style_id=style_source.style_id,
            style_name=sanitized_style,
            format=clean_format,
            filename=filename,
            file_path=output_path,
            size_bytes=len(raw_bytes),
            sha256_hex=sha256_hex,
        )
