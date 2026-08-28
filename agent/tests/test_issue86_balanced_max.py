"""Issue #86 Stage 16 BALANCED_MAX focused causal tests (LOCAL_ONLY, NO_LOOP).

The 12 ACCEPT causal tests plus versioned-policy unit coverage:

 1  confident BALANCED_MAX + all final gates PASS -> publish
 2  low/missing/invalid confidence -> FULL_MAX
 3  held-out fail/incomplete -> FULL_MAX
 4  consumer/attestation failure -> FULL_MAX
 5  FULL_MAX failure -> job failure
 6  rejected BALANCED_MAX artifact can never publish
 7  FULL_MAX reuses compatible intermediates without skipping canonical schedule
 8  TTF/OTF/repeat share the same accepted optimized FontModel
 9  checkpoint/resume skips completed compatible glyph work
10  stale/cross-identity cache/checkpoint entries fail closed
11  worker completion order cannot change final identity
12  held-out evidence cannot leak into fitting or confidence

Fixture substitution happens ONLY at the browser boundary (the canonical
test_issue75_fullmax_e2e fixture session); every production code path is
the real one.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from fidelity import release_gate as rg
from fidelity.balanced_search import (
    GLOBAL_INTERMEDIATE_CACHE,
    BalancedMaxSearch,
    GlyphCheckpointStore,
    IntermediateArtifactCache,
    glyph_payload_hash,
    reset_global_intermediate_cache,
)
from fidelity.optimizer import FitOnlyGlyphOptimizer, OptimizerPolicy
from fidelity.pipeline import ObservationStoreSnapshot, partition_snapshot
from fidelity.profiles import (
    BALANCED_MAX_PROFILE,
    CONFIDENCE_LOW,
    CONFIDENCE_PASS,
    FULL_MAX_PROFILE,
    PROFILE_BALANCED_MAX,
    PROFILE_FULL_MAX,
    ProfileConfidence,
    classify_glyph_difficulty,
    compute_profile_confidence,
    rerank_margin_accepts_top1,
)
from fidelity.release_gate import Stage9DReleaseGate
from tests.test_issue75_fullmax_e2e import (
    _E2EFixtureSession,
    _E2E_ORIGINAL_COVERAGE,
    _collect_family,
)


@pytest.fixture(scope="module")
def e2e_collection(tmp_path_factory):
    """Shared canonical MAX collection (ORIGINAL raster-only family)."""
    base_dir = tmp_path_factory.mktemp("issue86_balanced_collection")
    session = _E2EFixtureSession(_E2E_ORIGINAL_COVERAGE, "chromium_issue86_v1")
    store, config, bv = asyncio.run(
        _collect_family(base_dir, "e2e_fam", "E2EFam", session, _E2E_ORIGINAL_COVERAGE)
    )
    return base_dir, store, config, bv


@pytest.fixture(autouse=True)
def _clean_state():
    Stage9DReleaseGate._formation_memo_clear()
    reset_global_intermediate_cache()
    yield
    Stage9DReleaseGate._formation_memo_clear()
    reset_global_intermediate_cache()


def _gate_kwargs(store, config, bv, fmt="TTF", out=None):
    kw = dict(
        store=store,
        config=config,
        reference_id="e2e_fam",
        style_id="regular",
        family_name="E2EFam",
        style_name="Regular",
        browser_version=bv,
        format_type=fmt,
    )
    if out is not None:
        kw["output_dir"] = out
    return kw


# =========================================================================
# 1. confident BALANCED_MAX + all final gates PASS -> publish
# =========================================================================


def test_1_confident_balanced_max_publishes(e2e_collection, tmp_path):
    base_dir, store, config, bv = e2e_collection

    async def run():
        res = await Stage9DReleaseGate.execute(
            reconstruction_profile=BALANCED_MAX_PROFILE,
            **_gate_kwargs(store, config, bv, "TTF", tmp_path / "g1"),
        )
        assert res.is_publishable, res.failure_reasons
        assert res.status == "PASS"
        assert res.reconstruction_profile == PROFILE_BALANCED_MAX
        assert res.confidence_status == CONFIDENCE_PASS
        assert res.escalated_from_profile == ""
        assert res.attestation is not None
        assert res.trace is not None and res.trace.converged
        # The published artifact is a real on-disk file with attested bytes.
        art = Path(res.candidate_file_path)
        assert art.is_file() and res.candidate_size_bytes == art.stat().st_size
        assert "balanced_max_attempt" not in str(art)
        return res

    res = asyncio.run(run())
    res.cleanup()


# =========================================================================
# 2. low/missing/invalid confidence -> FULL_MAX
# =========================================================================


def test_2_low_confidence_escalates_to_full_max(e2e_collection, tmp_path, monkeypatch):
    base_dir, store, config, bv = e2e_collection

    def forced_low(profile, evidence):
        if profile.name == PROFILE_BALANCED_MAX:
            return ProfileConfidence(
                status=CONFIDENCE_LOW, score=0.0,
                reasons=("FORCED_LOW_FOR_CAUSAL_TEST",), per_glyph_min=0.0,
                budget_fraction=0.0,
            )
        return compute_profile_confidence.__wrapped__(profile, evidence) if hasattr(
            compute_profile_confidence, "__wrapped__"
        ) else ProfileConfidence(CONFIDENCE_PASS, 1.0, (), 1.0, 0.0)

    monkeypatch.setattr(rg, "compute_profile_confidence", forced_low)

    async def run():
        res = await Stage9DReleaseGate.execute(
            reconstruction_profile=BALANCED_MAX_PROFILE,
            **_gate_kwargs(store, config, bv, "TTF", tmp_path / "g2"),
        )
        assert res.is_publishable, res.failure_reasons
        # Published by the FULL_MAX fallback after exactly one escalation.
        assert res.reconstruction_profile == PROFILE_FULL_MAX
        assert res.escalated_from_profile == PROFILE_BALANCED_MAX
        assert "BALANCED_CONFIDENCE_LOW" in res.escalation_reason
        return res

    res = asyncio.run(run())
    res.cleanup()


def test_2b_confidence_low_on_missing_or_non_finite_inputs():
    good = {
        "code_point": 65,
        "full_objective": 0.1,
        "coverage_loss": 0.01,
        "edge_loss": 0.2,
        "sdf_loss": 0.001,
        "stop_reason": "CONVERGED",
        "margin": 0.01,
    }
    ok = compute_profile_confidence(BALANCED_MAX_PROFILE, [dict(good)])
    assert ok.status == CONFIDENCE_PASS

    non_finite = dict(good, full_objective=float("nan"))
    low = compute_profile_confidence(BALANCED_MAX_PROFILE, [dict(non_finite)])
    assert low.status == CONFIDENCE_LOW and low.score == 0.0

    missing = dict(good)
    del missing["edge_loss"]
    low2 = compute_profile_confidence(BALANCED_MAX_PROFILE, [dict(missing, edge_loss=None)])
    assert low2.status == CONFIDENCE_LOW

    empty = compute_profile_confidence(BALANCED_MAX_PROFILE, [])
    assert empty.status == CONFIDENCE_LOW

    # FULL_MAX reference profile never gates on confidence.
    full = compute_profile_confidence(FULL_MAX_PROFILE, [])
    assert full.status == CONFIDENCE_PASS


# =========================================================================
# 3. held-out fail/incomplete -> FULL_MAX
# =========================================================================


def test_3_held_out_failure_escalates_to_full_max(e2e_collection, tmp_path, monkeypatch):
    base_dir, store, config, bv = e2e_collection

    from fidelity import balanced_search as bs

    real_fit_glyph = BalancedMaxSearch._fit_glyph

    def degraded_fit_glyph(self, code_point, cp_records, raster_provider, units_per_em, cfg, identity):
        glyph, record = real_fit_glyph(
            self, code_point, cp_records, raster_provider, units_per_em, cfg, identity
        )
        # Deterministic causal degradation: shift geometry far outside the
        # held-out raster ink so the unchanged held-out geometry gate fails
        # for the BALANCED candidate only.
        from fidelity.optimizer import _transform_contours

        shifted = _transform_contours(glyph.contours, 120.0, 120.0, 1.0, 300.0, 300.0)
        from reconstruction.models import ReconstructedGlyph

        degraded = ReconstructedGlyph(
            code_point=glyph.code_point,
            character=glyph.character,
            advance_width_upem=glyph.advance_width_upem,
            lsb_upem=glyph.lsb_upem,
            rsb_upem=glyph.rsb_upem,
            ascent_upem=glyph.ascent_upem,
            descent_upem=glyph.descent_upem,
            contours=shifted,
            bounding_box_upem=glyph.bounding_box_upem,
            reconstruction_time_ms=0.0,
        )
        return degraded, record

    monkeypatch.setattr(BalancedMaxSearch, "_fit_glyph", degraded_fit_glyph)
    # Confidence must not pre-empt the held-out causal path: force PASS so
    # the candidate reaches the unchanged held-out gate.
    monkeypatch.setattr(
        rg, "compute_profile_confidence",
        lambda profile, evidence: ProfileConfidence(CONFIDENCE_PASS, 1.0, (), 1.0, 0.0),
    )

    async def run():
        res = await Stage9DReleaseGate.execute(
            reconstruction_profile=BALANCED_MAX_PROFILE,
            **_gate_kwargs(store, config, bv, "TTF", tmp_path / "g3"),
        )
        assert res.is_publishable, res.failure_reasons
        assert res.reconstruction_profile == PROFILE_FULL_MAX
        assert res.escalated_from_profile == PROFILE_BALANCED_MAX
        # Canonical unchanged gate order runs the four-consumer evidence
        # (held-out-based FreeType/HarfBuzz/Chromium gates) BEFORE the
        # evaluator geometry-raster gate; a 120-unit geometry shift is
        # therefore rejected fail-closed by the held-out consumer evidence
        # first. The causal contract is unchanged-final-gate rejection ->
        # single escalation to FULL_MAX.
        assert "FIDELITY_EVALUATION_FAILED" in res.escalation_reason
        return res

    res = asyncio.run(run())
    res.cleanup()


# =========================================================================
# 4. consumer/attestation failure -> FULL_MAX
# =========================================================================


def test_4_attestation_failure_escalates_to_full_max(e2e_collection, tmp_path, monkeypatch):
    base_dir, store, config, bv = e2e_collection

    from reconstruction import candidate_builder as cb

    real_build = cb.MaxCandidateFontBuilder.build_candidate_family
    state = {"corrupted": False}

    def corrupt_first_build(self, glyphs, output_dir, typography=None, formats=("TTF", "OTF")):
        result = real_build(self, glyphs, output_dir, typography=typography, formats=formats)
        if not state["corrupted"]:
            # Corrupt the BALANCED attempt's artifact bytes after the build:
            # descriptor attestation must fail closed for the candidate.
            state["corrupted"] = True
            art = result.ttf if result.ttf is not None else result.otf
            if art is not None and art.file_path:
                p = Path(art.file_path)
                data = bytearray(p.read_bytes())
                data[len(data) // 2] ^= 0xFF
                p.write_bytes(bytes(data))
        return result

    monkeypatch.setattr(cb.MaxCandidateFontBuilder, "build_candidate_family", corrupt_first_build)

    async def run():
        res = await Stage9DReleaseGate.execute(
            reconstruction_profile=BALANCED_MAX_PROFILE,
            **_gate_kwargs(store, config, bv, "TTF", tmp_path / "g4"),
        )
        assert res.is_publishable, res.failure_reasons
        assert res.reconstruction_profile == PROFILE_FULL_MAX
        assert res.escalated_from_profile == PROFILE_BALANCED_MAX
        assert "CANDIDATE_ATTESTATION_FAILED" in res.escalation_reason
        return res

    res = asyncio.run(run())
    res.cleanup()


# =========================================================================
# 5. FULL_MAX failure -> job failure (BALANCED never publishes either)
# =========================================================================


def test_5_full_max_failure_fails_job(e2e_collection, tmp_path):
    base_dir, store, config, bv = e2e_collection

    # Tamper one held-out raster on disk: BOTH profiles' unchanged held-out
    # gates must fail deterministically -> the job fails after at most one
    # escalation; nothing publishes.
    held_out = sorted(store.base_dir.rglob("*heldout*.png"))
    assert held_out, "expected held-out raster files in seeded store"
    target = held_out[0]
    original_bytes = target.read_bytes()
    data = bytearray(original_bytes)
    data[len(data) // 2] ^= 0xFF
    target.write_bytes(bytes(data))

    async def run():
        res = await Stage9DReleaseGate.execute(
            reconstruction_profile=BALANCED_MAX_PROFILE,
            **_gate_kwargs(store, config, bv, "TTF", tmp_path / "g5"),
        )
        assert not res.is_publishable
        assert res.reconstruction_profile == PROFILE_FULL_MAX
        assert res.escalated_from_profile == PROFILE_BALANCED_MAX
        assert res.candidate_artifact_sha == ""
        return res

    try:
        res = asyncio.run(run())
    finally:
        # Test isolation: the store is module-scoped and shared; restore
        # the sealed held-out raster so later tests see canonical bytes.
        target.write_bytes(original_bytes)
    res.cleanup()


# =========================================================================
# 6. rejected BALANCED_MAX artifact can never publish
# =========================================================================


def test_6_rejected_balanced_artifact_never_publishes(e2e_collection, tmp_path, monkeypatch):
    base_dir, store, config, bv = e2e_collection

    from fidelity import balanced_search as bs
    from reconstruction import candidate_builder as cb

    real_fit_glyph = BalancedMaxSearch._fit_glyph
    rejected_sha: dict[str, str] = {}

    def degraded_fit_glyph(self, code_point, cp_records, raster_provider, units_per_em, cfg, identity):
        glyph, record = real_fit_glyph(
            self, code_point, cp_records, raster_provider, units_per_em, cfg, identity
        )
        from fidelity.optimizer import _transform_contours
        from reconstruction.models import ReconstructedGlyph

        shifted = _transform_contours(glyph.contours, 120.0, 120.0, 1.0, 300.0, 300.0)
        return (
            ReconstructedGlyph(
                code_point=glyph.code_point, character=glyph.character,
                advance_width_upem=glyph.advance_width_upem, lsb_upem=glyph.lsb_upem,
                rsb_upem=glyph.rsb_upem, ascent_upem=glyph.ascent_upem,
                descent_upem=glyph.descent_upem, contours=shifted,
                bounding_box_upem=glyph.bounding_box_upem, reconstruction_time_ms=0.0,
            ),
            record,
        )

    real_build = cb.MaxCandidateFontBuilder.build_candidate_family

    def capture_balanced_sha(self, glyphs, output_dir, typography=None, formats=("TTF", "OTF")):
        result = real_build(self, glyphs, output_dir, typography=typography, formats=formats)
        if "balanced_max_attempt" in str(output_dir):
            art = result.ttf if result.ttf is not None else result.otf
            if art is not None:
                rejected_sha["sha"] = art.sha256_hex
                rejected_sha["path"] = art.file_path
        return result

    monkeypatch.setattr(BalancedMaxSearch, "_fit_glyph", degraded_fit_glyph)
    monkeypatch.setattr(cb.MaxCandidateFontBuilder, "build_candidate_family", capture_balanced_sha)
    monkeypatch.setattr(
        rg, "compute_profile_confidence",
        lambda profile, evidence: ProfileConfidence(CONFIDENCE_PASS, 1.0, (), 1.0, 0.0),
    )

    async def run():
        res = await Stage9DReleaseGate.execute(
            reconstruction_profile=BALANCED_MAX_PROFILE,
            **_gate_kwargs(store, config, bv, "TTF", tmp_path / "g6"),
        )
        assert res.is_publishable, res.failure_reasons
        assert "sha" in rejected_sha, "BALANCED attempt must have built a candidate"
        # The rejected BALANCED artifact is never the published artifact.
        assert res.candidate_artifact_sha != rejected_sha["sha"]
        assert "balanced_max_attempt" not in res.candidate_file_path
        # The published artifact is the FULL_MAX fallback's.
        assert res.reconstruction_profile == PROFILE_FULL_MAX
        return res

    res = asyncio.run(run())
    res.cleanup()


# =========================================================================
# 7. FULL_MAX reuses compatible intermediates without skipping schedule
# =========================================================================


def test_7_full_max_reuses_intermediates_not_schedule(e2e_collection, tmp_path, monkeypatch):
    base_dir, store, config, bv = e2e_collection

    # Force BALANCED to escalate so FULL_MAX runs AFTER BALANCED populated
    # the exact-identity intermediate cache.
    monkeypatch.setattr(
        rg, "compute_profile_confidence",
        lambda profile, evidence: (
            ProfileConfidence(CONFIDENCE_LOW, 0.0, ("FORCED",), 0.0, 0.0)
            if profile.name == PROFILE_BALANCED_MAX
            else ProfileConfidence(CONFIDENCE_PASS, 1.0, (), 1.0, 0.0)
        ),
    )

    async def run():
        # Reference: standalone canonical FULL_MAX truth.
        ref = await Stage9DReleaseGate.execute(
            reconstruction_profile=FULL_MAX_PROFILE,
            **_gate_kwargs(store, config, bv, "TTF", tmp_path / "ref"),
        )
        ref_hash = ref.model_hash
        ref.cleanup()

        from fidelity.balanced_search import GLOBAL_INTERMEDIATE_CACHE as cache

        before = cache.snapshot_stats()
        res = await Stage9DReleaseGate.execute(
            reconstruction_profile=BALANCED_MAX_PROFILE,
            **_gate_kwargs(store, config, bv, "TTF", tmp_path / "esc"),
        )
        after = cache.snapshot_stats()
        # Compatible intermediates were reused by the FULL_MAX fallback:
        # decode/prepare hits increased beyond the BALANCED pass's own
        # preparation misses.
        assert after["prepare_hits"] + after["decode_hits"] > before["prepare_hits"] + before["decode_hits"]
        # Canonical schedule NOT skipped: FULL_MAX truth identical to the
        # standalone canonical run (complete fit evidence still consumed).
        assert res.reconstruction_profile == PROFILE_FULL_MAX
        assert res.model_hash == ref_hash, "FULL_MAX fallback must reproduce canonical truth"
        return res

    res = asyncio.run(run())
    res.cleanup()


# =========================================================================
# 8. TTF/OTF/repeat share the same accepted optimized FontModel
# =========================================================================


def test_8_ttf_otf_repeat_share_one_model(e2e_collection, tmp_path):
    base_dir, store, config, bv = e2e_collection

    async def run():
        kw = _gate_kwargs(store, config, bv)
        kw.pop("format_type", None)
        res_ttf = await Stage9DReleaseGate.execute(
            reconstruction_profile=BALANCED_MAX_PROFILE, format_type="TTF",
            output_dir=tmp_path / "ttf", **kw
        )
        res_otf = await Stage9DReleaseGate.execute(
            reconstruction_profile=BALANCED_MAX_PROFILE, format_type="OTF",
            output_dir=tmp_path / "otf", **kw
        )
        res_rep = await Stage9DReleaseGate.execute(
            reconstruction_profile=BALANCED_MAX_PROFILE, format_type="TTF",
            output_dir=tmp_path / "rep", **kw
        )
        for r in (res_ttf, res_otf, res_rep):
            assert r.is_publishable, r.failure_reasons
        # One accepted optimized FontModel shared across TTF/OTF/repeat.
        assert res_ttf.model_hash == res_otf.model_hash == res_rep.model_hash != ""
        assert res_ttf.reconstruction_profile == PROFILE_BALANCED_MAX
        # Exact deterministic repeat is byte-identical.
        assert res_rep.candidate_artifact_sha == res_ttf.candidate_artifact_sha
        assert res_otf.candidate_artifact_sha != res_ttf.candidate_artifact_sha
        return res_ttf, res_otf, res_rep

    results = asyncio.run(run())
    for r in results:
        r.cleanup()


# =========================================================================
# 9. checkpoint/resume skips completed compatible glyph work
# =========================================================================


def test_9_checkpoint_resume_skips_completed_glyphs(e2e_collection, tmp_path, monkeypatch):
    base_dir, store, config, bv = e2e_collection
    from reconstruction import solver as solver_mod

    recon_calls = []
    real_reconstruct = solver_mod.MaxReconstructionSolver.reconstruct_glyph

    def counting_reconstruct(self, observations):
        recon_calls.append(1)
        return real_reconstruct(self, observations)

    monkeypatch.setattr(solver_mod.MaxReconstructionSolver, "reconstruct_glyph", counting_reconstruct)

    out = tmp_path / "resume"

    async def run():
        kw = _gate_kwargs(store, config, bv, "TTF", out)
        first = await Stage9DReleaseGate.execute(reconstruction_profile=BALANCED_MAX_PROFILE, **kw)
        assert first.is_publishable, first.failure_reasons
        first_recon = len(recon_calls)
        assert first_recon > 0
        first_hash = first.model_hash
        first.cleanup()

        # Drop the in-process formation memo so the second gate must
        # re-form; the on-disk identity-bound checkpoints must now skip
        # every completed compatible glyph.
        Stage9DReleaseGate._formation_memo_clear()
        recon_calls.clear()
        second = await Stage9DReleaseGate.execute(reconstruction_profile=BALANCED_MAX_PROFILE, **kw)
        assert second.is_publishable, second.failure_reasons
        resumed_recon = len(recon_calls)
        # Resume: completed glyph work skipped (no reconstruction reruns).
        assert resumed_recon == 0, f"expected 0 reconstructions on resume, got {resumed_recon}"
        assert second.model_hash == first_hash
        summary = dict(second.search_summary)
        assert summary.get("checkpoint_resumed_glyphs") == summary.get("glyph_count")
        return second

    res = asyncio.run(run())
    res.cleanup()


# =========================================================================
# 10. stale/cross-identity cache/checkpoint entries fail closed
# =========================================================================


def test_10_stale_cross_identity_entries_fail_closed(tmp_path):
    # --- Checkpoint store: cross-identity and tampered entries fail closed.
    from fidelity.balanced_search import CheckpointIdentity

    profile = BALANCED_MAX_PROFILE
    store = GlyphCheckpointStore(tmp_path / "ckpt", profile)
    identity_a = CheckpointIdentity("snapA", "fitA", profile.policy_hash(), "v1")
    identity_b = CheckpointIdentity("snapB", "fitB", profile.policy_hash(), "v1")

    store.save(65, "glyph", identity_a, {"glyph": {"code_point": 65}, "record": {}})
    # Same identity -> hit.
    assert store.load(65, "glyph", identity_a) is not None
    # Cross-identity -> fail closed (miss + discard).
    assert store.load(65, "glyph", identity_b) is None
    assert store.stats["invalid_discarded"] >= 1

    # Tampered payload hash -> fail closed.
    store.save(66, "glyph", identity_a, {"glyph": {"code_point": 66}, "record": {}})
    path = store._path(66, "glyph")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["payload_hash"] = "f" * 64
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert store.load(66, "glyph", identity_a) is None

    # --- Intermediate cache: entries are keyed by complete truth identity,
    # so a cross-identity request can never hit another observation's entry.
    cache = IntermediateArtifactCache()
    key_x = cache.prepare_key("obs_key_x", "sha_x", 512)
    key_y = cache.prepare_key("obs_key_y", "sha_y", 512)
    cache.put_prepared(key_x, {"crop": (0, 1, 0, 1)})
    assert cache.get_prepared(key_x) is not None
    assert cache.get_prepared(key_y) is None  # cross-identity never reuses


# =========================================================================
# 11. worker completion order cannot change final identity
# =========================================================================


def test_11_worker_order_does_not_change_identity(e2e_collection):
    base_dir, store, config, bv = e2e_collection

    async def run():
        snap = ObservationStoreSnapshot.load_from_store(
            store=store, reference_id="e2e_fam", style_id="regular",
            family_name="E2EFam", style_name="Regular", config=config, browser_version=bv,
        )
        part = partition_snapshot(snap)
        rp = lambda r: snap.get_raster_bytes(r.cache_key)

        hashes = []
        for workers in (1, 2, 4):
            search = BalancedMaxSearch(BALANCED_MAX_PROFILE)
            result = search.form_model(snap, part, config, rp, workers=workers)
            fingerprint = result.trace.compute_trace_hash()
            glyph_hashes = tuple(
                glyph_payload_hash(result.optimized_glyphs[cp])
                for cp in sorted(result.optimized_glyphs)
            )
            hashes.append((fingerprint, glyph_hashes))
        # Completion order varies with worker count; the assembled identity
        # (trace hash + every glyph payload hash) must be identical.
        assert hashes[0] == hashes[1] == hashes[2]

    asyncio.run(run())


# =========================================================================
# 12. held-out evidence cannot leak into fitting or confidence
# =========================================================================


def test_12_held_out_cannot_leak_into_fit_or_confidence(e2e_collection):
    base_dir, store, config, bv = e2e_collection

    async def run():
        snap = ObservationStoreSnapshot.load_from_store(
            store=store, reference_id="e2e_fam", style_id="regular",
            family_name="E2EFam", style_name="Regular", config=config, browser_version=bv,
        )
        part = partition_snapshot(snap)
        fit_keys = {r.cache_key for r in part.fit_records}
        held_out_keys = {r.cache_key for r in part.held_out_records}
        assert fit_keys and held_out_keys and not (fit_keys & held_out_keys)

        accessed = set()

        def instrumented(record):
            accessed.add(record.cache_key)
            return snap.get_raster_bytes(record.cache_key)

        search = BalancedMaxSearch(BALANCED_MAX_PROFILE)
        result = search.form_model(snap, part, config, instrumented)
        # Fitting touched fit evidence only; never any held-out raster.
        assert accessed <= fit_keys
        assert not (accessed & held_out_keys)
        # Confidence evidence is derived from the search records only and
        # references no held-out identity.
        for ev in result.confidence_evidence:
            assert set(ev) == {
                "code_point", "full_objective", "coverage_loss", "edge_loss",
                "sdf_loss", "stop_reason", "margin",
            }

    asyncio.run(run())


# =========================================================================
# Versioned policy unit coverage
# =========================================================================


def test_policy_hashes_are_deterministic_and_distinct():
    assert FULL_MAX_PROFILE.policy_hash() == FULL_MAX_PROFILE.policy_hash()
    assert BALANCED_MAX_PROFILE.policy_hash() == BALANCED_MAX_PROFILE.policy_hash()
    assert FULL_MAX_PROFILE.policy_hash() != BALANCED_MAX_PROFILE.policy_hash()
    assert BALANCED_MAX_PROFILE.name == "BALANCED_MAX"
    assert FULL_MAX_PROFILE.name == "FULL_MAX"


def test_core_resolution_and_phase_selection():
    max_res = (256, 512, 768, 1024, 1536, 2048, 3072, 4096)
    # FULL_MAX keeps the complete canonical schedule.
    assert FULL_MAX_PROFILE.select_core_resolutions(max_res) == max_res
    # BALANCED_MAX reduces to the versioned core fit resolutions.
    assert BALANCED_MAX_PROFILE.select_core_resolutions(max_res) == (512, 1024, 2048)
    # Small config with no versioned core member clamps deterministically.
    assert BALANCED_MAX_PROFILE.select_core_resolutions((128, 256)) == (256,)

    base_phases = tuple((x, y) for x in (0.0, 0.25, 0.5, 0.75) for y in (0.0, 0.25, 0.5, 0.75))
    assert FULL_MAX_PROFILE.select_easy_phases(base_phases) == base_phases
    easy = BALANCED_MAX_PROFILE.select_easy_phases(base_phases)
    assert easy == ((0.0, 0.0), (0.0, 0.5), (0.5, 0.0), (0.5, 0.5))
    # Config without a subset member falls back to the smallest base phase.
    assert BALANCED_MAX_PROFILE.select_easy_phases(((0.25, 0.25),)) == ((0.25, 0.25),)


def test_hard_classification_defaults():
    class _Cfg:
        base_subpixel_phases = ((0.0, 0.0),)
        expanded_subpixel_phases = ((0.0, 0.0), (0.25, 0.0))

        def get_phases_for_metrics(self, m):
            return self.expanded_subpixel_phases

    class _M:
        pass

    # Vietnamese combining mark defaults HARD.
    cls, reasons = classify_glyph_difficulty(0x0300, _M(), _Cfg(), BALANCED_MAX_PROFILE)
    assert cls == "HARD" and "COMBINING_MARK" in reasons
    # Multi-component composite defaults HARD.
    cls2, reasons2 = classify_glyph_difficulty(
        0x0041, _M(), _Cfg(), BALANCED_MAX_PROFILE, coarse_outer_contours=2
    )
    assert cls2 == "HARD" and "SYNTHESIZED_COMPOSITE" in reasons2
    # Budget-exhausted coarse convergence promotes HARD.
    cls3, reasons3 = classify_glyph_difficulty(
        0x0041, None, None, BALANCED_MAX_PROFILE, coarse_stop_reason="ITERATION_BUDGET_EXHAUSTED"
    )
    assert cls3 == "HARD" and "UNSTABLE_COARSE_CONVERGENCE" in reasons3


def test_rerank_margin_top1_rule():
    # A competitive pool (>=2) always satisfies the rerank.
    assert rerank_margin_accepts_top1(BALANCED_MAX_PROFILE, 2, 0.9) is True
    # Single candidate must sit inside the frozen top-1 bound.
    bound = BALANCED_MAX_PROFILE.confidence_thresholds.top1_full_objective
    assert rerank_margin_accepts_top1(BALANCED_MAX_PROFILE, 1, bound - 0.01) is True
    assert rerank_margin_accepts_top1(BALANCED_MAX_PROFILE, 1, bound + 0.01) is False
    assert rerank_margin_accepts_top1(BALANCED_MAX_PROFILE, 0, 0.0) is False


# =========================================================================
# 13. BALANCED_MAX production-path E2E: AI-VIETNAMESE missing-coverage case
# =========================================================================


@pytest.fixture(scope="module")
def e2e_vi_collection86(tmp_path_factory):
    """Shared canonical MAX collection for the BALANCED VIETNAMESE chain."""
    from tests.test_issue75_fullmax_e2e import _E2E_VI_COVERAGE

    base_dir = tmp_path_factory.mktemp("issue86_balanced_vi_collection")
    session = _E2EFixtureSession(_E2E_VI_COVERAGE, "chromium_issue86_vi_v1")
    store, config, bv = asyncio.run(
        _collect_family(base_dir, "e2e_vi_fam", "E2EViFam", session, _E2E_VI_COVERAGE)
    )
    return base_dir, store, config, bv


def test_13_balanced_vi_production_path_publishes(e2e_vi_collection86, tmp_path):
    """BALANCED_MAX production-path E2E (AI-VIETNAMESE): the missing-coverage
    Vietnamese chain publishes under the BALANCED_MAX profile with the
    deterministic confidence gate, deterministic-first extension, and every
    unchanged final gate; no escalation needed for the reference fixture."""
    import hashlib

    from compute.vietnamese import (
        MARK_CODEPOINT_SET,
        VIETNAMESE_REQUIRED_CODEPOINTS,
        VietnameseExtensionService,
    )
    from fidelity.profiles import PROFILE_BALANCED_MAX
    from fidelity.release_gate import PROVENANCE_STAGE9D_RASTER

    from tests.test_issue75_fullmax_e2e import _E2E_VI_COVERAGE

    base_dir, store, config, bv = e2e_vi_collection86

    class _E2EAIProvider:
        """Deterministic fake transport: closed schema, finite geometry."""

        model_id = "openrouter"
        model_version = "openrouter-route-v1"

        def __init__(self):
            self.calls = 0
            self.requested: list[int] = []

        def prompt_hash(self) -> str:
            return hashlib.sha256(b"e2e_prompt").hexdigest()

        async def generate_candidates(self, request):
            from compute.vietnamese import AICandidateSpec

            self.calls += 1
            self.requested = list(request["missing_codepoints"])
            specs = []
            for cp in self.requested:
                anchors = (("mark", 250.0, 320.0),) if cp in MARK_CODEPOINT_SET else ()
                specs.append(
                    AICandidateSpec(
                        code_point=cp,
                        contours=(((175.0, 100.0), (425.0, 100.0), (425.0, 340.0), (175.0, 340.0)),),
                        advance_width_upem=1.0 if cp in MARK_CODEPOINT_SET else 600.0,
                        lsb_upem=175.0,
                        rsb_upem=175.0,
                        ascent_upem=340.0,
                        descent_upem=-100.0,
                        anchors=anchors,
                    )
                )
            return specs

    provider = _E2EAIProvider()
    service = VietnameseExtensionService(
        provider, config_hash=config.compute_hash(), source_hash="e" * 64
    )

    async def run():
        res = await Stage9DReleaseGate.execute(
            store=store,
            config=config,
            reference_id="e2e_vi_fam",
            style_id="regular",
            family_name="E2EViFam",
            style_name="Regular",
            browser_version=bv,
            format_type="TTF",
            output_dir=tmp_path / "vi_balanced",
            mode="VIETNAMESE",
            vietnamese_service=service,
            reconstruction_profile=BALANCED_MAX_PROFILE,
        )
        assert res.is_publishable, res.failure_reasons
        # Published by BALANCED_MAX itself: deterministic confidence PASS,
        # no escalation, unchanged final gates decided publication.
        assert res.reconstruction_profile == PROFILE_BALANCED_MAX
        assert res.confidence_status == CONFIDENCE_PASS
        assert res.escalated_from_profile == ""
        assert res.attestation is not None
        assert res.attestation.overall_status == "PASS"
        assert res.attestation.ai_binding
        assert res.attestation.provenance != PROVENANCE_STAGE9D_RASTER
        # Deterministic-first extension invariant preserved under BALANCED.
        assert provider.calls == 1
        existing = set(_E2E_VI_COVERAGE)
        assert not (set(provider.requested) & existing)
        missing_all = {cp for cp in VIETNAMESE_REQUIRED_CODEPOINTS if cp not in existing}
        deterministic = missing_all - set(provider.requested)
        assert len(deterministic) > 0
        assert set(provider.requested) == missing_all - deterministic
        for cp in existing:
            assert cp in res.model.glyphs
        # Search ladder evidence: the ladder forms exactly the browser-fit
        # base coverage (extended glyphs are constructed by the deterministic
        # extension path, not the fit ladder); HARD defaults cover the
        # fixture's marks/composites, zero-ink space stays the trivial EASY.
        summary = dict(res.search_summary)
        assert summary["glyph_count"] == len(_E2E_VI_COVERAGE)
        assert len(res.model.glyphs) > summary["glyph_count"]
        assert summary["hard_glyphs"] >= 1
        assert summary["easy_glyphs"] >= 1
        return res

    res = asyncio.run(run())
    res.cleanup()