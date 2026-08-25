"""Fail-closed verification of authorized acquired font binaries.

Integrity outcomes are explicit and never ambiguous:
- VALID: well-formed sfnt, expected family/style identity, within size bound.
- ABSENT: the stage produced no bytes (pipeline proceeds to the next stage).
- INTEGRITY_FAILED: bytes exist but fail verification (fail-closed terminal;
  the binary never silently degrades into raster fallback).
"""
from __future__ import annotations

import io
from dataclasses import dataclass


@dataclass(frozen=True)
class BinaryVerification:
    status: str  # VALID | ABSENT | INTEGRITY_FAILED
    reason_code: str = ""
    format: str = ""
    family_name: str = ""
    style_name: str = ""


def _best_name(font, name_id: int) -> str:
    record = font["name"].getDebugName(name_id)
    return str(record).strip() if record else ""


def verify_acquired_binary(
    raw_bytes: bytes | None,
    expected_family: str,
    expected_style: str,
    max_bytes: int,
) -> BinaryVerification:
    """Verify binary type/integrity/identity. Deterministic and sanitized."""
    if raw_bytes is None or len(raw_bytes) == 0:
        return BinaryVerification(status="ABSENT", reason_code="BINARY_ABSENT")
    if len(raw_bytes) > max_bytes:
        return BinaryVerification(status="INTEGRITY_FAILED", reason_code="BINARY_SIZE_EXCEEDED")

    from fontTools.ttLib import TTFont

    try:
        font = TTFont(io.BytesIO(raw_bytes), fontNumber=0, lazy=True)
        cmap = font.getBestCmap()
        if not cmap:
            font.close()
            return BinaryVerification(status="INTEGRITY_FAILED", reason_code="BINARY_NO_CMAP")

        tables = set(font.keys())
        required_common = {"cmap", "head", "hhea", "hmtx", "maxp", "name", "post"}
        if not required_common.issubset(tables):
            font.close()
            return BinaryVerification(status="INTEGRITY_FAILED", reason_code="BINARY_REQUIRED_TABLES_MISSING")

        sfnt_version = font.sfntVersion
        if sfnt_version == "\x00\x01\x00\x00" and "glyf" in tables:
            fmt = "TTF"
        elif sfnt_version == "OTTO" and "CFF " in tables:
            fmt = "OTF"
        else:
            font.close()
            return BinaryVerification(status="INTEGRITY_FAILED", reason_code="BINARY_UNKNOWN_FLAVOR")

        family_name = _best_name(font, 16) or _best_name(font, 1)
        style_name = _best_name(font, 17) or _best_name(font, 2)
        glyph_count = int(font["maxp"].numGlyphs)
        units_per_em = int(font["head"].unitsPerEm)
        font.close()
    except Exception:
        return BinaryVerification(status="INTEGRITY_FAILED", reason_code="BINARY_PARSE_FAILED")

    if glyph_count <= 0 or units_per_em <= 0:
        return BinaryVerification(status="INTEGRITY_FAILED", reason_code="BINARY_EMPTY_GLYPHSET")

    expected_family_norm = " ".join(str(expected_family).split()).lower()
    expected_style_norm = " ".join(str(expected_style).split()).lower()
    if expected_family_norm and family_name.lower() != expected_family_norm:
        return BinaryVerification(
            status="INTEGRITY_FAILED",
            reason_code="BINARY_FAMILY_MISMATCH",
            format=fmt,
            family_name=family_name,
            style_name=style_name,
        )
    if expected_style_norm and style_name.lower() != expected_style_norm:
        return BinaryVerification(
            status="INTEGRITY_FAILED",
            reason_code="BINARY_STYLE_MISMATCH",
            format=fmt,
            family_name=family_name,
            style_name=style_name,
        )

    return BinaryVerification(
        status="VALID",
        format=fmt,
        family_name=family_name,
        style_name=style_name,
    )
