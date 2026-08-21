"""Authoritative MAX Pipeline B & F Master Outline Reconstruction Solver."""
from __future__ import annotations

import dataclasses
import logging
import time
from typing import Any

from measurement.models import ObservationRecord
from reconstruction.bezier_fitter import SchneiderFitter
from reconstruction.geometry_optimizer import MaxGeometryOptimizer
from reconstruction.models import (
    Contour,
    Point2D,
    ReconstructedGlyph,
    ReconstructionConfig,
)
from reconstruction.sdf import fuse_observation_sdfs
from reconstruction.topology import build_topology_hierarchy, extract_zero_crossing_contours

logger = logging.getLogger("telegramfonts.agent.reconstruction.solver")


class MaxReconstructionSolver:
    """Reconstructs continuous master cubic Bézier glyph outlines from multi-resolution SDF observations."""

    def __init__(self, config: ReconstructionConfig | None = None) -> None:
        self.config = config or ReconstructionConfig()

    def reconstruct_glyph(
        self,
        observations: list[tuple[ObservationRecord, bytes]],
    ) -> ReconstructedGlyph:
        """Execute authoritative continuous SDF fusion, topology extraction, and adaptive Schneider cubic Bézier fitting.
        
        Args:
            observations: List of (ObservationRecord, PNG bytes) from ObservationStore.
            
        Returns:
            ReconstructedGlyph with canonical cubic Bézier contours and UPEM metrics.
        """
        start_time = time.perf_counter()
        if not observations:
            raise ValueError("NO_OBSERVATIONS_SUPPLIED")

        first_rec = observations[0][0]
        code_point = first_rec.code_point
        metrics = first_rec.metrics

        # Step 1: Fuse all multi-resolution and subpixel observations into continuous UPEM SDF grid
        fused_sdf, x_coords, y_coords, bbox_upem = fuse_observation_sdfs(
            observations=observations,
            config=self.config,
        )

        # Step 2: Extract closed polygon contours along the continuous zero-level set (SDF == 0.0)
        raw_loops = extract_zero_crossing_contours(
            sdf_grid=fused_sdf,
            x_coords=x_coords,
            y_coords=y_coords,
            min_area_upem=self.config.min_contour_area_upem,
        )

        # Step 3: Classify topology hierarchy (outer boundaries vs inner hole cutouts and nesting depth)
        classified_contours = build_topology_hierarchy(raw_loops)

        # Step 4: Fit adaptive Schneider cubic Bézier curves to each classified contour
        fitted_contours: list[Contour] = []
        for c_data in classified_contours:
            contour = SchneiderFitter.fit_contour(
                points=c_data["points"],
                is_hole=c_data["is_hole"],
                parent_index=c_data["parent_index"],
                area_upem=c_data["area_upem"],
                config=self.config,
            )
            if contour.segments:
                fitted_contours.append(contour)

        initial_glyph = ReconstructedGlyph(
            code_point=code_point,
            character=metrics.character,
            advance_width_upem=metrics.advance_width_upem,
            lsb_upem=metrics.lsb_upem,
            rsb_upem=metrics.rsb_upem,
            ascent_upem=metrics.ascent_upem,
            descent_upem=metrics.descent_upem,
            contours=fitted_contours,
            bounding_box_upem=bbox_upem,
            reconstruction_time_ms=0.0,
        )

        # Step 5: Bounded Local Geometry Optimization (MAX Pipeline F)
        if self.config.enable_geometry_optimization:
            optimizer = MaxGeometryOptimizer(
                config=self.config,
                max_nudge_upem=self.config.max_optimization_nudge_upem,
            )
            final_glyph = optimizer.optimize_glyph(
                glyph=initial_glyph,
                fused_sdf=fused_sdf,
                x_coords=x_coords,
                y_coords=y_coords,
            )
        else:
            final_glyph = initial_glyph

        elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        return dataclasses.replace(final_glyph, reconstruction_time_ms=elapsed_ms)
