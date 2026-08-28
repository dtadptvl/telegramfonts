"""Issue #82 focused performance-intervention tests (LOCAL_ONLY, NO_LOOP).

Narrow repros for the two causally verified Stage 9D redundancies:

1. Gate-internal identity-bound memo of model formation: hit/miss/
   drift-guard/LRU. A hit skips ONLY reconstruction/optimization/
   calibration/assembly; snapshot load, partition, candidate build,
   four-consumer evidence, held-out evaluation and attestation still run
   fail-closed on every call and every truth identity is preserved.
2. Single-format candidate build on gate paths: each gate builds exactly
   the requested format; default callers keep building both; built bytes
   are identical to the dual-format build.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from fidelity.evaluator import FidelityEvaluator
from fidelity.optimizer import FitOnlyGlyphOptimizer, OptimizerPolicy
from fidelity.release_gate import Stage9DReleaseGate, _ModelFormationEntry
from reconstruction.candidate_builder import MaxCandidateFontBuilder
from reconstruction.solver import MaxReconstructionSolver
from tests.test_stage9d_release_gate import _rect_glyph, _seed_completed_store


@pytest.fixture(autouse=True)
def _clear_formation_memo():
    Stage9DReleaseGate._formation_memo_clear()
    yield
    Stage9DReleaseGate._formation_memo_clear()


def _count(monkeypatch, target, attr):
    calls: list[int] = []
    fn = getattr(target, attr)

    def wrapper(*args, **kwargs):
        calls.append(1)
        return fn(*args, **kwargs)

    monkeypatch.setattr(target, attr, wrapper)
    return calls


def _gate_kwargs(store, config, bv, fmt, out_dir):
    return dict(
        store=store,
        config=config,
        reference_id="stage9d_family",
        style_id="regular",
        family_name="Stage9DFamily",
        style_name="Regular",
        browser_version=bv,
        format_type=fmt,
        output_dir=out_dir,
    )


# =========================================================================
# Change 2: single-format candidate build
# =========================================================================

def test_candidate_family_format_selection_builds_only_requested(tmp_path):
    glyphs = {65: _rect_glyph(), 66: _rect_glyph(code_point=66)}
    builder = MaxCandidateFontBuilder(family_name="I82", style_name="Regular")

    res_both = builder.build_candidate_family(glyphs, tmp_path / "both")
    res_ttf = builder.build_candidate_family(glyphs, tmp_path / "ttf", formats=("TTF",))
    res_otf = builder.build_candidate_family(glyphs, tmp_path / "otf", formats=("OTF",))

    # Default behavior unchanged: both formats built.
    assert res_both.otf is not None and res_both.ttf is not None

    # TTF-only: no OTF artifact requested, produced, or left on disk.
    assert res_ttf.ttf is not None
    assert res_ttf.otf is None
    assert not list((tmp_path / "ttf").glob("*.otf"))

    # OTF-only: no TTF artifact requested, produced, or left on disk.
    assert res_otf.otf is not None
    assert res_otf.ttf is None
    assert not list((tmp_path / "otf").glob("*.ttf"))

    # Byte identity with the dual build (SAME MAX TRUTH).
    assert res_ttf.ttf.sha256_hex == res_both.ttf.sha256_hex
    assert res_ttf.ttf.size_bytes == res_both.ttf.size_bytes
    assert res_otf.otf.sha256_hex == res_both.otf.sha256_hex
    assert res_otf.otf.size_bytes == res_both.otf.size_bytes


def test_candidate_family_format_selection_rejects_invalid(tmp_path):
    glyphs = {65: _rect_glyph()}
    builder = MaxCandidateFontBuilder(family_name="I82", style_name="Regular")
    with pytest.raises(ValueError, match="CANDIDATE_BUILD_FORMATS_INVALID"):
        builder.build_candidate_family(glyphs, tmp_path / "bad", formats=("WOFF2",))
    with pytest.raises(ValueError, match="CANDIDATE_BUILD_FORMATS_INVALID"):
        builder.build_candidate_family(glyphs, tmp_path / "bad2", formats=())


# =========================================================================
# Change 1: identity-bound model-formation memo
# =========================================================================

@pytest.mark.asyncio
async def test_gate_memo_hit_skips_formation_only_and_preserves_truth(monkeypatch):
    opt_calls = _count(monkeypatch, FitOnlyGlyphOptimizer, "optimize")
    rec_calls = _count(monkeypatch, MaxReconstructionSolver, "reconstruct_glyph")
    eval_calls = _count(monkeypatch, FidelityEvaluator, "evaluate")
    otf_builds = _count(monkeypatch, MaxCandidateFontBuilder, "build_candidate_otf")
    ttf_builds = _count(monkeypatch, MaxCandidateFontBuilder, "build_candidate_ttf")
    from fidelity import release_gate as rg

    partition_calls = _count(monkeypatch, rg, "partition_snapshot")

    with tempfile.TemporaryDirectory() as store_dir, tempfile.TemporaryDirectory() as out_root:
        store, config, bv = await _seed_completed_store(Path(store_dir))
        out_root = Path(out_root)

        res_ttf = await Stage9DReleaseGate.execute(**_gate_kwargs(store, config, bv, "TTF", out_root / "ttf"))
        res_otf = await Stage9DReleaseGate.execute(**_gate_kwargs(store, config, bv, "OTF", out_root / "otf"))
        res_rep = await Stage9DReleaseGate.execute(**_gate_kwargs(store, config, bv, "TTF", out_root / "rep"))

        for res in (res_ttf, res_otf, res_rep):
            assert res.is_publishable, res.failure_reasons

        # Miss then hit+hit: formation stages ran exactly once.
        assert len(opt_calls) == 1
        assert len(rec_calls) == 2  # two fit code points, miss path only
        # Never skipped by the memo: partition, per-gate build, held-out eval.
        assert len(partition_calls) == 3
        assert len(eval_calls) == 3
        # Change 2 on gate paths: each gate builds exactly its format.
        assert len(otf_builds) == 1
        assert len(ttf_builds) == 2

        # SAME MAX TRUTH across miss and hit gates.
        assert res_otf.model_hash == res_ttf.model_hash == res_rep.model_hash
        assert res_rep.trace.compute_trace_hash() == res_ttf.trace.compute_trace_hash()
        assert res_rep.candidate_artifact_sha == res_ttf.candidate_artifact_sha
        assert res_rep.report_hash == res_ttf.report_hash
        assert res_rep.attestation.compute_hash() == res_ttf.attestation.compute_hash()
        assert res_otf.candidate_artifact_sha != res_ttf.candidate_artifact_sha

        for res in (res_ttf, res_otf, res_rep):
            res.cleanup()


@pytest.mark.asyncio
async def test_gate_memo_miss_on_optimizer_policy_change(monkeypatch):
    opt_calls = _count(monkeypatch, FitOnlyGlyphOptimizer, "optimize")
    with tempfile.TemporaryDirectory() as store_dir:
        store, config, bv = await _seed_completed_store(Path(store_dir))
        kwargs = dict(
            store=store,
            config=config,
            reference_id="stage9d_family",
            style_id="regular",
            family_name="Stage9DFamily",
            style_name="Regular",
            browser_version=bv,
            format_type="TTF",
        )
        res_a = await Stage9DReleaseGate.execute(**kwargs)
        res_b = await Stage9DReleaseGate.execute(
            optimizer_policy=OptimizerPolicy(convergence_tol=1e-8), **kwargs
        )
        assert res_a.is_publishable, res_a.failure_reasons
        assert res_b.is_publishable, res_b.failure_reasons
        # Optimizer policy identity change -> no reuse, full formation.
        assert len(opt_calls) == 2
        res_a.cleanup()
        res_b.cleanup()


@pytest.mark.asyncio
async def test_gate_memo_miss_on_snapshot_identity_change(monkeypatch):
    from measurement.models import ObservationConfig
    from tests.test_stage9d_release_gate import STAGE9D_CONFIG

    opt_calls = _count(monkeypatch, FitOnlyGlyphOptimizer, "optimize")
    config_b = ObservationConfig(
        resolutions=STAGE9D_CONFIG.resolutions,
        base_subpixel_phases=STAGE9D_CONFIG.base_subpixel_phases,
        expanded_subpixel_phases=STAGE9D_CONFIG.expanded_subpixel_phases,
        held_out_subpixel_phases=((0.5, 0.5),),
    )
    assert config_b.compute_hash() != STAGE9D_CONFIG.compute_hash()

    with tempfile.TemporaryDirectory() as dir_a, tempfile.TemporaryDirectory() as dir_b:
        store_a, cfg_a, bv_a = await _seed_completed_store(Path(dir_a))
        store_b, cfg_b, bv_b = await _seed_completed_store(Path(dir_b), config=config_b)
        res_a = await Stage9DReleaseGate.execute(**_gate_kwargs(store_a, cfg_a, bv_a, "TTF", None))
        res_b = await Stage9DReleaseGate.execute(**_gate_kwargs(store_b, cfg_b, bv_b, "TTF", None))
        assert res_a.is_publishable, res_a.failure_reasons
        assert res_b.is_publishable, res_b.failure_reasons
        assert res_a.snapshot_fingerprint != res_b.snapshot_fingerprint
        # Evidence identity change -> no cross-identity reuse.
        assert len(opt_calls) == 2
        res_a.cleanup()
        res_b.cleanup()


@pytest.mark.asyncio
async def test_gate_memo_drift_guard_discards_entry_and_recomputes(monkeypatch):
    opt_calls = _count(monkeypatch, FitOnlyGlyphOptimizer, "optimize")
    with tempfile.TemporaryDirectory() as store_dir:
        store, config, bv = await _seed_completed_store(Path(store_dir))
        kwargs = _gate_kwargs(store, config, bv, "TTF", None)
        res_a = await Stage9DReleaseGate.execute(**kwargs)
        assert res_a.is_publishable, res_a.failure_reasons
        assert len(opt_calls) == 1

        # Tamper with the memoized LIVE model: its canonical hash no longer
        # matches the sealed model hash, so the hit must fail closed.
        with Stage9DReleaseGate._formation_memo_lock:
            entry = next(iter(Stage9DReleaseGate._formation_memo.values()))
        glyph = entry.model.glyphs[65]
        glyph.advance_width_upem = glyph.advance_width_upem + 25.0

        res_b = await Stage9DReleaseGate.execute(**kwargs)
        assert res_b.is_publishable, res_b.failure_reasons
        # The drifted entry was discarded and never consumed.
        assert len(opt_calls) == 2
        assert res_b.model_hash == res_a.model_hash
        assert res_b.candidate_artifact_sha == res_a.candidate_artifact_sha
        res_a.cleanup()
        res_b.cleanup()


def test_formation_memo_lru_bounded():
    def dummy() -> _ModelFormationEntry:
        return _ModelFormationEntry(
            model=None, sealed=None, trace=None, calibrated_glyphs=(), kerning_map=()
        )

    Stage9DReleaseGate._formation_memo_put(("fp1", ()), dummy())
    Stage9DReleaseGate._formation_memo_put(("fp2", ()), dummy())
    assert Stage9DReleaseGate._formation_memo_get(("fp1", ())) is not None  # refresh
    Stage9DReleaseGate._formation_memo_put(("fp3", ()), dummy())  # evicts fp2
    assert Stage9DReleaseGate._formation_memo_get(("fp2", ())) is None
    assert Stage9DReleaseGate._formation_memo_get(("fp1", ())) is not None
    assert Stage9DReleaseGate._formation_memo_get(("fp3", ())) is not None
