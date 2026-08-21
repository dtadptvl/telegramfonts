"""Authoritative MAX Pipeline B Master Outline Reconstruction Solver."""
from __future__ import annotations

import io
import logging
import math
import time
from typing import Any
import numpy as np
from PIL import Image

from measurement.models import ObservationRecord
from reconstruction.baseline import SingleObservationBaselineReconstructor
from reconstruction.bezier_fitter import SchneiderFitter
from reconstruction.models import (
    Contour,
    Point2D,
    ReconstructedGlyph,
    ReconstructionConfig,
)
from reconstruction.topology import build_topology_hierarchy, compute_polygon_area

logger = logging.getLogger("telegramfonts.agent.reconstruction.solver")


class MaxReconstructionSolver:
    """Reconstructs continuous master cubic Bézier glyph outlines from multi-resolution SDF observations."""

    def __init__(self, config: ReconstructionConfig | None = None) -> None:
        self.config = config or ReconstructionConfig()

    def reconstruct_glyph(
        self,
        observations: list[tuple[ObservationRecord, bytes]],
    ) -> ReconstructedGlyph:
        """Execute continuous SDF fusion, topology hierarchy extraction, and adaptive Schneider cubic Bézier fitting.
        
        Args:
            observations: List of (ObservationRecord, PNG bytes) from ObservationStore.
            
        Returns:
            ReconstructedGlyph with canonical cubic Bézier contours and UPEM metrics.
        """
        start_time = time.perf_counter()
        if not observations:
            raise ValueError("NO_OBSERVATIONS_SUPPLIED")

        # Step 1: Select available observations prioritizing highest spatial resolution (e.g. 256px)
        max_res = max((r.resolution for r, _ in observations), default=128)
        target_obs = [(r, b) for r, b in observations if r.resolution == max_res and b]
        if not target_obs:
            target_obs = [(r, b) for r, b in observations if b]
        if not target_obs:
            raise ValueError("NO_VALID_RASTER_OBSERVATIONS")

        first_rec, _ = target_obs[0]
        code_point = first_rec.code_point
        metrics = first_rec.metrics
        res = first_rec.resolution

        # Step 2: Explicit coordinate normalization from direct metrics
        f_size = math.floor(res * 0.72)
        scale = f_size / 1000.0  # pixels per UPEM

        adv_px = metrics.advance_width_upem * scale
        ascent_px = metrics.ascent_upem * scale
        descent_px = metrics.descent_upem * scale
        total_h_px = ascent_px + descent_px

        # Base origin in canvas pixel coordinates
        x_base = round((res - adv_px) / 2.0)
        y_base = round((res - total_h_px) / 2.0 + ascent_px)

        # Step 3: Multi-observation subpixel fusion
        avg_ink_mask = np.zeros((res, res), dtype=np.float32)
        for rec, raw_b in target_obs:
            img = Image.open(io.BytesIO(raw_b)).convert("L")
            arr = 1.0 - np.array(img, dtype=np.float32) / 255.0
            avg_ink_mask += arr
        avg_ink_mask /= len(target_obs)

        binary_ink = avg_ink_mask >= 0.5

        # Step 4: Extract topologically closed boundary loops
        raw_pixel_loops = SingleObservationBaselineReconstructor._trace_binary_boundary(binary_ink)

        upem_loops: list[list[Point2D]] = []
        for poly in raw_pixel_loops:
            if len(poly) < 3:
                continue
            # Map raster pixel coordinates (u, v) into continuous UPEM (X, Y)
            pts = [Point2D((u - x_base) / scale, (y_base - v) / scale) for u, v in poly]
            area = compute_polygon_area(pts)
            if abs(area) < self.config.min_contour_area_upem:
                continue
            upem_loops.append(pts)

        # Step 5: Classify topology hierarchy (outer vs holes and nesting depth)
        classified_contours = build_topology_hierarchy(upem_loops)

        # Step 6: Fit adaptive cubic Bézier curves (Schneider's algorithm) to master representation
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

        elapsed_ms = (time.perf_counter() - start_time) * 1000.0

        return ReconstructedGlyph(
            code_point=code_point,
            character=metrics.character,
            advance_width_upem=metrics.advance_width_upem,
            lsb_upem=metrics.lsb_upem,
            rsb_upem=metrics.rsb_upem,
            ascent_upem=metrics.ascent_upem,
            descent_upem=metrics.descent_upem,
            contours=fitted_contours,
            bounding_box_upem=(
                metrics.lsb_upem,
                -metrics.descent_upem,
                metrics.lsb_upem + metrics.bbox_width_upem,
                metrics.ascent_upem,
            ),
            reconstruction_time_ms=elapsed_ms,
        )
