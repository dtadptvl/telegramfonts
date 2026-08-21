"""Tests for FontBuilder service, format outputs, and source-driven glyph data."""
import io
from pathlib import Path
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
async def test_build_font_ttf_otf_woff2(tmp_path: Path):
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

    # 3. WOFF2
    file_woff2 = builder.build_font(payload.styles["regular"], "Roboto Flex", "WOFF2", tmp_path)
    assert file_woff2.file_path.exists()
    assert file_woff2.format == "WOFF2"
    assert file_woff2.file_path.read_bytes().startswith(b"wOF2")


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


def test_polygon_signed_area_and_winding_direction():
    from compute.font_builder import ensure_winding_direction, polygon_signed_area
    import numpy as np

    # CCW polygon (positive area in Cartesian coords)
    ccw_poly = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]
    # CW polygon (negative area)
    cw_poly = [(0.0, 0.0), (0.0, 100.0), (100.0, 100.0), (100.0, 0.0)]

    area_ccw = polygon_signed_area(ccw_poly)
    area_cw = polygon_signed_area(cw_poly)
    assert area_ccw > 0
    assert area_cw < 0

    # TrueType: Outer must be CW (negative area), Hole must be CCW (positive area)
    ttf_outer = ensure_winding_direction(ccw_poly, is_outer=True, is_ttf=True)
    assert polygon_signed_area(ttf_outer) < 0

    ttf_hole = ensure_winding_direction(cw_poly, is_outer=False, is_ttf=True)
    assert polygon_signed_area(ttf_hole) > 0

    # CFF / OTF: Outer must be CCW (positive area), Hole must be CW (negative area)
    cff_outer = ensure_winding_direction(cw_poly, is_outer=True, is_ttf=False)
    assert polygon_signed_area(cff_outer) > 0

    cff_hole = ensure_winding_direction(ccw_poly, is_outer=False, is_ttf=False)
    assert polygon_signed_area(cff_hole) < 0


def test_font_builder_full_cmap_construction(tmp_path: Path):
    from fontTools.ttLib import TTFont
    from compute.models import GlyphContour, GlyphVector, StyleSourceData

    builder = FontBuilderService()

    # Create style source with custom glyphs and cmap (e.g. A, B, and Vietnamese character 'Đ' = 0x0110)
    glyphs = {
        ".notdef": GlyphVector(character=".notdef", code_point=0, contours=[]),
        "uni0041": GlyphVector(character="uni0041", code_point=0x41, contours=[GlyphContour(points=[(10, 10), (10, 80), (80, 80), (80, 10)], is_outer=True)]),
        "uni0042": GlyphVector(character="uni0042", code_point=0x42, contours=[GlyphContour(points=[(10, 10), (10, 80), (80, 80), (80, 10)], is_outer=True)]),
        "uni0110": GlyphVector(character="uni0110", code_point=0x0110, contours=[GlyphContour(points=[(10, 10), (10, 80), (80, 80), (80, 10)], is_outer=True)]),
    }
    cmap = {0x41: "uni0041", 0x42: "uni0042", 0x0110: "uni0110"}

    style_data = StyleSourceData(
        style_id="reg",
        style_name="Regular",
        glyphs=glyphs,
        cmap=cmap,
    )

    ttf_file = builder.build_font(style_data, "Custom Test", "TTF", tmp_path)
    assert ttf_file.file_path.exists()

    font = TTFont(ttf_file.file_path)
    font_cmap = font.getBestCmap()
    assert 0x41 in font_cmap
    assert 0x42 in font_cmap
    assert 0x0110 in font_cmap
    assert font_cmap[0x0110] == "uni0110"

