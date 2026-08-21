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


def test_adaptive_subpixel_schedule_rules():
    config = ObservationConfig(
        base_subpixel_phases=((0.0, 0.0),),
        expanded_subpixel_phases=((0.0, 0.0), (0.25, 0.0), (0.5, 0.0), (0.75, 0.0)),
        adaptive_expansion_threshold=0.05,
    )

    # 1. Clean integer metrics (no fractional subpixel uncertainty) -> base schedule
    integer_metrics = DirectMetrics(
        code_point=32,
        character=" ",
        font_size_px=200.0,
        raw_advance_width=50.0,
        raw_actual_left=0.0,
        raw_actual_right=50.0,
        raw_actual_ascent=0.0,
        raw_actual_descent=0.0,
        raw_font_ascent=160.0,
        raw_font_descent=40.0,
        advance_width_upem=250.0,
        lsb_upem=0.0,
        rsb_upem=0.0,
        ascent_upem=0.0,
        descent_upem=0.0,
        bbox_width_upem=250.0,
        bbox_height_upem=0.0,
    )
    assert config.get_phases_for_metrics(integer_metrics) == ((0.0, 0.0),)

    # 2. Fractional metrics with subpixel offset (e.g. 73.6px advance width) -> expanded schedule
    fractional_metrics = DirectMetrics(
        code_point=65,
        character="A",
        font_size_px=200.0,
        raw_advance_width=73.6,
        raw_actual_left=-4.2,
        raw_actual_right=70.1,
        raw_actual_ascent=140.0,
        raw_actual_descent=0.0,
        raw_font_ascent=160.0,
        raw_font_descent=40.0,
        advance_width_upem=368.0,
        lsb_upem=-21.0,
        rsb_upem=17.5,
        ascent_upem=700.0,
        descent_upem=0.0,
        bbox_width_upem=371.5,
        bbox_height_upem=700.0,
    )
    assert config.get_phases_for_metrics(fractional_metrics) == (
        (0.0, 0.0),
        (0.25, 0.0),
        (0.5, 0.0),
        (0.75, 0.0),
    )


@pytest.mark.asyncio
async def test_browser_session_font_restoration_and_fallback_rejection():
    from measurement.browser_session import ChromiumSession, find_chromium_executable

    try:
        find_chromium_executable()
    except Exception:
        pytest.skip("Chromium executable not available on host")

    font_path = Path("agent/benchmark_data/ground_truth/BeVietnamPro-Regular.ttf")
    if not font_path.exists():
        pytest.skip("Benchmark font not downloaded")

    session = ChromiumSession(timeout_seconds=10.0)
    try:
        await session.start()
        font_bytes = font_path.read_bytes()
        await session.load_font_data("BeVietnamTestFont", font_bytes)

        # 1. Verify target font glyph support detection vs fallback
        assert await session.is_glyph_supported_in_font("BeVietnamTestFont", ord("A")) is True
        assert await session.is_glyph_supported_in_font("BeVietnamTestFont", ord("ơ")) is True
        assert await session.is_glyph_supported_in_font("BeVietnamTestFont", ord("đ")) is True
        # Unsupported scripts must be rejected and not accepted as fallback
        assert await session.is_glyph_supported_in_font("BeVietnamTestFont", ord("你")) is False
        assert await session.is_glyph_supported_in_font("BeVietnamTestFont", ord("ع")) is False

        # 2. Force session restart and verify loaded font is durably restored
        await session.restart()
        assert "BeVietnamTestFont" in session._loaded_font_blobs
        assert await session.is_glyph_supported_in_font("BeVietnamTestFont", ord("A")) is True
    finally:
        session.close()


def test_ground_truth_coverage_set_comparison_rejects_equal_count_mismatch(tmp_path):
    """Proves that equal counts with one missing and one extra glyph cannot report 100% coverage."""
    # Expected ground truth: [65, 66, 67, 68, 69] (length 5)
    # Discovered candidate: [65, 66, 67, 68, 999] (length 5: cp 69 missing, cp 999 extra)
    truth_metrics = {
        65: {"advance_width_upem": 600.0, "lsb_upem": 10.0},
        66: {"advance_width_upem": 600.0, "lsb_upem": 10.0},
        67: {"advance_width_upem": 600.0, "lsb_upem": 10.0},
        68: {"advance_width_upem": 600.0, "lsb_upem": 10.0},
        69: {"advance_width_upem": 600.0, "lsb_upem": 10.0},
    }

    truth_set = set(truth_metrics.keys())
    discovered_cps = [65, 66, 67, 68, 999]  # Equal count (5), but mismatched sets!
    discovered_set = set(discovered_cps)

    exact_matches = truth_set & discovered_set
    missing_cps = truth_set - discovered_set
    extra_cps = discovered_set - truth_set

    precision = len(exact_matches) / max(len(discovered_set), 1)
    recall = len(exact_matches) / max(len(truth_set), 1)
    iou_match_rate = len(exact_matches) / max(len(truth_set | discovered_set), 1)

    # Assert that equal counts NEVER falsely report 100% match
    assert len(truth_set) == 5
    assert len(discovered_set) == 5
    assert len(missing_cps) == 1
    assert len(extra_cps) == 1
    assert 69 in missing_cps
    assert 999 in extra_cps
    assert precision == 0.8
    assert recall == 0.8
    assert iou_match_rate < 1.0
    assert iou_match_rate == 4 / 6  # 4 exact matches out of 6 total unique glyphs


