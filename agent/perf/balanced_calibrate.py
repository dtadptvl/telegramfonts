"""Issue #86 Stage 16 BALANCED_MAX confidence calibration harness (LOCAL_ONLY).

One-time pre-production calibration of the frozen BALANCED_MAX confidence
thresholds against the fixed FULL_MAX reference corpus (the canonical E2E
fixture families of test_issue75_fullmax_e2e). FULL_MAX formation runs on
the complete canonical schedule; the observed per-glyph full-resolution
objective/loss envelope defines the reference quality level, and the
frozen thresholds are the versioned safety-margin envelope around it.

The reference envelope is computed over ink glyphs only; zero-ink glyphs
(canonical 0/0 coverage degeneracy) are recorded separately and handled
deterministically by the search ladder. The accept scalar is derived from
the safety factors (versioned headroom), never tuned per job.

The emitted report is committed evidence; the frozen thresholds in
fidelity/profiles.py must match the derived values below. Re-running is
only legitimate under a new policy version (new corpus/fixture identity
or calibration-semantics change).

Usage:
    python perf/balanced_calibrate.py --out perf/reports/balanced_max_calibration.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR / "src"))
sys.path.insert(0, str(AGENT_DIR))

from fidelity.optimizer import FitOnlyGlyphOptimizer, OptimizerPolicy  # noqa: E402
from fidelity.pipeline import ObservationStoreSnapshot, partition_snapshot  # noqa: E402
from fidelity.profiles import BALANCED_MAX_PROFILE  # noqa: E402
from reconstruction.solver import MaxReconstructionSolver  # noqa: E402
from tests.test_issue75_fullmax_e2e import (  # noqa: E402
    _E2EFixtureSession,
    _E2E_ORIGINAL_COVERAGE,
    _E2E_VI_COVERAGE,
    _collect_family,
)

# Versioned safety factors applied to the reference envelope (frozen with
# the calibration policy version).
SAFETY_FACTORS = {
    "objective": 1.5,
    "coverage": 3.0,
    "edge": 1.6,
    "sdf": 6.0,
    "budget_fraction": 0.25,
    "top1_factor": 0.85,
}
HARD_CAPS = {"objective": 0.75, "coverage": 0.30, "edge": 0.95, "sdf": 0.10}

# Versioned accept-headroom: the accept scalar is DERIVED from the safety
# factors, never hand-tuned per job. A candidate matching the reference
# worst case scores min_c(1 - 1/f_c); the frozen gate must accept such a
# candidate by construction, so the accept scalar sits below that score
# at a fixed headroom fraction.
ACCEPT_HEADROOM_FRACTION = 0.75
_LOSS_FACTOR_KEYS = ("objective", "coverage", "edge", "sdf")


def _derived_accept_scalar() -> float:
    reference_worst_score = min(
        1.0 - 1.0 / SAFETY_FACTORS[k] for k in _LOSS_FACTOR_KEYS
    )
    return round(ACCEPT_HEADROOM_FRACTION * reference_worst_score, 4)


def _is_zero_ink_row(row: dict) -> bool:
    """Deterministic zero-ink signature of the canonical loss decomposition.

    Empty reference vs empty model yields the degenerate 0/0 coverage
    convention (1.0) with zero edge/sdf terms. Such glyphs carry no
    fittable geometry and are excluded from the reference envelope (the
    BALANCED search ladder handles them deterministically as trivial).
    """
    return row["coverage"] == 1.0 and row["edge"] == 0.0 and row["sdf"] == 0.0


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=AGENT_DIR, timeout=30
        ).stdout.strip()
    except Exception:
        return "unknown"


async def _fullmax_formation(base_dir: Path, reference_id: str, family_name: str, session, coverage):
    store, config, bv = await _collect_family(base_dir, reference_id, family_name, session, coverage)
    snapshot = ObservationStoreSnapshot.load_from_store(
        store=store,
        reference_id=reference_id,
        style_id="regular",
        family_name=family_name,
        style_name="Regular",
        config=config,
        browser_version=bv,
    )
    partition = partition_snapshot(snapshot)
    rp = lambda r: snapshot.get_raster_bytes(r.cache_key)
    solver = MaxReconstructionSolver()
    fit_by_cp: dict[int, list] = {}
    for r in partition.fit_records:
        fit_by_cp.setdefault(r.code_point, []).append(r)
    glyphs = {
        cp: solver.reconstruct_glyph([(r, rp(r)) for r in recs])
        for cp, recs in sorted(fit_by_cp.items())
    }
    optimizer = FitOnlyGlyphOptimizer(policy=OptimizerPolicy())
    _optimized, trace = optimizer.optimize(glyphs, partition.fit_records, rp, units_per_em=1000)
    rows = []
    for rec in trace.records:
        comps = dict(rec.loss_components)
        rows.append(
            {
                "code_point": rec.code_point,
                "objective": rec.final_objective,
                "coverage": float(comps["coverage"]),
                "edge": float(comps["edge"]),
                "sdf": float(comps["sdf"]),
                "curvature": float(comps["curvature"]),
                "complexity": float(comps["complexity"]),
                "iterations": rec.iterations,
                "stop_reason": rec.stop_reason,
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    started = time.perf_counter()
    report: dict = {
        "issue": 86,
        "calibration_policy_version": BALANCED_MAX_PROFILE.calibration_policy_version,
        "corpus_identity": BALANCED_MAX_PROFILE.calibration_corpus_identity,
        "profile": BALANCED_MAX_PROFILE.name,
        "profile_hash": BALANCED_MAX_PROFILE.policy_hash(),
        "fullmax_profile_reference": "canonical FULL_MAX formation, complete schedule",
        "safety_factors": SAFETY_FACTORS,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": __import__("os").cpu_count(),
        "git_sha": _git_sha(),
        "coverage_original": list(_E2E_ORIGINAL_COVERAGE),
        "coverage_vietnamese": list(_E2E_VI_COVERAGE),
    }

    async def drive():
        rows: list[dict] = []
        with tempfile.TemporaryDirectory(prefix="issue86_calibrate_") as tmp:
            base = Path(tmp)
            t0 = time.perf_counter()
            rows.extend(
                await _fullmax_formation(
                    base / "o", "e2e_fam", "E2EFam",
                    _E2EFixtureSession(_E2E_ORIGINAL_COVERAGE, "chromium_fullmax_e2e_v1"),
                    _E2E_ORIGINAL_COVERAGE,
                )
            )
            report["original_wall_ms"] = (time.perf_counter() - t0) * 1000.0
            t0 = time.perf_counter()
            rows.extend(
                await _fullmax_formation(
                    base / "v", "e2e_vi_fam", "E2EViFam",
                    _E2EFixtureSession(_E2E_VI_COVERAGE, "chromium_fullmax_e2e_v2"),
                    _E2E_VI_COVERAGE,
                )
            )
            report["vi_wall_ms"] = (time.perf_counter() - t0) * 1000.0
        return rows

    rows = asyncio.run(drive())
    report["reference_glyphs"] = rows

    zero_ink_rows = [r for r in rows if _is_zero_ink_row(r)]
    ink_rows = [r for r in rows if not _is_zero_ink_row(r)]
    if not ink_rows:
        raise SystemExit("CALIBRATION_NO_INK_GLYPHS")
    # Reference envelope: worst per-component loss over INK glyphs only.
    worst_obj = max(r["objective"] for r in ink_rows)
    worst_cov = max(r["coverage"] for r in ink_rows)
    worst_edge = max(r["edge"] for r in ink_rows)
    worst_sdf = max(r["sdf"] for r in ink_rows)
    budget_glyphs = sum(1 for r in rows if r["stop_reason"] != "CONVERGED")
    report["reference_envelope"] = {
        "worst_objective": worst_obj,
        "worst_coverage": worst_cov,
        "worst_edge": worst_edge,
        "worst_sdf": worst_sdf,
        "glyph_count": len(rows),
        "ink_glyph_count": len(ink_rows),
        "zero_ink_code_points": sorted(r["code_point"] for r in zero_ink_rows),
        "budget_exhausted_glyphs": budget_glyphs,
    }

    derived = {
        "max_full_objective": min(HARD_CAPS["objective"], worst_obj * SAFETY_FACTORS["objective"]),
        "max_coverage_loss": min(HARD_CAPS["coverage"], worst_cov * SAFETY_FACTORS["coverage"]),
        "max_edge_loss": min(HARD_CAPS["edge"], worst_edge * SAFETY_FACTORS["edge"]),
        "max_sdf_loss": min(HARD_CAPS["sdf"], worst_sdf * SAFETY_FACTORS["sdf"]),
        "max_budget_fraction": SAFETY_FACTORS["budget_fraction"],
        "accept_scalar": _derived_accept_scalar(),
        "top1_full_objective": min(
            HARD_CAPS["objective"],
            worst_obj * SAFETY_FACTORS["objective"] * SAFETY_FACTORS["top1_factor"],
        ),
    }
    report["derived_frozen_thresholds"] = {k: round(float(v), 6) for k, v in derived.items()}
    report["total_wall_ms"] = (time.perf_counter() - started) * 1000.0

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report["reference_envelope"], indent=2, sort_keys=True))
    print(json.dumps(report["derived_frozen_thresholds"], indent=2, sort_keys=True))
    print(f"artifact: {out_path} total={report['total_wall_ms']:.0f}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
