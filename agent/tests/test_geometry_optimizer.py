"""Authoritative test suite for Benchmark-Gated Geometry Optimizer (Issue #45)."""
from __future__ import annotations

import copy
from pathlib import Path
import numpy as np
import pytest

from measurement.models import DirectMetrics, ObservationRecord
from measurement.store import ObservationStore
from reconstruction.geometry_optimizer import MaxGeometryOptimizer
from reconstruction.models import (
    Contour,
    CubicSegment,
    LineSegment,
    Point2D,
    ReconstructedGlyph,
    ReconstructionConfig,
)
from reconstruction.sdf import fuse_observation_sdfs
from reconstruction.solver import MaxReconstructionSolver


def _create_mock_glyph() -> ReconstructedGlyph:
    """Create a mock reconstructed glyph with standard cubic Bézier contours."""
    seg1 = CubicSegment(
        p0=Point2D(50.0, 0.0),
        p1=Point2D(50.0, 300.0),
        p2=Point2D(250.0, 700.0),
        p3=Point2D(350.0, 700.0),
    )
    seg2 = CubicSegment(
        p0=Point2D(350.0, 700.0),
        p1=Point2D(450.0, 700.0),
        p2=Point2D(650.0, 300.0),
        p3=Point2D(650.0, 0.0),
    )
    contour = Contour(
        segments=[seg1, seg2],
        is_hole=False,
        parent_index=None,
        area_upem=200000.0,
    )
    return ReconstructedGlyph(
        code_point=65,
        character="A",
        advance_width_upem=736.0,
        lsb_upem=50.0,
        rsb_upem=50.0,
        ascent_upem=740.0,
        descent_upem=0.0,
        contours=[contour],
        bounding_box_upem=(50.0, 0.0, 650.0, 740.0),
        reconstruction_time_ms=10.0,
    )


def test_geometry_optimizer_determinism():
    """Verify optimizer is strictly deterministic (identical inputs yield bit-for-bit identical outputs)."""
    glyph = _create_mock_glyph()
    x_coords = np.linspace(0.0, 700.0, 128, dtype=np.float32)
    y_coords = np.linspace(-50.0, 750.0, 128, dtype=np.float32)
    X_grid, Y_grid = np.meshgrid(x_coords, y_coords)
    # Synthetic SDF field: distance to an elliptical target
    fused_sdf = -np.sqrt(((X_grid - 350.0) / 300.0) ** 2 + ((Y_grid - 350.0) / 350.0) ** 2) + 1.0

    optimizer = MaxGeometryOptimizer(max_nudge_upem=4.0)

    res1 = optimizer.optimize_glyph(copy.deepcopy(glyph), fused_sdf, x_coords, y_coords)
    res2 = optimizer.optimize_glyph(copy.deepcopy(glyph), fused_sdf, x_coords, y_coords)

    assert len(res1.contours) == len(res2.contours)
    for c1, c2 in zip(res1.contours, res2.contours):
        assert len(c1.segments) == len(c2.segments)
        for s1, s2 in zip(c1.segments, c2.segments):
            assert isinstance(s1, CubicSegment) and isinstance(s2, CubicSegment)
            assert s1.p0 == s2.p0
            assert s1.p1 == s2.p1
            assert s1.p2 == s2.p2
            assert s1.p3 == s2.p3


def test_geometry_optimizer_no_truth_leakage():
    """Verify optimizer relies exclusively on cached observation SDF without ground-truth TTFont inspection."""
    import inspect
    import reconstruction.geometry_optimizer as go_mod

    src = inspect.getsource(go_mod)
    assert "TTFont" not in src
    assert "fontTools" not in src
    assert "freetype" not in src
    assert "ground_truth" not in src
    assert "reference_binary" not in src


def test_geometry_optimizer_cache_only_operation(tmp_path):
    """Verify optimizer operates directly on cached observation store items."""
    import io
    from PIL import Image

    store_dir = Path("observations/benchmark")
    if not (store_dir / "index.sqlite3").exists():
        store_dir = tmp_path / "obs_store"
        store = ObservationStore(store_dir)
        # Create a sample observation
        img = Image.new("L", (128, 128), 255)
        arr = np.ones((128, 128), dtype=np.uint8) * 255
        arr[25:100, 25:100] = 0
        img = Image.fromarray(arr)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.getvalue()

        metrics = DirectMetrics(
            code_point=65,
            character="A",
            font_size_px=200.0,
            raw_advance_width=147.2,
            raw_actual_left=8.0,
            raw_actual_right=139.0,
            raw_actual_ascent=148.0,
            raw_actual_descent=0.0,
            raw_font_ascent=148.0,
            raw_font_descent=0.0,
            advance_width_upem=736.0,
            lsb_upem=40.0,
            rsb_upem=41.0,
            ascent_upem=740.0,
            descent_upem=0.0,
            bbox_width_upem=655.0,
            bbox_height_upem=740.0,
        )
        rec = ObservationRecord(
            cache_key="font_a_reg_65_128_0_0",
            reference_id="font_a",
            style_id="reg",
            code_point=65,
            resolution=128,
            subpixel_x=0.0,
            subpixel_y=0.0,
            raster_relative_path="rasters/font_a_reg_65_128.png",
            raster_sha256="dummy_sha",
            raster_size_bytes=len(png_bytes),
            metrics=metrics,
            created_at="2026-08-21T00:00:00Z",
        )
        store.save_observation(rec, png_bytes)
        obs = store.get_glyph_observations("font_a", "reg", 65)
    else:
        store = ObservationStore(store_dir)
        obs = store.get_glyph_observations("be_vietnam_pro", "regular", 65)

    assert obs is not None
    assert len(obs) > 0

    config = ReconstructionConfig(enable_geometry_optimization=True)
    solver = MaxReconstructionSolver(config=config)
    glyph = solver.reconstruct_glyph(obs)

    assert glyph.code_point == 65
    assert len(glyph.contours) > 0
    assert glyph.advance_width_upem == 736.0


def test_geometry_optimizer_no_change_when_objective_cannot_improve():
    """Verify optimizer retains original segments (NO_CHANGE) when SDF objective cannot be improved."""
    glyph = _create_mock_glyph()
    x_coords = np.linspace(0.0, 700.0, 64, dtype=np.float32)
    y_coords = np.linspace(-50.0, 750.0, 64, dtype=np.float32)
    # Uniform flat SDF where gradients are zero (no improvement possible)
    fused_sdf = np.zeros((64, 64), dtype=np.float32)

    optimizer = MaxGeometryOptimizer(max_nudge_upem=3.0)
    res = optimizer.optimize_glyph(copy.deepcopy(glyph), fused_sdf, x_coords, y_coords)

    # Must be exact identity (no changes applied)
    assert res.contours == glyph.contours


def test_geometry_optimizer_bounded_nudge_enforcement():
    """Verify optimizer strictly respects max_nudge_upem bounds and does not displace points beyond limit."""
    glyph = _create_mock_glyph()
    x_coords = np.linspace(0.0, 700.0, 64, dtype=np.float32)
    y_coords = np.linspace(-50.0, 750.0, 64, dtype=np.float32)
    X_grid, Y_grid = np.meshgrid(x_coords, y_coords)
    # Strong synthetic gradient pulling far away
    fused_sdf = ((X_grid - 1000.0) + (Y_grid - 1000.0)).astype(np.float32)

    max_nudge = 2.5
    optimizer = MaxGeometryOptimizer(max_nudge_upem=max_nudge)
    res = optimizer.optimize_glyph(copy.deepcopy(glyph), fused_sdf, x_coords, y_coords)

    for c_orig, c_opt in zip(glyph.contours, res.contours):
        for s_orig, s_opt in zip(c_orig.segments, c_opt.segments):
            if isinstance(s_orig, CubicSegment) and isinstance(s_opt, CubicSegment):
                assert abs(s_opt.p1.x - s_orig.p1.x) <= max_nudge + 1e-4
                assert abs(s_opt.p1.y - s_orig.p1.y) <= max_nudge + 1e-4
                assert abs(s_opt.p2.x - s_orig.p2.x) <= max_nudge + 1e-4
                assert abs(s_opt.p2.y - s_orig.p2.y) <= max_nudge + 1e-4


def test_geometry_optimizer_preserves_topology_and_metrics():
    """Verify contour hierarchy, hole flags, and direct metric dimensions are strictly preserved."""
    glyph = _create_mock_glyph()
    x_coords = np.linspace(0.0, 700.0, 64, dtype=np.float32)
    y_coords = np.linspace(-50.0, 750.0, 64, dtype=np.float32)
    fused_sdf = np.random.RandomState(42).randn(64, 64).astype(np.float32)

    optimizer = MaxGeometryOptimizer(max_nudge_upem=2.0)
    res = optimizer.optimize_glyph(copy.deepcopy(glyph), fused_sdf, x_coords, y_coords)

    assert res.code_point == glyph.code_point
    assert res.character == glyph.character
    assert res.advance_width_upem == glyph.advance_width_upem
    assert res.lsb_upem == glyph.lsb_upem
    assert res.rsb_upem == glyph.rsb_upem
    assert res.ascent_upem == glyph.ascent_upem
    assert res.descent_upem == glyph.descent_upem
    assert len(res.contours) == len(glyph.contours)
    assert res.outer_contours_count == glyph.outer_contours_count
    assert res.holes_count == glyph.holes_count
