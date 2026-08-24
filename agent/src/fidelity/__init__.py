"""Fidelity evaluation, quality gates, and canonical verification models (Stage 9A)."""
from fidelity.evaluator import FidelityEvaluator
from fidelity.models import (
    CoverageGateResult,
    FidelityReport,
    FidelityThresholds,
    GeometryRasterGateResult,
    MetricsGateResult,
    TopologyGateResult,
    TypographyGateResult,
)

__all__ = [
    "CoverageGateResult",
    "FidelityEvaluator",
    "FidelityReport",
    "FidelityThresholds",
    "GeometryRasterGateResult",
    "MetricsGateResult",
    "TopologyGateResult",
    "TypographyGateResult",
]
