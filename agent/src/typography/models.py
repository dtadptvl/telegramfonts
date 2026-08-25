"""Data models for evidence-driven typography and pair kerning inference."""
from __future__ import annotations

import datetime
from dataclasses import asdict, dataclass, field
from typing import Any


# Bounded evidence-driven candidate fit pairs (prioritized typographical pairs, strictly non-N^2)
BOUNDED_FIT_PAIRS: list[tuple[int, int]] = [
    (ord("A"), ord("O")),    # (65, 79): -40 UPEM
    (ord("B"), ord("O")),    # (66, 79): -10 UPEM
    (ord("A"), ord("%")),    # (65, 37): -40 UPEM
    (ord("A"), ord("g")),    # (65, 103): -20 UPEM
    (ord("A"), ord("ơ")),    # (65, 417): -20 UPEM
    (ord("Đ"), ord("A")),    # (272, 65): -40 UPEM
    (ord("g"), ord("ắ")),    # (103, 7855): -20 UPEM
    # Control unadjusted pairs (evidence of 0 delta)
    (ord("A"), ord("A")),    # (65, 65): 0 UPEM
    (ord("O"), ord("O")),    # (79, 79): 0 UPEM
    (ord("8"), ord("A")),    # (56, 65): 0 UPEM
    (ord("g"), ord("m")),    # (103, 109): 0 UPEM
    (ord("Đ"), ord("O")),    # (272, 79): 0 UPEM
]

# Distinct held-out in-cmap pairs for evaluation only (never in the fit set)
SEPARATE_HELD_OUT_IN_CMAP_PAIRS: list[tuple[str, int, int]] = [
    ("OA", ord("O"), ord("A")),    # (79, 65) - mirror pair
    ("OĐ", ord("O"), ord("Đ")),    # (79, 272)
    ("ơA", ord("ơ"), ord("A")),    # (417, 65)
    ("mơ", ord("m"), ord("ơ")),    # (109, 417)
]


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
    provenance: str = "untrusted"
    reference_id: str = "default_reference"
    style_id: str = "regular"
    browser_version: str = "chromium"
    config_hash: str = ""

    def __post_init__(self) -> None:
        if not self.reference_id or not self.style_id or not self.browser_version:
            raise ValueError(
                "PAIR_IDENTITY_REQUIRED: reference_id, style_id, and browser_version must be non-empty strings"
            )
        if not self.config_hash:
            from measurement.models import ObservationConfig
            object.__setattr__(self, "config_hash", ObservationConfig().compute_hash())
        elif len(self.config_hash) != 64 or not all(c in "0123456789abcdefABCDEF" for c in self.config_hash):
            raise ValueError(f"PairKerningObservation config_hash must be a 64-char hex digest, got: '{self.config_hash}'")

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
    provenance: str = ""
    fit_rows_count: int = 0
    fit_rows_sha256: str = ""
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
            "provenance": self.provenance,
            "fit_rows_count": self.fit_rows_count,
            "fit_rows_sha256": self.fit_rows_sha256,
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
            "observations": [obs.to_dict() for obs in self.observations],
            "inference_method": self.inference_method,
            "created_at": self.created_at,
        }
