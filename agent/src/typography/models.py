"""Data models for evidence-driven typography and pair kerning inference."""
from __future__ import annotations

import datetime
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class PairKerningObservation:
    """Observable measurement of a single character pair and inferred kerning adjustment."""

    left_cp: int
    right_cp: int
    left_char: str
    right_char: str
    left_advance_upem: float
    right_advance_upem: float
    measured_pair_advance_upem: float
    inferred_kerning_upem: int
    is_kerning_applied: bool
    confidence: float = 1.0

    @property
    def raw_delta_upem(self) -> float:
        return self.measured_pair_advance_upem - (self.left_advance_upem + self.right_advance_upem)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TypographyDataset:
    """Canonical typography dataset containing inferred OpenType GPOS kerning adjustments."""

    family_name: str
    style_name: str
    units_per_em: int = 1000
    kerning_pairs: dict[tuple[int, int], int] = field(default_factory=dict)
    observations: list[PairKerningObservation] = field(default_factory=list)
    total_pairs_probed: int = 0
    active_kerning_pairs_count: int = 0
    inference_method: str = "browser_text_metrics_differential"
    created_at: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())

    def get_kerning(self, left_cp: int, right_cp: int) -> int:
        """Get kerning adjustment for code point pair in UPEM (0 if unkerned)."""
        return self.kerning_pairs.get((left_cp, right_cp), 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "family_name": self.family_name,
            "style_name": self.style_name,
            "units_per_em": self.units_per_em,
            "kerning_pairs": [
                {
                    "left_cp": left_cp,
                    "right_cp": right_cp,
                    "left_char": chr(left_cp) if left_cp > 0 else "?",
                    "right_char": chr(right_cp) if right_cp > 0 else "?",
                    "kerning_upem": val,
                }
                for (left_cp, right_cp), val in sorted(self.kerning_pairs.items())
            ],
            "total_pairs_probed": self.total_pairs_probed,
            "active_kerning_pairs_count": self.active_kerning_pairs_count,
            "inference_method": self.inference_method,
            "created_at": self.created_at,
        }
