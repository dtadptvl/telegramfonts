"""CLI runner for Ground-Truth Font Observation Benchmark (MAX Pipeline Foundation)."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import asdict
from pathlib import Path

# Add src to path if executed standalone
sys.path.insert(0, str(Path(__file__).parent))

from measurement.benchmark_runner import GroundTruthBenchmarkRunner
from measurement.models import ObservationConfig


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


async def main_async(args: argparse.Namespace) -> int:
    font_path = Path(args.font_path).resolve()
    if not font_path.exists():
        print(f"Error: font path {font_path} does not exist", file=sys.stderr)
        return 1

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    config = ObservationConfig(
        resolutions=tuple(int(r) for r in args.resolutions.split(",")),
        subpixel_phases=((0.0, 0.0), (0.25, 0.0), (0.5, 0.0), (0.75, 0.0)),
        font_size_px=float(args.font_size),
        timeout_seconds=float(args.timeout),
    )

    runner = GroundTruthBenchmarkRunner(
        ground_truth_font_path=font_path,
        output_dir=output_dir,
        config=config,
    )

    # Standard representative subset covering ASCII + Vietnamese diacritics
    test_chars = (
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        "àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđĐ"
    )
    test_cps = sorted(set(ord(c) for c in test_chars))

    if args.samples > 0:
        test_cps = test_cps[: args.samples]

    print("================================================================================")
    print("  MAX Pipeline Foundation: Ground-Truth Browser Observation Benchmark")
    print("================================================================================")
    print(f"  Target Font:          {font_path.name}")
    print(f"  Target Glyphs:        {len(test_cps)} glyphs (ASCII + Vietnamese)")
    print(f"  Resolutions:          {config.resolutions}")
    print(f"  Subpixel Phases:      {len(config.subpixel_phases)} phases/resolution")
    print(f"  Output Directory:     {output_dir}")
    print("--------------------------------------------------------------------------------")

    # Run 1: Observation collection & Ground Truth comparison
    print("Executing Run 1 (initial observation capture)...")
    res1 = await runner.run(code_points_subset=test_cps)

    # Run 2: Verification of deterministic resume & cache reuse
    print("Executing Run 2 (resume / cache hit verification)...")
    res2 = await runner.run(code_points_subset=test_cps)

    print("\n--------------------------------------------------------------------------------")
    print("  Benchmark Observation Results:")
    print("--------------------------------------------------------------------------------")
    print(f"  Observed Glyphs:      {res1.total_glyphs_observed}")
    print(f"  Total Rasters:        {res1.total_raster_observations}")
    print(f"  Coverage Match Rate:  {res1.coverage_match_rate * 100:.1f}%")
    print(f"  Adv Width Mean Delta: {res1.advance_width_mean_delta_upem:.2f} UPEM")
    print(f"  Adv Width Max Delta:  {res1.advance_width_max_delta_upem:.2f} UPEM")
    print(f"  Adv Width RMS Delta:  {res1.advance_width_rms_delta_upem:.2f} UPEM")
    print(f"  LSB Mean Delta:       {res1.lsb_mean_delta_upem:.2f} UPEM")
    print(f"  LSB Max Delta:        {res1.lsb_max_delta_upem:.2f} UPEM")
    print(f"  Observation Time:     {res1.observation_time_seconds:.2f}s ({res1.glyphs_per_second:.1f} glyphs/s)")
    print(f"  Resume Run Time:      {res2.observation_time_seconds:.2f}s ({res2.glyphs_per_second:.1f} glyphs/s)")
    print(f"  Peak RSS:             {res1.peak_rss_mb:.2f} MB")
    print(f"  Total Storage:        {res1.total_storage_bytes:,} bytes ({res1.bytes_per_glyph:,.1f} bytes/glyph)")
    print("================================================================================")

    out_data = {
        "run_1": asdict(res1),
        "run_2_resume": asdict(res2),
        "deterministic_resume_pass": res2.observation_time_seconds < res1.observation_time_seconds,
    }

    if args.json_out:
        out_file = Path(args.json_out)
        out_file.write_text(json.dumps(out_data, indent=2), encoding="utf-8")
        print(f"Wrote benchmark report to {out_file}")

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Ground Truth Browser Observation Benchmark")
    parser.add_argument(
        "--font-path",
        default="agent/benchmark_data/ground_truth/BeVietnamPro-Regular.ttf",
        help="Path to ground truth TTF font binary",
    )
    parser.add_argument(
        "--output-dir",
        default="observations/benchmark",
        help="Target directory for observation database and rasters",
    )
    parser.add_argument(
        "--resolutions",
        default="128,256",
        help="Comma-separated raster resolutions (e.g. 128,256)",
    )
    parser.add_argument("--font-size", type=float, default=200.0, help="Direct measurement font size in px")
    parser.add_argument("--timeout", type=float, default=10.0, help="CDP timeout in seconds")
    parser.add_argument("--samples", type=int, default=0, help="Number of glyph samples (0 for all target characters)")
    parser.add_argument("--json-out", default=None, help="Path to write JSON benchmark report")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")

    args = parser.parse_args()
    setup_logging(args.verbose)
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
