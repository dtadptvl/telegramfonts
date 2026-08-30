"""FAST_ATLAS_ULTRA_V1 policy identity, walls, and runtime defaults (ADR-0004).

FAST_ATLAS_ULTRA_V1 is the INTERNAL speed-first reconstruction policy bound
under the FAST_30 public profile name (ADR-0001 amendment, generation 10 /
E-00023). FAST_30 remains the SOLE public profile name; BALANCED_MAX and
FULL_MAX remain retired and every selection route fails closed with
PROFILE_RETIRED. No fallback/escalation of any trigger type exists.

Walls (ADR-0004, configuration-driven):
- ORIGINAL hard wall 480 s (8 min)
- VIETNAMESE hard wall 720 s (12 min)
Wall expiry -> FAST30_FAILED, no retry, no heavier profile. The job-level
monotonic wall machinery of T-FAST30-A23-FIX (claim -> ACK) is preserved
and re-targeted to these walls; watchdog/checkpoint/zombie hardening is
untouched.

Runtime defaults documented for the i3-7100 / 8 GB Debian worker; every
value is configuration-driven (no A23-specific hardcoding).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

# Internal policy identity (ADR-0004). Public profile name stays FAST_30.
FAST_ATLAS_ULTRA_V1 = "FAST_ATLAS_ULTRA_V1"
PUBLIC_PROFILE_NAME = "FAST_30"

# ADR-0004 hard walls (seconds). Wall expiry is terminal FAST30_FAILED with
# no retry and no heavier profile.
ORIGINAL_WALL_SECONDS = 480
VIETNAMESE_WALL_SECONDS = 720

# Retired profiles stay retired (ADR-0001): selection fails closed.
RETIRED_PROFILE_NAMES = frozenset({"BALANCED_MAX", "FULL_MAX"})

# ADR-0004 fast raster pass: size 1024, phase x=0/y=0. Never pre-collect
# 512/2048/4096 or extra phases in the fast pass; the single refinement for
# failed glyphs adds exactly 1024@x=0.5,y=0 and 2048@x=0,y=0.
FAST_RASTER_SIZE_PX = 1024
FAST_RASTER_PHASE = (0.0, 0.0)
REFINEMENT_OBSERVATIONS: tuple[tuple[int, float, float], ...] = (
    (1024, 0.5, 0.0),
    (2048, 0.0, 0.0),
)

# Metrics batch sizes (U3): batched measureText at these render sizes,
# regressed to UPEM=1000.
METRICS_SIZES_PX: tuple[int, ...] = (512, 1024, 2048)

POLICY_SCHEMA = {
    "policy": FAST_ATLAS_ULTRA_V1,
    "public_profile": PUBLIC_PROFILE_NAME,
    "walls_seconds": {
        "ORIGINAL": ORIGINAL_WALL_SECONDS,
        "VIETNAMESE": VIETNAMESE_WALL_SECONDS,
    },
    "fast_raster": {
        "size_px": FAST_RASTER_SIZE_PX,
        "phase": list(FAST_RASTER_PHASE),
    },
    "refinement_observations": [list(o) for o in REFINEMENT_OBSERVATIONS],
    "metrics_sizes_px": list(METRICS_SIZES_PX),
    "targets": {
        "ordinary_original_minutes": [2, 5],
        "large_original_minutes": [5, 10],
        "reuse_seconds": 60,
    },
}


def policy_identity_hash() -> str:
    """Deterministic identity hash of the FAST_ATLAS_ULTRA_V1 policy."""
    serialized = json.dumps(POLICY_SCHEMA, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AtlasRuntimeDefaults:
    """Configuration-driven runtime defaults (ADR-0004, i3-7100 / 8 GB Debian).

    browser_sessions=1: one persistent Chromium session, started lazily ONLY
    when metadata/measureText/browser-atlas fallback is actually needed.
    http_concurrency=8: bounded concurrent HTTP raster acquisitions.
    glyph_workers=2: bounded glyph geometry workers.
    atlas_pages_in_memory=1: one atlas page in memory at a time.
    atlas_target_mb=96 / atlas_max_mb=128: page budget target / hard max.
    checkpoint_batch=32: checkpoint every 32 frozen glyphs (or per page).
    """

    browser_sessions: int = 1
    http_concurrency: int = 8
    glyph_workers: int = 2
    atlas_pages_in_memory: int = 1
    atlas_target_mb: int = 96
    atlas_max_mb: int = 128
    checkpoint_batch: int = 32

    def validate(self) -> "AtlasRuntimeDefaults":
        if not (1 <= self.browser_sessions <= 4):
            raise ValueError("ATLAS_BROWSER_SESSIONS_OUT_OF_RANGE")
        if not (1 <= self.http_concurrency <= 32):
            raise ValueError("ATLAS_HTTP_CONCURRENCY_OUT_OF_RANGE")
        if not (1 <= self.glyph_workers <= 8):
            raise ValueError("ATLAS_GLYPH_WORKERS_OUT_OF_RANGE")
        if not (1 <= self.atlas_pages_in_memory <= 2):
            # ADR-0004: one page in memory at a time (bounded lookahead of 2
            # permits streaming page N+1 while N decodes).
            raise ValueError("ATLAS_PAGES_IN_MEMORY_OUT_OF_RANGE")
        if not (1 <= self.atlas_target_mb <= self.atlas_max_mb <= 512):
            raise ValueError("ATLAS_PAGE_BUDGET_INVALID")
        if not (1 <= self.checkpoint_batch <= 512):
            raise ValueError("ATLAS_CHECKPOINT_BATCH_OUT_OF_RANGE")
        return self

    def to_dict(self) -> dict:
        return asdict(self)


def resolve_job_wall_seconds(mode: str, original_wall: int, vietnamese_wall: int) -> int:
    """Mode-aware hard wall selection (U11).

    ORIGINAL -> 480 s; VIETNAMESE -> 720 s. Unknown/absent modes fail closed
    (the runner's fail-closed mode binding raises MISSING_MODE/
    UNSUPPORTED_MODE before any wall is born).
    """
    m = str(mode or "").strip().upper()
    if m == "ORIGINAL":
        return int(original_wall)
    if m == "VIETNAMESE":
        return int(vietnamese_wall)
    raise ValueError("UNSUPPORTED_MODE")


def resolve_atlas_policy(requested: str | None = None) -> str:
    """Fail-closed policy binding shim (U1).

    The public profile name FAST_30 (or an absent request) binds internally
    to FAST_ATLAS_ULTRA_V1. Retired profiles fail closed with
    PROFILE_RETIRED; unknown names with PROFILE_UNKNOWN. No fallback or
    escalation of any kind.
    """
    if requested is None:
        return FAST_ATLAS_ULTRA_V1
    name = str(requested).strip().upper()
    if name == PUBLIC_PROFILE_NAME or name == FAST_ATLAS_ULTRA_V1:
        return FAST_ATLAS_ULTRA_V1
    if name in RETIRED_PROFILE_NAMES:
        raise ValueError(f"PROFILE_RETIRED: {name}")
    raise ValueError(f"PROFILE_UNKNOWN: {name}")
