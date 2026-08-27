"""Issue #75 FULL MAX RECONSTRUCTION PROFILE conformance pack.

Named adversarial coverage: MAX_SCHEDULE_EXACT, LOSS_VECTOR_COMPLETE,
DISCOVERY_TERMINATION, PROVIDER_AXIS_FORGERY (closed-set re-assertion),
HELDOUT_SEALED schedule disjointness, and closed feature-probe identity.
Items FONTMODEL_SINGLE_SOURCE, AI_ROUTING_CLOSED, OUTPUT_FORMAT_CLOSED,
FOUR_CONSUMER_IDENTITY, PORTABLE_CORE, REPEAT_EXACT, TYPOGRAPHY_CAUSAL are
enforced by the merged Stage 9A-9D / issue71 / issue72 packs cited in the
Issue #75 audit matrix.
"""
from __future__ import annotations

import asyncio
import math

import numpy as np
import pytest

from acquisition.capability import (
    FIXED_PHASE,
    PROVIDER_MONOTYPE_RENDER,
    PROVIDER_PLAYWRIGHT_STEALTH,
    ProviderRasterCapability,
    resolve_raster_provider,
)
from acquisition.models import SpriteRasterPage
from fidelity.optimizer import (
    OPTIMIZATION_LOSS_WEIGHTS,
    REQUIRED_OPTIMIZATION_LOSSES,
    FitOnlyGlyphOptimizer,
    GlyphOptimizationRecord,
    validate_loss_vector_complete,
)
from measurement.calibration import CalibrationTransform
from measurement.discovery import ObservableGlyphDiscovery
from measurement.max_profile import (
    MAX_BROWSER_PHASES_4X4,
    MAX_CORE_RASTER_SIZES_PX,
    MAX_FEATURE_PROBES,
    MAX_FEATURE_PROBE_SAMPLES,
    MAX_FEATURE_PROBE_TAGS,
    MAX_HARD_PHASES_8X8,
    MAX_HELDOUT_PHASES,
    MAX_HELDOUT_PHASE_GRID,
    MAX_HELDOUT_SIZES_PX,
    MAX_METRIC_SIZES_PX,
    MAX_RASTER_SIZES_PX,
    max_schedule_identity_hash,
    validate_max_schedule,
    validate_observation_config_max,
)
from measurement.models import ObservationConfig
from reconstruction.models import Contour, LineSegment, Point2D


def _canonical_schedule_kwargs(**overrides):
    kwargs = dict(
        metric_sizes=MAX_METRIC_SIZES_PX,
        raster_sizes=MAX_RASTER_SIZES_PX,
        core_sizes=MAX_CORE_RASTER_SIZES_PX,
        heldout_sizes=MAX_HELDOUT_SIZES_PX,
        fit_phases=MAX_BROWSER_PHASES_4X4,
        hard_phases=MAX_HARD_PHASES_8X8,
        heldout_phases=MAX_HELDOUT_PHASES,
        feature_probe_tags=MAX_FEATURE_PROBE_TAGS,
    )
    kwargs.update(overrides)
    return kwargs


# =========================================================================
# MAX_SCHEDULE_EXACT
# =========================================================================


def test_MAX_SCHEDULE_EXACT_canonical_accepted():
    validate_max_schedule(**_canonical_schedule_kwargs())
    # Deterministic closed identity.
    assert max_schedule_identity_hash() == max_schedule_identity_hash()
    config = ObservationConfig.max_profile()
    validate_observation_config_max(config)


def test_MAX_SCHEDULE_EXACT_missing_extra_duplicate_wrong_rejected():
    # Missing metric size.
    with pytest.raises(ValueError, match="MAX_SCHEDULE_MISMATCH:METRIC_SIZES"):
        validate_max_schedule(**_canonical_schedule_kwargs(metric_sizes=MAX_METRIC_SIZES_PX[1:]))
    # Extra raster size.
    with pytest.raises(ValueError, match="MAX_SCHEDULE_MISMATCH:RASTER_SIZES"):
        validate_max_schedule(**_canonical_schedule_kwargs(raster_sizes=MAX_RASTER_SIZES_PX + (8192,)))
    # Duplicate held-out size.
    with pytest.raises(ValueError, match="MAX_SCHEDULE_DUPLICATE:HELDOUT_SIZES"):
        validate_max_schedule(
            **_canonical_schedule_kwargs(heldout_sizes=(144, 144) + MAX_HELDOUT_SIZES_PX[1:])
        )
    # Wrong (reordered) metric schedule.
    with pytest.raises(ValueError, match="MAX_SCHEDULE_MISMATCH:METRIC_SIZES"):
        validate_max_schedule(**_canonical_schedule_kwargs(metric_sizes=tuple(reversed(MAX_METRIC_SIZES_PX))))
    # Wrong core subset: a core schedule that is not a subset of the raster
    # schedule rejects via the subset invariant (checked first); a subset that
    # merely differs from the canonical core rejects via exact closure.
    with pytest.raises(ValueError, match="MAX_SCHEDULE_CORE_NOT_SUBSET"):
        validate_max_schedule(**_canonical_schedule_kwargs(core_sizes=(128, 256)))
    with pytest.raises(ValueError, match="MAX_SCHEDULE_MISMATCH:CORE_RASTER_SIZES"):
        validate_max_schedule(**_canonical_schedule_kwargs(core_sizes=(512, 1024, 2048)))
    # Missing phase.
    with pytest.raises(ValueError, match="MAX_SCHEDULE_MISMATCH:FIT_PHASES"):
        validate_max_schedule(**_canonical_schedule_kwargs(fit_phases=MAX_BROWSER_PHASES_4X4[1:]))
    # Extra hard phase.
    with pytest.raises(ValueError, match="MAX_SCHEDULE_MISMATCH:HARD_PHASES"):
        validate_max_schedule(**_canonical_schedule_kwargs(hard_phases=MAX_HARD_PHASES_8X8 + ((0.9375, 0.0),)))
    # Wrong held-out phase set.
    with pytest.raises(ValueError, match="MAX_SCHEDULE_MISMATCH:HELDOUT_PHASES"):
        validate_max_schedule(**_canonical_schedule_kwargs(heldout_phases=MAX_HELDOUT_PHASES[1:]))
    # Missing feature tag.
    with pytest.raises(ValueError, match="MAX_SCHEDULE_MISMATCH:FEATURE_PROBE_TAGS"):
        validate_max_schedule(**_canonical_schedule_kwargs(feature_probe_tags=MAX_FEATURE_PROBE_TAGS[:-1]))
    # Extra feature tag.
    with pytest.raises(ValueError, match="MAX_SCHEDULE_MISMATCH:FEATURE_PROBE_TAGS"):
        validate_max_schedule(
            **_canonical_schedule_kwargs(feature_probe_tags=MAX_FEATURE_PROBE_TAGS + ("ss21",))
        )


def test_MAX_SCHEDULE_EXACT_wrong_core_and_heldout_substitution_rejected():
    # Substituting a fit raster size into the held-out schedule is both an
    # exact-schedule mismatch and a held-out overlap; the closed validator
    # rejects it fail-closed.
    with pytest.raises(ValueError, match="MAX_SCHEDULE"):
        validate_max_schedule(
            **_canonical_schedule_kwargs(heldout_sizes=(256,) + MAX_HELDOUT_SIZES_PX[1:])
        )
    with pytest.raises(ValueError, match="MAX_SCHEDULE"):
        validate_max_schedule(
            **_canonical_schedule_kwargs(
                heldout_phases=((0.0, 0.0),) + MAX_HELDOUT_PHASES[1:]
            )
        )


def test_MAX_SCHEDULE_config_hash_binds_heldout_sizes():
    base = ObservationConfig.max_profile()
    drifted = ObservationConfig(
        resolutions=base.resolutions,
        base_subpixel_phases=base.base_subpixel_phases,
        expanded_subpixel_phases=base.expanded_subpixel_phases,
        held_out_subpixel_phases=base.held_out_subpixel_phases,
        held_out_sizes_px=MAX_HELDOUT_SIZES_PX[:-1],
        metric_sizes_px=base.metric_sizes_px,
        feature_probes=base.feature_probes,
        config_version=base.config_version,
    )
    assert base.compute_hash() != drifted.compute_hash()
    # Legacy config is never MAX-conformant.
    with pytest.raises(ValueError, match="MAX_SCHEDULE"):
        validate_observation_config_max(ObservationConfig())


# =========================================================================
# DISCOVERY_TERMINATION
# =========================================================================


def test_DISCOVERY_TERMINATION_observable_signals_complete():
    candidates = [65, 66, 67]

    async def run():
        # Exhaustion with full observability: grounded completion.
        cov, reason = await ObservableGlyphDiscovery.discover_with_termination(
            lambda cp: True, candidate_code_points=candidates
        )
        assert cov == [65, 66, 67] and reason == "EXHAUSTED"

        # Empty source after exhaustive enumeration.
        cov, reason = await ObservableGlyphDiscovery.discover_with_termination(
            lambda cp: False, candidate_code_points=candidates, max_consecutive_misses=10
        )
        assert cov == [] and reason == "EMPTY"

        # Trailing gap below the miss window: exhaustive scan still completes.
        cov, reason = await ObservableGlyphDiscovery.discover_with_termination(
            lambda cp: cp == 65, candidate_code_points=candidates, max_consecutive_misses=10
        )
        assert cov == [65] and reason == "EXHAUSTED"

        # Duplicated caller-owned candidates never fabricate a REPEATED
        # completion signal; enumeration stays grounded.
        cov, reason = await ObservableGlyphDiscovery.discover_with_termination(
            lambda cp: True, candidate_code_points=[65, 66, 65]
        )
        assert cov == [65, 66] and reason == "EXHAUSTED"

        for r in ("EXHAUSTED", "EMPTY"):
            assert r in ObservableGlyphDiscovery.TERMINAL_COMPLETE

    asyncio.run(run())


def test_LATE_GLYPH_DISCOVERY_gap_never_silently_completed():
    """LATE_GLYPH_DISCOVERY: a supported glyph after >500 unsupported
    candidates cannot be silently completed away; the miss window is a
    safety budget and yields BLOCKED, never completion."""

    async def run():
        late = {65, 700}
        cov, reason = await ObservableGlyphDiscovery.discover_with_termination(
            lambda cp: cp in late,
            candidate_code_points=list(range(65, 800)),
            max_consecutive_misses=500,
        )
        assert reason == "BUDGET_EXHAUSTED"
        assert reason in ObservableGlyphDiscovery.TERMINAL_BLOCKED
        assert reason not in ObservableGlyphDiscovery.TERMINAL_COMPLETE
        # Only the pre-gap glyph was gathered; the late glyph is never
        # silently included under a completion claim.
        assert cov == [65]

    asyncio.run(run())


def test_DISCOVERY_TERMINATION_budget_exhaustion_never_complete():
    async def run():
        # The safety probe budget stops execution before any observable
        # termination signal: BLOCKED, never completion.
        cov, reason = await ObservableGlyphDiscovery.discover_with_termination(
            lambda cp: cp % 200 == 0,
            candidate_code_points=list(range(1, 5000)),
            max_consecutive_misses=500,
            max_candidates=250,
        )
        assert reason == "BUDGET_EXHAUSTED"
        assert reason in ObservableGlyphDiscovery.TERMINAL_BLOCKED
        assert reason not in ObservableGlyphDiscovery.TERMINAL_COMPLETE

    asyncio.run(run())


# =========================================================================
# LOSS_VECTOR_COMPLETE
# =========================================================================


def _square_contours():
    pts = [Point2D(100.0, 0.0), Point2D(600.0, 0.0), Point2D(600.0, 700.0), Point2D(100.0, 700.0)]
    segments = [LineSegment(p0=pts[i], p1=pts[(i + 1) % 4]) for i in range(4)]
    return [Contour(segments=segments, is_hole=False, parent_index=-1, area_upem=350000.0)]


def _prepared_identity():
    transform = CalibrationTransform(
        resolution=64,
        font_size_px=46.0,
        units_per_em=1000,
        scale=0.046,
        x_origin_px=8.0,
        y_origin_px=48.0,
        subpixel_x=0.0,
        subpixel_y=0.0,
    )
    optimizer = FitOnlyGlyphOptimizer()
    ref_mask = optimizer._rasterize_contours(_square_contours(), transform, 64, 16)
    return optimizer, [(transform, ref_mask, 64)]


def test_LOSS_VECTOR_COMPLETE_all_components_real_and_required():
    optimizer, prepared = _prepared_identity()
    components = optimizer._loss_components(_square_contours(), prepared)
    assert set(components) == set(REQUIRED_OPTIMIZATION_LOSSES)
    for name in REQUIRED_OPTIMIZATION_LOSSES:
        assert math.isfinite(components[name])
    # Perfect fit: observable losses vanish; shape terms remain real.
    assert components["coverage"] == 0.0
    assert components["edge"] == 0.0
    assert components["sdf"] == 0.0
    assert components["curvature"] > 0.0
    assert components["complexity"] > 0.0

    objective = optimizer._objective(_square_contours(), prepared)
    expected = sum(OPTIMIZATION_LOSS_WEIGHTS[n] * components[n] for n in REQUIRED_OPTIMIZATION_LOSSES)
    assert objective == pytest.approx(expected)

    # Misaligned outlines make every observable loss strictly positive...
    shifted = [
        Contour(
            segments=[
                LineSegment(
                    p0=Point2D(s.p0.x + 80.0, s.p0.y + 40.0),
                    p1=Point2D(s.p1.x + 80.0, s.p1.y + 40.0),
                )
                for s in c.segments
            ],
            is_hole=c.is_hole,
            parent_index=c.parent_index,
            area_upem=c.area_upem,
        )
        for c in _square_contours()
    ]
    misfit = optimizer._loss_components(shifted, prepared)
    for name in ("coverage", "edge", "sdf"):
        assert misfit[name] > 0.0
    misfit_objective = sum(OPTIMIZATION_LOSS_WEIGHTS[n] * misfit[n] for n in REQUIRED_OPTIMIZATION_LOSSES)

    # ...so zeroing any single component causally changes the production
    # objective (no no-op loss can satisfy MAX).
    for name in REQUIRED_OPTIMIZATION_LOSSES:
        tampered = dict(misfit)
        tampered[name] = 0.0
        tampered_objective = sum(
            OPTIMIZATION_LOSS_WEIGHTS[n] * tampered[n] for n in REQUIRED_OPTIMIZATION_LOSSES
        )
        assert tampered_objective != misfit_objective


def test_LOSS_VECTOR_COMPLETE_validation_rejects_missing_or_non_finite():
    from fidelity.optimizer import recompute_objective_from_components

    components_ok = tuple((n, 0.1) for n in REQUIRED_OPTIMIZATION_LOSSES)
    record = GlyphOptimizationRecord(
        code_point=65,
        initial_objective=1.0,
        final_objective=recompute_objective_from_components(dict(components_ok)),
        iterations=1,
        stop_reason="CONVERGED",
        accepted_objective_trace=(1.0, 0.5),
        loss_components=components_ok,
    )
    validate_loss_vector_complete(record)

    missing = GlyphOptimizationRecord(
        code_point=65,
        initial_objective=1.0,
        final_objective=0.5,
        iterations=1,
        stop_reason="CONVERGED",
        accepted_objective_trace=(1.0, 0.5),
        loss_components=tuple((n, 0.1) for n in REQUIRED_OPTIMIZATION_LOSSES if n != "sdf"),
    )
    with pytest.raises(ValueError, match=r"OPTIMIZER_LOSS_VECTOR_INCOMPLETE:missing=\['sdf'\]"):
        validate_loss_vector_complete(missing)

    non_finite = GlyphOptimizationRecord(
        code_point=65,
        initial_objective=1.0,
        final_objective=0.181,
        iterations=1,
        stop_reason="CONVERGED",
        accepted_objective_trace=(1.0, 0.5),
        loss_components=tuple(
            (n, float("nan") if n == "edge" else 0.1) for n in REQUIRED_OPTIMIZATION_LOSSES
        ),
    )
    with pytest.raises(ValueError, match="OPTIMIZER_LOSS_NON_FINITE:edge"):
        validate_loss_vector_complete(non_finite)

    # Forged total: components valid but recorded objective differs from the
    # recomputed weighted sum.
    forged_total = GlyphOptimizationRecord(
        code_point=65,
        initial_objective=1.0,
        final_objective=0.999,
        iterations=1,
        stop_reason="CONVERGED",
        accepted_objective_trace=(1.0, 0.5),
        loss_components=components_ok,
    )
    with pytest.raises(ValueError, match="OPTIMIZER_LOSS_TOTAL_FORGED"):
        validate_loss_vector_complete(forged_total)

    # Duplicate term rejects.
    duplicated = GlyphOptimizationRecord(
        code_point=65,
        initial_objective=1.0,
        final_objective=0.5,
        iterations=1,
        stop_reason="CONVERGED",
        accepted_objective_trace=(1.0, 0.5),
        loss_components=(("coverage", 0.1), ("coverage", 0.1), ("edge", 0.1),
                         ("sdf", 0.1), ("curvature", 0.1), ("complexity", 0.1)),
    )
    with pytest.raises(ValueError, match="OPTIMIZER_LOSS_VECTOR_DUPLICATE_TERM"):
        validate_loss_vector_complete(duplicated)


# =========================================================================
# PROVIDER_AXIS_FORGERY (closed-set re-assertion)
# =========================================================================


def _typed_page(provenance: str) -> SpriteRasterPage:
    return SpriteRasterPage(
        page_index=1,
        glyph_count=1,
        raster_bytes=b"\x89PNG\r\n\x1a\n" + b"\x00" * 16,
        payload={"provenance": provenance, "md5": "a" * 32, "acs_pt": 120},
    )


def test_PROVIDER_AXIS_FORGERY_closed_provider_set():
    with pytest.raises(ValueError, match="CAPABILITY_FORGED"):
        ProviderRasterCapability(
            provider="invented_provider", phase=FIXED_PHASE, fit_sizes=(120,), held_out_sizes=(240,)
        ).validate()
    with pytest.raises(ValueError, match="CAPABILITY_FORGED"):
        ProviderRasterCapability(
            provider=PROVIDER_PLAYWRIGHT_STEALTH, phase=(0.25, 0.25), fit_sizes=(120,), held_out_sizes=(240,)
        ).validate()

    assert resolve_raster_provider((_typed_page(PROVIDER_MONOTYPE_RENDER),)) == PROVIDER_MONOTYPE_RENDER
    assert resolve_raster_provider((_typed_page(PROVIDER_PLAYWRIGHT_STEALTH),)) == PROVIDER_PLAYWRIGHT_STEALTH
    with pytest.raises(ValueError, match="RASTER_PROVIDER_UNKNOWN_OR_ABSENT"):
        resolve_raster_provider((_typed_page("invented_provider"),))
    with pytest.raises(ValueError, match="RASTER_PROVIDER_UNKNOWN_OR_ABSENT"):
        resolve_raster_provider((_typed_page(""),))
    with pytest.raises(ValueError, match="RASTER_PROVIDER_MIXED"):
        resolve_raster_provider(
            (_typed_page(PROVIDER_MONOTYPE_RENDER), _typed_page(PROVIDER_PLAYWRIGHT_STEALTH))
        )
    with pytest.raises(ValueError, match="RASTER_PROVIDER_ABSENT"):
        resolve_raster_provider(())


# =========================================================================
# HELDOUT_SEALED schedule disjointness + feature probe identity
# =========================================================================


def test_HELDOUT_SEALED_disjoint_canonical_schedules():
    assert not (set(MAX_HELDOUT_SIZES_PX) & set(MAX_RASTER_SIZES_PX))
    assert not (set(MAX_HELDOUT_SIZES_PX) & set(MAX_METRIC_SIZES_PX))
    assert set(MAX_CORE_RASTER_SIZES_PX).issubset(set(MAX_RASTER_SIZES_PX))
    # Held-out phases are disjoint from the ACTUAL maximum per-glyph fit set
    # (the hard 8x8 expansion grid), and therefore also from the base grid.
    assert not (set(MAX_HELDOUT_PHASES) & set(MAX_HARD_PHASES_8X8))
    assert not (set(MAX_HELDOUT_PHASES) & set(MAX_BROWSER_PHASES_4X4))
    assert set(MAX_BROWSER_PHASES_4X4).issubset(set(MAX_HARD_PHASES_8X8))
    assert len(MAX_BROWSER_PHASES_4X4) == 16
    assert len(MAX_HARD_PHASES_8X8) == 64
    assert len(MAX_HELDOUT_PHASES) == 16


def test_FEATURE_PROBE_TAGS_closed_identity_and_causal_samples():
    assert MAX_FEATURE_PROBE_TAGS[:14] == (
        "kern", "liga", "clig", "dlig", "calt", "case", "frac",
        "tnum", "pnum", "onum", "lnum", "zero", "smcp", "c2sc",
    )
    assert MAX_FEATURE_PROBE_TAGS[14:] == tuple(f"ss{i:02d}" for i in range(1, 21))
    assert len(MAX_FEATURE_PROBES) == 34
    for tag, sample in MAX_FEATURE_PROBES:
        assert sample and sample.strip()
        assert MAX_FEATURE_PROBE_SAMPLES[tag] == sample
