"""Benchmark-Gated Geometry Optimizer (MAX Pipeline F).

Refines cubic Bézier master outline geometry strictly against cached observable evidence
(multi-resolution fused SDF zero-crossings) using bounded deterministic local optimization.
No ground-truth reference font binary may be read or utilized by this optimizer.
"""
from __future__ import annotations

import logging
import math
from typing import Any
import numpy as np
import scipy.ndimage as ndi
import scipy.optimize as opt

from reconstruction.models import (
    Contour,
    CubicSegment,
    LineSegment,
    Point2D,
    ReconstructedGlyph,
    ReconstructionConfig,
)

logger = logging.getLogger("telegramfonts.agent.reconstruction.geometry_optimizer")


class MaxGeometryOptimizer:
    """Bounded deterministic geometry optimizer for cubic Bézier outline refinement."""

    def __init__(
        self,
        config: ReconstructionConfig | None = None,
        max_nudge_upem: float = 3.0,
        regularization_weight: float = 0.02,
        samples_per_segment: int = 11,
        max_iterations: int = 20,
    ) -> None:
        self.config = config or ReconstructionConfig()
        self.max_nudge_upem = max_nudge_upem
        self.regularization_weight = regularization_weight
        self.samples_per_segment = samples_per_segment
        self.max_iterations = max_iterations

        # Precompute Bernstein basis polynomials for cubic Bézier sampling
        t_vals = np.linspace(0.0, 1.0, self.samples_per_segment, dtype=np.float64)
        u_vals = 1.0 - t_vals
        self._b0 = (u_vals**3)[:, None]
        self._b1 = (3.0 * u_vals**2 * t_vals)[:, None]
        self._b2 = (3.0 * u_vals * t_vals**2)[:, None]
        self._b3 = (t_vals**3)[:, None]

    def optimize_glyph(
        self,
        glyph: ReconstructedGlyph,
        fused_sdf: np.ndarray,
        x_coords: np.ndarray,
        y_coords: np.ndarray,
    ) -> ReconstructedGlyph:
        """Optimize cubic Bézier control points of a glyph against cached observation SDF.
        
        Args:
            glyph: ReconstructedGlyph with initial master cubic Bézier contours.
            fused_sdf: Continuous 2D UPEM Signed Distance Field from cached observations.
            x_coords: 1D array of X coordinates corresponding to grid columns.
            y_coords: 1D array of Y coordinates corresponding to grid rows.
            
        Returns:
            Optimized ReconstructedGlyph with improved fidelity and strictly preserved topology/metrics.
        """
        if not glyph.contours:
            return glyph

        x_span = float(x_coords[-1] - x_coords[0])
        y_span = float(y_coords[-1] - y_coords[0])
        if x_span <= 1e-6 or y_span <= 1e-6:
            return glyph

        x_min_grid = float(x_coords[0])
        y_min_grid = float(y_coords[0])
        n_cols = len(x_coords)
        n_rows = len(y_coords)

        def sample_sdf(pts: np.ndarray) -> np.ndarray:
            """Sample continuous SDF at given UPEM (x, y) coordinates."""
            u = (pts[:, 0] - x_min_grid) / x_span * (n_cols - 1)
            v = (pts[:, 1] - y_min_grid) / y_span * (n_rows - 1)
            return ndi.map_coordinates(fused_sdf, [v, u], order=1, mode="nearest")

        optimized_contours: list[Contour] = []
        any_improvement = False

        for contour in glyph.contours:
            if not contour.segments:
                optimized_contours.append(contour)
                continue

            optimized_segments = []
            for seg in contour.segments:
                if isinstance(seg, LineSegment):
                    optimized_segments.append(seg)
                    continue

                # Segment is CubicSegment(p0, p1, p2, p3)
                p0 = np.array([seg.p0.x, seg.p0.y], dtype=np.float64)
                p1_init = np.array([seg.p1.x, seg.p1.y], dtype=np.float64)
                p2_init = np.array([seg.p2.x, seg.p2.y], dtype=np.float64)
                p3 = np.array([seg.p3.x, seg.p3.y], dtype=np.float64)

                init_params = np.concatenate([p1_init, p2_init])

                def segment_loss(params: np.ndarray) -> float:
                    p1_c = params[:2]
                    p2_c = params[2:]
                    curve_pts = (
                        self._b0 * p0
                        + self._b1 * p1_c
                        + self._b2 * p2_c
                        + self._b3 * p3
                    )
                    sdf_vals = sample_sdf(curve_pts)
                    fit_loss = float(np.mean(sdf_vals**2))
                    # Regularization penalty to prevent excessive elongation of control handles
                    reg_penalty = float(
                        self.regularization_weight
                        * (np.sum((p1_c - p1_init) ** 2) + np.sum((p2_c - p2_init) ** 2))
                    )
                    return fit_loss + reg_penalty

                initial_error = segment_loss(init_params)

                # Bounded box constraints
                nudge = self.max_nudge_upem
                bounds = [
                    (p1_init[0] - nudge, p1_init[0] + nudge),
                    (p1_init[1] - nudge, p1_init[1] + nudge),
                    (p2_init[0] - nudge, p2_init[0] + nudge),
                    (p2_init[1] - nudge, p2_init[1] + nudge),
                ]

                res = opt.minimize(
                    segment_loss,
                    init_params,
                    method="L-BFGS-B",
                    bounds=bounds,
                    options={"maxiter": self.max_iterations, "ftol": 1e-5},
                )

                if res.success and res.fun < initial_error - 1e-4:
                    p1_opt = Point2D(float(res.x[0]), float(res.x[1]))
                    p2_opt = Point2D(float(res.x[2]), float(res.x[3]))
                    optimized_segments.append(
                        CubicSegment(p0=seg.p0, p1=p1_opt, p2=p2_opt, p3=seg.p3)
                    )
                    any_improvement = True
                else:
                    # Fail-safe / No change fallback if optimizer did not measurably improve fit
                    optimized_segments.append(seg)

            optimized_contours.append(
                Contour(
                    segments=optimized_segments,
                    is_hole=contour.is_hole,
                    parent_index=contour.parent_index,
                    area_upem=contour.area_upem,
                )
            )

        return ReconstructedGlyph(
            code_point=glyph.code_point,
            character=glyph.character,
            advance_width_upem=glyph.advance_width_upem,
            lsb_upem=glyph.lsb_upem,
            rsb_upem=glyph.rsb_upem,
            ascent_upem=glyph.ascent_upem,
            descent_upem=glyph.descent_upem,
            contours=optimized_contours if any_improvement else glyph.contours,
            bounding_box_upem=glyph.bounding_box_upem,
            reconstruction_time_ms=glyph.reconstruction_time_ms,
        )
