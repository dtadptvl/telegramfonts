"""Fidelity evaluation, quality gates, and canonical verification models (Stage 9A / 9B)."""
from fidelity.evaluator import FidelityEvaluator
from fidelity.models import (
    BoundChromiumEvidence,
    BoundFontToolsEvidence,
    BoundFreeTypeEvidence,
    BoundHarfBuzzEvidence,
    ConsumerEvidenceBundle,
    ConsumerGateResult,
    CoverageGateResult,
    FidelityReport,
    FidelityThresholds,
    GeometryRasterGateResult,
    MetricsGateResult,
    TopologyGateResult,
    TypographyGateResult,
)
from fidelity.producers import (
    CandidateArtifact,
    ChromiumEvidenceProducer,
    FontToolsEvidenceProducer,
    FreeTypeEvidenceProducer,
    HarfBuzzEvidenceProducer,
    ProductionConsumerEvidenceProducer,
)

__all__ = [
    "BoundChromiumEvidence",
    "BoundFontToolsEvidence",
    "BoundFreeTypeEvidence",
    "BoundHarfBuzzEvidence",
    "CandidateArtifact",
    "ChromiumEvidenceProducer",
    "ConsumerEvidenceBundle",
    "ConsumerGateResult",
    "CoverageGateResult",
    "FidelityEvaluator",
    "FidelityReport",
    "FidelityThresholds",
    "FontToolsEvidenceProducer",
    "FreeTypeEvidenceProducer",
    "GeometryRasterGateResult",
    "HarfBuzzEvidenceProducer",
    "MetricsGateResult",
    "ProductionConsumerEvidenceProducer",
    "TopologyGateResult",
    "TypographyGateResult",
]
