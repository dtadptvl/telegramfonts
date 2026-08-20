import json
import pytest
from pathlib import Path
from benchmark import (
    calculate_percentile,
    make_representative_preview_bytes,
    run_benchmark,
    get_git_sha,
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


def test_get_git_sha():
    sha = get_git_sha()
    assert isinstance(sha, str)
    assert len(sha) > 0


@pytest.mark.asyncio
async def test_run_benchmark_produces_valid_report(tmp_path):
    report = await run_benchmark(sample_count=2, style_count=1, formats=["TTF"])

    assert report.samples_count == 2
    assert report.success_count == 2
    assert report.failure_count == 0
    assert report.p50_total_ms > 0
    assert report.p95_total_ms >= report.p50_total_ms
    assert report.is_production_proof is False
    assert "production capacity proof" in report.disclaimer.lower()
    assert report.capacity_model["target_1000_jobs_day"]["required_consumers"] >= 1
    assert report.capacity_model["target_500_jobs_day"]["required_consumers"] >= 1
