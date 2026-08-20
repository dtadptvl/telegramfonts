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
async def test_validate_valid_ttf_otf_woff2(tmp_path: Path):
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

    # 3. Valid WOFF2
    woff2_file = builder.build_font(payload.styles["regular"], "Roboto Flex", "WOFF2", tmp_path)
    assert validate_font_file(woff2_file.file_path, "WOFF2") is True


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
