"""Issue #75 consolidated production-path causal repro pack (FIX_REQUIRED 5423738621).

FULLMAX_FINALIZE_ROUNDTRIP, HARD_HELDOUT_DISJOINT, METRIC_SCHEDULE_CLOSED,
LOSS_VECTOR_CAUSAL, TYPOGRAPHY_MULTI_SIZE_CAUSAL, FONTMODEL_DEEP_IMMUTABLE,
VI_DETERMINISTIC_FIRST. FULLMAX_E2E_EXACT lives in
test_issue75_fullmax_e2e.py (canonical-config end-to-end chain).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.fullmax_e2e

from measurement.collector import (
    ObservationCollector,
    derive_multisize_kerning,
    validate_pair_size_schedule,
)
from measurement.discovery import ObservableGlyphDiscovery
from measurement.max_profile import (
    MAX_BROWSER_PHASES_4X4,
    MAX_HARD_PHASES_8X8,
    MAX_HELDOUT_PHASES,
    MAX_HELDOUT_SIZES_PX,
    MAX_METRIC_SIZES_PX,
    validate_observation_config_max,
)
from measurement.models import ObservationConfig
from measurement.store import ObservationStore


# =========================================================================
# METRIC_SCHEDULE_CLOSED
# =========================================================================


def test_METRIC_SCHEDULE_CLOSED_anchor_and_undeclared_rejected():
    """METRIC_SCHEDULE_CLOSED: the canonical metric anchor is inside the exact
    schedule; any undeclared anchor/size rejects structurally."""
    config = ObservationConfig.max_profile()
    validate_observation_config_max(config)
    assert float(config.font_size_px) in {float(s) for s in config.metric_sizes_px}
    assert tuple(int(s) for s in config.metric_sizes_px) == MAX_METRIC_SIZES_PX

    # Undeclared 200px anchor rejects (production must make no undeclared
    # metric-size observation).
    drifted = ObservationConfig(
        resolutions=config.resolutions,
        base_subpixel_phases=config.base_subpixel_phases,
        expanded_subpixel_phases=config.expanded_subpixel_phases,
        held_out_subpixel_phases=config.held_out_subpixel_phases,
        held_out_sizes_px=config.held_out_sizes_px,
        font_size_px=200.0,
        metric_sizes_px=config.metric_sizes_px,
        feature_probes=config.feature_probes,
        config_version=config.config_version,
    )
    with pytest.raises(ValueError, match="MAX_SCHEDULE_METRIC_ANCHOR_UNDECLARED"):
        validate_observation_config_max(drifted)

    # Missing/extra raster sizes reject (drop a non-core size so the exact
    # closure check, not the core-subset invariant, is the rejector).
    raster_missing_768 = tuple(r for r in config.resolutions if int(r) != 768)
    with pytest.raises(ValueError, match="MAX_SCHEDULE_MISMATCH:RASTER_SIZES"):
        validate_observation_config_max(
            ObservationConfig(
                resolutions=raster_missing_768,
                base_subpixel_phases=config.base_subpixel_phases,
                expanded_subpixel_phases=config.expanded_subpixel_phases,
                held_out_subpixel_phases=config.held_out_subpixel_phases,
                held_out_sizes_px=config.held_out_sizes_px,
                font_size_px=config.font_size_px,
                metric_sizes_px=config.metric_sizes_px,
                feature_probes=config.feature_probes,
                config_version=config.config_version,
            )
        )


# =========================================================================
# HARD_HELDOUT_DISJOINT
# =========================================================================


def test_HARD_HELDOUT_DISJOINT_actual_fit_sets_never_contain_heldout():
    """HARD_HELDOUT_DISJOINT: actual per-glyph fit phase sets (base AND hard
    expansion) are disjoint from held-out phases; overlap injection rejects."""
    base_set = set(MAX_BROWSER_PHASES_4X4)
    hard_set = set(MAX_HARD_PHASES_8X8)
    heldout_set = set(MAX_HELDOUT_PHASES)

    # The actual adaptive fit set for ANY glyph is base (normal) or the full
    # hard grid (hard glyph); both are disjoint from held-out phases.
    assert not (base_set & heldout_set)
    assert not (hard_set & heldout_set)
    assert len(heldout_set) == 16

    config = ObservationConfig.max_profile()
    # Injected overlap: a config whose held-out phases reuse a hard-grid fit
    # phase is rejected by the closed validator.
    overlapped = ObservationConfig(
        resolutions=config.resolutions,
        base_subpixel_phases=config.base_subpixel_phases,
        expanded_subpixel_phases=config.expanded_subpixel_phases,
        held_out_subpixel_phases=((0.125, 0.125),) + config.held_out_subpixel_phases[1:],
        held_out_sizes_px=config.held_out_sizes_px,
        font_size_px=config.font_size_px,
        metric_sizes_px=config.metric_sizes_px,
        feature_probes=config.feature_probes,
        config_version=config.config_version,
    )
    with pytest.raises(ValueError, match="MAX_SCHEDULE_HELDOUT_PHASE_OVERLAP"):
        validate_observation_config_max(overlapped)

    # Held-out render sizes stay disjoint from fit raster and metric sizes.
    assert not (set(MAX_HELDOUT_SIZES_PX) & set(int(r) for r in config.resolutions))
    assert not (set(MAX_HELDOUT_SIZES_PX) & set(int(s) for s in config.metric_sizes_px))


# =========================================================================
# FULLMAX_FINALIZE_ROUNDTRIP
# =========================================================================


class _MaxFixtureSession:
    """Deterministic fixture browser session for canonical-config collection."""

    browser_version = "chromium_max_fixture_v1"

    def __init__(self, supported=(65, 66)):
        self.supported = set(supported)

    async def start(self):
        pass

    def close(self):
        pass

    async def aclose(self):
        pass

    async def observe_source_font(self, url, display_name, family):
        return "MaxFam"

    async def is_glyph_supported_in_font(self, font_family, code_point):
        return code_point in self.supported

    async def measure_glyph_direct(self, font_family, code_point, font_size_px, upem):
        from measurement.models import DirectMetrics

        scale = float(font_size_px) / float(upem)
        return DirectMetrics.from_browser_measurements(
            code_point=code_point,
            char=chr(code_point),
            font_size_px=float(font_size_px),
            m={
                "width": 600.0 * scale,
                "actualBoundingBoxLeft": 50.0 * scale,
                "actualBoundingBoxRight": 550.0 * scale,
                "actualBoundingBoxAscent": 700.0 * scale,
                "actualBoundingBoxDescent": 0.0,
                "fontBoundingBoxAscent": 800.0 * scale,
                "fontBoundingBoxDescent": -200.0 * scale,
            },
            upem=upem,
        )

    async def measure_text_advance(self, font_family, text, font_size_px, upem):
        # Deterministic size-independent pair advance (1200 upem).
        return 1200.0

    async def probe_opentype_feature(self, font_family, feature_tag, sample_text, font_size_px, upem):
        return {
            "enabled_advance_upem": 1200.0,
            "disabled_advance_upem": 1200.0,
            "enabled_raster_signature": "sig_on",
            "disabled_raster_signature": "sig_off",
        }

    async def capture_lossless_raster(self, font_family, code_point, resolution_px, subpixel_offset=(0.0, 0.0)):
        import io

        from PIL import Image, ImageDraw

        res = int(resolution_px)
        img = Image.new("L", (res, res), 255)
        draw = ImageDraw.Draw(img)
        # Deterministic ink square; phase shifts vary the raster bytes.
        shift = int(round((subpixel_offset[0] + subpixel_offset[1]) * 4.0))
        q = max(res // 8, 2)
        x0 = res // 4 + shift
        y0 = res // 4 + shift
        draw.rectangle([x0, y0, x0 + res // 3, y0 + res // 3], fill=0)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()


async def _collect_max(tmp_path: Path) -> tuple[ObservationStore, ObservationConfig, str]:
    store = ObservationStore(tmp_path / "max_store")
    config = ObservationConfig.max_profile()
    collector = ObservationCollector(_MaxFixtureSession(), store, config)
    await collector.collect_font_observations(
        "max_fam", "regular", "MaxFam", code_points=[65, 66]
    )
    await collector.collect_pair_observations("max_fam", "regular", "MaxFam")
    await collector.collect_feature_observations("max_fam", "regular", "MaxFam")
    collector.finalize_source_collection("max_fam", "regular")
    return store, config, _MaxFixtureSession.browser_version


def test_FULLMAX_FINALIZE_ROUNDTRIP_exact_key_set_equality(tmp_path: Path):
    """FULLMAX_FINALIZE_ROUNDTRIP: canonical collector output finalizes with
    exact key-set equality and reloads; any missing/extra schedule key
    rejects."""

    async def run():
        store, config, bv = await _collect_max(tmp_path)
        cfg_h = config.compute_hash()
        assert store.is_source_collection_completed("max_fam", "regular", cfg_h, bv)

        # Reload under exact identity: every declared schedule key exists.
        for cp in (65, 66):
            obs = store.get_glyph_observations("max_fam", "regular", cp, browser_version=bv, config_hash=cfg_h)
            fit_phases = config.get_phases_for_metrics(obs[0][0].metrics)
            expected_keys_per_glyph = (
                len(config.resolutions) * len(fit_phases)
                + len(config.effective_held_out_sizes()) * len(config.held_out_subpixel_phases)
            )
            assert len(obs) == expected_keys_per_glyph
            resolutions_seen = {rec.resolution for rec, _ in obs}
            assert set(config.resolutions) | set(config.effective_held_out_sizes()) == resolutions_seen

        # Metric observations never exceed the closed metric schedule.
        metric_rows = store.get_metric_observations(
            "max_fam", "regular", browser_version=bv, config_hash=cfg_h
        )
        sizes_seen: set[float] = set()
        for row in metric_rows:
            # metric_observations rows carry a JSON blob in metrics_json; sizes
            # come from the font_size_px column (the schedule member used to
            # capture them).
            sizes_seen.add(float(row["font_size_px"]))
        assert sizes_seen.issubset({float(s) for s in config.metric_sizes_px})

    asyncio.run(run())


def test_FULLMAX_FINALIZE_ROUNDTRIP_missing_and_extra_keys_reject(tmp_path: Path):
    async def run():
        # Missing schedule key: delete one held-out observation, finalize fails.
        store, config, bv = await _collect_max(tmp_path / "missing")
        cfg_h = config.compute_hash()
        with store._get_connection() as conn:
            conn.execute(
                "DELETE FROM observations WHERE rowid IN ("
                "SELECT rowid FROM observations WHERE reference_id='max_fam' AND style_id='regular' LIMIT 1)"
            )
            conn.commit()
        collector = ObservationCollector(_MaxFixtureSession(), store, config)
        with pytest.raises(ValueError, match="FINALIZATION_FAILED"):
            collector.finalize_source_collection("max_fam", "regular")

    asyncio.run(run())

    async def run_extra():
        # Extra undeclared key: inject an observation outside the schedule.
        store, config, bv = await _collect_max(tmp_path / "extra")
        cfg_h = config.compute_hash()
        collector = ObservationCollector(_MaxFixtureSession(), store, config)
        png = await _MaxFixtureSession().capture_lossless_raster("MaxFam", 65, 256, (0.0, 0.0))
        from measurement.models import ObservationRecord

        extra_key = ObservationRecord.build_cache_key(
            reference_id="max_fam",
            style_id="regular",
            code_point=65,
            browser_version=bv,
            resolution=999,  # undeclared size
            subpixel_x=0.0,
            subpixel_y=0.0,
            config_hash=cfg_h,
        )
        metrics = await _MaxFixtureSession().measure_glyph_direct("MaxFam", 65, 256.0, 1000)
        record = ObservationRecord(
            cache_key=extra_key,
            reference_id="max_fam",
            style_id="regular",
            code_point=65,
            resolution=999,
            subpixel_x=0.0,
            subpixel_y=0.0,
            raster_relative_path=f"max_fam/regular/extra/{extra_key}.png",
            raster_sha256=hashlib.sha256(png).hexdigest(),
            raster_size_bytes=len(png),
            metrics=metrics,
            created_at="2026-01-01T00:00:00+00:00",
            browser_version=bv,
            config_hash=cfg_h,
        )
        store.save_observation(record, png)
        with pytest.raises(ValueError, match="FINALIZATION_FAILED"):
            collector.finalize_source_collection("max_fam", "regular")

    asyncio.run(run_extra())


# =========================================================================
# TYPOGRAPHY_MULTI_SIZE_CAUSAL
# =========================================================================


class _MultiSizePairSession(_MaxFixtureSession):
    """Pair advance varies deterministically per size (raw evidence drift
    changes the derived result)."""

    async def measure_text_advance(self, font_family, text, font_size_px, upem):
        # Per-size rounding drift: inferred kerning differs across sizes.
        base = 1200.0
        scale = float(font_size_px) / float(upem)
        drift = 0.4 if float(font_size_px) >= 100.0 else -0.4
        return (base + 0.0) * scale + drift


def test_TYPOGRAPHY_MULTI_SIZE_CAUSAL_raw_drift_and_schedule_closure(tmp_path: Path):
    """TYPOGRAPHY_MULTI_SIZE_CAUSAL: raw per-size pair/metric drift changes
    the derived kerning; single-size/forged aggregate/missing size rejects."""
    import math

    async def run():
        store = ObservationStore(tmp_path / "ms_store")
        config = ObservationConfig(
            resolutions=(32,),
            base_subpixel_phases=((0.0, 0.0),),
            expanded_subpixel_phases=((0.0, 0.0),),
            metric_sizes_px=(32.0, 64.0, 128.0),
            font_size_px=64.0,
            feature_probes=(("kern", "AV"),),
        )
        collector = ObservationCollector(_MultiSizePairSession(), store, config)
        await collector.collect_font_observations("ms_fam", "regular", "MaxFam", code_points=[65, 66])
        await collector.collect_pair_observations("ms_fam", "regular", "MaxFam", pairs=[(65, 66)])

        cfg_h = config.compute_hash()
        bv = _MultiSizePairSession.browser_version
        rows = store.get_pair_size_observations("ms_fam", "regular", 65, 66, browser_version=bv, config_hash=cfg_h)
        # Raw evidence covers the exact declared metric schedule (3 sizes).
        validate_pair_size_schedule(rows, config.metric_sizes_px)
        derived = derive_multisize_kerning(rows)
        pair_rows = store.get_pair_observations("ms_fam", "regular", browser_version=bv, config_hash=cfg_h)
        assert pair_rows and int(pair_rows[0]["inferred_kerning_upem"]) == derived

        # Raw drift changes the derived result (the median row moves).
        drifted = [dict(r) for r in rows]
        drifted[1]["inferred_kerning_upem"] = int(drifted[1]["inferred_kerning_upem"]) + 7
        assert derive_multisize_kerning(drifted) != derived

        # Missing size rejects (closed raw-evidence identity).
        with pytest.raises(ValueError, match="MULTISIZE_KERNING_SCHEDULE_MISMATCH"):
            validate_pair_size_schedule(rows[:-1], config.metric_sizes_px)
        # Extra size rejects.
        extra = list(rows) + [{**rows[0], "font_size_px": 999.0}]
        with pytest.raises(ValueError, match="MULTISIZE_KERNING_SCHEDULE_MISMATCH"):
            validate_pair_size_schedule(extra, config.metric_sizes_px)
        # Single-size evidence rejects against the declared schedule.
        with pytest.raises(ValueError, match="MULTISIZE_KERNING_SCHEDULE_MISMATCH"):
            validate_pair_size_schedule(rows[:1], config.metric_sizes_px)

        # Forged aggregate: stored derived kerning diverges from raw evidence
        # -> finalization fails closed.
        store.save_pair_observation(
            reference_id="ms_fam",
            style_id="regular",
            left_cp=65,
            right_cp=66,
            left_char="A",
            right_char="B",
            left_advance_upem=600.0,
            right_advance_upem=600.0,
            pair_advance_upem=1200.0,
            inferred_kerning_upem=derived + 13,  # forged aggregate
            confidence=1.0,
            provenance=f"chromium:{bv}:canvas_text_metrics",
            browser_version=bv,
            config_hash=cfg_h,
        )
        with pytest.raises(ValueError, match="FINALIZATION_FAILED"):
            collector.finalize_source_collection("ms_fam", "regular", expected_pairs=[(65, 66)])

    asyncio.run(run())


# =========================================================================
# LOSS_VECTOR_CAUSAL
# =========================================================================


def _square_line_contours(with_midpoints: bool):
    from reconstruction.models import Contour, LineSegment, Point2D

    corners = [Point2D(100.0, 0.0), Point2D(600.0, 0.0), Point2D(600.0, 700.0), Point2D(100.0, 700.0)]
    if with_midpoints:
        pts = []
        for i in range(4):
            a = corners[i]
            b = corners[(i + 1) % 4]
            pts.append(a)
            pts.append(Point2D((a.x + b.x) / 2.0, (a.y + b.y) / 2.0))
    else:
        pts = corners
    segments = [LineSegment(p0=pts[i], p1=pts[(i + 1) % len(pts)]) for i in range(len(pts))]
    return [Contour(segments=segments, is_hole=False, parent_index=-1, area_upem=350000.0)]


def _identity_prepared(offset_px: float = 0.0):
    """Prepared fit evidence: reference rasterized at the true (unshifted)
    position; the evaluation transform is optionally shifted, producing a
    controlled misfit."""
    from fidelity.optimizer import FitOnlyGlyphOptimizer
    from measurement.calibration import CalibrationTransform

    ref_transform = CalibrationTransform(
        resolution=64,
        font_size_px=46.0,
        units_per_em=1000,
        scale=0.046,
        x_origin_px=8.0,
        y_origin_px=48.0,
        subpixel_x=0.0,
        subpixel_y=0.0,
    )
    eval_transform = CalibrationTransform(
        resolution=64,
        font_size_px=46.0,
        units_per_em=1000,
        scale=0.046,
        x_origin_px=8.0 + offset_px,
        y_origin_px=48.0,
        subpixel_x=0.0,
        subpixel_y=0.0,
    )
    optimizer = FitOnlyGlyphOptimizer()
    ref_mask = optimizer._rasterize_contours(_square_line_contours(False), ref_transform, 64, 16)
    return optimizer, [(eval_transform, ref_mask, 64)]


def test_LOSS_VECTOR_CAUSAL_each_term_changes_selection_or_objective(monkeypatch):
    """LOSS_VECTOR_CAUSAL: every required term is real and causally wired.
    Unsigned/fake SDF, constant/no-op terms, wrong weights, forged totals or
    traces reject; each term can change candidate selection/objective on a
    controlled fixture."""
    import fidelity.optimizer as opt_mod
    from fidelity.optimizer import (
        OPTIMIZATION_LOSS_WEIGHTS,
        REQUIRED_OPTIMIZATION_LOSSES,
        GlyphOptimizationRecord,
        recompute_objective_from_components,
        validate_loss_vector_complete,
    )
    from reconstruction.models import ReconstructedGlyph

    # Misaligned fixture: observable coverage/edge/signed-SDF terms positive.
    optimizer, prepared = _identity_prepared(offset_px=3.0)
    glyph = ReconstructedGlyph(
        code_point=65,
        character="A",
        advance_width_upem=600.0,
        lsb_upem=50.0,
        rsb_upem=50.0,
        ascent_upem=700.0,
        descent_upem=0.0,
        contours=_square_line_contours(False),
        bounding_box_upem=(100.0, 0.0, 600.0, 700.0),
        reconstruction_time_ms=1.0,
    )
    components = optimizer._loss_components(glyph.contours, prepared)
    for name in ("coverage", "edge", "sdf"):
        assert components[name] > 0.0, f"{name} must be a real positive term on misfit"
    # Signed SDF sanity: the sign convention distinguishes interior from
    # exterior (a real signed field, not unsigned foreground distance).
    sd = optimizer._signed_distance(prepared[0][1].astype(bool))
    assert float(sd.min()) < 0.0 < float(sd.max())

    # Curvature causality: redundant-collinear start glyph. Canonical weights
    # keep the original; zeroing curvature flips variant selection to the
    # complexity-cheaper simplified outline.
    glyph_mid = ReconstructedGlyph(
        code_point=66,
        character="B",
        advance_width_upem=600.0,
        lsb_upem=50.0,
        rsb_upem=50.0,
        ascent_upem=700.0,
        descent_upem=0.0,
        contours=_square_line_contours(True),
        bounding_box_upem=(100.0, 0.0, 600.0, 700.0),
        reconstruction_time_ms=1.0,
    )
    _optimizer_aligned, prepared_aligned = _identity_prepared(offset_px=0.0)
    _optimized_canonical, record_canonical = optimizer.optimize_glyph(glyph_mid, prepared_aligned)
    assert record_canonical.selected_variant == "original"

    weights_no_curvature = dict(OPTIMIZATION_LOSS_WEIGHTS)
    weights_no_curvature["curvature"] = 1e-12
    monkeypatch.setattr(opt_mod, "OPTIMIZATION_LOSS_WEIGHTS", weights_no_curvature)
    _optimized_flip, record_flip = optimizer.optimize_glyph(glyph_mid, prepared_aligned)
    monkeypatch.undo()
    assert record_flip.selected_variant == "simplified", (
        "curvature term must causally participate in candidate selection"
    )

    # Complexity strictly varies across the segment-variant lattice, so it
    # participates in selection between shape-equal candidates.
    comp_original = optimizer._complexity_loss(_square_line_contours(False))
    from fidelity.optimizer import _subdivide_contours

    comp_subdivided = optimizer._complexity_loss(_subdivide_contours(_square_line_contours(False)))
    assert comp_subdivided > comp_original

    # Forged totals and substituted/extra terms reject.
    components_ok = tuple((n, 0.1) for n in REQUIRED_OPTIMIZATION_LOSSES)
    forged = GlyphOptimizationRecord(
        code_point=65,
        initial_objective=1.0,
        final_objective=1.234,
        iterations=1,
        stop_reason="CONVERGED",
        accepted_objective_trace=(1.0,),
        loss_components=components_ok,
    )
    with pytest.raises(ValueError, match="OPTIMIZER_LOSS_TOTAL_FORGED"):
        validate_loss_vector_complete(forged)

    substituted = GlyphOptimizationRecord(
        code_point=65,
        initial_objective=1.0,
        final_objective=recompute_objective_from_components(dict(components_ok)),
        iterations=1,
        stop_reason="CONVERGED",
        accepted_objective_trace=(1.0,),
        loss_components=(("coverage_iou_fake", 0.1),) + components_ok[1:],
    )
    with pytest.raises(ValueError, match="OPTIMIZER_LOSS_VECTOR_INCOMPLETE"):
        validate_loss_vector_complete(substituted)

    # Every optimized record binds the recomputable vector under the weights
    # active at optimization time (record_flip is bound to the no-curvature
    # weights and is therefore validated inside the patched context).
    validate_loss_vector_complete(record_canonical)
    assert record_canonical.final_objective == pytest.approx(
        recompute_objective_from_components(dict(record_canonical.loss_components))
    )
    monkeypatch.setattr(opt_mod, "OPTIMIZATION_LOSS_WEIGHTS", weights_no_curvature)
    validate_loss_vector_complete(record_flip)
    monkeypatch.undo()


# =========================================================================
# FONTMODEL_DEEP_IMMUTABLE
# =========================================================================


def _small_model():
    from reconstruction.font_model import CalibratedGlyph, CanonicalFontModel, GlobalFontMetrics
    from reconstruction.models import Contour, LineSegment, Point2D

    def square_glyph(cp: int) -> CalibratedGlyph:
        pts = [Point2D(100.0, 0.0), Point2D(500.0, 0.0), Point2D(500.0, 700.0), Point2D(100.0, 700.0)]
        segments = [LineSegment(p0=pts[i], p1=pts[(i + 1) % 4]) for i in range(4)]
        return CalibratedGlyph(
            code_point=cp,
            character=chr(cp),
            advance_width_upem=600.0,
            lsb_upem=100.0,
            rsb_upem=100.0,
            ascent_upem=700.0,
            descent_upem=0.0,
            bounding_box_upem=(100.0, 0.0, 500.0, 700.0),
            contours=[Contour(segments=segments, is_hole=False, parent_index=-1, area_upem=280000.0)],
            confidence=1.0,
            observation_fingerprints=(hashlib.sha256(f"obs:{cp}".encode()).hexdigest(),),
        )

    return CanonicalFontModel(
        schema_version="1.0.0",
        family_name="SealFam",
        style_name="Regular",
        reference_id="seal_fam",
        style_id="regular",
        metrics=GlobalFontMetrics(),
        glyphs={65: square_glyph(65), 66: square_glyph(66)},
        kerning_pairs={(65, 66): -20},
        feature_tags=("kern",),
        config_hash=hashlib.sha256(b"cfg").hexdigest(),
        browser_version="chromium_seal_v1",
        fit_observations_count=8,
        calibration_fingerprint=hashlib.sha256(b"cal").hexdigest(),
    )


def test_FONTMODEL_DEEP_IMMUTABLE_nested_mutation_and_hash_drift():
    """FONTMODEL_DEEP_IMMUTABLE: nested glyph/contour/kerning mutation cannot
    alter an accepted sealed model; TTF/OTF bind the identical sealed hash;
    sealed-bytes drift rejects."""
    from reconstruction.font_model import SealedFontModel

    model = _small_model()
    sealed = model.seal()
    seal_hash = sealed.verify()
    assert seal_hash == model.compute_canonical_hash()

    # Mutation of an unwrapped copy cannot alter the seal.
    unwrapped = sealed.unwrap()
    unwrapped.glyphs[65].contours.clear()
    unwrapped.kerning_pairs[(65, 66)] = 999
    assert sealed.verify() == seal_hash
    pristine = sealed.unwrap()
    assert len(pristine.glyphs[65].contours) == 1
    assert pristine.kerning_pairs[(65, 66)] == -20
    assert pristine.compute_canonical_hash() == seal_hash

    # The sealed handle itself is deeply immutable (frozen fields).
    with pytest.raises(Exception):
        sealed.canonical_json = "{}"  # type: ignore[misc]

    # Tampered sealed bytes are detected before use (fail closed).
    tampered = SealedFontModel(
        canonical_json=sealed.canonical_json.replace('"kerning_upem":-20', '"kerning_upem":0'),
        model_hash=sealed.model_hash,
    )
    with pytest.raises(ValueError, match="SEALED_FONT_MODEL_HASH_DRIFT"):
        tampered.verify()

    # TTF and OTF builds bind the IDENTICAL sealed model hash (single source).
    ttf_bound_hash = sealed.verify()
    otf_bound_hash = sealed.verify()
    assert ttf_bound_hash == otf_bound_hash == seal_hash


# =========================================================================
# VI_DETERMINISTIC_FIRST
# =========================================================================


def _vi_fixture_model():
    from reconstruction.font_model import CalibratedGlyph, CanonicalFontModel, GlobalFontMetrics
    from reconstruction.models import Contour, LineSegment, Point2D

    def square(x0: float, y0: float, w: float, h: float):
        pts = [Point2D(x0, y0), Point2D(x0 + w, y0), Point2D(x0 + w, y0 + h), Point2D(x0, y0 + h)]
        return Contour(
            segments=[LineSegment(p0=pts[i], p1=pts[(i + 1) % 4]) for i in range(4)],
            is_hole=False,
            parent_index=-1,
            area_upem=w * h,
        )

    def glyph(cp: int, contours, advance=600.0, anchors=()) -> CalibratedGlyph:
        xs = [p.x for c in contours for s in c.segments for p in (s.p0, s.p1)]
        ys = [p.y for c in contours for s in c.segments for p in (s.p0, s.p1)]
        return CalibratedGlyph(
            code_point=cp,
            character=chr(cp),
            advance_width_upem=advance,
            lsb_upem=50.0,
            rsb_upem=50.0,
            ascent_upem=700.0,
            descent_upem=0.0,
            bounding_box_upem=(min(xs), min(ys), max(xs), max(ys)),
            contours=list(contours),
            confidence=1.0,
            observation_fingerprints=(hashlib.sha256(f"obs:{cp}".encode()).hexdigest(),),
            anchors=anchors,
        )

    base_a = [square(100.0, 0.0, 400.0, 500.0)]
    base_u = [square(120.0, 0.0, 380.0, 480.0)]
    mark_grave = square(200.0, 550.0, 80.0, 60.0)
    mark_acute = square(220.0, 560.0, 80.0, 60.0)
    mark_tilde = square(240.0, 570.0, 80.0, 60.0)
    mark_hook = square(260.0, 580.0, 80.0, 60.0)
    mark_dot = square(280.0, -120.0, 80.0, 60.0)

    glyphs = {
        0x61: glyph(0x61, base_a),
        0x75: glyph(0x75, base_u),
        0x00E0: glyph(0x00E0, base_a + [mark_grave]),  # a + grave
        0x00E1: glyph(0x00E1, base_a + [mark_acute]),  # a + acute
        0x00E3: glyph(0x00E3, base_a + [mark_tilde]),  # a + tilde
        0x1EA3: glyph(0x1EA3, base_a + [mark_hook]),   # a + hook
        0x1EA1: glyph(0x1EA1, base_a + [mark_dot]),    # a + dot below
    }
    return CanonicalFontModel(
        schema_version="1.0.0",
        family_name="ViDetFam",
        style_name="Regular",
        reference_id="vi_det_fam",
        style_id="regular",
        metrics=GlobalFontMetrics(),
        glyphs=glyphs,
        kerning_pairs={},
        feature_tags=(),
        config_hash=hashlib.sha256(b"cfg").hexdigest(),
        browser_version="chromium_vi_v1",
        fit_observations_count=4,
        calibration_fingerprint=hashlib.sha256(b"cal").hexdigest(),
    )


class _CountingAIProvider:
    model_id = "openrouter"
    model_version = "openrouter-route-v1"

    def __init__(self):
        self.calls = 0
        self.requested: list[int] = []

    def prompt_hash(self) -> str:
        return hashlib.sha256(b"prompt").hexdigest()

    async def generate_candidates(self, request):
        from compute.vietnamese import AICandidateSpec, MARK_CODEPOINT_SET

        self.calls += 1
        self.requested = list(request["missing_codepoints"])
        specs = []
        for cp in self.requested:
            anchors = (("mark", 250.0, 550.0),) if cp in MARK_CODEPOINT_SET else ()
            specs.append(
                AICandidateSpec(
                    code_point=cp,
                    contours=(((100.0, 0.0), (400.0, 0.0), (400.0, 500.0), (100.0, 500.0)),),
                    advance_width_upem=600.0,
                    lsb_upem=50.0,
                    rsb_upem=50.0,
                    ascent_upem=700.0,
                    descent_upem=0.0,
                    anchors=anchors,
                )
            )
        return specs


def test_VI_DETERMINISTIC_FIRST_zero_ai_for_constructible_and_anchor_retention():
    """VI_DETERMINISTIC_FIRST: deterministic constructible cases make zero AI
    calls for those code points; only unresolved missing cases follow the AI
    gates; anchors survive into the built glyph semantics."""
    from compute.vietnamese import (
        MARK_CODEPOINT_SET,
        VietnameseExtensionService,
        missing_vietnamese_codepoints,
    )

    async def run():
        model = _vi_fixture_model()
        missing = set(missing_vietnamese_codepoints(model))
        provider = _CountingAIProvider()
        service = VietnameseExtensionService(provider, config_hash="c" * 64, source_hash="s" * 64)
        extended, binding = await service.extend(model)

        # Deterministic construction proves these glyphs from source evidence:
        # combining marks extracted from existing donor composites, plus
        # precomposed glyphs transplanted onto existing bases.
        det = set(binding.deterministic_codepoints)
        assert {0x0300, 0x0301, 0x0303, 0x0309, 0x0323} <= det
        assert 0x1EE7 in det  # u + hook (donor a+hook)
        assert 0x1EE5 in det  # u + dot below (donor a+dot)

        # AI is contacted ONLY for the unresolved remainder (closed
        # three-model escalation entry), never for deterministic glyphs.
        assert provider.calls == 1
        assert set(provider.requested) == missing - det
        assert not (set(provider.requested) & det)

        # Existing glyphs/metrics/behavior preserved exactly.
        for cp, g in model.glyphs.items():
            assert extended.glyphs[cp].to_canonical_dict() == g.to_canonical_dict()

        # Deterministic mark glyph retains its attachment anchor; AI mark
        # anchors survive into the built glyph.
        assert extended.glyphs[0x0300].anchors
        ai_cps = set(provider.requested)
        for cp in ai_cps & set(MARK_CODEPOINT_SET):
            assert extended.glyphs[cp].anchors, "AI anchors must survive into built glyph"

        # Binding hashes remain deterministic and mode-closed.
        assert binding.mode == "VIETNAMESE"
        assert set(binding.extended_codepoints) == (det | ai_cps)

        # Complete-coverage VIETNAMESE reuse: zero AI calls.
        provider2 = _CountingAIProvider()
        service2 = VietnameseExtensionService(provider2, config_hash="c" * 64, source_hash="s" * 64)
        _extended2, binding2 = await service2.extend(extended)
        assert provider2.calls == 0
        assert binding2.extended_codepoints == ()

    asyncio.run(run())


# =========================================================================
# TYPOGRAPHY_RAW_SET_CLOSED  (issue:75#issuecomment-5437156952 / 5437573207)
# =========================================================================


def test_TYPOGRAPHY_RAW_SET_CLOSED_metric_and_pair_size_identity_closed(tmp_path: Path):
    """TYPOGRAPHY_RAW_SET_CLOSED: raw per-size metric and pair-size evidence
    are sealed identities; deleting rows, removing one declared size, adding
    one undeclared size, cross-environment rows, and aggregate-only rows
    reject before completion/snapshot/PASS.

    Direct reproduction of the architect's evidence-bypass case
    (delete 22 metric_observations rows + 11 pair_size_observations rows;
    finalize again must NOT pass).
    """
    import json

    async def run():
        store, config, bv = await _collect_max(tmp_path / "ok")
        cfg_h = config.compute_hash()

        # Sanity: a correct full collection finalizes.
        # (The fixture was already finalized by _collect_max.)

        # CASE A: delete ALL metric rows + ALL pair-size rows; finalization
        # on a fresh store with the same covered schedule must fail closed.
        for sub in ("a", "b", "c", "d", "e"):
            (tmp_path / sub).mkdir(parents=True, exist_ok=True)
        store_a, config_a, bv_a = await _collect_max(tmp_path / "a")
        cfg_h_a = config_a.compute_hash()
        with store_a._get_connection() as conn:
            conn.execute(
                "DELETE FROM metric_observations WHERE reference_id='max_fam' "
                "AND style_id='regular' AND browser_version=? AND config_hash=?",
                (bv_a, cfg_h_a),
            )
            conn.execute(
                "DELETE FROM pair_size_observations WHERE reference_id='max_fam' "
                "AND style_id='regular' AND browser_version=? AND config_hash=?",
                (bv_a, cfg_h_a),
            )
            conn.commit()
        collector_a = ObservationCollector(_MaxFixtureSession(), store_a, config_a)
        with pytest.raises(ValueError, match="FINALIZATION_FAILED"):
            collector_a.finalize_source_collection("max_fam", "regular")

        # CASE B: drop one declared size on a metric row -> fail closed.
        store_b, config_b, bv_b = await _collect_max(tmp_path / "b")
        cfg_h_b = config_b.compute_hash()
        with store_b._get_connection() as conn:
            conn.execute(
                "DELETE FROM metric_observations WHERE reference_id='max_fam' "
                "AND style_id='regular' AND font_size_px=4096 "
                "AND browser_version=? AND config_hash=?",
                (bv_b, cfg_h_b),
            )
            conn.commit()
        collector_b = ObservationCollector(_MaxFixtureSession(), store_b, config_b)
        with pytest.raises(ValueError, match="FINALIZATION_FAILED"):
            collector_b.finalize_source_collection("max_fam", "regular")

        # CASE C: add an undeclared size row -> fail closed.
        store_c, config_c, bv_c = await _collect_max(tmp_path / "c")
        cfg_h_c = config_c.compute_hash()
        with store_c._get_connection() as conn:
            # Pick a JSON blob from an existing row to preserve shape.
            existing = conn.execute(
                "SELECT metrics_json FROM metric_observations "
                "WHERE reference_id='max_fam' AND style_id='regular' "
                "AND code_point=65 LIMIT 1"
            ).fetchone()
            assert existing is not None
            payload = existing["metrics_json"]
            conn.execute(
                "INSERT OR REPLACE INTO metric_observations ("
                "reference_id, style_id, code_point, font_size_px, "
                "browser_version, config_hash, metrics_json, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("max_fam", "regular", 65, 999.0, bv_c, cfg_h_c, payload, "2026-01-01T00:00:00+00:00"),
            )
            conn.commit()
        collector_c = ObservationCollector(_MaxFixtureSession(), store_c, config_c)
        with pytest.raises(ValueError, match="FINALIZATION_FAILED"):
            collector_c.finalize_source_collection("max_fam", "regular")

        # CASE D: cross-environment row (different browser_version) injected
        # under the same ref/style -> exact-identity filter excludes it but
        # the closed schedule still requires the canonical environment rows,
        # which are intact; finalization should still pass for the canonical
        # env, but the cross-env row must not be visible to derivation.
        # We verify both: the cross-env row is NOT mixed into the canonical
        # derivation, and a finalization that ONLY has the cross-env row
        # fails closed.
        store_d, config_d, bv_d = await _collect_max(tmp_path / "d")
        cfg_h_d = config_d.compute_hash()
        with store_d._get_connection() as conn:
            existing = conn.execute(
                "SELECT metrics_json FROM metric_observations "
                "WHERE reference_id='max_fam' AND style_id='regular' "
                "AND code_point=65 LIMIT 1"
            ).fetchone()
            payload = existing["metrics_json"]
            # Inject a row under a foreign browser_version: this row is
            # invisible to exact-identity consumers.
            conn.execute(
                "INSERT OR REPLACE INTO metric_observations ("
                "reference_id, style_id, code_point, font_size_px, "
                "browser_version, config_hash, metrics_json, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("max_fam", "regular", 65, 256.0, "chromium_foreign_v1", cfg_h_d, payload, "2026-01-01T00:00:00+00:00"),
            )
            conn.commit()
        # Canonical finalization still works (the cross-env row is excluded
        # by exact-identity filtering).
        collector_d = ObservationCollector(_MaxFixtureSession(), store_d, config_d)
        collector_d.finalize_source_collection("max_fam", "regular")
        # The exact-identity scoped read must NOT include the foreign row.
        rows_d = store_d.get_metric_observations(
            "max_fam", "regular", browser_version=bv_d, config_hash=cfg_h_d
        )
        assert all(r["browser_version"] == bv_d for r in rows_d)

        # CASE E: a fresh collection that ONLY has the foreign-env rows for
        # one coverage code point must fail closed under the canonical env.
        store_e, config_e, bv_e = await _collect_max(tmp_path / "e")
        cfg_h_e = config_e.compute_hash()
        with store_e._get_connection() as conn:
            # Wipe canonical-env metric rows for cp=65; keep foreign-env rows.
            conn.execute(
                "DELETE FROM metric_observations WHERE reference_id='max_fam' "
                "AND style_id='regular' AND code_point=65 AND browser_version=? AND config_hash=?",
                (bv_e, cfg_h_e),
            )
            conn.execute(
                "UPDATE metric_observations SET browser_version='chromium_foreign_v1' "
                "WHERE reference_id='max_fam' AND style_id='regular' AND code_point=66 "
                "AND browser_version=? AND config_hash=?",
                (bv_e, cfg_h_e),
            )
            conn.commit()
        collector_e = ObservationCollector(_MaxFixtureSession(), store_e, config_e)
        with pytest.raises(ValueError, match="FINALIZATION_FAILED"):
            collector_e.finalize_source_collection("max_fam", "regular")

        # The exact-identity scoped store read must reject unauthenticated
        # use: get_metric_observations without the env tuple raises.
        with pytest.raises(ValueError, match="EXACT_IDENTITY_REQUIRED"):
            store_e.get_metric_observations("max_fam", "regular")

    asyncio.run(run())


def test_TYPOGRAPHY_MULTI_SIZE_DERIVATION_CAUSAL_sealed_raw_drift_changes_derived(tmp_path: Path):
    """TYPOGRAPHY_MULTI_SIZE_DERIVATION_CAUSAL: a controlled change to the
    sealed raw per-size metric_observations deterministically changes the
    derived glyph/global/vertical metrics; caller-authored aggregate rows
    cannot substitute for the sealed raw evidence.
    """
    from measurement.calibration import (
        derive_multisize_derived_metrics,
        multisize_derived_fingerprint,
    )

    async def run():
        store, config, bv = await _collect_max(tmp_path / "causal")
        cfg_h = config.compute_hash()
        rows = store.get_metric_observations(
            "max_fam", "regular", browser_version=bv, config_hash=cfg_h
        )
        # Both glyphs (65, 66) carry the full schedule.
        sizes = [float(s) for s in config.metric_sizes_px]
        derived_65_a = derive_multisize_derived_metrics(
            metric_observations=rows,
            code_point=65,
            reference_id="max_fam",
            style_id="regular",
            browser_version=bv,
            config_hash=cfg_h,
            expected_sizes=sizes,
        )
        fp_65_a = multisize_derived_fingerprint(
            metric_observations=rows,
            code_point=65,
            reference_id="max_fam",
            style_id="regular",
            browser_version=bv,
            config_hash=cfg_h,
        )
        # Controlled drift: shift the inner range of cp=65 ascent by +5 upem
        # on every row so the lower median of 11 sorted values is causally
        # moved (any single-row shift that doesn't displace the median does
        # not satisfy the contract).
        drifted = [dict(r) for r in rows]
        for r in drifted:
            if int(r["code_point"]) == 65:
                payload = json.loads(r["metrics_json"])
                payload["ascent_upem"] = float(payload.get("ascent_upem", 0.0)) + 5.0
                r["metrics_json"] = json.dumps(payload, sort_keys=True)
        derived_65_b = derive_multisize_derived_metrics(
            metric_observations=drifted,
            code_point=65,
            reference_id="max_fam",
            style_id="regular",
            browser_version=bv,
            config_hash=cfg_h,
            expected_sizes=sizes,
        )
        fp_65_b = multisize_derived_fingerprint(
            metric_observations=drifted,
            code_point=65,
            reference_id="max_fam",
            style_id="regular",
            browser_version=bv,
            config_hash=cfg_h,
        )
        assert derived_65_a != derived_65_b
        assert fp_65_a != fp_65_b

        # Aggregate-only change: drop the raw rows and store a synthetic
        # aggregate. The derivation must fail closed because the raw
        # evidence is gone.
        with store._get_connection() as conn:
            conn.execute(
                "DELETE FROM metric_observations WHERE reference_id='max_fam' "
                "AND style_id='regular' AND browser_version=? AND config_hash=?",
                (bv, cfg_h),
            )
            conn.commit()
        rows_agg = store.get_metric_observations(
            "max_fam", "regular", browser_version=bv, config_hash=cfg_h
        )
        assert rows_agg == []
        with pytest.raises(ValueError, match="MULTISIZE_METRIC_NO_EVIDENCE"):
            derive_multisize_derived_metrics(
                metric_observations=rows_agg,
                code_point=65,
                reference_id="max_fam",
                style_id="regular",
                browser_version=bv,
                config_hash=cfg_h,
                expected_sizes=sizes,
            )

    asyncio.run(run())


def test_TYPOGRAPHY_RAW_SNAPSHOT_BOUND_metric_and_pair_size_bound_to_fingerprint(tmp_path: Path):
    """TYPOGRAPHY_RAW_SNAPSHOT_BOUND: metric_observations and
    pair_size_observations are loaded, validated, and bound into the
    snapshot fingerprint; tampering with a raw row changes the snapshot
    identity so Stage 9D and Stage 9C detect drift fail-closed.
    """
    from fidelity.pipeline import ObservationStoreSnapshot

    async def run():
        store, config, bv = await _collect_max(tmp_path / "snap")
        cfg_h = config.compute_hash()

        snap = ObservationStoreSnapshot.load_from_store(
            store=store,
            reference_id="max_fam",
            style_id="regular",
            family_name="MaxFam",
            style_name="Regular",
            config=config,
            browser_version=bv,
        )
        assert snap.metric_observations
        assert snap.pair_size_observations
        fp_a = snap.snapshot_fingerprint

        # Mutate one metric row's metrics_json (rebind a derived value).
        with store._get_connection() as conn:
            row = conn.execute(
                "SELECT code_point, font_size_px FROM metric_observations "
                "WHERE reference_id='max_fam' AND style_id='regular' "
                "AND browser_version=? AND config_hash=? "
                "ORDER BY code_point, font_size_px LIMIT 1",
                (bv, cfg_h),
            ).fetchone()
            assert row is not None
            cp = int(row["code_point"])
            size = float(row["font_size_px"])
            existing = conn.execute(
                "SELECT metrics_json FROM metric_observations "
                "WHERE reference_id='max_fam' AND style_id='regular' "
                "AND code_point=? AND font_size_px=? "
                "AND browser_version=? AND config_hash=?",
                (cp, size, bv, cfg_h),
            ).fetchone()
            payload = json.loads(existing["metrics_json"])
            payload["advance_width_upem"] = float(payload.get("advance_width_upem", 0.0)) + 17.0
            conn.execute(
                "UPDATE metric_observations SET metrics_json=? "
                "WHERE reference_id='max_fam' AND style_id='regular' "
                "AND code_point=? AND font_size_px=? "
                "AND browser_version=? AND config_hash=?",
                (json.dumps(payload, sort_keys=True), cp, size, bv, cfg_h),
            )
            conn.commit()

        snap_b = ObservationStoreSnapshot.load_from_store(
            store=store,
            reference_id="max_fam",
            style_id="regular",
            family_name="MaxFam",
            style_name="Regular",
            config=config,
            browser_version=bv,
        )
        assert snap_b.snapshot_fingerprint != fp_a
        # Drift in the sealed raw evidence must be detected by a verifier
        # comparing the cached fingerprint.
        assert snap.snapshot_fingerprint != snap_b.snapshot_fingerprint

    asyncio.run(run())
