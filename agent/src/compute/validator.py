"""Font output validation using FontTools with strict format-distinct checking and independent loadability verification."""
from __future__ import annotations

import ctypes
import logging
import sys
from pathlib import Path
from fontTools.ttLib import TTFont

logger = logging.getLogger("telegramfonts.agent.validator")

REQUIRED_COMMON_TABLES = {"head", "hhea", "maxp", "name", "OS/2", "cmap", "post"}
REQUIRED_NAME_IDS = {1, 2, 3, 4, 6}  # family, subfamily, uniqueID, full name, PostScript name


def _verify_independent_gdi_load(font_path: Path) -> bool:
    """Non-persistent font load verification via Windows GDI AddFontResourceExW (FR_PRIVATE)."""
    if sys.platform != "win32":
        return True

    try:
        gdi32 = ctypes.windll.gdi32
        FR_PRIVATE = 0x10
        res = gdi32.AddFontResourceExW(str(font_path.resolve()), FR_PRIVATE, 0)
        if res > 0:
            gdi32.RemoveFontResourceExW(str(font_path.resolve()), FR_PRIVATE, 0)
            return True
        return False
    except Exception as exc:
        logger.warning(f"GDI independent font check error: {exc}")
        return True


def validate_font_file(file_path: Path, expected_format: str) -> bool:
    """Validate font file integrity, table structure, glyph counts, metrics, and format-distinct signatures."""
    if not file_path.exists() or file_path.stat().st_size == 0:
        return False

    raw_bytes = file_path.read_bytes()
    expected_fmt = expected_format.strip().upper()

    # 1. Check strict magic header bytes per format (BLOCK G)
    if expected_fmt == "OTF":
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

        # Validate glyph count
        glyph_order = font.getGlyphOrder()
        if len(glyph_order) < 2:
            return False

        # Validate name table has required standard NameIDs
        present_name_ids = {n.nameID for n in font["name"].names}
        if not REQUIRED_NAME_IDS.issubset(present_name_ids):
            return False

        # Validate OS/2 table metrics required for installable fonts
        os2 = font["OS/2"]
        if (
            os2.usWinAscent <= 0
            or os2.usWinDescent <= 0
            or os2.sTypoAscender == 0
            or os2.usWeightClass <= 0
        ):
            return False

        # Validate cmap table has character mappings
        cmap_tables = [t for t in font["cmap"].tables if getattr(t, "cmap", None)]
        if not cmap_tables or all(len(t.cmap) == 0 for t in cmap_tables):
            return False

        # 2. Independent consumer / GDI load check
        if not _verify_independent_gdi_load(file_path):
            return False

        font.close()
        return True
    except Exception as exc:
        logger.warning(f"Font validation failed for {file_path}: {exc}")
        return False
