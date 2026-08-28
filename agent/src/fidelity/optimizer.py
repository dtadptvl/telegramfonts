"""Stage 9D: Deterministic fit-only outline optimizer with bounded convergence.

Optimizes reconstructed glyph outlines strictly against fit-partition raster
evidence. Held-out evidence never enters the objective, candidate selection,
stopping criterion, or retry logic (structural guarantee: the optimizer API
only ever receives fit records).

FULL MAX production loss vector (all five components are required and
computed on every objective evaluation; none is optional, no-op-able, or
caller-attested):
- coverage:  1 - IoU between model and reference masks;
- edge:      boundary-pixel mismatch between model and reference masks;
- sdf:       mean normalized signed-distance mismatch over mask boundaries;
- curvature: outline turning-angle energy (shape smoothness);
- complexity: outline segment-count penalty.

Guarantees:
- Finite objective (weighted loss vector over fit observations); non-finite
  objectives raise and fail closed.
- Explicit iteration budget and stop criterion (step exhaustion = CONVERGED,
  budget exhaustion = fail-closed non-convergence).
- Accepted objective trace is strictly non-increasing.
- Fully deterministic: no RNG, sorted glyph order, fixed candidate lattice.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

import numpy as np
from PIL import Image

from measurement.calibration import CalibrationTransform
from measurement.models import ObservationRecord
from reconstruction.models import Contour, CubicSegment, LineSegment, Point2D, ReconstructedGlyph

OPTIMIZER_VERSION = "stage9d-fit-only-outline-v2-lossvector"

# Canonical FULL MAX production loss vector. Fixed order and weights; the
# vector is closed — removing or no-op-ing any component invalidates the
# production objective (LOSS_VECTOR_COMPLETE).
REQUIRED_OPTIMIZATION_LOSSES: tuple[str, ...] = (
    "coverage",
    "edge",
    "sdf",
    "curvature",
    "complexity",
)
OPTIMIZATION_LOSS_WEIGHTS: dict[str, float] = {
    "coverage": 1.0,
    "edge": 0.5,
    "sdf": 0.25,
    "curvature": 0.05,
    "complexity": 0.01,
}
# Canonical complexity normalization: segment counts above this bound saturate
# the complexity penalty.
CANONICAL_MAX_OUTLINE_SEGMENTS = 128
# Deterministic pixel margin for precomputed reference crops and candidate
# rasterization windows (closed constant; part of the objective identity).
REFERENCE_CROP_MARGIN_PX = 2


class OptimizerNonFiniteObjectiveError(RuntimeError):
    """Raised when the objective evaluates to a non-finite value (fail-closed)."""


class OptimizerNonConvergenceError(RuntimeError):
    """Raised when the bounded budget is exhausted before convergence (fail-closed)."""


@dataclass(frozen=True)
class OptimizerPolicy:
    """Fixed, hashable optimizer constants bound into every convergence trace."""

    max_iterations: int = 240
    initial_translation_step_upem: float = 8.0
    initial_scale_step: float = 0.01
    min_translation_step_upem: float = 0.25
    min_scale_step: float = 0.0005
    convergence_tol: float = 1e-9
    samples_per_segment: int = 16

    def to_dict(self) -> dict[str, float | int]:
        return {
            "max_iterations": self.max_iterations,
            "initial_translation_step_upem": self.initial_translation_step_upem,
            "initial_scale_step": self.initial_scale_step,
            "min_translation_step_upem": self.min_translation_step_upem,
            "min_scale_step": self.min_scale_step,
            "convergence_tol": self.convergence_tol,
            "samples_per_segment": self.samples_per_segment,
        }


@dataclass(frozen=True)
class GlyphOptimizationRecord:
    """Per-glyph bounded convergence evidence."""

    code_point: int
    initial_objective: float
    final_objective: float
    iterations: int
    stop_reason: str
    accepted_objective_trace: tuple[float, ...]
    loss_components: tuple[tuple[str, float], ...] = ()
    selected_variant: str = "original"
    transform: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)


def recompute_objective_from_components(components: Mapping[str, float]) -> float:
    """Recompute the canonical weighted total from loss components."""
    return float(
        sum(OPTIMIZATION_LOSS_WEIGHTS[name] * float(components[name]) for name in REQUIRED_OPTIMIZATION_LOSSES)
    )


def validate_loss_vector_complete(record: GlyphOptimizationRecord) -> None:
    """Fail closed unless the loss vector is exact, real, and recomputably bound.

    Missing, extra, duplicate, non-finite, or no-op-substituted terms reject;
    canonical weights must be non-zero finite; the recorded final objective
    must equal the recomputed weighted total (no forged totals).
    """
    names = [name for name, _value in record.loss_components]
    if len(names) != len(set(names)):
        raise ValueError("OPTIMIZER_LOSS_VECTOR_DUPLICATE_TERM")
    if set(names) != set(REQUIRED_OPTIMIZATION_LOSSES):
        missing = set(REQUIRED_OPTIMIZATION_LOSSES) - set(names)
        extra = set(names) - set(REQUIRED_OPTIMIZATION_LOSSES)
        raise ValueError(
            f"OPTIMIZER_LOSS_VECTOR_INCOMPLETE:missing={sorted(missing)}:extra={sorted(extra)}"
        )
    for name in REQUIRED_OPTIMIZATION_LOSSES:
        weight = OPTIMIZATION_LOSS_WEIGHTS[name]
        if not (math.isfinite(weight) and weight > 0.0):
            raise ValueError(f"OPTIMIZER_LOSS_WEIGHT_INVALID:{name}")
    components = dict(record.loss_components)
    for name in REQUIRED_OPTIMIZATION_LOSSES:
        if not math.isfinite(float(components[name])):
            raise ValueError(f"OPTIMIZER_LOSS_NON_FINITE:{name}")
    recomputed = recompute_objective_from_components(components)
    if abs(recomputed - float(record.final_objective)) > 1e-9:
        raise ValueError("OPTIMIZER_LOSS_TOTAL_FORGED")


@dataclass(frozen=True)
class OptimizationTrace:
    """Reproducible convergence trace bound to exact fit evidence and policy."""

    optimizer_version: str
    input_fingerprint: str
    policy: OptimizerPolicy
    records: tuple[GlyphOptimizationRecord, ...]
    total_iterations: int
    converged: bool
    stop_reason: str

    def compute_trace_hash(self) -> str:
        payload = {
            "optimizer_version": self.optimizer_version,
            "input_fingerprint": self.input_fingerprint,
            "policy": self.policy.to_dict(),
            "loss_vector": {k: OPTIMIZATION_LOSS_WEIGHTS[k] for k in REQUIRED_OPTIMIZATION_LOSSES},
            "records": [
                {
                    "code_point": r.code_point,
                    "initial_objective": repr(r.initial_objective),
                    "final_objective": repr(r.final_objective),
                    "iterations": r.iterations,
                    "stop_reason": r.stop_reason,
                    "accepted_objective_trace": [repr(v) for v in r.accepted_objective_trace],
                    "loss_components": [[name, repr(value)] for name, value in r.loss_components],
                    "selected_variant": r.selected_variant,
                    "transform": [repr(v) for v in r.transform],
                }
                for r in self.records
            ],
            "total_iterations": self.total_iterations,
            "converged": self.converged,
            "stop_reason": self.stop_reason,
        }
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _transform_contours(
    contours: Sequence[Contour],
    dx: float,
    dy: float,
    scale: float,
    center_x: float,
    center_y: float,
    scale_y: float | None = None,
) -> list[Contour]:
    """Apply deterministic translate + (an)isotropic scale about a fixed center.

    ``scale`` is the X-axis scale; ``scale_y`` defaults to ``scale``
    (uniform). Anisotropic scaling is part of the causal search space: it
    changes rasterized coverage/edge/SDF evidence AND the curvature term.
    """
    sy = scale if scale_y is None else scale_y

    def tp(p: Point2D) -> Point2D:
        return Point2D(
            center_x + scale * (p.x - center_x) + dx,
            center_y + sy * (p.y - center_y) + dy,
        )

    transformed: list[Contour] = []
    for c in contours:
        new_segments = []
        for s in c.segments:
            if isinstance(s, CubicSegment):
                new_segments.append(CubicSegment(p0=tp(s.p0), p1=tp(s.p1), p2=tp(s.p2), p3=tp(s.p3)))
            else:
                new_segments.append(LineSegment(p0=tp(s.p0), p1=tp(s.p1)))
        transformed.append(
            Contour(
                segments=new_segments,
                is_hole=c.is_hole,
                parent_index=c.parent_index,
                area_upem=c.area_upem * scale * sy,
            )
        )
    return transformed


def _midpoint(a: Point2D, b: Point2D) -> Point2D:
    return Point2D((a.x + b.x) / 2.0, (a.y + b.y) / 2.0)


def _subdivide_contours(contours: Sequence[Contour]) -> list[Contour]:
    """Deterministic segment subdivision (de Casteljau at t=0.5).

    Doubles the segment count WITHOUT changing the represented shape:
    identical raster evidence, strictly higher complexity, and a distinct
    curvature sampling — the complexity/curvature terms therefore causally
    participate in candidate selection.
    """
    subdivided: list[Contour] = []
    for c in contours:
        new_segments = []
        for s in c.segments:
            if isinstance(s, CubicSegment):
                q0 = _midpoint(s.p0, s.p1)
                q1 = _midpoint(s.p1, s.p2)
                q2 = _midpoint(s.p2, s.p3)
                r0 = _midpoint(q0, q1)
                r1 = _midpoint(q1, q2)
                m = _midpoint(r0, r1)
                new_segments.append(CubicSegment(p0=s.p0, p1=q0, p2=r0, p3=m))
                new_segments.append(CubicSegment(p0=m, p1=r1, p2=q2, p3=s.p3))
            else:
                m = _midpoint(s.p0, s.p1)
                new_segments.append(LineSegment(p0=s.p0, p1=m))
                new_segments.append(LineSegment(p0=m, p1=s.p1))
        subdivided.append(
            Contour(
                segments=new_segments,
                is_hole=c.is_hole,
                parent_index=c.parent_index,
                area_upem=c.area_upem,
            )
        )
    return subdivided


_SIMPLIFY_EPSILON_UPEM = 0.75


def _point_segment_distance(p: Point2D, a: Point2D, b: Point2D) -> float:
    abx = b.x - a.x
    aby = b.y - a.y
    denom = math.hypot(abx, aby)
    if denom == 0.0:
        return math.hypot(p.x - a.x, p.y - a.y)
    t = max(0.0, min(1.0, ((p.x - a.x) * abx + (p.y - a.y) * aby) / (denom * denom)))
    proj_x = a.x + t * abx
    proj_y = a.y + t * aby
    return math.hypot(p.x - proj_x, p.y - proj_y)


def _simplify_contours(contours: Sequence[Contour]) -> list[Contour]:
    """Deterministic simplification: merge near-collinear LINE segments.

    Reduces segment count below the original when the source outline
    contains redundant line corners; cubic segments are never altered.
    The segment-count (complexity) term causally prefers the cheapest
    shape-equal representation.
    """
    simplified: list[Contour] = []
    for c in contours:
        if any(isinstance(s, CubicSegment) for s in c.segments):
            simplified.append(c)
            continue
        pts = [s.p0 for s in c.segments]
        if len(pts) <= 3:
            simplified.append(c)
            continue
        kept: list[Point2D] = [pts[0]]
        i = 1
        while i < len(pts):
            if i + 1 < len(pts) and _point_segment_distance(pts[i], kept[-1], pts[i + 1]) < _SIMPLIFY_EPSILON_UPEM:
                i += 1
                continue
            kept.append(pts[i])
            i += 1
        while len(kept) > 3 and _point_segment_distance(kept[0], kept[-1], kept[1]) < _SIMPLIFY_EPSILON_UPEM:
            kept = kept[1:]
        if len(kept) < 3:
            simplified.append(c)
            continue
        segments = [
            LineSegment(p0=kept[j], p1=kept[(j + 1) % len(kept)]) for j in range(len(kept))
        ]
        simplified.append(
            Contour(
                segments=segments,
                is_hole=c.is_hole,
                parent_index=c.parent_index,
                area_upem=c.area_upem,
            )
        )
    return simplified


VARIANT_ORDER: tuple[str, ...] = ("original", "simplified", "subdivided")


class FitOnlyGlyphOptimizer:
    """Bounded deterministic coordinate-descent optimizer over fit raster evidence."""

    def __init__(self, policy: OptimizerPolicy | None = None) -> None:
        self.policy = policy or OptimizerPolicy()
        if self.policy.max_iterations <= 0:
            raise ValueError("OPTIMIZER_POLICY_INVALID: max_iterations must be positive")

    @staticmethod
    def compute_input_fingerprint(fit_records: Sequence[ObservationRecord]) -> str:
        payload = sorted((r.cache_key, r.raster_sha256) for r in fit_records)
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _decode_mask(png_bytes: bytes, resolution: int) -> np.ndarray:
        img = Image.open(io.BytesIO(png_bytes)).convert("L")
        if img.size != (resolution, resolution):
            raise ValueError("OPTIMIZER_RASTER_SIZE_MISMATCH")
        arr = np.array(img, dtype=np.float32) / 255.0
        return ((1.0 - arr) >= 0.5).astype(np.uint8)

    @classmethod
    def _rasterize_contours(
        cls,
        contours: Sequence[Contour],
        transform: CalibrationTransform,
        resolution: int,
        samples_per_segment: int,
    ) -> np.ndarray:
        from PIL import ImageDraw

        img = Image.new("L", (resolution, resolution), 0)
        draw = ImageDraw.Draw(img)
        outer = [c for c in contours if not c.is_hole]
        holes = [c for c in contours if c.is_hole]
        for c in outer:
            pts = c.sample_points(samples_per_segment=samples_per_segment)
            if len(pts) < 3:
                continue
            draw.polygon([transform.inverse(p.x, p.y) for p in pts], fill=255)
        for c in holes:
            pts = c.sample_points(samples_per_segment=samples_per_segment)
            if len(pts) < 3:
                continue
            draw.polygon([transform.inverse(p.x, p.y) for p in pts], fill=0)
        arr = np.array(img, dtype=np.uint8)
        return (arr > 127).astype(np.uint8)

    def _objective(
        self,
        contours: Sequence[Contour],
        prepared: Sequence[tuple[CalibrationTransform, np.ndarray, int]],
    ) -> float:
        components = self._loss_components(contours, prepared)
        objective = sum(
            OPTIMIZATION_LOSS_WEIGHTS[name] * components[name]
            for name in REQUIRED_OPTIMIZATION_LOSSES
        )
        if not math.isfinite(objective):
            raise OptimizerNonFiniteObjectiveError("OPTIMIZER_NON_FINITE_OBJECTIVE")
        return float(objective)

    @staticmethod
    def _boundary(mask: np.ndarray) -> np.ndarray:
        """4-neighborhood boundary pixels of a binary mask."""
        if mask.size == 0:
            return mask.astype(bool)
        padded = np.pad(mask, 1, mode="constant", constant_values=0)
        interior = (
            padded[1:-1, 2:]
            & padded[1:-1, :-2]
            & padded[2:, 1:-1]
            & padded[:-2, 1:-1]
        )
        return mask.astype(bool) & ~interior

    @staticmethod
    def _prepare_reference_artifacts(
        ref_mask: np.ndarray, margin: int = REFERENCE_CROP_MARGIN_PX
    ) -> dict[str, Any]:
        """Compute the exact reference-side artifacts once per observation.

        The crop is the reference ink bounding box expanded by the closed
        pixel margin; ref_edge / sd_ref / ref_count are derived from that
        crop and never recomputed per candidate, so the objective stays
        computable at MAX schedule scale while remaining exact on the
        sampled region.
        """
        ref_bool = ref_mask.astype(bool)
        ref_count = int(ref_bool.sum())
        if ref_count == 0:
            return {
                "crop": (0, 0, 0, 0),
                "ref_crop": ref_bool[0:0, 0:0],
                "ref_edge": ref_bool[0:0, 0:0],
                "sd_ref": np.zeros((0, 0), dtype=np.float64),
                "ref_count": 0,
            }
        ys, xs = np.nonzero(ref_bool)
        y0 = max(int(ys.min()) - margin, 0)
        y1 = min(int(ys.max()) + margin + 1, ref_bool.shape[0])
        x0 = max(int(xs.min()) - margin, 0)
        x1 = min(int(xs.max()) + margin + 1, ref_bool.shape[1])
        ref_crop = ref_bool[y0:y1, x0:x1]
        return {
            "crop": (y0, y1, x0, x1),
            "ref_crop": ref_crop,
            "ref_edge": FitOnlyGlyphOptimizer._boundary(ref_crop),
            "sd_ref": FitOnlyGlyphOptimizer._signed_distance(ref_crop),
            "ref_count": ref_count,
        }

    @classmethod
    def _rasterize_model_crop(
        cls,
        contours: Sequence[Contour],
        transform: CalibrationTransform,
        resolution: int,
        samples_per_segment: int,
        crop: tuple[int, int, int, int],
        margin: int = REFERENCE_CROP_MARGIN_PX,
    ) -> tuple[np.ndarray, int]:
        """Exact crop-window rasterization of transformed contours.

        Renders into the smallest canvas covering the union of the reference
        crop window and the exact transformed-contour pixel bounding box, so
        the returned crop equals the full-canvas model mask restricted to the
        reference window and the returned outside-crop ink count is exact.
        Never allocates or converts a full scheduled canvas.
        """
        from PIL import ImageDraw

        y0, y1, x0, x1 = crop
        # Sample every contour exactly once; the affine image of the sampled
        # point set bounds the transformed contour pixels, so its pixel
        # bounding box is the exact render-window bound.
        sampled_outer: list[list[tuple[float, float]]] = []
        sampled_holes: list[list[tuple[float, float]]] = []
        all_px: list[tuple[float, float]] = []
        for c in contours:
            pts = c.sample_points(samples_per_segment=samples_per_segment)
            if len(pts) < 3:
                continue
            pix = [transform.inverse(p.x, p.y) for p in pts]
            if c.is_hole:
                sampled_holes.append(pix)
            else:
                sampled_outer.append(pix)
            all_px.extend(pix)

        if all_px:
            mx0 = min(px for px, _py in all_px) - margin
            mx1 = max(px for px, _py in all_px) + margin
            my0 = min(py for _px, py in all_px) - margin
            my1 = max(py for _px, py in all_px) + margin
        else:
            mx0, mx1, my0, my1 = float(x0), float(x1), float(y0), float(y1)

        uy0 = max(int(math.floor(min(my0, float(y0)))), 0)
        uy1 = min(int(math.ceil(max(my1, float(y1)))), resolution)
        ux0 = max(int(math.floor(min(mx0, float(x0)))), 0)
        ux1 = min(int(math.ceil(max(mx1, float(x1)))), resolution)
        if uy1 <= uy0 or ux1 <= ux0:
            crop_shape = (max(y1 - y0, 0), max(x1 - x0, 0))
            return np.zeros(crop_shape, dtype=bool), 0

        img = Image.new("L", (ux1 - ux0, uy1 - uy0), 0)
        draw = ImageDraw.Draw(img)
        for pix in sampled_outer:
            draw.polygon([(px - ux0, py - uy0) for px, py in pix], fill=255)
        for pix in sampled_holes:
            draw.polygon([(px - ux0, py - uy0) for px, py in pix], fill=0)
        canvas = (np.array(img, dtype=np.uint8) > 127)
        model_total = int(canvas.sum())
        model_crop = canvas[y0 - uy0 : y1 - uy0, x0 - ux0 : x1 - ux0]
        return model_crop, model_total - int(model_crop.sum())

    @staticmethod
    def _signed_distance(mask: np.ndarray) -> np.ndarray:
        """Signed distance field: negative inside, positive outside.

        Deterministic exact chamfer (L2 3-4) distances via scipy; the sign
        convention makes the SDF loss a real signed-distance mismatch, not
        an unsigned foreground distance.
        """
        from scipy.ndimage import distance_transform_cdt

        mask_bool = mask.astype(bool)
        dist_to_foreground = distance_transform_cdt(~mask_bool)
        dist_to_complement = distance_transform_cdt(mask_bool)
        return np.where(mask_bool, -dist_to_complement, dist_to_foreground).astype(np.float64)

    def _curvature_loss(self, contours: Sequence[Contour]) -> float:
        """Outline turning-angle energy normalized to [0, 1]; shape-only term."""
        angles: list[float] = []
        for c in contours:
            pts = c.sample_points(samples_per_segment=self.policy.samples_per_segment)
            if len(pts) < 3:
                continue
            for i in range(1, len(pts) - 1):
                v1x = pts[i].x - pts[i - 1].x
                v1y = pts[i].y - pts[i - 1].y
                v2x = pts[i + 1].x - pts[i].x
                v2y = pts[i + 1].y - pts[i].y
                n1 = math.hypot(v1x, v1y)
                n2 = math.hypot(v2x, v2y)
                if n1 == 0.0 or n2 == 0.0:
                    continue
                cos_t = max(-1.0, min(1.0, (v1x * v2x + v1y * v2y) / (n1 * n2)))
                angles.append(abs(math.acos(cos_t)))
        if not angles:
            return 1.0
        return float(min(1.0, (sum(angles) / len(angles)) / math.pi))

    def _complexity_loss(self, contours: Sequence[Contour]) -> float:
        """Outline segment-count penalty normalized by the canonical bound."""
        total_segments = sum(len(c.segments) for c in contours)
        return float(min(1.0, total_segments / float(CANONICAL_MAX_OUTLINE_SEGMENTS)))

    def _loss_components(
        self,
        contours: Sequence[Contour],
        prepared: Sequence[tuple],
    ) -> dict[str, float]:
        """Compute the complete required loss vector; every component is real.

        Prepared entries are ``(transform, ref_mask, resolution)`` or extended
        ``(transform, ref_mask, resolution, precomputed)`` where the
        precomputed dict carries the exact reference-side artifacts
        (crop/ref_edge/sd_ref/ref_count) computed once per observation.
        """
        if not prepared:
            raise ValueError("OPTIMIZER_NO_FIT_OBSERVATIONS")
        coverage_terms: list[float] = []
        edge_terms: list[float] = []
        sdf_terms: list[float] = []
        for entry in prepared:
            transform, ref_mask, resolution = entry[0], entry[1], entry[2]
            pre = entry[3] if len(entry) > 3 else None

            if pre is not None:
                # Precomputed reference artifacts: the model is rasterized
                # only inside the exact union of the reference crop window
                # and the candidate pixel bounding box (never a full
                # scheduled canvas); outside-crop model ink stays counted
                # exactly in the coverage union.
                crop = pre["crop"]
                ref_crop = pre["ref_crop"]
                ref_edge = pre["ref_edge"]
                sd_ref = pre["sd_ref"]
                ref_count = pre["ref_count"]
                model_crop, outside_model = self._rasterize_model_crop(
                    contours,
                    transform,
                    resolution,
                    self.policy.samples_per_segment,
                    crop,
                )
                intersection = int(np.logical_and(model_crop, ref_crop).sum())
                model_crop_count = int(model_crop.sum())
                union = (
                    intersection
                    + (model_crop_count - intersection)
                    + (ref_count - intersection)
                    + outside_model
                )
                coverage_terms.append(1.0 - (float(intersection) / max(union, 1)))

                model_edge = self._boundary(model_crop)
                edge_mismatch = int(np.logical_xor(model_edge, ref_edge).sum())
                edge_denom = max(int(np.logical_or(model_edge, ref_edge).sum()), 1)
                edge_terms.append(min(1.0, float(edge_mismatch) / float(edge_denom)))

                if model_crop_count == 0 and ref_count == 0:
                    sdf_terms.append(0.0)
                elif model_crop_count == 0 or ref_count == 0:
                    sdf_terms.append(1.0)
                else:
                    sd_model = self._signed_distance(model_crop)
                    mean_abs = float(np.abs(sd_model - sd_ref).mean())
                    sdf_terms.append(min(1.0, mean_abs / float(max(resolution, 1))))
                continue

            model_mask = self._rasterize_contours(
                contours, transform, resolution, self.policy.samples_per_segment
            ).astype(bool)
            ref_bool = ref_mask.astype(bool)

            intersection = int(np.logical_and(model_mask, ref_bool).sum())
            union = int(np.logical_or(model_mask, ref_bool).sum())
            coverage_terms.append(1.0 - (float(intersection) / max(union, 1)))

            model_edge = self._boundary(model_mask)
            ref_edge = self._boundary(ref_bool)
            edge_mismatch = int(np.logical_xor(model_edge, ref_edge).sum())
            edge_denom = max(int(np.logical_or(model_edge, ref_edge).sum()), 1)
            edge_terms.append(min(1.0, float(edge_mismatch) / float(edge_denom)))

            union_mask = np.logical_or(model_mask, ref_bool)
            union_count = int(union_mask.sum())
            if union_count == 0:
                sdf_terms.append(0.0)
            elif not model_mask.any() or not ref_bool.any():
                # One side empty: maximal signed-distance mismatch.
                sdf_terms.append(1.0)
            else:
                # Signed-SDF mismatch over the union bounding region
                # (interior sign flips are penalized, not only exterior
                # foreground distance).
                ys, xs = np.nonzero(union_mask)
                margin = 2
                y0 = max(int(ys.min()) - margin, 0)
                y1 = min(int(ys.max()) + margin + 1, union_mask.shape[0])
                x0 = max(int(xs.min()) - margin, 0)
                x1 = min(int(xs.max()) + margin + 1, union_mask.shape[1])
                sd_model = self._signed_distance(model_mask[y0:y1, x0:x1])
                sd_ref = self._signed_distance(ref_bool[y0:y1, x0:x1])
                mean_abs = float(np.abs(sd_model - sd_ref).mean())
                sdf_terms.append(min(1.0, mean_abs / float(max(resolution, 1))))

        components = {
            "coverage": float(np.mean(coverage_terms)),
            "edge": float(np.mean(edge_terms)),
            "sdf": float(np.mean(sdf_terms)),
            "curvature": self._curvature_loss(contours),
            "complexity": self._complexity_loss(contours),
        }
        for name in REQUIRED_OPTIMIZATION_LOSSES:
            if not math.isfinite(components[name]):
                raise OptimizerNonFiniteObjectiveError(
                    f"OPTIMIZER_NON_FINITE_OBJECTIVE:{name}"
                )
        return components

    def optimize_glyph(
        self,
        glyph: ReconstructedGlyph,
        prepared: Sequence[tuple[CalibrationTransform, np.ndarray, int]],
        fail_on_budget_exhaustion: bool = True,
        allow_scale_search: bool = True,
    ) -> tuple[ReconstructedGlyph, GlyphOptimizationRecord]:
        """Optimize one glyph's outline; fail-closed on non-convergence.

        ``fail_on_budget_exhaustion`` preserves the canonical FULL MAX
        fail-closed semantics (budget exhaustion raises). Versioned
        reduced-budget search schedules (BALANCED_MAX ladder tiers) set
        it to False to keep the best valid candidate deterministically;
        the record still carries the honest stop reason and every
        downstream unchanged gate still applies.

        ``allow_scale_search`` is a call-time search-space bound (never
        part of the canonical policy identity): coarse ladder tiers that
        run on observation subsets set it to False because subset
        evidence cannot reliably identify anisotropic scale, and a
        spurious coarse scale decision does not generalize to the
        complete evidence set. Canonical callers use the default True.

        Causal search space: the deterministic segment-variant lattice
        (original / simplified / subdivided) crossed with translate +
        anisotropic-scale coordinate descent, so every required loss term
        (coverage, edge, signed SDF, curvature, complexity) can causally
        affect candidate selection.
        """
        x0, y0, x1, y1 = glyph.bounding_box_upem
        center_x = (x0 + x1) / 2.0
        center_y = (y0 + y1) / 2.0

        variant_bases: dict[str, list[Contour]] = {
            "original": list(glyph.contours),
            "simplified": _simplify_contours(glyph.contours),
            "subdivided": _subdivide_contours(glyph.contours),
        }

        # Deterministic variant start: best identity-transform objective.
        best_variant = VARIANT_ORDER[0]
        best_base = variant_bases[best_variant]
        best = self._objective(best_base, prepared)
        for name in VARIANT_ORDER[1:]:
            obj = self._objective(variant_bases[name], prepared)
            if obj < best:
                best_variant = name
                best_base = variant_bases[name]
                best = obj

        dx = 0.0
        dy = 0.0
        sx = 1.0
        sy = 1.0
        current_contours = best_base
        initial = best
        accepted_trace: list[float] = [best]
        iterations = 0

        step_t = self.policy.initial_translation_step_upem
        step_s = self.policy.initial_scale_step
        tol = self.policy.convergence_tol
        stop_reason = "ITERATION_BUDGET_EXHAUSTED"

        search_dim_names = ("dx", "dy")
        if allow_scale_search:
            search_dim_names = search_dim_names + ("sx", "sy")

        while iterations < self.policy.max_iterations:
            improved = False
            # Rebuilt every round from the LIVE step sizes: canonical
            # coordinate-descent annealing halves step_t/step_s after a
            # non-improving round, and the next round must search with the
            # halved deltas. Freezing the initial deltas outside the loop
            # regressed canonical FULL MAX fit quality (issue #86 probe:
            # deterministic 0.3905/0.4045 vs frozen-envelope 0.2946/0.3225).
            search_dims = [
                (dim, (-step_t, step_t) if dim in ("dx", "dy") else (-step_s, step_s))
                for dim in search_dim_names
            ]
            for dim, delta_candidates in search_dims:
                for delta in delta_candidates:
                    if iterations >= self.policy.max_iterations:
                        break
                    cand_dx = dx + (delta if dim == "dx" else 0.0)
                    cand_dy = dy + (delta if dim == "dy" else 0.0)
                    cand_sx = sx + (delta if dim == "sx" else 0.0)
                    cand_sy = sy + (delta if dim == "sy" else 0.0)
                    if cand_sx <= 0.0 or cand_sy <= 0.0:
                        iterations += 1
                        continue
                    cand_contours = _transform_contours(
                        best_base, cand_dx, cand_dy, cand_sx, center_x, center_y,
                        scale_y=cand_sy,
                    )
                    iterations += 1
                    obj = self._objective(cand_contours, prepared)
                    if obj < best - tol:
                        dx, dy, sx, sy = cand_dx, cand_dy, cand_sx, cand_sy
                        current_contours = cand_contours
                        best = obj
                        accepted_trace.append(best)
                        improved = True
                        break

            if not improved:
                step_t /= 2.0
                step_s /= 2.0
                if step_t < self.policy.min_translation_step_upem and step_s < self.policy.min_scale_step:
                    stop_reason = "CONVERGED"
                    break

        converged = stop_reason == "CONVERGED"
        final_components = self._loss_components(current_contours, prepared)
        record = GlyphOptimizationRecord(
            code_point=glyph.code_point,
            initial_objective=initial,
            final_objective=best,
            iterations=iterations,
            stop_reason=stop_reason,
            accepted_objective_trace=tuple(accepted_trace),
            loss_components=tuple(
                (name, final_components[name]) for name in REQUIRED_OPTIMIZATION_LOSSES
            ),
            selected_variant=best_variant,
            transform=(dx, dy, sx, sy),
        )
        validate_loss_vector_complete(record)
        if not converged and fail_on_budget_exhaustion:
            raise OptimizerNonConvergenceError(
                f"OPTIMIZER_NON_CONVERGENCE_CP_{glyph.code_point}"
            )

        x_min = min((p.x for c in current_contours for p in c.sample_points(samples_per_segment=8)), default=x0)
        x_max = max((p.x for c in current_contours for p in c.sample_points(samples_per_segment=8)), default=x1)
        y_min = min((p.y for c in current_contours for p in c.sample_points(samples_per_segment=8)), default=y0)
        y_max = max((p.y for c in current_contours for p in c.sample_points(samples_per_segment=8)), default=y1)

        optimized = ReconstructedGlyph(
            code_point=glyph.code_point,
            character=glyph.character,
            advance_width_upem=glyph.advance_width_upem,
            lsb_upem=glyph.lsb_upem,
            rsb_upem=glyph.rsb_upem,
            ascent_upem=glyph.ascent_upem,
            descent_upem=glyph.descent_upem,
            contours=current_contours,
            bounding_box_upem=(x_min, y_min, x_max, y_max),
            reconstruction_time_ms=glyph.reconstruction_time_ms,
        )
        return optimized, record

    def prepare_glyph_observations(
        self,
        cp_records: Sequence[ObservationRecord],
        raster_provider: Callable[[ObservationRecord], bytes],
        units_per_em: int = 1000,
        cache: Any = None,
    ) -> list[tuple]:
        """Prepare one glyph's fit observations for objective evaluation.

        Exact semantics of the canonical ``optimize`` preparation loop:
        raster bytes are size+SHA256 verified against the sealed records,
        the calibration transform is derived per observation, and the
        reference-side artifacts (crop/edge/signed distance/ink count)
        are computed exactly once per observation.

        ``cache`` is an optional exact-identity intermediate cache
        (decode + prepared artifacts). Cached entries are pure
        derivations of the sealed, hash-verified observation bytes; any
        stale/cross-identity entry fails closed to recomputation. FULL
        MAX callers pass no cache and keep the canonical behavior.
        """
        if not cp_records:
            raise ValueError("OPTIMIZER_NO_FIT_OBSERVATIONS")
        identity = {
            (r.reference_id, r.style_id, r.browser_version, r.config_hash)
            for r in cp_records
        }
        if len(identity) != 1:
            raise ValueError("OPTIMIZER_MIXED_EVIDENCE_IDENTITY")

        prepared: list[tuple] = []
        for r in sorted(cp_records, key=lambda rec: rec.cache_key):
            png_bytes = raster_provider(r)
            if not isinstance(png_bytes, bytes) or len(png_bytes) != r.raster_size_bytes:
                raise ValueError("OPTIMIZER_RASTER_BYTES_MISMATCH")
            if hashlib.sha256(png_bytes).hexdigest() != r.raster_sha256:
                raise ValueError("OPTIMIZER_RASTER_SHA_MISMATCH")
            transform = CalibrationTransform.from_observation(
                resolution=r.resolution,
                metrics=r.metrics,
                subpixel_x=r.subpixel_x,
                subpixel_y=r.subpixel_y,
                units_per_em=units_per_em,
            )
            if cache is not None:
                decode_key = cache.decode_key(
                    r.raster_sha256, r.resolution, r.raster_size_bytes
                )
                ref_mask = cache.get_decode(decode_key)
                if ref_mask is None:
                    ref_mask = self._decode_mask(png_bytes, r.resolution)
                    cache.put_decode(decode_key, ref_mask)
                prepare_key = cache.prepare_key(r.cache_key, r.raster_sha256, r.resolution)
                precomputed = cache.get_prepared(prepare_key)
                if precomputed is None:
                    precomputed = self._prepare_reference_artifacts(ref_mask)
                    cache.put_prepared(prepare_key, precomputed)
            else:
                ref_mask = self._decode_mask(png_bytes, r.resolution)
                # Production preparation: reference-side artifacts (crop,
                # edge, signed distance, ink count) are computed exactly
                # once per observation and bound into every objective
                # evaluation of this glyph. The full-resolution mask is
                # released immediately after preparation.
                precomputed = self._prepare_reference_artifacts(ref_mask)
                del ref_mask
            prepared.append((transform, None, r.resolution, precomputed))
        return prepared

    def score_glyph(
        self,
        contours: Sequence[Contour],
        prepared: Sequence[tuple],
    ) -> tuple[float, dict[str, float]]:
        """Score candidate contours against prepared fit evidence.

        Read-only authoritative scoring used by the full-resolution
        rerank: returns the canonical weighted objective and the
        complete loss vector. Never mutates state; never consumes
        held-out evidence (callers pass fit-prepared observations only).
        """
        components = self._loss_components(contours, prepared)
        objective = sum(
            OPTIMIZATION_LOSS_WEIGHTS[name] * components[name]
            for name in REQUIRED_OPTIMIZATION_LOSSES
        )
        if not math.isfinite(objective):
            raise OptimizerNonFiniteObjectiveError("OPTIMIZER_NON_FINITE_OBJECTIVE")
        return float(objective), components

    def optimize(
        self,
        glyphs: Mapping[int, ReconstructedGlyph],
        fit_records: Sequence[ObservationRecord],
        raster_provider: Callable[[ObservationRecord], bytes],
        units_per_em: int = 1000,
        cache: Any = None,
    ) -> tuple[dict[int, ReconstructedGlyph], OptimizationTrace]:
        """Optimize all glyphs strictly against fit evidence; fail-closed on any non-convergence.

        ``cache`` optionally supplies the exact-identity intermediate cache
        (reuse of compatible decode/prepare artifacts). It never skips any
        scheduled observation: every fit record is still prepared and
        consumed; only the deterministic preprocessing may be reused.
        """
        if not fit_records:
            raise ValueError("OPTIMIZER_NO_FIT_OBSERVATIONS")
        if not glyphs:
            raise ValueError("OPTIMIZER_NO_GLYPHS")

        identity = {(r.reference_id, r.style_id, r.browser_version, r.config_hash) for r in fit_records}
        if len(identity) != 1:
            raise ValueError("OPTIMIZER_MIXED_EVIDENCE_IDENTITY")

        by_cp: dict[int, list[ObservationRecord]] = {}
        for r in fit_records:
            by_cp.setdefault(r.code_point, []).append(r)

        # Streaming preparation: each glyph's reference artifacts are built
        # and consumed one glyph at a time so full-resolution reference
        # masks never accumulate for the whole family at MAX schedule scale.
        optimized_glyphs: dict[int, ReconstructedGlyph] = {}
        records: list[GlyphOptimizationRecord] = []
        total_iterations = 0
        for cp in sorted(glyphs):
            cp_records = by_cp.get(cp)
            if not cp_records:
                raise ValueError(f"OPTIMIZER_MISSING_FIT_EVIDENCE_CP_{cp}")
            prepared = self.prepare_glyph_observations(
                cp_records, raster_provider, units_per_em=units_per_em, cache=cache
            )
            optimized, record = self.optimize_glyph(glyphs[cp], prepared)
            optimized_glyphs[cp] = optimized
            records.append(record)
            total_iterations += record.iterations
            del prepared

        trace = OptimizationTrace(
            optimizer_version=OPTIMIZER_VERSION,
            input_fingerprint=self.compute_input_fingerprint(fit_records),
            policy=self.policy,
            records=tuple(records),
            total_iterations=total_iterations,
            converged=True,
            stop_reason="ALL_CONVERGED",
        )
        return optimized_glyphs, trace
