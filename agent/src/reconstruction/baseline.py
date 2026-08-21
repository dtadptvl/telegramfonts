"""Single-observation contour baseline used exclusively for benchmark comparison against MAX Pipeline B."""
from __future__ import annotations

import io
import math
import time
import numpy as np
from PIL import Image

from measurement.models import ObservationRecord
from reconstruction.models import (
    Contour,
    LineSegment,
    Point2D,
    ReconstructedGlyph,
    ReconstructionConfig,
)
from reconstruction.topology import compute_polygon_area, point_in_polygon


class SingleObservationBaselineReconstructor:
    """Simple baseline that reconstructs glyph contours from a single 128px observation raster without SDF fusion."""

    @classmethod
    def reconstruct_glyph(
        cls,
        observations: list[tuple[ObservationRecord, bytes]],
        config: ReconstructionConfig | None = None,
    ) -> ReconstructedGlyph:
        """Reconstruct glyph geometry using only the first available base observation (e.g. 128px, phase 0,0)."""
        start_time = time.perf_counter()
        if not observations:
            raise ValueError("NO_OBSERVATIONS_AVAILABLE")

        # Pick single 128px observation (or first available)
        selected_rec, png_bytes = observations[0]
        for rec, raw_b in observations:
            if rec.resolution == 128 and rec.subpixel_x == 0.0 and rec.subpixel_y == 0.0:
                selected_rec, png_bytes = rec, raw_b
                break

        img = Image.open(io.BytesIO(png_bytes)).convert("L")
        arr = np.array(img, dtype=np.float32) / 255.0
        ink = (1.0 - arr) >= 0.5  # Simple binary threshold

        res = selected_rec.resolution
        f_size = math.floor(res * 0.72)
        scale = f_size / 1000.0

        adv_px = selected_rec.metrics.advance_width_upem * scale
        ascent_px = selected_rec.metrics.ascent_upem * scale
        descent_px = selected_rec.metrics.descent_upem * scale
        total_h_px = ascent_px + descent_px

        x_base = round((res - adv_px) / 2.0)
        y_base = round((res - total_h_px) / 2.0 + ascent_px)

        # Extract naive boundary pixel chains
        raw_polygons = cls._trace_binary_boundary(ink)

        contours: list[Contour] = []
        for poly in raw_polygons:
            if len(poly) < 3:
                continue

            # Convert pixel coords (u, v) to UPEM (X, Y)
            upem_pts: list[Point2D] = []
            for u, v in poly:
                x_upem = (u - x_base) / max(scale, 1e-6)
                y_upem = (y_base - v) / max(scale, 1e-6)
                upem_pts.append(Point2D(x_upem, y_upem))

            area = compute_polygon_area(upem_pts)
            if abs(area) < 15.0:
                continue

            # Build simple linear segments (polygonal representation)
            segments: list[LineSegment] = []
            for i in range(len(upem_pts)):
                p0 = upem_pts[i]
                p1 = upem_pts[(i + 1) % len(upem_pts)]
                segments.append(LineSegment(p0, p1))

            contours.append(
                Contour(
                    segments=segments,
                    is_hole=False,  # Raw baseline does not perform hierarchical hole classification
                    area_upem=area,
                )
            )

        # Classify holes via simple nesting depth
        for i, c in enumerate(contours):
            sample_pt = c.segments[0].p0 if c.segments else Point2D(0, 0)
            depth = 0
            for j, other in enumerate(contours):
                if i == j:
                    continue
                other_pts = [s.p0 for s in other.segments]
                if point_in_polygon(sample_pt, other_pts):
                    depth += 1
            c.is_hole = (depth % 2 == 1)

        elapsed_ms = (time.perf_counter() - start_time) * 1000.0

        return ReconstructedGlyph(
            code_point=selected_rec.code_point,
            character=selected_rec.metrics.character,
            advance_width_upem=selected_rec.metrics.advance_width_upem,
            lsb_upem=selected_rec.metrics.lsb_upem,
            rsb_upem=selected_rec.metrics.rsb_upem,
            ascent_upem=selected_rec.metrics.ascent_upem,
            descent_upem=selected_rec.metrics.descent_upem,
            contours=contours,
            bounding_box_upem=(
                selected_rec.metrics.lsb_upem,
                -selected_rec.metrics.descent_upem,
                selected_rec.metrics.lsb_upem + selected_rec.metrics.bbox_width_upem,
                selected_rec.metrics.ascent_upem,
            ),
            reconstruction_time_ms=elapsed_ms,
        )

    @classmethod
    def _trace_binary_boundary(cls, binary_mask: np.ndarray) -> list[list[tuple[float, float]]]:
        """Simple discrete boundary tracer on 2D boolean grid."""
        H, W = binary_mask.shape
        padded = np.pad(binary_mask, 1, mode="constant", constant_values=False)
        diff_h = np.diff(padded.astype(int), axis=0) != 0
        diff_v = np.diff(padded.astype(int), axis=1) != 0

        # Extract boundary pixel centers
        edges = []
        for r in range(H):
            for c in range(W):
                if binary_mask[r, c]:
                    # Check 4 neighbors
                    if not padded[r, c + 1]:
                        edges.append(((c, r), (c + 1, r)))
                    if not padded[r + 2, c + 1]:
                        edges.append(((c + 1, r + 1), (c, r + 1)))
                    if not padded[r + 1, c]:
                        edges.append(((c, r + 1), (c, r)))
                    if not padded[r + 1, c + 2]:
                        edges.append(((c + 1, r), (c + 1, r + 1)))

        # Chain edges
        adj = {}
        for p1, p2 in edges:
            adj.setdefault(p1, []).append(p2)

        loops = []
        visited = set()
        for p1, p2 in edges:
            if p1 in visited or not adj.get(p1):
                continue
            loop = [p1]
            curr = adj[p1].pop(0)
            visited.add(p1)
            while curr != p1 and adj.get(curr):
                loop.append(curr)
                visited.add(curr)
                curr = adj[curr].pop(0)
            if len(loop) >= 4:
                # Subsample loop to avoid 1-pixel staircases
                subsampled = loop[::3]
                if len(subsampled) >= 3:
                    loops.append([(float(x), float(y)) for x, y in subsampled])

        return loops
