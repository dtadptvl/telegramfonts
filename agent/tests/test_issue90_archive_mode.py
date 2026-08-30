"""Issue #90 (D21): safe A23 archive mode — explicit versioned NO_LOCAL_ARCHIVE.

Covers the archive-mode decision identity semantics, fail-closed
contradictions, runner integration under NO_LOCAL_ARCHIVE (delivery works,
local L1 reuse disabled, repeat orders recompute, mode truth observable),
the upload/complete fenced fail-closed queue branches (causal), the
readiness report mode surface, and the D12 Debian supervisor adapter mode
selection.
"""
import io
import json
import time
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.integration

from PIL import Image, ImageDraw

from compute.archive import (
    ARCHIVE_MODE_AUTO,
    ARCHIVE_MODE_EXTERNAL_EXT4,
    ARCHIVE_MODE_IDENTITIES,
    ARCHIVE_MODE_NO_LOCAL_ARCHIVE,
    ARCHIVE_MODE_VERSIONS,
    FinalFontArchive,
    resolve_archive_mode,
)
from compute.source import SourceAcquirer
from config import Settings
from queue_client import CloudflareQueueClient, QueueMessage
from runner import A23Runner, RunnerAction
from worker_client import WorkerJobClient

ROOT = Path(__file__).parents[2]
SUPERVISOR = ROOT / "scripts" / "debian_worker_supervisor.sh"


def _make_test_image_bytes(stroke_x0: int, stroke_x1: int) -> bytes:
    img = Image.new("L", (100, 100), color=255)
    draw = ImageDraw.Draw(img)
    draw.rectangle([stroke_x0, 10, stroke_x1, 90], fill=0)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class FixtureSourceAcquirer(SourceAcquirer):
    """Preview-input fixture adapter (mirrors test_runner.py)."""

    def __init__(self, preview_bytes: bytes, **kwargs):
        super().__init__(**kwargs)
        self.preview_bytes = preview_bytes
        self.store_dir = None
        self.store = None

    async def acquire_source(self, source_url, styles, preview_input=None, allow_web_fallback=False):
        return await super().acquire_source(
            source_url,
            styles,
            preview_input=self.preview_bytes,
            allow_web_fallback=allow_web_fallback,
        )


# ---------------------------------------------------------------------------
# 1. Archive-mode resolution identity semantics (fail-closed).
# ---------------------------------------------------------------------------


def test_auto_mode_without_root_resolves_no_local_archive(test_settings: Settings):
    resolution = resolve_archive_mode(test_settings)
    assert resolution.mode == ARCHIVE_MODE_NO_LOCAL_ARCHIVE
    assert resolution.identity == ARCHIVE_MODE_IDENTITIES[ARCHIVE_MODE_NO_LOCAL_ARCHIVE]
    assert resolution.identity == "no_local_archive_v1"
    assert resolution.archive_enabled is False
    assert resolution.explicit is False
    assert resolution.to_dict()["version"] == ARCHIVE_MODE_VERSIONS[ARCHIVE_MODE_NO_LOCAL_ARCHIVE]


def test_auto_mode_with_root_resolves_external_ext4(test_settings: Settings, tmp_path: Path):
    settings = test_settings.model_copy(update={"FONT_ARCHIVE_ROOT": tmp_path / "archive_root"})
    resolution = resolve_archive_mode(settings)
    assert resolution.mode == ARCHIVE_MODE_EXTERNAL_EXT4
    assert resolution.identity == "external_ext4_archive_v1"
    assert resolution.archive_enabled is True
    assert resolution.explicit is False


def test_auto_mode_with_injected_archive_resolves_external(test_settings: Settings):
    resolution = resolve_archive_mode(test_settings, archive_present=True)
    assert resolution.mode == ARCHIVE_MODE_EXTERNAL_EXT4
    assert resolution.archive_enabled is True


def test_explicit_no_local_archive_rejects_configured_root(test_settings: Settings, tmp_path: Path):
    with pytest.raises(ValueError, match="ARCHIVE_MODE_CONTRADICTION_FONT_ARCHIVE_ROOT_SET"):
        Settings(
            _env_file=None,
            CF_ACCOUNT_ID="test_account_123",
            CF_QUEUE_ID="test_queue_456",
            CF_QUEUES_TOKEN="test_cf_token_secret",
            EDGE_BASE_URL="http://localhost:8787",
            A23_NODE_SECRET="test_node_secret_abc",
            A23_WORKER_ID="test-worker-1",
            SCRATCH_DIR=tmp_path / "scratch",
            FONT_ARCHIVE_ROOT=tmp_path / "archive_root",
            FONT_ARCHIVE_MODE="NO_LOCAL_ARCHIVE",
        )


def test_explicit_external_ext4_requires_root(test_settings: Settings, tmp_path: Path):
    with pytest.raises(ValueError, match="ARCHIVE_MODE_EXTERNAL_EXT4_REQUIRES_FONT_ARCHIVE_ROOT"):
        Settings(
            _env_file=None,
            CF_ACCOUNT_ID="test_account_123",
            CF_QUEUE_ID="test_queue_456",
            CF_QUEUES_TOKEN="test_cf_token_secret",
            EDGE_BASE_URL="http://localhost:8787",
            A23_NODE_SECRET="test_node_secret_abc",
            A23_WORKER_ID="test-worker-1",
            SCRATCH_DIR=tmp_path / "scratch",
            FONT_ARCHIVE_MODE="EXTERNAL_EXT4",
        )


def test_unknown_archive_mode_fails_closed(test_settings: Settings, tmp_path: Path):
    with pytest.raises(ValueError, match="UNSUPPORTED_ARCHIVE_MODE"):
        Settings(
            _env_file=None,
            CF_ACCOUNT_ID="test_account_123",
            CF_QUEUE_ID="test_queue_456",
            CF_QUEUES_TOKEN="test_cf_token_secret",
            EDGE_BASE_URL="http://localhost:8787",
            A23_NODE_SECRET="test_node_secret_abc",
            A23_WORKER_ID="test-worker-1",
            SCRATCH_DIR=tmp_path / "scratch",
            FONT_ARCHIVE_MODE="LOOPBACK_PRETEND_ARCHIVE",
        )


def test_explicit_no_local_archive_runner_forbids_injected_archive(test_settings: Settings, tmp_path: Path):
    settings = test_settings.model_copy(update={"FONT_ARCHIVE_MODE": ARCHIVE_MODE_NO_LOCAL_ARCHIVE})
    fake_archive = FinalFontArchive(tmp_path / "archive_root", tmp_path / "index.sqlite3")
    with pytest.raises(ValueError, match="ARCHIVE_FORBIDDEN_IN_NO_LOCAL_ARCHIVE_MODE"):
        A23Runner(
            settings,
            queue_client=None,
            worker_client=None,
            archive=fake_archive,
        )


def test_no_local_archive_runner_disables_archive(test_settings: Settings):
    settings = test_settings.model_copy(update={"FONT_ARCHIVE_MODE": ARCHIVE_MODE_NO_LOCAL_ARCHIVE})
    runner = A23Runner(settings, queue_client=None, worker_client=None)
    assert runner.archive is None
    assert runner.archive_mode.mode == ARCHIVE_MODE_NO_LOCAL_ARCHIVE
    assert runner.archive_mode.archive_enabled is False


# ---------------------------------------------------------------------------
# 2. Runner integration: NO_LOCAL_ARCHIVE delivery works, repeat recomputes,
#    mode truth observable, zero archive side effects.
# ---------------------------------------------------------------------------


def _claim_payload(job_id: str, order_id: str) -> dict:
    return {
        "job_id": job_id,
        "order_id": order_id,
        "lease_token": "12345678-1234-1234-1234-123456789abc",
        "lease_expires_at": int(time.time() * 1000) + 300000,
        "source_url": "https://www.myfonts.com/collections/be-vietnam-pro",
        "family_name": "Be Vietnam Pro",
        "styles": [{"id": "regular", "display_name": "Regular"}],
        "formats": ["TTF"],
        "mode": "ORIGINAL",
    }


@pytest.mark.asyncio
async def test_no_local_archive_repeat_order_recomputes_and_delivers(test_settings: Settings):
    """NO_LOCAL_ARCHIVE: both the first order and the exact repeat deliver;
    the repeat recomputes (no L1 archive hit is possible); the mode truth is
    observable in the job trace; no archive index is ever created."""
    settings = test_settings.model_copy(update={"FONT_ARCHIVE_MODE": ARCHIVE_MODE_NO_LOCAL_ARCHIVE})
    preview_bytes = _make_test_image_bytes(20, 60)
    acked_leases: list[str] = []
    uploaded_keys: list[str] = []
    completed_jobs: list[str] = []
    claims = iter(["job_nla_1", "job_nla_2"])

    def queue_handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        if "acks" in data:
            acked_leases.extend([a["lease_id"] for a in data["acks"]])
        return httpx.Response(200, json={"success": True})

    def worker_handler(request: httpx.Request) -> httpx.Response:
        if "claim" in request.url.path:
            job_id = next(claims)
            return httpx.Response(200, json=_claim_payload(job_id, f"ord_{job_id}"))
        if "heartbeat" in request.url.path:
            return httpx.Response(
                200, json={"success": True, "lease_expires_at": int(time.time() * 1000) + 300000}
            )
        if "artifact" in request.url.path:
            key = f"artifacts/ord/{request.headers['X-Artifact-SHA256']}.zip"
            uploaded_keys.append(key)
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "artifact_key": key,
                    "sha256": request.headers["X-Artifact-SHA256"],
                    "size": len(request.content),
                },
            )
        if "complete" in request.url.path:
            completed_jobs.append(str(request.url))
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "status": "COMPLETED",
                    "queue_action": "ack",
                    "completed_at": int(time.time() * 1000),
                },
            )
        return httpx.Response(404)

    def source_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=preview_bytes, headers={"content-type": "image/png"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(queue_handler)) as q_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(worker_handler)) as w_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(source_handler)) as s_http:
        q_client = CloudflareQueueClient(settings, client=q_http)
        w_client = WorkerJobClient(settings, client=w_http)
        runner = A23Runner(
            settings,
            q_client,
            w_client,
            source_acquirer=FixtureSourceAcquirer(preview_bytes, client=s_http),
        )

        for index in (1, 2):
            msg = QueueMessage(
                id=f"m{index}",
                lease_id=f"l_nla_{index}",
                body_raw=json.dumps({"job_id": f"job_nla_{index}"}),
                attempts=1,
                job_id=f"job_nla_{index}",
            )
            res = await runner.process_message(msg, preview_input=preview_bytes)
            assert res.action == RunnerAction.ACKED
            # Mode truth observable in every job report trace.
            assert runner.last_reuse_trace["archive_mode"]["mode"] == ARCHIVE_MODE_NO_LOCAL_ARCHIVE
            assert runner.last_reuse_trace["archive_mode"]["identity"] == "no_local_archive_v1"
            assert runner.last_reuse_trace["archive_mode"]["archive_enabled"] is False
            # No L1 archive hit events can exist in NO_LOCAL_ARCHIVE mode.
            assert all(
                not str(event.get("key", "")).startswith("L1_")
                for event in runner.last_reuse_trace["events"]
            )

        await runner.close()

    # Delivery worked for both orders: two uploads, two durable completions.
    assert len(uploaded_keys) == 2
    assert len(completed_jobs) == 2
    assert "l_nla_1" in acked_leases and "l_nla_2" in acked_leases
    # Local L1 archive reuse disabled: no archive index ever materialized.
    assert not (Path(settings.SCRATCH_DIR) / "font_archive_index.sqlite3").exists()


# ---------------------------------------------------------------------------
# 3. Fenced fail-closed queue lifecycle (causal branches).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_upload_fenced_aborts_without_ack_or_complete(test_settings: Settings):
    preview_bytes = _make_test_image_bytes(20, 60)
    acked_leases: list[str] = []
    completed: list[str] = []

    def queue_handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        if "acks" in data:
            acked_leases.extend([a["lease_id"] for a in data["acks"]])
        return httpx.Response(200, json={"success": True})

    def worker_handler(request: httpx.Request) -> httpx.Response:
        if "claim" in request.url.path:
            return httpx.Response(200, json=_claim_payload("job_up_fenced", "ord_up_fenced"))
        if "heartbeat" in request.url.path:
            return httpx.Response(
                200, json={"success": True, "lease_expires_at": int(time.time() * 1000) + 300000}
            )
        if "artifact" in request.url.path:
            # Lease fenced during artifact upload.
            return httpx.Response(409, json={"error": "Lease expired or fenced", "queue_action": "ack"})
        if "complete" in request.url.path:
            completed.append("completed")
            return httpx.Response(200, json={"success": True, "status": "COMPLETED"})
        return httpx.Response(404)

    def source_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=preview_bytes, headers={"content-type": "image/png"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(queue_handler)) as q_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(worker_handler)) as w_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(source_handler)) as s_http:
        q_client = CloudflareQueueClient(test_settings, client=q_http)
        w_client = WorkerJobClient(test_settings, client=w_http)
        runner = A23Runner(
            test_settings,
            q_client,
            w_client,
            source_acquirer=FixtureSourceAcquirer(preview_bytes, client=s_http),
        )
        msg = QueueMessage(
            id="m1",
            lease_id="l_up_fenced",
            body_raw='{"job_id":"job_up_fenced"}',
            attempts=1,
            job_id="job_up_fenced",
        )
        res = await runner.process_message(msg, preview_input=preview_bytes)
        await runner.close()

    assert res.action == RunnerAction.FENCED_ABORT
    assert completed == []
    assert "l_up_fenced" not in acked_leases


@pytest.mark.asyncio
async def test_runner_complete_fenced_aborts_without_ack(test_settings: Settings):
    preview_bytes = _make_test_image_bytes(20, 60)
    acked_leases: list[str] = []

    def queue_handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        if "acks" in data:
            acked_leases.extend([a["lease_id"] for a in data["acks"]])
        return httpx.Response(200, json={"success": True})

    def worker_handler(request: httpx.Request) -> httpx.Response:
        if "claim" in request.url.path:
            return httpx.Response(200, json=_claim_payload("job_cmp_fenced", "ord_cmp_fenced"))
        if "heartbeat" in request.url.path:
            return httpx.Response(
                200, json={"success": True, "lease_expires_at": int(time.time() * 1000) + 300000}
            )
        if "artifact" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "artifact_key": "artifacts/ord/job/sha.zip",
                    "sha256": request.headers["X-Artifact-SHA256"],
                    "size": len(request.content),
                },
            )
        if "complete" in request.url.path:
            return httpx.Response(
                409, json={"status": "EXPIRED_OR_FENCED", "queue_action": "ack", "reason": "lease_fenced"}
            )
        return httpx.Response(404)

    def source_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=preview_bytes, headers={"content-type": "image/png"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(queue_handler)) as q_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(worker_handler)) as w_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(source_handler)) as s_http:
        q_client = CloudflareQueueClient(test_settings, client=q_http)
        w_client = WorkerJobClient(test_settings, client=w_http)
        runner = A23Runner(
            test_settings,
            q_client,
            w_client,
            source_acquirer=FixtureSourceAcquirer(preview_bytes, client=s_http),
        )
        msg = QueueMessage(
            id="m1",
            lease_id="l_cmp_fenced",
            body_raw='{"job_id":"job_cmp_fenced"}',
            attempts=1,
            job_id="job_cmp_fenced",
        )
        res = await runner.process_message(msg, preview_input=preview_bytes)
        await runner.close()

    assert res.action == RunnerAction.FENCED_ABORT
    assert "l_cmp_fenced" not in acked_leases


# ---------------------------------------------------------------------------
# 4. Readiness report surfaces the archive-mode truth.
# ---------------------------------------------------------------------------


def test_readiness_reports_no_local_archive_mode(test_settings: Settings, tmp_path: Path):
    from readiness import run_a23_preflight

    settings = test_settings.model_copy(update={"FONT_ARCHIVE_MODE": ARCHIVE_MODE_NO_LOCAL_ARCHIVE})
    report = run_a23_preflight(settings=settings, test_db_dir=tmp_path / "db")
    mode_checks = [c for c in report.checks if c.name == "Archive Mode Identity (D21)"]
    assert len(mode_checks) == 1
    check = mode_checks[0]
    assert check.passed is True
    assert "no_local_archive_v1" in check.message
    assert "repeat orders recompute" in check.message
    assert check.details["mode"] == ARCHIVE_MODE_NO_LOCAL_ARCHIVE
    assert check.details["archive_enabled"] is False


def test_readiness_reports_external_ext4_mode(test_settings: Settings, tmp_path: Path):
    from readiness import run_a23_preflight

    settings = test_settings.model_copy(
        update={
            "FONT_ARCHIVE_MODE": ARCHIVE_MODE_EXTERNAL_EXT4,
            "FONT_ARCHIVE_ROOT": tmp_path / "archive_root",
        }
    )
    report = run_a23_preflight(settings=settings, test_db_dir=tmp_path / "db")
    mode_checks = [c for c in report.checks if c.name == "Archive Mode Identity (D21)"]
    assert len(mode_checks) == 1
    check = mode_checks[0]
    assert check.passed is True
    assert "external_ext4_archive_v1" in check.message
    assert check.details["archive_enabled"] is True


# ---------------------------------------------------------------------------
# 5. D12 Debian supervisor adapter: explicit mode selection.
# ---------------------------------------------------------------------------


def _supervisor_text() -> str:
    return SUPERVISOR.read_text(encoding="utf-8")


def _bash_command() -> str | None:
    import os
    import shutil

    if os.name == "nt":
        for candidate in (
            Path(r"C:\Program Files\Git\bin\bash.exe"),
            Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
        ):
            if candidate.exists():
                return str(candidate)
    return shutil.which("bash")


def test_supervisor_mode_selection_is_explicit_and_versioned():
    source = _supervisor_text()
    assert 'archive_mode="$(printenv FONT_ARCHIVE_MODE 2>/dev/null || true)"' in source
    assert "case \"$archive_mode\" in" in source
    assert "NO_LOCAL_ARCHIVE) archive_mode=\"NO_LOCAL_ARCHIVE\" ;;" in source
    assert "EXTERNAL_EXT4) archive_mode=\"EXTERNAL_EXT4\" ;;" in source
    assert "unsupported FONT_ARCHIVE_MODE" in source
    assert 'log "started release=$RELEASE_SHA archive_mode=$archive_mode"' in source


def test_supervisor_ext4_identity_checks_only_gate_external_mode():
    source = _supervisor_text()
    case_index = source.index("case \"$archive_mode\" in")
    gate_index = source.index('if [ "$archive_mode" = "EXTERNAL_EXT4" ]; then')
    mountinfo_index = source.index("/proc/self/mountinfo")
    bridge_index = source.index("canonical archive is not the accepted external archive filesystem")
    skip_log_index = source.index("archive_mode=NO_LOCAL_ARCHIVE: canonical external ext4")
    assert case_index < gate_index < mountinfo_index < bridge_index < skip_log_index
    # The exact external identity checks remain present and unchanged.
    assert '/usr/bin/stat -c %d "$ARCHIVE_ROOT"' in source
    assert 'canonical archive resolves to the Debian root device' in source
    assert 'HOST_ARCHIVE_BRIDGE="/data/data/com.termux/files/home/telefont-archive-bridge"' in source


def test_supervisor_no_local_archive_launch_never_propagates_archive_root():
    source = _supervisor_text()
    run_worker = source[source.index("run_worker() {"):]
    run_worker = run_worker[: run_worker.index("\n}\n") + 3]
    assert 'env -u FONT_ARCHIVE_ROOT' in run_worker
    assert 'FONT_ARCHIVE_MODE="NO_LOCAL_ARCHIVE"' in run_worker
    assert 'FONT_ARCHIVE_ROOT="$ARCHIVE_ROOT"' in run_worker  # EXTERNAL_EXT4 branch only
    assert 'FONT_ARCHIVE_MODE="EXTERNAL_EXT4"' in run_worker
    # The NO_LOCAL_ARCHIVE branch must not carry the archive root assignment.
    nla_branch = run_worker[run_worker.index('if [ "$archive_mode" = "NO_LOCAL_ARCHIVE" ]'):run_worker.index("else")]
    assert 'FONT_ARCHIVE_ROOT="$ARCHIVE_ROOT"' not in nla_branch


def test_supervisor_mode_selection_functional_probe():
    bash = _bash_command()
    if bash is None:
        pytest.skip("bash is not available on the validation host")
    import subprocess
    import tempfile

    source = _supervisor_text()
    start = source.index('archive_mode="$(printenv FONT_ARCHIVE_MODE')
    end = source.index("esac", start) + len("esac")
    case_block = source[start:end]

    def probe(value: str | None) -> subprocess.CompletedProcess[str]:
        env_setup = "" if value is None else f'export FONT_ARCHIVE_MODE="{value}"\n'
        script = (
            "set -u\n"
            "fail() { printf 'FAIL %s\\n' \"$1\" >&2; exit 1; }\n"
            f"{env_setup}"
            f"{case_block}\n"
            "printf '%s\\n' \"$archive_mode\"\n"
        )
        env = None
        if value is None:
            import os as _os

            env = {k: v for k, v in _os.environ.items() if k != "FONT_ARCHIVE_MODE"}
        return subprocess.run(
            [bash, "-c", script],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    assert probe(None).stdout.strip() == "EXTERNAL_EXT4"
    assert probe("NO_LOCAL_ARCHIVE").stdout.strip() == "NO_LOCAL_ARCHIVE"
    assert probe("EXTERNAL_EXT4").stdout.strip() == "EXTERNAL_EXT4"
    assert probe("LOOPBACK_FAKE").returncode != 0
