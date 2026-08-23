"""Tests for font file validation with format-distinct assertions."""
import io
from pathlib import Path
import pytest
from PIL import Image, ImageDraw

from compute.font_builder import FontBuilderService
from compute.models import ClaimStyle
from compute.source import SourceAcquirer
from compute.validator import validate_font_file


def _make_test_image_bytes(stroke_x0: int = 20, stroke_x1: int = 50) -> bytes:
    img = Image.new("L", (100, 100), color=255)
    draw = ImageDraw.Draw(img)
    draw.rectangle([stroke_x0, 10, stroke_x1, 90], fill=0)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_validate_valid_ttf_otf_and_reject_woff2(tmp_path: Path):
    source_acquirer = SourceAcquirer()
    builder = FontBuilderService()

    preview_bytes = _make_test_image_bytes(20, 60)
    styles = [ClaimStyle(id="regular", display_name="Regular")]
    payload = await source_acquirer.acquire_source(
        "https://www.myfonts.com/collections/roboto-flex", styles, preview_input=preview_bytes
    )

    # 1. Valid TTF
    ttf_file = builder.build_font(payload.styles["regular"], "Roboto Flex", "TTF", tmp_path)
    assert validate_font_file(ttf_file.file_path, "TTF") is True

    # 2. Valid OTF (real OpenType CFF)
    otf_file = builder.build_font(payload.styles["regular"], "Roboto Flex", "OTF", tmp_path)
    assert validate_font_file(otf_file.file_path, "OTF") is True

    # 3. WOFF2 is rejected even if its magic is present.
    woff2_file = tmp_path / "unsupported.woff2"
    woff2_file.write_bytes(b"wOF2unsupported")
    assert validate_font_file(woff2_file, "WOFF2") is False


@pytest.mark.asyncio
async def test_otf_validator_rejects_renamed_ttf_file(tmp_path: Path):
    source_acquirer = SourceAcquirer()
    builder = FontBuilderService()

    preview_bytes = _make_test_image_bytes(20, 60)
    styles = [ClaimStyle(id="regular", display_name="Regular")]
    payload = await source_acquirer.acquire_source(
        "https://www.myfonts.com/collections/roboto-flex", styles, preview_input=preview_bytes
    )

    # Build real TTF file
    ttf_file = builder.build_font(payload.styles["regular"], "Roboto Flex", "TTF", tmp_path)
    assert validate_font_file(ttf_file.file_path, "TTF") is True

    # Rename TTF file to .otf
    fake_otf = tmp_path / "FakeOTF.otf"
    fake_otf.write_bytes(ttf_file.file_path.read_bytes())

    # Validating fake OTF as OTF must FAIL (BLOCK G: rejects renamed TTF)
    assert validate_font_file(fake_otf, "OTF") is False


@pytest.mark.asyncio
async def test_ttf_validator_rejects_renamed_otf_file(tmp_path: Path):
    source_acquirer = SourceAcquirer()
    builder = FontBuilderService()

    preview_bytes = _make_test_image_bytes(20, 60)
    styles = [ClaimStyle(id="regular", display_name="Regular")]
    payload = await source_acquirer.acquire_source(
        "https://www.myfonts.com/collections/roboto-flex", styles, preview_input=preview_bytes
    )

    # Build real OTF file
    otf_file = builder.build_font(payload.styles["regular"], "Roboto Flex", "OTF", tmp_path)
    assert validate_font_file(otf_file.file_path, "OTF") is True

    # Rename OTF file to .ttf
    fake_ttf = tmp_path / "FakeTTF.ttf"
    fake_ttf.write_bytes(otf_file.file_path.read_bytes())

    # Validating fake TTF as TTF must FAIL
    assert validate_font_file(fake_ttf, "TTF") is False


def test_validate_corrupt_or_empty_file(tmp_path: Path):
    # Empty file
    empty_file = tmp_path / "empty.ttf"
    empty_file.touch()
    assert validate_font_file(empty_file, "TTF") is False

    # Corrupt file
    corrupt_file = tmp_path / "corrupt.ttf"
    corrupt_file.write_bytes(b"\x00\x01\x00\x00corrupted_garbage_bytes")
    assert validate_font_file(corrupt_file, "TTF") is False

    # Wrong magic bytes
    wrong_magic = tmp_path / "wrong.woff2"
    wrong_magic.write_bytes(b"NOT_WOFF2_MAGIC")
    assert validate_font_file(wrong_magic, "WOFF2") is False


@pytest.mark.asyncio
async def test_validator_rejects_missing_name_or_zero_os2_metrics(tmp_path: Path):
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    # Build a font that has missing NameID 3 and 4, and zero OS/2 metrics (the old bug)
    fb = FontBuilder(unitsPerEm=1024, isTTF=True)
    fb.setupGlyphOrder([".notdef", "space", "A"])
    fb.setupCharacterMap({0x20: "space", 0x41: "A"})
    pen = TTGlyphPen(None)
    pen.moveTo((50, 0))
    pen.lineTo((50, 700))
    pen.lineTo((450, 700))
    pen.closePath()
    g = pen.glyph()
    g_empty = TTGlyphPen(None).glyph()
    fb.setupGlyf({".notdef": g, "space": g_empty, "A": g})
    fb.setupHorizontalMetrics({".notdef": (500, 50), "space": (250, 0), "A": (500, 50)})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": "BrokenFont", "styleName": "Regular", "psName": "BrokenFont-Regular"})
    fb.setupOS2(usWeightClass=400)
    fb.setupPost()

    broken_ttf = tmp_path / "broken.ttf"
    fb.save(broken_ttf)

    # Must be rejected by strengthened validator
    assert validate_font_file(broken_ttf, "TTF") is False


@pytest.mark.asyncio
async def test_built_fonts_pass_independent_load_and_contain_required_records(tmp_path: Path):
    from fontTools.ttLib import TTFont

    source_acquirer = SourceAcquirer()
    builder = FontBuilderService()

    preview_bytes = _make_test_image_bytes(20, 60)
    styles = [ClaimStyle(id="regular", display_name="Regular"), ClaimStyle(id="bold", display_name="Bold")]
    payload = await source_acquirer.acquire_source(
        "https://www.myfonts.com/collections/roboto-flex", styles, preview_input=preview_bytes
    )

    for fmt in ("TTF", "OTF"):
        font_file = builder.build_font(payload.styles["regular"], "Roboto Flex", fmt, tmp_path)
        assert validate_font_file(font_file.file_path, fmt) is True

        # Verify Name records
        tt = TTFont(font_file.file_path)
        name_ids = {n.nameID for n in tt["name"].names}
        assert {1, 2, 3, 4, 6}.issubset(name_ids)

        # Verify OS/2 metrics
        os2 = tt["OS/2"]
        assert os2.usWinAscent > 0
        assert os2.usWinDescent > 0
        assert os2.sTypoAscender != 0
        assert os2.usWeightClass > 0
        tt.close()
