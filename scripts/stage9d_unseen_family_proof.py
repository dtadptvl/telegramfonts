"""LOCAL_ONLY Stage 9D unseen-family proof (Issue #71 ACCEPT).

Runs the production runner archive-miss gate end-to-end for one family/style
that is not Be Vietnam Pro and not hardcoded in production or existing
reconstruction fixtures:

  real Chromium observation -> real ObservationStore -> production
  JobRunner miss path -> Stage 9D release gate (fit-only deterministic
  optimization + four real consumers) -> attested archive -> package.

Zero production/remote mutation: queue/worker transport boundaries are local
stubs; source observation is public read-only. Fails closed (exit BLOCKED)
when a required real capability is unavailable; never substitutes a mock PASS.

Emits a machine-derived proof artifact under ops/ containing exact tuple,
fit/held-out fingerprints, convergence trace, four-consumer evidence summary,
report/model/policy hashes, artifact hashes, and package hash.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import shutil
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent" / "src"))

import fontTools  # noqa: E402
import freetype  # noqa: E402
import uharfbuzz  # noqa: E402

from compute.archive import FinalFontArchive  # noqa: E402
from compute.models import ClaimStyle  # noqa: E402
from compute.source import SourceAcquirer  # noqa: E402
from config import Settings  # noqa: E402
from fidelity.release_gate import Stage9DReleaseGate  # noqa: E402
from measurement.browser_session import find_chromium_executable  # noqa: E402
from measurement.models import ObservationConfig  # noqa: E402
from measurement.store import ObservationStore  # noqa: E402
from queue_client import QueueMessage  # noqa: E402
from runner import A23Runner, RunnerAction  # noqa: E402
from worker_client import (  # noqa: E402
    ClaimResult,
    ClaimedJob,
    CompleteResult,
    FailResult,
    HeartbeatResult,
    UploadResult,
)

DEFAULT_URL = "https://www.myfonts.com/collections/futura-font-linotype"

BOUNDED_CONFIG = ObservationConfig(
    resolutions=(64, 128),
    base_subpixel_phases=((0.0, 0.0),),
    expanded_subpixel_phases=((0.0, 0.0),),
    held_out_subpixel_phases=((0.25, 0.25),),
    metric_sizes_px=(32.0, 64.0),
    feature_probes=(("kern", "AV"),),
)


class ProofQueueClient:
    def __init__(self) -> None:
        self.acks: list[str] = []
        self.retries: list = []

    async def acknowledge_messages(self, lease_ids):
        self.acks.extend(lease_ids)

    async def retry_messages(self, retries):
        self.retries.extend(retries)

    async def pull_messages(self, *args, **kwargs):
        return []

    async def close(self):
        pass


class ProofWorkerClient:
    def __init__(self, job: ClaimedJob) -> None:
        self.job = job
        self.uploads: list[dict] = []
        self.completes: list[dict] = []
        self.fails: list[str] = []

    async def claim(self, job_id):
        return ClaimResult(status="CLAIMED", queue_action="claimed", job=self.job)

    async def heartbeat(self, job_id, lease_token):
        return HeartbeatResult(success=True, fenced=False, lease_expires_at=self.job.lease_expires_at)

    async def upload_artifact(self, job_id, lease_token, zip_path, sha256_hex):
        data = Path(zip_path).read_bytes()
        self.uploads.append(
            {
                "declared_sha256": sha256_hex,
                "recomputed_sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
            }
        )
        return UploadResult(
            success=True,
            fenced=False,
            artifact_key=f"local-proof/{job_id}.zip",
            sha256=sha256_hex,
            size=len(data),
        )

    async def complete(self, job_id, lease_token, artifact_key, sha256_hex, size, parts=None):
        self.completes.append({"job_id": job_id, "artifact_key": artifact_key, "sha256": sha256_hex})
        return CompleteResult(success=True, status="COMPLETED", queue_action="ack", completed_at=1)

    async def fail(self, job_id, lease_token, retryable, reason_code="UNSPECIFIED_FAILURE"):
        self.fails.append(reason_code)
        return FailResult(
            success=True, fenced=False, status="RECORDED", queue_action="ack", reason=reason_code
        )

    async def close(self):
        pass


async def run_production_runner_miss_gate(source_url: str, work_root: Path, formats: list[str]) -> dict:
    store_dir = work_root / "observations"
    store_dir.mkdir(parents=True, exist_ok=True)

    acquirer = SourceAcquirer(observation_store_dir=store_dir, observation_config=BOUNDED_CONFIG)
    settings = Settings(
        CF_ACCOUNT_ID="local_proof",
        CF_QUEUE_ID="local_proof_queue",
        CF_QUEUES_TOKEN="local_proof_token",
        EDGE_BASE_URL="http://localhost:0",
        A23_NODE_SECRET="local_proof_secret",
        A23_WORKER_ID="local-proof-worker",
        SCRATCH_DIR=work_root / "scratch",
        FONT_ARCHIVE_ROOT=work_root / "archive_root",
    )
    archive = FinalFontArchive(settings.FONT_ARCHIVE_ROOT, settings.SCRATCH_DIR / "archive_index.sqlite3")

    job = ClaimedJob(
        job_id="issue71-unseen-family-proof",
        order_id="issue71-proof-order",
        lease_token="12345678-1234-1234-1234-123456789abc",
        lease_expires_at=int(time.time() * 1000) + 3600_000,
        source_url=source_url,
        family_name=None,
        foundry=None,
        styles=[ClaimStyle("regular", "Regular")],
        formats=formats,
    )
    queue = ProofQueueClient()
    worker = ProofWorkerClient(job)
    runner = A23Runner(
        settings,
        queue_client=queue,
        worker_client=worker,
        source_acquirer=acquirer,
        archive=archive,
    )
    msg = QueueMessage(
        id="proof_msg",
        lease_id="proof_lease",
        body_raw=json.dumps({"job_id": job.job_id}),
        attempts=1,
        job_id=job.job_id,
    )

    started = time.time()
    result = await runner.process_message(msg)
    await runner.close()

    evidence = {
        "runner_action": result.action.value,
        "runner_reason": result.reason,
        "uploads": worker.uploads,
        "completes": worker.completes,
        "fails": worker.fails,
        "queue_acks": queue.acks,
        "elapsed_seconds": round(time.time() - started, 2),
    }
    if result.action != RunnerAction.ACKED or not worker.uploads or worker.fails:
        raise RuntimeError(f"PROOF_RUNNER_GATE_FAILED: {json.dumps(evidence, sort_keys=True)}")
    return evidence


def read_attestations(archive: FinalFontArchive, formats: list[str]) -> dict[str, dict]:
    attestations: dict[str, dict] = {}
    with archive._connect() as conn:
        for row in conn.execute("SELECT format, attestation_json, attestation_hash FROM final_fonts"):
            fmt = str(row["format"]).upper()
            if fmt in formats:
                attestations[fmt] = {
                    "payload": json.loads(row["attestation_json"]),
                    "stored_hash": row["attestation_hash"],
                }
    return attestations


async def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 9D unseen-family LOCAL_ONLY proof")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--formats", default="TTF,OTF")
    parser.add_argument("--work-root", default=str(REPO_ROOT / "scratch" / "stage9d_unseen_family_proof"))
    args = parser.parse_args()
    formats = [f.strip().upper() for f in args.formats.split(",") if f.strip()]
    work_root = Path(args.work_root)
    work_root.mkdir(parents=True, exist_ok=True)

    # Required real capabilities (fail closed; no mock substitution).
    chromium_exe = find_chromium_executable()
    capabilities = {
        "chromium_executable": chromium_exe,
        "fonttools_version": getattr(fontTools, "version", "unknown"),
        "freetype_binding": "freetype-py",
        "harfbuzz_binding": f"uharfbuzz {uharfbuzz.version_string()}",
        "python": platform.python_version(),
        "platform": platform.platform(),
    }
    print(f"[proof] capabilities: {json.dumps(capabilities, sort_keys=True)}")

    # Phase 1: production runner archive-miss gate (real browser observation).
    print(f"[proof] running production runner miss gate for {args.url} formats={formats}")
    runner_evidence = await run_production_runner_miss_gate(args.url, work_root, formats)
    package = runner_evidence["uploads"][0]
    assert package["declared_sha256"] == package["recomputed_sha256"], "PROOF_PACKAGE_SHA_MISMATCH"
    print(f"[proof] runner ACKED; package sha={package['declared_sha256']}")

    # Phase 2: verify runner-attested archive entries and bind the full
    # convergence trace via one deterministic direct gate run per format.
    archive = FinalFontArchive(work_root / "archive_root", work_root / "scratch" / "archive_index.sqlite3")
    attestations = read_attestations(archive, formats)
    if set(attestations) != set(formats):
        raise RuntimeError(f"PROOF_MISSING_ATTESTATIONS: {sorted(attestations)}")

    store = ObservationStore(work_root / "observations")
    config = BOUNDED_CONFIG
    per_format: dict[str, dict] = {}
    artifact_dir = REPO_ROOT / "ops" / "stage9d_unseen_family"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    for fmt in formats:
        att = attestations[fmt]["payload"]
        stored_hash = attestations[fmt]["stored_hash"]
        recomputed = hashlib.sha256(
            json.dumps(att, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if recomputed != stored_hash:
            raise RuntimeError(f"PROOF_ATTESTATION_HASH_MISMATCH_{fmt}")
        if att["overall_status"] != "PASS" or not att["optimizer_converged"]:
            raise RuntimeError(f"PROOF_ATTESTATION_NOT_PASS_{fmt}")

        # Deterministic direct run over the exact persisted tuple.
        direct = await Stage9DReleaseGate.execute(
            store=store,
            config=config,
            reference_id=att["reference_id"],
            style_id=att["style_id"],
            family_name=att["reference_id"].replace("_", " ").title(),
            style_name="Regular",
            browser_version=att["browser_version"],
            format_type=fmt,
            output_dir=work_root / f"direct_{fmt.lower()}",
        )
        if not direct.is_publishable or direct.attestation is None:
            raise RuntimeError(f"PROOF_DIRECT_GATE_NOT_PUBLISHABLE_{fmt}: {direct.failure_reasons}")

        checks = {
            "model_hash": direct.model_hash == att["model_hash"],
            "report_hash": direct.report_hash == att["report_hash"],
            "optimizer_trace_hash": direct.trace.compute_trace_hash() == att["optimizer_trace_hash"],
            "artifact_sha256": direct.candidate_artifact_sha == att["artifact_sha256"],
            "fit_fingerprint": direct.fit_set_fingerprint == att["fit_set_fingerprint"],
            "held_out_fingerprint": direct.held_out_set_fingerprint == att["held_out_set_fingerprint"],
            "policy_hash": direct.report.policy_hash == att["policy_hash"],
        }
        if not all(checks.values()):
            raise RuntimeError(f"PROOF_DETERMINISM_MISMATCH_{fmt}: {json.dumps(checks, sort_keys=True)}")

        # Persist the exact PASS-gated artifact bytes under ops/.
        entry = archive.get_attested(archive_identity_for(archive, att, fmt))
        if entry is None:
            raise RuntimeError(f"PROOF_ARCHIVE_ATTESTED_MISS_{fmt}")
        if entry.sha256_hex != att["artifact_sha256"]:
            raise RuntimeError(f"PROOF_ARCHIVE_SHA_MISMATCH_{fmt}")
        dest = artifact_dir / f"{att['reference_id']}_{att['style_id']}_{fmt.lower()}.{att['artifact_sha256'][:16]}.{fmt.lower()}"
        shutil.copyfile(entry.file_path, dest)

        consumer_gate = direct.report.consumer_gate
        per_format[fmt] = {
            "attestation": att,
            "attestation_hash_verified": True,
            "determinism_checks": checks,
            "convergence_trace": {
                "optimizer_version": direct.trace.optimizer_version,
                "input_fingerprint": direct.trace.input_fingerprint,
                "policy": direct.trace.policy.to_dict(),
                "total_iterations": direct.trace.total_iterations,
                "converged": direct.trace.converged,
                "stop_reason": direct.trace.stop_reason,
                "trace_hash": direct.trace.compute_trace_hash(),
                "glyphs": [
                    {
                        "code_point": r.code_point,
                        "initial_objective": r.initial_objective,
                        "final_objective": r.final_objective,
                        "iterations": r.iterations,
                        "stop_reason": r.stop_reason,
                        "accepted_objective_trace": list(r.accepted_objective_trace),
                    }
                    for r in direct.trace.records
                ],
            },
            "four_consumer_evidence": {
                "status": consumer_gate.status,
                "fonttools_passed": consumer_gate.fonttools_passed,
                "freetype_passed": consumer_gate.freetype_passed,
                "harfbuzz_passed": consumer_gate.harfbuzz_passed,
                "chromium_passed": consumer_gate.chromium_passed,
                "consumer_bundle_hash": consumer_gate.consumer_bundle_hash,
            },
            "report_overall_status": direct.report.overall_status,
            "artifact": {
                "format": fmt,
                "sha256": att["artifact_sha256"],
                "size_bytes": att["artifact_size_bytes"],
                "ops_copy_path": str(dest.relative_to(REPO_ROOT)).replace("\\", "/"),
            },
        }
        direct.cleanup()
        print(f"[proof] {fmt}: PASS bound; artifact sha={att['artifact_sha256']}")

    proof = {
        "schema": "stage9d-unseen-family-proof/v1",
        "issue": 71,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "policy": "LOCAL_ONLY; production mutations=0; public read-only source observation only",
        "source_url": args.url,
        "family_reference_id": per_format[formats[0]]["attestation"]["reference_id"],
        "style_id": per_format[formats[0]]["attestation"]["style_id"],
        "exact_tuple": {
            "reference_id": per_format[formats[0]]["attestation"]["reference_id"],
            "style_id": per_format[formats[0]]["attestation"]["style_id"],
            "browser_version": per_format[formats[0]]["attestation"]["browser_version"],
            "config_hash": per_format[formats[0]]["attestation"]["config_hash"],
        },
        "capabilities": capabilities,
        "runner_miss_gate": runner_evidence,
        "package": {
            "zip_sha256": package["declared_sha256"],
            "size_bytes": package["size_bytes"],
        },
        "formats": per_format,
    }

    proof_path = REPO_ROOT / "ops" / "stage9d_unseen_family_proof.json"
    serialized = json.dumps(proof, sort_keys=True, indent=2, separators=(",", ": "))
    proof_path.write_text(serialized + "\n", encoding="utf-8")
    proof_sha = hashlib.sha256(proof_path.read_bytes()).hexdigest()
    print(f"[proof] artifact written: {proof_path} sha256={proof_sha}")
    print(f"PROOF_SHA256={proof_sha}")
    return 0


def archive_identity_for(archive: FinalFontArchive, att: dict, fmt: str):
    from compute.archive import ArchiveIdentity

    with archive._connect() as conn:
        row = conn.execute(
            "SELECT source_identity, family_name, style_id, style_name, mode, observation_identity, config_version "
            "FROM final_fonts WHERE format = ? LIMIT 1",
            (fmt,),
        ).fetchone()
    return ArchiveIdentity(
        source_identity=row["source_identity"],
        family_name=row["family_name"],
        style_id=row["style_id"],
        style_name=row["style_name"],
        mode=row["mode"],
        format=fmt,
        observation_identity=row["observation_identity"],
        config_version=row["config_version"],
    )


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    except Exception as exc:
        print(f"BLOCKED: {type(exc).__name__}: {exc}")
        rc = 2
    raise SystemExit(rc)
