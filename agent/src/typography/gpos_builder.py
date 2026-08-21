"""OpenType GPOS table builder attaching deterministic pair kerning adjustments."""
from __future__ import annotations

import logging
from typing import Any

from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
from fontTools.ttLib import TTFont

def _get_glyph_name(cp: int) -> str:
    if cp == 0x20:
        return "space"
    if 0x21 <= cp <= 0x7E:
        char = chr(cp)
        if char.isalnum():
            return char
        if char == "@":
            return "at"
        if char == "%":
            return "percent"
    return f"uni{cp:04X}"


logger = logging.getLogger("telegramfonts.agent.typography.gpos_builder")


def generate_kern_feature_syntax(
    typography: TypographyDataset,
    cmap: dict[int, str],
) -> str:
    """Generate canonical OpenType Feature syntax (.fea) for PairPos kerning."""
    if not typography.kerning_pairs:
        return ""

    lines: list[str] = [
        "languagesystem DFLT dflt;",
        "languagesystem latn dflt;",
        "",
        "feature kern {",
    ]

    # Sort pairs deterministically by (left_cp, right_cp)
    valid_pair_count = 0
    for (left_cp, right_cp), kern_val in sorted(typography.kerning_pairs.items()):
        left_name = cmap.get(left_cp) or _get_glyph_name(left_cp)
        right_name = cmap.get(right_cp) or _get_glyph_name(right_cp)

        # Only emit pairs where both glyphs are present in the character map
        if left_name and right_name and kern_val != 0:
            lines.append(f"    pos {left_name} {right_name} {kern_val};")
            valid_pair_count += 1

    lines.append("} kern;")
    lines.append("")

    if valid_pair_count == 0:
        return ""

    return "\n".join(lines)


def attach_gpos_to_font(
    font: TTFont,
    typography: TypographyDataset | None,
    cmap: dict[int, str] | None = None,
) -> bool:
    """Attach deterministic OpenType GPOS table to TTFont object from TypographyDataset."""
    if not typography or not typography.kerning_pairs:
        return False

    effective_cmap = cmap or font.getBestCmap() or {}
    # Convert integer-to-glyph-name map if needed
    name_cmap: dict[int, str] = {}
    for cp, val in effective_cmap.items():
        if isinstance(val, str):
            name_cmap[cp] = val
        else:
            name_cmap[cp] = _get_glyph_name(cp)

    fea_text = generate_kern_feature_syntax(typography, name_cmap)
    if not fea_text:
        return False

    try:
        addOpenTypeFeaturesFromString(font, fea_text)
        logger.info("Attached OpenType GPOS kerning table with %d active pairs", len(typography.kerning_pairs))
        return "GPOS" in font
    except Exception as exc:
        logger.error("Failed to attach GPOS table via feaLib: %s", exc)
        return False
