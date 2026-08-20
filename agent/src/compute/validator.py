"""Font output validation using FontTools with strict format-distinct checking."""
from __future__ import annotations

import logging
from pathlib import Path
from fontTools.ttLib import TTFont

logger = logging.getLogger("telegramfonts.agent.validator")

REQUIRED_COMMON_TABLES = {"head", "maxp", "name", "OS/2", "cmap"}


def validate_font_file(file_path: Path, expected_format: str) -> bool:
    """Validate font file integrity, table structure, glyph counts, and format-distinct signatures."""
    if not file_path.exists() or file_path.stat().st_size == 0:
        return False

    raw_bytes = file_path.read_bytes()
    expected_fmt = expected_format.strip().upper()

    # 1. Check strict magic header bytes per format (BLOCK G)
    if expected_fmt == "WOFF2":
        if not raw_bytes.startswith(b"wOF2"):
            return False
    elif expected_fmt == "OTF":
        # Real OpenType CFF must have 'OTTO' sfntVersion
        if not raw_bytes.startswith(b"OTTO"):
            return False
    elif expected_fmt == "TTF":
        # TrueType sfntVersion must be 0x00010000 or 'true'
        if not (raw_bytes.startswith(b"\x00\x01\x00\x00") or raw_bytes.startswith(b"true")):
            return False
    else:
        return False

    try:
        font = TTFont(file_path)
        tables = set(font.keys())

        # Check required common tables
        if not REQUIRED_COMMON_TABLES.issubset(tables):
            return False

        # Format-specific table assertions (BLOCK G)
        if expected_fmt == "OTF":
            if "CFF " not in tables and "CFF2" not in tables:
                return False
            if "glyf" in tables or "loca" in tables:
                return False
        elif expected_fmt == "TTF":
            if "glyf" not in tables or "loca" not in tables:
                return False
            if "CFF " in tables or "CFF2" in tables:
                return False
        elif expected_fmt == "WOFF2":
            if font.flavor != "woff2":
                return False

        # Validate glyph count
        glyph_order = font.getGlyphOrder()
        if len(glyph_order) < 2:
            return False

        font.close()
        return True
    except Exception as exc:
        logger.warning(f"Font validation failed for {file_path}: {exc}")
        return False
