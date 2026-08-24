"""Observation collector orchestrating direct browser metric extraction and multi-resolution raster storage."""
from __future__ import annotations

import datetime
import hashlib
import logging
import time
from typing import Callable

from measurement.browser_session import ChromiumSession
from measurement.discovery import ObservableGlyphDiscovery
from measurement.manifest import create_reproducibility_manifest
from measurement.models import (
    BrowserFontSelection,
    MetricObservation,
    ObservationConfig,
    ObservationRecord,
    OpenTypeFeatureObservation,
)
from measurement.store import ObservationStore

logger = logging.getLogger("telegramfonts.agent.measurement.collector")


class ObservationCollector:
    """Orchestrates persistent Chromium measurement session and immutable observation storage."""

    def __init__(
        self,
        session: ChromiumSession,
        store: ObservationStore,
        config: ObservationConfig | None = None,
    ) -> None:
        self.session = session
        self.store = store
        self.config = config or ObservationConfig()

    async def initialize(self) -> None:
        """Initialize browser session and store manifest."""
        await self.session.start()
        manifest = create_reproducibility_manifest(
            config=self.config,
            chromium_version=self.session.browser_version,
        )
        self.store.save_manifest(manifest)

    async def collect_font_observations(
        self,
        reference_id: str,
        style_id: str,
        font_family: str | BrowserFontSelection,
        code_points: list[int] | None = None,
        progress_cb: Callable[[int, int], None] | None = None,
    ) -> tuple[int, int, float]:
        """Collect direct browser metrics and multi-resolution raster observations for a font style.
        
        Returns:
            (glyphs_count, total_observations_captured_or_resumed, elapsed_time_seconds)
        """
        start_time = time.perf_counter()
        config_hash = self.config.compute_hash()

        # If code_points not explicitly supplied, discover observable glyphs dynamically using authoritative discovery
        if code_points is None:
            code_points = await ObservableGlyphDiscovery.discover_observable_glyphs(
                measure_fn=lambda cp: self.session.is_glyph_supported_in_font(font_family, cp),
            )

        self.store.save_coverage(reference_id, style_id, code_points)

        total_rasters = 0
        total_glyphs = len(code_points)

        for idx, cp in enumerate(code_points, start=1):
            # 1. Direct browser metric measurements across the canonical size schedule.
            metrics_by_size = {}
            for metric_size in self.config.metric_sizes_px:
                measured = await self.session.measure_glyph_direct(
                    font_family=font_family,
                    code_point=cp,
                    font_size_px=metric_size,
                    upem=self.config.upem,
                )
                metrics_by_size[metric_size] = measured
                self.store.save_metric_observation(
                    MetricObservation(
                        reference_id=reference_id,
                        style_id=style_id,
                        browser_version=self.session.browser_version,
                        config_hash=config_hash,
                        metrics=measured,
                        created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    )
                )
            direct_metrics = metrics_by_size.get(self.config.font_size_px)
            if direct_metrics is None:
                direct_metrics = await self.session.measure_glyph_direct(
                    font_family=font_family,
                    code_point=cp,
                    font_size_px=self.config.font_size_px,
                    upem=self.config.upem,
                )

            # 2. Determine adaptive subpixel phase schedule based on metric boundary alignment
            subpixel_phases = self.config.get_phases_for_metrics(direct_metrics)

            # 3. Multi-resolution lossless raster captures + adaptive subpixel phase schedule
            for res in self.config.resolutions:
                for sub_x, sub_y in subpixel_phases:
                    cache_key = ObservationRecord.build_cache_key(
                        reference_id=reference_id,
                        style_id=style_id,
                        code_point=cp,
                        browser_version=self.session.browser_version,
                        resolution=res,
                        subpixel_x=sub_x,
                        subpixel_y=sub_y,
                        config_hash=config_hash,
                    )

                    # Resume check: skip already completed observations
                    if self.store.has_observation(cache_key):
                        total_rasters += 1
                        continue

                    # Capture lossless raster from browser Canvas
                    png_bytes = await self.session.capture_lossless_raster(
                        font_family=font_family,
                        code_point=cp,
                        resolution_px=res,
                        subpixel_offset=(sub_x, sub_y),
                    )

                    png_sha256 = hashlib.sha256(png_bytes).hexdigest()
                    rel_path = f"{reference_id}/{style_id}/{cp:04X}/{res}px_{sub_x:.2f}_{sub_y:.2f}.png"
                    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

                    record = ObservationRecord(
                        cache_key=cache_key,
                        reference_id=reference_id,
                        style_id=style_id,
                        code_point=cp,
                        resolution=res,
                        subpixel_x=sub_x,
                        subpixel_y=sub_y,
                        raster_relative_path=rel_path,
                        raster_sha256=png_sha256,
                        raster_size_bytes=len(png_bytes),
                        metrics=direct_metrics,
                        created_at=now_iso,
                        browser_version=self.session.browser_version,
                        config_hash=config_hash,
                    )

                    self.store.save_observation(record, png_bytes)
                    total_rasters += 1

            if progress_cb:
                progress_cb(idx, total_glyphs)

        elapsed = time.perf_counter() - start_time
        logger.info(
            f"Collected {total_glyphs} glyphs ({total_rasters} total observation rasters) in {elapsed:.2f}s"
        )
        return total_glyphs, total_rasters, elapsed

    async def collect_pair_observations(
        self,
        reference_id: str,
        style_id: str,
        font_family: str | BrowserFontSelection,
        pairs: list[tuple[int, int]] | None = None,
    ) -> int:
        """Collect observable character pair advance measurements from browser Canvas text metrics.

        Measures raw left advance, right advance, and pair advance in Chromium to derive
        observable kerning differentials with real browser acquisition provenance.
        """
        from typography.models import BOUNDED_FIT_PAIRS

        target_pairs = pairs or BOUNDED_FIT_PAIRS
        captured = 0
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        provenance = f"chromium:{self.session.browser_version}:canvas_text_metrics"

        for left_cp, right_cp in target_pairs:
            m_left = await self.session.measure_glyph_direct(
                font_family=font_family,
                code_point=left_cp,
                font_size_px=self.config.font_size_px,
                upem=self.config.upem,
            )
            m_right = await self.session.measure_glyph_direct(
                font_family=font_family,
                code_point=right_cp,
                font_size_px=self.config.font_size_px,
                upem=self.config.upem,
            )
            pair_str = chr(left_cp) + chr(right_cp)

            pair_adv_upem = await self.session.measure_text_advance(
                font_family,
                pair_str,
                font_size_px=self.config.font_size_px,
                upem=self.config.upem,
            )
            raw_delta = pair_adv_upem - (m_left.advance_width_upem + m_right.advance_width_upem)
            inferred_kern = int(round(raw_delta))

            self.store.save_pair_observation(
                reference_id=reference_id,
                style_id=style_id,
                left_cp=left_cp,
                right_cp=right_cp,
                left_char=chr(left_cp),
                right_char=chr(right_cp),
                left_advance_upem=round(m_left.advance_width_upem, 2),
                right_advance_upem=round(m_right.advance_width_upem, 2),
                pair_advance_upem=round(pair_adv_upem, 2),
                inferred_kerning_upem=inferred_kern,
                confidence=1.0,
                provenance=provenance,
                created_at=now_iso,
            )
            captured += 1

        logger.info(f"Captured {captured} observable pair text metrics with provenance {provenance}")
        return captured

    async def collect_feature_observations(
        self,
        reference_id: str,
        style_id: str,
        font_family: str | BrowserFontSelection,
    ) -> int:
        """Collect bounded browser probes for kerning and common GSUB/GPOS features."""
        captured = 0
        provenance = f"chromium:{self.session.browser_version}:canvas_feature_probe"
        for feature_tag, sample_text in self.config.feature_probes:
            raw = await self.session.probe_opentype_feature(
                font_family,
                feature_tag,
                sample_text,
                font_size_px=self.config.font_size_px,
                upem=self.config.upem,
            )
            effect_observed = (
                raw["enabled_advance_upem"] != raw["disabled_advance_upem"]
                or raw["enabled_raster_signature"] != raw["disabled_raster_signature"]
            )
            self.store.save_feature_observation(
                OpenTypeFeatureObservation(
                    reference_id=reference_id,
                    style_id=style_id,
                    feature_tag=feature_tag,
                    sample_text=sample_text,
                    enabled_advance_upem=raw["enabled_advance_upem"],
                    disabled_advance_upem=raw["disabled_advance_upem"],
                    enabled_raster_signature=raw["enabled_raster_signature"],
                    disabled_raster_signature=raw["disabled_raster_signature"],
                    effect_observed=effect_observed,
                    provenance=provenance,
                    created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                )
            )
            captured += 1
        return captured
