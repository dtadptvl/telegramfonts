"""Focused tests for FAST_ATLAS_ULTRA_V1 (T-FAST-ATLAS-ULTRA-01, ADR-0004).

Scoped exactly to the task budget: unit tests for atlas paging/memory
release, metrics batching/regression, geometry confidence decisions,
single-refinement boundaries, checkpoint identity/resume, wall config
selection, validation-run-once gating, policy binding, and one small
end-to-end fixture run with checkpoint resume. No full suite, no soak, no
retired E2E.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import numpy as np
import pytest

from atlas.cache import (
    AtlasCacheStore,
    AtlasCheckpoint,
    AtlasCheckpointStore,
    identity_hash,
)
from atlas.geometry import (
    MIN_IOU_EASY_PASS,
    fast_geometry_for_glyph,
    structural_check,
)
from atlas.metrics import (
    build_metrics_batches,
    metrics_js_call_count,
    parse_measure_text_rows,
    regress_glyph_metrics,
    METRICS_BATCH_MAX,
)
from atlas.models import CellMapping, GlyphStatus, RegressedMetrics
from atlas.paging import (
    AtlasBudgetExceeded,
    PagePlanInput,
    cell_dimensions,
    plan_atlas_pages,
)
from atlas.policy import (
    FAST_ATLAS_ULTRA_V1,
    ORIGINAL_WALL_SECONDS,
    PUBLIC_PROFILE_NAME,
    VIETNAMESE_WALL_SECONDS,
    AtlasRuntimeDefaults,
    resolve_atlas_policy,
    resolve_job_wall_seconds,
)
from atlas.refine import (
    RefinementScheduleViolation,
    merge_alpha_observations,
    validate_refinement_schedule,
)
from atlas.validation import ValidationAlreadyRan, run_speed_first_validation
from config import Settings

FIXTURE_FONT = (
    Path(__file__).parent.parent
    / "benchmark_data"
    / "ground_truth"
    / "BeVietnamPro-Regular.ttf"
)


# ----------------------------------------------------------------------
# Policy binding + wall selection (U1/U11)
# ----------------------------------------------------------------------

def test_policy_binding_fast30_sole_public_name_binds_atlas_ultra():
    assert resolve_atlas_policy(PUBLIC_PROFILE_NAME) == FAST_ATLAS_ULTRA_V1
    assert resolve_atlas_policy(None) == FAST_ATLAS_ULTRA_V1
    assert resolve_atlas_policy(FAST_ATLAS_ULTRA_V1) == FAST_ATLAS_ULTRA_V1
    with pytest.raises(ValueError, match="PROFILE_RETIRED"):
        resolve_atlas_policy("BALANCED_MAX")
    with pytest.raises(ValueError, match="PROFILE_RETIRED"):
        resolve_atlas_policy("FULL_MAX")
    with pytest.raises(ValueError, match="PROFILE_UNKNOWN"):
        resolve_atlas_policy("SOMETHING_ELSE")


def test_wall_config_selection_mode_aware_and_retargeted():
    assert ORIGINAL_WALL_SECONDS == 480
    assert VIETNAMESE_WALL_SECONDS == 720
    assert resolve_job_wall_seconds("ORIGINAL", 480, 720) == 480
    assert resolve_job_wall_seconds("VIETNAMESE", 480, 720) == 720
    assert resolve_job_wall_seconds(" vietnamese ", 480, 720) == 720
    with pytest.raises(ValueError, match="UNSUPPORTED_MODE"):
        resolve_job_wall_seconds("", 480, 720)


def test_settings_defaults_retargeted_and_config_driven(test_settings: Settings):
    assert test_settings.JOB_WALL_SECONDS == 480
    assert test_settings.JOB_WALL_SECONDS_VIETNAMESE == 720
    assert test_settings.ATLAS_ULTRA_ENABLED is True
    runtime = AtlasRuntimeDefaults(
        browser_sessions=test_settings.ATLAS_BROWSER_SESSIONS,
        http_concurrency=test_settings.ATLAS_HTTP_CONCURRENCY,
        glyph_workers=test_settings.ATLAS_GLYPH_WORKERS,
        atlas_pages_in_memory=test_settings.ATLAS_PAGES_IN_MEMORY,
        atlas_target_mb=test_settings.ATLAS_TARGET_MB,
        atlas_max_mb=test_settings.ATLAS_MAX_MB,
        checkpoint_batch=test_settings.ATLAS_CHECKPOINT_BATCH,
    ).validate()
    assert runtime.to_dict() == {
        "browser_sessions": 1,
        "http_concurrency": 8,
        "glyph_workers": 2,
        "atlas_pages_in_memory": 1,
        "atlas_target_mb": 96,
        "atlas_max_mb": 128,
        "checkpoint_batch": 32,
    }


# ----------------------------------------------------------------------
# Paging: bounded pages, no overlap, fail-closed budget (U2/U6)
# ----------------------------------------------------------------------

def test_atlas_paging_bounded_no_overlap_and_fail_closed():
    inputs = []
    for cp in range(32, 282):  # 250 glyphs, ordinary ORIGINAL fixture shape
        w, h = cell_dimensions(617.0, 800.0, 200.0, 1024)
        inputs.append(PagePlanInput(cp, w, h, 1024))
    target = 96 * 1024 * 1024
    hard_max = 128 * 1024 * 1024
    pages = plan_atlas_pages(inputs, target, hard_max)

    assert sum(len(p.cells) for p in pages) == 250
    for page in pages:
        assert page.decoded_bytes <= hard_max
        rects = [(c.x, c.y, c.x + c.w, c.y + c.h) for c in page.cells]
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                a, b = rects[i], rects[j]
                assert not (
                    a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]
                )

    with pytest.raises(AtlasBudgetExceeded):
        plan_atlas_pages(
            [PagePlanInput(65, 12000, 12000, 1024)], target, hard_max
        )


def test_atlas_page_memory_release_single_page_budget():
    w, h = cell_dimensions(500.0, 800.0, 200.0, 1024)
    pages = plan_atlas_pages(
        [PagePlanInput(cp, w, h, 1024) for cp in range(200)],
        4 * 1024 * 1024,
        8 * 1024 * 1024,
    )
    assert len(pages) > 1
    for page in pages:
        assert page.decoded_bytes <= 8 * 1024 * 1024


# ----------------------------------------------------------------------
# Metrics batching + regression (U3)
# ----------------------------------------------------------------------

def test_metrics_batching_is_few_batched_calls_never_per_glyph():
    cps = list(range(32, 32 + 500))
    batches = build_metrics_batches(cps)
    # 500 glyphs <= one chunk of 512 per size -> exactly 3 calls total.
    assert len(batches) == 3
    assert metrics_js_call_count(500) == 3
    # 250 glyphs -> still 3 calls. Never per-glyph (would be 750).
    assert metrics_js_call_count(250) == 3
    # 1200 glyphs -> 3 sizes x 3 chunks.
    assert metrics_js_call_count(1200) == 9
    assert all(len(chunk) <= METRICS_BATCH_MAX for _, chunk in batches)


def test_metrics_regression_to_upem_through_origin():
    cp = 65
    obs = []
    for size in (512, 1024, 2048):
        rows = [[
            600.0 * size / 1000.0,
            50.0 * size / 1000.0,
            550.0 * size / 1000.0,
            700.0 * size / 1000.0,
            200.0 * size / 1000.0,
            800.0 * size / 1000.0,
            200.0 * size / 1000.0,
        ]]
        obs += parse_measure_text_rows([cp], float(size), rows)
    reg = regress_glyph_metrics(obs)
    assert reg.code_point == cp
    assert abs(reg.advance_width_upem - 600.0) < 1e-6
    assert abs(reg.lsb_upem - 50.0) < 1e-6
    assert abs(reg.ascent_upem - 700.0) < 1e-6
    assert abs(reg.descent_upem - (-200.0)) < 1e-6
    assert reg.regression_residual < 1e-9

    with pytest.raises(ValueError, match="MISSING_SIZES"):
        regress_glyph_metrics(obs[:2])


# ----------------------------------------------------------------------
# Geometry confidence decisions (U4)
# ----------------------------------------------------------------------

def _regressed(cp: int = 65) -> RegressedMetrics:
    return RegressedMetrics(
        code_point=cp,
        advance_width_upem=600.0,
        lsb_upem=50.0,
        rsb_upem=50.0,
        ascent_upem=700.0,
        descent_upem=-100.0,
        bbox_upem=(50.0, -100.0, 550.0, 700.0),
        regression_residual=0.0,
    )


def test_geometry_confidence_easy_pass_and_thresholds():
    mapping = CellMapping(size_px=1024, pad_left_px=128, pad_top_px=128, ascent_px=800.0)
    alpha = np.zeros((1056, 856), dtype=np.uint8)
    alpha[300:800, 200:700] = 255
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(alpha, mode="L").save(buf, format="PNG")
    ev, contours, ink = fast_geometry_for_glyph(
        buf.getvalue(), mapping, _regressed(), 856, 1056
    )
    assert ev.status == GlyphStatus.EASY_PASS
    assert ev.iou >= MIN_IOU_EASY_PASS
    assert ev.structure_ok
    assert contours and all(c.is_closed for c in contours)


def test_geometry_confidence_structural_reasons_fail_closed():
    bad_residual = RegressedMetrics(
        code_point=65,
        advance_width_upem=600.0,
        lsb_upem=50.0,
        rsb_upem=50.0,
        ascent_upem=700.0,
        descent_upem=-100.0,
        bbox_upem=(50.0, -100.0, 550.0, 700.0),
        regression_residual=0.5,
    )
    ok, reasons = structural_check([], bad_residual, 0.0, None)
    assert not ok
    assert "METRICS_REGRESSION_RESIDUAL_EXCEEDED" in reasons
    ok2, reasons2 = structural_check([], _regressed(), float("inf"), None)
    assert not ok2
    assert "NON_FINITE_FIT_RESIDUAL" in reasons2


# ----------------------------------------------------------------------
# Single refinement boundaries (U5)
# ----------------------------------------------------------------------

def test_refinement_schedule_boundaries_fail_closed():
    validate_refinement_schedule([(1024, 0.0, 0.0), (1024, 0.5, 0.0), (2048, 0.0, 0.0)])
    for forbidden in [(512, 0.0, 0.0), (4096, 0.0, 0.0), (1024, 0.25, 0.0), (1024, 0.75, 0.0)]:
        with pytest.raises(RefinementScheduleViolation):
            validate_refinement_schedule([forbidden])


def test_refinement_merge_alpha_is_vectorized_average():
    from PIL import Image
    import io

    def png_of(arr: np.ndarray) -> bytes:
        buf = io.BytesIO()
        Image.fromarray(arr, mode="L").save(buf, format="PNG")
        return buf.getvalue()

    base = np.full((4, 4), 128, dtype=np.uint8)
    shifted = np.full((4, 4), 255, dtype=np.uint8)
    double = np.full((8, 8), 0, dtype=np.uint8)
    merged = merge_alpha_observations(png_of(base), png_of(shifted), png_of(double))
    assert merged.shape == (4, 4)
    assert np.all(np.isfinite(merged))
    # 0.5*(128/255) + 0.25*(aligned shifted: 0.5*1 + 0.5*0 padding) + 0.25*0
    assert 0.30 < float(merged[0, 0]) < 0.45


# ----------------------------------------------------------------------
# Checkpoint identity/resume + cache fail-closed (U6)
# ----------------------------------------------------------------------

def test_checkpoint_identity_resume_and_drift_fail_closed(tmp_path: Path):
    store = AtlasCheckpointStore(tmp_path / "ck")
    identity = identity_hash({"job": "atlas_1", "glyphs": [65, 66]})
    ck = AtlasCheckpoint(
        checkpoint_identity=identity,
        pages_completed=2,
        frozen_code_points=[65, 66],
        failed_code_points=[64],
    )
    store.save(ck)

    loaded = AtlasCheckpointStore(tmp_path / "ck").load(identity)
    assert loaded is not None
    assert loaded.pages_completed == 2
    assert loaded.frozen_code_points == [65, 66]
    assert loaded.failed_code_points == [64]

    drifted = identity_hash({"job": "atlas_2"})
    assert AtlasCheckpointStore(tmp_path / "ck").load(drifted) is None


def test_cache_exact_identity_integrity_fail_closed(tmp_path: Path):
    cache = AtlasCacheStore(tmp_path / "cache")
    idh = identity_hash({"obs": 65})
    cache.put_bytes_verified("observations", idh, b"png-bytes", "png")
    assert cache.get_bytes("observations", idh, "png") == b"png-bytes"
    assert cache.get_bytes("observations", identity_hash({"obs": 66}), "png") is None
    entry = tmp_path / "cache" / "observations" / f"{idh}.png"
    entry.write_bytes(b"tampered")
    assert cache.get_bytes("observations", idh, "png") is None


# ----------------------------------------------------------------------
# Validation run-once gating (U10)
# ----------------------------------------------------------------------

def test_validation_run_once_gate_raises_on_second_run():
    with pytest.raises(ValidationAlreadyRan):
        run_speed_first_validation(
            ttf_path=Path("nonexistent.ttf"),
            code_points=[65],
            kern_pairs=[],
            mode="ORIGINAL",
            already_ran=True,
        )


# ----------------------------------------------------------------------
# Small end-to-end fixture + checkpoint resume (U4/U6/U9/U10)
# ----------------------------------------------------------------------

@pytest.mark.integration
def test_pipeline_end_to_end_small_and_checkpoint_resume(tmp_path: Path):
    from atlas.pipeline import AtlasUltraPipeline, AtlasStyleSpec
    from atlas.local_fixture import (
        LocalFontMetricsProvider,
        LocalFontRasterProvider,
        fixture_glyph_set,
    )

    if not FIXTURE_FONT.exists():
        pytest.skip("local fixture font unavailable")

    cps = fixture_glyph_set(FIXTURE_FONT, limit=12)
    spec = AtlasStyleSpec(
        source_url="fixture://local/be-vietnam-pro",
        family_name="Atlas Test Fixture",
        style_name="Regular",
        style_id="regular",
        mode="ORIGINAL",
        code_points=cps,
    )
    cache = AtlasCacheStore(tmp_path / "cache")
    ck_store = AtlasCheckpointStore(tmp_path / "ck")

    def make() -> AtlasUltraPipeline:
        return AtlasUltraPipeline(
            spec=spec,
            runtime=AtlasRuntimeDefaults(),
            metrics_provider=LocalFontMetricsProvider(FIXTURE_FONT),
            raster_provider=LocalFontRasterProvider(FIXTURE_FONT),
            cache=cache,
            checkpoint_store=ck_store,
            deadline=time.monotonic() + 300,
        )

    result = asyncio.run(make().run())
    assert result.ttf_path is not None and result.ttf_path.exists()
    assert result.otf_path is not None and result.otf_path.exists()
    assert result.report["passed"] is True
    ev = result.evidence.to_dict()
    assert ev["cdp_calls"] == 0  # never per-glyph CDP in this pipeline
    assert ev["easy_glyphs"] + ev["refined_glyphs"] + ev["failed_glyphs"] == len(cps)
    assert result.model is not None
    assert len(result.frozen_glyphs) >= 10

    ck = ck_store.load(make()._checkpoint_identity)
    assert ck is not None
    assert sorted(ck.frozen_code_points) == sorted(result.frozen_glyphs.keys())


# ----------------------------------------------------------------------
# Runner gate-off (U10)
# ----------------------------------------------------------------------

def test_runner_atlas_route_fails_closed_without_factory(test_settings: Settings):
    from runner import A23Runner

    r = A23Runner.__new__(A23Runner)
    r.settings = test_settings
    r.atlas_pipeline_factory = None
    r.last_reuse_trace = {"events": []}

    class _Style:
        id = "regular"
        display_name = "Regular"

    with pytest.raises(ValueError, match="ATLAS_RASTER_SOURCE_UNAVAILABLE"):
        r._atlas_ultra_resolve_artifact(
            job=None,
            style=_Style(),
            family_name="Fam",
            fmt="TTF",
            build_dir=Path("nowhere"),
            mode="ORIGINAL",
            job_deadline=None,
            completed=[("bv", "cfg")],
            archive_context=None,
        )
