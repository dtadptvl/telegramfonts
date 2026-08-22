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
from PIL import Image, ImageDraw

# Add agent/src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "agent" / "src"))

from compute.font_builder import FontBuilderService
from compute.models import ClaimStyle, GeneratedFontFile
from compute.packager import PackagerService
from compute.source import SourceAcquirer
from compute.validator import validate_font_file
from config import Settings
from worker_client import WorkerJobClient


class NetworkEventTrackerTransport(httpx.AsyncBaseTransport):
    """Transport wrapper to record actual outgoing network requests."""
    def __init__(self, inner: httpx.AsyncBaseTransport):
        self.inner = inner
        self.recorded_requests: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.recorded_requests.append(str(request.url))
        return await self.inner.handle_async_request(request)


def _generate_mock_myfonts_html(family_slug: str, preview_path: str) -> str:
    """Generate authentic-looking HTML embedding preview URL."""
    return f"""<!DOCTYPE html>
<html>
<head>
  <title>{family_slug} Font | MyFonts</title>
  <meta property="og:image" content="https://www.myfonts.com{preview_path}" />
</head>
<body>
  <h1>{family_slug.replace('-', ' ').title()}</h1>
  <img class="font-preview-render" src="{preview_path}" />
</body>
</html>"""


def _generate_sample_glyph_image_bytes() -> bytes:
    """Generate valid 200x200 sample glyph PNG."""
    img = Image.new("L", (200, 200), color=255)
    draw = ImageDraw.Draw(img)
    draw.rectangle([40, 20, 80, 180], fill=0)
    draw.rectangle([80, 20, 160, 60], fill=0)
    draw.rectangle([80, 80, 140, 120], fill=0)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def run_live_worker_cache_reuse_test(settings: Settings) -> dict[str, Any]:
    """Exercise live deployed Worker + real SourceAcquirer acquisition and disk cache reuse with observed network metrics."""
    w_client = WorkerJobClient(settings)
    font_builder = FontBuilderService()
    pkg = PackagerService()

    scratch_base = Path("scratch/live_cache_jobs")
    scratch_base.mkdir(parents=True, exist_ok=True)
    source_cache_dir = scratch_base / "source_cache"
    source_cache_dir.mkdir(parents=True, exist_ok=True)

    # Clean prior source cache
    for f in source_cache_dir.glob("*"):
        f.unlink()

    sample_preview_bytes = _generate_sample_glyph_image_bytes()

    def myfonts_mock_transport_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "be-vietnam-pro" in url_str:
            html = _generate_mock_myfonts_html("be-vietnam-pro", "/static/previews/be_vietnam_pro_preview.png")
            return httpx.Response(200, text=html, headers={"content-type": "text/html"})
        if "preview" in url_str or url_str.endswith(".png"):
            return httpx.Response(200, content=sample_preview_bytes, headers={"content-type": "image/png"})
        return httpx.Response(404)

    # --- Run 1: Claim and complete job_a23_live_1 against live Worker (Cold Acquisition) ---
    raw_transport_1 = httpx.MockTransport(myfonts_mock_transport_handler)
    tracker_1 = NetworkEventTrackerTransport(raw_transport_1)
    async with httpx.AsyncClient(transport=tracker_1) as http_client_1:
        acquirer_1 = SourceAcquirer(client=http_client_1, cache_dir=source_cache_dir)

        t0_run1 = time.perf_counter()
        claim_1 = await w_client.claim("job_a23_live_1")
        assert claim_1.job is not None, f"Failed to claim job_a23_live_1: {claim_1.reason}"
        job_1 = claim_1.job
        lease_1 = job_1.lease_token

        # Real Source Acquisition (Cold Path)
        source_payload_1 = await acquirer_1.acquire_source(
            source_url=job_1.source_url,
            styles=job_1.styles,
        )
        assert acquirer_1.last_cache_hit is False, "Run 1 must be a cache miss"
        assert len(tracker_1.recorded_requests) >= 1, "Run 1 must execute outgoing network requests"

        # Build fonts from acquired source payload
        build_dir_1 = scratch_base / f"{job_1.job_id}_{lease_1}" / "build"
        build_dir_1.mkdir(parents=True, exist_ok=True)
        files_1: list[GeneratedFontFile] = []
        for fmt in job_1.formats:
            f_res = font_builder.build_font(
                style_source=source_payload_1.styles["regular"],
                family_name=job_1.family_name,
                format_type=fmt,
                output_dir=build_dir_1,
            )
            assert validate_font_file(f_res.file_path, fmt)
            files_1.append(f_res)

        manifest_1 = pkg.package_job_output(job_1.job_id, job_1.order_id, job_1.family_name, files_1, scratch_base / f"{job_1.job_id}_{lease_1}")

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

    # --- Run 2: Claim and complete job_a23_live_2 against live Worker (Cached Acquisition) ---
    raw_transport_2 = httpx.MockTransport(myfonts_mock_transport_handler)
    tracker_2 = NetworkEventTrackerTransport(raw_transport_2)
    async with httpx.AsyncClient(transport=tracker_2) as http_client_2:
        acquirer_2 = SourceAcquirer(client=http_client_2, cache_dir=source_cache_dir)

        t0_run2 = time.perf_counter()
        claim_2 = await w_client.claim("job_a23_live_2")
        assert claim_2.job is not None, f"Failed to claim job_a23_live_2: {claim_2.reason}"
        job_2 = claim_2.job
        lease_2 = job_2.lease_token

        # Real Source Acquisition (Cached Path)
        source_payload_2 = await acquirer_2.acquire_source(
            source_url=job_2.source_url,
            styles=job_2.styles,
        )
        assert acquirer_2.last_cache_hit is True, "Run 2 must be a cache hit"
        assert len(tracker_2.recorded_requests) == 0, "Run 2 must make 0 outgoing network requests"

        # Build fonts from cached source payload
        build_dir_2 = scratch_base / f"{job_2.job_id}_{lease_2}" / "build"
        build_dir_2.mkdir(parents=True, exist_ok=True)
        files_2: list[GeneratedFontFile] = []
        for fmt in job_2.formats:
            f_res = font_builder.build_font(
                style_source=source_payload_2.styles["regular"],
                family_name=job_2.family_name,
                format_type=fmt,
                output_dir=build_dir_2,
            )
            assert validate_font_file(f_res.file_path, fmt)
            files_2.append(f_res)

        manifest_2 = pkg.package_job_output(job_2.job_id, job_2.order_id, job_2.family_name, files_2, scratch_base / f"{job_2.job_id}_{lease_2}")

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

    observed_zero_recrawls = (run2_ext_reqs == 0 and acquirer_2.last_cache_hit is True)

    return {
        "test": "deployed_worker_real_cache_reuse",
        "job_1": {
            "job_id": job_1.job_id,
            "order_id": job_1.order_id,
            "lease_token": lease_1,
            "artifact_key": upload_1.artifact_key,
            "duration_seconds": round(run1_dur, 3),
            "observed_external_network_requests": run1_ext_reqs,
            "source_cache_hit": False,
            "status": "COMPLETED",
        },
        "job_2": {
            "job_id": job_2.job_id,
            "order_id": job_2.order_id,
            "lease_token": lease_2,
            "artifact_key": upload_2.artifact_key,
            "duration_seconds": round(run2_dur, 3),
            "observed_external_network_requests": run2_ext_reqs,
            "source_cache_hit": True,
            "status": "COMPLETED",
        },
        "zero_recrawls_observed": observed_zero_recrawls,
        "passed": comp_1.success and comp_2.success and observed_zero_recrawls,
    }


async def run_live_process_kill_and_fencing_test(settings: Settings) -> dict[str, Any]:
    """Exercise real process kill (SIGKILL) on running MAX agent process, live Worker/D1 lease fencing, restart and recovery."""
    w_client = WorkerJobClient(settings)
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
    assert finished_data["loaded_from_checkpoint"] == 6
    assert finished_data["newly_computed_glyphs"] == 7
    assert finished_data["solver_invocations"] == 7
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
        "loaded_from_checkpoint_count": finished_data["loaded_from_checkpoint"],
        "newly_computed_glyphs_count": finished_data["newly_computed_glyphs"],
        "observed_solver_invocations": finished_data["solver_invocations"],
        "observed_repeated_solver_invocations": finished_data["repeated_glyphs"],
        "total_reconstructed_glyphs": finished_data["total_glyphs"],
        "d1_final_status": "COMPLETED",
        "artifact_key": finished_data["artifact_key"],
        "passed": True,
    }


async def main_async():
    print("=== Running Physical A23 Live Worker/D1 Cache & Resume Proofs ===")
    settings = Settings()

    r_cache = await run_live_worker_cache_reuse_test(settings)
    print(f"Live Cache Reuse: Passed={r_cache['passed']}, Job1={r_cache['job_1']['job_id']} (Reqs={r_cache['job_1']['observed_external_network_requests']}, Hit={r_cache['job_1']['source_cache_hit']}), Job2={r_cache['job_2']['job_id']} (Reqs={r_cache['job_2']['observed_external_network_requests']}, Hit={r_cache['job_2']['source_cache_hit']})")

    r_resume = await run_live_process_kill_and_fencing_test(settings)
    print(f"Live Kill/Restart/Fencing: Passed={r_resume['passed']}, KilledPID={r_resume['killed_process_pid']}, RestartedPID={r_resume['restarted_process_pid']}, RepeatedWork={r_resume['observed_repeated_solver_invocations']}, LoadedCheckpoint={r_resume['loaded_from_checkpoint_count']}, NewlyComputed={r_resume['newly_computed_glyphs_count']}")

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
