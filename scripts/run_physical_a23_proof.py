"""Physical A23 production-representative full-style MAX proof runner."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
import sys
import time
from pathlib import Path

# Add agent/src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "agent" / "src"))

from measurement.benchmark_runner import get_peak_rss_mb
from measurement.store import ObservationStore
from reconstruction.candidate_builder import MaxCandidateFontBuilder
from reconstruction.candidate_validator import MaxCandidateHeldOutValidator
from reconstruction.models import ReconstructionConfig
from reconstruction.solver import MaxReconstructionSolver
from reconstruction_benchmark import REPRESENTATIVE_CODE_POINTS
from typography import EvidenceKerningInferencer


def run_a23_proof() -> dict:
    start_wall_time = time.perf_counter()
    start_rss = get_peak_rss_mb()

    # 1. Device identity
    device_info = {
        "hostname": socket.gethostname(),
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "python_version": platform.python_version(),
    }

    # 2. Observation store
    store_dir = Path("observations/benchmark")
    if not store_dir.exists():
        raise FileNotFoundError(f"Observation store not found at {store_dir}")
    store = ObservationStore(str(store_dir))

    # 3. Solver & Reconstruction
    config = ReconstructionConfig()
    solver = MaxReconstructionSolver(config=config)
    inferencer = EvidenceKerningInferencer(store=store)
    builder = MaxCandidateFontBuilder()

    family_id = "be_vietnam_pro"
    style_id = "regular"

    # Measure per-glyph reconstruction time
    glyph_timings: dict[str, float] = {}
    reconstructed_glyphs = []
    total_cache_hits = 0

    for cp in REPRESENTATIVE_CODE_POINTS:
        t0 = time.perf_counter()
        obs = store.get_glyph_observations(family_id, style_id, cp)
        total_cache_hits += len(obs)
        glyph = solver.reconstruct_glyph(obs)
        reconstructed_glyphs.append(glyph)
        t1 = time.perf_counter()
        glyph_timings[f"U+{cp:04X}"] = round((t1 - t0) * 1000, 2)

    # 4. GPOS Kerning Table Inference
    kerning_result = inferencer.infer_kerning(family_id, style_id)

    # 5. Font Binary Build (OTF, TTF, WOFF2)
    output_dir = Path("scratch/a23_proof_build")
    output_dir.mkdir(parents=True, exist_ok=True)
    build_result = builder.build_candidate_family(
        reconstructed_glyphs=reconstructed_glyphs,
        output_dir=str(output_dir),
        family_name="Be Vietnam Pro",
        style_name="Regular",
        kerning_result=kerning_result,
    )

    # 6. Artifact Hashes & Sizes
    artifacts: dict[str, dict] = {}
    for fmt_name, path_str in [
        ("OTF", build_result.otf_path),
        ("TTF", build_result.ttf_path),
        ("WOFF2", build_result.woff2_path),
    ]:
        p = Path(path_str)
        content = p.read_bytes()
        artifacts[fmt_name] = {
            "path": str(p),
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    # 7. Multi-Consumer Validation
    ground_truth_ttf = Path("agent/benchmark_data/ground_truth/BeVietnamPro-Regular.ttf")
    validator = MaxCandidateHeldOutValidator(str(ground_truth_ttf))
    validation_report = validator.validate_family(
        candidate_result=build_result,
        tested_codepoints=REPRESENTATIVE_CODE_POINTS,
    )

    end_wall_time = time.perf_counter()
    end_rss = get_peak_rss_mb()
    total_wall_seconds = end_wall_time - start_wall_time
    peak_rss_mb = end_rss

    report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "device_identity": device_info,
        "family_name": "Be Vietnam Pro",
        "style_name": "Regular",
        "total_wall_time_seconds": round(total_wall_seconds, 3),
        "mean_glyph_time_ms": round((total_wall_seconds / len(REPRESENTATIVE_CODE_POINTS)) * 1000, 2),
        "start_rss_mb": round(start_rss, 2),
        "peak_rss_mb": round(peak_rss_mb, 2),
        "rss_budget_mb": 120.0,
        "rss_budget_passed": peak_rss_mb <= 120.0,
        "total_cache_hits": total_cache_hits,
        "glyph_timings_ms": glyph_timings,
        "artifacts": artifacts,
        "kerning_summary": {
            "inferred_pairs_count": len(kerning_result.inferred_pairs),
            "fit_pairs_count": len(kerning_result.fit_pairs),
            "held_out_pairs_count": len(kerning_result.held_out_pairs),
        },
        "validation_summary": {
            "all_formats_passed": validation_report.all_formats_passed,
            "mean_held_out_raster_iou": round(validation_report.mean_held_out_raster_iou, 4),
            "mean_advance_error_upem": round(validation_report.mean_advance_error_upem, 2),
            "max_advance_error_upem": round(validation_report.max_advance_error_upem, 2),
            "mean_lsb_error_upem": round(validation_report.mean_lsb_error_upem, 2),
            "in_cmap_shaping_match_rate": round(validation_report.in_cmap_shaping_match_rate, 4),
            "chromium_available": validation_report.chromium_result.is_available,
            "chromium_direct_loadable": validation_report.chromium_result.is_direct_loadable_chromium,
            "chromium_advance_error_upem": round(validation_report.chromium_result.mean_chromium_advance_error_upem, 2),
        },
        "per_glyph_raster_iou": [
            {
                "code_point": f"U+{r.code_point:04X}",
                "character": r.character,
                "render_size_px": r.render_size_px,
                "raster_iou": round(r.raster_iou, 4),
            }
            for r in validation_report.raster_results
        ],
    }
    return report


if __name__ == "__main__":
    print("Running MAX physical A23 full-style proof...")
    rep = run_a23_proof()
    out_file = Path("ops/max_physical_a23_proof_report.json")
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=2)
    print(f"Report saved to {out_file}")
    print(f"Total Wall Time: {rep['total_wall_time_seconds']}s")
    print(f"Peak RSS: {rep['peak_rss_mb']} MB (Budget: 120 MB -> Passed: {rep['rss_budget_passed']})")
    print(f"All Formats Passed: {rep['validation_summary']['all_formats_passed']}")
    print(f"Mean Held-Out Raster IoU: {rep['validation_summary']['mean_held_out_raster_iou']}")
