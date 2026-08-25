"""Stage 9D: Deterministic fit-only outline optimizer with bounded convergence.

Optimizes reconstructed glyph outlines strictly against fit-partition raster
evidence. Held-out evidence never enters the objective, candidate selection,
stopping criterion, or retry logic (structural guarantee: the optimizer API
only ever receives fit records).

Guarantees:
- Finite objective (mean raster mismatch over fit observations); non-finite
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
from typing import Callable, Mapping, Sequence

import numpy as np
from PIL import Image

from measurement.calibration import CalibrationTransform
from measurement.models import ObservationRecord
from reconstruction.models import Contour, CubicSegment, LineSegment, Point2D, ReconstructedGlyph

OPTIMIZER_VERSION = "stage9d-fit-only-outline-v1"


class OptimizerNonFiniteObjectiveError(RuntimeError):
    """Raised when the objective evaluates to a non-finite value (fail-closed)."""


class OptimizerNonConvergenceError(RuntimeError):
    """Raised when the bounded budget is exhausted before convergence (fail-closed)."""


@dataclass(frozen=True)
class OptimizerPolicy:
    """Fixed, hashable optimizer constants bound into every convergence trace."""

    max_iterations: int = 120
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
            "records": [
                {
                    "code_point": r.code_point,
                    "initial_objective": repr(r.initial_objective),
                    "final_objective": repr(r.final_objective),
                    "iterations": r.iterations,
                    "stop_reason": r.stop_reason,
                    "accepted_objective_trace": [repr(v) for v in r.accepted_objective_trace],
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
) -> list[Contour]:
    """Apply deterministic translate + uniform scale about a fixed center."""

    def tp(p: Point2D) -> Point2D:
        return Point2D(
            center_x + scale * (p.x - center_x) + dx,
            center_y + scale * (p.y - center_y) + dy,
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
                area_upem=c.area_upem * scale * scale,
            )
        )
    return transformed


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
        if not prepared:
            raise ValueError("OPTIMIZER_NO_FIT_OBSERVATIONS")
        losses: list[float] = []
        for transform, ref_mask, resolution in prepared:
            model_mask = self._rasterize_contours(contours, transform, resolution, self.policy.samples_per_segment)
            intersection = int(np.logical_and(model_mask, ref_mask).sum())
            union = int(np.logical_or(model_mask, ref_mask).sum())
            iou = float(intersection) / max(union, 1)
            losses.append(1.0 - iou)
        objective = float(np.mean(losses))
        if not math.isfinite(objective):
            raise OptimizerNonFiniteObjectiveError("OPTIMIZER_NON_FINITE_OBJECTIVE")
        return objective

    def optimize_glyph(
        self,
        glyph: ReconstructedGlyph,
        prepared: Sequence[tuple[CalibrationTransform, np.ndarray, int]],
    ) -> tuple[ReconstructedGlyph, GlyphOptimizationRecord]:
        """Optimize one glyph's outline; fail-closed on non-convergence."""
        x0, y0, x1, y1 = glyph.bounding_box_upem
        center_x = (x0 + x1) / 2.0
        center_y = (y0 + y1) / 2.0

        dx = 0.0
        dy = 0.0
        scale = 1.0
        current_contours = list(glyph.contours)
        best = self._objective(current_contours, prepared)
        initial = best
        accepted_trace: list[float] = [best]
        iterations = 0

        step_t = self.policy.initial_translation_step_upem
        step_s = self.policy.initial_scale_step
        tol = self.policy.convergence_tol
        stop_reason = "ITERATION_BUDGET_EXHAUSTED"

        while iterations < self.policy.max_iterations:
            improved = False
            for dim, delta_candidates in (
                ("dx", (-step_t, step_t)),
                ("dy", (-step_t, step_t)),
                ("scale", (-step_s, step_s)),
            ):
                for delta in delta_candidates:
                    if iterations >= self.policy.max_iterations:
                        break
                    cand_dx = dx + (delta if dim == "dx" else 0.0)
                    cand_dy = dy + (delta if dim == "dy" else 0.0)
                    cand_scale = scale + (delta if dim == "scale" else 0.0)
                    if cand_scale <= 0.0:
                        iterations += 1
                        continue
                    cand_contours = _transform_contours(
                        glyph.contours, cand_dx, cand_dy, cand_scale, center_x, center_y
                    )
                    iterations += 1
                    obj = self._objective(cand_contours, prepared)
                    if obj < best - tol:
                        dx, dy, scale = cand_dx, cand_dy, cand_scale
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
        record = GlyphOptimizationRecord(
            code_point=glyph.code_point,
            initial_objective=initial,
            final_objective=best,
            iterations=iterations,
            stop_reason=stop_reason,
            accepted_objective_trace=tuple(accepted_trace),
        )
        if not converged:
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

    def optimize(
        self,
        glyphs: Mapping[int, ReconstructedGlyph],
        fit_records: Sequence[ObservationRecord],
        raster_provider: Callable[[ObservationRecord], bytes],
        units_per_em: int = 1000,
    ) -> tuple[dict[int, ReconstructedGlyph], OptimizationTrace]:
        """Optimize all glyphs strictly against fit evidence; fail-closed on any non-convergence."""
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

        prepared_by_cp: dict[int, list[tuple[CalibrationTransform, np.ndarray, int]]] = {}
        for cp in sorted(by_cp):
            prepared: list[tuple[CalibrationTransform, np.ndarray, int]] = []
            for r in sorted(by_cp[cp], key=lambda rec: rec.cache_key):
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
                prepared.append((transform, self._decode_mask(png_bytes, r.resolution), r.resolution))
            prepared_by_cp[cp] = prepared

        optimized_glyphs: dict[int, ReconstructedGlyph] = {}
        records: list[GlyphOptimizationRecord] = []
        total_iterations = 0
        for cp in sorted(glyphs):
            if cp not in prepared_by_cp:
                raise ValueError(f"OPTIMIZER_MISSING_FIT_EVIDENCE_CP_{cp}")
            optimized, record = self.optimize_glyph(glyphs[cp], prepared_by_cp[cp])
            optimized_glyphs[cp] = optimized
            records.append(record)
            total_iterations += record.iterations

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
