"""U8 VIETNAMESE path fail-closed evidence (T-FAST-ATLAS-ULTRA-01, ADR-0004).

Focused test proving the atlas pipeline's VIETNAMESE path fails CLOSED
without provider keys: deterministic composition first; any glyph class
that would require AI hits the AI gate with ai_provider=None (the pipeline
wires no provider; the local environment carries no wokushop_api_key /
openrouter_api_key) and raises a clean VietnameseAIIntegrityError - never
a network call. The ADR-0003 cascade key NAMES are preserved by name only;
runtime evidence is per ADR-0003.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from atlas.cache import AtlasCacheStore, AtlasCheckpointStore
from atlas.local_fixture import (
    LocalFontMetricsProvider,
    LocalFontRasterProvider,
    fixture_glyph_set,
)
from atlas.pipeline import AtlasStyleSpec, AtlasUltraPipeline
from atlas.policy import AtlasRuntimeDefaults
from compute.vietnamese import VietnameseAIIntegrityError

FIXTURE_FONT = (
    Path(__file__).parent.parent
    / "benchmark_data"
    / "ground_truth"
    / "BeVietnamPro-Regular.ttf"
)


def test_u8_vietnamese_pipeline_fails_closed_without_ai_keys(tmp_path: Path):
    if not FIXTURE_FONT.exists():
        pytest.skip("local fixture font unavailable")

    # Secret boundary: no provider keys in the environment (NAME check only;
    # values are never read). Recorded, not bypassed.
    env_lower = {k.lower() for k in os.environ}
    assert "wokushop_api_key" not in env_lower
    assert "openrouter_api_key" not in env_lower

    cps = fixture_glyph_set(FIXTURE_FONT, limit=12)
    spec = AtlasStyleSpec(
        source_url="fixture://local/be-vietnam-pro",
        family_name="Atlas VN Test Fixture",
        style_name="Regular",
        style_id="regular",
        mode="VIETNAMESE",
        code_points=cps,
    )

    pipeline = AtlasUltraPipeline(
        spec=spec,
        runtime=AtlasRuntimeDefaults(),
        metrics_provider=LocalFontMetricsProvider(FIXTURE_FONT),
        raster_provider=LocalFontRasterProvider(FIXTURE_FONT),
        cache=AtlasCacheStore(tmp_path / "cache"),
        checkpoint_store=AtlasCheckpointStore(tmp_path / "ck"),
        deadline=time.monotonic() + 240,
    )

    # vietnamese=true runs the optional path; with no deterministic donor
    # evidence in this tiny subset, the unresolved classes reach the AI gate
    # where ai_provider is None -> clean fail-closed error, zero AI/network
    # calls.
    with pytest.raises(VietnameseAIIntegrityError, match="VI_AI_PROVIDER_UNAVAILABLE"):
        asyncio.run(pipeline.run())
