"""Tests for FontBuilder service."""
from pathlib import Path
import pytest

from compute.font_builder import FontBuilderService


def test_build_font_ttf_otf_woff2(tmp_path: Path):
    service = FontBuilderService()

    # TTF
    file_ttf = service.build_font("Roboto Flex", "Regular", "TTF", tmp_path)
    assert file_ttf.file_path.exists()
    assert file_ttf.format == "TTF"
    assert file_ttf.size_bytes > 0
    assert len(file_ttf.sha256_hex) == 64

    # OTF
    file_otf = service.build_font("Roboto Flex", "Bold", "OTF", tmp_path)
    assert file_otf.file_path.exists()
    assert file_otf.format == "OTF"

    # WOFF2
    file_woff2 = service.build_font("Roboto Flex", "Regular", "WOFF2", tmp_path)
    assert file_woff2.file_path.exists()
    assert file_woff2.format == "WOFF2"


def test_unsupported_format(tmp_path: Path):
    service = FontBuilderService()
    with pytest.raises(ValueError, match="UNSUPPORTED_FORMAT"):
        service.build_font("Roboto", "Regular", "EXE", tmp_path)
