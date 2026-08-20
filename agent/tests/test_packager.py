"""Tests for packager and manifest generation."""
import json
import zipfile
from pathlib import Path
import pytest

from compute.font_builder import FontBuilderService
from compute.packager import PackagerService


def test_package_job_output_deterministic(tmp_path: Path):
    builder = FontBuilderService()
    packager = PackagerService()

    file_ttf = builder.build_font("Roboto Flex", "Regular", "TTF", tmp_path)
    file_woff2 = builder.build_font("Roboto Flex", "Regular", "WOFF2", tmp_path)

    # First packaging run
    out_dir_1 = tmp_path / "run_1"
    out_dir_1.mkdir()
    manifest_1 = packager.package_job_output(
        job_id="job_123",
        order_id="ord_456",
        family_name="Roboto Flex",
        files=[file_ttf, file_woff2],
        output_dir=out_dir_1,
    )

    # Second packaging run
    out_dir_2 = tmp_path / "run_2"
    out_dir_2.mkdir()
    manifest_2 = packager.package_job_output(
        job_id="job_123",
        order_id="ord_456",
        family_name="Roboto Flex",
        files=[file_ttf, file_woff2],
        output_dir=out_dir_2,
    )

    # Assert byte-for-byte reproducibility
    assert manifest_1.zip_sha256_hex == manifest_2.zip_sha256_hex
    assert manifest_1.zip_size_bytes == manifest_2.zip_size_bytes

    # Assert manifest JSON on disk
    manifest_json_path = out_dir_1 / "manifest.json"
    assert manifest_json_path.exists()
    data = json.loads(manifest_json_path.read_text(encoding="utf-8"))
    assert data["job_id"] == "job_123"
    assert len(data["files"]) == 2

    # Verify ZIP contents
    with zipfile.ZipFile(manifest_1.zip_file_path, "r") as zf:
        namelist = zf.namelist()
        assert "RobotoFlex-Regular.ttf" in namelist
        assert "RobotoFlex-Regular.woff2" in namelist
