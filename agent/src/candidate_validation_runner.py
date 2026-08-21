"""CLI Runner for MAX Candidate Font Build & Held-Out Consumer Validation (MAX Pipeline C/D)."""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import platform
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from measurement.benchmark_runner import get_peak_rss_mb
from measurement.store import ObservationStore
from reconstruction.candidate_builder import MaxCandidateFontBuilder
from reconstruction.candidate_validator import MaxCandidateHeldOutValidator
from reconstruction.models import ReconstructionConfig
from reconstruction.solver import MaxReconstructionSolver

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("telegramfonts.agent.candidate_validation_runner")

REPRESENTATIVE_CODE_POINTS = [
    ord("A"),
    ord("B"),
    ord("O"),
    ord("8"),
    ord("@"),
    ord("%"),
    ord("g"),
    ord("m"),
    ord("ơ"),
    ord("ư"),
    ord("ắ"),
    ord("đ"),
    ord("Đ"),
]


def run_candidate_pipeline(
    store_dir: str | Path = "observations/benchmark",
    truth_path: str | Path = "agent/benchmark_data/ground_truth/BeVietnamPro-Regular.ttf",
    output_dir: str | Path = "build/candidate_fonts",
    json_out: str | Path = "ops/max_candidate_validation_report.json",
    reference_id: str = "be_vietnam_pro",
    style_id: str = "regular",
) -> dict[str, Any]:
    """Execute end-to-end MAX candidate font build and held-out multi-consumer validation."""
    start_time = time.perf_counter()

    store = ObservationStore(store_dir)
    solver = MaxReconstructionSolver(ReconstructionConfig(grid_resolution=512, fitting_tolerance_upem=1.5))

    logger.info("Reconstructing cubic master glyphs from cached observations...")
    reconstructed_glyphs = []
    for cp in REPRESENTATIVE_CODE_POINTS:
        obs = store.get_glyph_observations(reference_id, style_id, cp)
        if obs:
            glyph = solver.reconstruct_glyph(obs)
            reconstructed_glyphs.append(glyph)

    logger.info("Building candidate font binaries (OTF, TTF, WOFF2)...")
    builder = MaxCandidateFontBuilder(
        family_name="BeVietnamPro MAX",
        style_name="Regular",
        units_per_em=1000,
    )
    build_result = builder.build_candidate_family(reconstructed_glyphs, output_dir)

    logger.info("Executing independent held-out multi-consumer validation...")
    validator = MaxCandidateHeldOutValidator(truth_path)
    report = validator.validate_family(build_result, tested_codepoints=REPRESENTATIVE_CODE_POINTS)

    elapsed_s = time.perf_counter() - start_time
    peak_rss = get_peak_rss_mb()

    device_id = {
        "hostname": platform.node(),
        "os": platform.system(),
        "os_release": platform.release(),
        "architecture": platform.machine(),
        "processor": platform.processor(),
        "python_version": platform.python_version(),
        "cpu_count": os.cpu_count(),
    }

    report_dict = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "device_identity": device_id,
        "reference_id": reference_id,
        "style_id": style_id,
        "total_time_seconds": round(elapsed_s, 2),
        "peak_rss_mb": round(peak_rss, 2),
        "candidate_family": {
            "family_name": build_result.family_name,
            "style_name": build_result.style_name,
            "glyph_count": build_result.glyph_count,
            "formats": [
                {
                    "format": art.format,
                    "filename": art.filename,
                    "file_path": str(art.file_path),
                    "size_bytes": art.size_bytes,
                    "sha256_hex": art.sha256_hex,
                    "glyph_count": art.glyph_count,
                    "units_per_em": art.units_per_em,
                }
                for art in (build_result.otf, build_result.ttf, build_result.woff2)
            ],
        },
        "validation_summary": {
            "all_formats_passed": report.all_formats_passed,
            "mean_advance_error_upem": report.mean_advance_error_upem,
            "max_advance_error_upem": report.max_advance_error_upem,
            "mean_lsb_error_upem": report.mean_lsb_error_upem,
            "max_lsb_error_upem": report.max_lsb_error_upem,
            "in_cmap_shaping_match_rate": report.in_cmap_shaping_match_rate,
            "mean_held_out_raster_iou": report.mean_held_out_raster_iou,
            "requires_typography_phase_e": report.requires_typography_phase_e,
            "typography_evidence_summary": report.typography_evidence_summary,
        },
        "chromium_validation": asdict(report.chromium_result),
        "format_details": [asdict(f) for f in report.format_results],
        "metric_differences": [asdict(m) for m in report.metric_differences],
        "shaping_results": [asdict(s) for s in report.shaping_results],
        "raster_results": [asdict(r) for r in report.raster_results],
    }

    json_path = Path(json_out)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report_dict, f, indent=2, ensure_ascii=False)

    print("\n" + "=" * 80)
    print("  MAX Candidate Font Build & Held-Out Consumer Validation Summary")
    print("=" * 80)
    print(f"  Family Name:             {build_result.family_name} {build_result.style_name}")
    print(f"  Glyph Count:             {build_result.glyph_count} glyphs")
    print(f"  OTF/CFF Candidate:       {build_result.otf.size_bytes} bytes ({build_result.otf.filename})")
    print(f"  TTF Candidate:           {build_result.ttf.size_bytes} bytes ({build_result.ttf.filename})")
    print(f"  WOFF2 Candidate:         {build_result.woff2.size_bytes} bytes ({build_result.woff2.filename})")
    print("-" * 80)
    print(f"  Multi-Consumer Load:     {'PASS (100%)' if report.all_formats_passed else 'FAIL'}")
    print(f"  Chromium WOFF2 Load:     {'PASS' if report.chromium_result.is_direct_loadable_chromium else 'FAIL / N/A'} ({report.chromium_result.browser_version})")
    print(f"  Fallback Rejection:      {'PASS' if report.chromium_result.fallback_rejection_verified else 'FAIL / N/A'}")
    print(f"  Mean Advance Error:      {report.mean_advance_error_upem} UPEM (Max: {report.max_advance_error_upem} UPEM)")
    print(f"  Mean LSB Error:          {report.mean_lsb_error_upem} UPEM (Max: {report.max_lsb_error_upem} UPEM)")
    print(f"  In-Cmap Shaping Match:   {report.in_cmap_shaping_match_rate * 100:.1f}%")
    print(f"  Held-Out Raster IoU:     {report.mean_held_out_raster_iou * 100:.1f}%")
    print(f"  Requires Typography:     {report.requires_typography_phase_e}")
    print(f"  Typography Assessment:   {report.typography_evidence_summary}")
    print(f"  Total Runtime:           {elapsed_s:.2f} s, Peak RSS: {peak_rss:.2f} MB")
    print("=" * 80)
    print(f"Wrote validation report to {json_path}\n")

    return report_dict


def main() -> int:
    parser = argparse.ArgumentParser(description="MAX Candidate Font Build & Held-Out Validation")
    parser.add_argument("--store-dir", default="observations/benchmark")
    parser.add_argument("--truth-path", default="agent/benchmark_data/ground_truth/BeVietnamPro-Regular.ttf")
    parser.add_argument("--output-dir", default="build/candidate_fonts")
    parser.add_argument("--json-out", default="ops/max_candidate_validation_report.json")
    args = parser.parse_args()

    run_candidate_pipeline(
        store_dir=args.store_dir,
        truth_path=args.truth_path,
        output_dir=args.output_dir,
        json_out=args.json_out,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
