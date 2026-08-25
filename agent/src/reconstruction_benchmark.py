"""MAX Pipeline B Benchmark Runner: Evaluates SDF + Cubic Bézier Reconstruction against Single-Observation Baseline."""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from measurement.benchmark_runner import get_peak_rss_mb
from measurement.store import ObservationStore
from reconstruction.baseline import SingleObservationBaselineReconstructor
from reconstruction.evaluator import GroundTruthGeometryEvaluator
from reconstruction.models import GeometricScoreResult, ReconstructionConfig
from reconstruction.solver import MaxReconstructionSolver

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("telegramfonts.agent.reconstruction_benchmark")

# Canonical representative subset covering diverse topologies:
# Outer-only: 'A', 'm'
# Nested holes: 'B', '8', 'O'
# Complex/disconnected symbols: '@', '%'
# Vietnamese diacritics / crosses: 'ơ', 'ư', 'ắ', 'đ', 'Đ'
REPRESENTATIVE_CODE_POINTS: list[int] = [
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


def run_benchmark(
    store_dir: str | Path = "observations/benchmark",
    ttf_path: str | Path = "agent/benchmark_data/ground_truth/BeVietnamPro-Regular.ttf",
    reference_id: str = "be_vietnam_pro",
    style_id: str = "regular",
    browser_version: str = "",
    config_hash: str = "",
    code_points: list[int] | None = None,
) -> dict[str, Any]:
    """Execute comparative benchmark between Baseline and MAX Pipeline B."""
    if not browser_version or not config_hash:
        raise ValueError("EXACT_IDENTITY_REQUIRED: browser_version and config_hash are required for benchmark execution")
    store = ObservationStore(store_dir)
    evaluator = GroundTruthGeometryEvaluator(ttf_path)

    test_cps = code_points or REPRESENTATIVE_CODE_POINTS

    baseline_scores: list[GeometricScoreResult] = []
    max_b_scores: list[GeometricScoreResult] = []

    config = ReconstructionConfig(
        grid_resolution=512,
        fitting_tolerance_upem=1.5,
        corner_threshold_degrees=120.0,
        min_contour_area_upem=15.0,
    )
    solver = MaxReconstructionSolver(config=config)

    logger.info("Evaluating %d representative glyphs...", len(test_cps))

    start_bench = time.perf_counter()

    for cp in test_cps:
        char = chr(cp)
        observations = store.get_glyph_observations(
            reference_id, style_id, cp, browser_version=browser_version, config_hash=config_hash
        )
        if not observations:
            logger.warning("No observations found in store for U+%04X ('%s')", cp, char)
            continue

        # 1. Baseline Reconstruction
        base_glyph = SingleObservationBaselineReconstructor.reconstruct_glyph(observations, config)
        base_score = evaluator.evaluate_glyph(base_glyph)
        baseline_scores.append(base_score)

        # 2. MAX Pipeline B Reconstruction (SDF + Cubic Bézier)
        max_glyph = solver.reconstruct_glyph(observations)
        max_score = evaluator.evaluate_glyph(max_glyph)
        max_b_scores.append(max_score)

    total_time = time.perf_counter() - start_bench

    # Aggregate comparative summary
    def summarize(scores: list[GeometricScoreResult]) -> dict[str, Any]:
        if not scores:
            return {}
        return {
            "glyph_count": len(scores),
            "mean_outline_iou": float(np.mean([s.outline_iou for s in scores])),
            "min_outline_iou": float(np.min([s.outline_iou for s in scores])),
            "mean_chamfer_distance_upem": float(np.mean([s.chamfer_distance_mean_upem for s in scores])),
            "max_chamfer_distance_upem": float(np.max([s.chamfer_distance_mean_upem for s in scores])),
            "mean_p95_error_upem": float(np.mean([s.p95_edge_error_upem for s in scores])),
            "max_hausdorff_upem": float(np.max([s.hausdorff_distance_upem for s in scores])),
            "topology_mismatch_count": sum(1 for s in scores if not s.topology_match),
            "mean_cubic_segments": float(np.mean([s.cubic_segments_count for s in scores])),
            "mean_control_points": float(np.mean([s.control_points_count for s in scores])),
            "mean_runtime_ms": float(np.mean([s.runtime_ms for s in scores])),
        }

    import numpy as np

    base_summary = summarize(baseline_scores)
    max_b_summary = summarize(max_b_scores)

    import platform

    device_identity = {
        "hostname": platform.node(),
        "os": platform.system(),
        "os_release": platform.release(),
        "architecture": platform.machine(),
        "processor": platform.processor(),
        "python_version": platform.python_version(),
        "cpu_count": os.cpu_count(),
    }

    report = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "device_identity": device_identity,
        "reference_id": reference_id,
        "style_id": style_id,
        "representative_glyph_count": len(test_cps),
        "total_benchmark_time_seconds": round(total_time, 2),
        "peak_rss_mb": round(get_peak_rss_mb(), 2),
        "baseline_summary": base_summary,
        "max_pipeline_b_summary": max_b_summary,
        "per_glyph_results": [
            {
                "code_point": s_max.code_point,
                "character": s_max.character,
                "baseline_iou": round(s_base.outline_iou, 4),
                "max_b_iou": round(s_max.outline_iou, 4),
                "baseline_chamfer_upem": round(s_base.chamfer_distance_mean_upem, 2),
                "max_b_chamfer_upem": round(s_max.chamfer_distance_mean_upem, 2),
                "baseline_p95_upem": round(s_base.p95_edge_error_upem, 2),
                "max_b_p95_upem": round(s_max.p95_edge_error_upem, 2),
                "topology_match": s_max.topology_match,
                "cubic_segments": s_max.cubic_segments_count,
                "runtime_ms": round(s_max.runtime_ms, 2),
            }
            for s_base, s_max in zip(baseline_scores, max_b_scores)
        ],
    }

    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="MAX Pipeline B Geometry Reconstruction Benchmark"
    )
    parser.add_argument(
        "--store-dir",
        default="observations/benchmark",
        help="Path to MAX A observation store",
    )
    parser.add_argument(
        "--ttf-path",
        default="agent/benchmark_data/ground_truth/BeVietnamPro-Regular.ttf",
        help="Path to isolated ground-truth TTF font binary",
    )
    parser.add_argument(
        "--browser-version",
        default="",
        help="Chromium browser version (exact identity)",
    )
    parser.add_argument(
        "--config-hash",
        default="",
        help="Observation config hash (exact identity)",
    )
    parser.add_argument(
        "--json-out",
        default="scratch/reconstruction_benchmark_report.json",
        help="Path to write JSON benchmark report",
    )

    args = parser.parse_args()

    browser_ver = args.browser_version
    cfg_hash = args.config_hash
    if not browser_ver or not cfg_hash:
        try:
            st = ObservationStore(args.store_dir)
            manifest = st.get_latest_manifest()
            if manifest:
                browser_ver = browser_ver or manifest.get("chromium_version", "")
                cfg_hash = cfg_hash or manifest.get("config_hash", "")
        except Exception:
            pass

    if not browser_ver or not cfg_hash:
        print("ERROR: --browser-version and --config-hash must be supplied or present in store manifest")
        return 1

    print("=" * 80)
    print("  MAX Pipeline B: Continuous SDF/Topology + Cubic Bézier Reconstruction")
    print("=" * 80)

    report = run_benchmark(
        store_dir=args.store_dir,
        ttf_path=args.ttf_path,
        browser_version=browser_ver,
        config_hash=cfg_hash,
    )

    base = report["baseline_summary"]
    max_b = report["max_pipeline_b_summary"]

    print("\n--------------------------------------------------------------------------------")
    print("  Benchmark Comparison Summary:")
    print("--------------------------------------------------------------------------------")
    print(f"  Representative Glyphs:   {report['representative_glyph_count']}")
    print(f"  Mean Outline IoU:        Baseline: {base.get('mean_outline_iou', 0)*100:.2f}%  ->  MAX B: {max_b.get('mean_outline_iou', 0)*100:.2f}%")
    print(f"  Mean Chamfer Dist (UPEM):Baseline: {base.get('mean_chamfer_distance_upem', 0):.2f}  ->  MAX B: {max_b.get('mean_chamfer_distance_upem', 0):.2f}")
    print(f"  P95 Edge Error (UPEM):   Baseline: {base.get('mean_p95_error_upem', 0):.2f}  ->  MAX B: {max_b.get('mean_p95_error_upem', 0):.2f}")
    print(f"  Topology Mismatches:     Baseline: {base.get('topology_mismatch_count', 0)}  ->  MAX B: {max_b.get('topology_mismatch_count', 0)}")
    print(f"  Mean Cubic Segments:     MAX B: {max_b.get('mean_cubic_segments', 0):.1f} segments/glyph ({max_b.get('mean_control_points', 0):.1f} control points)")
    print(f"  Reconstruction Speed:    MAX B: {max_b.get('mean_runtime_ms', 0):.2f} ms/glyph ({1000.0/max(max_b.get('mean_runtime_ms', 1), 0.1):.1f} glyphs/s)")
    print(f"  Peak RSS:                {report['peak_rss_mb']:.2f} MB")
    print("=" * 80)

    if args.json_out:
        out_p = Path(args.json_out)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Wrote benchmark report to {out_p}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
