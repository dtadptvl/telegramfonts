"""Stage 16 (Issue #86 / #6 D18): BALANCED_MAX search ladder over shared interfaces.

Implements the BALANCED_MAX fit/search schedule WITHOUT duplicating the
reconstruction pipeline: the canonical MaxReconstructionSolver and
FitOnlyGlyphOptimizer are reused unchanged; this module only selects
versioned observation subsets (coarse-to-fine), bounds iteration
budgets, reranks the bounded candidate pool on authoritative
full-resolution fit evidence, and assembles results deterministically.

Optimization priorities implemented here (D18 order):
- exact-identity intermediate cache (PNG decode + reference artifacts);
- deterministic per-glyph parallelism with canonical assembly order;
- coarse-to-fine ladder with full-resolution rerank;
- identity-bound checkpoint after every completed glyph and search tier.

Held-out and consumer evidence are structurally unreachable from this
module: every API receives fit records only.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from fidelity.optimizer import (
    GlyphOptimizationRecord,
    OptimizationTrace,
    OptimizerPolicy,
    FitOnlyGlyphOptimizer,
    validate_loss_vector_complete,
)
from fidelity.profiles import (
    BALANCED_MAX_PROFILE,
    PROFILE_BALANCED_MAX,
    ReconstructionProfile,
    classify_glyph_difficulty,
    profile_workers,
    rerank_margin_accepts_top1,
)
from measurement.models import ObservationRecord
from reconstruction.models import Contour, CubicSegment, LineSegment, Point2D, ReconstructedGlyph
from reconstruction.solver import MaxReconstructionSolver

logger = logging.getLogger("telegramfonts.agent.fidelity.balanced_search")

SEARCH_VERSION = "stage16-balanced-search-v1.0.0"
BALANCED_OPTIMIZER_VERSION_PREFIX = "stage16-balanced-max"

# Intermediate-cache identity versions: any version change invalidates
# every entry (fail-closed miss), never a cross-version hit.
DECODE_CACHE_VERSION = "decode-v1"
PREPARE_CACHE_VERSION = "prepare-v1"
_DECODE_CACHE_MAX_ENTRIES = 48
_PREPARE_CACHE_MAX_ENTRIES = 384


# ----------------------------------------------------------------------
# Exact-identity intermediate cache (priority 2)
# ----------------------------------------------------------------------


class IntermediateArtifactCache:
    """Bounded, thread-safe, exact-identity cache of fit preprocessing.

    Two entry classes:
    - decode: binary mask decoded from PNG bytes; content-addressed by
      (DECODE_CACHE_VERSION, resolution, raster SHA256, byte size).
    - prepared: reference-side objective artifacts (crop/edge/SDF/ink);
      identity-bound by (PREPARE_CACHE_VERSION, observation cache_key,
      raster SHA256, resolution). The observation cache_key binds
      reference/style/glyph/browser/resolution/phase/config — every
      truth-changing input of the derivation.

    Hits are validated against the request identity before use; any
    stale/partial/malformed/cross-identity entry is discarded and
    recomputed fail-closed. The cache never substitutes required
    evidence: it only memoizes deterministic preprocessing of sealed,
    hash-verified fit observations.
    """

    def __init__(
        self,
        decode_max_entries: int = _DECODE_CACHE_MAX_ENTRIES,
        prepare_max_entries: int = _PREPARE_CACHE_MAX_ENTRIES,
    ) -> None:
        self._lock = threading.Lock()
        self._decode: "OrderedDict[tuple, Any]" = OrderedDict()
        self._prepare: "OrderedDict[tuple, Any]" = OrderedDict()
        self._decode_max = int(decode_max_entries)
        self._prepare_max = int(prepare_max_entries)
        self.stats = {
            "decode_hits": 0,
            "decode_misses": 0,
            "prepare_hits": 0,
            "prepare_misses": 0,
            "evictions": 0,
            "discarded_invalid": 0,
        }

    @staticmethod
    def decode_key(raster_sha256: str, resolution: int, size_bytes: int) -> tuple:
        return (DECODE_CACHE_VERSION, int(resolution), raster_sha256, int(size_bytes))

    @staticmethod
    def prepare_key(observation_cache_key: str, raster_sha256: str, resolution: int) -> tuple:
        return (PREPARE_CACHE_VERSION, observation_cache_key, raster_sha256, int(resolution))

    def get_decode(self, key: tuple) -> Any:
        with self._lock:
            if key in self._decode:
                self._decode.move_to_end(key)
                self.stats["decode_hits"] += 1
                return self._decode[key]
            self.stats["decode_misses"] += 1
            return None

    def put_decode(self, key: tuple, mask: Any) -> None:
        with self._lock:
            self._decode[key] = mask
            self._decode.move_to_end(key)
            while len(self._decode) > self._decode_max:
                self._decode.popitem(last=False)
                self.stats["evictions"] += 1

    def get_prepared(self, key: tuple) -> Any:
        with self._lock:
            if key in self._prepare:
                self._prepare.move_to_end(key)
                self.stats["prepare_hits"] += 1
                return self._prepare[key]
            self.stats["prepare_misses"] += 1
            return None

    def put_prepared(self, key: tuple, artifacts: Any) -> None:
        with self._lock:
            self._prepare[key] = artifacts
            self._prepare.move_to_end(key)
            while len(self._prepare) > self._prepare_max:
                self._prepare.popitem(last=False)
                self.stats["evictions"] += 1

    def discard_invalid(self, kind: str, key: tuple) -> None:
        with self._lock:
            store = self._decode if kind == "decode" else self._prepare
            if key in store:
                del store[key]
                self.stats["discarded_invalid"] += 1

    def snapshot_stats(self) -> dict[str, int]:
        with self._lock:
            return dict(self.stats)


# Process-wide bounded cache: shared by every BALANCED_MAX formation in
# the process (optimize-once-reuse-everywhere within a job and across
# TTF/OTF/repeat gates), and reused by the FULL_MAX fallback because the
# cached derivations are pure functions of sealed, hash-verified fit
# observation bytes (FULL_MAX-compatible intermediates).
GLOBAL_INTERMEDIATE_CACHE = IntermediateArtifactCache()


def reset_global_intermediate_cache() -> None:
    """Test/admin boundary: drop all cached intermediates and counters."""
    global GLOBAL_INTERMEDIATE_CACHE
    GLOBAL_INTERMEDIATE_CACHE = IntermediateArtifactCache()


# ----------------------------------------------------------------------
# Identity-bound per-glyph / per-tier checkpoints
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class CheckpointIdentity:
    """Complete identity bound into every checkpoint entry."""

    snapshot_fingerprint: str
    fit_evidence_fingerprint: str
    profile_hash: str
    search_version: str

    def to_dict(self) -> dict[str, str]:
        return {
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "fit_evidence_fingerprint": self.fit_evidence_fingerprint,
            "profile_hash": self.profile_hash,
            "search_version": self.search_version,
        }


class GlyphCheckpointStore:
    """Identity-bound checkpoint files under one gate working directory.

    Entries are JSON payloads carrying the checkpoint identity, a
    canonical payload, and a SHA-256 over the canonical payload. On
    load, identity AND payload hash must match exactly; any
    stale/partial/malformed/cross-identity entry is deleted and treated
    as a miss (fail-closed recompute). Checkpoints never skip canonical
    FULL_MAX work: only the BALANCED_MAX ladder reads them, keyed by the
    BALANCED policy hash.
    """

    def __init__(self, root: Path, profile: ReconstructionProfile) -> None:
        self.root = Path(root) / "stage16_ckpts" / profile.policy_hash()[:16]
        self.root.mkdir(parents=True, exist_ok=True)
        self.stats = {"hits": 0, "misses": 0, "invalid_discarded": 0, "writes": 0}

    def _path(self, code_point: int, tier: str) -> Path:
        return self.root / f"cp_{code_point:06X}_{tier}.json"

    def load(
        self, code_point: int, tier: str, identity: CheckpointIdentity
    ) -> dict[str, Any] | None:
        path = self._path(code_point, tier)
        if not path.is_file():
            self.stats["misses"] += 1
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("CHECKPOINT_NOT_A_MAPPING")
            if dict(raw.get("identity") or {}) != identity.to_dict():
                raise ValueError("CHECKPOINT_IDENTITY_MISMATCH")
            payload = raw.get("payload")
            payload_hash = str(raw.get("payload_hash") or "")
            if not isinstance(payload, dict) or not payload_hash:
                raise ValueError("CHECKPOINT_MALFORMED")
            recomputed = hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            if recomputed != payload_hash:
                raise ValueError("CHECKPOINT_PAYLOAD_DRIFT")
            self.stats["hits"] += 1
            return payload
        except Exception:
            self.stats["invalid_discarded"] += 1
            try:
                path.unlink()
            except OSError:
                pass
            self.stats["misses"] += 1
            return None

    def save(
        self, code_point: int, tier: str, identity: CheckpointIdentity, payload: dict[str, Any]
    ) -> None:
        path = self._path(code_point, tier)
        payload_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        envelope = {
            "identity": identity.to_dict(),
            "payload": payload,
            "payload_hash": payload_hash,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(envelope, sort_keys=True, separators=(",", ":")), encoding="utf-8"
        )
        tmp.replace(path)
        self.stats["writes"] += 1

    def snapshot_stats(self) -> dict[str, int]:
        return dict(self.stats)


# ----------------------------------------------------------------------
# Deterministic glyph (de)serialization for checkpoints
# ----------------------------------------------------------------------


def _point_to_list(p: Point2D) -> list[float]:
    return [float(p.x), float(p.y)]


def _contour_to_dict(c: Contour) -> dict[str, Any]:
    segments: list[dict[str, Any]] = []
    for s in c.segments:
        if isinstance(s, CubicSegment):
            segments.append(
                {
                    "kind": "cubic",
                    "p0": _point_to_list(s.p0),
                    "p1": _point_to_list(s.p1),
                    "p2": _point_to_list(s.p2),
                    "p3": _point_to_list(s.p3),
                }
            )
        else:
            segments.append(
                {"kind": "line", "p0": _point_to_list(s.p0), "p1": _point_to_list(s.p1)}
            )
    return {
        "segments": segments,
        "is_hole": bool(c.is_hole),
        "parent_index": None if c.parent_index is None else int(c.parent_index),
        "area_upem": float(c.area_upem),
    }


def _glyph_to_payload(g: ReconstructedGlyph) -> dict[str, Any]:
    return {
        "code_point": int(g.code_point),
        "character": g.character,
        "advance_width_upem": float(g.advance_width_upem),
        "lsb_upem": float(g.lsb_upem),
        "rsb_upem": float(g.rsb_upem),
        "ascent_upem": float(g.ascent_upem),
        "descent_upem": float(g.descent_upem),
        "bounding_box_upem": [float(v) for v in g.bounding_box_upem],
        "contours": [_contour_to_dict(c) for c in g.contours],
    }


def _payload_to_glyph(payload: Mapping[str, Any]) -> ReconstructedGlyph:
    contours: list[Contour] = []
    for c in payload.get("contours", []):
        segments: list[Any] = []
        for s in c.get("segments", []):
            if s.get("kind") == "cubic":
                segments.append(
                    CubicSegment(
                        p0=Point2D(*s["p0"]),
                        p1=Point2D(*s["p1"]),
                        p2=Point2D(*s["p2"]),
                        p3=Point2D(*s["p3"]),
                    )
                )
            else:
                segments.append(LineSegment(p0=Point2D(*s["p0"]), p1=Point2D(*s["p1"])))
        parent_index = c.get("parent_index")
        contours.append(
            Contour(
                segments=segments,
                is_hole=bool(c.get("is_hole", False)),
                parent_index=None if parent_index is None else int(parent_index),
                area_upem=float(c.get("area_upem", 0.0)),
            )
        )
    return ReconstructedGlyph(
        code_point=int(payload["code_point"]),
        character=str(payload["character"]),
        advance_width_upem=float(payload["advance_width_upem"]),
        lsb_upem=float(payload["lsb_upem"]),
        rsb_upem=float(payload["rsb_upem"]),
        ascent_upem=float(payload["ascent_upem"]),
        descent_upem=float(payload["descent_upem"]),
        contours=contours,
        bounding_box_upem=tuple(float(v) for v in payload["bounding_box_upem"]),
        reconstruction_time_ms=0.0,
    )


def glyph_payload_hash(g: ReconstructedGlyph) -> str:
    return hashlib.sha256(
        json.dumps(_glyph_to_payload(g), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


# ----------------------------------------------------------------------
# BALANCED_MAX search ladder (coarse-to-fine + full-resolution rerank)
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class GlyphSearchRecord:
    """Deterministic per-glyph search evidence (fit evidence only)."""

    code_point: int
    classification: str
    classification_reasons: tuple[str, ...]
    tiers_executed: tuple[str, ...]
    iterations_per_tier: tuple[int, ...]
    objectives_per_tier: tuple[float, ...]
    early_stopped: bool
    pool_size: int
    full_objective: float
    coverage_loss: float
    edge_loss: float
    sdf_loss: float
    curvature_loss: float
    complexity_loss: float
    stop_reason: str
    selected_candidate: str
    margin: float
    checkpoint_resumed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "code_point": self.code_point,
            "classification": self.classification,
            "classification_reasons": list(self.classification_reasons),
            "tiers_executed": list(self.tiers_executed),
            "iterations_per_tier": list(self.iterations_per_tier),
            "objectives_per_tier": [repr(v) for v in self.objectives_per_tier],
            "early_stopped": self.early_stopped,
            "pool_size": self.pool_size,
            "full_objective": repr(self.full_objective),
            "coverage_loss": repr(self.coverage_loss),
            "edge_loss": repr(self.edge_loss),
            "sdf_loss": repr(self.sdf_loss),
            "curvature_loss": repr(self.curvature_loss),
            "complexity_loss": repr(self.complexity_loss),
            "stop_reason": self.stop_reason,
            "selected_candidate": self.selected_candidate,
            "margin": repr(self.margin),
            "checkpoint_resumed": self.checkpoint_resumed,
        }


@dataclass(frozen=True)
class BalancedFormationResult:
    """Deterministic outcome of one BALANCED_MAX model formation."""

    optimized_glyphs: dict[int, ReconstructedGlyph]
    trace: OptimizationTrace
    search_records: tuple[GlyphSearchRecord, ...]
    confidence_evidence: tuple[dict[str, Any], ...]
    cache_stats: dict[str, int]
    checkpoint_stats: dict[str, int]
    worker_count: int


class BalancedMaxSearch:
    """BALANCED_MAX model formation over the shared solver/optimizer."""

    def __init__(
        self,
        profile: ReconstructionProfile,
        cache: IntermediateArtifactCache | None = None,
        checkpoint_store: GlyphCheckpointStore | None = None,
    ) -> None:
        if profile.name != PROFILE_BALANCED_MAX:
            raise ValueError("BALANCED_SEARCH_PROFILE_INVALID")
        self.profile = profile
        self.cache = cache if cache is not None else GLOBAL_INTERMEDIATE_CACHE
        self.checkpoint_store = checkpoint_store

    # -- observation subset selection (deterministic, config-clamped) --

    def _phase_key(self, record: ObservationRecord) -> tuple[float, float]:
        return (round(float(record.subpixel_x), 4), round(float(record.subpixel_y), 4))

    def _tier_observations(
        self,
        cp_records: Sequence[ObservationRecord],
        core_resolutions: set[int],
        tier_phases: set[tuple[float, float]] | None,
    ) -> list[ObservationRecord]:
        selected: list[ObservationRecord] = []
        for r in sorted(cp_records, key=lambda rec: rec.cache_key):
            if int(r.resolution) not in core_resolutions:
                continue
            if tier_phases is not None and self._phase_key(r) not in tier_phases:
                continue
            selected.append(r)
        return selected

    # -- prepared observations with exact-identity caching -------------

    def _prepare_observations(
        self,
        optimizer: FitOnlyGlyphOptimizer,
        cp_records: Sequence[ObservationRecord],
        raster_provider: Callable[[ObservationRecord], bytes],
        units_per_em: int,
    ) -> list[tuple]:
        return optimizer.prepare_glyph_observations(
            cp_records, raster_provider, units_per_em=units_per_em, cache=self.cache
        )

    # -- one tier: reconstruct + bounded optimize on the tier subset ---

    def _run_tier(
        self,
        tier_name: str,
        glyph_source: ReconstructedGlyph | None,
        tier_records: Sequence[ObservationRecord],
        raster_provider: Callable[[ObservationRecord], bytes],
        units_per_em: int,
        max_iterations: int,
        allow_scale_search: bool = True,
    ) -> tuple[ReconstructedGlyph, GlyphOptimizationRecord, list[tuple]]:
        if not tier_records:
            raise ValueError(f"BALANCED_TIER_NO_EVIDENCE:{tier_name}")
        if glyph_source is None:
            solver = MaxReconstructionSolver()
            observations = [
                (r, raster_provider(r)) for r in sorted(tier_records, key=lambda x: x.cache_key)
            ]
            glyph_source = solver.reconstruct_glyph(observations)
        tier_optimizer = FitOnlyGlyphOptimizer(
            policy=OptimizerPolicy(max_iterations=max_iterations)
        )
        prepared = self._prepare_observations(
            tier_optimizer, tier_records, raster_provider, units_per_em
        )
        # Bounded tier budget: budget exhaustion preserves the best valid
        # candidate (versioned schedule delta); non-finite objectives and
        # evidence failures still raise fail-closed.
        optimized, record = tier_optimizer.optimize_glyph(
            glyph_source,
            prepared,
            fail_on_budget_exhaustion=False,
            allow_scale_search=allow_scale_search,
        )
        return optimized, record, prepared

    def _run_expansion_ladder(
        self,
        sorted_records: Sequence[ObservationRecord],
        core_set: set[int],
        base_phases: set[tuple[float, float]],
        expanded_phases: set[tuple[float, float]],
        raster_provider: Callable[[ObservationRecord], bytes],
        units_per_em: int,
        cand: ReconstructedGlyph,
        rec: GlyphOptimizationRecord,
        pool: list[tuple[str, ReconstructedGlyph]],
        tiers_executed: list[str],
        tier_iterations: list[int],
        tier_objectives: list[float],
    ) -> tuple[ReconstructedGlyph, GlyphOptimizationRecord, bool]:
        """HARD expansion ladder: 4x4 -> 8x8 -> complete required set (D18).

        Deterministic early-stop on the expansion tiers only; the complete
        required resolution set is mandatory (run even after early-stop).
        The authoritative FULL_SET tier always starts from the canonical
        solver reconstruction (never a subset-trained warm start), so the
        complete-set candidate is the deterministic canonical-schedule
        result on the complete evidence set. Mutates the pool/tier lists.
        """
        profile = self.profile
        ladder: list[tuple[str, set[int], set[tuple[float, float]] | None, int]] = [
            ("HARD_4X4", core_set, base_phases, profile.tier_max_iterations),
            ("HARD_8X8", core_set, expanded_phases, profile.tier_max_iterations),
            ("FULL_SET", set(int(r.resolution) for r in sorted_records), None,
             profile.final_max_iterations),
        ]
        early_stopped = False
        prev_best = rec.final_objective
        stagnant_rounds = 0
        for tier_name, tier_res, tier_ph, budget in ladder:
            obs = self._tier_observations(sorted_records, tier_res, tier_ph)
            if not obs:
                continue
            tier_source = None if tier_name == "FULL_SET" else cand
            if tier_name != "FULL_SET":
                # Deterministic early-stop on the expansion ladder:
                # versioned absolute AND relative improvement below
                # thresholds for the required consecutive rounds. The
                # best valid candidate is always preserved; held-out /
                # consumer evidence never participates.
                if (
                    stagnant_rounds >= profile.early_stop_rounds
                    and profile.early_stop_rounds > 0
                ):
                    early_stopped = True
                    break
            cand, rec, _prepared = self._run_tier(
                tier_name, tier_source, obs, raster_provider, units_per_em, budget
            )
            tiers_executed.append(tier_name)
            tier_iterations.append(rec.iterations)
            tier_objectives.append(rec.final_objective)
            pool.append((f"{tier_name}:{rec.selected_variant}", cand))
            improvement = prev_best - rec.final_objective
            if (
                improvement < profile.early_stop_abs_tol
                and improvement < profile.early_stop_rel_tol * max(abs(prev_best), 1e-12)
            ):
                stagnant_rounds += 1
            else:
                stagnant_rounds = 0
            prev_best = rec.final_objective
        # The complete required resolution set is mandatory for HARD:
        # if early-stop skipped it, run it now (never publish coarse).
        if tiers_executed[-1] != "FULL_SET":
            obs = self._tier_observations(
                sorted_records, set(int(r.resolution) for r in sorted_records), None
            )
            cand, rec, _prepared = self._run_tier(
                "FULL_SET", None, obs, raster_provider, units_per_em,
                profile.final_max_iterations,
            )
            tiers_executed.append("FULL_SET")
            tier_iterations.append(rec.iterations)
            tier_objectives.append(rec.final_objective)
            pool.append((f"FULL_SET:{rec.selected_variant}", cand))
        return cand, rec, early_stopped

    # -- full ladder for one glyph --------------------------------------

    def _fit_glyph(
        self,
        code_point: int,
        cp_records: Sequence[ObservationRecord],
        raster_provider: Callable[[ObservationRecord], bytes],
        units_per_em: int,
        config: Any,
        identity: CheckpointIdentity,
    ) -> tuple[ReconstructedGlyph, GlyphSearchRecord]:
        if not cp_records:
            raise ValueError(f"BALANCED_NO_FIT_EVIDENCE_CP_{code_point}")
        sorted_records = sorted(cp_records, key=lambda rec: rec.cache_key)
        fit_fp_cp = hashlib.sha256(
            ":".join(r.raster_sha256 for r in sorted_records).encode("utf-8")
        ).hexdigest()
        cp_identity = CheckpointIdentity(
            snapshot_fingerprint=identity.snapshot_fingerprint,
            fit_evidence_fingerprint=fit_fp_cp,
            profile_hash=identity.profile_hash,
            search_version=identity.search_version,
        )

        # Identity-bound glyph checkpoint: a completed compatible glyph
        # skips the whole ladder (resume), never partial truth.
        if self.checkpoint_store is not None:
            payload = self.checkpoint_store.load(code_point, "glyph", cp_identity)
            if payload is not None:
                glyph = _payload_to_glyph(payload["glyph"])
                rd = payload["record"]
                resumed = GlyphSearchRecord(
                    code_point=int(rd["code_point"]),
                    classification=str(rd["classification"]),
                    classification_reasons=tuple(rd["classification_reasons"]),
                    tiers_executed=tuple(rd["tiers_executed"]),
                    iterations_per_tier=tuple(int(v) for v in rd["iterations_per_tier"]),
                    objectives_per_tier=tuple(float(v) for v in rd["objectives_per_tier"]),
                    early_stopped=bool(rd["early_stopped"]),
                    pool_size=int(rd["pool_size"]),
                    full_objective=float(rd["full_objective"]),
                    coverage_loss=float(rd["coverage_loss"]),
                    edge_loss=float(rd["edge_loss"]),
                    sdf_loss=float(rd["sdf_loss"]),
                    curvature_loss=float(rd["curvature_loss"]),
                    complexity_loss=float(rd["complexity_loss"]),
                    stop_reason=str(rd["stop_reason"]),
                    selected_candidate=str(rd["selected_candidate"]),
                    margin=float(rd["margin"]),
                    checkpoint_resumed=True,
                )
                return glyph, resumed

        profile = self.profile

        # Prepare the complete per-glyph fit set once (cached, hash-verified):
        # it serves the zero-ink probe, every ladder tier subset (cache hits),
        # and the authoritative full-resolution rerank.
        full_prepared = self._prepare_observations(
            FitOnlyGlyphOptimizer(policy=OptimizerPolicy(max_iterations=1)),
            sorted_records,
            raster_provider,
            units_per_em,
        )

        # ---- ZERO-INK trivial path (deterministic, fail-closed) ----
        # A glyph whose sealed fit observations carry no ink anywhere has
        # no fittable geometry: the deterministic outcome is the solver
        # glyph with perfect fit evidence. The canonical loss
        # decomposition reports the degenerate 0/0 coverage convention
        # (1.0) for empty-vs-empty; that degeneracy is not defect
        # evidence, so the confidence record normalizes it to 0.0 and
        # names the reason explicitly. Every unchanged downstream gate
        # still applies to the built glyph.
        if all(int(entry[3]["ref_count"]) == 0 for entry in full_prepared):
            solver = MaxReconstructionSolver()
            observations = [(r, raster_provider(r)) for r in sorted_records]
            zero_glyph = solver.reconstruct_glyph(observations)
            zero_record = GlyphSearchRecord(
                code_point=code_point,
                classification="EASY",
                classification_reasons=("ZERO_INK_TRIVIAL",),
                tiers_executed=("ZERO_INK",),
                iterations_per_tier=(0,),
                objectives_per_tier=(0.0,),
                early_stopped=False,
                pool_size=1,
                full_objective=0.0,
                coverage_loss=0.0,
                edge_loss=0.0,
                sdf_loss=0.0,
                curvature_loss=0.0,
                complexity_loss=0.0,
                stop_reason="ZERO_INK_TRIVIAL",
                selected_candidate="ZERO_INK:solver",
                margin=0.0,
                checkpoint_resumed=False,
            )
            if self.checkpoint_store is not None:
                self.checkpoint_store.save(
                    code_point,
                    "glyph",
                    cp_identity,
                    {
                        "glyph": _glyph_to_payload(zero_glyph),
                        "record": zero_record.to_dict(),
                    },
                )
            return zero_glyph, zero_record

        core = profile.select_core_resolutions(
            tuple(sorted({int(r.resolution) for r in sorted_records}))
        )
        core_set = set(int(r) for r in core)
        base_phases = {
            (round(float(x), 4), round(float(y), 4))
            for x, y in config.base_subpixel_phases
        }
        expanded_phases = {
            (round(float(x), 4), round(float(y), 4))
            for x, y in config.expanded_subpixel_phases
        }
        easy_phases = set(profile.select_easy_phases(tuple(sorted(base_phases))))

        first_metrics = sorted_records[0].metrics

        # ---- CORE FIT tier (versioned core resolutions) ----
        classification, reasons = classify_glyph_difficulty(
            code_point, first_metrics, config, profile
        )
        tier0_phases = base_phases if classification == "HARD" else easy_phases
        tier_records = self._tier_observations(sorted_records, core_set, tier0_phases)
        if not tier_records:
            # Deterministic clamp fallback: the config carries no subset
            # member, so the complete per-glyph fit set is the only
            # fail-closed evidence selection.
            tier_records = list(sorted_records)

        tiers_executed: list[str] = ["CORE"]
        tier_iterations: list[int] = []
        tier_objectives: list[float] = []
        pool: list[tuple[str, ReconstructedGlyph]] = []

        cand, rec, _prepared = self._run_tier(
            "CORE", None, tier_records, raster_provider, units_per_em,
            profile.coarse_max_iterations,
        )
        tier_iterations.append(rec.iterations)
        tier_objectives.append(rec.final_objective)
        pool.append((f"CORE:{rec.selected_variant}", cand))

        coarse_components = dict(rec.loss_components)
        classification, reasons = classify_glyph_difficulty(
            code_point,
            first_metrics,
            config,
            profile,
            coarse_objective=rec.final_objective,
            coarse_coverage_loss=coarse_components.get("coverage"),
            coarse_stop_reason=rec.stop_reason,
            coarse_outer_contours=sum(1 for c in cand.contours if not c.is_hole),
        )

        # ---- HARD expansion ladder: 4x4 -> 8x8 -> complete set ----
        early_stopped = False
        if classification == "HARD":
            cand, rec, early_stopped = self._run_expansion_ladder(
                sorted_records, core_set, base_phases, expanded_phases,
                raster_provider, units_per_em, cand, rec,
                pool, tiers_executed, tier_iterations, tier_objectives,
            )

        # ---- FULL-RESOLUTION RERANK (authoritative fit evidence) ----
        # Coarse stages only propose; the bounded pool is scored on the
        # complete per-glyph fit set (prepared once above). Deduplicate
        # shape-identical candidates deterministically before scoring.
        rerank_optimizer = FitOnlyGlyphOptimizer(
            policy=OptimizerPolicy(max_iterations=1)
        )

        def rerank_pool():
            # Pool selection (deterministic coarse ranking): rank the tier
            # candidates by their tier final objective, keep the bounded
            # top-k, and ALWAYS include the finest-tier candidate (the only
            # candidate trained on the complete required evidence set).
            pool_limit = max(2, int(profile.rerank_top_k))
            order = sorted(
                range(len(pool)), key=lambda i: (tier_objectives[i], pool[i][0])
            )
            keep = set(order[:pool_limit])
            keep.add(len(pool) - 1)
            selected_pool = [pool[i] for i in sorted(keep)]
            seen_hashes: set[str] = set()
            deduped: list[tuple[str, ReconstructedGlyph]] = []
            for name, glyph in selected_pool:
                gh = glyph_payload_hash(glyph)
                if gh in seen_hashes:
                    continue
                seen_hashes.add(gh)
                deduped.append((name, glyph))

            scored_local: list[tuple[float, int, str, ReconstructedGlyph, dict[str, float]]] = []
            for idx, (name, glyph) in enumerate(deduped):
                objective, components = rerank_optimizer.score_glyph(
                    glyph.contours, full_prepared
                )
                scored_local.append((objective, idx, name, glyph, components))
            scored_local.sort(key=lambda item: (item[0], item[1]))
            # Publication eligibility: only candidates trained on the
            # complete required evidence set (FULL_SET tier) may win a HARD
            # search. Subset-trained coarse candidates propose but never
            # publish: a subset-trained local optimum can beat the canonical
            # full-schedule optimum on the fit objective while still losing
            # the unchanged held-out consumer fidelity gates (coarse
            # evidence is never final publication evidence, extended to
            # coarse-trained candidates). The EASY path (no FULL_SET tier)
            # keeps its single CORE candidate under the versioned accept
            # margin check below plus the top-1 confidence-margin bound.
            complete = [item for item in scored_local if item[2].startswith("FULL_SET")]
            eligible_local = complete if complete else scored_local
            winner = min(eligible_local, key=lambda item: (item[0], item[1]))
            competitor_objectives = [
                item[0] for item in scored_local if item[1] != winner[1]
            ]
            margin_local = (
                float(min(competitor_objectives) - winner[0])
                if competitor_objectives
                else 0.0
            )
            if not rerank_margin_accepts_top1(profile, len(eligible_local), winner[0]):
                raise ValueError(
                    f"BALANCED_RERANK_TOP1_REJECTED_CP_{code_point}: "
                    f"pool={len(scored_local)} objective={winner[0]!r} exceeds "
                    f"the versioned confidence-margin bound"
                )
            return scored_local, complete, winner, margin_local

        scored, complete_set, winner, margin = rerank_pool()
        best_obj, _best_idx, best_name, best_glyph, best_components = winner

        # ---- EASY accept-margin check (fit evidence only) ----
        # A reduced-schedule EASY candidate publishes only when its
        # authoritative full-resolution evidence already sits inside the
        # confidence accept margin; otherwise the glyph is deterministically
        # promoted into the complete HARD ladder (one expansion, same
        # profile, no bouncing; held-out/consumer evidence never feeds
        # this decision).
        if not complete_set:
            th = profile.confidence_thresholds
            bound_scale = 1.0 - th.accept_scalar
            within_margin = (
                best_obj <= bound_scale * th.max_full_objective
                and best_components["coverage"] <= bound_scale * th.max_coverage_loss
                and best_components["edge"] <= bound_scale * th.max_edge_loss
                and best_components["sdf"] <= bound_scale * th.max_sdf_loss
            )
            if not within_margin:
                classification = "HARD"
                reasons = tuple(
                    sorted(set(reasons) | {"EASY_ACCEPT_MARGIN_MISSED"})
                )
                cand, rec, early_stopped = self._run_expansion_ladder(
                    sorted_records, core_set, base_phases, expanded_phases,
                    raster_provider, units_per_em, cand, rec,
                    pool, tiers_executed, tier_iterations, tier_objectives,
                )
                scored, complete_set, winner, margin = rerank_pool()
                best_obj, _best_idx, best_name, best_glyph, best_components = winner

        final_stop = rec.stop_reason
        search_record = GlyphSearchRecord(
            code_point=code_point,
            classification=classification,
            classification_reasons=reasons,
            tiers_executed=tuple(tiers_executed),
            iterations_per_tier=tuple(tier_iterations),
            objectives_per_tier=tuple(float(v) for v in tier_objectives),
            early_stopped=early_stopped,
            pool_size=len(scored),
            full_objective=float(best_obj),
            coverage_loss=float(best_components["coverage"]),
            edge_loss=float(best_components["edge"]),
            sdf_loss=float(best_components["sdf"]),
            curvature_loss=float(best_components["curvature"]),
            complexity_loss=float(best_components["complexity"]),
            stop_reason=final_stop,
            selected_candidate=best_name,
            margin=margin,
            checkpoint_resumed=False,
        )

        if self.checkpoint_store is not None:
            self.checkpoint_store.save(
                code_point,
                "glyph",
                cp_identity,
                {
                    "glyph": _glyph_to_payload(best_glyph),
                    "record": search_record.to_dict(),
                },
            )
        return best_glyph, search_record

    # -- deterministic parallel model formation -------------------------

    def form_model(
        self,
        snapshot: Any,
        partition: Any,
        config: Any,
        raster_provider: Callable[[ObservationRecord], bytes],
        units_per_em: int = 1000,
        workers: int | None = None,
    ) -> BalancedFormationResult:
        """Form the optimized glyph set under the BALANCED_MAX schedule.

        Deterministic per-glyph parallelism: each glyph is independently
        identity-bound and fail-closed; completion order can never affect
        the final model ordering/hashes/evidence because assembly walks
        the canonical sorted code point order and every per-glyph
        computation is deterministic. Held-out records are never passed
        to this API (structural guarantee).
        """
        fit_records = tuple(partition.fit_records)
        if not fit_records:
            raise ValueError("BALANCED_NO_FIT_EVIDENCE")
        by_cp: dict[int, list[ObservationRecord]] = {}
        for r in fit_records:
            by_cp.setdefault(r.code_point, []).append(r)
        cps = sorted(by_cp)

        identity = CheckpointIdentity(
            snapshot_fingerprint=snapshot.snapshot_fingerprint,
            fit_evidence_fingerprint=partition.fit_set_fingerprint,
            profile_hash=self.profile.policy_hash(),
            search_version=SEARCH_VERSION,
        )

        worker_count = max(1, int(workers or profile_workers(self.profile)))

        def task(cp: int) -> tuple[int, ReconstructedGlyph, GlyphSearchRecord]:
            glyph, record = self._fit_glyph(
                cp, by_cp[cp], raster_provider, units_per_em, config, identity
            )
            return cp, glyph, record

        results: dict[int, tuple[ReconstructedGlyph, GlyphSearchRecord]] = {}
        if worker_count <= 1 or len(cps) <= 1:
            for cp in cps:
                _cp, glyph, record = task(cp)
                results[_cp] = (glyph, record)
        else:
            with ThreadPoolExecutor(max_workers=worker_count) as pool_exec:
                futures = {pool_exec.submit(task, cp): cp for cp in cps}
                for future in futures:
                    cp, glyph, record = future.result()
                    results[cp] = (glyph, record)

        # Canonical deterministic assembly order (sorted code points).
        optimized_glyphs: dict[int, ReconstructedGlyph] = {}
        search_records: list[GlyphSearchRecord] = []
        confidence_evidence: list[dict[str, Any]] = []
        records_for_trace: list[GlyphOptimizationRecord] = []
        total_iterations = 0
        for cp in cps:
            glyph, srec = results[cp]
            optimized_glyphs[cp] = glyph
            search_records.append(srec)
            confidence_evidence.append(
                {
                    "code_point": cp,
                    "full_objective": srec.full_objective,
                    "coverage_loss": srec.coverage_loss,
                    "edge_loss": srec.edge_loss,
                    "sdf_loss": srec.sdf_loss,
                    "stop_reason": srec.stop_reason,
                    "margin": srec.margin,
                }
            )
            trace_record = GlyphOptimizationRecord(
                code_point=cp,
                initial_objective=float(srec.objectives_per_tier[0])
                if srec.objectives_per_tier
                else srec.full_objective,
                final_objective=float(srec.full_objective),
                iterations=int(sum(srec.iterations_per_tier)) if srec.iterations_per_tier else 0,
                stop_reason=srec.stop_reason,
                accepted_objective_trace=tuple(
                    float(v) for v in srec.objectives_per_tier
                ) + (float(srec.full_objective),),
                loss_components=(
                    ("coverage", float(srec.coverage_loss)),
                    ("edge", float(srec.edge_loss)),
                    ("sdf", float(srec.sdf_loss)),
                    ("curvature", float(srec.curvature_loss)),
                    ("complexity", float(srec.complexity_loss)),
                ),
                selected_variant=srec.selected_candidate,
                transform=(0.0, 0.0, 1.0, 1.0),
            )
            validate_loss_vector_complete(trace_record)
            records_for_trace.append(trace_record)
            total_iterations += trace_record.iterations

        trace = OptimizationTrace(
            optimizer_version=(
                f"{BALANCED_OPTIMIZER_VERSION_PREFIX}-"
                f"{self.profile.profile_version}:{self.profile.policy_hash()[:16]}"
            ),
            input_fingerprint=FitOnlyGlyphOptimizer.compute_input_fingerprint(fit_records),
            policy=OptimizerPolicy(max_iterations=self.profile.final_max_iterations),
            records=tuple(records_for_trace),
            total_iterations=total_iterations,
            converged=True,
            stop_reason="BALANCED_LADDER_COMPLETE",
        )

        return BalancedFormationResult(
            optimized_glyphs=optimized_glyphs,
            trace=trace,
            search_records=tuple(search_records),
            confidence_evidence=tuple(confidence_evidence),
            cache_stats=self.cache.snapshot_stats(),
            checkpoint_stats=(
                self.checkpoint_store.snapshot_stats() if self.checkpoint_store else {}
            ),
            worker_count=worker_count,
        )
