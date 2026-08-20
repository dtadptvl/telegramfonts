"""Tests for FontBuilder service, format outputs, and source-driven glyph data."""
from pathlib import Path
import pytest

from compute.font_builder import FontBuilderService
from compute.source import SourceAcquirer
from worker_client import ClaimStyle


@pytest.mark.asyncio
async def test_build_font_ttf_otf_woff2(tmp_path: Path):
    source_acquirer = SourceAcquirer()
    builder = FontBuilderService()

    styles = [ClaimStyle(id="regular", display_name="Regular"), ClaimStyle(id="bold", display_name="Bold")]
    payload = await source_acquirer.acquire_source("https://www.myfonts.com/collections/roboto-flex", styles)

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
    styles = [ClaimStyle(id="regular", display_name="Regular")]
    payload = await source_acquirer.acquire_source("https://www.myfonts.com/collections/roboto", styles)

    with pytest.raises(ValueError, match="UNSUPPORTED_FORMAT"):
        builder.build_font(payload.styles["regular"], "Roboto", "EXE", tmp_path)
