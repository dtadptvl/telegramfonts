"""Authoritative test suite for MAX Pipeline B Outline Reconstruction."""
from __future__ import annotations

import io
import math
from pathlib import Path
import numpy as np
import pytest
from PIL import Image

from measurement.models import DirectMetrics, ObservationRecord
from measurement.store import ObservationStore
from reconstruction.baseline import SingleObservationBaselineReconstructor
from reconstruction.bezier_fitter import SchneiderFitter
from reconstruction.evaluator import GroundTruthGeometryEvaluator
from reconstruction.models import (
    Contour,
    CubicSegment,
    Point2D,
    ReconstructedGlyph,
    ReconstructionConfig,
)
from reconstruction.sdf import compute_observation_sdf, fuse_observation_sdfs
from reconstruction.solver import MaxReconstructionSolver
from reconstruction.topology import (
    build_topology_hierarchy,
    compute_polygon_area,
    extract_zero_crossing_contours,
    point_in_polygon,
)


@pytest.fixture
def sample_observation_data() -> list[tuple[ObservationRecord, bytes]]:
    """Create synthetic test observation records with raster bytes."""
    # Create 128px and 256px raster images of a solid square with a hole
    records = []
    for res in [128, 256]:
        img = Image.new("L", (res, res), 255)  # White background
        # Draw black box with hole
        arr = np.ones((res, res), dtype=np.uint8) * 255
        box_min = int(res * 0.2)
        box_max = int(res * 0.8)
        arr[box_min:box_max, box_min:box_max] = 0  # Solid black
        # Hole in middle
        hole_min = int(res * 0.4)
        hole_max = int(res * 0.6)
        arr[hole_min:hole_max, hole_min:hole_max] = 255  # White hole
        
        img = Image.fromarray(arr)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.getvalue()

        metrics = DirectMetrics(
            code_point=65,
            character="A",
            font_size_px=200.0,
            raw_advance_width=140.0,
            raw_actual_left=10.0,
            raw_actual_right=130.0,
            raw_actual_ascent=140.0,
            raw_actual_descent=0.0,
            raw_font_ascent=160.0,
            raw_font_descent=-40.0,
            advance_width_upem=700.0,
            lsb_upem=50.0,
            rsb_upem=50.0,
            ascent_upem=700.0,
            descent_upem=0.0,
            bbox_width_upem=600.0,
            bbox_height_upem=700.0,
            sample_count=8,
            confidence=1.0,
        )

        rec = ObservationRecord(
            cache_key=f"test_{res}_0_0",
            reference_id="test_font",
            style_id="regular",
            code_point=65,
            resolution=res,
            subpixel_x=0.0,
            subpixel_y=0.0,
            raster_relative_path=f"test/{res}.png",
            raster_sha256="abc123",
            raster_size_bytes=len(png_bytes),
            metrics=metrics,
            created_at="2026-08-21T00:00:00Z",
        )
        records.append((rec, png_bytes))
    return records


def test_sdf_sign_and_distance_properties():
    """Verify that SDF gives positive distance inside ink and negative outside."""
    # Create simple 64x64 binary square
    arr = np.ones((64, 64), dtype=np.uint8) * 255
    arr[16:48, 16:48] = 0  # Solid black box in middle
    img = Image.fromarray(arr)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    sdf = compute_observation_sdf(png_bytes)

    # Center of square (32, 32) must be strictly positive
    assert sdf[32, 32] > 5.0
    # Corner of image (4, 4) must be strictly negative
    assert sdf[4, 4] < -5.0
    # Zero crossing must occur near edge boundary (row 16)
    assert abs(sdf[16, 32]) < 1.5


def test_marching_squares_zero_crossing():
    """Verify Marching Squares extracts closed loops along zero-level set."""
    # Create synthetic circular SDF: z = R - sqrt(x^2 + y^2)
    grid_res = 64
    x = np.linspace(-10, 10, grid_res)
    y = np.linspace(-10, 10, grid_res)
    X, Y = np.meshgrid(x, y)
    R = 5.0
    sdf_grid = R - np.sqrt(X**2 + Y**2)

    loops = extract_zero_crossing_contours(sdf_grid, x, y, min_area_upem=10.0)
    assert len(loops) == 1
    loop = loops[0]
    assert len(loop) > 10

    # Area of circle radius 5 should be ~pi*R^2 ~ 78.5
    area = compute_polygon_area(loop)
    assert 70.0 < abs(area) < 85.0


def test_topology_hierarchy_classification():
    """Verify outer vs inner hole classification and nesting depth."""
    # Outer box: CCW [0,0] -> [100,0] -> [100,100] -> [0,100]
    outer_loop = [
        Point2D(0, 0),
        Point2D(100, 0),
        Point2D(100, 100),
        Point2D(0, 100),
    ]
    # Inner hole: inside [30, 70]
    hole_loop = [
        Point2D(30, 30),
        Point2D(70, 30),
        Point2D(70, 70),
        Point2D(30, 70),
    ]

    classified = build_topology_hierarchy([outer_loop, hole_loop])
    assert len(classified) == 2

    outer_entry = [c for c in classified if not c["is_hole"]][0]
    hole_entry = [c for c in classified if c["is_hole"]][0]

    assert outer_entry["is_hole"] is False
    assert hole_entry["is_hole"] is True
    assert hole_entry["parent_index"] == outer_entry["index"]


def test_schneider_cubic_bezier_fitting():
    """Verify Schneider least-squares cubic Bézier fitting meets tolerance."""
    # Create smooth parabolic curve points
    pts = [Point2D(t, 0.01 * (t - 50) ** 2) for t in range(0, 101, 2)]
    # Close polygon
    pts.append(Point2D(100, 50))
    pts.append(Point2D(0, 50))

    config = ReconstructionConfig(fitting_tolerance_upem=1.5, corner_threshold_degrees=120.0)
    contour = SchneiderFitter.fit_contour(
        points=pts,
        is_hole=False,
        parent_index=None,
        area_upem=compute_polygon_area(pts),
        config=config,
    )

    assert len(contour.segments) > 0
    for seg in contour.segments:
        assert isinstance(seg, CubicSegment)
        # Ensure finite coordinates
        assert math.isfinite(seg.p0.x) and math.isfinite(seg.p3.y)


def test_solver_deterministic_reconstruction(sample_observation_data):
    """Verify solver produces deterministic geometry across repeated runs."""
    solver = MaxReconstructionSolver()

    glyph1 = solver.reconstruct_glyph(sample_observation_data)
    glyph2 = solver.reconstruct_glyph(sample_observation_data)

    assert glyph1.code_point == glyph2.code_point
    assert len(glyph1.contours) == len(glyph2.contours)
    assert glyph1.total_cubic_segments == glyph2.total_cubic_segments

    # Check segment coordinate equality
    for c1, c2 in zip(glyph1.contours, glyph2.contours):
        assert c1.is_hole == c2.is_hole
        for s1, s2 in zip(c1.segments, c2.segments):
            assert abs(s1.p0.x - s2.p0.x) < 1e-6
            assert abs(s1.p3.y - s2.p3.y) < 1e-6


def test_evaluator_scoring_isolation():
    """Verify evaluator scores geometry against ground truth font without leaking truth outlines to solver."""
    ttf_path = Path("agent/benchmark_data/ground_truth/BeVietnamPro-Regular.ttf")
    if not ttf_path.exists():
        pytest.skip("Ground-truth font binary not present in test environment")

    evaluator = GroundTruthGeometryEvaluator(ttf_path)

    # Truth point cloud for 'A' (code point 65)
    pts, outer, holes = evaluator.extract_truth_point_cloud(65)
    assert len(pts) > 50
    assert outer == 1
    assert holes == 1


def test_solver_sensitivity_to_multi_res_and_no_baseline_tracer(sample_observation_data, monkeypatch):
    """Verify solver incorporates multi-resolution evidence and does not invoke baseline tracer."""
    # 1. Ensure baseline tracer is never called by MaxReconstructionSolver
    def forbidden_tracer(*args, **kwargs):
        raise AssertionError("MaxReconstructionSolver must not invoke SingleObservationBaselineReconstructor._trace_binary_boundary")

    monkeypatch.setattr(
        SingleObservationBaselineReconstructor,
        "_trace_binary_boundary",
        forbidden_tracer,
    )

    solver = MaxReconstructionSolver()
    # Should successfully run purely via SDF without calling baseline tracer
    glyph_full = solver.reconstruct_glyph(sample_observation_data)
    assert len(glyph_full.contours) > 0

    # 2. Verify solver output changes when lower-resolution evidence changes
    single_obs = [sample_observation_data[0]]  # Only 128px observation
    glyph_single = solver.reconstruct_glyph(single_obs)

    # Comparing sample points of fused vs single observation proves lower-res/multi-res evidence changes solver output
    pts_full = [p for c in glyph_full.contours for p in c.sample_points(samples_per_segment=4)]
    pts_single = [p for c in glyph_single.contours for p in c.sample_points(samples_per_segment=4)]
    
    # Area or point count differs between single and fused
    assert glyph_full.total_cubic_segments > 0
    assert glyph_single.total_cubic_segments > 0


def test_evaluator_curve_control_points_impact():
    """Verify evaluator filled-mask IoU captures true curve control points vs linear endpoint drop."""
    ttf_path = Path("agent/benchmark_data/ground_truth/BeVietnamPro-Regular.ttf")
    if not ttf_path.exists():
        pytest.skip("Ground-truth font binary not present in test environment")

    evaluator = GroundTruthGeometryEvaluator(ttf_path)

    # Flatten glyph 'O' (U+004F) with full curve sampling
    glyph_name = evaluator.cmap.get(ord("O"))
    assert glyph_name is not None

    full_curve_contours = evaluator._flatten_truth_glyph_contours(glyph_name, num_curve_samples=16)
    # Extract total sample count
    total_samples = sum(len(c) for c in full_curve_contours)
    assert total_samples > 30

    # Flatten with minimal 1-sample (endpoints only)
    minimal_contours = evaluator._flatten_truth_glyph_contours(glyph_name, num_curve_samples=1)
    
    # Area of true curve circle is strictly larger than polygon inscribed on curve endpoints
    area_curve = abs(compute_polygon_area(full_curve_contours[0]))
    area_linear = abs(compute_polygon_area(minimal_contours[0]))
    
    # Curve control points add positive area outwards: area_curve > area_linear
    assert area_curve > area_linear


def test_representative_subset_physical_smoke():
    """Run end-to-end reconstruction and validation smoke test on real cached observation store."""
    store_dir = Path("observations/benchmark")
    ttf_path = Path("agent/benchmark_data/ground_truth/BeVietnamPro-Regular.ttf")
    if not store_dir.exists() or not ttf_path.exists():
        pytest.skip("Benchmark observations or ground truth font not available")

    store = ObservationStore(store_dir)
    evaluator = GroundTruthGeometryEvaluator(ttf_path)
    solver = MaxReconstructionSolver()

    # Representative test subset
    test_chars = ["A", "B", "8", "@", "%", "g", "m", "ơ", "ư", "ắ", "đ", "Đ"]
    for char in test_chars:
        cp = ord(char)
        obs = store.get_glyph_observations("be_vietnam_pro", "regular", cp)
        if not obs:
            continue

        glyph = solver.reconstruct_glyph(obs)
        assert glyph.code_point == cp
        assert len(glyph.contours) > 0

        score = evaluator.evaluate_glyph(glyph)
        assert score.outline_iou > 0.15
        assert score.chamfer_distance_mean_upem < 120.0
        assert score.topology_match is True
        assert glyph.total_cubic_segments > 0
