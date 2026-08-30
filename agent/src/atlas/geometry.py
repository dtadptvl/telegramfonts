"""Fast vectorized geometry chain (ADR-0004, U4).

alpha decode -> coordinate normalization -> direct alpha/subpixel contour
-> topology cleanup -> cubic Bezier fitting -> error-bounded simplification
-> cheap FIT-confidence. Easy glyphs MUST NOT compute SDF or run the heavy
optimizer: PASS freezes the GlyphModel immediately and releases transient
memory. All pixel-domain work is native NumPy/SciPy/Pillow; Python loops
only walk boundary-length data (never per-pixel over the page).
"""
from __future__ import annotations

import io
import math

import numpy as np
from PIL import Image

from reconstruction.models import Contour, CubicSegment, LineSegment, Point2D

from atlas.models import CellMapping, GeometryEvidence, GlyphStatus, RegressedMetrics

# Versioned fast-path thresholds (fit evidence only).
MIN_IOU_EASY_PASS = 0.90
MIN_IOU_REFINED_ACCEPT = 0.80
MIN_IOU_LOW_CONFIDENCE = 0.90
MAX_METRICS_RESIDUAL = 0.08
MIN_CONTOUR_AREA_UPEM = 15.0
FIT_TOLERANCE_UPEM = 1.5
MAX_SEGMENTS_PER_CONTOUR = 32
MAX_SELF_INTERSECTION_AREA_DIVERGENCE = 0.35

ISO_LEVEL = 0.5


# ----------------------------------------------------------------------
# Alpha decode + normalization (native Pillow/NumPy only)
# ----------------------------------------------------------------------

def decode_alpha(png_bytes: bytes) -> np.ndarray:
    """Decode one cropped cell observation into an 8-bit coverage plane."""
    if not png_bytes:
        raise ValueError("ATLAS_EMPTY_OBSERVATION")
    with Image.open(io.BytesIO(png_bytes)) as img:
        return np.asarray(img.convert("L"), dtype=np.uint8)


def alpha_to_coverage(alpha: np.ndarray) -> np.ndarray:
    """Normalize 8-bit alpha to float coverage in [0, 1] (ink fraction)."""
    return alpha.astype(np.float32) / 255.0


def coverage_to_ink(coverage: np.ndarray, iso: float = ISO_LEVEL) -> np.ndarray:
    return coverage >= iso


# ----------------------------------------------------------------------
# Direct alpha/subpixel contour extraction (marching squares)
# ----------------------------------------------------------------------

def _interp(p0: tuple[float, float], v0: float, p1: tuple[float, float], v1: float, iso: float) -> tuple[float, float]:
    d = v1 - v0
    if abs(d) < 1e-12:
        return (0.5 * (p0[0] + p1[0]), 0.5 * (p0[1] + p1[1]))
    t = (iso - v0) / d
    t = min(1.0, max(0.0, t))
    return (p0[0] + t * (p1[0] - p0[0]), p0[1] + t * (p1[1] - p0[1]))


def marching_squares_contours(coverage: np.ndarray, iso: float = ISO_LEVEL) -> list[np.ndarray]:
    """Extract closed iso-contours directly from the anti-aliased coverage.

    Returns polygons as (N, 2) float arrays in pixel coordinates (x, y with
    y DOWN). Subpixel crossings are linearly interpolated, so the contours
    are the DIRECT alpha contours required by ADR-0004. Case computation is
    vectorized; the boundary walk follows cell-edge adjacency (each grid
    edge is crossed by at most one segment per adjacent cell), which is
    deterministic and immune to ambiguous vertex pairing. Only
    boundary-length data is walked in Python - never per-pixel.
    """
    h, w = coverage.shape
    if h < 2 or w < 2:
        return []

    tl = coverage[:-1, :-1]
    tr = coverage[:-1, 1:]
    br = coverage[1:, 1:]
    bl = coverage[1:, :-1]

    case = (
        ((tl > iso).astype(np.uint8) << 3)
        | ((tr > iso).astype(np.uint8) << 2)
        | ((br > iso).astype(np.uint8) << 1)
        | (bl > iso).astype(np.uint8)
    )
    ys, xs = np.nonzero((case > 0) & (case < 15))
    if ys.size == 0:
        return []

    center = (tl + tr + br + bl) / 4.0

    # Segments carry their grid-edge keys so the walk never re-derives them.
    # Edge keys: ('H', gridline_y, cell_x) horizontal, ('V', cell_y, gridline_x) vertical.
    segments: list[tuple[tuple, tuple[float, float], tuple, tuple[float, float]]] = []

    def interp_edge_x(y_line: int, x: int, v0: float, v1: float) -> tuple[float, float]:
        d = v1 - v0
        t = 0.5 if abs(d) < 1e-12 else (iso - v0) / d
        t = min(1.0, max(0.0, t))
        return (x + t, float(y_line))

    def interp_edge_y(x_line: int, y: int, v0: float, v1: float) -> tuple[float, float]:
        d = v1 - v0
        t = 0.5 if abs(d) < 1e-12 else (iso - v0) / d
        t = min(1.0, max(0.0, t))
        return (float(x_line), y + t)

    for idx in range(ys.size):
        y = int(ys[idx])
        x = int(xs[idx])
        c = int(case[y, x])
        v_tl, v_tr, v_br, v_bl = float(tl[y, x]), float(tr[y, x]), float(br[y, x]), float(bl[y, x])
        top_k = ("H", y, x)
        bottom_k = ("H", y + 1, x)
        left_k = ("V", y, x)
        right_k = ("V", y, x + 1)
        top_p = interp_edge_x(y, x, v_tl, v_tr)
        bottom_p = interp_edge_x(y + 1, x, v_bl, v_br)
        left_p = interp_edge_y(x, y, v_tl, v_bl)
        right_p = interp_edge_y(x + 1, y, v_tr, v_br)

        if c in (1, 14):
            segments.append((left_k, left_p, bottom_k, bottom_p))
        elif c in (2, 13):
            segments.append((bottom_k, bottom_p, right_k, right_p))
        elif c in (3, 12):
            segments.append((left_k, left_p, right_k, right_p))
        elif c in (4, 11):
            segments.append((top_k, top_p, right_k, right_p))
        elif c == 5:
            if float(center[y, x]) > iso:
                segments.append((left_k, left_p, top_k, top_p))
                segments.append((bottom_k, bottom_p, right_k, right_p))
            else:
                segments.append((left_k, left_p, bottom_k, bottom_p))
                segments.append((top_k, top_p, right_k, right_p))
        elif c in (6, 9):
            segments.append((top_k, top_p, bottom_k, bottom_p))
        elif c == 10:
            if float(center[y, x]) > iso:
                segments.append((top_k, top_p, right_k, right_p))
                segments.append((left_k, left_p, bottom_k, bottom_p))
            else:
                segments.append((left_k, left_p, top_k, top_p))
                segments.append((bottom_k, bottom_p, right_k, right_p))
        elif c in (7, 8):
            segments.append((left_k, left_p, top_k, top_p))

    # Edge adjacency: each grid edge is crossed by at most one segment from
    # each adjacent cell (<= 2 segments total per edge).
    edge_map: dict[tuple, list[int]] = {}
    for i, (ka, _pa, kb, _pb) in enumerate(segments):
        edge_map.setdefault(ka, []).append(i)
        edge_map.setdefault(kb, []).append(i)

    used = np.zeros(len(segments), dtype=bool)
    loops: list[np.ndarray] = []
    for start in range(len(segments)):
        if used[start]:
            continue
        used[start] = True
        ka, pa, kb, pb = segments[start]
        loop: list[tuple[float, float]] = [pa, pb]
        entry_edge = kb
        guard = 0
        closed = False
        while guard < 10_000_000:
            guard += 1
            if entry_edge == ka:
                closed = True
                break
            nxt = None
            for cand in edge_map.get(entry_edge, ()):
                if not used[cand]:
                    nxt = cand
                    break
            if nxt is None:
                break
            used[nxt] = True
            cka, cpa, ckb, cpb = segments[nxt]
            if cka == entry_edge:
                loop.append(cpb)
                entry_edge = ckb
            else:
                loop.append(cpa)
                entry_edge = cka
        if closed and len(loop) >= 3:
            loops.append(np.asarray(loop, dtype=np.float64))
    return loops


# ----------------------------------------------------------------------
# Coordinate normalization + topology cleanup
# ----------------------------------------------------------------------

def signed_area(poly: np.ndarray) -> float:
    """Shoelace signed area (positive = CCW in x-right/y-UP space)."""
    x = poly[:, 0]
    y = poly[:, 1]
    return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def _crossing_contains(poly: np.ndarray, qx: float, qy: float) -> bool:
    """Vectorized crossing-number point-in-polygon test."""
    x0 = poly[:, 0]
    y0 = poly[:, 1]
    x1 = np.roll(x0, -1)
    y1 = np.roll(y0, -1)
    cond = (y0 > qy) != (y1 > qy)
    if not np.any(cond):
        return False
    xs = x0[cond]
    ys = y0[cond]
    xe = x1[cond]
    ye = y1[cond]
    x_cross = xs + (qy - ys) * (xe - xs) / np.where(np.abs(ye - ys) < 1e-12, 1e-12, ye - ys)
    return bool(np.count_nonzero(x_cross > qx) % 2 == 1)


def classify_topology(
    loops_px: list[np.ndarray],
    coverage: np.ndarray,
    mapping: CellMapping,
    min_area_upem: float = MIN_CONTOUR_AREA_UPEM,
) -> list[tuple[np.ndarray, bool, int | None]]:
    """Topology cleanup: area filter, hole classification, parent links.

    Returns (polygon_px, is_hole, parent_index) triples in extraction order.
    A contour's enclosed side is determined by sampling the coverage just
    inside it (ink-side => encloses ink; background-side => encloses a hole
    pocket). Nesting depth is counted with crossing-number tests against
    an interior sample point. All heavy work is vectorized NumPy.
    """
    if not loops_px:
        return []

    # Interior sample point per loop: midpoint of the longest edge nudged
    # toward the enclosed side along the edge normal.
    interiors: list[tuple[float, float]] = []
    enclosed_ink: list[bool] = []
    areas_upem: list[float] = []
    scale = mapping.upem_per_px
    for poly in loops_px:
        p0 = poly
        p1 = np.roll(poly, -1, axis=0)
        edge_len2 = np.sum((p1 - p0) ** 2, axis=1)
        i_max = int(np.argmax(edge_len2))
        mid = 0.5 * (p0[i_max] + p1[i_max])
        edge = p1[i_max] - p0[i_max]
        length = math.sqrt(float(edge_len2[i_max]))
        if length < 1e-9:
            interiors.append((float(mid[0]), float(mid[1])))
            enclosed_ink.append(False)
            areas_upem.append(0.0)
            continue
        normal = np.array([-edge[1], edge[0]]) / length
        centroid = poly.mean(axis=0)
        if float(np.dot(normal, centroid - mid)) < 0.0:
            normal = -normal
        probe_a = mid + 0.6 * normal
        probe_b = mid - 0.6 * normal

        def sample(p: np.ndarray) -> float:
            xi = int(round(float(p[0])))
            yi = int(round(float(p[1])))
            if 0 <= yi < coverage.shape[0] and 0 <= xi < coverage.shape[1]:
                return float(coverage[yi, xi])
            return 0.0

        sa, sb = sample(probe_a), sample(probe_b)
        if sa >= ISO_LEVEL and sb < ISO_LEVEL:
            interior = probe_a
            ink_inside = True
        elif sb >= ISO_LEVEL and sa < ISO_LEVEL:
            interior = probe_b
            ink_inside = True
        elif sa >= ISO_LEVEL and sb >= ISO_LEVEL:
            # Thick region: both sides ink near the boundary; the centroid-
            # directed probe_a is the deterministic interior choice.
            interior = probe_a
            ink_inside = True
        else:
            # Both sides background at the probe: the loop encloses a thin
            # background pocket (a hole between close strokes).
            interior = probe_a
            ink_inside = False
        interiors.append((float(interior[0]), float(interior[1])))
        enclosed_ink.append(bool(ink_inside))
        # Signed area in UPEM space (y flips => negate pixel-space area).
        areas_upem.append(abs(signed_area(poly)) * scale * scale)

    kept_idx = [i for i in range(len(loops_px)) if areas_upem[i] >= min_area_upem]

    # Nesting depth: count how many OTHER kept contours contain this
    # contour's interior sample point.
    result: list[tuple[np.ndarray, bool, int | None]] = []
    new_index: dict[int, int] = {}
    polys_kept = [loops_px[i] for i in kept_idx]
    interiors_kept = [interiors[i] for i in kept_idx]
    enclosed_kept = [enclosed_ink[i] for i in kept_idx]

    depths: list[int] = []
    for j, poly in enumerate(polys_kept):
        qx, qy = interiors_kept[j]
        depth = 0
        for k, other in enumerate(polys_kept):
            if k == j:
                continue
            if _crossing_contains(other, qx, qy):
                depth += 1
        depths.append(depth)

    for out_idx, src_idx in enumerate(kept_idx):
        new_index[src_idx] = out_idx

    for out_idx, src_idx in enumerate(kept_idx):
        is_hole = not enclosed_kept[out_idx] or (depths[out_idx] % 2 == 1)
        # Enclosed-ink contours at even depth are outer; enclosed-background
        # contours at odd depth are holes. Enclosed-ink at odd depth or
        # enclosed-background at even depth indicate inconsistent nesting:
        # fall back to depth parity (deterministic).
        if enclosed_kept[out_idx]:
            is_hole = depths[out_idx] % 2 == 1
        else:
            is_hole = depths[out_idx] % 2 == 1 or depths[out_idx] == 0
        parent_index = None
        if depths[out_idx] > 0:
            best = None
            best_area = float("inf")
            qx, qy = interiors_kept[out_idx]
            for k, other in enumerate(polys_kept):
                if k == out_idx or not _crossing_contains(other, qx, qy):
                    continue
                if depths[k] == depths[out_idx] - 1:
                    a = areas_upem[kept_idx[k]]
                    if a < best_area:
                        best_area = a
                        best = k
            parent_index = best
        result.append((polys_kept[out_idx], is_hole, parent_index))
    return result


# ----------------------------------------------------------------------
# Cubic Bezier fitting (error-bounded, vectorized least squares)
# ----------------------------------------------------------------------

def _fit_cubic_ls(points: np.ndarray) -> tuple[np.ndarray, float]:
    """Least-squares cubic with pinned endpoints; returns (ctrl, max_dev).

    ``points`` is an (N, 2) ordered slice including both endpoints. The
    Bernstein weights give a linear system for the two inner control points;
    the deviation is measured at the sample parameters themselves (cheap
    bounded FIT residual proxy).
    """
    n = points.shape[0]
    if n <= 2:
        ctrl = np.vstack([points[0], points[0], points[-1], points[-1]])
        return ctrl, 0.0
    t = np.cumsum(np.sqrt(np.sum(np.diff(points, axis=0) ** 2, axis=1)))
    total = float(t[-1])
    if total < 1e-9:
        ctrl = np.vstack([points[0], points[0], points[-1], points[-1]])
        return ctrl, 0.0
    t = np.concatenate([[0.0], t / total])
    u = 1.0 - t
    b0 = u ** 3
    b1 = 3.0 * u * u * t
    b2 = 3.0 * u * t * t
    b3 = t ** 3
    rhs = points - np.outer(b0, points[0]) - np.outer(b3, points[-1])
    A = np.column_stack([b1, b2])
    sol, *_ = np.linalg.lstsq(A, rhs, rcond=None)
    p1, p2 = sol[0], sol[1]
    fitted = (
        np.outer(b0, points[0]) + np.outer(b1, p1) + np.outer(b2, p2) + np.outer(b3, points[-1])
    )
    dev = float(np.max(np.sqrt(np.sum((fitted - points) ** 2, axis=1))))
    ctrl = np.vstack([points[0], p1, p2, points[-1]])
    return ctrl, dev


def fit_polyline_to_segments(
    poly_upem: np.ndarray,
    tol_upem: float = FIT_TOLERANCE_UPEM,
    max_segments: int = MAX_SEGMENTS_PER_CONTOUR,
) -> tuple[list[CubicSegment | LineSegment], float]:
    """Error-bounded cubic fit of one closed polygon (bounded simplification).

    The closed loop is opened at a deterministic anchor (the vertex farthest
    from the centroid - an extremum, usually near a corner) into two open
    spans, each fit by recursive chord-splitting with least-squares cubic
    fits: a span is accepted when its fit deviation is <= tol or when the
    segment budget is exhausted (bounded simplification never diverges).
    Collinear spans collapse to LineSegments. Returns
    (segments, worst_residual_upem); segments chain around the loop and the
    last endpoint returns to the anchor (closed).
    """
    n_pts = poly_upem.shape[0]
    if n_pts < 3:
        return [], float("inf")

    centroid = poly_upem.mean(axis=0)
    anchor = int(np.argmax(np.sum((poly_upem - centroid) ** 2, axis=1)))
    rotated = np.roll(poly_upem, -anchor, axis=0)
    opp = max(1, min(n_pts - 1, n_pts // 2))
    chain_a = rotated[0:opp + 1]
    chain_b = np.vstack([rotated[opp:], rotated[0:1]])

    segments: list[CubicSegment | LineSegment] = []
    worst = 0.0

    def fit_open(chain: np.ndarray) -> None:
        m = chain.shape[0]
        if m < 2:
            return

        def span(i0: int, i1: int) -> None:
            nonlocal worst
            if len(segments) >= max_segments:
                ctrl, dev = _fit_cubic_ls(chain[i0:i1 + 1])
                segments.append(_to_segment(ctrl))
                worst = max(worst, dev)
                return
            chunk = chain[i0:i1 + 1]
            chord = float(np.linalg.norm(chain[i1] - chain[i0]))
            if chord < 1e-6:
                return
            dists = _point_line_distances(chunk, chain[i0], chain[i1])
            if float(np.max(dists)) <= tol_upem:
                segments.append(
                    LineSegment(
                        Point2D(float(chain[i0][0]), float(chain[i0][1])),
                        Point2D(float(chain[i1][0]), float(chain[i1][1])),
                    )
                )
                worst = max(worst, float(np.max(dists)))
                return
            ctrl, dev = _fit_cubic_ls(chunk)
            if dev <= tol_upem or (i1 - i0) <= 2 or len(segments) >= max_segments - 1:
                segments.append(_to_segment(ctrl))
                worst = max(worst, dev)
                return
            # Split at the worst-deviating sample.
            i_mid = i0 + int(np.argmax(dists))
            if i_mid <= i0 or i_mid >= i1:
                i_mid = (i0 + i1) // 2
            span(i0, i_mid)
            span(i_mid, i1)

        span(0, m - 1)

    fit_open(chain_a)
    fit_open(chain_b)
    return segments, worst


def _point_line_distances(chunk: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    ab = b - a
    denom = float(np.linalg.norm(ab))
    if denom < 1e-12:
        return np.sqrt(np.sum((chunk - a) ** 2, axis=1))
    num = np.abs((chunk[:, 0] - a[0]) * ab[1] - (chunk[:, 1] - a[1]) * ab[0])
    return num / denom


def _to_segment(ctrl: np.ndarray) -> CubicSegment | LineSegment:
    p0 = Point2D(float(ctrl[0][0]), float(ctrl[0][1]))
    p1 = Point2D(float(ctrl[1][0]), float(ctrl[1][1]))
    p2 = Point2D(float(ctrl[2][0]), float(ctrl[2][1]))
    p3 = Point2D(float(ctrl[3][0]), float(ctrl[3][1]))
    return CubicSegment(p0, p1, p2, p3)


# ----------------------------------------------------------------------
# Vectorized even-odd rasterization (for cheap FIT confidence)
# ----------------------------------------------------------------------

def rasterize_segments_mask(
    contours: list[Contour],
    mapping: CellMapping,
    width: int,
    height: int,
    samples_per_curve: int = 8,
) -> np.ndarray:
    """Even-odd fill of fitted outlines into the cell mask (fully vectorized).

    Crossing counts are accumulated with np.add.at over the flattened
    (row, column) grid; the parity of the per-row cumulative sum is the
    fill mask. Holes are produced automatically by the even-odd rule.
    """
    grid = np.zeros((height, width + 1), dtype=np.int32)
    for contour in contours:
        for seg in contour.segments:
            if isinstance(seg, CubicSegment):
                ts = np.linspace(0.0, 1.0, samples_per_curve + 1)
                pts = [seg.evaluate(float(t)) for t in ts]
            else:
                pts = [seg.p0, seg.p1]
            arr = np.asarray([mapping.upem_to_px(p.x, p.y) for p in pts], dtype=np.float64)
            x0 = arr[:-1, 0]
            y0 = arr[:-1, 1]
            x1 = arr[1:, 0]
            y1 = arr[1:, 1]
            dy = y1 - y0
            valid = np.abs(dy) > 1e-9
            if not np.any(valid):
                continue
            for ei in np.nonzero(valid)[0]:
                ey0, ey1 = float(y0[ei]), float(y1[ei])
                lo, hi = (ey0, ey1) if ey0 < ey1 else (ey1, ey0)
                # Row centers are r + 0.5: count rows whose center lies in
                # [lo, hi). Half-open handling keeps vertex crossings counted
                # exactly once (shared vertices split cleanly).
                r_lo = max(0, int(math.ceil(lo - 0.5)))
                r_hi = min(height - 1, int(math.ceil(hi - 0.5)) - 1)
                if r_hi < r_lo:
                    continue
                rows = np.arange(r_lo, r_hi + 1) + 0.5
                xs = float(x0[ei]) + (rows - ey0) * (float(x1[ei]) - float(x0[ei])) / (ey1 - ey0)
                cols = np.clip(np.floor(xs).astype(np.int64), 0, width - 1)
                grid[rows.astype(np.int64), cols] += 1
    parity = np.cumsum(grid[:, :width], axis=1) & 1
    return parity.astype(bool)


# ----------------------------------------------------------------------
# Cheap FIT-confidence + fast geometry entry point
# ----------------------------------------------------------------------

def mask_iou(a: np.ndarray, b: np.ndarray) -> float:
    inter = int(np.count_nonzero(a & b))
    union = int(np.count_nonzero(a | b))
    if union == 0:
        return 1.0
    return inter / union


def structural_check(
    contours: list[Contour],
    regressed: RegressedMetrics,
    fit_residual_upem: float,
    observed_ink_area_upem: float | None = None,
) -> tuple[bool, tuple[str, ...]]:
    """ADR-0004 confidence checks: finite/closed contours, sane bbox/advance,
    topology/hole/component consistency, Bezier residual, metrics-regression
    residual, catastrophic self-intersection (net-area vs observed-ink-area
    divergence proxy), degenerate segments. Returns (ok, reasons).
    """
    reasons: list[str] = []
    if not math.isfinite(fit_residual_upem):
        reasons.append("NON_FINITE_FIT_RESIDUAL")
    elif fit_residual_upem > 3.0 * FIT_TOLERANCE_UPEM:
        reasons.append("BEZIER_RESIDUAL_EXCEEDED")
    if regressed.regression_residual > MAX_METRICS_RESIDUAL:
        reasons.append("METRICS_REGRESSION_RESIDUAL_EXCEEDED")

    for c in contours:
        if not c.segments:
            reasons.append("EMPTY_CONTOUR")
            continue
        if not c.is_closed:
            reasons.append("OPEN_CONTOUR")
        for s in c.segments:
            if s.approximate_length() < 1e-4:
                reasons.append("DEGENERATE_SEGMENT")
                break
        for s in c.segments:
            pts = [s.p0, s.p1] + ([s.p2, s.p3] if isinstance(s, CubicSegment) else [])
            if any(not (math.isfinite(p.x) and math.isfinite(p.y)) for p in pts):
                reasons.append("NON_FINITE_CONTROL_POINT")
                break

    # Sane bbox vs regressed metrics (tolerant, deterministic bounds).
    if contours:
        xs: list[float] = []
        ys: list[float] = []
        for c in contours:
            for s in c.segments:
                pts = [s.p0, s.p3 if isinstance(s, CubicSegment) else s.p1]
                xs.extend(p.x for p in pts)
                ys.extend(p.y for p in pts)
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        tol_x = 300.0
        tol_y = 300.0
        if x0 < regressed.lsb_upem - tol_x or x1 > regressed.advance_width_upem + tol_x:
            reasons.append("BBOX_X_OUT_OF_RANGE")
        if y0 < regressed.descent_upem - tol_y or y1 > regressed.ascent_upem + tol_y:
            reasons.append("BBOX_Y_OUT_OF_RANGE")

    # Catastrophic self-intersection / winding-consistency proxy: the NET
    # signed shoelace area of the fitted outlines (outers positive, holes
    # negative) must agree with the OBSERVED ink area. Self-crossing or
    # mis-wound outlines cancel winding and diverge from the observed ink.
    if (
        observed_ink_area_upem is not None
        and observed_ink_area_upem > MIN_CONTOUR_AREA_UPEM
        and contours
    ):
        net_area_upem = abs(signed_area_from_contours(contours))
        divergence = abs(net_area_upem - observed_ink_area_upem) / observed_ink_area_upem
        if divergence > MAX_SELF_INTERSECTION_AREA_DIVERGENCE:
            reasons.append("CATASTROPHIC_SELF_INTERSECTION")

    return (len(reasons) == 0, tuple(sorted(set(reasons))))


def signed_area_from_contours(contours: list[Contour]) -> float:
    """Signed shoelace area over sampled segment polylines (y-up space)."""
    total = 0.0
    for c in contours:
        pts: list[Point2D] = []
        for s in c.segments:
            if isinstance(s, CubicSegment):
                pts.extend(s.sample_points(8)[:-1])
            else:
                pts.append(s.p0)
        if len(pts) < 3:
            continue
        arr = np.asarray([(p.x, p.y) for p in pts], dtype=np.float64)
        total += signed_area(arr)
    return total


def fast_geometry_for_glyph(
    png_bytes: bytes,
    mapping: CellMapping,
    regressed: RegressedMetrics,
    cell_w: int,
    cell_h: int,
    tol_upem: float = FIT_TOLERANCE_UPEM,
    min_iou: float = MIN_IOU_EASY_PASS,
) -> tuple[GeometryEvidence, list[Contour], np.ndarray | None]:
    """One glyph of the fast geometry chain (U4).

    alpha decode -> normalization -> direct contour -> topology cleanup ->
    cubic fit -> bounded simplification -> cheap FIT-confidence. No SDF, no
    heavy optimizer. Returns (evidence, fitted contours, observed ink mask);
    the caller freezes PASS glyphs immediately and releases transients.
    """
    import time

    t0 = time.perf_counter()
    cp = regressed.code_point
    try:
        alpha = decode_alpha(png_bytes)
    except ValueError as exc:
        return (
            GeometryEvidence(cp, GlyphStatus.FAILED_GLYPH, reasons=(str(exc),)),
            [],
            None,
        )
    coverage = alpha_to_coverage(alpha)
    ink = coverage_to_ink(coverage)
    if not np.any(ink):
        # Zero-ink observation (space/empty): deterministic EASY_PASS with
        # no geometry, exactly as the canonical degeneracy is handled.
        ev = GeometryEvidence(
            code_point=cp,
            status=GlyphStatus.EASY_PASS,
            iou=1.0,
            structure_ok=True,
            metrics_residual=regressed.regression_residual,
            reasons=("ZERO_INK",),
            time_ms=(time.perf_counter() - t0) * 1000.0,
        )
        return ev, [], ink

    loops = marching_squares_contours(coverage)
    if not loops:
        return (
            GeometryEvidence(cp, GlyphStatus.FAILED_GLYPH, reasons=("NO_CONTOURS",)),
            [],
            ink,
        )

    topo = classify_topology(loops, coverage, mapping)
    contours: list[Contour] = []
    worst_residual = 0.0
    for poly_px, is_hole, parent_index in topo:
        upem_pts = np.asarray(
            [mapping.px_to_upem(float(x), float(y)) for (x, y) in poly_px],
            dtype=np.float64,
        )
        segments, residual = fit_polyline_to_segments(upem_pts, tol_upem=tol_upem)
        if not segments:
            continue
        # Canonical winding in design space (y-up): outers CCW (positive
        # shoelace area), holes CW. The shoelace area is computed directly
        # in upem coordinates (the px->upem y-mirror already happened).
        area = signed_area(upem_pts)
        if not is_hole and area < 0:
            segments = segments[::-1]
            segments = [_reverse_segment(s) for s in segments]
        elif is_hole and area > 0:
            segments = segments[::-1]
            segments = [_reverse_segment(s) for s in segments]
        contours.append(
            Contour(
                segments=segments,
                is_hole=is_hole,
                parent_index=parent_index,
                area_upem=abs(signed_area(upem_pts)),
            )
        )
        worst_residual = max(worst_residual, residual)

    if not contours:
        return (
            GeometryEvidence(cp, GlyphStatus.FAILED_GLYPH, reasons=("NO_FITTABLE_CONTOURS",)),
            [],
            ink,
        )

    observed_ink_area_upem = float(np.count_nonzero(ink)) * (mapping.upem_per_px ** 2)
    structure_ok, reasons = structural_check(
        contours, regressed, worst_residual, observed_ink_area_upem
    )

    fit_mask = rasterize_segments_mask(contours, mapping, cell_w, cell_h)
    iou = mask_iou(ink, fit_mask)

    ok = structure_ok and iou >= min_iou
    all_reasons = tuple(reasons) + (() if iou >= min_iou else (f"IOU_{iou:.4f}_BELOW_{min_iou:.2f}",))
    evidence = GeometryEvidence(
        code_point=cp,
        status=GlyphStatus.EASY_PASS if ok else GlyphStatus.FAILED_GLYPH,
        iou=iou,
        structure_ok=structure_ok,
        metrics_residual=regressed.regression_residual,
        reasons=all_reasons,
        low_confidence=False,
        time_ms=(time.perf_counter() - t0) * 1000.0,
    )
    if not ok:
        return evidence, contours, ink
    return evidence, contours, ink


def _reverse_segment(seg: CubicSegment | LineSegment) -> CubicSegment | LineSegment:
    if isinstance(seg, CubicSegment):
        return CubicSegment(seg.p3, seg.p2, seg.p1, seg.p0)
    return LineSegment(seg.p1, seg.p0)
