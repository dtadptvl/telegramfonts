"""Data structures for the FAST_ATLAS_ULTRA_V1 atlas pipeline (ADR-0004)."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class GlyphStatus(str, Enum):
    """Terminal per-glyph outcome under the speed-first policy.

    EASY_PASS: fast geometry chain passed on the 1024@0,0 observation; SDF
    and the heavy optimizer were never computed (ADR-0004).
    REFINED_PASS: the single refinement produced a structurally valid
    candidate (possibly recorded low-confidence).
    FAILED_GLYPH: structural/outline/shaping/rendering invalidity after the
    single refinement; never retried at whole-font scope.
    """

    EASY_PASS = "EASY_PASS"
    REFINED_PASS = "REFINED_PASS"
    FAILED_GLYPH = "FAILED_GLYPH"


@dataclass(frozen=True)
class CellMapping:
    """Affine mapping between atlas-cell pixels and UPEM design space.

    The cell renders one glyph at ``size_px`` em size, phase-shifted by
    (phase_x_px, phase_y_px). The pen origin sits at
    (pad_left_px, pad_top_px + ascent_px) in pixel coordinates; +y is DOWN
    in pixels and UP in design space.
    """

    size_px: int
    pad_left_px: int
    pad_top_px: int
    ascent_px: float
    phase_x_px: float = 0.0
    phase_y_px: float = 0.0

    @property
    def upem_per_px(self) -> float:
        return 1000.0 / float(self.size_px)

    def px_to_upem(self, x_px: float, y_px: float) -> tuple[float, float]:
        k = self.upem_per_px
        return (
            (x_px - self.pad_left_px - self.phase_x_px) * k,
            (self.pad_top_px + self.ascent_px + self.phase_y_px - y_px) * k,
        )

    def upem_to_px(self, x_upem: float, y_upem: float) -> tuple[float, float]:
        s = float(self.size_px) / 1000.0
        return (
            x_upem * s + self.pad_left_px + self.phase_x_px,
            self.pad_top_px + self.ascent_px + self.phase_y_px - y_upem * s,
        )


@dataclass(frozen=True)
class PlacedCell:
    """One glyph cell placed inside an atlas page."""

    code_point: int
    page_index: int
    x: int
    y: int
    w: int
    h: int
    size_px: int
    phase_x: float = 0.0
    phase_y: float = 0.0


@dataclass(frozen=True)
class AtlasPage:
    """A bounded atlas page: decoded alpha budget never exceeds byte budget.

    ADR-0004: one page in memory at a time, one readback per page, crop the
    cells in memory and release the page memory immediately.
    """

    index: int
    width: int
    height: int
    cells: tuple[PlacedCell, ...]

    @property
    def decoded_bytes(self) -> int:
        return int(self.width) * int(self.height)

    @property
    def code_points(self) -> tuple[int, ...]:
        return tuple(c.code_point for c in self.cells)


@dataclass(frozen=True)
class GlyphMetricsObservation:
    """One batched measureText observation at one render size (pixel units)."""

    code_point: int
    size_px: float
    width_px: float
    actual_left_px: float
    actual_right_px: float
    actual_ascent_px: float
    actual_descent_px: float
    font_ascent_px: float
    font_descent_px: float


@dataclass(frozen=True)
class RegressedMetrics:
    """Multi-size-regressed glyph metrics normalized to UPEM=1000 (U3)."""

    code_point: int
    advance_width_upem: float
    lsb_upem: float
    rsb_upem: float
    ascent_upem: float
    descent_upem: float
    bbox_upem: tuple[float, float, float, float]
    regression_residual: float

    @property
    def bbox_width_upem(self) -> float:
        return max(0.0, self.bbox_upem[2] - self.bbox_upem[0])

    @property
    def bbox_height_upem(self) -> float:
        return max(0.0, self.bbox_upem[3] - self.bbox_upem[1])


@dataclass(frozen=True)
class GlobalMetricsRegression:
    """Font-level regressed metrics (font bounding box ascent/descent)."""

    font_ascent_upem: float
    font_descent_upem: float
    regression_residual: float


@dataclass
class GeometryEvidence:
    """Cheap FIT-confidence evidence for one glyph (U4)."""

    code_point: int
    status: GlyphStatus
    iou: float = 0.0
    structure_ok: bool = False
    metrics_residual: float = float("inf")
    reasons: tuple[str, ...] = ()
    low_confidence: bool = False
    time_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "code_point": self.code_point,
            "status": self.status.value,
            "iou": round(self.iou, 6),
            "structure_ok": self.structure_ok,
            "metrics_residual": (
                round(self.metrics_residual, 6)
                if self.metrics_residual != float("inf")
                else "inf"
            ),
            "reasons": list(self.reasons),
            "low_confidence": self.low_confidence,
            "time_ms": round(self.time_ms, 3),
        }


@dataclass
class AtlasRunEvidence:
    """Deterministic sanitized evidence record for one atlas run (U12)."""

    policy: str = ""
    policy_hash: str = ""
    mode: str = ""
    glyph_count: int = 0
    pages_total: int = 0
    pages_by_source: dict = field(default_factory=dict)
    http_requests: int = 0
    cdp_calls: int = 0
    browser_readbacks: int = 0
    metrics_js_calls: int = 0
    easy_glyphs: int = 0
    refined_glyphs: int = 0
    failed_glyphs: int = 0
    failed_glyph_ids: list = field(default_factory=list)
    low_confidence_glyph_ids: list = field(default_factory=list)
    stage_timings_ms: dict = field(default_factory=dict)
    total_wall_seconds: float = 0.0
    peak_tracemalloc_mb: float = 0.0
    peak_rss_mb: float = 0.0
    validation: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "policy": self.policy,
            "policy_hash": self.policy_hash,
            "mode": self.mode,
            "glyph_count": self.glyph_count,
            "pages_total": self.pages_total,
            "pages_by_source": dict(self.pages_by_source),
            "http_requests": self.http_requests,
            "cdp_calls": self.cdp_calls,
            "browser_readbacks": self.browser_readbacks,
            "metrics_js_calls": self.metrics_js_calls,
            "easy_glyphs": self.easy_glyphs,
            "refined_glyphs": self.refined_glyphs,
            "failed_glyphs": self.failed_glyphs,
            "failed_glyph_ids": sorted(self.failed_glyph_ids),
            "low_confidence_glyph_ids": sorted(self.low_confidence_glyph_ids),
            "stage_timings_ms": {
                k: round(v, 3) for k, v in sorted(self.stage_timings_ms.items())
            },
            "total_wall_seconds": round(self.total_wall_seconds, 3),
            "peak_tracemalloc_mb": round(self.peak_tracemalloc_mb, 3),
            "peak_rss_mb": round(self.peak_rss_mb, 3),
            "validation": self.validation,
        }
