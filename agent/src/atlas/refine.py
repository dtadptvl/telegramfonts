"""Single refinement for failed glyphs (ADR-0004, U5).

Failed glyphs receive EXACTLY one refinement:
  add 1024@x=0.5,y=0 and 2048@x=0,y=0 -> merge alpha with the 1024@x=0,y=0
  observation -> SDF -> local contour/Bezier refit -> bounded local
  optimizer -> confidence check.
NO 512, NO 4096, NO x=0.25/0.75 phases, NO second tier, NO whole-font
optimizer, NO whole-font reconstruction retry - these are enforced as
structural constants below. After the single refinement the structurally
valid best candidate is accepted even with low visual confidence (recorded
as low-confidence glyph IDs in the validation report); structural/outline/
shaping/rendering invalidity remains FAILED_GLYPH.
"""
from __future__ import annotations

import math
import time

import numpy as np
import scipy.ndimage as ndi

from reconstruction.models import Contour, CubicSegment, LineSegment

from atlas.geometry import (
    MIN_IOU_LOW_CONFIDENCE,
    MIN_IOU_REFINED_ACCEPT,
    alpha_to_coverage,
    classify_topology,
    decode_alpha,
    fast_geometry_for_glyph,
    fit_polyline_to_segments,
    mask_iou,
    rasterize_segments_mask,
    signed_area,
    structural_check,
    _reverse_segment,
)
from atlas.models import CellMapping, GeometryEvidence, GlyphStatus, RegressedMetrics

# Hard structural boundaries of the single refinement (U5). Any attempt to
# schedule observations outside this set fails closed.
REFINED_SIZE_SET: frozenset[tuple[int, float, float]] = frozenset(
    {(1024, 0.0, 0.0), (1024, 0.5, 0.0), (2048, 0.0, 0.0)}
)
FORBIDDEN_REFINEMENT_SIZES: frozenset[int] = frozenset({512, 4096})
MAX_LOCAL_OPTIMIZER_ITERATIONS = 24
LOCAL_OPTIMIZER_STEP_PX = 0.75

# Merge weights over (base 1024@0,0 ; shifted 1024@0.5,0 ; 2048@0,0).
MERGE_WEIGHT_BASE = 0.5
MERGE_WEIGHT_SHIFTED = 0.25
MERGE_WEIGHT_DOUBLE = 0.25


class RefinementScheduleViolation(ValueError):
    """A refinement observation outside the allowed set was requested."""

    def __init__(self, size_px: int, phase_x: float, phase_y: float) -> None:
        super().__init__(
            "REFINEMENT_SCHEDULE_VIOLATION:"
            f" size={size_px} phase=({phase_x},{phase_y})"
        )


def validate_refinement_schedule(observations: list[tuple[int, float, float]]) -> None:
    """Fail closed on any observation outside the single-refinement set."""
    for size_px, px, py in observations:
        key = (int(size_px), round(float(px), 4), round(float(py), 4))
        if key not in REFINED_SIZE_SET or int(size_px) in FORBIDDEN_REFINEMENT_SIZES:
            raise RefinementScheduleViolation(int(size_px), float(px), float(py))


def merge_alpha_observations(
    base_png: bytes,
    shifted_png: bytes | None,
    double_png: bytes | None,
) -> np.ndarray:
    """Merge the refinement observations onto the base 1024@0,0 grid.

    The shifted observation carries a 0.5px phase in the base grid; the
    2048 observation is box-downsampled 2x onto the same grid. All
    resampling is native NumPy (never per-pixel Python).
    """
    base = alpha_to_coverage(decode_alpha(base_png))
    h, w = base.shape
    acc = base * MERGE_WEIGHT_BASE
    weight = MERGE_WEIGHT_BASE

    if shifted_png is not None:
        shifted = alpha_to_coverage(decode_alpha(shifted_png))
        if shifted.shape == (h, w):
            # Subpixel alignment: the 0.5px shift is realized by averaging
            # the shifted plane with its 1px neighbor (deterministic).
            padded = np.zeros_like(shifted)
            padded[:, 1:] = shifted[:, :-1]
            aligned = 0.5 * shifted + 0.5 * padded
            acc += aligned * MERGE_WEIGHT_SHIFTED
            weight += MERGE_WEIGHT_SHIFTED

    if double_png is not None:
        double = alpha_to_coverage(decode_alpha(double_png))
        dh, dw = double.shape
        if dh >= 2 * h and dw >= 2 * w:
            crop = double[: 2 * h, : 2 * w].astype(np.float64)
            down = crop.reshape(h, 2, w, 2).mean(axis=(1, 3))
            acc += down.astype(np.float32) * MERGE_WEIGHT_DOUBLE
            weight += MERGE_WEIGHT_DOUBLE

    return acc / max(weight, 1e-9)


def coverage_sdf(coverage: np.ndarray) -> np.ndarray:
    """Signed distance field (px, positive inside ink) from merged coverage."""
    mask = coverage >= 0.5
    if not np.any(mask):
        return -np.ones_like(coverage, dtype=np.float32) * 100.0
    d_in = ndi.distance_transform_edt(mask)
    d_out = ndi.distance_transform_edt(~mask)
    sdf = (d_in - d_out).astype(np.float32)
    boundary = (coverage > 0.05) & (coverage < 0.95)
    sdf[boundary] = coverage[boundary] - 0.5
    return sdf


def bounded_local_optimizer(
    contours: list[Contour],
    mapping: CellMapping,
    sdf: np.ndarray,
    max_iterations: int = MAX_LOCAL_OPTIMIZER_ITERATIONS,
    step_px: float = LOCAL_OPTIMIZER_STEP_PX,
) -> list[Contour]:
    """Bounded LOCAL optimizer: nudges control points toward the SDF zero
    crossing. Scope is the single glyph's control points only; iteration
    count is bounded; never a whole-font optimizer.
    """
    if not contours:
        return contours
    h, w = sdf.shape
    gy, gx = np.gradient(sdf)

    def grad_px(x_px: float, y_px: float) -> tuple[float, float]:
        xi = min(max(int(round(x_px)), 0), w - 1)
        yi = min(max(int(round(y_px)), 0), h - 1)
        return float(gx[yi, xi]), float(gy[yi, xi])

    for _ in range(max_iterations):
        moved = False
        for contour in contours:
            for seg in contour.segments:
                if isinstance(seg, LineSegment):
                    continue
                for name in ("p1", "p2"):
                    pt = getattr(seg, name)
                    x_px, y_px = mapping.upem_to_px(pt.x, pt.y)
                    if not (0 <= x_px < w and 0 <= y_px < h):
                        continue
                    xi = min(max(int(round(x_px)), 0), w - 1)
                    yi = min(max(int(round(y_px)), 0), h - 1)
                    sdf_val = float(sdf[yi, xi])
                    if abs(sdf_val) < 0.35:
                        continue
                    gxp, gyp = grad_px(x_px, y_px)
                    norm = math.hypot(gxp, gyp)
                    if norm < 1e-6:
                        continue
                    direction = 1.0 if sdf_val > 0 else -1.0
                    dx = -direction * gxp / norm * step_px
                    dy = -direction * gyp / norm * step_px
                    new_x_px = x_px + dx
                    new_y_px = y_px + dy
                    nx, ny = mapping.px_to_upem(new_x_px, new_y_px)
                    from reconstruction.models import Point2D

                    setattr(seg, name, Point2D(nx, ny))
                    moved = True
        if not moved:
            break
    return contours


def refine_glyph(
    base_png: bytes,
    shifted_png: bytes | None,
    double_png: bytes | None,
    mapping: CellMapping,
    regressed: RegressedMetrics,
    cell_w: int,
    cell_h: int,
) -> tuple[GeometryEvidence, list[Contour]]:
    """The single refinement for one failed glyph (U5)."""
    t0 = time.perf_counter()
    cp = regressed.code_point
    validate_refinement_schedule(
        [
            (1024, 0.0, 0.0),
            (1024, 0.5, 0.0) if shifted_png is not None else (1024, 0.0, 0.0),
            (2048, 0.0, 0.0) if double_png is not None else (1024, 0.0, 0.0),
        ]
    )

    merged = merge_alpha_observations(base_png, shifted_png, double_png)
    sdf = coverage_sdf(merged)

    from atlas.geometry import marching_squares_contours

    loops = marching_squares_contours(merged)
    if not loops:
        return (
            GeometryEvidence(
                cp,
                GlyphStatus.FAILED_GLYPH,
                reasons=("REFINEMENT_NO_CONTOURS",),
                time_ms=(time.perf_counter() - t0) * 1000.0,
            ),
            [],
        )

    topo = classify_topology(loops, merged, mapping)
    contours: list[Contour] = []
    worst_residual = 0.0
    for poly_px, is_hole, parent_index in topo:
        upem_pts = np.asarray(
            [mapping.px_to_upem(float(x), float(y)) for (x, y) in poly_px],
            dtype=np.float64,
        )
        segments, residual = fit_polyline_to_segments(upem_pts)
        if not segments:
            continue
        area = signed_area(upem_pts)
        if not is_hole and area < 0:
            segments = [_reverse_segment(s) for s in segments[::-1]]
        elif is_hole and area > 0:
            segments = [_reverse_segment(s) for s in segments[::-1]]
        contours.append(
            Contour(
                segments=segments,
                is_hole=is_hole,
                parent_index=parent_index,
                area_upem=abs(area),
            )
        )
        worst_residual = max(worst_residual, residual)

    if not contours:
        return (
            GeometryEvidence(
                cp,
                GlyphStatus.FAILED_GLYPH,
                reasons=("REFINEMENT_NO_FITTABLE_CONTOURS",),
                time_ms=(time.perf_counter() - t0) * 1000.0,
            ),
            [],
        )

    contours = bounded_local_optimizer(contours, mapping, sdf)

    observed_ink = merged >= 0.5
    observed_ink_area_upem = float(np.count_nonzero(observed_ink)) * (
        mapping.upem_per_px ** 2
    )
    structure_ok, reasons = structural_check(
        contours, regressed, worst_residual, observed_ink_area_upem
    )
    fit_mask = rasterize_segments_mask(contours, mapping, cell_w, cell_h)
    iou = mask_iou(observed_ink, fit_mask)

    elapsed = (time.perf_counter() - t0) * 1000.0

    if not structure_ok:
        # Structural/outline invalidity stays FAILED_GLYPH even after the
        # single refinement (never retried at whole-font scope).
        return (
            GeometryEvidence(
                cp,
                GlyphStatus.FAILED_GLYPH,
                iou=iou,
                structure_ok=False,
                metrics_residual=regressed.regression_residual,
                reasons=tuple(reasons),
                time_ms=elapsed,
            ),
            contours,
        )

    if iou < MIN_IOU_REFINED_ACCEPT:
        return (
            GeometryEvidence(
                cp,
                GlyphStatus.FAILED_GLYPH,
                iou=iou,
                structure_ok=True,
                metrics_residual=regressed.regression_residual,
                reasons=(f"REFINED_IOU_{iou:.4f}_BELOW_{MIN_IOU_REFINED_ACCEPT:.2f}",),
                time_ms=elapsed,
            ),
            contours,
        )

    # Structurally valid best candidate: accepted even with low visual
    # confidence; recorded as a low-confidence glyph ID in the report.
    low_confidence = iou < MIN_IOU_LOW_CONFIDENCE
    return (
        GeometryEvidence(
            cp,
            GlyphStatus.REFINED_PASS,
            iou=iou,
            structure_ok=True,
            metrics_residual=regressed.regression_residual,
            reasons=tuple(reasons),
            low_confidence=low_confidence,
            time_ms=elapsed,
        ),
        contours,
    )
