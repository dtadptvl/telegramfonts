"""Tests for packager, manifest generation, style_id preservation, and path security."""
import json
import zipfile
from pathlib import Path
import pytest

from compute.font_builder import FontBuilderService
from compute.packager import PackagerService
from compute.source import SourceAcquirer
from worker_client import ClaimStyle


@pytest.mark.asyncio
async def test_package_job_output_deterministic(tmp_path: Path):
    source_acquirer = SourceAcquirer()
    builder = FontBuilderService()
    packager = PackagerService()

    styles = [ClaimStyle(id="rf_regular_id", display_name="Regular")]
    payload = await source_acquirer.acquire_source("https://www.myfonts.com/collections/roboto-flex", styles)

    file_ttf = builder.build_font(payload.styles["rf_regular_id"], "Roboto Flex", "TTF", tmp_path)
    file_woff2 = builder.build_font(payload.styles["rf_regular_id"], "Roboto Flex", "WOFF2", tmp_path)

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

    # Assert manifest JSON preserves claim style_id (BLOCK C)
    manifest_json_path = out_dir_1 / "manifest.json"
    assert manifest_json_path.exists()
    data = json.loads(manifest_json_path.read_text(encoding="utf-8"))
    assert data["job_id"] == "job_123"
    assert data["files"][0]["style_id"] == "rf_regular_id"
    assert data["files"][1]["style_id"] == "rf_regular_id"

    # Verify ZIP contents
    with zipfile.ZipFile(manifest_1.zip_file_path, "r") as zf:
        namelist = zf.namelist()
        assert "RobotoFlex-Regular.ttf" in namelist
        assert "RobotoFlex-Regular.woff2" in namelist


@pytest.mark.asyncio
async def test_package_sanitizes_pathlike_order_id(tmp_path: Path):
    source_acquirer = SourceAcquirer()
    builder = FontBuilderService()
    packager = PackagerService()

    styles = [ClaimStyle(id="rf_regular", display_name="Regular")]
    payload = await source_acquirer.acquire_source("https://www.myfonts.com/collections/roboto-flex", styles)
    file_ttf = builder.build_font(payload.styles["rf_regular"], "Roboto Flex", "TTF", tmp_path)

    out_dir = tmp_path / "safe_dir"
    out_dir.mkdir()

    # Pass path-like traversal order_id
    manifest = packager.package_job_output(
        job_id="job_123",
        order_id="../../etc/passwd",
        family_name="Roboto Flex",
        files=[file_ttf],
        output_dir=out_dir,
    )

    # Assert output zip is strictly within out_dir and sanitized (BLOCK E)
    assert manifest.zip_file_path.exists()
    assert str(manifest.zip_file_path).startswith(str(out_dir))
    assert ".." not in manifest.zip_filename
