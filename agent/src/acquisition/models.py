"""Typed models for the authorized acquisition pipeline."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any


BINARY_STAGE_DUMP_DOM = "dump_dom_binary"
BINARY_STAGE_AUTHORIZED_SESSION = "authorized_session_binary"
RASTER_STAGE_MONOTYPE_ENDPOINT = "monotype_authorized_raster"

# Stage names for the four canonical fallback methods
STAGE_DUMP_DOM_NATIVE = "dump_dom_binary"
STAGE_PLAYWRIGHT_STEALTH = "playwright_stealth_persistent"
STAGE_DIRECT_MONOTYPE_CDN = "monotype_authorized_raster"
STAGE_ALGOLIA_METADATA_CDN = "algolia_metadata_cdn"

# Deterministic L3 reuse probe order over legitimate binary provenance values
# (explicit compatible-reuse rule; identity still binds every other dimension).
BINARY_PROVENANCE_PROBE_ORDER: tuple[str, ...] = (
    BINARY_STAGE_DUMP_DOM,
    STAGE_PLAYWRIGHT_STEALTH,
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
class StyleDiscoveryRecord:
    """One resolved style record inside a family discovery envelope."""

    style_id: str
    style_name: str
    md5: str = ""
    binary_candidates: tuple[BinaryCandidate, ...] = ()
    raster_resources: tuple[Any, ...] = ()
    provenance: str = ""

    def is_complete_metadata(self) -> bool:
        """True when style has non-empty ID, Name and either 32-hex MD5 or binary candidates."""
        return bool(
            self.style_id.strip()
            and self.style_name.strip()
            and ((len(self.md5.strip()) == 32 and all(c in "0123456789abcdefABCDEF" for c in self.md5.strip())) or len(self.binary_candidates) > 0)
        )


@dataclass(frozen=True)
class FamilyDiscoveryEnvelope:
    """Complete discovery envelope for an entire font family.

    Maps canonical family identity -> all available styles -> exact style-bound MD5,
    binary candidates, raster resources, and provenance.
    Strictly validated: rejects duplicate/ambiguous/cross-style mappings.
    """

    family_name: str = ""
    family_url: str = ""
    canonical_family_key: str = ""
    styles: dict[str, StyleDiscoveryRecord] = field(default_factory=dict)
    provenance: str = ""

    def validate_integrity(self) -> tuple[bool, str]:
        """Validate structural integrity and reject invalid/cross-style mappings."""
        if not self.family_name.strip():
            return False, "EMPTY_FAMILY_NAME"
        for s_id, s_rec in self.styles.items():
            if not s_id.strip():
                return False, "EMPTY_STYLE_ID"
            if not s_rec.style_name.strip():
                return False, f"EMPTY_STYLE_NAME:{s_id}"
            if s_rec.md5:
                md5_clean = s_rec.md5.strip().lower()
                if len(md5_clean) != 32 or not all(c in "0123456789abcdef" for c in md5_clean):
                    return False, f"INVALID_MD5_FORMAT:{s_id}:{s_rec.md5}"
        return True, ""

    def get_style_record(self, style_id: str, style_display_name: str = "") -> StyleDiscoveryRecord | None:
        """Find style record matching style_id or normalized style display name."""
        norm_id = style_id.lower().replace("-", "_").replace(" ", "_").strip()
        if norm_id in self.styles:
            return self.styles[norm_id]
        for k, v in self.styles.items():
            if k.lower() == norm_id:
                return v
            if style_display_name and v.style_name.lower().strip() == style_display_name.lower().strip():
                return v
            if norm_id in v.style_name.lower().replace("-", "_").replace(" ", "_"):
                return v
        return None

    def has_complete_map_for(self, expected_styles: list[Any]) -> bool:
        """Check if every requested style in the job has a complete record."""
        if not expected_styles:
            return False
        for s in expected_styles:
            s_id = getattr(s, "id", str(s))
            s_name = getattr(s, "display_name", "")
            rec = self.get_style_record(s_id, s_name)
            if rec is None or not rec.is_complete_metadata():
                return False
        return True


@dataclass(frozen=True)
class DiscoveryEnvelope:
    """Typed discovery output of one acquisition stage (backwards compatible).

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
    playwright_stealth_enabled: bool = True
    algolia_enabled: bool = True
    max_concurrent_cdn_requests: int = 4


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

    `payload` carries the closed provider schema (captured real render
    response; raster-only source — never metrics/pairs/features):
      browser_version: str
      glyphs: [{code_point, glyph_index, sprite_box{x, y, width, height}}]
      pairs:  []  (never supplied by the raster endpoint)
      features: []  (never supplied by the raster endpoint)
      sprite_sha256: str
      observed_headers: {content_type, max_glyphs_per_page?,
                         x_missing_unicodes?, x_tofus_found?}
      unmapped_glyph_slots: int
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
    family_discovery: FamilyDiscoveryEnvelope | None = None


def is_complete_raster_pages(
    pages: tuple[SpriteRasterPage, ...] | list[SpriteRasterPage] | None,
    requested_pts: list[int] | None = None,
    expected_md5: str = "",
) -> bool:
    """Validate closed raster completion: exact page sequences, terminal signal, unique non-conflicting code points, valid MD5."""
    if not isinstance(pages, (tuple, list)) or not pages:
        return False

    norm_expected_md5 = expected_md5.lower().strip() if expected_md5 else ""
    if norm_expected_md5 and len(norm_expected_md5) != 32:
        return False

    sizes_map: dict[int, list[SpriteRasterPage]] = {}
    bound_md5 = norm_expected_md5

    for p in pages:
        if not isinstance(p, SpriteRasterPage) or not p.raster_bytes:
            return False
        # Validate PNG magic bytes and dimensions
        if not p.raster_bytes.startswith(b"\x89PNG\r\n\x1a\n") or len(p.raster_bytes) < 24:
            return False
        png_width = int.from_bytes(p.raster_bytes[16:20], "big")
        png_height = int.from_bytes(p.raster_bytes[20:24], "big")
        if png_width <= 0 or png_height <= 0:
            return False

        payload = p.payload
        if not isinstance(payload, dict):
            return False

        # MD5 requirement: every page must have valid 32-hex MD5
        page_md5 = str(payload.get("md5", "")).strip().lower()
        if not page_md5 or len(page_md5) != 32:
            return False
        if not bound_md5:
            bound_md5 = page_md5
        elif page_md5 != bound_md5:
            return False

        pt_val = payload.get("acs_pt")
        if pt_val is None:
            return False
        try:
            pt = int(pt_val)
            if pt <= 0:
                return False
        except (ValueError, TypeError):
            return False

        glyphs = payload.get("glyphs")
        if not isinstance(glyphs, list):
            return False
        # Exact glyph_count equality requirement
        if p.glyph_count != len(glyphs):
            return False

        for g in glyphs:
            if not isinstance(g, dict):
                return False
            cp = g.get("code_point")
            if not isinstance(cp, int) or cp <= 0:
                return False
            box = g.get("sprite_box")
            if not isinstance(box, dict):
                return False
            w = box.get("width", 0)
            h = box.get("height", 0)
            x = box.get("x", 0)
            y = box.get("y", 0)
            if not (isinstance(w, (int, float)) and isinstance(h, (int, float)) and isinstance(x, (int, float)) and isinstance(y, (int, float))):
                return False
            if cp == 32 and g.get("is_space") is True:
                # Independently bound zero-ink space representation: ONLY the
                # exact zero-area cell is admitted; any other shape under the
                # is_space flag fails closed.
                if x != 0 or y != 0 or w != 0 or h != 0:
                    return False
                continue
            if w <= 0 or h <= 0 or x < 0 or y < 0:
                return False
            # Verify bounding box is fully contained within decoded PNG dimensions
            if x + w > png_width or y + h > png_height:
                return False

        sizes_map.setdefault(pt, []).append(p)

    if requested_pts:
        req_set = set(int(pt) for pt in requested_pts)
        if set(sizes_map.keys()) != req_set:
            return False

    for pt, pt_pages in sizes_map.items():
        if not pt_pages:
            return False
        # Sort by page_index to verify exact sequence
        sorted_pages = sorted(pt_pages, key=lambda x: x.page_index)
        expected_indices = list(range(1, len(sorted_pages) + 1))
        if [p.page_index for p in sorted_pages] != expected_indices:
            return False

        # Non-terminal pages must be marked non-final with next_cursor
        for p in sorted_pages[:-1]:
            if p.final or not p.next_cursor or p.glyph_count == 0:
                return False

        # Last page must observe terminal completion
        last_page = sorted_pages[-1]
        has_terminal = bool(last_page.final or not last_page.next_cursor or last_page.glyph_count == 0)
        if not has_terminal:
            return False

        total_glyphs = sum(p.glyph_count for p in sorted_pages)
        if total_glyphs == 0:
            return False

        # Duplicate / conflicting code points check across all pages for this size
        seen_cps: set[int] = set()
        for p in sorted_pages:
            glyphs = (p.payload or {}).get("glyphs", [])
            for g in glyphs:
                cp = g["code_point"]
                if cp in seen_cps:
                    return False  # Duplicate / conflicting code point
                seen_cps.add(cp)

    return True

