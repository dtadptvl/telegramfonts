"""Isolated Ground-Truth Geometry Evaluator for scoring reconstructed outlines against true font outlines."""
from __future__ import annotations

import io
from pathlib import Path
import numpy as np
import scipy.spatial as spatial
from fontTools.pens.recordingPen import RecordingPen
from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw

from reconstruction.models import (
    CubicSegment,
    GeometricScoreResult,
    LineSegment,
    Point2D,
    ReconstructedGlyph,
)


class GroundTruthGeometryEvaluator:
    """Isolated ground-truth outline comparator and geometric metric validator.
    
    Reads the ground-truth TTF font binary ONLY for scoring and validation.
    """

    def __init__(self, ttf_path: str | Path) -> None:
        self.ttf_path = Path(ttf_path)
        if not self.ttf_path.exists():
            raise FileNotFoundError(f"Ground-truth font binary not found at: {self.ttf_path}")
        self.font = TTFont(str(self.ttf_path))
        self.glyph_set = self.font.getGlyphSet()
        self.cmap = self.font.getBestCmap() or {}

    def extract_truth_point_cloud(
        self, code_point: int, num_samples: int = 2000
    ) -> tuple[np.ndarray, int, int]:
        """Extract continuous boundary point cloud and topology (outer, holes) from ground truth TTF glyph.
        
        Returns:
            (point_cloud_2d_array, outer_contours_count, holes_count)
        """
        glyph_name = self.cmap.get(code_point)
        if not glyph_name or glyph_name not in self.glyph_set:
            return np.zeros((0, 2), dtype=np.float32), 0, 0

        contours_pts = self._flatten_truth_glyph_contours(glyph_name, num_curve_samples=16)

        # Classify outer vs hole by signed area
        outer_cnt = 0
        holes_cnt = 0
        all_pts: list[Point2D] = []

        for c in contours_pts:
            if len(c) < 3:
                continue
            all_pts.extend(c)
            # Area in TrueType: Clockwise (negative in standard mathematical Shoelace) is outer
            area = 0.0
            for i in range(len(c)):
                j = (i + 1) % len(c)
                area += c[i].x * c[j].y - c[j].x * c[i].y
            if abs(area) >= 15.0:
                # TrueType convention
                if area < 0:
                    outer_cnt += 1
                else:
                    holes_cnt += 1

        if not all_pts:
            return np.zeros((0, 2), dtype=np.float32), 0, 0

        # Subsample to uniform cloud
        pts_arr = np.array([[p.x, p.y] for p in all_pts], dtype=np.float32)
        if len(pts_arr) > num_samples:
            indices = np.linspace(0, len(pts_arr) - 1, num_samples, dtype=int)
            pts_arr = pts_arr[indices]

        return pts_arr, max(outer_cnt, 1 if len(contours_pts) > 0 else 0), holes_cnt

    def evaluate_glyph(
        self,
        reconstructed: ReconstructedGlyph,
        raster_eval_resolution: int = 1024,
    ) -> GeometricScoreResult:
        """Score reconstructed glyph outline against ground truth TTF geometry."""
        code_point = reconstructed.code_point
        char = reconstructed.character

        # 1. Extract ground truth point cloud and topology
        truth_pts, truth_outer, truth_holes = self.extract_truth_point_cloud(code_point)

        # 2. Extract reconstructed point cloud
        recon_pts_list: list[Point2D] = []
        for c in reconstructed.contours:
            recon_pts_list.extend(c.sample_points(samples_per_segment=16))

        recon_pts = (
            np.array([[p.x, p.y] for p in recon_pts_list], dtype=np.float32)
            if recon_pts_list
            else np.zeros((0, 2), dtype=np.float32)
        )

        # 3. Compute bidirectional Chamfer, Hausdorff, and P95 edge errors
        if len(truth_pts) > 0 and len(recon_pts) > 0:
            tree_truth = spatial.cKDTree(truth_pts)
            tree_recon = spatial.cKDTree(recon_pts)

            dists_recon_to_truth, _ = tree_truth.query(recon_pts)
            dists_truth_to_recon, _ = tree_recon.query(truth_pts)

            chamfer_mean = float(
                0.5 * (np.mean(dists_recon_to_truth) + np.mean(dists_truth_to_recon))
            )
            all_dists = np.concatenate([dists_recon_to_truth, dists_truth_to_recon])
            hausdorff = float(np.max(all_dists))
            p95_error = float(np.percentile(all_dists, 95))
        else:
            chamfer_mean = 999.0
            hausdorff = 999.0
            p95_error = 999.0

        # 4. Compute High-Resolution Filled Outline IoU
        iou = self._compute_raster_iou(
            reconstructed=reconstructed,
            code_point=code_point,
            resolution=raster_eval_resolution,
        )

        # 5. Topology match check
        recon_outer = reconstructed.outer_contours_count
        recon_holes = reconstructed.holes_count
        topology_match = (
            (truth_outer == recon_outer and truth_holes == recon_holes)
            or (truth_outer == 0 and len(reconstructed.contours) == 0)
        )

        return GeometricScoreResult(
            code_point=code_point,
            character=char,
            outline_iou=iou,
            chamfer_distance_mean_upem=chamfer_mean,
            hausdorff_distance_upem=hausdorff,
            p95_edge_error_upem=p95_error,
            truth_outer_count=truth_outer,
            reconstructed_outer_count=recon_outer,
            truth_holes_count=truth_holes,
            reconstructed_holes_count=recon_holes,
            topology_match=topology_match,
            cubic_segments_count=reconstructed.total_cubic_segments,
            control_points_count=reconstructed.total_control_points,
            runtime_ms=reconstructed.reconstruction_time_ms,
        )

    def _compute_raster_iou(
        self,
        reconstructed: ReconstructedGlyph,
        code_point: int,
        resolution: int = 1024,
    ) -> float:
        """Render high-resolution binary filled masks for truth vs reconstructed and compute IoU."""
        # Render ground truth mask
        glyph_name = self.cmap.get(code_point)
        if not glyph_name or glyph_name not in self.glyph_set:
            return 1.0 if not reconstructed.contours else 0.0

        # Create PIL canvas of size (resolution, resolution)
        # Normalization box: X in [-100, 1100], Y in [-300, 900] (covers full standard UPEM)
        box_x_min, box_x_max = -100.0, 1100.0
        box_y_min, box_y_max = -300.0, 900.0
        scale_x = resolution / (box_x_max - box_x_min)
        scale_y = resolution / (box_y_max - box_y_min)

        def to_img_coords(x: float, y: float) -> tuple[float, float]:
            u = (x - box_x_min) * scale_x
            v = resolution - (y - box_y_min) * scale_y
            return (u, v)

    def _flatten_truth_glyph_contours(
        self, glyph_name: str, num_curve_samples: int = 16
    ) -> list[list[Point2D]]:
        """Flatten TrueType glyph outline into densely sampled continuous polygon loops preserving all curve control points."""
        if glyph_name not in self.glyph_set:
            return []

        pen = RecordingPen()
        self.glyph_set[glyph_name].draw(pen)

        contours: list[list[Point2D]] = []
        curr_contour: list[Point2D] = []
        curr_pt = Point2D(0.0, 0.0)

        for cmd, args in pen.value:
            if cmd == "moveTo":
                if curr_contour and len(curr_contour) >= 3:
                    contours.append(curr_contour)
                curr_pt = Point2D(args[0][0], args[0][1])
                curr_contour = [curr_pt]
            elif cmd == "lineTo":
                target = Point2D(args[0][0], args[0][1])
                steps = max(num_curve_samples // 2, 4)
                for s in range(1, steps + 1):
                    t = s / steps
                    curr_contour.append(curr_pt + (target - curr_pt) * t)
                curr_pt = target
            elif cmd == "qCurveTo":
                # Quadratic Bézier or TrueType poly-quadratic spline
                pts = [Point2D(p[0], p[1]) for p in args]
                p0 = curr_pt
                if len(pts) == 1:
                    p1 = pts[0]
                    for s in range(1, num_curve_samples + 1):
                        t = s / num_curve_samples
                        u = 1.0 - t
                        curr_contour.append(p0 * (u * u) + p1 * (2.0 * u * t) + pts[-1] * (t * t))
                    curr_pt = pts[-1]
                else:
                    # Multi-point TT quadratic spline with implied on-curve midpoints
                    for k in range(len(pts) - 1):
                        p1 = pts[k]
                        p2 = (pts[k] + pts[k + 1]) * 0.5 if k < len(pts) - 2 else pts[-1]
                        for s in range(1, num_curve_samples + 1):
                            t = s / num_curve_samples
                            u = 1.0 - t
                            curr_contour.append(p0 * (u * u) + p1 * (2.0 * u * t) + p2 * (t * t))
                        p0 = p2
                    curr_pt = pts[-1]
            elif cmd == "curveTo":
                # True Cubic Bézier
                p0 = curr_pt
                p1 = Point2D(args[0][0], args[0][1])
                p2 = Point2D(args[1][0], args[1][1])
                p3 = Point2D(args[2][0], args[2][1])
                for s in range(1, num_curve_samples + 1):
                    t = s / num_curve_samples
                    u = 1.0 - t
                    x = u**3 * p0.x + 3 * u**2 * t * p1.x + 3 * u * t**2 * p2.x + t**3 * p3.x
                    y = u**3 * p0.y + 3 * u**2 * t * p1.y + 3 * u * t**2 * p2.y + t**3 * p3.y
                    curr_contour.append(Point2D(x, y))
                curr_pt = p3
            elif cmd in ("closePath", "endPath"):
                if curr_contour and len(curr_contour) >= 3:
                    contours.append(curr_contour)
                    curr_contour = []

        if curr_contour and len(curr_contour) >= 3:
            contours.append(curr_contour)

        return contours

    def _compute_raster_iou(
        self,
        reconstructed: ReconstructedGlyph,
        code_point: int,
        resolution: int = 1024,
    ) -> float:
        """Render high-resolution binary filled masks for truth vs reconstructed and compute IoU."""
        glyph_name = self.cmap.get(code_point)
        if not glyph_name or glyph_name not in self.glyph_set:
            return 1.0 if not reconstructed.contours else 0.0

        box_x_min, box_x_max = -100.0, 1100.0
        box_y_min, box_y_max = -300.0, 900.0
        scale_x = resolution / (box_x_max - box_x_min)
        scale_y = resolution / (box_y_max - box_y_min)

        def to_img_coords(x: float, y: float) -> tuple[float, float]:
            u = (x - box_x_min) * scale_x
            v = resolution - (y - box_y_min) * scale_y
            return (u, v)

        # 1. Draw Truth Mask with full quadratic and cubic curve geometry
        raw_truth_contours = self._flatten_truth_glyph_contours(glyph_name, num_curve_samples=16)
        classified_truth: list[tuple[list[tuple[float, float]], bool]] = []

        for contour_pts in raw_truth_contours:
            # Area in TrueType font coordinates (Y-up): area > 0 is hole
            area = 0.5 * sum(
                contour_pts[k].x * contour_pts[(k + 1) % len(contour_pts)].y
                - contour_pts[(k + 1) % len(contour_pts)].x * contour_pts[k].y
                for k in range(len(contour_pts))
            )
            is_hole = (area > 0)
            poly_coords = [to_img_coords(p.x, p.y) for p in contour_pts]
            classified_truth.append((poly_coords, is_hole))

        img_truth = Image.new("1", (resolution, resolution), 0)
        draw_truth = ImageDraw.Draw(img_truth)

        # Sort truth: outer (is_hole=False) first with fill=1, holes (is_hole=True) second with fill=0
        classified_truth.sort(key=lambda item: item[1])
        for poly_coords, is_hole in classified_truth:
            draw_truth.polygon(poly_coords, fill=0 if is_hole else 1)

        # 2. Draw Reconstructed Mask (outer first, holes second)
        img_recon = Image.new("1", (resolution, resolution), 0)
        draw_recon = ImageDraw.Draw(img_recon)

        recon_contours_sorted = sorted(reconstructed.contours, key=lambda c: c.is_hole)
        for contour in recon_contours_sorted:
            pts = contour.sample_points(samples_per_segment=16)
            if len(pts) >= 3:
                poly_coords = [to_img_coords(p.x, p.y) for p in pts]
                fill_val = 0 if contour.is_hole else 1
                draw_recon.polygon(poly_coords, fill=fill_val)

        arr_truth = np.array(img_truth, dtype=bool)
        arr_recon = np.array(img_recon, dtype=bool)

        intersection = np.logical_and(arr_truth, arr_recon).sum()
        union = np.logical_or(arr_truth, arr_recon).sum()

        if union == 0:
            return 1.0 if intersection == 0 else 0.0

        return float(intersection / union)
