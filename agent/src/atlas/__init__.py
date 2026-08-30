"""FAST_ATLAS_ULTRA_V1 speed-first atlas pipeline (ADR-0004).

Internal policy bound under the FAST_30 public profile name. Replaces the
heavy per-glyph CDP measurement/reconstruction schedule with bounded atlas
pages, batched metrics, a fast vectorized geometry chain, one refinement for
failed glyphs, streaming caches, and speed-first validation run once.
"""
from atlas.policy import (  # noqa: F401
    FAST_ATLAS_ULTRA_V1,
    PUBLIC_PROFILE_NAME,
    ORIGINAL_WALL_SECONDS,
    VIETNAMESE_WALL_SECONDS,
    AtlasRuntimeDefaults,
    policy_identity_hash,
    resolve_atlas_policy,
    resolve_job_wall_seconds,
)
