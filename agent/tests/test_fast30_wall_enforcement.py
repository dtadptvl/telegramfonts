"""Focused FAST_30 wall-enforcement tests (T-FAST30-A23-FIX, F1/F2/F3/F6).

Binds the new runtime walls introduced by this task:

- F1 (runner): hard monotonic job wall (JOB_WALL_SECONDS, claim -> ACK),
  independent of the heartbeat-moved lease expiry; breach is terminal
  FAST30_FAILED with all-or-nothing semantics (no upload/complete after it).
- F2 (release_gate): preemptive hard wall at the sync Stage 9D boundary:
  execute_sync bounds the executor future with the supplied budget and
  returns non-publishable FAST30_FAILED WALL_LIMIT_EXCEEDED on overrun.
- F3 (raster_ingest): aggregate deadline around the per-codepoint x per-size
  browser measurement loop; breach raises BrowserMeasurementWallExceeded,
  which maps to terminal FAST30_FAILED upstream.
- F6 (scratch/checkpoints): durable job-scoped checkpoint placement keyed by
  sanitized job id (not lease token), identity-bound resume across re-claims,
  fail-closed on identity drift.

Hermetic fakes only: no device access, no network, no full-suite scope.
"""
import asyncio
import hashlib
import json
import threading
import time
from pathlib import Path

import httpx
import pytest

import runner as runner_module
from acquisition.raster_ingest import (
    BrowserMeasurementWallExceeded,
    collect_browser_measurement,
)
from compute.font_builder import FontBuilderService
from config import Settings
from fidelity.balanced_search import CheckpointIdentity, GlyphCheckpointStore
from fidelity.profiles import FAST_30_PROFILE
from fidelity.release_gate import ReleaseGateResult, Stage9DReleaseGate
from queue_client import CloudflareQueueClient, QueueMessage
from runner import (
    A23Runner,
    JobWallLimitExceeded,
    RunnerAction,
    _safe_error_code,
)
from scratch import ScratchManager, is_path_contained_within
from tests.test_issue71_adversarial import ISSUE71_CONFIG
from tests.test_runner import FixtureSourceAcquirer, _make_test_image_bytes
from worker_client import WorkerJobClient


def _wall_settings(test_settings: Settings, job_wall_seconds: int = 2) -> Settings:
    return test_settings.model_copy(
        update={
            "JOB_WALL_SECONDS": job_wall_seconds,
            "HEARTBEAT_INTERVAL_SECONDS": 1,
            "LEASE_DURATION_SECONDS": 60,
        }
    )


def _claim_json(job_id: str, order_id: str) -> dict:
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


def test_safe_error_code_maps_wall_breaches_to_terminal_fast30_failed():
    """F1/F3: the new wall exceptions carry the exact bounded terminal code."""
    assert _safe_error_code(JobWallLimitExceeded()) == "FAST30_FAILED"
    assert _safe_error_code(BrowserMeasurementWallExceeded()) == "FAST30_FAILED"
    # Unknown/superset text never maps onto the terminal wall code.
    assert _safe_error_code(RuntimeError("FAST30_FAILED_SUPERSET")) == "UNEXPECTED_RUNTIME_ERROR"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_job_wall_breach_fails_terminal_with_zero_upload_and_complete(test_settings: Settings):
    """F1: tiny JOB_WALL_SECONDS -> terminal FAST30_FAILED, all-or-nothing.

    With a 2 s job wall the 15 s wall-safety margin guard refuses to start
    build/package/upload work it cannot finish inside the wall: the run fails
    closed with FAST30_FAILED and zero upload/complete calls, independent of
    the heartbeat-moved lease expiry.
    """
    preview_bytes = _make_test_image_bytes(20, 60)
    acked_leases: list[str] = []
    uploaded: list[str] = []
    completed: list[str] = []
    fail_payloads: list[dict] = []

    def queue_handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        if "acks" in data:
            acked_leases.extend([a["lease_id"] for a in data["acks"]])
        return httpx.Response(200, json={"success": True})

    def worker_handler(request: httpx.Request) -> httpx.Response:
        if "claim" in request.url.path:
            return httpx.Response(200, json=_claim_json("job_wall_tiny", "ord_wall_tiny"))
        if "heartbeat" in request.url.path:
            return httpx.Response(
                200,
                json={"success": True, "lease_expires_at": int(time.time() * 1000) + 300000},
            )
        if "artifact" in request.url.path:
            uploaded.append(request.headers["X-Artifact-SHA256"])
            return httpx.Response(
                200, json={"success": True, "artifact_key": "k", "sha256": "s", "size": 1}
            )
        if "complete" in request.url.path:
            completed.append("job_wall_tiny")
            return httpx.Response(
                200,
                json={"success": True, "status": "COMPLETED", "queue_action": "ack", "completed_at": 1},
            )
        if "fail" in request.url.path:
            fail_payloads.append(json.loads(request.content))
            return httpx.Response(200, json={"success": True, "status": "FAILED", "queue_action": "ack"})
        return httpx.Response(404)

    class SlowBuilder(FontBuilderService):
        def __init__(self):
            super().__init__()
            self.build_calls = 0

        def build_font(self, style_source, family_name, format_type, output_dir):
            self.build_calls += 1
            time.sleep(5.0)
            return super().build_font(style_source, family_name, format_type, output_dir)

    builder = SlowBuilder()
    settings = _wall_settings(test_settings, job_wall_seconds=2)
    source_404 = httpx.MockTransport(lambda r: httpx.Response(404))

    sync_http = httpx.Client(transport=httpx.MockTransport(worker_handler))
    async with httpx.AsyncClient(transport=httpx.MockTransport(queue_handler)) as q_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(worker_handler)) as w_http, \
               httpx.AsyncClient(transport=source_404) as s_http:
        q_client = CloudflareQueueClient(settings, client=q_http)
        w_client = WorkerJobClient(settings, client=w_http, sync_client=sync_http)
        runner = A23Runner(
            settings,
            q_client,
            w_client,
            source_acquirer=FixtureSourceAcquirer(preview_bytes, client=s_http),
            font_builder=builder,
        )

        msg = QueueMessage(
            id="m_wall", lease_id="l_wall", body_raw='{"job_id":"job_wall_tiny"}',
            attempts=1, job_id="job_wall_tiny",
        )
        t0 = time.monotonic()
        res = await runner.process_message(msg, preview_input=preview_bytes)
        elapsed = time.monotonic() - t0

    # Terminal wall failure, well below any lease lifetime.
    assert res.action == RunnerAction.FAILED_TERMINAL
    assert res.reason == "FAST30_FAILED"
    assert elapsed < 10.0

    # All-or-nothing: nothing was uploaded or completed after the breach.
    assert uploaded == []
    assert completed == []
    # The wall guard refused to start work it could not finish in the wall.
    assert builder.build_calls == 0

    # Exactly one terminal fail with the bounded code (not retryable).
    assert len(fail_payloads) == 1
    assert fail_payloads[0]["reason_code"] == "FAST30_FAILED"
    assert fail_payloads[0]["retryable"] is False
    # Queue message ACKed only after the durable terminal fail.
    assert "l_wall" in acked_leases


@pytest.mark.integration
@pytest.mark.asyncio
async def test_job_wall_timeout_fires_while_build_running(test_settings: Settings, monkeypatch):
    """F1: the monotonic wall fires even while compute is mid-build.

    With the wall-safety margin neutralized the slow build actually starts;
    the job-level asyncio timeout born at claim still terminates the run at
    ~JOB_WALL_SECONDS while heartbeats keep extending the lease - proving the
    wall is independent of the heartbeat-moved expiry holder (RC-3).
    """
    monkeypatch.setattr(runner_module, "WALL_SAFETY_MARGIN_MS", 0.0)

    preview_bytes = _make_test_image_bytes(20, 60)
    acked_leases: list[str] = []
    uploaded: list[str] = []
    completed: list[str] = []
    fail_payloads: list[dict] = []
    heartbeat_count = {"n": 0}

    def queue_handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        if "acks" in data:
            acked_leases.extend([a["lease_id"] for a in data["acks"]])
        return httpx.Response(200, json={"success": True})

    def worker_handler(request: httpx.Request) -> httpx.Response:
        if "claim" in request.url.path:
            return httpx.Response(200, json=_claim_json("job_wall_midbuild", "ord_wall_midbuild"))
        if "heartbeat" in request.url.path:
            heartbeat_count["n"] += 1
            return httpx.Response(
                200,
                json={"success": True, "lease_expires_at": int(time.time() * 1000) + 300000},
            )
        if "artifact" in request.url.path:
            uploaded.append(request.headers["X-Artifact-SHA256"])
            return httpx.Response(
                200, json={"success": True, "artifact_key": "k", "sha256": "s", "size": 1}
            )
        if "complete" in request.url.path:
            completed.append("job_wall_midbuild")
            return httpx.Response(
                200,
                json={"success": True, "status": "COMPLETED", "queue_action": "ack", "completed_at": 1},
            )
        if "fail" in request.url.path:
            fail_payloads.append(json.loads(request.content))
            return httpx.Response(200, json={"success": True, "status": "FAILED", "queue_action": "ack"})
        return httpx.Response(404)

    class SlowBuilder(FontBuilderService):
        def build_font(self, style_source, family_name, format_type, output_dir):
            time.sleep(6.0)
            return super().build_font(style_source, family_name, format_type, output_dir)

    settings = _wall_settings(test_settings, job_wall_seconds=2)
    source_404 = httpx.MockTransport(lambda r: httpx.Response(404))

    sync_http = httpx.Client(transport=httpx.MockTransport(worker_handler))
    async with httpx.AsyncClient(transport=httpx.MockTransport(queue_handler)) as q_http, \
               httpx.AsyncClient(transport=httpx.MockTransport(worker_handler)) as w_http, \
               httpx.AsyncClient(transport=source_404) as s_http:
        q_client = CloudflareQueueClient(settings, client=q_http)
        w_client = WorkerJobClient(settings, client=w_http, sync_client=sync_http)
        runner = A23Runner(
            settings,
            q_client,
            w_client,
            source_acquirer=FixtureSourceAcquirer(preview_bytes, client=s_http),
            font_builder=SlowBuilder(),
        )

        msg = QueueMessage(
            id="m_wall2", lease_id="l_wall2", body_raw='{"job_id":"job_wall_midbuild"}',
            attempts=1, job_id="job_wall_midbuild",
        )
        t0 = time.monotonic()
        res = await runner.process_message(msg, preview_input=preview_bytes)
        elapsed = time.monotonic() - t0

    assert res.action == RunnerAction.FAILED_TERMINAL
    assert res.reason == "FAST30_FAILED"
    # The wall fired at ~2 s: after the build started, long before it ended.
    assert 1.5 <= elapsed < 6.0

    # All-or-nothing even mid-build: no upload/complete after the breach.
    assert uploaded == []
    assert completed == []

    # The lease heartbeat kept extending the lease while the wall fired:
    # the monotonic job wall is independent of the heartbeat-moved expiry.
    assert heartbeat_count["n"] >= 1

    assert len(fail_payloads) == 1
    assert fail_payloads[0]["reason_code"] == "FAST30_FAILED"
    assert fail_payloads[0]["retryable"] is False
    assert "l_wall2" in acked_leases


def test_execute_sync_preemptive_wall_unblocks_fast_with_wall_limit_exceeded(monkeypatch):
    """F2: a slow gate is preempted at the sync boundary by the supplied budget.

    The slow gate body never finishes inside the budget; execute_sync must
    return a non-publishable FAST30_FAILED WALL_LIMIT_EXCEEDED result at the
    hard bound instead of blocking on the orphaned worker thread.
    """
    started = threading.Event()

    async def _slow_execute(cls, **kwargs):
        started.set()
        await asyncio.sleep(2.0)
        raise AssertionError("slow gate body must not complete before the preemptive wall")

    monkeypatch.setattr(Stage9DReleaseGate, "execute", classmethod(_slow_execute))

    t0 = time.monotonic()
    result = Stage9DReleaseGate.execute_sync(
        store=None,
        config=None,
        reference_id="ref_wall",
        style_id="regular",
        family_name="Wall Fam",
        style_name="Regular",
        browser_version="chromium_wall_stub_v1",
        format_type="ttf",
        wall_limit_seconds=0.5,
    )
    elapsed = time.monotonic() - t0

    assert started.is_set()
    # Unblocked at the hard bound (~0.5 s), never at the slow body (2 s).
    assert elapsed < 1.8
    assert result.is_publishable is False
    assert result.status == "FAIL"
    assert result.format == "TTF"
    assert result.failure_reasons == ("FAST30_FAILED: WALL_LIMIT_EXCEEDED",)


def test_execute_sync_without_wall_returns_gate_result_unbounded(monkeypatch):
    """F2 sanity: with no supplied budget the future result passes through."""
    sentinel = ReleaseGateResult(
        is_publishable=True,
        status="PASS",
        family_name="Wall Fam",
        style_name="Regular",
        reference_id="ref_wall",
        style_id="regular",
        format="TTF",
        model_hash="",
        candidate_file_path="",
        candidate_size_bytes=0,
        candidate_artifact_sha="",
    )

    async def _fast_execute(cls, **kwargs):
        return sentinel

    monkeypatch.setattr(Stage9DReleaseGate, "execute", classmethod(_fast_execute))

    result = Stage9DReleaseGate.execute_sync(
        store=None,
        config=None,
        reference_id="ref_wall",
        style_id="regular",
        family_name="Wall Fam",
        style_name="Regular",
        browser_version="chromium_wall_stub_v1",
        format_type="TTF",
    )
    assert result is sentinel


class _SlowMeasureSession:
    """Deterministic slow stand-in for the approved ChromiumSession canvas path."""

    created = 0
    measure_calls = 0
    measure_delay = 0.0

    def __init__(self, executable_path=None, timeout_seconds=10.0, port=0):
        _SlowMeasureSession.created += 1
        self.browser_version = "chromium_wall_stub_v1"
        self.timeout_seconds = timeout_seconds

    async def start(self):
        pass

    async def observe_source_font(self, source_url, style_name, family_name=None):
        return "Wall Fam"

    async def measure_glyph_direct(self, font, code_point, font_size_px=200.0, upem=1000):
        _SlowMeasureSession.measure_calls += 1
        if _SlowMeasureSession.measure_delay:
            await asyncio.sleep(_SlowMeasureSession.measure_delay)
        from measurement.models import DirectMetrics

        scale = float(font_size_px) / float(upem)
        return DirectMetrics(
            code_point=code_point,
            character=chr(code_point),
            font_size_px=float(font_size_px),
            raw_advance_width=600.0 * scale,
            raw_actual_left=50.0 * scale,
            raw_actual_right=550.0 * scale,
            raw_actual_ascent=700.0 * scale,
            raw_actual_descent=200.0 * scale,
            raw_font_ascent=700.0 * scale,
            raw_font_descent=200.0 * scale,
            advance_width_upem=600.0,
            lsb_upem=50.0,
            rsb_upem=50.0,
            ascent_upem=700.0,
            descent_upem=-200.0,
            bbox_width_upem=500.0,
            bbox_height_upem=900.0,
        )

    async def measure_text_advance(self, font, text, font_size_px=200.0, upem=1000):
        if _SlowMeasureSession.measure_delay:
            await asyncio.sleep(_SlowMeasureSession.measure_delay)
        return 1200.0

    async def probe_opentype_feature(self, font, feature_tag, sample_text, font_size_px=200.0, upem=1000):
        return {
            "enabled_advance_upem": 1200.0,
            "disabled_advance_upem": 1200.0,
            "enabled_raster_signature": "a",
            "disabled_raster_signature": "a",
        }


def _patch_browser_session(monkeypatch) -> dict:
    import measurement.browser_session as browser_session_mod

    closed = {"n": 0}

    async def _noop_close(session):
        closed["n"] += 1

    monkeypatch.setattr(browser_session_mod, "ChromiumSession", _SlowMeasureSession)
    monkeypatch.setattr(browser_session_mod, "close_browser_session", _noop_close)
    return closed


@pytest.mark.asyncio
async def test_browser_measurement_past_aggregate_deadline_fails_closed(monkeypatch):
    """F3: an already-breached aggregate deadline fails before any session work."""
    closed = _patch_browser_session(monkeypatch)
    _SlowMeasureSession.created = 0
    _SlowMeasureSession.measure_calls = 0
    _SlowMeasureSession.measure_delay = 0.5

    with pytest.raises(BrowserMeasurementWallExceeded):
        await collect_browser_measurement(
            "https://www.myfonts.com/collections/wall-fam",
            "Wall Fam",
            "Regular",
            [65, 66],
            ISSUE71_CONFIG,
            aggregate_deadline=time.monotonic() - 1.0,
        )

    # Fail-closed before session creation: no browser work was ever started.
    assert _SlowMeasureSession.created == 0
    assert _SlowMeasureSession.measure_calls == 0
    assert closed["n"] == 0
    # The breach maps to the terminal bounded code upstream.
    assert _safe_error_code(BrowserMeasurementWallExceeded()) == "FAST30_FAILED"


@pytest.mark.asyncio
async def test_browser_measurement_midloop_aggregate_deadline_stops_and_closes(monkeypatch):
    """F3: the aggregate deadline bounds the ENTIRE per-codepoint measurement loop."""
    closed = _patch_browser_session(monkeypatch)
    _SlowMeasureSession.created = 0
    _SlowMeasureSession.measure_calls = 0
    _SlowMeasureSession.measure_delay = 0.5

    with pytest.raises(BrowserMeasurementWallExceeded):
        await collect_browser_measurement(
            "https://www.myfonts.com/collections/wall-fam",
            "Wall Fam",
            "Regular",
            [65, 66],
            ISSUE71_CONFIG,
            aggregate_deadline=time.monotonic() + 0.8,
        )

    # The session was opened, measured at least once, and the loop was halted
    # mid-flight by the aggregate wall; the session is always closed (finally).
    assert _SlowMeasureSession.created == 1
    assert _SlowMeasureSession.measure_calls >= 1
    assert closed["n"] == 1


def test_durable_job_cache_dir_isolation_stability_and_traversal(tmp_path):
    """F6: durable cache dirs are job-scoped, re-claim-stable, traversal-closed."""
    sm_a = ScratchManager(tmp_path / "scratch")
    # A re-claim constructs a fresh manager over the same root.
    sm_reclaim = ScratchManager(tmp_path / "scratch")

    dir_a = sm_a.get_durable_job_cache_dir("job_wall_A", "glyph_checkpoints")
    dir_b = sm_a.get_durable_job_cache_dir("job_wall_B", "glyph_checkpoints")
    # Distinct jobs never share a directory.
    assert dir_a != dir_b
    assert is_path_contained_within(dir_a, sm_a.root)
    assert is_path_contained_within(dir_b, sm_a.root)

    # A re-claimed attempt of the same job resolves the SAME durable dir,
    # so identity-bound persisted state resumes instead of restarting.
    assert sm_reclaim.get_durable_job_cache_dir("job_wall_A", "glyph_checkpoints") == dir_a

    # Distinct namespaces are isolated trees.
    assert sm_a.get_durable_job_cache_dir("job_wall_A", "other_ns") != dir_a

    # Traversal fails closed: identifiers empty after sanitization are rejected.
    with pytest.raises(ValueError):
        sm_a.get_durable_job_cache_dir("..", "glyph_checkpoints")
    with pytest.raises(ValueError):
        sm_a.get_durable_job_cache_dir("job_wall_A", "..")

    # Sanitized traversal attempts never escape the scratch root.
    sneaky = sm_a.get_durable_job_cache_dir("../../evil", "glyph_checkpoints")
    assert is_path_contained_within(sneaky, sm_a.root)

    # The durable dir survives lease-bound job-dir cleanup (resume > restart):
    # cleanup removes only the lease-token-bound job dir, never the cache tree.
    job_dir = sm_a.get_job_dir("job_wall_A", "12345678-1234-1234-1234-123456789abc")
    assert job_dir != dir_a
    sm_a.cleanup_job_dir(job_dir)
    assert not job_dir.exists()
    assert dir_a.exists()


def test_glyph_checkpoint_store_resume_across_instances_and_identity_drift(tmp_path):
    """F6: identity-bound checkpoints resume across re-claims; drift fails closed."""
    sm = ScratchManager(tmp_path / "scratch")
    checkpoint_root = sm.get_durable_job_cache_dir("job_resume_1", "glyph_checkpoints")
    snapshot_fingerprint = hashlib.sha256(b"snapshot-evidence").hexdigest()
    # Mirrors the gate placement: durable root / snapshot_fingerprint[:16].
    store_dir = Path(checkpoint_root) / snapshot_fingerprint[:16]

    identity = CheckpointIdentity(
        snapshot_fingerprint=snapshot_fingerprint,
        fit_evidence_fingerprint=hashlib.sha256(b"fit-evidence").hexdigest(),
        profile_hash=FAST_30_PROFILE.policy_hash(),
        search_version="fast30-search-v1",
    )
    payload = {"advance_width_upem": 612.5, "tiers": ["coarse", "fine"]}

    # Attempt 1 writes the checkpoint under the durable placement.
    store_attempt_1 = GlyphCheckpointStore(store_dir, FAST_30_PROFILE)
    store_attempt_1.save(65, "FULL", identity, payload)
    assert store_attempt_1.stats["writes"] == 1

    # Attempt 2 (re-claim): a fresh store instance over the SAME durable root
    # resumes the identical checkpoint (hit, exact payload).
    store_attempt_2 = GlyphCheckpointStore(store_dir, FAST_30_PROFILE)
    assert store_attempt_2.load(65, "FULL", identity) == payload
    assert store_attempt_2.stats["hits"] == 1

    # Identity drift fails closed: mismatched identity discards, never reuses.
    drifted = CheckpointIdentity(
        snapshot_fingerprint=hashlib.sha256(b"snapshot-evidence-drifted").hexdigest(),
        fit_evidence_fingerprint=identity.fit_evidence_fingerprint,
        profile_hash=identity.profile_hash,
        search_version=identity.search_version,
    )
    assert store_attempt_2.load(65, "FULL", drifted) is None
    assert store_attempt_2.stats["invalid_discarded"] == 1

    # The drifted entry was deleted on disk: even the original identity now
    # misses (fail-closed recompute, never stale reuse).
    assert store_attempt_2.load(65, "FULL", identity) is None
