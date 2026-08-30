"""Bounded atlas page planning and streaming (ADR-0004, U2/U6).

Fast raster pass: size 1024, phase x=0/y=0, one logical atlas in bounded
pages. Target 96 MB decoded page memory, hard max 128 MB. One page in
memory at a time; page N+1 streams (downloads) while page N decodes; cells
are cropped in memory and the page memory is released immediately.

The byte budget is applied to the DECODED 8-bit alpha plane (1 byte/pixel),
which is the dominant transient allocation; compressed transport bytes are
bounded separately by the HTTP layer.
"""
from __future__ import annotations

from dataclasses import dataclass

from atlas.models import AtlasPage, PlacedCell

# Deterministic cell padding around the em box (pixels). Generous vertical
# padding absorbs overshoot beyond the regressed ascent/descent; horizontal
# padding absorbs advance/bbox regression uncertainty.
CELL_PAD_X_PX = 128
CELL_PAD_Y_PX = 128

# Canvas geometry caps keep a single page addressable and crop-friendly.
MAX_CANVAS_DIMENSION_PX = 16384


def pen_left_px(extra_left_px: float = 0.0) -> int:
    """Pen x offset inside the cell: base padding + observed left ink extent.

    Combining marks (R2) frequently carry ink LEFT of the pen origin; the
    pen offset grows by exactly the observed extent so mark ink is captured.
    """
    return int(CELL_PAD_X_PX + max(0.0, float(extra_left_px)) + 0.5)


def cell_dimensions(
    advance_px: float,
    ascent_px: float,
    descent_px: float,
    size_px: int,
    extra_left_px: float = 0.0,
) -> tuple[int, int]:
    """Deterministic cell size for one glyph at one render size.

    Width covers the regressed advance plus padding plus any observed
    left-of-pen ink extent (combining marks, R2); height covers the
    regressed ink column (ascent + descent, floored at the em size) plus
    padding. Never smaller than the em box so whole-glyph ink is captured.
    """
    if size_px <= 0:
        raise ValueError("ATLAS_CELL_SIZE_INVALID")
    w = int(max(1.0, advance_px) + max(0.0, float(extra_left_px)) + 2 * CELL_PAD_X_PX + 0.9999)
    ink_h = max(float(size_px), float(ascent_px) + float(descent_px))
    h = int(ink_h + 2 * CELL_PAD_Y_PX + 0.9999)
    if w > MAX_CANVAS_DIMENSION_PX or h > MAX_CANVAS_DIMENSION_PX:
        raise ValueError("ATLAS_CELL_EXCEEDS_CANVAS_CAP")
    return w, h


@dataclass(frozen=True)
class PagePlanInput:
    code_point: int
    cell_w: int
    cell_h: int
    size_px: int
    phase_x: float = 0.0
    phase_y: float = 0.0
    extra_left_px: float = 0.0


class AtlasBudgetExceeded(ValueError):
    """A single cell cannot fit under the hard page byte cap (fail closed)."""

    def __init__(self) -> None:
        super().__init__("ATLAS_PAGE_BUDGET_EXCEEDED")


def plan_atlas_pages(
    inputs: list[PagePlanInput],
    target_bytes: int,
    max_bytes: int,
) -> list[AtlasPage]:
    """Greedy deterministic shelf packing into bounded pages.

    Order is deterministic (cell height descending, code point ascending).
    Each page's decoded byte count (width x height) stays <= max_bytes and
    aims at target_bytes; pages close when the next cell cannot be placed
    without crossing the hard cap or the canvas dimension caps.
    """
    if target_bytes <= 0 or max_bytes <= 0 or target_bytes > max_bytes:
        raise ValueError("ATLAS_PAGE_BUDGET_INVALID")

    ordered = sorted(inputs, key=lambda c: (-c.cell_h, c.code_point))
    pages: list[AtlasPage] = []
    current: list[PlacedCell] = []
    shelf_x = 0
    shelf_y = 0
    shelf_h = 0
    page_w = 0

    def close_page() -> None:
        nonlocal current, shelf_x, shelf_y, shelf_h, page_w
        if not current:
            return
        pages.append(
            AtlasPage(
                index=len(pages),
                width=page_w,
                height=shelf_y + shelf_h,
                cells=tuple(current),
            )
        )
        current = []
        shelf_x = 0
        shelf_y = 0
        shelf_h = 0
        page_w = 0

    for item in ordered:
        w, h = item.cell_w, item.cell_h
        if w * h > max_bytes:
            raise AtlasBudgetExceeded()

        def fits(x: int, y: int, new_w: int, new_h: int) -> bool:
            return (
                new_w <= MAX_CANVAS_DIMENSION_PX
                and new_h <= MAX_CANVAS_DIMENSION_PX
                and new_w * new_h <= max_bytes
            )

        # Try the current shelf, then a new shelf, then a new page.
        placed = False
        if current:
            cand_w = max(page_w, shelf_x + w)
            cand_h = shelf_y + max(shelf_h, h)
            if fits(shelf_x, shelf_y, cand_w, cand_h):
                current.append(
                    PlacedCell(
                        item.code_point, len(pages), shelf_x, shelf_y, w, h,
                        item.size_px, item.phase_x, item.phase_y,
                        pen_left_px=pen_left_px(item.extra_left_px),
                    )
                )
                shelf_x += w
                shelf_h = max(shelf_h, h)
                page_w = max(page_w, shelf_x)
                placed = True
            else:
                # New shelf on the same page.
                cand_w = max(page_w, w)
                cand_h = shelf_y + shelf_h + h
                if fits(0, shelf_y + shelf_h, cand_w, cand_h) and (
                    (shelf_y + shelf_h) * cand_w <= target_bytes
                ):
                    shelf_y += shelf_h
                    shelf_x = w
                    shelf_h = h
                    page_w = max(page_w, shelf_x)
                    current.append(
                        PlacedCell(
                            item.code_point, len(pages), 0, shelf_y, w, h,
                            item.size_px, item.phase_x, item.phase_y,
                        )
                    )
                    placed = True
        else:
            current.append(
                PlacedCell(
                    item.code_point, len(pages), 0, 0, w, h,
                    item.size_px, item.phase_x, item.phase_y,
                    pen_left_px=pen_left_px(item.extra_left_px),
                )
            )
            shelf_x = w
            shelf_h = h
            page_w = w
            placed = True

        if not placed:
            close_page()
            current.append(
                PlacedCell(
                    item.code_point, len(pages), 0, 0, w, h,
                    item.size_px, item.phase_x, item.phase_y,
                    pen_left_px=pen_left_px(item.extra_left_px),
                )
            )
            shelf_x = w
            shelf_h = h
            page_w = w

    close_page()

    # Post-condition: every page respects the hard budget (defensive).
    for page in pages:
        if page.decoded_bytes > max_bytes:
            raise AtlasBudgetExceeded()
    return pages


def estimate_cell_plan(
    code_points: list[int],
    advances_px: dict[int, float],
    ascents_px: dict[int, float],
    descents_px: dict[int, float],
    size_px: int,
    target_mb: int,
    max_mb: int,
    extra_left_px: dict[int, float] | None = None,
) -> list[AtlasPage]:
    """Convenience planner from regressed pixel metrics (fast pass).

    ``extra_left_px`` carries the observed left-of-pen ink extent for
    combining-mark cells (R2); absent entries default to zero.
    """
    extras = extra_left_px or {}
    inputs: list[PagePlanInput] = []
    for cp in code_points:
        extra = float(extras.get(cp, 0.0))
        w, h = cell_dimensions(
            advances_px.get(cp, float(size_px) * 0.6),
            ascents_px.get(cp, float(size_px) * 0.8),
            descents_px.get(cp, float(size_px) * 0.2),
            size_px,
            extra_left_px=extra,
        )
        inputs.append(PagePlanInput(cp, w, h, size_px, extra_left_px=extra))
    return plan_atlas_pages(inputs, target_mb * 1024 * 1024, max_mb * 1024 * 1024)
