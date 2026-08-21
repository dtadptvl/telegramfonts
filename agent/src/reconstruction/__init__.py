"""MAX Pipeline B/C/D: SDF/Topology Reconstruction, Candidate Font Builder, and Held-Out Validation."""
from reconstruction.baseline import SingleObservationBaselineReconstructor
from reconstruction.bezier_fitter import SchneiderFitter
from reconstruction.candidate_builder import (
    CandidateFamilyBuildResult,
    CandidateFontArtifact,
    MaxCandidateFontBuilder,
    draw_reconstructed_glyph_to_pen,
    get_glyph_name_for_codepoint,
)
from reconstruction.candidate_validator import (
    ChromiumValidationResult,
    FormatValidationResult,
    HeldOutValidationReport,
    MaxCandidateHeldOutValidator,
    MetricDifferenceResult,
    RasterComparisonResult,
    ShapingTestResult,
)
from reconstruction.evaluator import GroundTruthGeometryEvaluator
from reconstruction.geometry_optimizer import MaxGeometryOptimizer
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
    "MaxGeometryOptimizer",
    "GroundTruthGeometryEvaluator",
    "CandidateFontArtifact",
    "CandidateFamilyBuildResult",
    "MaxCandidateFontBuilder",
    "draw_reconstructed_glyph_to_pen",
    "get_glyph_name_for_codepoint",
    "FormatValidationResult",
    "ChromiumValidationResult",
    "MetricDifferenceResult",
    "ShapingTestResult",
    "RasterComparisonResult",
    "HeldOutValidationReport",
    "MaxCandidateHeldOutValidator",
]
