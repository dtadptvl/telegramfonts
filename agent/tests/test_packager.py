"""Tests for packager, manifest generation, style_id preservation, and path security."""
import io
import json
import zipfile
from pathlib import Path
import pytest
from PIL import Image, ImageDraw

from compute.font_builder import FontBuilderService
from compute.models import ClaimStyle
from compute.packager import PackagerService
from compute.source import SourceAcquirer


def _make_test_image_bytes(stroke_x0: int = 20, stroke_x1: int = 50) -> bytes:
    img = Image.new("L", (100, 100), color=255)
    draw = ImageDraw.Draw(img)
    draw.rectangle([stroke_x0, 10, stroke_x1, 90], fill=0)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_package_job_output_deterministic(tmp_path: Path):
    source_acquirer = SourceAcquirer()
    builder = FontBuilderService()
    packager = PackagerService()

    preview_bytes = _make_test_image_bytes(20, 60)
    styles = [ClaimStyle(id="rf_regular_id", display_name="Regular")]
    payload = await source_acquirer.acquire_source(
        "https://www.myfonts.com/collections/roboto-flex", styles, preview_input=preview_bytes
    )

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

    preview_bytes = _make_test_image_bytes(20, 50)
    styles = [ClaimStyle(id="rf_regular", display_name="Regular")]
    payload = await source_acquirer.acquire_source(
        "https://www.myfonts.com/collections/roboto-flex", styles, preview_input=preview_bytes
    )
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


@pytest.mark.asyncio
async def test_package_multipart_partitioning_and_naming(tmp_path: Path):
    source_acquirer = SourceAcquirer()
    builder = FontBuilderService()
    packager = PackagerService()

    preview_bytes = _make_test_image_bytes(20, 50)
    styles = [
        ClaimStyle(id="rf_regular", display_name="Regular"),
        ClaimStyle(id="rf_bold", display_name="Bold"),
        ClaimStyle(id="rf_italic", display_name="Italic"),
    ]
    payload = await source_acquirer.acquire_source(
        "https://www.myfonts.com/collections/roboto-flex", styles, preview_input=preview_bytes
    )

    f1 = builder.build_font(payload.styles["rf_regular"], "Roboto Flex", "TTF", tmp_path)
    f2 = builder.build_font(payload.styles["rf_bold"], "Roboto Flex", "TTF", tmp_path)
    f3 = builder.build_font(payload.styles["rf_italic"], "Roboto Flex", "TTF", tmp_path)

    out_dir = tmp_path / "multipart_out"
    out_dir.mkdir()

    # Use a small per-part cap to trigger multipart bin-packing across 3 files
    # Each file is ~2-3 KB, so 3500 bytes forces partition into 2 or 3 parts
    manifest = packager.package_job_output(
        job_id="job_multi_1",
        order_id="ord_multi_1",
        family_name="Roboto Flex",
        files=[f1, f2, f3],
        output_dir=out_dir,
        max_part_bytes=3500,
    )

    assert len(manifest.parts) > 1
    assert manifest.parts[0].filename == f"roboto_flex_ord_multi_1_part-01-of-{len(manifest.parts):02d}.zip"
    assert manifest.parts[1].filename == f"roboto_flex_ord_multi_1_part-02-of-{len(manifest.parts):02d}.zip"

    # Verify all parts exist, are valid ZIPs, and contain all font files without duplicates
    seen_fonts = set()
    for part in manifest.parts:
        part_path = Path(part.file_path)
        assert part_path.exists()
        assert part.size_bytes == part_path.stat().st_size
        with zipfile.ZipFile(part_path, "r") as zf:
            names = zf.namelist()
            assert len(names) > 0
            for name in names:
                assert name not in seen_fonts, f"Duplicate font {name} found across parts"
                seen_fonts.add(name)

    assert "RobotoFlex-Regular.ttf" in seen_fonts
    assert "RobotoFlex-Bold.ttf" in seen_fonts
    assert "RobotoFlex-Italic.ttf" in seen_fonts


@pytest.mark.asyncio
async def test_package_individual_file_oversize_fails_closed(tmp_path: Path):
    source_acquirer = SourceAcquirer()
    builder = FontBuilderService()
    packager = PackagerService()

    preview_bytes = _make_test_image_bytes(20, 50)
    styles = [ClaimStyle(id="rf_regular", display_name="Regular")]
    payload = await source_acquirer.acquire_source(
        "https://www.myfonts.com/collections/roboto-flex", styles, preview_input=preview_bytes
    )
    f1 = builder.build_font(payload.styles["rf_regular"], "Roboto Flex", "TTF", tmp_path)

    out_dir = tmp_path / "oversize_out"
    out_dir.mkdir()

    # Pass max_part_bytes smaller than the single font file
    with pytest.raises(ValueError, match="INDIVIDUAL_FONT_FILE_EXCEEDS_CAP"):
        packager.package_job_output(
            job_id="job_oversize",
            order_id="ord_oversize",
            family_name="Roboto Flex",
            files=[f1],
            output_dir=out_dir,
            max_part_bytes=100,  # 100 bytes is smaller than ~2KB font file
        )

