import json
import pytest
from pathlib import Path
from benchmark import (
    calculate_percentile,
    make_representative_preview_bytes,
    run_benchmark,
    get_git_metadata,
    get_device_identity,
)


def test_calculate_percentile():
    data = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert calculate_percentile(data, 50) == 30.0
    assert calculate_percentile(data, 0) == 10.0
    assert calculate_percentile(data, 100) == 50.0
    assert calculate_percentile([], 50) == 0.0


def test_make_representative_preview_bytes():
    raw_png = make_representative_preview_bytes()
    assert len(raw_png) > 100
    assert raw_png.startswith(b"\x89PNG\r\n\x1a\n")


def test_get_git_metadata():
    meta = get_git_metadata()
    assert "git_sha" in meta
    assert isinstance(meta["git_is_dirty"], bool)


def test_get_device_identity():
    dev = get_device_identity()
    assert "os" in dev
    assert "architecture" in dev
    assert "cpu_count" in dev
    assert dev["cpu_count"] >= 1


@pytest.mark.asyncio
async def test_run_benchmark_validation_and_bounds():
    with pytest.raises(ValueError, match="sample_count must be >= 1"):
        await run_benchmark(sample_count=0)

    with pytest.raises(ValueError, match="style_count must be >= 1"):
        await run_benchmark(sample_count=1, style_count=0)

    with pytest.raises(ValueError, match="Unsupported format"):
        await run_benchmark(sample_count=1, formats=["INVALID_FMT"])


@pytest.mark.asyncio
async def test_run_benchmark_produces_valid_report(tmp_path):
    report = await run_benchmark(sample_count=2, style_count=1, formats=["TTF"])

    assert report.samples_count == 2
    assert report.success_count == 2
    assert report.failure_count == 0
    assert report.is_valid is True
    assert report.p50_total_ms > 0
    is_android_arm64 = (
        report.device_identity.get("os", "").lower() == "android"
        and report.device_identity.get("architecture", "").lower() in ("aarch64", "arm64")
    )
    assert report.is_production_proof is is_android_arm64
    assert ("production capacity proof" in report.disclaimer.lower() or "authoritative production" in report.disclaimer.lower())
    assert report.capacity_model is not None
    assert report.capacity_model["status"] == "VALID"
    assert report.capacity_model["target_1000_jobs_day"]["required_consumers"] >= 1
    assert report.capacity_model["target_500_jobs_day"]["required_consumers"] >= 1
