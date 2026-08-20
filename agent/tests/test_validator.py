"""Tests for font file validation."""
from pathlib import Path
import pytest

from compute.font_builder import FontBuilderService
from compute.validator import validate_font_file


def test_validate_valid_fonts(tmp_path: Path):
    builder = FontBuilderService()

    ttf_file = builder.build_font("Roboto Flex", "Regular", "TTF", tmp_path)
    assert validate_font_file(ttf_file.file_path, "TTF") is True

    woff2_file = builder.build_font("Roboto Flex", "Regular", "WOFF2", tmp_path)
    assert validate_font_file(woff2_file.file_path, "WOFF2") is True


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
