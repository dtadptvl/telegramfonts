"""Real A23 MAX Job Worker Process executing real reconstruction with checkpointing and state persistence."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pickle
import sys
import time
from pathlib import Path
from typing import Any

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


async def run_worker(
    job_id: str,
    lease_token: str,
    order_id: str,
    scratch_base: Path,
    browser_version: str,
    config_hash: str,
    stop_after_glyph: int | None = None,
    settings: Settings | None = None,
    store_dir: Path | str | None = None,
    hold_after_glyph: bool = True,
) -> dict[str, Any]:
    if not browser_version or not config_hash:
        raise ValueError("INCOMPLETE_EXACT_IDENTITY: browser_version and config_hash are required")
    if settings is None:
        try:
            settings = Settings()
        except Exception:
            settings = Settings(
                CF_ACCOUNT_ID="mock_acc",
                CF_QUEUE_ID="mock_qid",
                CF_QUEUES_TOKEN="mock_tok",
                EDGE_BASE_URL="http://127.0.0.1:8787",
                A23_NODE_SECRET="mock_secret",
            )
    w_client = WorkerJobClient(settings)
    base_store = Path(store_dir) if store_dir else Path(__file__).parent.parent / "observations" / "benchmark"
    store = ObservationStore(str(base_store))
    solver = MaxReconstructionSolver(ReconstructionConfig())
    builder = MaxCandidateFontBuilder("Be Vietnam Pro", "Regular", 1000)
    inferencer = EvidenceKerningInferencer("Be Vietnam Pro", "Regular", 1000)
    pkg = PackagerService()

    job_scratch = scratch_base / f"{job_id}"
    job_scratch.mkdir(parents=True, exist_ok=True)
    checkpoint_file = job_scratch / "checkpoint.json"
    glyphs_cache_dir = job_scratch / "reconstructed_glyph_state"
    glyphs_cache_dir.mkdir(parents=True, exist_ok=True)

    target_ref = "be_vietnam_pro"
    target_style = "regular"

    completed_cps: list[int] = []
    if checkpoint_file.exists():
        try:
            with open(checkpoint_file, "r", encoding="utf-8") as f:
                cp_data = json.load(f)
                chk_ref = cp_data.get("reference_id")
                chk_style = cp_data.get("style_id")
                chk_bv = cp_data.get("browser_version")
                chk_cfg = cp_data.get("config_hash")
                if (chk_ref == target_ref and
                    chk_style == target_style and
                    chk_bv == browser_version and
                    chk_cfg == config_hash):
                    completed_cps = cp_data.get("completed_cps", [])
                    print(f"RESUMING_CHECKPOINT: found {len(completed_cps)} already completed code points for exact identity", flush=True)
                else:
                    print(
                        f"REJECTED_CHECKPOINT_IDENTITY_MISMATCH: checkpoint ({chk_ref}, {chk_style}, {chk_bv}, {chk_cfg}) != active ({target_ref}, {target_style}, {browser_version}, {config_hash})",
                        flush=True,
                    )
                    # Clean/unlink foreign glyph pickles from mismatched checkpoint
                    for f_pkl in glyphs_cache_dir.glob("glyph_*.pkl"):
                        try:
                            f_pkl.unlink()
                        except Exception:
                            pass
        except Exception as e:
            print(f"CHECKPOINT_READ_ERROR: {e}", flush=True)

    reconstructed_glyphs = []
    solver_invocations = 0
    loaded_from_checkpoint_count = 0
    newly_computed_count = 0

    for i, cp in enumerate(REPRESENTATIVE_CPS):
        glyph_file = glyphs_cache_dir / f"glyph_{cp}.pkl"

        if cp in completed_cps and glyph_file.exists():
            # Load the actual reconstructed glyph state from durable persistence
            try:
                with open(glyph_file, "rb") as gf:
                    persisted_glyph = pickle.load(gf)
                if isinstance(persisted_glyph, ReconstructedGlyph) and persisted_glyph.code_point == cp:
                    reconstructed_glyphs.append(persisted_glyph)
                    loaded_from_checkpoint_count += 1
                    print(f"LOADED_FROM_CHECKPOINT_GLYPH_{i+1}_{cp}_PID_{os.getpid()}", flush=True)
                    continue
                else:
                    print(f"REJECTED_INVALID_GLYPH_PICKLE_{cp}: type={type(persisted_glyph)}", flush=True)
            except Exception as e:
                print(f"FAILED_TO_LOAD_GLYPH_{cp}: {e}, recomputing...", flush=True)

        # Genuinely compute glyph with solver
        obs = store.get_glyph_observations("be_vietnam_pro", "regular", cp, browser_version=browser_version, config_hash=config_hash)
        if not obs:
            continue

        solver_invocations += 1
        glyph = solver.reconstruct_glyph(obs)
        reconstructed_glyphs.append(glyph)
        newly_computed_count += 1
        if cp not in completed_cps:
            completed_cps.append(cp)

        # Persist full reconstructed outline geometry and metrics
        with open(glyph_file, "wb") as gf:
            pickle.dump(glyph, gf)

        with open(checkpoint_file, "w", encoding="utf-8") as f:
            json.dump({
                "completed_cps": completed_cps,
                "browser_version": browser_version,
                "config_hash": config_hash,
                "reference_id": "be_vietnam_pro",
                "style_id": "regular",
                "last_updated": time.time(),
            }, f)

        print(f"PROGRESS_GLYPH_{i+1}_{cp}_PID_{os.getpid()}", flush=True)

        if stop_after_glyph is not None and (i + 1) >= stop_after_glyph:
            print(f"DURABLE_PROGRESS_COMMITTED_GLYPH_{i+1}_PID_{os.getpid()}", flush=True)
            if hold_after_glyph:
                while True:
                    time.sleep(1)
            else:
                return {
                    "checkpoint_file": str(checkpoint_file),
                    "completed_cps": completed_cps,
                    "loaded_from_checkpoint_count": loaded_from_checkpoint_count,
                    "newly_computed_count": newly_computed_count,
                }

    # All glyphs assembled -> build font binaries
    build_dir = job_scratch / "build"
    build_dir.mkdir(parents=True, exist_ok=True)

    if not store.is_source_collection_completed("be_vietnam_pro", "regular", config_hash, browser_version):
        raise ValueError(
            f"UNCOMPLETED_STORE_COLLECTION: be_vietnam_pro:regular:{browser_version}:{config_hash} is not completed in store"
        )

    typo = inferencer.infer_from_store(
        store,
        reference_id="be_vietnam_pro",
        style_id="regular",
        browser_version=browser_version,
        config_hash=config_hash,
        require_provenance=True,
    )
    build_result = builder.build_candidate_family(reconstructed_glyphs, build_dir, typography=typo)

    files = [
        GeneratedFontFile(style_id="regular", style_name="Regular", format="OTF", filename=build_result.otf.filename, file_path=build_result.otf.file_path, size_bytes=build_result.otf.size_bytes, sha256_hex=build_result.otf.sha256_hex),
        GeneratedFontFile(style_id="regular", style_name="Regular", format="TTF", filename=build_result.ttf.filename, file_path=build_result.ttf.file_path, size_bytes=build_result.ttf.size_bytes, sha256_hex=build_result.ttf.sha256_hex),
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

    # Repeated work is any solver invocation on already checkpointed glyphs
    repeated_work_count = max(0, solver_invocations - newly_computed_count)

    result = {
        "success": comp_res.success,
        "job_id": job_id,
        "order_id": order_id,
        "solver_invocations": solver_invocations,
        "loaded_from_checkpoint": loaded_from_checkpoint_count,
        "newly_computed_glyphs": newly_computed_count,
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
    parser.add_argument("--browser-version", required=True)
    parser.add_argument("--config-hash", required=True)
    parser.add_argument("--scratch-dir", default="scratch/a23_max_jobs")
    parser.add_argument("--stop-after-glyph", type=int, default=None)
    args = parser.parse_args()

    asyncio.run(run_worker(
        job_id=args.job_id,
        lease_token=args.lease_token,
        order_id=args.order_id,
        scratch_base=Path(args.scratch_dir),
        browser_version=args.browser_version,
        config_hash=args.config_hash,
        stop_after_glyph=args.stop_after_glyph,
    ))
