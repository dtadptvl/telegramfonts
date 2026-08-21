"""MAX Pipeline B: Continuous Signed Distance Field, Topology Hierarchy, and Schneider Cubic Bézier Outline Reconstruction."""
from reconstruction.baseline import SingleObservationBaselineReconstructor
from reconstruction.bezier_fitter import SchneiderFitter
from reconstruction.evaluator import GroundTruthGeometryEvaluator
from reconstruction.models import (
    Contour,
    CubicSegment,
    GeometricScoreResult,
    LineSegment,
    Point2D,
    ReconstructedGlyph,
    ReconstructionConfig,
)
from reconstruction.sdf import compute_observation_sdf, fuse_observation_sdfs
from reconstruction.solver import MaxReconstructionSolver
from reconstruction.topology import build_topology_hierarchy, extract_zero_crossing_contours

__all__ = [
    "Point2D",
    "CubicSegment",
    "LineSegment",
    "Contour",
    "ReconstructedGlyph",
    "ReconstructionConfig",
    "GeometricScoreResult",
    "compute_observation_sdf",
    "fuse_observation_sdfs",
    "extract_zero_crossing_contours",
    "build_topology_hierarchy",
    "SchneiderFitter",
    "SingleObservationBaselineReconstructor",
    "MaxReconstructionSolver",
    "GroundTruthGeometryEvaluator",
]
