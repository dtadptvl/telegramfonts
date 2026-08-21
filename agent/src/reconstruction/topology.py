"""Vectorized Marching Squares zero-crossing extraction and topology hierarchy classification."""
from __future__ import annotations

import math
from typing import Any
import numpy as np

from reconstruction.models import Point2D


def compute_polygon_area(pts: list[Point2D]) -> float:
    """Compute signed area of 2D polygon using Shoelace formula.
    
    Positive area denotes Counter-Clockwise (CCW); Negative denotes Clockwise (CW).
    """
    n = len(pts)
    if n < 3:
        return 0.0
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += pts[i].x * pts[j].y - pts[j].x * pts[i].y
    return 0.5 * area


def point_in_polygon(pt: Point2D, polygon: list[Point2D]) -> bool:
    """Ray-casting point-in-polygon containment test."""
    x, y = pt.x, pt.y
    n = len(polygon)
    inside = False
    p1 = polygon[0]
    for i in range(n + 1):
        p2 = polygon[i % n]
        if y > min(p1.y, p2.y):
            if y <= max(p1.y, p2.y):
                if x <= max(p1.x, p2.x):
                    if p1.y != p2.y:
                        x_inters = (y - p1.y) * (p2.x - p1.x) / (p2.y - p1.y) + p1.x
                    if p1.x == p2.x or x <= x_inters:
                        inside = not inside
        p1 = p2
    return inside


def extract_zero_crossing_contours(
    sdf_grid: np.ndarray,
    x_coords: np.ndarray,
    y_coords: np.ndarray,
    min_area_upem: float = 15.0,
) -> list[list[Point2D]]:
    """Extract closed 2D polygon contours along the continuous zero-level set (SDF == 0)."""
    H, W = sdf_grid.shape

    # 1. Compute exact unique edge crossing points on the grid
    h_crossings: dict[tuple[int, int], Point2D] = {}
    for i in range(H):
        for j in range(W - 1):
            z0, z1 = float(sdf_grid[i, j]), float(sdf_grid[i, j + 1])
            if (z0 >= 0) != (z1 >= 0):
                t = (-z0) / (z1 - z0 + 1e-12)
                x = float(x_coords[j]) + t * (float(x_coords[j + 1]) - float(x_coords[j]))
                y = float(y_coords[i])
                h_crossings[(i, j)] = Point2D(x, y)

    v_crossings: dict[tuple[int, int], Point2D] = {}
    for i in range(H - 1):
        for j in range(W):
            z0, z1 = float(sdf_grid[i, j]), float(sdf_grid[i + 1, j])
            if (z0 >= 0) != (z1 >= 0):
                t = (-z0) / (z1 - z0 + 1e-12)
                x = float(x_coords[j])
                y = float(y_coords[i]) + t * (float(y_coords[i + 1]) - float(y_coords[i]))
                v_crossings[(i, j)] = Point2D(x, y)

    # 2. Build cell-based directed edge graph
    adj: dict[tuple[str, int, int], list[tuple[str, int, int]]] = {}
    for i in range(H - 1):
        for j in range(W - 1):
            z0 = float(sdf_grid[i, j])
            z1 = float(sdf_grid[i, j + 1])
            z2 = float(sdf_grid[i + 1, j + 1])
            z3 = float(sdf_grid[i + 1, j])

            case = int(z0 >= 0) | (int(z1 >= 0) << 1) | (int(z2 >= 0) << 2) | (int(z3 >= 0) << 3)
            if case in (0, 15):
                continue

            e_bottom = ("H", i, j)
            e_right = ("V", i, j + 1)
            e_top = ("H", i + 1, j)
            e_left = ("V", i, j)

            def add_seg(src: tuple[str, int, int], dst: tuple[str, int, int]) -> None:
                adj.setdefault(src, []).append(dst)

            if case == 1:
                add_seg(e_bottom, e_left)
            elif case == 14:
                add_seg(e_left, e_bottom)
            elif case == 2:
                add_seg(e_right, e_bottom)
            elif case == 13:
                add_seg(e_bottom, e_right)
            elif case == 3:
                add_seg(e_right, e_left)
            elif case == 12:
                add_seg(e_left, e_right)
            elif case == 4:
                add_seg(e_top, e_right)
            elif case == 11:
                add_seg(e_right, e_top)
            elif case == 5:
                add_seg(e_bottom, e_left)
                add_seg(e_top, e_right)
            elif case == 10:
                add_seg(e_right, e_bottom)
                add_seg(e_left, e_top)
            elif case == 6:
                add_seg(e_top, e_bottom)
            elif case == 9:
                add_seg(e_bottom, e_top)
            elif case == 7:
                add_seg(e_top, e_left)
            elif case == 8:
                add_seg(e_left, e_top)

    # 3. Chain graph into continuous closed cycles
    loops: list[list[Point2D]] = []
    visited_edges: set[tuple[str, int, int]] = set()

    for start_edge in list(adj.keys()):
        if start_edge in visited_edges or not adj[start_edge]:
            continue

        cycle: list[Point2D] = []
        curr = start_edge

        while adj.get(curr):
            next_edge = adj[curr].pop(0)
            visited_edges.add(curr)
            etype, ei, ej = curr
            pt = h_crossings[(ei, ej)] if etype == "H" else v_crossings[(ei, ej)]
            cycle.append(pt)
            curr = next_edge
            if curr == start_edge:
                break

        if len(cycle) >= 3:
            area = compute_polygon_area(cycle)
            if abs(area) >= min_area_upem:
                loops.append(cycle)

    return loops


def build_topology_hierarchy(
    raw_loops: list[list[Point2D]],
) -> list[dict[str, Any]]:
    """Classify polygon loops into outer boundaries vs inner hole cutouts using containment and nesting depth.
    
    Returns:
        List of dicts: {"points": list[Point2D], "is_hole": bool, "parent_index": int | None, "area_upem": float}
    """
    if not raw_loops:
        return []

    # Sort loops from largest area to smallest
    sorted_loops = sorted(raw_loops, key=lambda loop: abs(compute_polygon_area(loop)), reverse=True)

    classified: list[dict[str, Any]] = []

    for i, loop in enumerate(sorted_loops):
        sample_pt = loop[0]
        depth = 0
        parent_idx = None
        for j in range(i):
            other = sorted_loops[j]
            if point_in_polygon(sample_pt, other):
                depth += 1
                parent_idx = j

        is_hole = (depth % 2 == 1)
        area = compute_polygon_area(loop)

        # Enforce standard TrueType winding: outer is CCW (positive area), holes CW (negative area)
        pts = loop
        if not is_hole and area < 0:
            pts = list(reversed(pts))
            area = -area
        elif is_hole and area > 0:
            pts = list(reversed(pts))
            area = -area

        classified.append({
            "index": i,
            "points": pts,
            "is_hole": is_hole,
            "parent_index": parent_idx,
            "area_upem": area,
        })

    return classified
