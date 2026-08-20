"""Deterministic font binary builder using FontTools."""
from __future__ import annotations

import hashlib
import io
import logging
from pathlib import Path
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen

from compute.models import GeneratedFontFile

logger = logging.getLogger("telegramfonts.agent.font_builder")


def _build_base_glyphs() -> tuple[dict[str, object], dict[str, tuple[int, int]], dict[int, str]]:
    """Generate minimal valid TrueType glyph set and unicode mappings."""
    # .notdef glyph
    pen_notdef = TTGlyphPen(None)
    pen_notdef.moveTo((50, 0))
    pen_notdef.lineTo((50, 600))
    pen_notdef.lineTo((300, 600))
    pen_notdef.lineTo((300, 0))
    pen_notdef.closePath()
    glyph_notdef = pen_notdef.glyph()

    # space glyph (empty outline)
    pen_space = TTGlyphPen(None)
    glyph_space = pen_space.glyph()

    # standard letter glyph
    pen_char = TTGlyphPen(None)
    pen_char.moveTo((100, 0))
    pen_char.lineTo((100, 700))
    pen_char.lineTo((500, 700))
    pen_char.lineTo((500, 0))
    pen_char.closePath()
    glyph_char = pen_char.glyph()

    glyphs = {
        ".notdef": glyph_notdef,
        "space": glyph_space,
        "A": glyph_char,
        "B": glyph_char,
        "a": glyph_char,
        "b": glyph_char,
    }

    metrics = {
        ".notdef": (350, 50),
        "space": (300, 0),
        "A": (600, 100),
        "B": (600, 100),
        "a": (500, 100),
        "b": (500, 100),
    }

    cmap = {
        0x20: "space",
        0x41: "A",
        0x42: "B",
        0x61: "a",
        0x62: "b",
    }

    return glyphs, metrics, cmap


class FontBuilderService:
    def build_font(
        self,
        family_name: str,
        style_name: str,
        format_type: str,
        output_dir: Path,
    ) -> GeneratedFontFile:
        clean_format = format_type.strip().upper()
        if clean_format not in ("TTF", "OTF", "WOFF2"):
            raise ValueError(f"UNSUPPORTED_FORMAT: {clean_format}")

        ext_map = {"TTF": "ttf", "OTF": "otf", "WOFF2": "woff2"}
        ext = ext_map[clean_format]

        sanitized_family = "".join(c for c in family_name if c.isalnum() or c in (" ", "-", "_")).strip()
        sanitized_style = "".join(c for c in style_name if c.isalnum() or c in (" ", "-", "_")).strip()
        ps_family = sanitized_family.replace(" ", "")
        ps_style = sanitized_style.replace(" ", "")
        ps_name = f"{ps_family}-{ps_style}"

        filename = f"{ps_name}.{ext}"
        output_path = output_dir / filename

        fb = FontBuilder(unitsPerEm=1024, isTTF=True)
        glyphs, metrics, cmap = _build_base_glyphs()

        fb.setupGlyphOrder(list(glyphs.keys()))
        fb.setupCharacterMap(cmap)
        fb.setupGlyf(glyphs)
        fb.setupHorizontalMetrics(metrics)
        fb.setupHorizontalHeader(ascent=800, descent=-200)

        name_strings = {
            "familyName": sanitized_family or "TeleFont",
            "styleName": sanitized_style or "Regular",
            "psName": ps_name or "TeleFont-Regular",
        }
        fb.setupNameTable(name_strings)
        fb.setupOS2()
        fb.setupPost()

        if clean_format == "WOFF2":
            fb.font.flavor = "woff2"

        buffer = io.BytesIO()
        fb.save(buffer)
        raw_bytes = buffer.getvalue()

        output_path.write_bytes(raw_bytes)
        sha256_hex = hashlib.sha256(raw_bytes).hexdigest()

        return GeneratedFontFile(
            style_id=style_name.lower().replace(" ", "_"),
            style_name=sanitized_style,
            format=clean_format,
            filename=filename,
            file_path=output_path,
            size_bytes=len(raw_bytes),
            sha256_hex=sha256_hex,
        )
