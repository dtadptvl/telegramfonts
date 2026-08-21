"""Adaptive cubic Bézier curve fitting using Schneider's least-squares algorithm with corner detection."""
from __future__ import annotations

import math
from reconstruction.models import Contour, CubicSegment, LineSegment, Point2D, ReconstructionConfig


class SchneiderFitter:
    """Philip J. Schneider's adaptive cubic Bézier curve fitting algorithm."""

    @classmethod
    def fit_contour(
        cls,
        points: list[Point2D],
        is_hole: bool,
        parent_index: int | None,
        area_upem: float,
        config: ReconstructionConfig,
    ) -> Contour:
        """Fit smooth cubic Bézier segments to a closed polygon loop while preserving sharp corners."""
        n = len(points)
        if n < 3:
            return Contour(segments=[], is_hole=is_hole, parent_index=parent_index, area_upem=area_upem)

        # 1. Detect sharp corners along the polygon loop
        corners = cls._detect_corners(points, config.corner_threshold_degrees)

        segments: list[CubicSegment | LineSegment] = []

        if corners:
            # Partition loop into subpaths between consecutive sharp corners
            num_corners = len(corners)
            for c_idx in range(num_corners):
                start_i = corners[c_idx]
                end_i = corners[(c_idx + 1) % num_corners]

                if end_i > start_i:
                    sub_pts = points[start_i : end_i + 1]
                else:
                    sub_pts = points[start_i:] + points[: end_i + 1]

                if len(sub_pts) < 2:
                    continue

                t1 = (sub_pts[1] - sub_pts[0]).normalize()
                t2 = (sub_pts[-1] - sub_pts[-2]).normalize()

                sub_curves = cls._fit_cubic_subsegment(
                    sub_pts, t1, t2, config.fitting_tolerance_upem
                )
                segments.extend(sub_curves)
        else:
            # Smooth loop with no sharp corners (e.g. circle / oval): split at 4 cardinal extrema
            split_indices = cls._find_quadrant_splits(points)
            for s_idx in range(len(split_indices)):
                start_i = split_indices[s_idx]
                end_i = split_indices[(s_idx + 1) % len(split_indices)]

                if end_i > start_i:
                    sub_pts = points[start_i : end_i + 1]
                else:
                    sub_pts = points[start_i:] + points[: end_i + 1]

                if len(sub_pts) < 2:
                    continue

                t1 = (sub_pts[1] - sub_pts[0]).normalize()
                t2 = (sub_pts[-1] - sub_pts[-2]).normalize()

                sub_curves = cls._fit_cubic_subsegment(
                    sub_pts, t1, t2, config.fitting_tolerance_upem
                )
                segments.extend(sub_curves)

        return Contour(
            segments=segments,
            is_hole=is_hole,
            parent_index=parent_index,
            area_upem=area_upem,
        )

    @classmethod
    def _detect_corners(cls, pts: list[Point2D], corner_threshold_deg: float) -> list[int]:
        """Identify sharp corner vertices by measuring turning angles between adjacent edges."""
        n = len(pts)
        corners: list[int] = []
        # Max turning angle threshold (e.g. corner_threshold=120 deg -> turning angle > 60 deg is corner)
        max_turning_angle = 180.0 - corner_threshold_deg

        for i in range(n):
            p_prev = pts[(i - 1) % n]
            p_curr = pts[i]
            p_next = pts[(i + 1) % n]

            v1 = (p_curr - p_prev).normalize()
            v2 = (p_next - p_curr).normalize()

            dot = max(-1.0, min(1.0, v1.dot(v2)))
            turning_deg = math.degrees(math.acos(dot))

            if turning_deg >= max_turning_angle:
                corners.append(i)

        return corners

    @classmethod
    def _find_quadrant_splits(cls, pts: list[Point2D]) -> list[int]:
        """Find 4 balanced split points along a smooth closed loop to seed cubic fitting."""
        n = len(pts)
        if n <= 4:
            return list(range(n))

        # Find min/max X and min/max Y extrema
        min_x_idx = min(range(n), key=lambda i: pts[i].x)
        max_x_idx = max(range(n), key=lambda i: pts[i].x)
        min_y_idx = min(range(n), key=lambda i: pts[i].y)
        max_y_idx = max(range(n), key=lambda i: pts[i].y)

        splits = sorted(list({min_x_idx, max_x_idx, min_y_idx, max_y_idx}))
        if len(splits) < 2:
            return [0, n // 4, n // 2, (3 * n) // 4]
        return splits

    @classmethod
    def _fit_cubic_subsegment(
        cls,
        points: list[Point2D],
        t1: Point2D,
        t2: Point2D,
        error_tol: float,
        depth: int = 0,
        max_depth: int = 8,
    ) -> list[CubicSegment]:
        """Fit single cubic Bézier or recursively split at point of maximum error."""
        if len(points) == 2:
            dist = points[0].distance_to(points[1]) / 3.0
            return [
                CubicSegment(
                    p0=points[0],
                    p1=points[0] + t1 * dist,
                    p2=points[1] - t2 * dist,
                    p3=points[1],
                )
            ]

        # 1. Chord length parameterization
        dists = [0.0]
        for i in range(1, len(points)):
            dists.append(dists[-1] + points[i - 1].distance_to(points[i]))

        total_len = dists[-1]
        if total_len < 1e-6:
            return [CubicSegment(points[0], points[0], points[-1], points[-1])]

        u = [d / total_len for d in dists]

        # 2. Least-squares solve for control points P1, P2
        c11 = c12 = c21 = c22 = x1 = x2 = 0.0
        p0 = points[0]
        p3 = points[-1]

        for i, t in enumerate(u):
            b0 = (1.0 - t) ** 3
            b1 = 3.0 * (1.0 - t) ** 2 * t
            b2 = 3.0 * (1.0 - t) * (t ** 2)
            b3 = t ** 3

            a1 = t1 * b1
            a2 = t2 * (-b2)

            c11 += a1.dot(a1)
            c12 += a1.dot(a2)
            c21 += a1.dot(a2)
            c22 += a2.dot(a2)

            baseline_pt = Point2D(
                p0.x * b0 + p0.x * b1 + p3.x * b2 + p3.x * b3,
                p0.y * b0 + p0.y * b1 + p3.y * b2 + p3.y * b3,
            )
            diff = points[i] - baseline_pt
            x1 += a1.dot(diff)
            x2 += a2.dot(diff)

        det = c11 * c22 - c12 * c21
        if abs(det) > 1e-9:
            alpha1 = (x1 * c22 - x2 * c12) / det
            alpha2 = (c11 * x2 - c21 * x1) / det
        else:
            alpha1 = alpha2 = total_len / 3.0

        # Enforce positive handle lengths
        if alpha1 <= 1e-4 or alpha2 <= 1e-4:
            alpha1 = alpha2 = total_len / 3.0

        p1 = p0 + t1 * alpha1
        p2 = p3 - t2 * alpha2
        curve = CubicSegment(p0, p1, p2, p3)

        # 3. Parameter refinement with Newton-Raphson
        refined_u = cls._reparameterize(curve, points, u)

        # 4. Measure maximum fitting error
        max_err = 0.0
        split_idx = len(points) // 2

        for i in range(1, len(points) - 1):
            pt_eval = curve.evaluate(refined_u[i])
            err = pt_eval.distance_to(points[i])
            if err > max_err:
                max_err = err
                split_idx = i

        # If fitting error is within tolerance or max depth reached, accept curve
        if max_err <= error_tol or depth >= max_depth:
            return [curve]

        # 5. Recursive subdivision at point of maximum error
        p_prev = points[max_idx_clamp(split_idx - 1, 0, len(points) - 1)]
        p_next = points[max_idx_clamp(split_idx + 1, 0, len(points) - 1)]
        t_center = (p_next - p_prev).normalize()
        if t_center.length() < 1e-6:
            t_center = (points[-1] - points[0]).normalize()

        left = cls._fit_cubic_subsegment(
            points[: split_idx + 1], t1, t_center, error_tol, depth + 1, max_depth
        )
        right = cls._fit_cubic_subsegment(
            points[split_idx:], t_center, t2, error_tol, depth + 1, max_depth
        )
        return left + right

    @classmethod
    def _reparameterize(
        cls, curve: CubicSegment, points: list[Point2D], u: list[float]
    ) -> list[float]:
        """Perform 1 Newton-Raphson iteration to find optimal curve parameter t for each point."""
        refined: list[float] = [0.0]
        for i in range(1, len(points) - 1):
            t = u[i]
            pt = points[i]

            p_eval = curve.evaluate(t)
            d_eval = curve.derivative(t)
            d2_eval = curve.second_derivative(t)

            diff = p_eval - pt
            num = diff.dot(d_eval)
            den = d_eval.length_squared() + diff.dot(d2_eval)

            if abs(den) > 1e-9:
                t_prime = t - num / den
                refined.append(max(0.0, min(1.0, t_prime)))
            else:
                refined.append(t)

        refined.append(1.0)
        return refined


def max_idx_clamp(val: int, low: int, high: int) -> int:
    return max(low, min(high, val))
