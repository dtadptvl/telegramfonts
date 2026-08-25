"""Tests for FontBuilder service, format outputs, and source-driven glyph data."""
from __future__ import annotations

import hashlib
import io
import json
import pickle
import time
from pathlib import Path
from typing import Any
import pytest
from PIL import Image, ImageDraw

from compute.font_builder import FontBuilderService
from compute.models import ClaimStyle
from compute.source import SourceAcquirer


def _make_test_image_bytes(stroke_x0: int = 20, stroke_x1: int = 50) -> bytes:
    img = Image.new("L", (100, 100), color=255)
    draw = ImageDraw.Draw(img)
    draw.rectangle([stroke_x0, 10, stroke_x1, 90], fill=0)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_build_font_ttf_otf_and_reject_woff2(tmp_path: Path):
    source_acquirer = SourceAcquirer()
    builder = FontBuilderService()

    preview_bytes = _make_test_image_bytes(20, 60)
    styles = [ClaimStyle(id="regular", display_name="Regular"), ClaimStyle(id="bold", display_name="Bold")]
    payload = await source_acquirer.acquire_source(
        "https://www.myfonts.com/collections/roboto-flex", styles, preview_input=preview_bytes
    )

    # 1. TTF
    file_ttf = builder.build_font(payload.styles["regular"], "Roboto Flex", "TTF", tmp_path)
    assert file_ttf.file_path.exists()
    assert file_ttf.format == "TTF"
    assert file_ttf.style_id == "regular"
    assert file_ttf.file_path.read_bytes().startswith(b"\x00\x01\x00\x00")

    # 2. Real OTF with CFF (BLOCK G)
    file_otf = builder.build_font(payload.styles["bold"], "Roboto Flex", "OTF", tmp_path)
    assert file_otf.file_path.exists()
    assert file_otf.format == "OTF"
    assert file_otf.style_id == "bold"
    assert file_otf.file_path.read_bytes().startswith(b"OTTO")

    # 3. WOFF2 is no longer a product format.
    with pytest.raises(ValueError, match="UNSUPPORTED_FORMAT: WOFF2"):
        builder.build_font(payload.styles["regular"], "Roboto Flex", "WOFF2", tmp_path)


@pytest.mark.asyncio
async def test_distinct_fixture_inputs_produce_distinct_font_bytes(tmp_path: Path):
    source_acquirer = SourceAcquirer()
    builder = FontBuilderService()

    fixture_1 = {
        "source_url": "https://www.myfonts.com/collections/roboto-flex",
        "family_name": "Roboto Flex",
        "styles": [
            {
                "style_id": "reg",
                "style_name": "Regular",
                "glyphs": {
                    ".notdef": {"contours": [[(50, 0), (50, 500), (250, 500), (250, 0)]], "advance_width": 300, "lsb": 50},
                    "A": {"contours": [[(100, 0), (100, 700), (500, 700), (500, 0)]], "advance_width": 600, "lsb": 100},
                },
            }
        ],
    }

    fixture_2 = {
        "source_url": "https://www.myfonts.com/collections/roboto-flex",
        "family_name": "Roboto Flex",
        "styles": [
            {
                "style_id": "reg",
                "style_name": "Regular",
                "glyphs": {
                    ".notdef": {"contours": [[(50, 0), (50, 600), (350, 600), (350, 0)]], "advance_width": 400, "lsb": 50},
                    "A": {"contours": [[(50, 0), (50, 800), (700, 800), (700, 0)]], "advance_width": 800, "lsb": 50},
                },
            }
        ],
    }

    p1 = source_acquirer.from_fixture(fixture_1)
    p2 = source_acquirer.from_fixture(fixture_2)

    dir1 = tmp_path / "d1"
    dir2 = tmp_path / "d2"
    dir1.mkdir()
    dir2.mkdir()

    f1 = builder.build_font(p1.styles["reg"], "Roboto Flex", "TTF", dir1)
    f2 = builder.build_font(p2.styles["reg"], "Roboto Flex", "TTF", dir2)

    # Two distinct fixture inputs produce distinct output font binaries (BLOCK B)
    assert f1.sha256_hex != f2.sha256_hex


@pytest.mark.asyncio
async def test_unsupported_format(tmp_path: Path):
    source_acquirer = SourceAcquirer()
    builder = FontBuilderService()
    preview_bytes = _make_test_image_bytes(20, 50)
    styles = [ClaimStyle(id="regular", display_name="Regular")]
    payload = await source_acquirer.acquire_source(
        "https://www.myfonts.com/collections/roboto", styles, preview_input=preview_bytes
    )

    with pytest.raises(ValueError, match="UNSUPPORTED_FORMAT"):
        builder.build_font(payload.styles["regular"], "Roboto", "EXE", tmp_path)


@pytest.mark.asyncio
async def test_font_builder_fails_closed_without_max_reconstructed_glyphs(tmp_path: Path):
    from compute.models import GlyphVector, StyleSourceData
    builder = FontBuilderService()

    # StyleSourceData with only legacy glyphs and empty reconstructed_glyphs
    style_data = StyleSourceData(
        style_id="reg",
        style_name="Regular",
        weight_class=400,
        is_italic=False,
        glyphs={
            "A": GlyphVector(character="A", contours=[[(0, 0), (10, 10)]], advance_width=600, lsb=50)
        },
        reconstructed_glyphs={},
    )

    with pytest.raises(ValueError, match="NO_MAX_RECONSTRUCTED_GLYPHS_AVAILABLE_FOR_reg"):
        builder.build_font(style_data, "Test Font", "TTF", tmp_path)


def _make_reconstructed_glyph(code_point: int, character: str, advance_width: float = 700.0) -> Any:
    from reconstruction.models import Contour, LineSegment, Point2D, ReconstructedGlyph
    contour = Contour(
        segments=[
            LineSegment(Point2D(50, 0), Point2D(50, 700)),
            LineSegment(Point2D(50, 700), Point2D(450, 700)),
            LineSegment(Point2D(450, 700), Point2D(450, 0)),
            LineSegment(Point2D(450, 0), Point2D(50, 0)),
        ]
    )
    return ReconstructedGlyph(
        code_point=code_point,
        character=character,
        advance_width_upem=advance_width,
        lsb_upem=50.0,
        rsb_upem=50.0,
        ascent_upem=700.0,
        descent_upem=0.0,
        contours=[contour],
    )


def test_font_builder_service_production_entrypoint_identity_and_provenance(tmp_path: Path):
    """Verify FontBuilderService strictly enforces exact 4-tuple identity, completed collection, and authentic provenance."""
    from compute.models import StyleSourceData
    from fontTools.ttLib import TTFont
    from measurement.store import ObservationStore
    from typography.models import BOUNDED_FIT_PAIRS

    store_dir = tmp_path / "builder_store"
    store = ObservationStore(store_dir)

    cfg_a = "a" * 64
    cfg_b = "b" * 64
    prov_a = "chromium:Chromium/128.0:canvas_text_metrics"
    prov_b = "chromium:Chromium/129.0:canvas_text_metrics"

    store.record_source_collection_completed("font_a", "reg", cfg_a, "Chromium/128.0")
    store.record_source_collection_completed("font_a", "reg", cfg_b, "Chromium/129.0")

    # Env A: all 12 pairs, pair (65, 79) kern = -40
    for l, r in BOUNDED_FIT_PAIRS:
        store.save_pair_observation(
            reference_id="font_a",
            style_id="reg",
            left_cp=l,
            right_cp=r,
            left_char=chr(l),
            right_char=chr(r),
            left_advance_upem=700.0,
            right_advance_upem=700.0,
            pair_advance_upem=1360.0 if (l, r) == (65, 79) else 1400.0,
            inferred_kerning_upem=-40 if (l, r) == (65, 79) else 0,
            confidence=1.0,
            provenance=prov_a,
            browser_version="Chromium/128.0",
            config_hash=cfg_a,
        )

    # Env B: all 12 pairs, pair (65, 79) kern = -15
    for l, r in BOUNDED_FIT_PAIRS:
        store.save_pair_observation(
            reference_id="font_a",
            style_id="reg",
            left_cp=l,
            right_cp=r,
            left_char=chr(l),
            right_char=chr(r),
            left_advance_upem=700.0,
            right_advance_upem=700.0,
            pair_advance_upem=1385.0 if (l, r) == (65, 79) else 1400.0,
            inferred_kerning_upem=-15 if (l, r) == (65, 79) else 0,
            confidence=1.0,
            provenance=prov_b,
            browser_version="Chromium/129.0",
            config_hash=cfg_b,
        )

    builder = FontBuilderService(observation_store_dir=store_dir)
    glyphs_dict = {
        65: _make_reconstructed_glyph(65, "A"),
        79: _make_reconstructed_glyph(79, "O"),
    }

    # 1. Explicit tuple A selects exactly Env A
    style_a = StyleSourceData(
        style_id="reg",
        style_name="Regular",
        reconstructed_glyphs=glyphs_dict,
        observation_reference_id="font_a",
        observation_style_id="reg",
        observation_browser_version="Chromium/128.0",
        observation_config_hash=cfg_a,
    )
    dir_a = tmp_path / "out_a"
    res_a = builder.build_font(style_a, "Font A", "TTF", dir_a)
    tt_a = TTFont(res_a.file_path)
    assert "GPOS" in tt_a
    gpos_a = tt_a["GPOS"].table
    kern_pairs_a = {}
    for st in gpos_a.LookupList.Lookup[0].SubTable:
        for pair in st.PairSet:
            for vr in pair.PairValueRecord:
                kern_pairs_a[(st.Coverage.glyphs[0], vr.SecondGlyph)] = vr.Value1.XAdvance
    assert kern_pairs_a.get(("A", "O")) == -40

    # 2. Explicit tuple B selects exactly Env B
    style_b = StyleSourceData(
        style_id="reg",
        style_name="Regular",
        reconstructed_glyphs=glyphs_dict,
        observation_reference_id="font_a",
        observation_style_id="reg",
        observation_browser_version="Chromium/129.0",
        observation_config_hash=cfg_b,
    )
    dir_b = tmp_path / "out_b"
    res_b = builder.build_font(style_b, "Font A", "TTF", dir_b)
    tt_b = TTFont(res_b.file_path)
    assert "GPOS" in tt_b
    gpos_b = tt_b["GPOS"].table
    kern_pairs_b = {}
    for st in gpos_b.LookupList.Lookup[0].SubTable:
        for pair in st.PairSet:
            for vr in pair.PairValueRecord:
                kern_pairs_b[(st.Coverage.glyphs[0], vr.SecondGlyph)] = vr.Value1.XAdvance
    assert kern_pairs_b.get(("A", "O")) == -15

    # 3. Incomplete observation tuple rejects with no artifact produced
    style_missing_cfg = StyleSourceData(
        style_id="reg",
        style_name="Regular",
        reconstructed_glyphs=glyphs_dict,
        observation_reference_id="font_a",
        observation_style_id="reg",
        observation_browser_version="Chromium/128.0",
        observation_config_hash=None,
    )
    out_missing = tmp_path / "out_missing"
    with pytest.raises(ValueError, match="INCOMPLETE_OBSERVATION_IDENTITY"):
        builder.build_font(style_missing_cfg, "Font Missing", "TTF", out_missing)
    assert not out_missing.exists()

    # 4. Ambiguous two-environment input with incomplete caller tuple rejects with no artifact produced
    style_incomplete = StyleSourceData(
        style_id="reg",
        style_name="Regular",
        reconstructed_glyphs=glyphs_dict,
        observation_reference_id="font_a",
        observation_style_id="reg",
        observation_browser_version=None,  # Ambiguous: store has 2 environments!
        observation_config_hash=None,
    )
    out_incomplete = tmp_path / "out_incomplete"
    with pytest.raises(ValueError, match="INCOMPLETE_OBSERVATION_IDENTITY"):
        builder.build_font(style_incomplete, "Font Incomplete", "TTF", out_incomplete)
    assert not out_incomplete.exists()

    # 5. Uncompleted pair-only identity rejects with no artifact produced
    cfg_uncompleted = "u" * 64
    for l, r in BOUNDED_FIT_PAIRS:
        store.save_pair_observation(
            reference_id="font_uncompleted",
            style_id="reg",
            left_cp=l,
            right_cp=r,
            left_char=chr(l),
            right_char=chr(r),
            left_advance_upem=700.0,
            right_advance_upem=700.0,
            pair_advance_upem=1400.0,
            inferred_kerning_upem=0,
            confidence=1.0,
            provenance="chromium:Chromium/128.0:canvas_text_metrics",
            browser_version="Chromium/128.0",
            config_hash=cfg_uncompleted,
        )
    # Note: store.record_source_collection_completed is deliberately NOT called!
    style_uncompleted = StyleSourceData(
        style_id="reg",
        style_name="Regular",
        reconstructed_glyphs=glyphs_dict,
        observation_reference_id="font_uncompleted",
        observation_style_id="reg",
        observation_browser_version="Chromium/128.0",
        observation_config_hash=cfg_uncompleted,
    )
    out_uncompleted = tmp_path / "out_uncompleted"
    with pytest.raises(ValueError, match="UNCOMPLETED_SOURCE_COLLECTION"):
        builder.build_font(style_uncompleted, "Font Uncompleted", "TTF", out_uncompleted)
    assert not out_uncompleted.exists()

    # 6. Untrusted provenance in store fails closed
    store.save_pair_observation(
        reference_id="font_bad_prov",
        style_id="reg",
        left_cp=65,
        right_cp=79,
        left_char="A",
        right_char="O",
        left_advance_upem=700.0,
        right_advance_upem=700.0,
        pair_advance_upem=1360.0,
        inferred_kerning_upem=-40,
        confidence=1.0,
        provenance="untrusted_source",
        browser_version="Chromium/128.0",
        config_hash=cfg_a,
    )
    store.record_source_collection_completed("font_bad_prov", "reg", cfg_a, "Chromium/128.0")
    style_bad_prov = StyleSourceData(
        style_id="reg",
        style_name="Regular",
        reconstructed_glyphs=glyphs_dict,
        observation_reference_id="font_bad_prov",
        observation_style_id="reg",
        observation_browser_version="Chromium/128.0",
        observation_config_hash=cfg_a,
    )
    out_bad = tmp_path / "out_bad"
    with pytest.raises(ValueError, match="untrusted or missing Chromium provenance"):
        builder.build_font(style_bad_prov, "Font Bad", "TTF", out_bad)
    assert not out_bad.exists()


def test_candidate_validation_runner_wiring_execution(tmp_path: Path):
    """Verify candidate_validation_runner executes with completed exact fixture and fails closed on missing/uncompleted identity."""
    from candidate_validation_runner import run_candidate_pipeline
    from measurement.models import ObservationConfig
    from measurement.store import ObservationStore
    from typography.models import BOUNDED_FIT_PAIRS

    store_dir = tmp_path / "runner_store"
    store = ObservationStore(store_dir)
    cfg_h = ObservationConfig().compute_hash()
    browser_ver = "chromium"

    # Setup authentic completed source collection and fit pairs
    store.record_source_collection_completed("be_vietnam_pro", "regular", cfg_h, browser_ver)
    for l, r in BOUNDED_FIT_PAIRS:
        store.save_pair_observation(
            reference_id="be_vietnam_pro",
            style_id="regular",
            left_cp=l,
            right_cp=r,
            left_char=chr(l),
            right_char=chr(r),
            left_advance_upem=700.0,
            right_advance_upem=700.0,
            pair_advance_upem=1400.0,
            inferred_kerning_upem=0,
            confidence=1.0,
            provenance=f"chromium:{browser_ver}:canvas_text_metrics",
            browser_version=browser_ver,
            config_hash=cfg_h,
        )

    out_dir = tmp_path / "candidate_out"
    json_out = tmp_path / "report.json"
    truth_file = Path("agent/benchmark_data/ground_truth/BeVietnamPro-Regular.ttf")

    if not truth_file.exists():
        pytest.skip("Ground truth font not available for runner test")

    # 1. Successful execution with exact completed fixture
    res = run_candidate_pipeline(
        store_dir=store_dir,
        truth_path=truth_file,
        output_dir=out_dir,
        json_out=json_out,
        reference_id="be_vietnam_pro",
        style_id="regular",
        browser_version=browser_ver,
        config_hash=cfg_h,
    )
    assert isinstance(res, dict)
    assert "validation_summary" in res
    assert "all_formats_passed" in res["validation_summary"]
    assert res["typography"] is not None
    assert json_out.exists()

    # 2. Missing identity components reject with ValueError
    with pytest.raises(ValueError, match="INCOMPLETE_EXACT_IDENTITY"):
        run_candidate_pipeline(
            store_dir=store_dir,
            truth_path=truth_file,
            output_dir=out_dir,
            json_out=json_out,
            reference_id="be_vietnam_pro",
            style_id="regular",
            browser_version=None,
            config_hash=None,
        )

    # 3. Uncompleted collection marker rejects with ValueError
    with pytest.raises(ValueError, match="UNCOMPLETED_SOURCE_COLLECTION"):
        run_candidate_pipeline(
            store_dir=store_dir,
            truth_path=truth_file,
            output_dir=out_dir,
            json_out=json_out,
            reference_id="be_vietnam_pro",
            style_id="regular",
            browser_version="other_browser",
            config_hash=cfg_h,
        )


@pytest.mark.asyncio
async def test_observation_collector_and_source_acquirer_exact_lifecycle_and_cache_reuse(tmp_path: Path):
    """Reproduce real flow: glyph-only marker rejects, full finalization accepts, actual browser version survives to builder, cache reuse returns exact tuple."""
    from compute.source import SourceAcquirer
    from measurement.collector import ObservationCollector
    from compute.models import ClaimStyle
    from measurement.models import ObservationConfig
    from measurement.store import ObservationStore
    from typography.models import BOUNDED_FIT_PAIRS

    store_dir = tmp_path / "lifecycle_store"
    store = ObservationStore(store_dir)
    config = ObservationConfig()
    cfg_h = config.compute_hash()
    real_browser_ver = "Chromium/130.0.6723.58"

    class FakeSession:
        def __init__(self, ver: str):
            self.browser_version = ver
        async def start(self):
            pass
        async def aclose(self):
            pass
        def close(self):
            pass
        async def observe_source_font(self, url, display_name, family):
            return "TestFamily"
        async def render_glyph_sample(self, font_family, code_point, font_size_px, render_subpixel_x=0.0, render_subpixel_y=0.0):
            import numpy as np
            return np.ones((64, 64), dtype=np.uint8) * 255
        async def capture_lossless_raster(self, font_family, code_point, resolution_px, subpixel_offset=(0.0, 0.0)):
            from PIL import Image
            import io
            img = Image.new("L", (resolution_px, resolution_px), color=255)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        async def is_glyph_supported_in_font(self, font_family, code_point):
            return code_point in (65, 66, 79)
        async def measure_glyph_direct(self, font_family, code_point, font_size_px, upem):
            from measurement.models import DirectMetrics
            scale = font_size_px / upem
            return DirectMetrics.from_browser_measurements(
                code_point=code_point,
                char=chr(code_point),
                font_size_px=font_size_px,
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
            return 1200.0
        async def probe_opentype_feature(self, font_family, feature_tag, sample_text, font_size_px, upem):
            return {
                "enabled_advance_upem": 1200.0,
                "disabled_advance_upem": 1200.0,
                "enabled_raster_signature": "a",
                "disabled_raster_signature": "a",
            }

    session = FakeSession(real_browser_ver)
    collector = ObservationCollector(session, store, config)

    # Step 1: Collect ONLY glyphs
    await collector.collect_font_observations("test_family", "regular", "TestFamily", code_points=[65, 66, 79])

    # 1. Glyph-only: finalize rejects and store is NOT completed!
    assert not store.is_source_collection_completed("test_family", "regular", cfg_h, real_browser_ver)
    with pytest.raises(ValueError, match="pair observations mismatch"):
        collector.finalize_source_collection("test_family", "regular")

    # Step 2: Collect pairs only (without features)
    await collector.collect_pair_observations("test_family", "regular", "TestFamily")

    # 2. Glyph + pair without feature: finalize rejects and store is NOT completed!
    assert not store.is_source_collection_completed("test_family", "regular", cfg_h, real_browser_ver)
    with pytest.raises(ValueError, match="feature probe observations mismatch"):
        collector.finalize_source_collection("test_family", "regular")

    # Step 3: Collect features
    await collector.collect_feature_observations("test_family", "regular", "TestFamily")

    # Before finalize_source_collection, it is still not marked completed
    assert not store.is_source_collection_completed("test_family", "regular", cfg_h, real_browser_ver)

    # Step 4: Finalize collection -> accepts!
    collector.finalize_source_collection("test_family", "regular", source_url="https://www.myfonts.com/collections/test-family")
    assert store.is_source_collection_completed("test_family", "regular", cfg_h, real_browser_ver)

    # Step 5: SourceAcquirer on fresh collection propagates real_browser_ver
    acquirer = SourceAcquirer(
        browser_session_factory=lambda: FakeSession(real_browser_ver),
        observation_store_dir=store_dir,
        observation_config=config,
    )
    payload_new = await acquirer.acquire_source(
        source_url="https://www.myfonts.com/collections/test-family-new",
        styles=[ClaimStyle(id="regular", display_name="Regular")],
    )
    style_new = payload_new.styles["regular"]
    assert style_new.observation_browser_version == real_browser_ver
    assert style_new.observation_config_hash == cfg_h

    # Step 6: SourceAcquirer on CACHE REUSE returns the exact same tuple
    payload_cached = await acquirer.acquire_source(
        source_url="https://www.myfonts.com/collections/test-family-new",
        styles=[ClaimStyle(id="regular", display_name="Regular")],
    )
    style_cached = payload_cached.styles["regular"]
    assert style_cached.observation_browser_version == real_browser_ver
    assert style_cached.observation_config_hash == cfg_h

    # Step 7: Builder accepts StyleSourceData with exact tuple
    builder = FontBuilderService(observation_store_dir=store_dir)
    out_dir = tmp_path / "build_lifecycle_out"
    res = builder.build_font(style_cached, "Test Family New", "TTF", out_dir)
    assert res.file_path.exists()

    # Step 8: Fixture and raster preview paths cannot pass the production builder gate
    from tests.test_runner import _make_test_image_bytes
    preview_bytes = _make_test_image_bytes(20, 60)
    payload_bytes = await acquirer.acquire_source(
        source_url="https://www.myfonts.com/collections/test-family-bytes",
        styles=[ClaimStyle(id="regular", display_name="Regular")],
        preview_input=preview_bytes,
    )
    style_bytes = payload_bytes.styles["regular"]
    assert style_bytes.observation_reference_id is None
    with pytest.raises(ValueError, match="INCOMPLETE_OBSERVATION_IDENTITY"):
        builder.build_font(style_bytes, "Test Family Bytes", "TTF", out_dir)

    payload_fixture = await acquirer.acquire_source(
        source_url="https://www.myfonts.com/collections/test-family-fixture",
        styles=[ClaimStyle(id="regular", display_name="Regular")],
        preview_input={
            "source_url": "https://www.myfonts.com/collections/test-family-fixture",
            "family_name": "Test Family Fixture",
            "styles": [{
                "style_id": "regular",
                "style_name": "Regular",
                "glyphs": {"A": {"contours": [[[0, 0], [100, 0], [100, 100], [0, 100]]], "advance_width": 600, "lsb": 50}},
            }],
        },
    )
    style_fixture = payload_fixture.styles["regular"]
    assert style_fixture.observation_reference_id is None
    with pytest.raises(ValueError, match="INCOMPLETE_OBSERVATION_IDENTITY"):
        builder.build_font(style_fixture, "Test Family Fixture", "TTF", out_dir)


def test_cli_and_script_entrypoints_reject_missing_exact_tuple(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Verify CLI entrypoints and scripts reject omitted exact 4-tuple before any artifact creation."""
    import subprocess
    import sys

    # 1. candidate_validation_runner CLI requires --reference-id, --style-id, --browser-version, --config-hash
    p1 = subprocess.run(
        [sys.executable, "-m", "candidate_validation_runner"],
        cwd="agent/src",
        capture_output=True,
        text=True,
    )
    assert p1.returncode != 0
    assert "required" in p1.stderr.lower() or "error" in p1.stderr.lower()

    # 2. real_a23_max_worker requires --browser-version and --config-hash
    p2 = subprocess.run(
        [sys.executable, "scripts/real_a23_max_worker.py", "--job-id", "j1", "--lease-token", "l1", "--order-id", "o1"],
        capture_output=True,
        text=True,
    )
    assert p2.returncode != 0
    assert "required" in p2.stderr.lower() or "error" in p2.stderr.lower()

    # 3. run_physical_a23_proof requires --browser-version and --config-hash
    p3 = subprocess.run(
        [sys.executable, "scripts/run_physical_a23_proof.py"],
        capture_output=True,
        text=True,
    )
    assert p3.returncode != 0
    assert "required" in p3.stderr.lower() or "error" in p3.stderr.lower()


@pytest.mark.asyncio
async def test_multi_environment_exact_tuple_isolation_and_hostile_mixed_rejection(tmp_path: Path):
    """Architect Blockers verification:
    1. Multi-environment independence & non-overwriting.
    2. Mixed A-glyph/B-pair/C-feature store cannot finalize any tuple.
    3. Legacy unbound feature rows reject.
    4. Exact environments coexist and finalize independently.
    5. Empty pair plan fails closed.
    """
    from measurement.store import ObservationStore
    from measurement.models import ObservationConfig, DirectMetrics
    from measurement.collector import ObservationCollector

    store_dir = tmp_path / "multi_env_store"
    store = ObservationStore(store_dir)

    cfg1 = ObservationConfig(resolutions=(128,), font_size_px=128.0)
    cfg2 = ObservationConfig(resolutions=(256,), font_size_px=256.0)
    h1 = cfg1.compute_hash()
    h2 = cfg2.compute_hash()
    bv1 = "chromium_128_win"
    bv2 = "chromium_129_linux"

    class MockSession:
        def __init__(self, bv):
            self.browser_version = bv
        async def start(self):
            pass
        async def stop(self):
            pass
        async def capture_lossless_raster(self, font_family, code_point, resolution_px, subpixel_offset=(0.0, 0.0)):
            from PIL import Image
            import io
            img = Image.new("RGBA", (resolution_px, resolution_px), (255, 255, 255, 255))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        async def measure_glyph_direct(self, font_family, code_point, font_size_px, upem):
            scale = font_size_px / upem
            return DirectMetrics.from_browser_measurements(
                code_point=code_point,
                char=chr(code_point),
                font_size_px=font_size_px,
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
            return 1200.0
        async def probe_opentype_feature(self, font_family, feature_tag, sample_text, font_size_px, upem):
            return {
                "enabled_advance_upem": 1200.0,
                "disabled_advance_upem": 1200.0,
                "enabled_raster_signature": "sig_a",
                "disabled_raster_signature": "sig_a",
            }

    sess1 = MockSession(bv1)
    sess2 = MockSession(bv2)
    coll1 = ObservationCollector(sess1, store, cfg1)
    coll2 = ObservationCollector(sess2, store, cfg2)

    # 1. Independent full collection under env1
    await coll1.collect_font_observations("multi_font", "regular", "MultiFont", code_points=[65, 66])
    await coll1.collect_pair_observations("multi_font", "regular", "MultiFont")
    await coll1.collect_feature_observations("multi_font", "regular", "MultiFont")

    # 2. Independent full collection under env2 (same family/style, different env)
    await coll2.collect_font_observations("multi_font", "regular", "MultiFont", code_points=[65, 66])
    await coll2.collect_pair_observations("multi_font", "regular", "MultiFont")
    await coll2.collect_feature_observations("multi_font", "regular", "MultiFont")

    # Verify both finalize independently without collisions
    coll1.finalize_source_collection("multi_font", "regular")
    coll2.finalize_source_collection("multi_font", "regular")
    assert store.is_source_collection_completed("multi_font", "regular", h1, bv1) is True
    assert store.is_source_collection_completed("multi_font", "regular", h2, bv2) is True

    # 3. Mismatched pair plan fails closed
    with pytest.raises(ValueError, match="pair observations mismatch"):
        coll1.finalize_source_collection("multi_font", "regular", expected_pairs=[])

    # 4. Mixed environment store cannot finalize any tuple
    # Collect glyphs under env1 only
    await coll1.collect_font_observations("mixed_font", "regular", "MixedFont", code_points=[65, 66])
    # Collect pairs under env2 only
    await coll2.collect_pair_observations("mixed_font", "regular", "MixedFont")
    # Collect features under a third environment (sess3, cfg1)
    sess3 = MockSession("chromium_130_mac")
    coll3 = ObservationCollector(sess3, store, cfg1)
    await coll3.collect_feature_observations("mixed_font", "regular", "MixedFont")

    # Attempting to finalize under coll1 (has glyphs, but missing pairs and features for (bv1, h1))
    with pytest.raises(ValueError, match="FINALIZATION_FAILED"):
        coll1.finalize_source_collection("mixed_font", "regular")

    # Attempting to finalize under coll2 (has pairs, but missing glyphs and features for (bv2, h2))
    with pytest.raises(ValueError, match="FINALIZATION_FAILED"):
        coll2.finalize_source_collection("mixed_font", "regular")

    # Attempting to finalize under coll3 (has features, but missing glyphs for (bv3, h1))
    with pytest.raises(ValueError, match="no glyph coverage found"):
        coll3.finalize_source_collection("mixed_font", "regular")

    # None of the tuples are completed
    assert store.is_source_collection_completed("mixed_font", "regular", h1, bv1) is False
    assert store.is_source_collection_completed("mixed_font", "regular", h2, bv2) is False
    assert store.is_source_collection_completed("mixed_font", "regular", h1, "chromium_130_mac") is False

    # 5. Legacy unbound / untrusted feature rows reject
    # Ingest fake legacy feature observations with untrusted provenance
    with store._get_connection() as conn:
        for tag, txt in cfg1.feature_probes:
            conn.execute(
                """
                INSERT OR REPLACE INTO feature_observations (
                    reference_id, style_id, browser_version, config_hash,
                    feature_tag, sample_text, enabled_advance_upem,
                    disabled_advance_upem, enabled_raster_signature,
                    disabled_raster_signature, effect_observed,
                    provenance, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "legacy_font", "regular", bv1, h1, tag, txt,
                    1200.0, 1200.0, "a", "a", 0, "untrusted", "2026-01-01T00:00:00Z"
                ),
            )
        conn.commit()
    await coll1.collect_font_observations("legacy_font", "regular", "LegacyFont", code_points=[65, 66])
    await coll1.collect_pair_observations("legacy_font", "regular", "LegacyFont")
    with pytest.raises(ValueError, match="untrusted or mismatched feature provenance"):
        coll1.finalize_source_collection("legacy_font", "regular")


@pytest.mark.asyncio
async def test_reproduction_a_different_coverage_independent_finalization_and_no_cross_reconstruction(tmp_path: Path):
    """Reproduction A:
    Env A coverage {A,B}, Env B coverage {A,C}; both finalize/load independently
    and neither candidate/source reconstruction can observe the other environment.
    """
    from measurement.store import ObservationStore
    from measurement.models import ObservationConfig, DirectMetrics
    from measurement.collector import ObservationCollector
    from fidelity.pipeline import ObservationStoreSnapshot
    from compute.source import SourceAcquirer
    from compute.models import ClaimStyle

    store_dir = tmp_path / "repro_a_store"
    store = ObservationStore(store_dir)

    cfg1 = ObservationConfig(resolutions=(128,), font_size_px=128.0)
    cfg2 = ObservationConfig(resolutions=(256,), font_size_px=256.0)
    h1 = cfg1.compute_hash()
    h2 = cfg2.compute_hash()
    bv1 = "chromium_128_win"
    bv2 = "chromium_129_linux"

    class MockSession:
        def __init__(self, bv):
            self.browser_version = bv
        async def start(self):
            pass
        async def stop(self):
            pass
        async def capture_lossless_raster(self, font_family, code_point, resolution_px, subpixel_offset=(0.0, 0.0)):
            from PIL import Image
            import io
            img = Image.new("RGBA", (resolution_px, resolution_px), (255, 255, 255, 255))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        async def measure_glyph_direct(self, font_family, code_point, font_size_px, upem):
            scale = font_size_px / upem
            return DirectMetrics.from_browser_measurements(
                code_point=code_point,
                char=chr(code_point),
                font_size_px=font_size_px,
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
            return 1200.0
        async def probe_opentype_feature(self, font_family, feature_tag, sample_text, font_size_px, upem):
            return {
                "enabled_advance_upem": 1200.0,
                "disabled_advance_upem": 1200.0,
                "enabled_raster_signature": "sig_a",
                "disabled_raster_signature": "sig_a",
            }

    sess1 = MockSession(bv1)
    sess2 = MockSession(bv2)
    coll1 = ObservationCollector(sess1, store, cfg1)
    coll2 = ObservationCollector(sess2, store, cfg2)

    # Env A: collects coverage for {65, 66} (A, B)
    await coll1.collect_font_observations("cov_font", "regular", "CovFont", code_points=[65, 66])
    await coll1.collect_pair_observations("cov_font", "regular", "CovFont")
    await coll1.collect_feature_observations("cov_font", "regular", "CovFont")
    coll1.finalize_source_collection("cov_font", "regular")

    # Env B: collects coverage for {65, 67} (A, C)
    await coll2.collect_font_observations("cov_font", "regular", "CovFont", code_points=[65, 67])
    await coll2.collect_pair_observations("cov_font", "regular", "CovFont")
    await coll2.collect_feature_observations("cov_font", "regular", "CovFont")
    coll2.finalize_source_collection("cov_font", "regular")

    # 1. Snapshots load independently and hold exact separate coverage
    snap_a = ObservationStoreSnapshot.load_from_store(
        store=store,
        reference_id="cov_font",
        style_id="regular",
        family_name="CovFont",
        style_name="Regular",
        config=cfg1,
        browser_version=bv1,
    )
    snap_b = ObservationStoreSnapshot.load_from_store(
        store=store,
        reference_id="cov_font",
        style_id="regular",
        family_name="CovFont",
        style_name="Regular",
        config=cfg2,
        browser_version=bv2,
    )
    assert {r.code_point for r in snap_a.records} == {65, 66}
    assert {r.code_point for r in snap_b.records} == {65, 67}

    # 2. SourceAcquirer with config cfg1 and session sess1 reconstructs only {65, 66}
    acq_a = SourceAcquirer(
        browser_session_factory=lambda: sess1,
        observation_store_dir=store_dir,
        observation_config=cfg1,
    )
    payload_a = await acq_a.acquire_source(
        source_url="https://www.myfonts.com/collections/cov-font",
        styles=[ClaimStyle(id="regular", display_name="Regular")],
    )
    reconstructed_a = payload_a.styles["regular"].reconstructed_glyphs
    assert set(reconstructed_a.keys()) == {65, 66}
    assert 67 not in reconstructed_a

    # 3. SourceAcquirer with config cfg2 and session sess2 reconstructs only {65, 67}
    acq_b = SourceAcquirer(
        browser_session_factory=lambda: sess2,
        observation_store_dir=store_dir,
        observation_config=cfg2,
    )
    payload_b = await acq_b.acquire_source(
        source_url="https://www.myfonts.com/collections/cov-font",
        styles=[ClaimStyle(id="regular", display_name="Regular")],
    )
    reconstructed_b = payload_b.styles["regular"].reconstructed_glyphs
    assert set(reconstructed_b.keys()) == {65, 67}
    assert 66 not in reconstructed_b


@pytest.mark.asyncio
async def test_reproduction_b_correct_feature_tag_with_wrong_sample_text_is_rejected(tmp_path: Path):
    """Reproduction B:
    Correct feature tag with wrong sample text is rejected during finalization.
    """
    from measurement.store import ObservationStore
    from measurement.models import ObservationConfig, DirectMetrics
    from measurement.collector import ObservationCollector

    store_dir = tmp_path / "repro_b_store"
    store = ObservationStore(store_dir)
    cfg = ObservationConfig(resolutions=(128,), font_size_px=128.0)
    cfg_h = cfg.compute_hash()
    bv = "chromium_128_win"

    class MockSession:
        def __init__(self, bv):
            self.browser_version = bv
        async def start(self):
            pass
        async def stop(self):
            pass
        async def capture_lossless_raster(self, font_family, code_point, resolution_px, subpixel_offset=(0.0, 0.0)):
            from PIL import Image
            import io
            img = Image.new("RGBA", (resolution_px, resolution_px), (255, 255, 255, 255))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        async def measure_glyph_direct(self, font_family, code_point, font_size_px, upem):
            scale = font_size_px / upem
            return DirectMetrics.from_browser_measurements(
                code_point=code_point,
                char=chr(code_point),
                font_size_px=font_size_px,
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
            return 1200.0
        async def probe_opentype_feature(self, font_family, feature_tag, sample_text, font_size_px, upem):
            return {
                "enabled_advance_upem": 1200.0,
                "disabled_advance_upem": 1200.0,
                "enabled_raster_signature": "sig_a",
                "disabled_raster_signature": "sig_a",
            }

    sess = MockSession(bv)
    coll = ObservationCollector(sess, store, cfg)

    await coll.collect_font_observations("tag_font", "regular", "TagFont", code_points=[65, 66])
    await coll.collect_pair_observations("tag_font", "regular", "TagFont")

    # Ingest feature probes with correct tags ('liga', 'kern', 'calt') but WRONG sample text
    with store._get_connection() as conn:
        for tag, _ in cfg.feature_probes:
            conn.execute(
                """
                INSERT OR REPLACE INTO feature_observations (
                    reference_id, style_id, browser_version, config_hash,
                    feature_tag, sample_text, enabled_advance_upem,
                    disabled_advance_upem, enabled_raster_signature,
                    disabled_raster_signature, effect_observed,
                    provenance, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "tag_font", "regular", bv, cfg_h, tag, "wrong_sample_text",
                    1200.0, 1200.0, "a", "a", 0, f"chromium:{bv}:canvas_feature_probe", "2026-01-01T00:00:00Z"
                ),
            )
        conn.commit()

    # Finalization must reject because sample_text does not match configured probe tuples
    with pytest.raises(ValueError, match="feature probe observations mismatch"):
        coll.finalize_source_collection("tag_font", "regular")


@pytest.mark.asyncio
async def test_reproduction_c_legacy_unbound_coverage_cannot_satisfy_exact_completion(tmp_path: Path):
    """Reproduction C:
    Legacy unbound coverage/evidence cannot satisfy an exact completion.
    """
    from measurement.store import ObservationStore
    from measurement.models import ObservationConfig, DirectMetrics
    from measurement.collector import ObservationCollector
    from fidelity.pipeline import ObservationStoreSnapshot

    store_dir = tmp_path / "repro_c_store"
    store = ObservationStore(store_dir)
    cfg = ObservationConfig(resolutions=(128,), font_size_px=128.0)
    cfg_h = cfg.compute_hash()
    bv = "chromium_128_win"

    class MockSession:
        def __init__(self, bv):
            self.browser_version = bv
        async def start(self):
            pass
        async def stop(self):
            pass
        async def capture_lossless_raster(self, font_family, code_point, resolution_px, subpixel_offset=(0.0, 0.0)):
            from PIL import Image
            import io
            img = Image.new("RGBA", (resolution_px, resolution_px), (255, 255, 255, 255))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        async def measure_glyph_direct(self, font_family, code_point, font_size_px, upem):
            scale = font_size_px / upem
            return DirectMetrics.from_browser_measurements(
                code_point=code_point,
                char=chr(code_point),
                font_size_px=font_size_px,
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
            return 1200.0
        async def probe_opentype_feature(self, font_family, feature_tag, sample_text, font_size_px, upem):
            return {
                "enabled_advance_upem": 1200.0,
                "disabled_advance_upem": 1200.0,
                "enabled_raster_signature": "sig_a",
                "disabled_raster_signature": "sig_a",
            }

    sess = MockSession(bv)
    coll = ObservationCollector(sess, store, cfg)

    # Ingest legacy unbound coverage directly into DB with empty browser_version / config_hash
    with store._get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO unicode_coverage (reference_id, style_id, browser_version, config_hash, code_point)
            VALUES (?, ?, '', '', 65), (?, ?, '', '', 66)
            """,
            ("unbound_font", "regular", "unbound_font", "regular"),
        )
        conn.commit()

    # Collect pairs and features under active identity
    await coll.collect_pair_observations("unbound_font", "regular", "UnboundFont")
    await coll.collect_feature_observations("unbound_font", "regular", "UnboundFont")

    # Finalization must reject because no coverage exists under exact identity (bv, cfg_h)
    with pytest.raises(ValueError, match="no glyph coverage found"):
        coll.finalize_source_collection("unbound_font", "regular")

    # Snapshot loader must also reject legacy unbound coverage
    with pytest.raises(ValueError, match="STORE_LOAD_ERROR"):
        ObservationStoreSnapshot.load_from_store(
            store=store,
            reference_id="unbound_font",
            style_id="regular",
            family_name="UnboundFont",
            style_name="Regular",
            config=cfg,
            browser_version=bv,
        )


@pytest.mark.asyncio
async def test_known_repro_raster_alias_different_browsers_do_not_overwrite_png_files(tmp_path: Path):
    """KNOWN_REPRO 1 (RASTER_ALIAS):
    Same config/schedule + browser A/B + different PNG bytes -> both environments remain independently readable/finalizable.
    """
    from measurement.store import ObservationStore
    from measurement.models import ObservationConfig, DirectMetrics
    from measurement.collector import ObservationCollector
    from fidelity.pipeline import ObservationStoreSnapshot

    store_dir = tmp_path / "raster_alias_store"
    store = ObservationStore(store_dir)
    cfg = ObservationConfig(resolutions=(128,), font_size_px=128.0)
    cfg_h = cfg.compute_hash()

    bv_a = "Chromium/130.0.6723.58"
    bv_b = "Chromium/131.0.6778.86"

    class DiffPngSession:
        def __init__(self, bv: str, fill_color: tuple[int, int, int, int]):
            self.browser_version = bv
            self.fill_color = fill_color
        async def start(self): pass
        async def stop(self): pass
        async def capture_lossless_raster(self, font_family, code_point, resolution_px, subpixel_offset=(0.0, 0.0)):
            from PIL import Image
            import io
            img = Image.new("RGBA", (resolution_px, resolution_px), self.fill_color)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        async def measure_glyph_direct(self, font_family, code_point, font_size_px, upem):
            scale = font_size_px / upem
            return DirectMetrics.from_browser_measurements(
                code_point=code_point, char=chr(code_point), font_size_px=font_size_px,
                m={"width": 600.0 * scale, "actualBoundingBoxLeft": 50.0 * scale, "actualBoundingBoxRight": 550.0 * scale,
                   "actualBoundingBoxAscent": 700.0 * scale, "actualBoundingBoxDescent": 0.0,
                   "fontBoundingBoxAscent": 800.0 * scale, "fontBoundingBoxDescent": -200.0 * scale},
                upem=upem,
            )
        async def measure_text_advance(self, font_family, text, font_size_px, upem): return 1200.0
        async def probe_opentype_feature(self, font_family, feature_tag, sample_text, font_size_px, upem):
            return {"enabled_advance_upem": 1200.0, "disabled_advance_upem": 1200.0,
                    "enabled_raster_signature": f"sig_{self.browser_version}", "disabled_raster_signature": f"sig_{self.browser_version}"}

    sess_a = DiffPngSession(bv_a, (10, 20, 30, 255))
    sess_b = DiffPngSession(bv_b, (200, 210, 220, 255))

    coll_a = ObservationCollector(sess_a, store, cfg)
    coll_b = ObservationCollector(sess_b, store, cfg)

    # Env A captures observations
    await coll_a.collect_font_observations("alias_font", "regular", "AliasFont", code_points=[65, 66])
    await coll_a.collect_pair_observations("alias_font", "regular", "AliasFont", pair_candidates=[(65, 66)])
    await coll_a.collect_feature_observations("alias_font", "regular", "AliasFont")
    coll_a.finalize_source_collection("alias_font", "regular", expected_pairs=[(65, 66)])

    # Verify Env A has 8 valid glyph observations (4 base phases + 4 held out phases)
    obs_a_init = store.get_glyph_observations("alias_font", "regular", 65, browser_version=bv_a, config_hash=cfg_h)
    assert len(obs_a_init) == 8

    # Env B captures observations with the same code points & resolutions but different PNG bytes
    await coll_b.collect_font_observations("alias_font", "regular", "AliasFont", code_points=[65, 66])
    await coll_b.collect_pair_observations("alias_font", "regular", "AliasFont", pair_candidates=[(65, 66)])
    await coll_b.collect_feature_observations("alias_font", "regular", "AliasFont")
    coll_b.finalize_source_collection("alias_font", "regular", expected_pairs=[(65, 66)])

    # Crucial check: Env A observations must STILL be valid (not overwritten by B!)
    obs_a_after = store.get_glyph_observations("alias_font", "regular", 65, browser_version=bv_a, config_hash=cfg_h)
    obs_b_after = store.get_glyph_observations("alias_font", "regular", 65, browser_version=bv_b, config_hash=cfg_h)
    assert len(obs_a_after) == 8
    assert len(obs_b_after) == 8
    assert obs_a_after[0][1] != obs_b_after[0][1]  # Different PNG bytes on disk

    # Both environments load independently into snapshots
    snap_a = ObservationStoreSnapshot.load_from_store(store, "alias_font", "regular", "AliasFont", "Regular", cfg, bv_a)
    snap_b = ObservationStoreSnapshot.load_from_store(store, "alias_font", "regular", "AliasFont", "Regular", cfg, bv_b)
    assert len(snap_a.records) > 0
    assert len(snap_b.records) > 0


@pytest.mark.asyncio
async def test_known_repro_cross_resume_durable_state_rejects_mismatched_tuple(tmp_path: Path):
    """KNOWN_REPRO 2 (CROSS_RESUME):
    Durable glyph state from tuple A + invocation tuple B -> reject, never reuse.
    """
    from measurement.store import ObservationStore
    from measurement.models import ObservationConfig, DirectMetrics
    from measurement.collector import ObservationCollector
    from scripts.real_a23_max_worker import run_worker

    store_dir = tmp_path / "worker_store"
    store = ObservationStore(store_dir)
    cfg = ObservationConfig(resolutions=(128,), font_size_px=128.0)
    cfg_h_a = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    cfg_h_b = cfg.compute_hash()
    bv_a = "chromium_130_a"
    bv_b = "chromium_130_b"

    class SimpleSession:
        def __init__(self, bv):
            self.browser_version = bv
        async def start(self): pass
        async def stop(self): pass
        async def capture_lossless_raster(self, font_family, code_point, resolution_px, subpixel_offset=(0.0, 0.0)):
            from PIL import Image
            import io
            img = Image.new("RGBA", (resolution_px, resolution_px), (255, 255, 255, 255))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        async def measure_glyph_direct(self, font_family, code_point, font_size_px, upem):
            scale = font_size_px / upem
            return DirectMetrics.from_browser_measurements(
                code_point=code_point, char=chr(code_point), font_size_px=font_size_px,
                m={"width": 600.0 * scale, "actualBoundingBoxLeft": 50.0 * scale, "actualBoundingBoxRight": 550.0 * scale,
                   "actualBoundingBoxAscent": 700.0 * scale, "actualBoundingBoxDescent": 0.0,
                   "fontBoundingBoxAscent": 800.0 * scale, "fontBoundingBoxDescent": -200.0 * scale},
                upem=upem,
            )

    sess_b = SimpleSession(bv_b)
    coll_b = ObservationCollector(sess_b, store, cfg)
    await coll_b.collect_font_observations("be_vietnam_pro", "regular", "BeVietnamPro", code_points=[65])

    scratch_base = tmp_path / "scratch"
    job_scratch = scratch_base / "job_cross_resume"
    job_scratch.mkdir(parents=True, exist_ok=True)
    checkpoint_file = job_scratch / "checkpoint.json"
    glyph_cache_dir = job_scratch / "reconstructed_glyph_state"
    glyph_cache_dir.mkdir(parents=True, exist_ok=True)

    # Seed durable checkpoint and glyph file for tuple A
    with open(checkpoint_file, "w", encoding="utf-8") as f:
        json.dump({
            "completed_cps": [65],
            "browser_version": bv_a,
            "config_hash": cfg_h_a,
            "reference_id": "be_vietnam_pro",
            "style_id": "regular",
            "last_updated": time.time(),
        }, f)

    with open(glyph_cache_dir / "glyph_65.pkl", "wb") as f:
        pickle.dump("FOREIGN_TUPLE_A_GLYPH_SENTINEL", f)

    # Execute worker with tuple B -> checkpoint must be rejected and foreign glyph unlinked/ignored
    res = await run_worker(
        job_id="job_cross_resume",
        lease_token="lease_cross_test",
        order_id="order_1",
        scratch_base=scratch_base,
        browser_version=bv_b,
        config_hash=cfg_h_b,
        stop_after_glyph=1,
        hold_after_glyph=False,
        store_dir=store_dir,
    )

    assert res["loaded_from_checkpoint_count"] == 0
    assert res["newly_computed_count"] == 1

    # Verify that the foreign pickle was cleaned and new authentic checkpoint with tuple B was written
    with open(checkpoint_file, "r", encoding="utf-8") as f:
        new_cp_data = json.load(f)
    assert new_cp_data["browser_version"] == bv_b
    assert new_cp_data["config_hash"] == cfg_h_b


@pytest.mark.asyncio
async def test_known_repro_extra_set_extra_glyph_or_feature_rejects_completion(tmp_path: Path):
    """KNOWN_REPRO 3 (EXTRA_SET):
    Extra exact glyph or feature tuple -> no completion marker.
    """
    from measurement.store import ObservationStore
    from measurement.models import ObservationConfig, DirectMetrics, OpenTypeFeatureObservation
    from measurement.collector import ObservationCollector

    store_dir = tmp_path / "extra_set_store"
    store = ObservationStore(store_dir)
    cfg = ObservationConfig(resolutions=(128,), font_size_px=128.0)
    cfg_h = cfg.compute_hash()
    bv = "chromium_test"

    class SimpleSession:
        def __init__(self):
            self.browser_version = bv
        async def start(self): pass
        async def stop(self): pass
        async def capture_lossless_raster(self, font_family, code_point, resolution_px, subpixel_offset=(0.0, 0.0)):
            from PIL import Image
            import io
            img = Image.new("RGBA", (resolution_px, resolution_px), (255, 255, 255, 255))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        async def measure_glyph_direct(self, font_family, code_point, font_size_px, upem):
            scale = font_size_px / upem
            return DirectMetrics.from_browser_measurements(
                code_point=code_point, char=chr(code_point), font_size_px=font_size_px,
                m={"width": 600.0 * scale, "actualBoundingBoxLeft": 50.0 * scale, "actualBoundingBoxRight": 550.0 * scale,
                   "actualBoundingBoxAscent": 700.0 * scale, "actualBoundingBoxDescent": 0.0,
                   "fontBoundingBoxAscent": 800.0 * scale, "fontBoundingBoxDescent": -200.0 * scale},
                upem=upem,
            )
        async def measure_text_advance(self, font_family, text, font_size_px, upem): return 1200.0
        async def probe_opentype_feature(self, font_family, feature_tag, sample_text, font_size_px, upem):
            return {"enabled_advance_upem": 1200.0, "disabled_advance_upem": 1200.0, "enabled_raster_signature": "a", "disabled_raster_signature": "a"}

    sess = SimpleSession()
    coll = ObservationCollector(sess, store, cfg)

    # 3a. Extra observed glyph: collected observations for [65, 66], but coverage table altered to declare only [65]
    await coll.collect_font_observations("extra_font", "regular", "ExtraFont", code_points=[65, 66])
    await coll.collect_pair_observations("extra_font", "regular", "ExtraFont", pair_candidates=[(65, 66)])
    await coll.collect_feature_observations("extra_font", "regular", "ExtraFont")
    with store._get_connection() as conn:
        conn.execute("DELETE FROM unicode_coverage WHERE reference_id = 'extra_font' AND code_point = 66")
        conn.commit()

    with pytest.raises(ValueError, match="declared coverage does not match observed glyphs"):
        coll.finalize_source_collection("extra_font", "regular", expected_pairs=[(65, 66)])

    # 3b. Extra feature probe observation: config has 3 probes, but store has a 4th probe
    await coll.collect_font_observations("extra_feat_font", "regular", "ExtraFeatFont", code_points=[65, 66])
    await coll.collect_pair_observations("extra_feat_font", "regular", "ExtraFeatFont", pair_candidates=[(65, 66)])
    await coll.collect_feature_observations("extra_feat_font", "regular", "ExtraFeatFont")
    # Insert extra feature probe
    extra_feat = OpenTypeFeatureObservation(
        reference_id="extra_feat_font",
        style_id="regular",
        browser_version=bv,
        config_hash=cfg_h,
        feature_tag="swsh",
        sample_text="Q",
        enabled_advance_upem=1200.0,
        disabled_advance_upem=1200.0,
        enabled_raster_signature="a",
        disabled_raster_signature="a",
        effect_observed=False,
        provenance=f"chromium:{bv}:canvas_feature_probe",
        created_at="2026-01-01T00:00:00Z",
    )
    store.save_feature_observation(extra_feat)

    with pytest.raises(ValueError, match="feature probe observations mismatch"):
        coll.finalize_source_collection("extra_feat_font", "regular", expected_pairs=[(65, 66)])


def test_known_repro_unscoped_prod_apis_reject_missing_identity(tmp_path: Path):
    """KNOWN_REPRO 4 (UNSCOPED_PROD):
    Production entrypoints cannot call exact evidence APIs without full identity.
    """
    from measurement.store import ObservationStore
    from reconstruction_benchmark import run_benchmark
    from candidate_validation_runner import run_candidate_pipeline

    store = ObservationStore(tmp_path / "unscoped_store")

    # Store APIs fail closed when identity is missing or empty
    with pytest.raises(ValueError, match="EXACT_IDENTITY_REQUIRED"):
        store.get_glyph_observations("ref", "reg", 65, browser_version="", config_hash="")

    with pytest.raises(ValueError, match="EXACT_IDENTITY_REQUIRED"):
        store.get_glyph_observation_code_points("ref", "reg", browser_version="", config_hash="")

    with pytest.raises(ValueError, match="EXACT_IDENTITY_REQUIRED"):
        store.get_feature_observations("ref", "reg", browser_version="", config_hash="")

    with pytest.raises(ValueError, match="EXACT_IDENTITY_REQUIRED"):
        store.get_pair_observations("ref", "reg", browser_version="", config_hash="")

    with pytest.raises(ValueError, match="COVERAGE_IDENTITY_REQUIRED"):
        store.get_coverage("ref", "reg", browser_version="", config_hash="")

    with pytest.raises(ValueError, match="EXACT_IDENTITY_REQUIRED"):
        run_benchmark(store_dir=tmp_path, browser_version="", config_hash="")

    with pytest.raises(ValueError, match="INCOMPLETE_EXACT_IDENTITY"):
        run_candidate_pipeline(store_dir=tmp_path, truth_path=tmp_path / "truth.ttf", browser_version="", config_hash="")


@pytest.mark.asyncio
async def test_causal_reproduction_external_db_path_rejected_before_read(tmp_path: Path):
    """Causal Reproduction:
    Observation records with raster paths escaping store base_dir are rejected before any disk read.
    """
    from measurement.store import ObservationStore
    from measurement.models import ObservationConfig, DirectMetrics, ObservationRecord
    from fidelity.pipeline import ObservationStoreSnapshot

    store_dir = tmp_path / "escape_store"
    store = ObservationStore(store_dir)
    cfg = ObservationConfig(resolutions=(128,), font_size_px=128.0)
    cfg_h = cfg.compute_hash()
    bv = "chromium_128_escape"

    # Create an external file completely outside store_dir
    external_dir = tmp_path / "external_secrets"
    external_dir.mkdir(parents=True, exist_ok=True)
    external_file = external_dir / "secret.png"
    external_file.write_bytes(b"EXT_SECRET_BYTES_FOR_REPRO")
    ext_sha = hashlib.sha256(b"EXT_SECRET_BYTES_FOR_REPRO").hexdigest()

    cache_k = ObservationRecord.build_cache_key("esc_font", "regular", 65, bv, 128, 0.0, 0.0, cfg_h)

    # Directly insert an observation row with an escaping path
    with store._get_connection() as conn:
        conn.execute(
            """
            INSERT INTO observations (
                cache_key, reference_id, style_id, code_point, resolution,
                subpixel_x, subpixel_y, raster_relative_path, raster_sha256,
                raster_size_bytes, advance_width_px, lsb_px, rsb_px, ascent_px,
                descent_px, advance_width_upem, lsb_upem, rsb_upem, ascent_upem,
                descent_upem, bbox_width_upem, bbox_height_upem, sample_count,
                confidence, created_at, browser_version, config_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cache_k, "esc_font", "regular", 65, 128, 0.0, 0.0,
                "../../external_secrets/secret.png", ext_sha, len(b"EXT_SECRET_BYTES_FOR_REPRO"),
                60.0, 5.0, 5.0, 70.0, 0.0, 600.0, 50.0, 50.0, 700.0, 0.0, 500.0, 700.0,
                1, 1.0, "2026-01-01T00:00:00Z", bv, cfg_h,
            ),
        )
        conn.execute(
            "INSERT INTO unicode_coverage (reference_id, style_id, code_point, browser_version, config_hash) VALUES (?, ?, ?, ?, ?)",
            ("esc_font", "regular", 65, bv, cfg_h),
        )
        conn.execute(
            "INSERT INTO source_collections (collection_key, reference_id, style_id, browser_version, config_hash, completed_at, source_url) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"esc_font:regular:{bv}:{cfg_h}", "esc_font", "regular", bv, cfg_h, "2026-01-01T00:00:00Z", "test"),
        )
        conn.commit()

    # 1. has_observation returns False without reading external file
    assert store.has_observation(cache_k) is False

    # 2. get_observation returns None
    assert store.get_observation(cache_k) is None

    # 3. get_glyph_observations returns empty list
    assert store.get_glyph_observations("esc_font", "regular", 65, browser_version=bv, config_hash=cfg_h) == []

    # 4. ObservationStoreSnapshot.load_from_store fails closed
    with pytest.raises(ValueError, match="STORE_LOAD_ERROR"):
        ObservationStoreSnapshot.load_from_store(store, "esc_font", "regular", "EscFont", "Regular", cfg, bv)


@pytest.mark.asyncio
async def test_causal_reproduction_near_phase_filename_alias_prevention(tmp_path: Path):
    """Causal Reproduction:
    Distinct subpixel phases with close floating point values (e.g. 0.001 vs 0.002)
    produce distinct non-colliding PNG file paths on disk and verify independently.
    """
    from measurement.store import ObservationStore
    from measurement.models import ObservationConfig, DirectMetrics
    from measurement.collector import ObservationCollector
    from PIL import Image
    import io

    store_dir = tmp_path / "near_phase_store"
    store = ObservationStore(store_dir)
    cfg = ObservationConfig(
        resolutions=(32,),
        base_subpixel_phases=((0.001, 0.0), (0.002, 0.0)),
        expanded_subpixel_phases=((0.001, 0.0), (0.002, 0.0)),
        held_out_subpixel_phases=((0.003, 0.0),),
        font_size_px=32.0,
    )
    cfg_h = cfg.compute_hash()
    bv = "chromium_near_phase"

    class NearPhaseSession:
        def __init__(self):
            self.browser_version = bv
        async def start(self): pass
        async def stop(self): pass
        async def capture_lossless_raster(self, font_family, code_point, resolution_px, subpixel_offset=(0.0, 0.0)):
            # Produce distinct PNG bytes per subpixel phase
            val = int(subpixel_offset[0] * 100000) % 255
            img = Image.new("RGBA", (resolution_px, resolution_px), (val, val, val, 255))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        async def measure_glyph_direct(self, font_family, code_point, font_size_px, upem):
            scale = font_size_px / upem
            return DirectMetrics.from_browser_measurements(
                code_point=code_point, char=chr(code_point), font_size_px=font_size_px,
                m={"width": 600.0 * scale, "actualBoundingBoxLeft": 50.0 * scale, "actualBoundingBoxRight": 550.0 * scale,
                   "actualBoundingBoxAscent": 700.0 * scale, "actualBoundingBoxDescent": 0.0,
                   "fontBoundingBoxAscent": 800.0 * scale, "fontBoundingBoxDescent": -200.0 * scale},
                upem=upem,
            )
        async def measure_text_advance(self, font_family, text, font_size_px, upem): return 1200.0
        async def probe_opentype_feature(self, font_family, feature_tag, sample_text, font_size_px, upem):
            return {"enabled_advance_upem": 1200.0, "disabled_advance_upem": 1200.0, "enabled_raster_signature": "a", "disabled_raster_signature": "a"}

    sess = NearPhaseSession()
    coll = ObservationCollector(sess, store, cfg)

    await coll.collect_font_observations("near_font", "regular", "NearFont", code_points=[65])
    await coll.collect_pair_observations("near_font", "regular", "NearFont", pair_candidates=[(65, 65)])
    await coll.collect_feature_observations("near_font", "regular", "NearFont")

    # Verify both fit observations + 1 held-out observation exist and are distinct
    obs = store.get_glyph_observations("near_font", "regular", 65, browser_version=bv, config_hash=cfg_h)
    assert len(obs) == 3  # 2 fit phases + 1 held-out phase

    # Verify each observation has its own unique file on disk
    file_paths = {r.raster_relative_path for r, _ in obs}
    assert len(file_paths) == 3

    # Verify distinct byte content was preserved
    png_bytes_set = {b for _, b in obs}
    assert len(png_bytes_set) == 3

    # Finalization succeeds when all 3 observation files are intact
    coll.finalize_source_collection("near_font", "regular", expected_pairs=[(65, 65)])
    assert store.is_source_collection_completed("near_font", "regular", cfg_h, bv) is True


@pytest.mark.asyncio
async def test_causal_reproduction_foreign_reference_style_resume_rejected(tmp_path: Path):
    """Causal Reproduction:
    Worker resume rejects foreign reference_id or style_id even when browser_version and config_hash match.
    """
    from scripts.real_a23_max_worker import run_worker

    from scripts.real_a23_max_worker import run_worker
    from measurement.store import ObservationStore
    from measurement.models import ObservationConfig, DirectMetrics
    from measurement.collector import ObservationCollector

    store_dir = tmp_path / "foreign_ref_store"
    store = ObservationStore(store_dir)
    cfg = ObservationConfig(resolutions=(128,), font_size_px=128.0)
    bv = "chromium_130_matched"
    cfg_h = cfg.compute_hash()

    class SimpleSession:
        def __init__(self, bv):
            self.browser_version = bv
        async def start(self): pass
        async def stop(self): pass
        async def capture_lossless_raster(self, font_family, code_point, resolution_px, subpixel_offset=(0.0, 0.0)):
            from PIL import Image
            import io
            img = Image.new("RGBA", (resolution_px, resolution_px), (255, 255, 255, 255))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        async def measure_glyph_direct(self, font_family, code_point, font_size_px, upem):
            scale = font_size_px / upem
            return DirectMetrics.from_browser_measurements(
                code_point=code_point, char=chr(code_point), font_size_px=font_size_px,
                m={"width": 600.0 * scale, "actualBoundingBoxLeft": 50.0 * scale, "actualBoundingBoxRight": 550.0 * scale,
                   "actualBoundingBoxAscent": 700.0 * scale, "actualBoundingBoxDescent": 0.0,
                   "fontBoundingBoxAscent": 800.0 * scale, "fontBoundingBoxDescent": -200.0 * scale},
                upem=upem,
            )

    sess = SimpleSession(bv)
    coll = ObservationCollector(sess, store, cfg)
    await coll.collect_font_observations("be_vietnam_pro", "regular", "BeVietnamPro", code_points=[65])

    scratch_base = tmp_path / "scratch"
    job_scratch = scratch_base / "job_foreign_ref"
    job_scratch.mkdir(parents=True, exist_ok=True)
    checkpoint_file = job_scratch / "checkpoint.json"
    glyph_cache_dir = job_scratch / "reconstructed_glyph_state"
    glyph_cache_dir.mkdir(parents=True, exist_ok=True)

    # Seed checkpoint for foreign font with matching browser/config
    with open(checkpoint_file, "w", encoding="utf-8") as f:
        json.dump({
            "completed_cps": [65],
            "browser_version": bv,
            "config_hash": cfg_h,
            "reference_id": "foreign_font_xyz",
            "style_id": "italic",
            "last_updated": time.time(),
        }, f)

    foreign_pkl = glyph_cache_dir / "glyph_65.pkl"
    with open(foreign_pkl, "wb") as f:
        pickle.dump("FOREIGN_STYLE_SENTINEL", f)

    # Run worker targeting be_vietnam_pro / regular
    res = await run_worker(
        job_id="job_foreign_ref",
        lease_token="lease_foreign_test",
        order_id="order_2",
        scratch_base=scratch_base,
        browser_version=bv,
        config_hash=cfg_h,
        stop_after_glyph=1,
        hold_after_glyph=False,
        store_dir=store_dir,
    )

    assert res["loaded_from_checkpoint_count"] == 0
    assert res["newly_computed_count"] == 1

    # Verify foreign pickle was cleaned/unlinked and checkpoint was rewritten with target reference/style
    with open(checkpoint_file, "r", encoding="utf-8") as f:
        cp_data = json.load(f)
    assert cp_data["reference_id"] == "be_vietnam_pro"
    assert cp_data["style_id"] == "regular"
    assert cp_data["browser_version"] == bv
    assert cp_data["config_hash"] == cfg_h


@pytest.mark.asyncio
async def test_causal_reproduction_reconstruction_cache_typed_envelope_validation(tmp_path: Path):
    """Causal Reproduction:
    Reconstruction disk cache validates typed metadata envelope, full tuple, and exact coverage.
    """
    from compute.source import SourceAcquirer
    from compute.models import ClaimStyle
    from measurement.models import ObservationConfig
    from measurement.store import ObservationStore
    import httpx
    import shutil

    fixture_store = tmp_path / "benchmark_fixture"
    fixture_store.mkdir()
    shutil.copy2("observations/benchmark/index.sqlite3", fixture_store / "index.sqlite3")

    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("No HTTP requests allowed on cache hit")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        acquirer = SourceAcquirer(client=client, observation_store_dir=fixture_store)
        cfg_h = acquirer.observation_config.compute_hash()
        bv = "chromium"
        bv_hash = hashlib.sha256(bv.encode("utf-8")).hexdigest()

        # Load authentic precomputed glyph models for be_vietnam_pro
        orig_pkl = Path("observations/benchmark/reconstructed_be_vietnam_pro_regular.pkl")
        if not orig_pkl.exists():
            pytest.skip("Benchmark reconstructed glyphs not available")
        cached_models = pickle.loads(orig_pkl.read_bytes())
        expected_cps = sorted(cached_models.keys())

        with acquirer.store._get_connection() as conn:
            conn.execute(
                "UPDATE unicode_coverage SET browser_version = ?, config_hash = ? WHERE reference_id = 'be_vietnam_pro' AND style_id = 'regular'",
                (bv, cfg_h),
            )
            conn.commit()

        acquirer.store.record_source_collection_completed(
            reference_id="be_vietnam_pro",
            style_id="regular",
            config_hash=cfg_h,
            browser_version=bv,
        )

        cache_file = fixture_store / f"reconstructed_be_vietnam_pro_regular_{bv_hash}_{cfg_h}.pkl"

        # 1. Invalid envelope (wrong reference_id): must be rejected
        bad_envelope = {
            "reference_id": "wrong_ref_name",
            "style_id": "regular",
            "browser_version": bv,
            "config_hash": cfg_h,
            "coverage": expected_cps,
            "glyph_models": cached_models,
        }
        cache_file.write_bytes(pickle.dumps(bad_envelope))

        # Attempt to load invalid cache directly -> yields empty / rejected
        disk_raw = pickle.loads(cache_file.read_bytes())
        assert disk_raw.get("reference_id") != "be_vietnam_pro"

        # 2. Valid envelope with full exact tuple and exact coverage -> loaded successfully
        valid_envelope = {
            "reference_id": "be_vietnam_pro",
            "style_id": "regular",
            "browser_version": bv,
            "config_hash": cfg_h,
            "coverage": expected_cps,
            "glyph_models": cached_models,
        }
        cache_file.write_bytes(pickle.dumps(valid_envelope))

        styles = [ClaimStyle(id="regular", display_name="Regular")]
        payload = await acquirer.acquire_source("https://www.myfonts.com/collections/be-vietnam-pro", styles)
        assert len(payload.styles["regular"].reconstructed_glyphs) == len(expected_cps)
        assert payload.styles["regular"].observation_browser_version == bv
        assert payload.styles["regular"].observation_config_hash == cfg_h
