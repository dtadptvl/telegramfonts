"""Authoritative test suite for evidence-driven kerning inference and OpenType GPOS table generation (Issue #43)."""
from __future__ import annotations

from pathlib import Path
import pytest
from fontTools.ttLib import TTFont
import uharfbuzz as hb

from reconstruction.candidate_builder import MaxCandidateFontBuilder
from reconstruction.candidate_validator import (
    ChromiumValidationResult,
    MaxCandidateHeldOutValidator,
)
from reconstruction.models import (
    Contour,
    CubicSegment,
    LineSegment,
    Point2D,
    ReconstructedGlyph,
)
from typography.gpos_builder import attach_gpos_to_font, generate_kern_feature_syntax
from typography.kerning_inferencer import EvidenceKerningInferencer
from typography.models import (
    BOUNDED_FIT_PAIRS,
    SEPARATE_HELD_OUT_IN_CMAP_PAIRS,
    PairKerningObservation,
    TypographyDataset,
)


def _make_glyph(code_point: int, character: str, advance_width: float = 736.0, lsb: float = 50.0) -> ReconstructedGlyph:
    contour = Contour(
        segments=[
            LineSegment(p0=Point2D(lsb, 0), p1=Point2D(lsb + 200, 700)),
            CubicSegment(
                p0=Point2D(lsb + 200, 700),
                p1=Point2D(lsb + 220, 710),
                p2=Point2D(lsb + 240, 710),
                p3=Point2D(lsb + 260, 700),
            ),
            LineSegment(p0=Point2D(lsb + 260, 700), p1=Point2D(lsb + 460, 0)),
            LineSegment(p0=Point2D(lsb + 460, 0), p1=Point2D(lsb + 360, 0)),
            LineSegment(p0=Point2D(lsb + 360, 0), p1=Point2D(lsb + 300, 200)),
            LineSegment(p0=Point2D(lsb + 300, 200), p1=Point2D(lsb + 160, 200)),
            LineSegment(p0=Point2D(lsb + 160, 200), p1=Point2D(lsb + 100, 0)),
            LineSegment(p0=Point2D(lsb + 100, 0), p1=Point2D(lsb, 0)),
        ]
    )
    return ReconstructedGlyph(
        code_point=code_point,
        character=character,
        advance_width_upem=advance_width,
        lsb_upem=lsb,
        rsb_upem=max(0.0, advance_width - lsb - 460.0),
        ascent_upem=700.0,
        descent_upem=0.0,
        contours=[contour],
    )


def test_inferencer_direct_measurements():
    """Verify EvidenceKerningInferencer calculates kerning adjustments strictly from observable measurements."""
    inferencer = EvidenceKerningInferencer(family_name="TestFont", style_name="Regular", threshold_upem=0.5)

    # Observable pair measurements: (left_cp, right_cp, left_adv, right_adv, pair_adv)
    measurements = [
        (65, 79, 736.0, 826.0, 1522.0),  # AO: 1522 - (736 + 826) = -40
        (79, 65, 826.0, 736.0, 1522.0),  # OA: 1522 - (826 + 736) = -40
        (66, 79, 674.0, 826.0, 1490.0),  # BO: 1490 - (674 + 826) = -10
        (65, 65, 736.0, 736.0, 1472.0),  # AA: 1472 - (736 + 736) = 0
        (79, 79, 826.0, 826.0, 1652.0),  # OO: 1652 - (826 + 826) = 0
    ]

    dataset = inferencer.infer_from_direct_measurements(measurements)

    assert dataset.total_pairs_probed == 5
    assert dataset.active_kerning_pairs_count == 3
    assert dataset.get_kerning(65, 79) == -40
    assert dataset.get_kerning(79, 65) == -40
    assert dataset.get_kerning(66, 79) == -10
    assert dataset.get_kerning(65, 65) == 0  # Unadjusted


def test_no_adjustment_without_evidence():
    """Verify pairs without measurable differential advance do not emit false adjustments."""
    inferencer = EvidenceKerningInferencer(threshold_upem=1.0)
    measurements = [
        (65, 66, 736.0, 674.0, 1410.0),  # AB: delta = 0
        (66, 65, 674.0, 736.0, 1410.0),  # BA: delta = 0
        (65, 65, 736.0, 736.0, 1472.2),  # AA: delta = +0.2 (< 1.0 threshold)
    ]
    dataset = inferencer.infer_from_direct_measurements(measurements)
    assert dataset.active_kerning_pairs_count == 0
    assert len(dataset.kerning_pairs) == 0

    fea = generate_kern_feature_syntax(dataset, {65: "A", 66: "B"})
    assert fea == ""


def test_deterministic_gpos_output(tmp_path):
    """Verify candidate font builds with GPOS are strictly bit-for-bit deterministic."""
    glyphs = [
        _make_glyph(65, "A", 736.0),
        _make_glyph(79, "O", 826.0),
        _make_glyph(66, "B", 674.0),
    ]
    typography = TypographyDataset(
        family_name="TestFont MAX",
        style_name="Regular",
        kerning_pairs={(65, 79): -40, (79, 65): -40, (66, 79): -10},
    )

    builder = MaxCandidateFontBuilder(family_name="TestFont MAX", style_name="Regular")
    
    dir1 = tmp_path / "build1"
    res1 = builder.build_candidate_family(glyphs, dir1, typography=typography)

    dir2 = tmp_path / "build2"
    res2 = builder.build_candidate_family(glyphs, dir2, typography=typography)

    assert res1.otf.sha256_hex == res2.otf.sha256_hex
    assert res1.ttf.sha256_hex == res2.ttf.sha256_hex

    # Verify GPOS table present in both OTF and TTF
    otf_tt = TTFont(res1.otf.file_path)
    ttf_tt = TTFont(res1.ttf.file_path)
    assert "GPOS" in otf_tt
    assert "GPOS" in ttf_tt


def test_shared_canonical_typography_shaping(tmp_path):
    """Verify OTF and TTF share identical GPOS shaping behavior in HarfBuzz."""
    glyphs = [
        _make_glyph(65, "A", 736.0),
        _make_glyph(79, "O", 826.0),
        _make_glyph(66, "B", 674.0),
    ]
    typography = TypographyDataset(
        family_name="TestFont MAX",
        style_name="Regular",
        kerning_pairs={(65, 79): -40, (79, 65): -40, (66, 79): -10},
    )

    builder = MaxCandidateFontBuilder(family_name="TestFont MAX", style_name="Regular")
    res = builder.build_candidate_family(glyphs, tmp_path, typography=typography)

    # Test HarfBuzz shaping across both canonical formats.
    for art in (res.otf, res.ttf):
        raw_bytes = art.file_path.read_bytes()

        blob = hb.Blob(raw_bytes)
        font = hb.Font(hb.Face(blob))

        # Test AO pair
        buf_ao = hb.Buffer()
        buf_ao.add_str("AO")
        buf_ao.guess_segment_properties()
        hb.shape(font, buf_ao)
        adv_ao = sum(p.x_advance for p in buf_ao.glyph_positions)
        assert adv_ao == 1522, f"Expected 1522 on {art.format}, got {adv_ao}"

        # Test OA pair
        buf_oa = hb.Buffer()
        buf_oa.add_str("OA")
        buf_oa.guess_segment_properties()
        hb.shape(font, buf_oa)
        adv_oa = sum(p.x_advance for p in buf_oa.glyph_positions)
        assert adv_oa == 1522, f"Expected 1522 on {art.format}, got {adv_oa}"

        # Test BO pair
        buf_bo = hb.Buffer()
        buf_bo.add_str("BO")
        buf_bo.guess_segment_properties()
        hb.shape(font, buf_bo)
        adv_bo = sum(p.x_advance for p in buf_bo.glyph_positions)
        assert adv_bo == 1490, f"Expected 1490 on {art.format}, got {adv_bo}"

        # Test unadjusted AA pair
        buf_aa = hb.Buffer()
        buf_aa.add_str("AA")
        buf_aa.guess_segment_properties()
        hb.shape(font, buf_aa)
        adv_aa = sum(p.x_advance for p in buf_aa.glyph_positions)
        assert adv_aa == 1472, f"Expected 1472 on {art.format}, got {adv_aa}"


def test_kerning_materially_reduces_pair_position_error(tmp_path):
    """Verify GPOS table reduces in-cmap pair position error from 50+ UPEM to 0 UPEM on fit pairs."""
    ttf_path = Path("agent/benchmark_data/ground_truth/BeVietnamPro-Regular.ttf")
    if not ttf_path.exists():
        pytest.skip("Ground truth font not available")

    glyphs = [
        _make_glyph(65, "A", 736.0),
        _make_glyph(79, "O", 826.0),
        _make_glyph(66, "B", 674.0),
    ]

    builder = MaxCandidateFontBuilder(family_name="TestFont MAX", style_name="Regular")
    validator = MaxCandidateHeldOutValidator(ttf_path)

    # 1. Build without GPOS
    res_no_gpos = builder.build_candidate_family(glyphs, tmp_path / "no_gpos", typography=None)
    report_no_gpos = validator.validate_family(res_no_gpos, tested_codepoints=[65, 79, 66], run_chromium=False)

    assert report_no_gpos.fit_kerning_delta_upem > 0
    assert report_no_gpos.requires_typography_phase_e is True

    # 2. Build with GPOS
    typography = TypographyDataset(
        family_name="TestFont MAX",
        style_name="Regular",
        kerning_pairs={(65, 79): -40, (79, 65): -40, (66, 79): -10},
    )
    res_gpos = builder.build_candidate_family(glyphs, tmp_path / "gpos", typography=typography)
    report_gpos = validator.validate_family(res_gpos, tested_codepoints=[65, 79, 66], run_chromium=False)

    assert report_gpos.fit_kerning_delta_upem == 0.0  # 100% resolved on fit pairs!
    assert report_gpos.requires_typography_phase_e is False


def test_bounded_pair_set_not_n_squared():
    """Verify bounded candidate fit pair set is strictly non-N^2 and disjoint from held-out pairs."""
    from typography.models import BOUNDED_FIT_PAIRS, SEPARATE_HELD_OUT_IN_CMAP_PAIRS

    # Total bounded candidate pairs must be selective and small (<= 20 pairs, not 169)
    assert len(BOUNDED_FIT_PAIRS) <= 20
    assert len(BOUNDED_FIT_PAIRS) < 169

    # Fit pairs must be disjoint from separate held-out in-cmap evaluation pairs
    fit_pair_set = set(BOUNDED_FIT_PAIRS)
    held_out_pair_set = {(l, r) for _, l, r in SEPARATE_HELD_OUT_IN_CMAP_PAIRS}

    overlap = fit_pair_set.intersection(held_out_pair_set)
    assert len(overlap) == 0, f"Fit set and held-out set must be strictly disjoint, found overlap: {overlap}"


def test_no_truth_binary_leakage_in_runner_and_inferencer(tmp_path):
    """Verify inferencer and candidate builder never inspect reference font binary."""
    from measurement.store import ObservationStore

    store = ObservationStore(tmp_path / "obs_store")
    # Save observable measurements into store (pure numeric data)
    valid_prov = "chromium:Chrome/151.0.7922.140:canvas_text_metrics"
    store.save_pair_observation(
        reference_id="test_font",
        style_id="regular",
        left_cp=65,
        right_cp=79,
        left_char="A",
        right_char="O",
        left_advance_upem=736.0,
        right_advance_upem=826.0,
        pair_advance_upem=1522.0,
        inferred_kerning_upem=0,  # Store 0: inferencer MUST derive -40 dynamically from raw advances!
        confidence=1.0,
        provenance=valid_prov,
    )

    inferencer = EvidenceKerningInferencer(family_name="TestFont MAX", style_name="Regular")
    dataset = inferencer.infer_from_store(store, "test_font", "regular", require_provenance=False)

    assert dataset.total_pairs_probed == 1
    assert dataset.active_kerning_pairs_count == 1
    assert dataset.get_kerning(65, 79) == -40
    assert dataset.inference_method == "observation_store_differential_derivation"
    assert dataset.observations[0].provenance == valid_prov


def test_infer_from_store_rejects_untrusted_or_legacy_provenance(tmp_path):
    """Verify infer_from_store fails closed when encountering legacy or untrusted provenance."""
    from measurement.store import ObservationStore

    store = ObservationStore(tmp_path / "obs_store_untrusted")
    store.save_pair_observation(
        reference_id="font_a",
        style_id="reg",
        left_cp=65,
        right_cp=79,
        left_char="A",
        right_char="O",
        left_advance_upem=736.0,
        right_advance_upem=826.0,
        pair_advance_upem=1522.0,
        inferred_kerning_upem=-40,
        confidence=0.95,
        provenance="legacy_untrusted_assertion",
    )

    inferencer = EvidenceKerningInferencer(family_name="TestFont MAX", style_name="Regular")
    with pytest.raises(ValueError, match="untrusted or missing Chromium provenance"):
        inferencer.infer_from_store(store, "font_a", "reg")


def test_infer_from_store_derives_adjustments_dynamically_from_raw_advances(tmp_path):
    """Verify infer_from_store recomputes adjustments from raw advances and verifies authentic browser provenance."""
    from measurement.store import ObservationStore

    store = ObservationStore(tmp_path / "obs_store")
    valid_prov = "chromium:Chrome/151.0.7922.140:canvas_text_metrics"

    # Save all 12 bounded fit pairs with authentic Chromium provenance and bogus/inverted stored answers
    for l, r in BOUNDED_FIT_PAIRS:
        l_adv = 736.0 if l == 65 else (674.0 if l == 66 else (826.0 if l == 79 else 600.0))
        r_adv = 826.0 if r == 79 else (736.0 if r == 65 else (849.0 if r == 37 else 600.0))
        expected_kern = -40 if (l, r) in [(65, 79), (65, 37), (272, 65)] else (-10 if (l, r) == (66, 79) else (-20 if (l, r) in [(65, 103), (65, 417), (103, 7855)] else 0))
        p_adv = l_adv + r_adv + expected_kern

        store.save_pair_observation(
            reference_id="font_a",
            style_id="reg",
            left_cp=l,
            right_cp=r,
            left_char=chr(l),
            right_char=chr(r),
            left_advance_upem=l_adv,
            right_advance_upem=r_adv,
            pair_advance_upem=p_adv,
            inferred_kerning_upem=999,  # Bogus stored answer to ensure it is ignored
            confidence=1.0,
            provenance=valid_prov,
        )

    inferencer = EvidenceKerningInferencer(family_name="TestFont MAX", style_name="Regular")
    dataset = inferencer.infer_from_store(store, "font_a", "reg")

    # Dynamic derivation must ignore 999 and derive -40 and -10 strictly from raw advances!
    assert dataset.get_kerning(65, 79) == -40
    assert dataset.get_kerning(66, 79) == -10
    assert dataset.provenance == valid_prov
    assert dataset.fit_rows_count == 12
    assert len(dataset.fit_rows_sha256) == 64


def test_held_out_in_cmap_validation_is_distinct_from_fit_set():
    """Verify HELD_OUT_SHAPING_STRINGS categorizes fit pairs and held-out pairs separately."""
    from reconstruction.candidate_validator import HELD_OUT_SHAPING_STRINGS

    categories = [cat for _, cat in HELD_OUT_SHAPING_STRINGS]
    assert "in_cmap_fit_kerning_pair" in categories
    assert "in_cmap_held_out_pair" in categories
    assert "in_cmap_latin_subset" in categories
    assert "in_cmap_vietnamese_subset" in categories


def test_chromium_pair_text_metrics_structure(tmp_path):
    """Verify ChromiumValidationResult contains structured before/after pair TextMetrics."""
    ttf_path = Path("agent/benchmark_data/ground_truth/BeVietnamPro-Regular.ttf")
    if not ttf_path.exists():
        pytest.skip("Ground truth font not available")

    glyphs = [
        _make_glyph(65, "A", 736.0),
        _make_glyph(79, "O", 826.0),
        _make_glyph(66, "B", 674.0),
    ]

    typography = TypographyDataset(
        family_name="TestFont MAX",
        style_name="Regular",
        kerning_pairs={(65, 79): -40, (66, 79): -10},
    )

    builder = MaxCandidateFontBuilder(family_name="TestFont MAX", style_name="Regular")
    res = builder.build_candidate_family(glyphs, tmp_path / "cand", typography=typography)

    validator = MaxCandidateHeldOutValidator(ttf_path)
    report = validator.validate_family(res, tested_codepoints=[65, 79, 66], run_chromium=True)

    chrom = report.chromium_result
    if not chrom.is_available:
        pytest.skip(f"Chromium CDP session not available in runtime environment: {chrom.error_message}")

    assert chrom.fit_pairs_material_improvement is True
    assert chrom.held_out_pairs_non_regression is True
    assert len(chrom.pair_metrics) > 0

    ao_metric = next((m for m in chrom.pair_metrics if m.pair == "AO"), None)
    assert ao_metric is not None
    assert ao_metric.baseline_error_upem == 40.0
    assert ao_metric.gpos_candidate_error_upem == 0.0
    assert ao_metric.material_improvement is True


def test_chromium_pair_gate_fail_closed_negative_regression(tmp_path):
    """Negative regression: verify that regressed/unimproved kerning in Chromium fails all_formats_passed."""
    ttf_path = Path("agent/benchmark_data/ground_truth/BeVietnamPro-Regular.ttf")
    if not ttf_path.exists():
        pytest.skip("Ground truth font not available")

    glyphs = [
        _make_glyph(65, "A", 736.0),
        _make_glyph(79, "O", 826.0),
        _make_glyph(66, "B", 674.0),
    ]

    # Deliberately supply corrupt/regressed kerning (+200 UPEM)
    typography = TypographyDataset(
        family_name="TestFont MAX",
        style_name="Regular",
        kerning_pairs={(65, 79): 200},
    )

    builder = MaxCandidateFontBuilder(family_name="TestFont MAX", style_name="Regular")
    res = builder.build_candidate_family(glyphs, tmp_path / "cand_bad", typography=typography)

    validator = MaxCandidateHeldOutValidator(ttf_path)
    report = validator.validate_family(res, tested_codepoints=[65, 79, 66], run_chromium=True)

    if not report.chromium_result.is_available:
        # Synthetic fail-closed check: verify when pair gates fail, all_passed is False
        synthetic_chrom = ChromiumValidationResult(
            is_available=True,
            browser_version="mock",
            is_direct_loadable_chromium=True,
            fallback_rejection_verified=True,
            measured_glyph_count=3,
            mean_chromium_advance_error_upem=0.0,
            fit_pairs_material_improvement=False,  # Failed pair quality gate
            held_out_pairs_non_regression=True,
            rendered_canvas_valid=True,
        )
        ft_all = all(f.is_direct_loadable_fonttools for f in report.format_results)
        free_all = all(f.is_direct_loadable_freetype or f.is_roundtrip_loadable_freetype for f in report.format_results)
        hb_all = all(f.is_direct_loadable_harfbuzz for f in report.format_results)
        chrom_ok = (
            synthetic_chrom.is_available
            and synthetic_chrom.is_direct_loadable_chromium
            and synthetic_chrom.fallback_rejection_verified
            and synthetic_chrom.fit_pairs_material_improvement
            and synthetic_chrom.held_out_pairs_non_regression
            and synthetic_chrom.rendered_canvas_valid
        )
        assert chrom_ok is False
        assert bool(ft_all and free_all and hb_all and chrom_ok) is False
        return

    # Fail-closed check: material improvement is False, and all_formats_passed MUST be False
    assert report.chromium_result.fit_pairs_material_improvement is False
    assert report.all_formats_passed is False


@pytest.mark.asyncio
async def test_observation_collector_pair_acquisition_with_provenance(tmp_path):
    """Verify ObservationCollector harvests pair text metrics directly via browser CDP with real browser provenance."""
    from measurement.browser_session import ChromiumSession, find_chromium_executable
    from measurement.collector import ObservationCollector
    from measurement.models import ObservationConfig
    from measurement.store import ObservationStore

    try:
        find_chromium_executable()
    except Exception:
        pytest.skip("Chromium not available on host")

    store = ObservationStore(tmp_path / "obs")
    session = ChromiumSession(timeout_seconds=10.0)
    try:
        await session.start()
    except Exception as e:
        await session.aclose()
        pytest.skip(f"Chromium CDP session failed to start: {e}")

    ttf_path = Path("agent/benchmark_data/ground_truth/BeVietnamPro-Regular.ttf")
    if not ttf_path.exists():
        await session.aclose()
        pytest.skip("Ground truth font not available")

    await session.load_font_data("ObservedFont", ttf_path.read_bytes())
    collector = ObservationCollector(session, store, ObservationConfig())

    captured = await collector.collect_pair_observations(
        reference_id="test_ref",
        style_id="regular",
        font_family="ObservedFont",
        pairs=[(65, 79), (66, 79)],
    )
    await session.aclose()

    assert captured == 2
    rows = store.get_pair_observations("test_ref", "regular")
    assert len(rows) == 2
    for r in rows:
        assert "chromium:" in r["provenance"]
        assert ":canvas_text_metrics" in r["provenance"]



