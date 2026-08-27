"""Canonical FULL MAX RECONSTRUCTION PROFILE schedules (Issue #3 / #75).

Closed, exact, fail-closed schedule identity for production MAX
reconstruction:

- Metric sizes: 128, 192, 256, 384, 512, 768, 1024, 1536, 2048, 3072, 4096 px.
- Raster sizes: 256, 512, 768, 1024, 1536, 2048, 3072, 4096 px;
  core 512/1024/2048/4096.
- Sealed held-out sizes: 144, 288, 640, 896, 1280, 1792, 2560 px (disjoint
  from fit raster and metric schedules).
- Browser subpixel phases: full 4x4 Cartesian product {0,.25,.5,.75}^2;
  deterministically classified hard glyphs expand to the full 8x8
  {0,.125,...,.875}^2 grid; held-out phases are the exact 4x4 midpoint
  offset grid {0.0625,0.3125,0.5625,0.8125}^2 — observable subpixel phases
  strictly disjoint from BOTH the base fit grid and the hard expansion
  grid, so no per-glyph adaptive fit set can ever contain a held-out
  phase.
- Feature probe tags: kern, liga, clig, dlig, calt, case, frac, tnum,
  pnum, onum, lnum, zero, smcp, c2sc, ss01-ss20.

Missing/extra/duplicate/wrong entries are rejected; nothing here is
approximate, configurable, or caller-attested. Monotype provider evidence
follows the capability-bound disjoint size axis (acquisition.capability)
and never invents phases.
"""
from __future__ import annotations

import hashlib
import itertools
import json
from typing import Sequence

MAX_METRIC_SIZES_PX: tuple[int, ...] = (
    128, 192, 256, 384, 512, 768, 1024, 1536, 2048, 3072, 4096,
)
MAX_RASTER_SIZES_PX: tuple[int, ...] = (
    256, 512, 768, 1024, 1536, 2048, 3072, 4096,
)
MAX_CORE_RASTER_SIZES_PX: tuple[int, ...] = (512, 1024, 2048, 4096)
MAX_HELDOUT_SIZES_PX: tuple[int, ...] = (144, 288, 640, 896, 1280, 1792, 2560)

MAX_BROWSER_PHASE_GRID: tuple[float, ...] = (0.0, 0.25, 0.5, 0.75)
MAX_HARD_PHASE_GRID: tuple[float, ...] = (
    0.0, 0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875,
)
# Held-out phase grid: midpoints between hard-grid lines. Observable
# subpixel phases that are provably outside BOTH the base 4x4 fit grid and
# the full 8x8 hard expansion grid — per-glyph fit and held-out evidence
# stay disjoint even for hard glyphs.
MAX_HELDOUT_PHASE_GRID: tuple[float, ...] = (0.0625, 0.3125, 0.5625, 0.8125)
MAX_BROWSER_PHASES_4X4: tuple[tuple[float, float], ...] = tuple(
    (float(x), float(y))
    for x, y in itertools.product(MAX_BROWSER_PHASE_GRID, MAX_BROWSER_PHASE_GRID)
)
MAX_HARD_PHASES_8X8: tuple[tuple[float, float], ...] = tuple(
    (float(x), float(y))
    for x, y in itertools.product(MAX_HARD_PHASE_GRID, MAX_HARD_PHASE_GRID)
)
MAX_HELDOUT_PHASES: tuple[tuple[float, float], ...] = tuple(
    (float(x), float(y))
    for x, y in itertools.product(MAX_HELDOUT_PHASE_GRID, MAX_HELDOUT_PHASE_GRID)
)

MAX_FEATURE_PROBE_TAGS: tuple[str, ...] = (
    "kern", "liga", "clig", "dlig", "calt", "case", "frac",
    "tnum", "pnum", "onum", "lnum", "zero", "smcp", "c2sc",
) + tuple(f"ss{i:02d}" for i in range(1, 21))

# Deterministic causal ON/OFF probe samples per feature tag.
MAX_FEATURE_PROBE_SAMPLES: dict[str, str] = {
    "kern": "AV",
    "liga": "ffi",
    "clig": "fi",
    "dlig": "ct",
    "calt": "->",
    "case": "(A)",
    "frac": "1/2",
    "tnum": "1234567890",
    "pnum": "1234567890",
    "onum": "1234567890",
    "lnum": "1234567890",
    "zero": "0O",
    "smcp": "Abc",
    "c2sc": "ABC",
}
# Style-set probes reuse a neutral alphabetical sample (deterministic).
for _i in range(1, 21):
    MAX_FEATURE_PROBE_SAMPLES.setdefault(f"ss{_i:02d}", "Ag")

MAX_FEATURE_PROBES: tuple[tuple[str, str], ...] = tuple(
    (tag, MAX_FEATURE_PROBE_SAMPLES[tag]) for tag in MAX_FEATURE_PROBE_TAGS
)

MAX_CONFIG_VERSION = "2.0.0-max"


def _check_exact_tuple(
    observed: Sequence, canonical: tuple, label: str
) -> None:
    obs = tuple(observed)
    if len(obs) != len(set(obs)):
        raise ValueError(f"MAX_SCHEDULE_DUPLICATE:{label}")
    if len(obs) != len(canonical):
        missing = sorted(set(canonical) - set(obs))
        extra = sorted(set(obs) - set(canonical))
        raise ValueError(
            f"MAX_SCHEDULE_MISMATCH:{label}:missing={missing}:extra={extra}"
        )
    if obs != canonical:
        raise ValueError(f"MAX_SCHEDULE_MISMATCH:{label}")


def validate_max_schedule(
    metric_sizes: Sequence[int],
    raster_sizes: Sequence[int],
    core_sizes: Sequence[int],
    heldout_sizes: Sequence[int],
    fit_phases: Sequence[tuple[float, float]],
    hard_phases: Sequence[tuple[float, float]],
    heldout_phases: Sequence[tuple[float, float]],
    feature_probe_tags: Sequence[str],
) -> None:
    """Reject missing/extra/duplicate/wrong MAX schedule entries (fail closed).

    Also enforces the closed invariants: core subset of raster sizes,
    held-out sizes disjoint from fit raster and metric sizes, held-out
    phases disjoint from the actual maximum per-glyph fit set (base AND
    hard expansion grids), and hard grid strictly expanding the fit grid.
    """
    # Disjointness/subset invariants are checked FIRST so injected overlap is
    # reported as overlap (not masked by the exact-tuple closure check).
    if not set(core_sizes).issubset(set(raster_sizes)):
        raise ValueError("MAX_SCHEDULE_CORE_NOT_SUBSET")
    if set(heldout_sizes) & (set(raster_sizes) | set(metric_sizes)):
        raise ValueError("MAX_SCHEDULE_HELDOUT_SIZE_OVERLAP")
    fit_set = set(tuple(p) for p in fit_phases)
    heldout_phase_set = set(tuple(p) for p in heldout_phases)
    hard_set = set(tuple(p) for p in hard_phases)
    # Held-out phases must be disjoint from the ACTUAL maximum per-glyph fit
    # set (the hard expansion grid, which contains the base fit grid) —
    # never merely disjoint from the base grid.
    if hard_set & heldout_phase_set:
        raise ValueError("MAX_SCHEDULE_HELDOUT_PHASE_OVERLAP")
    if fit_set & heldout_phase_set:
        raise ValueError("MAX_SCHEDULE_HELDOUT_PHASE_OVERLAP")
    if not fit_set.issubset(hard_set):
        raise ValueError("MAX_SCHEDULE_HARD_GRID_NOT_EXPANDING")

    _check_exact_tuple(metric_sizes, MAX_METRIC_SIZES_PX, "METRIC_SIZES")
    _check_exact_tuple(raster_sizes, MAX_RASTER_SIZES_PX, "RASTER_SIZES")
    _check_exact_tuple(core_sizes, MAX_CORE_RASTER_SIZES_PX, "CORE_RASTER_SIZES")
    _check_exact_tuple(heldout_sizes, MAX_HELDOUT_SIZES_PX, "HELDOUT_SIZES")
    _check_exact_tuple(tuple(tuple(p) for p in fit_phases), MAX_BROWSER_PHASES_4X4, "FIT_PHASES")
    _check_exact_tuple(tuple(tuple(p) for p in hard_phases), MAX_HARD_PHASES_8X8, "HARD_PHASES")
    _check_exact_tuple(tuple(tuple(p) for p in heldout_phases), MAX_HELDOUT_PHASES, "HELDOUT_PHASES")
    _check_exact_tuple(feature_probe_tags, MAX_FEATURE_PROBE_TAGS, "FEATURE_PROBE_TAGS")


def validate_observation_config_max(config) -> None:
    """Validate one ObservationConfig against the canonical MAX profile.

    Structural production validation: exact closed schedules plus a closed
    metric anchor — no undeclared metric-size observation is possible.
    """
    validate_max_schedule(
        metric_sizes=tuple(int(s) for s in config.metric_sizes_px),
        raster_sizes=tuple(int(r) for r in config.resolutions),
        core_sizes=MAX_CORE_RASTER_SIZES_PX,
        heldout_sizes=tuple(int(s) for s in getattr(config, "held_out_sizes_px", ())),
        fit_phases=config.base_subpixel_phases,
        hard_phases=config.expanded_subpixel_phases,
        heldout_phases=config.held_out_subpixel_phases,
        feature_probe_tags=tuple(tag for tag, _sample in config.feature_probes),
    )
    anchor = float(getattr(config, "font_size_px", 0.0))
    if anchor not in {float(s) for s in config.metric_sizes_px}:
        raise ValueError(
            f"MAX_SCHEDULE_METRIC_ANCHOR_UNDECLARED: font_size_px {anchor} "
            f"is not inside the exact metric schedule"
        )
    if str(getattr(config, "config_version", "")) != MAX_CONFIG_VERSION:
        raise ValueError("MAX_SCHEDULE_CONFIG_VERSION_MISMATCH")


def max_schedule_identity_hash() -> str:
    """Deterministic hash of the closed MAX schedule identity."""
    payload = {
        "metric_sizes": list(MAX_METRIC_SIZES_PX),
        "raster_sizes": list(MAX_RASTER_SIZES_PX),
        "core_raster_sizes": list(MAX_CORE_RASTER_SIZES_PX),
        "heldout_sizes": list(MAX_HELDOUT_SIZES_PX),
        "fit_phases": [list(p) for p in MAX_BROWSER_PHASES_4X4],
        "hard_phases": [list(p) for p in MAX_HARD_PHASES_8X8],
        "heldout_phases": [list(p) for p in MAX_HELDOUT_PHASES],
        "heldout_phase_grid": list(MAX_HELDOUT_PHASE_GRID),
        "feature_probe_tags": list(MAX_FEATURE_PROBE_TAGS),
        "config_version": MAX_CONFIG_VERSION,
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
