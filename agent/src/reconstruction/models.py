"""Data structures, geometry primitives, and configuration models for MAX Pipeline B reconstruction."""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Point2D:
    """Immutable 2D vector / coordinate in font design space (UPEM units)."""

    x: float
    y: float

    def __add__(self, other: Point2D) -> Point2D:
        return Point2D(self.x + other.x, self.y + other.y)

    def __sub__(self, other: Point2D) -> Point2D:
        return Point2D(self.x - other.x, self.y - other.y)

    def __mul__(self, scalar: float) -> Point2D:
        return Point2D(self.x * scalar, self.y * scalar)

    def __rmul__(self, scalar: float) -> Point2D:
        return self.__mul__(scalar)

    def __truediv__(self, scalar: float) -> Point2D:
        if abs(scalar) < 1e-12:
            return Point2D(0.0, 0.0)
        return Point2D(self.x / scalar, self.y / scalar)

    def dot(self, other: Point2D) -> float:
        return self.x * other.x + self.y * other.y

    def cross(self, other: Point2D) -> float:
        return self.x * other.y - self.y * other.x

    def length(self) -> float:
        return math.hypot(self.x, self.y)

    def length_squared(self) -> float:
        return self.x * self.x + self.y * self.y

    def distance_to(self, other: Point2D) -> float:
        return math.hypot(self.x - other.x, self.y - other.y)

    def normalize(self) -> Point2D:
        l = self.length()
        if l < 1e-12:
            return Point2D(0.0, 0.0)
        return Point2D(self.x / l, self.y / l)

    def to_tuple(self) -> tuple[float, float]:
        return (self.x, self.y)


@dataclass
class CubicSegment:
    """Parametric cubic Bézier curve segment: B(t) = (1-t)^3*P0 + 3*(1-t)^2*t*P1 + 3*(1-t)*t^2*P2 + t^3*P3."""

    p0: Point2D
    p1: Point2D
    p2: Point2D
    p3: Point2D

    def evaluate(self, t: float) -> Point2D:
        """Evaluate point on cubic Bézier at parameter t in [0, 1]."""
        u = 1.0 - t
        u2 = u * u
        u3 = u2 * u
        t2 = t * t
        t3 = t2 * t
        x = u3 * self.p0.x + 3.0 * u2 * t * self.p1.x + 3.0 * u * t2 * self.p2.x + t3 * self.p3.x
        y = u3 * self.p0.y + 3.0 * u2 * t * self.p1.y + 3.0 * u * t2 * self.p2.y + t3 * self.p3.y
        return Point2D(x, y)

    def derivative(self, t: float) -> Point2D:
        """First derivative tangent vector B'(t)."""
        u = 1.0 - t
        c0 = 3.0 * (self.p1 - self.p0)
        c1 = 3.0 * (self.p2 - self.p1)
        c2 = 3.0 * (self.p3 - self.p2)
        return (u * u) * c0 + (2.0 * u * t) * c1 + (t * t) * c2

    def second_derivative(self, t: float) -> Point2D:
        """Second derivative acceleration vector B''(t)."""
        u = 1.0 - t
        d0 = 6.0 * (self.p2 - 2.0 * self.p1 + self.p0)
        d1 = 6.0 * (self.p3 - 2.0 * self.p2 + self.p1)
        return u * d0 + t * d1

    def sample_points(self, num_samples: int = 16) -> list[Point2D]:
        """Sample discrete points along the segment for rasterization or distance metrics."""
        return [self.evaluate(i / (num_samples - 1)) for i in range(num_samples)]

    def approximate_length(self, steps: int = 16) -> float:
        """Numerical arc length approximation."""
        pts = self.sample_points(steps)
        return sum(pts[i].distance_to(pts[i + 1]) for i in range(len(pts) - 1))

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "cubic",
            "p0": self.p0.to_tuple(),
            "p1": self.p1.to_tuple(),
            "p2": self.p2.to_tuple(),
            "p3": self.p3.to_tuple(),
        }


@dataclass
class LineSegment:
    """Linear segment between two points."""

    p0: Point2D
    p1: Point2D

    def evaluate(self, t: float) -> Point2D:
        return Point2D(
            self.p0.x + t * (self.p1.x - self.p0.x),
            self.p0.y + t * (self.p1.y - self.p0.y),
        )

    def sample_points(self, num_samples: int = 8) -> list[Point2D]:
        return [self.evaluate(i / (num_samples - 1)) for i in range(num_samples)]

    def approximate_length(self, steps: int = 8) -> float:
        return self.p0.distance_to(self.p1)

    def to_cubic(self) -> CubicSegment:
        """Convert line segment to an exact degree-elevated cubic Bézier."""
        p1 = self.p0 + (self.p1 - self.p0) * (1.0 / 3.0)
        p2 = self.p0 + (self.p1 - self.p0) * (2.0 / 3.0)
        return CubicSegment(self.p0, p1, p2, self.p1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "line",
            "p0": self.p0.to_tuple(),
            "p1": self.p1.to_tuple(),
        }


@dataclass
class Contour:
    """Closed loop consisting of parametric cubic / line segments with topology orientation."""

    segments: list[CubicSegment | LineSegment] = field(default_factory=list)
    is_hole: bool = False
    parent_index: int | None = None
    area_upem: float = 0.0

    @property
    def is_closed(self) -> bool:
        if not self.segments:
            return False
        p_start = self.segments[0].p0
        p_end = self.segments[-1].p1 if isinstance(self.segments[-1], LineSegment) else self.segments[-1].p3
        return p_start.distance_to(p_end) < 1.0

    def sample_points(self, samples_per_segment: int = 12) -> list[Point2D]:
        """Extract continuous point cloud around the entire contour."""
        pts: list[Point2D] = []
        for seg in self.segments:
            seg_pts = seg.sample_points(samples_per_segment)
            if pts:
                pts.extend(seg_pts[1:])
            else:
                pts.extend(seg_pts)
        return pts

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_hole": self.is_hole,
            "parent_index": self.parent_index,
            "area_upem": self.area_upem,
            "segments_count": len(self.segments),
            "segments": [s.to_dict() for s in self.segments],
        }


@dataclass
class ReconstructedGlyph:
    """Complete reconstructed master outline geometry and direct metrics for a single glyph."""

    code_point: int
    character: str
    advance_width_upem: float
    lsb_upem: float
    rsb_upem: float
    ascent_upem: float
    descent_upem: float
    contours: list[Contour] = field(default_factory=list)
    bounding_box_upem: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    reconstruction_time_ms: float = 0.0

    @property
    def total_contours(self) -> int:
        return len(self.contours)

    @property
    def outer_contours_count(self) -> int:
        return sum(1 for c in self.contours if not c.is_hole)

    @property
    def holes_count(self) -> int:
        return sum(1 for c in self.contours if c.is_hole)

    @property
    def total_cubic_segments(self) -> int:
        return sum(len(c.segments) for c in self.contours)

    @property
    def total_control_points(self) -> int:
        return sum(len(c.segments) * 3 for c in self.contours)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code_point": self.code_point,
            "character": self.character,
            "advance_width_upem": self.advance_width_upem,
            "lsb_upem": self.lsb_upem,
            "rsb_upem": self.rsb_upem,
            "ascent_upem": self.ascent_upem,
            "descent_upem": self.descent_upem,
            "outer_contours": self.outer_contours_count,
            "holes": self.holes_count,
            "total_cubic_segments": self.total_cubic_segments,
            "total_control_points": self.total_control_points,
            "bounding_box_upem": self.bounding_box_upem,
            "reconstruction_time_ms": self.reconstruction_time_ms,
            "contours": [c.to_dict() for c in self.contours],
        }


@dataclass
class ReconstructionConfig:
    """Hyperparameters for continuous SDF fusion, Marching Squares extraction, and Bézier fitting."""

    grid_resolution: int = 512
    sdf_pad_upem: float = 80.0
    fitting_tolerance_upem: float = 1.5
    corner_threshold_degrees: float = 120.0
    min_contour_area_upem: float = 15.0
    smooth_iterations: int = 1


@dataclass
class GeometricScoreResult:
    """Comparison metric evaluation between reconstructed outline and isolated ground truth."""

    code_point: int
    character: str
    outline_iou: float
    chamfer_distance_mean_upem: float
    hausdorff_distance_upem: float
    p95_edge_error_upem: float
    truth_outer_count: int
    reconstructed_outer_count: int
    truth_holes_count: int
    reconstructed_holes_count: int
    topology_match: bool
    cubic_segments_count: int
    control_points_count: int
    runtime_ms: float

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)
