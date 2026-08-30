"""R2 focused repros: zero-advance Unicode combining marks are VALID.

Corrects the 'degenerate standalone combining marks' semantics recorded in
E-00024: marks with valid ink/outline/bbox/anchor MUST survive into
GlyphModel, cmap and TTF/OTF outputs (never FAILED_GLYPH solely for zero
advance), while genuinely malformed glyphs / disallowed zero-ink /
degenerate outlines remain fail-closed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from atlas.fontbuild import AtlasFontBuilder, assemble_font_model
from atlas.geometry import structural_check
from atlas.marks import (
    is_combining_mark,
    mark_effective_advance_px,
    mark_extra_left_px,
)
from atlas.models import RegressedMetrics
from atlas.paging import CELL_PAD_X_PX, cell_dimensions, estimate_cell_plan, pen_left_px
from reconstruction.font_model import CalibratedGlyph
from reconstruction.models import Contour, LineSegment, Point2D

REQUESTED_MARKS = (0x0300, 0x0301, 0x0303, 0x0306, 0x0309, 0x031B, 0x0323)


def _square_contour(x0: float, y0: float, x1: float, y1: float) -> Contour:
    pts = [Point2D(x0, y0), Point2D(x1, y0), Point2D(x1, y1), Point2D(x0, y1)]
    segs = [LineSegment(pts[i], pts[(i + 1) % 4]) for i in range(4)]
    return Contour(segments=segs, is_hole=False, area_upem=(x1 - x0) * (y1 - y0))


def _regressed(cp: int, advance: float, lsb: float, ascent: float, descent: float) -> RegressedMetrics:
    bbox_w = 300.0
    return RegressedMetrics(
        code_point=cp,
        advance_width_upem=advance,
        lsb_upem=lsb,
        rsb_upem=advance - lsb - bbox_w,
        ascent_upem=ascent,
        descent_upem=descent,
        bbox_upem=(lsb, ascent - 400.0, lsb + bbox_w, ascent),
        regression_residual=0.01,
    )


def test_requested_marks_are_combining_category():
    for cp in REQUESTED_MARKS:
        assert is_combining_mark(cp), f"U+{cp:04X} must classify as combining"
    assert not is_combining_mark(0x41)  # 'A'
    assert not is_combining_mark(0x20)  # space


def test_zero_advance_mark_structural_check_passes():
    """Zero advance + ink straddling the origin is VALID for marks."""
    contours = [_square_contour(-150.0, 500.0, 150.0, 700.0)]
    regressed = _regressed(0x0300, advance=0.0, lsb=-150.0, ascent=750.0, descent=0.0)
    ok, reasons = structural_check(contours, regressed, fit_residual_upem=0.5, observed_ink_area_upem=60000.0)
    assert ok, reasons
    assert "BBOX_X_OUT_OF_RANGE" not in reasons


def test_non_mark_zero_advance_bbox_still_fail_closed():
    """Fail-closed preserved: non-mark glyphs keep advance-bound X checks."""
    contours = [_square_contour(400.0, 0.0, 700.0, 400.0)]
    regressed = _regressed(0x41, advance=0.0, lsb=0.0, ascent=700.0, descent=0.0)
    ok, reasons = structural_check(contours, regressed, fit_residual_upem=0.5)
    assert not ok
    assert "BBOX_X_OUT_OF_RANGE" in reasons


def test_degenerate_outlines_still_fail_closed_for_marks():
    """Marks with degenerate geometry remain FAILED (fail-closed kept)."""
    # Empty outline fails closed at the model integrity layer: a non-space
    # glyph (mark included) without contours is never publishable.
    with pytest.raises(ValueError, match="at least one contour"):
        CalibratedGlyph(
            code_point=0x0301,
            character=chr(0x0301),
            advance_width_upem=0.0,
            lsb_upem=0.0,
            rsb_upem=0.0,
            ascent_upem=700.0,
            descent_upem=0.0,
            bounding_box_upem=(0.0, 500.0, 100.0, 700.0),
            contours=[],
            observation_fingerprints=("a" * 64,),
        ).validate()
    regressed = _regressed(0x0301, advance=0.0, lsb=0.0, ascent=700.0, descent=0.0)
    # Degenerate segment (< 1e-4 length) fails closed even for marks.
    tiny = Contour(
        segments=[
            LineSegment(Point2D(0.0, 0.0), Point2D(1e-6, 0.0)),
            LineSegment(Point2D(1e-6, 0.0), Point2D(0.0, 100.0)),
            LineSegment(Point2D(0.0, 100.0), Point2D(0.0, 0.0)),
        ],
        is_hole=False,
    )
    ok2, reasons2 = structural_check([tiny], regressed, fit_residual_upem=0.5)
    assert not ok2
    assert "DEGENERATE_SEGMENT" in reasons2


def test_negative_advance_still_rejected():
    with pytest.raises(ValueError, match="Negative advance"):
        CalibratedGlyph(
            code_point=0x0300,
            character=chr(0x0300),
            advance_width_upem=-1.0,
            lsb_upem=0.0,
            rsb_upem=0.0,
            ascent_upem=700.0,
            descent_upem=0.0,
            bounding_box_upem=(0.0, 500.0, 100.0, 700.0),
            contours=[_square_contour(0.0, 500.0, 100.0, 700.0)],
            observation_fingerprints=("a" * 64,),
        ).validate()


def test_mark_cell_geometry_captures_left_ink():
    """Cell planning extends the pen offset by the OBSERVED left extent."""
    extra = mark_extra_left_px(-250.0, 1024)
    assert extra == pytest.approx(256.0)
    adv_px = mark_effective_advance_px(0.0, -250.0, 500.0, 1024)  # right = -250+500
    assert adv_px == pytest.approx(256.0)
    w, h = cell_dimensions(adv_px, 800.0, 200.0, 1024, extra_left_px=extra)
    assert w == int(256.0 + 256.0 + 2 * CELL_PAD_X_PX + 0.9999)
    assert pen_left_px(extra) == CELL_PAD_X_PX + 256
    pages = estimate_cell_plan(
        [0x0300], {0x0300: adv_px}, {0x0300: 800.0}, {0x0300: 200.0},
        1024, 96, 128, extra_left_px={0x0300: extra},
    )
    assert pages[0].cells[0].pen_left_px == CELL_PAD_X_PX + 256


def _mark_glyph(cp: int) -> CalibratedGlyph:
    return CalibratedGlyph(
        code_point=cp,
        character=chr(cp),
        # Zero advance is VALID for Unicode combining marks (R2).
        advance_width_upem=0.0,
        lsb_upem=0.0,
        rsb_upem=0.0,
        ascent_upem=750.0,
        descent_upem=0.0,
        bounding_box_upem=(0.0, 500.0, 200.0, 750.0),
        contours=[_square_contour(0.0, 500.0, 200.0, 750.0)],
        confidence=1.0,
        observation_fingerprints=(f"{cp:064x}",),
        anchors=(("mark", 100.0, 500.0),),
    )


def test_zero_advance_marks_survive_model_build_and_cmap(tmp_path: Path):
    """All 7 requested marks survive GlyphModel -> TTF/OTF cmap with zero
    advance; the count of mark glyphs in the output is asserted."""
    glyphs: dict[int, CalibratedGlyph] = {}
    for cp in (0x41, 0x61, 0x20):
        if cp == 0x20:
            g = CalibratedGlyph(
                code_point=cp, character=" ", advance_width_upem=250.0,
                lsb_upem=0.0, rsb_upem=250.0, ascent_upem=750.0, descent_upem=0.0,
                bounding_box_upem=(0.0, 0.0, 0.0, 0.0), contours=[],
                confidence=1.0, observation_fingerprints=(f"{cp:064x}",),
            )
        else:
            g = CalibratedGlyph(
                code_point=cp, character=chr(cp), advance_width_upem=600.0,
                lsb_upem=10.0, rsb_upem=10.0, ascent_upem=750.0, descent_upem=0.0,
                bounding_box_upem=(10.0, 0.0, 590.0, 750.0),
                contours=[_square_contour(10.0, 0.0, 590.0, 750.0)],
                confidence=1.0, observation_fingerprints=(f"{cp:064x}",),
            )
        glyphs[cp] = g
    for cp in REQUESTED_MARKS:
        glyphs[cp] = _mark_glyph(cp)

    model = assemble_font_model(
        family_name="Mark Coverage Proof",
        style_name="Regular",
        reference_id="b" * 64,
        style_id="regular",
        glyphs=glyphs,
        font_ascent_upem=800.0,
        font_descent_upem=-200.0,
        config_hash="c" * 64,
        browser_version="test",
        fit_observations_count=len(glyphs),
    )
    builder = AtlasFontBuilder("Mark Coverage Proof", "Regular")
    builder.bind_model(model)
    result = builder.build_final(model, tmp_path / "build")
    assert result.ttf is not None and result.otf is not None

    from fontTools.ttLib import TTFont

    for artifact in (result.ttf, result.otf):
        font = TTFont(artifact.file_path)
        try:
            cmap = font.getBestCmap() or {}
            present = [cp for cp in REQUESTED_MARKS if cp in cmap]
            assert len(present) == len(REQUESTED_MARKS), (
                f"{artifact.format} cmap missing marks: "
                f"{[hex(c) for c in REQUESTED_MARKS if c not in cmap]}"
            )
            hmtx = font["hmtx"]
            order = font.getGlyphOrder()
            mark_glyph_count = 0
            for cp in REQUESTED_MARKS:
                name = cmap[cp]
                assert name in order
                adv, _lsb = hmtx.metrics[name]
                assert adv == 0, f"{artifact.format} mark U+{cp:04X} advance != 0"
                mark_glyph_count += 1
            assert mark_glyph_count == len(REQUESTED_MARKS)
        finally:
            font.close()


# ----------------------------------------------------------------------
# End-to-end: the integrated pipeline keeps the requested marks through
# metrics -> cell plan -> geometry -> model -> TTF/OTF cmap.
# ----------------------------------------------------------------------

FIXTURE_FONT = (
    Path(__file__).parent.parent
    / "benchmark_data"
    / "ground_truth"
    / "BeVietnamPro-Regular.ttf"
)


def test_requested_marks_survive_full_pipeline_and_output_cmap(tmp_path: Path):
    """E-00024 correction proof: the 7 requested standalone combining marks
    are NOT FAILED_GLYPH for zero advance; they survive into GlyphModel,
    cmap and TTF/OTF outputs (cmap entries + mark glyph count asserted)."""
    import asyncio
    import time

    from atlas.cache import AtlasCacheStore, AtlasCheckpointStore
    from atlas.local_fixture import LocalFontMetricsProvider, LocalFontRasterProvider
    from atlas.pipeline import AtlasStyleSpec, AtlasUltraPipeline
    from atlas.policy import AtlasRuntimeDefaults

    if not FIXTURE_FONT.exists():
        pytest.skip("ground-truth binary absent")

    cps = sorted(
        [0x20, 0x41, 0x61, 0x65, 0x6F, 0x75, 0xE0, 0xE1] + list(REQUESTED_MARKS)
    )
    spec = AtlasStyleSpec(
        source_url="repro://pipeline-marks",
        family_name="Pipeline Mark Proof",
        style_name="Regular",
        style_id="regular",
        mode="ORIGINAL",
        code_points=cps,
    )
    pipeline = AtlasUltraPipeline(
        spec=spec,
        runtime=AtlasRuntimeDefaults(),
        metrics_provider=LocalFontMetricsProvider(FIXTURE_FONT),
        raster_provider=LocalFontRasterProvider(FIXTURE_FONT),
        cache=AtlasCacheStore(tmp_path / "cache"),
        checkpoint_store=AtlasCheckpointStore(tmp_path / "ckpt"),
        deadline=time.monotonic() + 240,
    )
    result = asyncio.run(pipeline.run())

    # None of the requested marks failed solely for zero advance.
    for cp in REQUESTED_MARKS:
        assert cp not in result.evidence.failed_glyph_ids, f"U+{cp:04X} FAILED"
        assert cp in result.frozen_glyphs, f"U+{cp:04X} not frozen"
        glyph = result.frozen_glyphs[cp]
        assert glyph.advance_width_upem == 0.0
        assert glyph.contours, f"U+{cp:04X} lost its outline"

    # Coverage proven present in BOTH built outputs (cmap + mark count).
    from fontTools.ttLib import TTFont

    for artifact_path in (result.ttf_path, result.otf_path):
        font = TTFont(artifact_path)
        try:
            cmap = font.getBestCmap() or {}
            present = [cp for cp in REQUESTED_MARKS if cp in cmap]
            assert len(present) == len(REQUESTED_MARKS)
            hmtx = font["hmtx"]
            mark_count = 0
            for cp in REQUESTED_MARKS:
                name = cmap[cp]
                adv, _lsb = hmtx.metrics[name]
                assert adv == 0
                mark_count += 1
            assert mark_count == len(REQUESTED_MARKS)
        finally:
            font.close()
    assert result.report["passed"] is True
