"""Tests for FontBuilder service and source-driven font output."""
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

    # TTF
    file_ttf = builder.build_font(payload.styles["regular"], "Roboto Flex", "TTF", tmp_path)
    assert file_ttf.file_path.exists()
    assert file_ttf.format == "TTF"
    assert file_ttf.style_id == "regular"
    assert file_ttf.size_bytes > 0
    assert len(file_ttf.sha256_hex) == 64

    # OTF
    file_otf = builder.build_font(payload.styles["bold"], "Roboto Flex", "OTF", tmp_path)
    assert file_otf.file_path.exists()
    assert file_otf.format == "OTF"
    assert file_otf.style_id == "bold"

    # WOFF2
    file_woff2 = builder.build_font(payload.styles["regular"], "Roboto Flex", "WOFF2", tmp_path)
    assert file_woff2.file_path.exists()
    assert file_woff2.format == "WOFF2"


@pytest.mark.asyncio
async def test_distinct_inputs_produce_distinct_font_bytes(tmp_path: Path):
    source_acquirer = SourceAcquirer()
    builder = FontBuilderService()
    styles = [ClaimStyle(id="regular", display_name="Regular")]

    p1 = await source_acquirer.acquire_source("https://www.myfonts.com/collections/roboto-flex", styles)
    p2 = await source_acquirer.acquire_source("https://www.myfonts.com/collections/helvetica-now", styles)

    dir1 = tmp_path / "d1"
    dir2 = tmp_path / "d2"
    dir1.mkdir()
    dir2.mkdir()

    f1 = builder.build_font(p1.styles["regular"], "Roboto Flex", "TTF", dir1)
    f2 = builder.build_font(p2.styles["regular"], "Helvetica Now", "TTF", dir2)

    # Two distinct inputs produce distinct output font bytes (BLOCK B)
    assert f1.sha256_hex != f2.sha256_hex


@pytest.mark.asyncio
async def test_unsupported_format(tmp_path: Path):
    source_acquirer = SourceAcquirer()
    builder = FontBuilderService()
    styles = [ClaimStyle(id="regular", display_name="Regular")]
    payload = await source_acquirer.acquire_source("https://www.myfonts.com/collections/roboto", styles)

    with pytest.raises(ValueError, match="UNSUPPORTED_FORMAT"):
        builder.build_font(payload.styles["regular"], "Roboto", "EXE", tmp_path)
