"""T-FAST30-01 focused causal tests: FAST_30 sole production profile (ADR-0001).

Fail-closed retirement guarantees (LOCAL_ONLY, NO_LOOP):
1. select_production_profile: FAST_30 is the sole resolvable production
   profile; retired names fail closed with PROFILE_RETIRED; unknown names
   fail closed with PROFILE_UNKNOWN.
2. Gate-level selection of a retired profile (name or object) fails closed
   with PROFILE_RETIRED before any pipeline work.
3. FAST_30 is the sole default through the production gate and publishes
   with the unchanged final gates; no escalation record exists (A2).
4. Wall-limit halt: a FAST_30 deadline overrun returns
   FAST30_FAILED: WALL_LIMIT_EXCEEDED and never publishes (A3).
5. Reuse precedence unchanged: exact-identity formation reuse still wins
   immediately on the second identical gate call (A5).
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from fidelity.balanced_search import BalancedMaxSearch
from fidelity.profiles import (
    FAST_30_PROFILE,
    PROFILE_BALANCED_MAX,
    PROFILE_FAST_30,
    PROFILE_FULL_MAX,
    RETIRED_PROFILES,
    ConfidenceThresholds,
    ReconstructionProfile,
    select_production_profile,
)
from fidelity.release_gate import Stage9DReleaseGate
from measurement.store import ObservationStore
from tests.test_stage9d_release_gate import STAGE9D_CONFIG, _seed_completed_store


def _retired_profile_object(name: str) -> ReconstructionProfile:
    """Ad-hoc object carrying a retired profile NAME (selection must fail
    closed on the name; the object contents never matter)."""
    return ReconstructionProfile(
        name=name,
        profile_version="0.0.0-retired",
        core_fit_resolutions=(),
        easy_phase_axes=(),
        coarse_max_iterations=1,
        tier_max_iterations=1,
        final_max_iterations=1,
        early_stop_abs_tol=0.0,
        early_stop_rel_tol=0.0,
        early_stop_rounds=0,
        rerank_top_k=1,
        workers_default=1,
        confidence_thresholds=ConfidenceThresholds(),
        calibration_corpus_identity="retired",
        calibration_policy_version="0.0.0",
    )


def _gate_kwargs(store, fmt: str = "TTF") -> dict:
    return dict(
        store=store,
        config=STAGE9D_CONFIG,
        reference_id="stage9d_family",
        style_id="regular",
        family_name="Stage9DFamily",
        style_name="Regular",
        browser_version="chromium_fast30_test",
        format_type=fmt,
    )


# =========================================================================
# 1. Policy-layer selection: FAST_30 sole, retired fail-closed
# =========================================================================


def test_select_production_profile_fast30_sole():
    assert select_production_profile(None) is FAST_30_PROFILE
    assert select_production_profile("FAST_30") is FAST_30_PROFILE
    assert select_production_profile("fast_30") is FAST_30_PROFILE
    assert select_production_profile(FAST_30_PROFILE) is FAST_30_PROFILE
    assert PROFILE_FAST_30 == "FAST_30"
    assert FAST_30_PROFILE.name == PROFILE_FAST_30
    assert FAST_30_PROFILE.wall_limit_seconds == 1800.0  # 30-minute wall
    assert RETIRED_PROFILES == frozenset({PROFILE_BALANCED_MAX, PROFILE_FULL_MAX})


def test_select_retired_profile_fails_closed_with_profile_retired():
    for name in ("BALANCED_MAX", "FULL_MAX", "balanced_max", "full_max"):
        with pytest.raises(ValueError, match=f"PROFILE_RETIRED: {name.upper()}"):
            select_production_profile(name)
    for retired in (PROFILE_BALANCED_MAX, PROFILE_FULL_MAX):
        with pytest.raises(ValueError, match="PROFILE_RETIRED"):
            select_production_profile(_retired_profile_object(retired))
    # Never a silent substitution: unknown names fail closed too.
    with pytest.raises(ValueError, match="PROFILE_UNKNOWN: MYSTERY"):
        select_production_profile("MYSTERY")


def test_ladder_guard_rejects_retired_profiles():
    with pytest.raises(ValueError, match="PROFILE_RETIRED: BALANCED_MAX"):
        BalancedMaxSearch(_retired_profile_object(PROFILE_BALANCED_MAX))
    with pytest.raises(ValueError, match="PROFILE_RETIRED: FULL_MAX"):
        BalancedMaxSearch(_retired_profile_object(PROFILE_FULL_MAX))


# =========================================================================
# 2. Gate-level retired selection fails closed (no work, no publish)
# =========================================================================


@pytest.mark.asyncio
async def test_gate_retired_profile_selection_fails_closed():
    with tempfile.TemporaryDirectory() as store_dir:
        store = ObservationStore(Path(store_dir))
        for retired in ("BALANCED_MAX", "FULL_MAX"):
            result = await Stage9DReleaseGate.execute(
                reconstruction_profile=retired, **_gate_kwargs(store)
            )
            assert result.is_publishable is False
            assert result.status == "FAIL"
            assert result.failure_reasons == (f"PROFILE_RETIRED: {retired}",)
            assert result.attestation is None
            assert result.candidate_file_path == ""
        # Ad-hoc objects carrying retired names fail closed identically.
        result = await Stage9DReleaseGate.execute(
            reconstruction_profile=_retired_profile_object(PROFILE_FULL_MAX),
            **_gate_kwargs(store),
        )
        assert result.is_publishable is False
        assert result.failure_reasons == ("PROFILE_RETIRED: FULL_MAX",)


# =========================================================================
# 3. FAST_30 sole default through the production gate
# =========================================================================


@pytest.mark.asyncio
async def test_gate_fast30_is_sole_default_and_publishes():
    with tempfile.TemporaryDirectory() as store_dir, tempfile.TemporaryDirectory() as out_dir:
        store, config, bv = await _seed_completed_store(Path(store_dir))
        result = await Stage9DReleaseGate.execute(
            store=store,
            config=config,
            reference_id="stage9d_family",
            style_id="regular",
            family_name="Stage9DFamily",
            style_name="Regular",
            browser_version=bv,
            format_type="TTF",
            output_dir=out_dir,
        )
        assert result.is_publishable is True
        assert result.status == "PASS"
        assert result.reconstruction_profile == PROFILE_FAST_30
        assert result.trace is not None and result.trace.converged is True
        # The retired regime's escalation record is structurally gone (A2):
        # no fallback/escalation path exists for any trigger type.
        assert not hasattr(result, "escalated_from_profile")
        assert not hasattr(result, "escalation_reason")
        result.cleanup()


# =========================================================================
# 4. Wall-limit halt: FAST30_FAILED, never publishes
# =========================================================================


@pytest.mark.asyncio
async def test_fast30_wall_limit_halt_fails_closed():
    with tempfile.TemporaryDirectory() as store_dir:
        store = ObservationStore(Path(store_dir))
        # Zero-second wall: the deadline is already crossed before any
        # pipeline stage; the wall halt precedes every other outcome.
        result = await Stage9DReleaseGate.execute(
            wall_limit_seconds=0.0, **_gate_kwargs(store)
        )
        assert result.is_publishable is False
        assert result.status == "FAIL"
        assert result.failure_reasons == ("FAST30_FAILED: WALL_LIMIT_EXCEEDED",)
        assert result.attestation is None
        assert result.candidate_file_path == ""


# =========================================================================
# 5. Reuse precedence unchanged under FAST_30
# =========================================================================


@pytest.mark.asyncio
async def test_reuse_precedence_unchanged_under_fast30(monkeypatch):
    """Exact-identity reuse still wins immediately: the second identical
    gate call reuses the sealed FAST_30 formation (one ladder formation
    total) while every unchanged final gate still runs per call, and the
    published artifact bytes stay identical."""
    calls: list[int] = []
    real_form_model = BalancedMaxSearch.form_model

    def counting_form_model(self, *args, **kwargs):
        calls.append(1)
        return real_form_model(self, *args, **kwargs)

    monkeypatch.setattr(BalancedMaxSearch, "form_model", counting_form_model)
    Stage9DReleaseGate._formation_memo_clear()
    try:
        with tempfile.TemporaryDirectory() as store_dir, \
                tempfile.TemporaryDirectory() as out_a, \
                tempfile.TemporaryDirectory() as out_b:
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
            res_a = await Stage9DReleaseGate.execute(output_dir=out_a, **kwargs)
            res_b = await Stage9DReleaseGate.execute(output_dir=out_b, **kwargs)
            assert res_a.is_publishable, res_a.failure_reasons
            assert res_b.is_publishable, res_b.failure_reasons
            # Reuse wins immediately on the second call.
            assert len(calls) == 1
            assert res_b.model_hash == res_a.model_hash
            assert res_b.candidate_artifact_sha == res_a.candidate_artifact_sha
            assert res_b.report_hash == res_a.report_hash
            res_a.cleanup()
            res_b.cleanup()
    finally:
        Stage9DReleaseGate._formation_memo_clear()
