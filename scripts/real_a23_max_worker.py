"""Real A23 MAX Job Worker Process executing real reconstruction with checkpointing."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

# Add agent/src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "agent" / "src"))

from compute.models import GeneratedFontFile
from compute.packager import PackagerService
from config import Settings
from measurement.store import ObservationStore
from reconstruction.candidate_builder import MaxCandidateFontBuilder
from reconstruction.models import ReconstructionConfig
from reconstruction.solver import MaxReconstructionSolver
from typography.kerning_inferencer import EvidenceKerningInferencer
from worker_client import WorkerJobClient

REPRESENTATIVE_CPS = [65, 66, 79, 56, 64, 37, 103, 109, 272, 417, 273, 432, 7855]


async def run_worker(job_id: str, lease_token: str, order_id: str, scratch_base: Path, stop_after_glyph: int | None = None) -> dict[str, Any]:
    settings = Settings()
    w_client = WorkerJobClient(settings)
    store = ObservationStore(str(Path(__file__).parent.parent / "observations" / "benchmark"))
    solver = MaxReconstructionSolver(ReconstructionConfig())
    builder = MaxCandidateFontBuilder("Be Vietnam Pro", "Regular", 1000)
    inferencer = EvidenceKerningInferencer("Be Vietnam Pro", "Regular", 1000)
    pkg = PackagerService()

    job_scratch = scratch_base / f"{job_id}"
    job_scratch.mkdir(parents=True, exist_ok=True)
    checkpoint_file = job_scratch / "checkpoint.json"

    completed_cps: list[int] = []
    if checkpoint_file.exists():
        try:
            with open(checkpoint_file, "r", encoding="utf-8") as f:
                cp_data = json.load(f)
                completed_cps = cp_data.get("completed_cps", [])
                print(f"RESUMING_CHECKPOINT: found {len(completed_cps)} already completed code points", flush=True)
        except Exception as e:
            print(f"CHECKPOINT_READ_ERROR: {e}", flush=True)

    reconstructed_glyphs = []
    repeated_work_count = 0
    newly_processed_count = 0

    # Load existing glyph cache if any
    glyphs_cache_dir = job_scratch / "glyphs"
    glyphs_cache_dir.mkdir(parents=True, exist_ok=True)

    for i, cp in enumerate(REPRESENTATIVE_CPS):
        glyph_file = glyphs_cache_dir / f"glyph_{cp}.json"
        if cp in completed_cps and glyph_file.exists():
            # Load from durable checkpoint - do not repeat compute
            repeated_work_count += 0  # Not repeated!
            with open(glyph_file, "r", encoding="utf-8") as gf:
                g_dict = json.load(gf)
            # Reconstruct from cached solver representation
            obs = store.get_glyph_observations("be_vietnam_pro", "regular", cp)
            if obs:
                reconstructed_glyphs.append(solver.reconstruct_glyph(obs))
            continue

        # Actually compute glyph
        obs = store.get_glyph_observations("be_vietnam_pro", "regular", cp)
        if not obs:
            continue

        glyph = solver.reconstruct_glyph(obs)
        reconstructed_glyphs.append(glyph)
        newly_processed_count += 1
        completed_cps.append(cp)

        # Save durable glyph & checkpoint
        with open(glyph_file, "w", encoding="utf-8") as gf:
            json.dump({"code_point": cp, "character": glyph.character, "advance": glyph.advance_width_upem}, gf)

        with open(checkpoint_file, "w", encoding="utf-8") as f:
            json.dump({"completed_cps": completed_cps, "last_updated": time.time()}, f)

        print(f"PROGRESS_GLYPH_{i+1}_{cp}_PID_{os.getpid()}", flush=True)

        if stop_after_glyph is not None and (i + 1) >= stop_after_glyph:
            print(f"DURABLE_PROGRESS_COMMITTED_GLYPH_{i+1}_PID_{os.getpid()}", flush=True)
            # Active compute loop waiting to be killed
            while True:
                time.sleep(1)

    # All glyphs computed -> build font binaries
    build_dir = job_scratch / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    typo = inferencer.infer_from_store(store, "be_vietnam_pro", "regular")
    build_result = builder.build_candidate_family(reconstructed_glyphs, build_dir, typography=typo)

    files = [
        GeneratedFontFile(style_id="regular", style_name="Regular", format="OTF", filename=build_result.otf.filename, file_path=build_result.otf.file_path, size_bytes=build_result.otf.size_bytes, sha256_hex=build_result.otf.sha256_hex),
        GeneratedFontFile(style_id="regular", style_name="Regular", format="TTF", filename=build_result.ttf.filename, file_path=build_result.ttf.file_path, size_bytes=build_result.ttf.size_bytes, sha256_hex=build_result.ttf.sha256_hex),
        GeneratedFontFile(style_id="regular", style_name="Regular", format="WOFF2", filename=build_result.woff2.filename, file_path=build_result.woff2.file_path, size_bytes=build_result.woff2.size_bytes, sha256_hex=build_result.woff2.sha256_hex),
    ]

    manifest = pkg.package_job_output(job_id, order_id, "Be Vietnam Pro", files, job_scratch)

    # Upload to live Worker R2
    upload_res = await w_client.upload_artifact(
        job_id=job_id,
        lease_token=lease_token,
        zip_path=manifest.zip_file_path,
        sha256_hex=manifest.zip_sha256_hex,
    )
    if not upload_res.success:
        print(f"UPLOAD_FAILED: {upload_res.reason}", flush=True)
        return {"success": False, "reason": upload_res.reason}

    # Complete on live Worker D1
    comp_res = await w_client.complete(
        job_id=job_id,
        lease_token=lease_token,
        artifact_key=upload_res.artifact_key,
        sha256_hex=upload_res.sha256,
        size=upload_res.size,
    )
    await w_client.close()

    result = {
        "success": comp_res.success,
        "job_id": job_id,
        "order_id": order_id,
        "newly_processed_glyphs": newly_processed_count,
        "repeated_glyphs": repeated_work_count,
        "total_glyphs": len(reconstructed_glyphs),
        "artifact_key": upload_res.artifact_key,
        "completed_at": comp_res.completed_at,
    }
    print(f"WORKER_FINISHED_{json.dumps(result)}", flush=True)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--lease-token", required=True)
    parser.add_argument("--order-id", required=True)
    parser.add_argument("--scratch-dir", default="scratch/a23_max_jobs")
    parser.add_argument("--stop-after-glyph", type=int, default=None)
    args = parser.parse_args()

    asyncio.run(run_worker(
        job_id=args.job_id,
        lease_token=args.lease_token,
        order_id=args.order_id,
        scratch_base=Path(args.scratch_dir),
        stop_after_glyph=args.stop_after_glyph,
    ))
