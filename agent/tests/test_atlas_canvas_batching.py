"""Causal tests for the G3 iteration-3 root cause fix.

Root cause (proven on A23, iteration 3 + bounded diagnostics): the planner
area-budgets atlas pages (up to ~128 Mpx), but fetch_cell_pages stacked every
cell of a page into ONE vertical canvas. Real pages reach ~118k px height and
the browser rejects canvas dimensions silently (empty data URL), losing the
entire page's observations. Fix: bounded stacked batches (<=
MAX_CANVAS_DIMENSION_PX), one readback per batch; per-cell crops identical.

The negative gate test proves the IOU comparator still rejects corrupted
contours (the fix must not weaken validation meaning).
"""
import asyncio
import base64
import io
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from atlas.geometry import (
    MIN_IOU_EASY_PASS,
    fast_geometry_for_glyph,
    mask_iou,
    rasterize_segments_mask,
)
from atlas.models import CellMapping, GlyphStatus, RegressedMetrics
from atlas.paging import CELL_PAD_X_PX, CELL_PAD_Y_PX, MAX_CANVAS_DIMENSION_PX
from atlas.transport import AtlasTransportCounters, PersistentBrowserAtlasSession

CELL_W = 1029
CELL_H = 1449


def _make_specs(n: int) -> list[dict]:
    return [
        {
            "cp": 33 + i,
            "w": CELL_W,
            "h": CELL_H,
            "y0": 0,
            "pen_left": CELL_PAD_X_PX,
            "baseline_y": CELL_PAD_Y_PX + 951,
            "phase_x": 0.0,
            "phase_y": 0.0,
            "size_px": 1024,
        }
        for i in range(n)
    ]


def _stub_session() -> tuple[PersistentBrowserAtlasSession, list[dict]]:
    session = PersistentBrowserAtlasSession(
        source_url="stub://canvas-batch",
        family_name="Stub",
        style_name="Regular",
        style_id="regular",
        counters=AtlasTransportCounters(),
    )
    session.started = True
    calls: list[dict] = []

    async def fake_evaluate(expression: str, arg=None):
        assert arg is not None
        page_w = int(arg["page_w"])
        page_h = int(arg["page_h"])
        assert page_h <= MAX_CANVAS_DIMENSION_PX, (
            f"unbounded canvas height {page_h} exceeds {MAX_CANVAS_DIMENSION_PX}"
        )
        assert page_w <= MAX_CANVAS_DIMENSION_PX
        calls.append({"n": len(arg["cells"]), "w": page_w, "h": page_h})
        img = Image.new("L", (page_w, page_h), 0)
        draw = ImageDraw.Draw(img)
        y = 0
        for c in arg["cells"]:
            cw, ch = int(c["w"]), int(c["h"])
            draw.rectangle([64, y + 200, min(cw, 400), y + min(ch, 900)], fill=255)
            y += ch
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

    session._evaluate = fake_evaluate  # type: ignore[assignment]
    return session, calls


def test_fetch_cell_pages_batches_canvas_height() -> None:
    session, calls = _stub_session()
    specs = _make_specs(20)  # 20 * 1449 = 28980 px > 16384 bound
    out = asyncio.run(session.fetch_cell_pages(specs))

    assert sorted(out) == [33 + i for i in range(20)]
    assert len(calls) >= 2, "stack must split into bounded batches"
    assert all(c["h"] <= MAX_CANVAS_DIMENSION_PX for c in calls)
    assert session.counters.browser_readbacks == len(calls)
    for cp, png in out.items():
        img = Image.open(io.BytesIO(png))
        assert img.size == (CELL_W, CELL_H)
        assert np.asarray(img.convert("L")).max() == 255


def test_fetch_cell_pages_small_stays_single_readback() -> None:
    session, calls = _stub_session()
    out = asyncio.run(session.fetch_cell_pages(_make_specs(3)))  # 4347 px
    assert len(calls) == 1
    assert len(out) == 3
    assert session.counters.browser_readbacks == 1


def test_fetch_cell_pages_exact_bound_no_split() -> None:
    session, calls = _stub_session()
    n = MAX_CANVAS_DIMENSION_PX // CELL_H  # largest n with n*h <= bound
    out = asyncio.run(session.fetch_cell_pages(_make_specs(n)))
    assert len(calls) == 1
    assert len(out) == n


# ---------------------------------------------------------------------------
# Comparator gate: corrupted contours must still fail (no weakened validation)
# ---------------------------------------------------------------------------

def _obs_png(dx: int = 0) -> bytes:
    arr = np.zeros((CELL_H, CELL_W), dtype=np.uint8)
    arr[300:900, 200 + dx:500 + dx] = 255
    buf = io.BytesIO()
    Image.fromarray(arr, "L").save(buf, format="PNG")
    return buf.getvalue()


def _regressed() -> RegressedMetrics:
    return RegressedMetrics(
        code_point=0x41,
        advance_width_upem=700.0,
        lsb_upem=100.0,
        rsb_upem=100.0,
        ascent_upem=900.0,
        descent_upem=-200.0,
        bbox_upem=(100.0, -100.0, 600.0, 800.0),
        regression_residual=0.01,
    )


def _mapping(pad_left: int = CELL_PAD_X_PX) -> CellMapping:
    return CellMapping(
        size_px=1024,
        pad_left_px=pad_left,
        pad_top_px=CELL_PAD_Y_PX,
        ascent_px=951.0,
    )


def test_easy_pass_positive_control() -> None:
    ev, contours, ink = fast_geometry_for_glyph(
        _obs_png(), _mapping(), _regressed(), CELL_W, CELL_H
    )
    assert ev.status == GlyphStatus.EASY_PASS
    assert ev.iou >= MIN_IOU_EASY_PASS
    assert contours, "rectangle ink must yield fitted contours"


def test_comparator_rejects_corrupted_contours() -> None:
    ev, contours, ink = fast_geometry_for_glyph(
        _obs_png(), _mapping(), _regressed(), CELL_W, CELL_H
    )
    assert ev.status == GlyphStatus.EASY_PASS

    good_mask = rasterize_segments_mask(contours, _mapping(), CELL_W, CELL_H)
    assert mask_iou(ink, good_mask) >= MIN_IOU_EASY_PASS

    # Corrupted placement: contours rasterized through a shifted mapping must
    # fall below the easy-pass IOU gate (validation meaning preserved).
    bad_mask = rasterize_segments_mask(
        contours, _mapping(pad_left=CELL_PAD_X_PX - 120), CELL_W, CELL_H
    )
    assert mask_iou(ink, bad_mask) < MIN_IOU_EASY_PASS
