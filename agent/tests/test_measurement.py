"""Tests for MAX Pipeline Phase A: Observation, Direct Browser Metrics, and Ground-Truth Benchmark Foundation."""
from __future__ import annotations

import asyncio
import io
import json
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from measurement.benchmark_runner import GroundTruthBenchmarkRunner, get_peak_rss_mb
from measurement.discovery import ObservableGlyphDiscovery
from measurement.manifest import create_reproducibility_manifest
from measurement.models import DirectMetrics, ObservationConfig, ObservationRecord
from measurement.store import ObservationStore


def test_observation_config_hash_determinism():
    c1 = ObservationConfig(resolutions=(128, 256), font_size_px=200.0)
    c2 = ObservationConfig(resolutions=(128, 256), font_size_px=200.0)
    c3 = ObservationConfig(resolutions=(128, 512), font_size_px=200.0)

    assert c1.compute_hash() == c2.compute_hash()
    assert c1.compute_hash() != c3.compute_hash()
    assert len(c1.compute_hash()) == 64


def test_reproducibility_manifest_generation():
    config = ObservationConfig()
    manifest = create_reproducibility_manifest(config, chromium_version="Chrome/133.0.0.0")

    assert manifest.os_name != ""
    assert manifest.python_version != ""
    assert manifest.chromium_version == "Chrome/133.0.0.0"
    assert manifest.fonttools_version != ""
    assert manifest.config_hash == config.compute_hash()
    assert "timestamp" in manifest.to_dict()


def test_direct_metrics_normalization():
    # Mock browser TextMetrics for glyph 'A' (code point 65) at 200px font size
    raw_m = {
        "width": 130.0,
        "actualBoundingBoxLeft": 2.0,
        "actualBoundingBoxRight": 128.0,
        "actualBoundingBoxAscent": 140.0,
        "actualBoundingBoxDescent": 0.0,
        "fontBoundingBoxAscent": 160.0,
        "fontBoundingBoxDescent": 40.0,
    }
    # With font_size_px=200, scale to UPEM=1000 is 1000/200 = 5.0
    metrics = DirectMetrics.from_browser_measurements(
        code_point=65,
        char="A",
        font_size_px=200.0,
        m=raw_m,
        upem=1000,
    )

    assert metrics.code_point == 65
    assert metrics.character == "A"
    assert metrics.advance_width_upem == 650.0  # 130.0 * 5.0
    assert metrics.lsb_upem == -10.0            # -2.0 * 5.0
    assert metrics.rsb_upem == 10.0             # (130 - 128) * 5.0
    assert metrics.ascent_upem == 700.0         # 140.0 * 5.0
    assert metrics.descent_upem == 0.0


def test_observation_store_persistence_and_resume(tmp_path: Path):
    store = ObservationStore(tmp_path)
    config = ObservationConfig()
    manifest = create_reproducibility_manifest(config, "Chrome/133.0.0.0")
    store.save_manifest(manifest)

    # Verify manifest table
    with store._get_connection() as conn:
        row = conn.execute("SELECT * FROM manifests").fetchone()
        assert row is not None
        assert row["chromium_version"] == "Chrome/133.0.0.0"

    # Save Unicode coverage
    store.save_coverage("ref1", "regular", [65, 66, 67])
    assert store.get_coverage("ref1", "regular") == [65, 66, 67]

    # Create dummy PNG raster
    img = Image.new("RGBA", (128, 128), color=(255, 255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    metrics = DirectMetrics(
        code_point=65,
        character="A",
        font_size_px=200.0,
        raw_advance_width=130.0,
        raw_actual_left=2.0,
        raw_actual_right=128.0,
        raw_actual_ascent=140.0,
        raw_actual_descent=0.0,
        raw_font_ascent=160.0,
        raw_font_descent=40.0,
        advance_width_upem=650.0,
        lsb_upem=-10.0,
        rsb_upem=10.0,
        ascent_upem=700.0,
        descent_upem=0.0,
        bbox_width_upem=650.0,
        bbox_height_upem=700.0,
    )

    cache_key = ObservationRecord.build_cache_key(
        reference_id="ref1",
        style_id="regular",
        code_point=65,
        browser_version="Chrome/133",
        resolution=128,
        subpixel_x=0.0,
        subpixel_y=0.0,
        config_hash=config.compute_hash(),
    )

    rec = ObservationRecord(
        cache_key=cache_key,
        reference_id="ref1",
        style_id="regular",
        code_point=65,
        resolution=128,
        subpixel_x=0.0,
        subpixel_y=0.0,
        raster_relative_path="ref1/regular/0041/128px_0.00_0.00.png",
        raster_sha256="dummy_sha256",
        raster_size_bytes=len(png_bytes),
        metrics=metrics,
        created_at="2026-08-21T00:00:00Z",
    )

    # Initial check -> not present
    assert not store.has_observation(cache_key)

    # Save
    store.save_observation(rec, png_bytes)

    # Second check -> present (resume works!)
    assert store.has_observation(cache_key)
    loaded = store.get_observation(cache_key)
    assert loaded is not None
    assert loaded.metrics.advance_width_upem == 650.0
    assert (tmp_path / "ref1/regular/0041/128px_0.00_0.00.png").exists()


@pytest.mark.asyncio
async def test_dynamic_observable_glyph_discovery_convergence():
    # Simulate candidate pool where first 5 candidates are present, next 10 are absent
    candidates = [65, 66, 67, 68, 69] + list(range(100, 150))
    present_set = {65, 66, 67, 68, 69}

    def measure_fn(cp: int) -> bool:
        return cp in present_set

    # Set max_consecutive_misses=5 to test dynamic convergence termination
    discovered = await ObservableGlyphDiscovery.discover_observable_glyphs(
        measure_fn=measure_fn,
        candidate_code_points=candidates,
        max_consecutive_misses=5,
    )

    assert discovered == [65, 66, 67, 68, 69]


def test_ground_truth_metrics_loader():
    font_path = Path("agent/benchmark_data/ground_truth/BeVietnamPro-Regular.ttf")
    if not font_path.exists():
        pytest.skip("Benchmark font not downloaded")

    runner = GroundTruthBenchmarkRunner(
        ground_truth_font_path=font_path,
        output_dir="tmp_bench",
    )
    truth = runner.load_ground_truth_metrics()

    assert len(truth) > 400
    # Check ASCII 'A' (65) and Vietnamese 'Đ' (0x0110)
    assert 65 in truth
    assert 0x0110 in truth
    assert truth[65]["advance_width_upem"] > 0
