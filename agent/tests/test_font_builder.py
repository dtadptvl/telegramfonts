"""Tests for FontBuilder service, format outputs, and source-driven glyph data."""
from __future__ import annotations

import io
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
    """Verify FontBuilderService selects exact requested 4-tuple environment and fails closed on missing/untrusted identity."""
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

    # 1. Build selecting Env A
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

    # 2. Build selecting Env B
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

    # 3. Missing identity fails closed with no artifact produced
    style_missing = StyleSourceData(
        style_id="reg",
        style_name="Regular",
        reconstructed_glyphs=glyphs_dict,
        observation_reference_id="unknown_font",
        observation_style_id="reg",
    )
    with pytest.raises(ValueError, match="MISSING_OBSERVATION_IDENTITY"):
        builder.build_font(style_missing, "Font Unknown", "TTF", tmp_path / "out_missing")

    # 4. Untrusted provenance in store fails closed
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
    with pytest.raises(ValueError, match="untrusted or missing Chromium provenance"):
        builder.build_font(style_bad_prov, "Font Bad", "TTF", tmp_path / "out_bad")


def test_candidate_validation_runner_wiring_execution(tmp_path: Path):
    """Verify candidate_validation_runner executes without NameError or wiring defects."""
    from candidate_validation_runner import run_candidate_pipeline
    from measurement.store import ObservationStore

    store_dir = tmp_path / "runner_store"
    store = ObservationStore(store_dir)
    out_dir = tmp_path / "candidate_out"
    json_out = tmp_path / "report.json"
    truth_file = Path("agent/benchmark_data/ground_truth/BeVietnamPro-Regular.ttf")

    if not truth_file.exists():
        pytest.skip("Ground truth font not available for runner test")

    res = run_candidate_pipeline(
        store_dir=store_dir,
        truth_path=truth_file,
        output_dir=out_dir,
        json_out=json_out,
        reference_id="be_vietnam_pro",
        style_id="regular",
    )
    assert isinstance(res, dict)
    assert "validation_summary" in res
    assert "all_formats_passed" in res["validation_summary"]
    assert json_out.exists()
