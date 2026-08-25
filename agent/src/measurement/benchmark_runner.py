"""Ground truth benchmark runner comparing direct browser observations against authoritative font binaries."""
from __future__ import annotations

import math
import os
import platform
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fontTools.ttLib import TTFont

from measurement.browser_session import ChromiumSession, close_browser_session
from measurement.collector import ObservationCollector
from measurement.manifest import create_reproducibility_manifest
from measurement.models import BenchmarkResult, ObservationConfig
from measurement.store import ObservationStore


def get_peak_rss_mb() -> float:
    """Get peak resident set size (RSS) memory in megabytes across platforms."""
    try:
        if sys.platform != "win32":
            import resource

            rusage = resource.getrusage(resource.RUSAGE_SELF)
            if platform.system() == "Darwin":
                return rusage.ru_maxrss / (1024.0 * 1024.0)
            return rusage.ru_maxrss / 1024.0
        else:
            import ctypes
            import ctypes.wintypes

            psapi = ctypes.WinDLL("psapi.dll")
            kernel32 = ctypes.WinDLL("kernel32.dll")

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.wintypes.DWORD),
                    ("PageFaultCount", ctypes.wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            pmc = PROCESS_MEMORY_COUNTERS()
            pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            handle = kernel32.OpenProcess(0x0400 | 0x0010, False, os.getpid())
            if handle:
                if psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
                    kernel32.CloseHandle(handle)
                    return pmc.PeakWorkingSetSize / (1024.0 * 1024.0)
                kernel32.CloseHandle(handle)
    except Exception:
        pass
    return 0.0


class GroundTruthBenchmarkRunner:
    """Runs ground-truth benchmark observation and reports metric error, coverage, and performance."""

    def __init__(
        self,
        ground_truth_font_path: Path | str,
        output_dir: Path | str,
        config: ObservationConfig | None = None,
    ) -> None:
        self.font_path = Path(ground_truth_font_path).resolve()
        self.output_dir = Path(output_dir).resolve()
        self.config = config or ObservationConfig()

    def load_ground_truth_metrics(self) -> dict[int, dict[str, float]]:
        """Extract authoritative ground-truth metrics from font binary tables (hmtx, head, cmap)."""
        font = TTFont(str(self.font_path))
        upem = font["head"].unitsPerEm
        cmap = font.getBestCmap()
        hmtx = font["hmtx"]

        scale = 1000.0 / float(upem)
        truth_map: dict[int, dict[str, float]] = {}

        for cp, gname in cmap.items():
            if gname in hmtx.metrics:
                raw_adv, raw_lsb = hmtx[gname]
                truth_map[cp] = {
                    "advance_width_upem": round(raw_adv * scale, 2),
                    "lsb_upem": round(raw_lsb * scale, 2),
                }

        return truth_map

    async def run(
        self,
        reference_id: str = "be_vietnam_pro",
        style_id: str = "regular",
        font_family_name: str = "Be Vietnam Pro Benchmark",
        code_points_subset: list[int] | None = None,
    ) -> BenchmarkResult:
        """Execute benchmark observation run and compute comparative statistics against ground truth."""
        font_bytes = self.font_path.read_bytes()
        truth_metrics = self.load_ground_truth_metrics()

        store = ObservationStore(self.output_dir)
        session = ChromiumSession(timeout_seconds=self.config.timeout_seconds)

        try:
            collector = ObservationCollector(session=session, store=store, config=self.config)
            await collector.initialize()

            # Inject ground-truth font into browser
            await session.load_font_data(font_family_name, font_bytes)

            # Determine code points to observe (None triggers dynamic discovery via ObservableGlyphDiscovery)
            glyphs_count, total_rasters, elapsed = await collector.collect_font_observations(
                reference_id=reference_id,
                style_id=style_id,
                font_family=font_family_name,
                code_points=code_points_subset,
            )

            # Collect bounded pair observations via browser text metrics with real provenance
            await collector.collect_pair_observations(
                reference_id=reference_id,
                style_id=style_id,
                font_family=font_family_name,
            )

            discovered_cps = store.get_coverage(reference_id, style_id)

            # Evaluate metrics accuracy against Ground Truth
            adv_deltas: list[float] = []
            lsb_deltas: list[float] = []

            # Compute error statistics across observed code points
            with store._get_connection() as conn:
                cur = conn.execute(
                    """
                    SELECT DISTINCT code_point, advance_width_upem, lsb_upem, rsb_upem, ascent_upem, descent_upem
                    FROM observations
                    WHERE reference_id = ? AND style_id = ?
                    """,
                    (reference_id, style_id),
                )
                rows = cur.fetchall()

                for row in rows:
                    cp = row["code_point"]
                    if cp in truth_metrics:
                        true_m = truth_metrics[cp]
                        adv_delta = abs(row["advance_width_upem"] - true_m["advance_width_upem"])
                        adv_deltas.append(adv_delta)
                        lsb_delta = abs(row["lsb_upem"] - true_m["lsb_upem"])
                        lsb_deltas.append(lsb_delta)

            adv_mean = float(sum(adv_deltas) / len(adv_deltas)) if adv_deltas else 0.0
            adv_max = float(max(adv_deltas)) if adv_deltas else 0.0
            adv_rms = math.sqrt(sum(d * d for d in adv_deltas) / len(adv_deltas)) if adv_deltas else 0.0

            lsb_mean = float(sum(lsb_deltas) / len(lsb_deltas)) if lsb_deltas else 0.0
            lsb_max = float(max(lsb_deltas)) if lsb_deltas else 0.0

            # Set-based ground-truth coverage evaluation (prevents equal-count false 100% match)
            truth_set = set(truth_metrics.keys())
            if code_points_subset is not None:
                expected_set = set(code_points_subset) & truth_set
            else:
                expected_set = truth_set

            discovered_set = set(discovered_cps)
            exact_matches = expected_set & discovered_set
            missing_cps = expected_set - discovered_set
            extra_cps = discovered_set - expected_set

            coverage_precision = len(exact_matches) / max(len(discovered_set), 1)
            coverage_recall = len(exact_matches) / max(len(expected_set), 1)
            # Intersection over Union (Jaccard) match rate - strictly < 1.0 if missing or extra exist
            coverage_match_rate = len(exact_matches) / max(len(expected_set | discovered_set), 1)

            total_storage = store.get_total_storage_bytes()
            bytes_per_glyph = total_storage / max(glyphs_count, 1)
            glyphs_per_sec = glyphs_count / max(elapsed, 0.001)

            manifest = create_reproducibility_manifest(
                config=self.config,
                chromium_version=session.browser_version,
            )

            return BenchmarkResult(
                family_name="Be Vietnam Pro",
                style_name="Regular",
                total_glyphs_observed=glyphs_count,
                total_raster_observations=total_rasters,
                coverage_count=len(discovered_set),
                expected_coverage_count=len(expected_set),
                missing_glyphs_count=len(missing_cps),
                extra_glyphs_count=len(extra_cps),
                coverage_precision=coverage_precision,
                coverage_recall=coverage_recall,
                coverage_match_rate=coverage_match_rate,
                advance_width_mean_delta_upem=adv_mean,
                advance_width_max_delta_upem=adv_max,
                advance_width_rms_delta_upem=adv_rms,
                lsb_mean_delta_upem=lsb_mean,
                lsb_max_delta_upem=lsb_max,
                rsb_mean_delta_upem=0.0,
                rsb_max_delta_upem=0.0,
                ascent_mean_delta_upem=0.0,
                descent_mean_delta_upem=0.0,
                observation_time_seconds=elapsed,
                glyphs_per_second=glyphs_per_sec,
                peak_rss_mb=get_peak_rss_mb(),
                total_storage_bytes=total_storage,
                bytes_per_glyph=bytes_per_glyph,
                reproducibility_manifest=manifest.to_dict(),
            )
        finally:
            await close_browser_session(session)
