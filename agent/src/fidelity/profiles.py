"""Stage 16 (Issue #86 / #6 D18): versioned reconstruction-profile policy layer.

BALANCED_MAX is the primary reconstruction profile for testing and
production; FULL_MAX remains the canonical quality reference and the
mandatory fail-closed fallback. Both are VERSIONED POLICIES over the
shared pipeline interfaces (snapshot -> partition -> reconstruction ->
optimization -> calibration -> sealed FontModel -> candidate build ->
four consumers -> held-out gating -> attestation). No duplicated
pipelines: the profiles only select the fit/search schedule and the
confidence policy; every downstream gate stays the canonical one.

SCHEDULE INVARIANT DELTA (D18): only BALANCED_MAX's separate versioned
fit/search schedule may shrink (adaptive resolutions, phase grids,
candidate counts, iteration budgets, deterministic early-stop). FULL_MAX
fit/search schedules remain exactly canonical. Held-out schedules,
thresholds, four-consumer validation, evidence independence,
attestation, deterministic identity, and publication requirements are
unchanged for BOTH profiles. Reduced fitting work is permitted only
when the artifact passes the complete unchanged final gates.

SEARCH LADDER (BALANCED_MAX, deterministic, versioned):
- CORE FIT on versioned core fit resolutions (512/1024/2048 on the
  canonical MAX raster schedule; deterministic clamp for smaller
  configs) with a reduced deterministic phase grid for EASY glyphs.
- HARD classification is deterministic and observable only (never AI
  self-assessment): Vietnamese combining marks and multi-component
  synthesized composites default HARD; metric boundary uncertainty,
  coarse residual/coverage, and budget-exhausted coarse convergence
  promote to HARD.
- HARD expansion ladder: core x base 4x4 grid -> core x expanded 8x8
  grid -> complete required resolution/phase set. Identity-bound
  checkpoint after every completed glyph and search tier.
- FULL-RESOLUTION RERANK: coarse tiers only propose; the top bounded
  candidate pool is scored on the authoritative full fit evidence; a
  top-1 acceptance without pool competition requires the versioned
  confidence-margin rule (absolute full-resolution objective bound).
  A coarse-only candidate can never be published: every published glyph
  carries a full-resolution objective and complete loss vector.
- Early-stop never consumes held-out or consumer evidence: tier
  expansion stops only when the versioned absolute AND relative
  fit-objective improvement stay below thresholds for the required
  consecutive rounds; the best valid candidate is always preserved.

CONFIDENCE: deterministic, evidence-derived, reproducible, versioned.
Derived exclusively from FIT evidence (full-resolution objective,
coverage/edge/SDF components, convergence stability, candidate margin).
Thresholds are calibrated against a fixed FULL_MAX reference corpus and
frozen before production execution (calibration identity bound into the
policy hash). Missing/incomplete/non-finite confidence inputs = LOW.
Held-out/consumer evidence may trigger escalation only and never feeds
fitting, ranking, early-stop, or confidence.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

PROFILE_FULL_MAX = "FULL_MAX"
PROFILE_BALANCED_MAX = "BALANCED_MAX"

# Versioned core fit resolutions of the BALANCED_MAX search ladder on the
# canonical MAX raster schedule (FULL MAX core is 512/1024/2048/4096; the
# 4096 tier is reserved for the full-resolution rerank / HARD expansion).
BALANCED_CORE_FIT_RESOLUTIONS_PX: tuple[int, ...] = (512, 1024, 2048)

# Reduced deterministic phase grid for EASY glyphs: the {0, 0.5}^2 subset
# of the canonical 4x4 base grid. Glyphs whose config carries no such
# phase fall back deterministically to the smallest declared base phase.
BALANCED_EASY_PHASE_AXES: tuple[float, ...] = (0.0, 0.5)

# Vietnamese combining marks default HARD (D18). The Vietnamese extension
# mark set is the production authority; generic Unicode combining class
# extends the rule deterministically to any combining mark evidence.
from compute.vietnamese import MARK_CODEPOINT_SET as _VI_MARK_CODEPOINT_SET  # noqa: E402

# Frozen FULL_MAX reference-corpus calibration identity. The thresholds
# below were calibrated against the canonical local E2E reference corpus
# (test_issue75_fullmax_e2e ORIGINAL + VIETNAMESE fixture families) under
# FULL_MAX formation and frozen before production execution. The reference
# envelope is the worst per-component loss over INK glyphs only: zero-ink
# glyphs (canonical 0/0 coverage degeneracy) carry no fittable geometry
# and are handled deterministically by the search ladder. The accept
# scalar is derived from the calibration safety factors (versioned
# headroom under the reference-worst score), so the frozen gate accepts
# FULL_MAX-reference-quality candidates by construction. Any change of
# corpus identity, thresholds, or profile version is a new policy version.
CALIBRATION_CORPUS_IDENTITY = (
    "e2e-original(65,79)+e2e-vi(31-base)+fullmax-canonical-formation"
)
CALIBRATION_POLICY_VERSION = "1.0.0"


@dataclass(frozen=True)
class ConfidenceThresholds:
    """Frozen, versioned confidence-gate thresholds (fit evidence only)."""

    max_full_objective: float = 0.501195
    max_coverage_loss: float = 0.064999
    max_edge_loss: float = 0.95
    max_sdf_loss: float = 0.006122
    max_budget_fraction: float = 0.25
    accept_scalar: float = 0.25
    top1_full_objective: float = 0.426016

    def to_dict(self) -> dict[str, float]:
        return {
            "max_full_objective": self.max_full_objective,
            "max_coverage_loss": self.max_coverage_loss,
            "max_edge_loss": self.max_edge_loss,
            "max_sdf_loss": self.max_sdf_loss,
            "max_budget_fraction": self.max_budget_fraction,
            "accept_scalar": self.accept_scalar,
            "top1_full_objective": self.top1_full_objective,
        }


@dataclass(frozen=True)
class ReconstructionProfile:
    """Versioned reconstruction fit/search policy over shared interfaces.

    FULL_MAX: every schedule field selects the complete canonical
    fit evidence with the canonical optimizer budget; the confidence
    gate is inactive (FULL_MAX is the canonical reference itself).
    BALANCED_MAX: reduced versioned fit/search schedule + deterministic
    confidence gate. All downstream gates are identical for both.
    """

    name: str
    profile_version: str
    # Fit/search schedule (only BALANCED_MAX may shrink these).
    core_fit_resolutions: tuple[int, ...]  # empty == every config resolution
    easy_phase_axes: tuple[float, ...]  # empty == every declared base phase
    coarse_max_iterations: int
    tier_max_iterations: int
    final_max_iterations: int
    early_stop_abs_tol: float
    early_stop_rel_tol: float
    early_stop_rounds: int
    rerank_top_k: int
    workers_default: int
    confidence_thresholds: ConfidenceThresholds
    calibration_corpus_identity: str
    calibration_policy_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "profile_version": self.profile_version,
            "core_fit_resolutions": list(self.core_fit_resolutions),
            "easy_phase_axes": list(self.easy_phase_axes),
            "coarse_max_iterations": self.coarse_max_iterations,
            "tier_max_iterations": self.tier_max_iterations,
            "final_max_iterations": self.final_max_iterations,
            "early_stop_abs_tol": self.early_stop_abs_tol,
            "early_stop_rel_tol": self.early_stop_rel_tol,
            "early_stop_rounds": self.early_stop_rounds,
            "rerank_top_k": self.rerank_top_k,
            "workers_default": self.workers_default,
            "confidence_thresholds": self.confidence_thresholds.to_dict(),
            "calibration_corpus_identity": self.calibration_corpus_identity,
            "calibration_policy_version": self.calibration_policy_version,
        }

    def policy_hash(self) -> str:
        serialized = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # Schedule selection (deterministic, config-clamped)
    # ------------------------------------------------------------------

    def select_core_resolutions(self, config_resolutions: Sequence[int]) -> tuple[int, ...]:
        """Core fit resolutions for the search ladder, clamped to config.

        FULL_MAX returns every config resolution (canonical). BALANCED_MAX
        returns the versioned core set intersected with the config; configs
        that carry no versioned core member clamp deterministically to the
        largest config resolution at/below the versioned maximum core size,
        else the smallest config resolution. Never empty for a valid config.
        """
        resolutions = tuple(int(r) for r in config_resolutions)
        if not resolutions:
            raise ValueError("PROFILE_NO_CONFIG_RESOLUTIONS")
        if self.name == PROFILE_FULL_MAX or not self.core_fit_resolutions:
            return resolutions
        available = set(resolutions)
        core = tuple(r for r in self.core_fit_resolutions if r in available)
        if core:
            return core
        max_core = max(self.core_fit_resolutions)
        below = [r for r in resolutions if r <= max_core]
        if below:
            return (max(below),)
        return (min(resolutions),)

    def select_easy_phases(
        self, base_phases: Sequence[tuple[float, float]]
    ) -> tuple[tuple[float, float], ...]:
        """Reduced EASY phase grid: versioned axis subset of the base grid.

        FULL_MAX (empty axes) returns every declared base phase. A config
        whose base grid carries no subset member falls back to the smallest
        declared base phase (deterministic, never empty).
        """
        phases = tuple((round(float(x), 4), round(float(y), 4)) for x, y in base_phases)
        if not phases:
            raise ValueError("PROFILE_NO_BASE_PHASES")
        if self.name == PROFILE_FULL_MAX or not self.easy_phase_axes:
            return phases
        axes = {round(float(a), 4) for a in self.easy_phase_axes}
        subset = tuple(p for p in phases if p[0] in axes and p[1] in axes)
        if subset:
            return tuple(sorted(set(subset)))
        return (min(phases),)


FULL_MAX_PROFILE = ReconstructionProfile(
    name=PROFILE_FULL_MAX,
    profile_version="1.0.0",
    core_fit_resolutions=(),
    easy_phase_axes=(),
    coarse_max_iterations=240,
    tier_max_iterations=240,
    final_max_iterations=240,
    early_stop_abs_tol=0.0,
    early_stop_rel_tol=0.0,
    early_stop_rounds=0,
    rerank_top_k=1,
    workers_default=1,
    confidence_thresholds=ConfidenceThresholds(),
    calibration_corpus_identity=CALIBRATION_CORPUS_IDENTITY,
    calibration_policy_version=CALIBRATION_POLICY_VERSION,
)

BALANCED_MAX_PROFILE = ReconstructionProfile(
    name=PROFILE_BALANCED_MAX,
    profile_version="1.0.0",
    core_fit_resolutions=BALANCED_CORE_FIT_RESOLUTIONS_PX,
    easy_phase_axes=BALANCED_EASY_PHASE_AXES,
    coarse_max_iterations=64,
    tier_max_iterations=64,
    # Authoritative FULL_SET tier budget equals the canonical FULL_MAX
    # per-glyph budget: the complete-set candidate must be able to
    # converge exactly as the canonical schedule does.
    final_max_iterations=240,
    early_stop_abs_tol=1e-5,
    early_stop_rel_tol=1e-3,
    early_stop_rounds=2,
    rerank_top_k=3,
    workers_default=4,
    confidence_thresholds=ConfidenceThresholds(),
    calibration_corpus_identity=CALIBRATION_CORPUS_IDENTITY,
    calibration_policy_version=CALIBRATION_POLICY_VERSION,
)


def profile_workers(profile: ReconstructionProfile) -> int:
    """Bounded deterministic worker count (default 4-6 when capability permits)."""
    cpu = os.cpu_count() or 1
    workers = max(1, min(cpu - 1, 6))
    if workers < profile.workers_default and cpu >= profile.workers_default + 1:
        workers = profile.workers_default
    return max(1, workers)


# ----------------------------------------------------------------------
# Deterministic observable HARD classification (never AI self-assessment)
# ----------------------------------------------------------------------

HARD_REASON_COMBINING_MARK = "COMBINING_MARK"
HARD_REASON_METRIC_UNCERTAINTY = "METRIC_UNCERTAINTY"
HARD_REASON_COMPOSITE = "SYNTHESIZED_COMPOSITE"
HARD_REASON_COARSE_RESIDUAL = "COARSE_RESIDUAL"
HARD_REASON_COARSE_COVERAGE = "INSUFFICIENT_INK_FIT"
HARD_REASON_COARSE_BUDGET = "UNSTABLE_COARSE_CONVERGENCE"


def is_combining_mark(code_point: int) -> bool:
    if code_point in _VI_MARK_CODEPOINT_SET:
        return True
    try:
        return unicodedata.combining(chr(code_point)) != 0
    except (ValueError, OverflowError):
        return False


def classify_glyph_difficulty(
    code_point: int,
    metrics: Any,
    config: Any,
    profile: ReconstructionProfile,
    coarse_objective: float | None = None,
    coarse_coverage_loss: float | None = None,
    coarse_stop_reason: str | None = None,
    coarse_outer_contours: int | None = None,
) -> tuple[str, tuple[str, ...]]:
    """Deterministic EASY/HARD classification from observable fit evidence.

    Vietnamese combining marks and newly synthesized composites default
    HARD (D18). Metric boundary uncertainty, coarse residual/coverage,
    budget-exhausted coarse convergence, and multi-component topology are
    observable HARD signals. Returns (classification, reasons).
    """
    if profile.name == PROFILE_FULL_MAX:
        return "HARD", ("FULL_MAX_CANONICAL_COMPLETE_SCHEDULE",)

    reasons: list[str] = []
    if is_combining_mark(code_point):
        reasons.append(HARD_REASON_COMBINING_MARK)
    if metrics is not None and config is not None:
        try:
            phases = config.get_phases_for_metrics(metrics)
            if tuple(phases) == tuple(config.expanded_subpixel_phases) and tuple(
                config.expanded_subpixel_phases
            ) != tuple(config.base_subpixel_phases):
                reasons.append(HARD_REASON_METRIC_UNCERTAINTY)
        except Exception:
            reasons.append(HARD_REASON_METRIC_UNCERTAINTY)
    if coarse_outer_contours is not None and coarse_outer_contours >= 2:
        reasons.append(HARD_REASON_COMPOSITE)
    if coarse_objective is not None and math.isfinite(coarse_objective):
        if coarse_objective > profile.confidence_thresholds.max_full_objective:
            reasons.append(HARD_REASON_COARSE_RESIDUAL)
    if coarse_coverage_loss is not None and math.isfinite(coarse_coverage_loss):
        if coarse_coverage_loss > profile.confidence_thresholds.max_coverage_loss:
            reasons.append(HARD_REASON_COARSE_COVERAGE)
    if coarse_stop_reason is not None and coarse_stop_reason == "ITERATION_BUDGET_EXHAUSTED":
        reasons.append(HARD_REASON_COARSE_BUDGET)

    if reasons:
        return "HARD", tuple(sorted(set(reasons)))
    return "EASY", ()


# ----------------------------------------------------------------------
# Deterministic versioned confidence gate (fit evidence only)
# ----------------------------------------------------------------------

CONFIDENCE_PASS = "PASS"
CONFIDENCE_LOW = "LOW"


@dataclass(frozen=True)
class ProfileConfidence:
    status: str
    score: float
    reasons: tuple[str, ...]
    per_glyph_min: float
    budget_fraction: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "score": self.score,
            "reasons": list(self.reasons),
            "per_glyph_min": self.per_glyph_min,
            "budget_fraction": self.budget_fraction,
        }


def compute_profile_confidence(
    profile: ReconstructionProfile,
    glyph_evidence: Sequence[Mapping[str, Any]],
) -> ProfileConfidence:
    """Deterministic confidence from FIT-only per-glyph search evidence.

    Each evidence mapping carries the glyph's full-resolution objective,
    loss components, final stop reason, and rerank margin produced by the
    search ladder. Missing/incomplete/non-finite inputs fail closed to LOW.
    Held-out/consumer evidence never enters this computation.
    """
    if profile.name == PROFILE_FULL_MAX:
        return ProfileConfidence(
            status=CONFIDENCE_PASS,
            score=1.0,
            reasons=(),
            per_glyph_min=1.0,
            budget_fraction=0.0,
        )

    th = profile.confidence_thresholds
    reasons: list[str] = []
    if not glyph_evidence:
        return ProfileConfidence(CONFIDENCE_LOW, 0.0, ("NO_FIT_EVIDENCE",), 0.0, 1.0)

    glyph_scores: list[float] = []
    budget_glyphs = 0
    for ev in glyph_evidence:
        cp = int(ev.get("code_point", -1))
        obj = ev.get("full_objective")
        cov = ev.get("coverage_loss")
        edge = ev.get("edge_loss")
        sdf = ev.get("sdf_loss")
        stop_reason = str(ev.get("stop_reason", ""))
        values = (obj, cov, edge, sdf)
        if any(v is None or not math.isfinite(float(v)) for v in values):
            reasons.append(f"NON_FINITE_CONFIDENCE_INPUT_CP_{cp:04X}")
            glyph_scores.append(0.0)
            continue
        obj, cov, edge, sdf = (float(v) for v in values)
        if stop_reason == "ITERATION_BUDGET_EXHAUSTED":
            budget_glyphs += 1
        terms = (
            1.0 - (obj / th.max_full_objective if th.max_full_objective > 0 else 0.0),
            1.0 - (cov / th.max_coverage_loss if th.max_coverage_loss > 0 else 0.0),
            1.0 - (edge / th.max_edge_loss if th.max_edge_loss > 0 else 0.0),
            1.0 - (sdf / th.max_sdf_loss if th.max_sdf_loss > 0 else 0.0),
        )
        glyph_score = max(0.0, min(1.0, min(terms)))
        if stop_reason == "ITERATION_BUDGET_EXHAUSTED":
            glyph_score *= 0.75
        glyph_scores.append(glyph_score)
        if obj > th.max_full_objective:
            reasons.append(f"FULL_OBJECTIVE_CP_{cp:04X}")
        if cov > th.max_coverage_loss:
            reasons.append(f"COVERAGE_LOSS_CP_{cp:04X}")
        if edge > th.max_edge_loss:
            reasons.append(f"EDGE_LOSS_CP_{cp:04X}")
        if sdf > th.max_sdf_loss:
            reasons.append(f"SDF_LOSS_CP_{cp:04X}")

    budget_fraction = float(budget_glyphs) / float(len(glyph_evidence))
    if budget_fraction > th.max_budget_fraction:
        reasons.append("BUDGET_FRACTION_EXCEEDED")
    per_glyph_min = float(min(glyph_scores)) if glyph_scores else 0.0
    score = 0.0 if (budget_fraction > th.max_budget_fraction) else per_glyph_min
    status = (
        CONFIDENCE_PASS
        if (not reasons and score >= th.accept_scalar)
        else CONFIDENCE_LOW
    )
    return ProfileConfidence(
        status=status,
        score=round(score, 6),
        reasons=tuple(sorted(set(reasons))),
        per_glyph_min=round(per_glyph_min, 6),
        budget_fraction=round(budget_fraction, 6),
    )


def rerank_margin_accepts_top1(
    profile: ReconstructionProfile,
    pool_size: int,
    best_full_objective: float,
) -> bool:
    """Versioned confidence-margin rule for top-1 full-resolution rerank.

    A single-candidate acceptance (no pool competition) is permitted only
    when the authoritative full-resolution objective proves a coarse-ranking
    inversion cannot materially alter acceptance: the objective must sit
    inside the frozen top-1 bound. Otherwise the ladder must supply a
    competitive pool (>= 2 full-resolution-scored candidates).
    """
    if pool_size >= 2:
        return True
    if pool_size == 1:
        return math.isfinite(best_full_objective) and (
            best_full_objective <= profile.confidence_thresholds.top1_full_objective
        )
    return False
