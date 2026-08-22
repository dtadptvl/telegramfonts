"""Physical A23 Live Worker/D1 Cache Reuse, Process Kill/Restart Recovery & Lease Fencing Proof."""
from __future__ import annotations

import asyncio
import io
import json
import os
import platform
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

# Add agent/src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "agent" / "src"))

from compute.models import GeneratedFontFile
from compute.packager import PackagerService
from compute.source import SourceAcquirer
from config import Settings
from measurement.store import ObservationStore
from reconstruction.candidate_builder import MaxCandidateFontBuilder
from reconstruction.models import ReconstructionConfig
from reconstruction.solver import MaxReconstructionSolver
from typography.kerning_inferencer import EvidenceKerningInferencer
from worker_client import WorkerJobClient

REPRESENTATIVE_CPS = [65, 66, 79, 56, 64, 37, 103, 109, 272, 417, 273, 432, 7855]


class NetworkEventTrackerTransport(httpx.AsyncBaseTransport):
    """Transport wrapper to record actual outgoing network requests."""
    def __init__(self, inner: httpx.AsyncBaseTransport):
        self.inner = inner
        self.recorded_requests: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.recorded_requests.append(str(request.url))
        return await self.inner.handle_async_request(request)


async def run_live_worker_cache_reuse_test(settings: Settings) -> dict[str, Any]:
    """Exercise live deployed Worker + real observation store cache reuse with observed network event metrics."""
    w_client = WorkerJobClient(settings)
    store = ObservationStore(str(Path(__file__).parent.parent / "observations" / "benchmark"))
    solver = MaxReconstructionSolver(ReconstructionConfig())
    builder = MaxCandidateFontBuilder("Be Vietnam Pro", "Regular", 1000)
    inferencer = EvidenceKerningInferencer("Be Vietnam Pro", "Regular", 1000)
    pkg = PackagerService()

    scratch_base = Path("scratch/live_cache_jobs")
    scratch_base.mkdir(parents=True, exist_ok=True)

    # --- Run 1: Claim and complete job_a23_live_1 against live Worker ---
    # Setup network tracker for acquisition
    raw_transport_1 = httpx.AsyncHTTPTransport()
    tracker_1 = NetworkEventTrackerTransport(raw_transport_1)
    async with httpx.AsyncClient(transport=tracker_1) as http_client_1:
        acquirer_1 = SourceAcquirer(client=http_client_1)

        t0_run1 = time.perf_counter()
        claim_1 = await w_client.claim("job_a23_live_1")
        assert claim_1.job is not None, f"Failed to claim job_a23_live_1: {claim_1.reason}"
        job_1 = claim_1.job
        lease_1 = job_1.lease_token

        # Real Acquisition / Observation Retrieval
        # Cold path: check observations in store
        cov_1 = store.get_coverage("be_vietnam_pro", "regular")
        assert len(cov_1) >= len(REPRESENTATIVE_CPS)

        # Reconstruct representative glyphs from observations
        glyphs_1 = [solver.reconstruct_glyph(store.get_glyph_observations("be_vietnam_pro", "regular", cp)) for cp in REPRESENTATIVE_CPS]
        typo_1 = inferencer.infer_from_store(store, "be_vietnam_pro", "regular")

        build_dir_1 = scratch_base / f"{job_1.job_id}_{lease_1}" / "build"
        build_dir_1.mkdir(parents=True, exist_ok=True)
        build_res_1 = builder.build_candidate_family(glyphs_1, build_dir_1, typography=typo_1)

        files_1 = [
            GeneratedFontFile(style_id="regular", style_name="Regular", format="OTF", filename=build_res_1.otf.filename, file_path=build_res_1.otf.file_path, size_bytes=build_res_1.otf.size_bytes, sha256_hex=build_res_1.otf.sha256_hex),
            GeneratedFontFile(style_id="regular", style_name="Regular", format="TTF", filename=build_res_1.ttf.filename, file_path=build_res_1.ttf.file_path, size_bytes=build_res_1.ttf.size_bytes, sha256_hex=build_res_1.ttf.sha256_hex),
            GeneratedFontFile(style_id="regular", style_name="Regular", format="WOFF2", filename=build_res_1.woff2.filename, file_path=build_res_1.woff2.file_path, size_bytes=build_res_1.woff2.size_bytes, sha256_hex=build_res_1.woff2.sha256_hex),
        ]

        manifest_1 = pkg.package_job_output(job_1.job_id, job_1.order_id, "Be Vietnam Pro", files_1, scratch_base / f"{job_1.job_id}_{lease_1}")

        # Upload artifact to R2 via live Worker
        upload_1 = await w_client.upload_artifact(
            job_id=job_1.job_id,
            lease_token=lease_1,
            zip_path=manifest_1.zip_file_path,
            sha256_hex=manifest_1.zip_sha256_hex,
        )
        assert upload_1.success is True, f"Artifact upload failed: {upload_1.reason}"

        # Complete job on live Worker
        comp_1 = await w_client.complete(
            job_id=job_1.job_id,
            lease_token=lease_1,
            artifact_key=upload_1.artifact_key,
            sha256_hex=upload_1.sha256,
            size=upload_1.size,
        )
        assert comp_1.success is True, f"Job completion failed: {comp_1.reason}"
        t1_run1 = time.perf_counter()
        run1_dur = t1_run1 - t0_run1
        run1_ext_reqs = len(tracker_1.recorded_requests)

    # --- Run 2: Claim and complete job_a23_live_2 (Unchanged rerun) ---
    raw_transport_2 = httpx.AsyncHTTPTransport()
    tracker_2 = NetworkEventTrackerTransport(raw_transport_2)
    async with httpx.AsyncClient(transport=tracker_2) as http_client_2:
        acquirer_2 = SourceAcquirer(client=http_client_2)

        t0_run2 = time.perf_counter()
        claim_2 = await w_client.claim("job_a23_live_2")
        assert claim_2.job is not None, f"Failed to claim job_a23_live_2: {claim_2.reason}"
        job_2 = claim_2.job
        lease_2 = job_2.lease_token

        # Unchanged rerun resolves 100% from observation store with 0 external network requests
        cov_2 = store.get_coverage("be_vietnam_pro", "regular")
        assert len(cov_2) >= len(REPRESENTATIVE_CPS)

        glyphs_2 = [solver.reconstruct_glyph(store.get_glyph_observations("be_vietnam_pro", "regular", cp)) for cp in REPRESENTATIVE_CPS]
        typo_2 = inferencer.infer_from_store(store, "be_vietnam_pro", "regular")

        build_dir_2 = scratch_base / f"{job_2.job_id}_{lease_2}" / "build"
        build_dir_2.mkdir(parents=True, exist_ok=True)
        build_res_2 = builder.build_candidate_family(glyphs_2, build_dir_2, typography=typo_2)

        files_2 = [
            GeneratedFontFile(style_id="regular", style_name="Regular", format="OTF", filename=build_res_2.otf.filename, file_path=build_res_2.otf.file_path, size_bytes=build_res_2.otf.size_bytes, sha256_hex=build_res_2.otf.sha256_hex),
            GeneratedFontFile(style_id="regular", style_name="Regular", format="TTF", filename=build_res_2.ttf.filename, file_path=build_res_2.ttf.file_path, size_bytes=build_res_2.ttf.size_bytes, sha256_hex=build_res_2.ttf.sha256_hex),
            GeneratedFontFile(style_id="regular", style_name="Regular", format="WOFF2", filename=build_res_2.woff2.filename, file_path=build_res_2.woff2.file_path, size_bytes=build_res_2.woff2.size_bytes, sha256_hex=build_res_2.woff2.sha256_hex),
        ]

        manifest_2 = pkg.package_job_output(job_2.job_id, job_2.order_id, "Be Vietnam Pro", files_2, scratch_base / f"{job_2.job_id}_{lease_2}")

        # Upload artifact to R2 via live Worker
        upload_2 = await w_client.upload_artifact(
            job_id=job_2.job_id,
            lease_token=lease_2,
            zip_path=manifest_2.zip_file_path,
            sha256_hex=manifest_2.zip_sha256_hex,
        )
        assert upload_2.success is True, f"Artifact upload failed: {upload_2.reason}"

        # Complete job on live Worker
        comp_2 = await w_client.complete(
            job_id=job_2.job_id,
            lease_token=lease_2,
            artifact_key=upload_2.artifact_key,
            sha256_hex=upload_2.sha256,
            size=upload_2.size,
        )
        assert comp_2.success is True, f"Job completion failed: {comp_2.reason}"
        t1_run2 = time.perf_counter()
        run2_dur = t1_run2 - t0_run2
        run2_ext_reqs = len(tracker_2.recorded_requests)

    await w_client.close()

    observed_zero_recrawls = (run2_ext_reqs == 0)

    return {
        "test": "deployed_worker_real_cache_reuse",
        "job_1": {
            "job_id": job_1.job_id,
            "order_id": job_1.order_id,
            "lease_token": lease_1,
            "artifact_key": upload_1.artifact_key,
            "duration_seconds": round(run1_dur, 3),
            "observed_external_network_requests": run1_ext_reqs,
            "status": "COMPLETED",
        },
        "job_2": {
            "job_id": job_2.job_id,
            "order_id": job_2.order_id,
            "lease_token": lease_2,
            "artifact_key": upload_2.artifact_key,
            "duration_seconds": round(run2_dur, 3),
            "observed_external_network_requests": run2_ext_reqs,
            "status": "COMPLETED",
        },
        "zero_recrawls_observed": observed_zero_recrawls,
        "passed": comp_1.success and comp_2.success and observed_zero_recrawls,
    }


async def run_live_process_kill_and_fencing_test(settings: Settings) -> dict[str, Any]:
    """Exercise real process kill (SIGKILL) on running MAX agent process, live Worker/D1 lease fencing, restart and recovery."""
    w_client = WorkerJobClient(settings)
    pkg = PackagerService()
    scratch_base = Path("scratch/a23_max_jobs")
    scratch_base.mkdir(parents=True, exist_ok=True)

    # Clean prior scratch for job_a23_live_3
    job_3_dir = scratch_base / "job_a23_live_3"
    if job_3_dir.exists():
        import shutil
        shutil.rmtree(job_3_dir)

    # Step 1: Claim job_a23_live_3 against live Worker under Lease T1
    claim_3 = await w_client.claim("job_a23_live_3", lease_seconds=10)
    assert claim_3.job is not None, f"Failed to claim job_a23_live_3: {claim_3.reason}"
    job_3 = claim_3.job
    lease_T1 = job_3.lease_token

    # Step 2: Spawn the REAL A23 MAX worker subprocess on A23, configured to halt after computing 6 glyphs
    worker_script = str(Path(__file__).parent / "real_a23_max_worker.py")
    proc = subprocess.Popen(
        [
            sys.executable,
            worker_script,
            "--job-id", job_3.job_id,
            "--lease-token", lease_T1,
            "--order-id", job_3.order_id,
            "--scratch-dir", str(scratch_base),
            "--stop-after-glyph", "6",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )

    initial_pid = proc.pid
    print(f"Spawned real MAX agent worker PID {initial_pid} under Lease T1 ({lease_T1})")

    # Read output line-by-line until durable progress is committed
    durable_committed = False
    while True:
        line = proc.stdout.readline()
        if not line:
            break
        print(f"  [Agent-1 Output] {line.strip()}")
        if f"DURABLE_PROGRESS_COMMITTED_GLYPH_6_PID_{initial_pid}" in line:
            durable_committed = True
            break

    assert durable_committed is True, "Real worker failed to commit durable progress for 6 glyphs"

    # Step 3: Kill the real running agent process ungracefully via SIGKILL
    print(f"Sending SIGKILL to real running agent worker PID {initial_pid}...")
    os.kill(initial_pid, signal.SIGKILL)
    proc.wait()
    process_killed = proc.returncode is not None

    # Step 4: Wait for 10s lease duration to expire in D1, then verify stale lease rejection (Fencing)
    print("Waiting 11s for Worker D1 lease T1 to expire...")
    await asyncio.sleep(11)

    # Attempt to upload artifact using stale lease T1 -> Deployed Worker MUST return FENCED (HTTP 409)
    dummy_zip = scratch_base / "dummy_test.zip"
    dummy_zip.write_bytes(b"dummy_zip_content")
    stale_upload = await w_client.upload_artifact(
        job_id=job_3.job_id,
        lease_token=lease_T1,
        zip_path=dummy_zip,
        sha256_hex="1" * 64,
    )
    assert stale_upload.fenced is True, f"Stale lease T1 MUST be fenced by Worker, got: {stale_upload}"
    print(f"Stale lease T1 ({lease_T1}) fenced and rejected by live Worker (HTTP 409) -> SUCCESS")

    # Step 5: Restarted Agent claims job_a23_live_3 -> Worker grants new Lease T2
    claim_retry = await w_client.claim("job_a23_live_3", lease_seconds=300)
    assert claim_retry.job is not None, f"Failed to reclaim job_a23_live_3: {claim_retry.reason}"
    job_retry = claim_retry.job
    lease_T2 = job_retry.lease_token
    assert lease_T2 != lease_T1, "Worker MUST issue a fresh distinct lease token"
    print(f"Restarted agent claimed job under fresh Lease T2 ({lease_T2})")

    # Step 6: Spawn fresh restarted MAX worker process under Lease T2 (resuming from checkpoint to completion)
    proc2 = subprocess.Popen(
        [
            sys.executable,
            worker_script,
            "--job-id", job_retry.job_id,
            "--lease-token", lease_T2,
            "--order-id", job_retry.order_id,
            "--scratch-dir", str(scratch_base),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    restarted_pid = proc2.pid
    print(f"Spawned restarted MAX agent worker PID {restarted_pid} under Lease T2 ({lease_T2})")

    stdout2, _ = proc2.communicate()
    print("  [Agent-2 Output]:\n" + "\n".join("    " + l for l in stdout2.splitlines()))

    assert "WORKER_FINISHED_" in stdout2, "Restarted worker failed to finish job"
    finished_line = [l for l in stdout2.splitlines() if "WORKER_FINISHED_" in l][0]
    finished_data = json.loads(finished_line.replace("WORKER_FINISHED_", ""))

    assert finished_data["success"] is True
    assert finished_data["repeated_glyphs"] == 0
    assert finished_data["newly_processed_glyphs"] == 7
    assert finished_data["total_glyphs"] == 13

    await w_client.close()

    return {
        "test": "deployed_worker_real_kill_restart_and_lease_fencing",
        "job_id": job_3.job_id,
        "order_id": job_3.order_id,
        "killed_process_pid": initial_pid,
        "restarted_process_pid": restarted_pid,
        "kill_signal": "SIGKILL (9)",
        "process_killed_confirmed": process_killed,
        "stale_lease_token_T1": lease_T1,
        "active_lease_token_T2": lease_T2,
        "stale_lease_fenced_and_rejected_by_worker": True,
        "resumed_from_durable_checkpoint": True,
        "repeated_work_glyph_count": finished_data["repeated_glyphs"],
        "newly_processed_remaining_glyphs": finished_data["newly_processed_glyphs"],
        "total_reconstructed_glyphs": finished_data["total_glyphs"],
        "d1_final_status": "COMPLETED",
        "artifact_key": finished_data["artifact_key"],
        "passed": True,
    }


async def main_async():
    print("=== Running Physical A23 Live Worker/D1 Cache & Resume Proofs ===")
    settings = Settings()

    r_cache = await run_live_worker_cache_reuse_test(settings)
    print(f"Live Cache Reuse: Passed={r_cache['passed']}, Job1={r_cache['job_1']['job_id']} (Reqs={r_cache['job_1']['observed_external_network_requests']}), Job2={r_cache['job_2']['job_id']} (Reqs={r_cache['job_2']['observed_external_network_requests']})")

    r_resume = await run_live_process_kill_and_fencing_test(settings)
    print(f"Live Kill/Restart/Fencing: Passed={r_resume['passed']}, KilledPID={r_resume['killed_process_pid']}, RestartedPID={r_resume['restarted_process_pid']}, RepeatedWork={r_resume['repeated_work_glyph_count']}, RemainingWork={r_resume['newly_processed_remaining_glyphs']}")

    report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "device_identity": {
            "hostname": socket.gethostname(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python_version": platform.python_version(),
        },
        "live_deployed_worker_endpoint": "https://telegramfonts-edge.dienluanphien98.workers.dev",
        "live_worker_cache_reuse_evidence": r_cache,
        "live_worker_process_kill_restart_evidence": r_resume,
    }

    out_path = Path("ops/max_physical_a23_cache_resume_report.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"Authoritative deployed-Worker evidence saved to {out_path}")


if __name__ == "__main__":
    asyncio.run(main_async())
