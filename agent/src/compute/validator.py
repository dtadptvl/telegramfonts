"""Font output validation using FontTools."""
from __future__ import annotations

import logging
from pathlib import Path
from fontTools.ttLib import TTFont

logger = logging.getLogger("telegramfonts.agent.validator")

REQUIRED_TABLES = {"head", "maxp", "name", "OS/2", "cmap"}


def validate_font_file(file_path: Path, expected_format: str) -> bool:
    """Validate font file integrity, required tables, non-empty glyphs, and magic bytes."""
    if not file_path.exists() or file_path.stat().st_size == 0:
        return False

    raw_bytes = file_path.read_bytes()
    expected_fmt = expected_format.strip().upper()

    # Check magic header bytes
    if expected_fmt == "WOFF2":
        if not raw_bytes.startswith(b"wOF2"):
            return False
    elif expected_fmt in ("TTF", "OTF"):
        if not (raw_bytes.startswith(b"\x00\x01\x00\x00") or raw_bytes.startswith(b"OTTO") or raw_bytes.startswith(b"true")):
            return False

    try:
        font = TTFont(file_path)
        tables = set(font.keys())
        if not REQUIRED_TABLES.issubset(tables):
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
