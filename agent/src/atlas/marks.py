"""Unicode combining-mark helpers shared across atlas stages (R2).

Zero advance is VALID for Unicode combining marks (categories Mn/Mc/Me):
attachment geometry is anchor-driven and the observed standalone advance
quantizes to zero px in direct browser metrics. These helpers make the mark
semantics deterministic and importable without circular dependencies.
"""
from __future__ import annotations

import unicodedata

COMBINING_MARK_CATEGORIES = ("Mn", "Mc", "Me")


def is_combining_mark(code_point: int) -> bool:
    """True for Unicode combining marks (category Mn/Mc/Me)."""
    try:
        return unicodedata.category(chr(int(code_point))) in COMBINING_MARK_CATEGORIES
    except (ValueError, OverflowError):
        return False


def mark_extra_left_px(lsb_upem: float, size_px: int) -> float:
    """Left-of-pen ink extent (px) for a zero/negative-LSB mark cell.

    Combining marks frequently carry ink to the LEFT of the pen origin; the
    cell must extend its pen offset by exactly the observed extent so the
    mark ink is captured (observed-metrics-driven, never invented).
    """
    left_extent_upem = max(0.0, -float(lsb_upem))
    return left_extent_upem * float(size_px) / 1000.0


def mark_effective_advance_px(
    advance_upem: float, lsb_upem: float, bbox_width_upem: float, size_px: int
) -> float:
    """Right-of-pen ink extent (px) a mark cell must cover.

    A mark's ink can extend beyond its (zero) advance; the cell width is the
    observed right ink extent, never smaller than the nominal advance.
    """
    right_upem = max(float(advance_upem), float(lsb_upem) + float(bbox_width_upem))
    right_upem = max(right_upem, 0.0)
    return right_upem * float(size_px) / 1000.0
