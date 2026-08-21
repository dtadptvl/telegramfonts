"""Typography, pair kerning inference, and OpenType GPOS table generation package."""
from __future__ import annotations

from typography.gpos_builder import attach_gpos_to_font, generate_kern_feature_syntax
from typography.kerning_inferencer import EvidenceKerningInferencer
from typography.models import PairKerningObservation, TypographyDataset

__all__ = [
    "PairKerningObservation",
    "TypographyDataset",
    "EvidenceKerningInferencer",
    "generate_kern_feature_syntax",
    "attach_gpos_to_font",
]
