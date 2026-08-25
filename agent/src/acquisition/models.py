"""Typed models for the authorized acquisition pipeline."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


BINARY_STAGE_DUMP_DOM = "dump_dom_binary"
BINARY_STAGE_AUTHORIZED_SESSION = "authorized_session_binary"
RASTER_STAGE_MONOTYPE_ENDPOINT = "monotype_authorized_raster"

# Deterministic L3 reuse probe order over legitimate binary provenance values
# (explicit compatible-reuse rule; identity still binds every other dimension).
BINARY_PROVENANCE_PROBE_ORDER: tuple[str, ...] = (
    BINARY_STAGE_DUMP_DOM,
    BINARY_STAGE_AUTHORIZED_SESSION,
)

VALIDATED_BINARY_FORMATS = frozenset({"TTF", "OTF"})

# Authorized font binary containers the converter accepts as input; the
# published output remains strictly TTF/OTF.
CONTAINER_FORMATS = frozenset({"TTF", "OTF", "WOFF", "WOFF2"})


@dataclass(frozen=True)
class BinaryCandidate:
    """One authorized binary resource discovered in a dump envelope."""

    url: str
    format: str  # TTF | OTF | WOFF | WOFF2
    embedded: bool = False  # True for data-URI payloads resolved by the transport


@dataclass(frozen=True)
class DiscoveryEnvelope:
    """Typed discovery output of one acquisition stage.

    Carries canonical family/style identity, authorized binary candidates,
    MD5/raster identity for later stages, and provenance. Sanitized: never
    contains page HTML or secret material.
    """

    family_name: str = ""
    style_name: str = ""
    md5: str = ""
    binary_candidates: tuple[BinaryCandidate, ...] = ()
    raster_identity: str = ""
    provenance: str = ""

    def has_raster_target(self) -> bool:
        return bool(self.family_name.strip() and self.style_name.strip() and self.md5.strip())


@dataclass(frozen=True)
class BinaryAcquisitionPolicy:
    """Deterministic bounds and switches for authorized acquisition."""

    max_sprite_pages: int = 8
    max_binary_bytes: int = 10 * 1024 * 1024
    authorized_session_enabled: bool = True
    monotype_raster_enabled: bool = True


@dataclass(frozen=True)
class AcquiredBinary:
    """One verified authorized font binary."""

    raw_bytes: bytes
    format: str  # "TTF" | "OTF"
    family_name: str
    style_name: str
    provenance: str  # stage identifier that produced the binary
    sha256_hex: str = ""

    def __post_init__(self) -> None:
        if not self.sha256_hex:
            object.__setattr__(self, "sha256_hex", hashlib.sha256(self.raw_bytes).hexdigest())


@dataclass(frozen=True)
class SpriteRasterPage:
    """One bounded page of authorized raster sprite evidence.

    `payload` carries the closed provider schema:
      browser_version: str
      glyphs: [{code_point, resolution, subpixel_x, subpixel_y, png_base64,
                metrics{advance_width_px, lsb_px, rsb_px, ascent_px, descent_px,
                        *_upem fields, sample_count, confidence}}]
      pairs:  [{left_cp, right_cp, left_advance_upem, right_advance_upem,
                pair_advance_upem}]
      features: [{feature_tag, sample_text, enabled_advance_upem,
                  disabled_advance_upem, enabled_raster_signature,
                  disabled_raster_signature}]
    """

    page_index: int
    glyph_count: int
    raster_bytes: bytes
    next_cursor: str = ""
    final: bool = True
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class AcquisitionStageRecord:
    """Immutable record of one acquisition stage attempt (sanitized, no secrets)."""

    stage: str
    attempted: bool
    produced_binary: bool
    produced_raster: bool
    outcome: str  # OK | BINARY_ABSENT | RASTER_ABSENT | INTEGRITY_FAILED | DISABLED | ERROR
    reason_code: str = ""


@dataclass(frozen=True)
class AcquisitionTrace:
    """Deterministic ordered call trace across the provider chain."""

    records: tuple[AcquisitionStageRecord, ...] = field(default_factory=tuple)

    def stage_order(self) -> tuple[str, ...]:
        return tuple(r.stage for r in self.records if r.attempted)

    def to_sanitized_dict(self) -> dict:
        return {
            "records": [
                {
                    "stage": r.stage,
                    "attempted": r.attempted,
                    "produced_binary": r.produced_binary,
                    "produced_raster": r.produced_raster,
                    "outcome": r.outcome,
                    "reason_code": r.reason_code,
                }
                for r in self.records
            ]
        }


@dataclass(frozen=True)
class AcquisitionOutcome:
    """Final acquisition result: binary-first win, raster evidence, or insufficient."""

    kind: str  # "binary" | "raster_authorized" | "insufficient"
    binary: AcquiredBinary | None = None
    raster_pages: tuple[SpriteRasterPage, ...] = ()
    trace: AcquisitionTrace = field(default_factory=AcquisitionTrace)
    terminal_reason_code: str = ""
    discovery: DiscoveryEnvelope | None = None
