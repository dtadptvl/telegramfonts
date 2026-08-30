"""T-PRICE-01: fail-closed mode handling + ORIGINAL-only L3 binary reuse.

Focused causal tests (contract P3/P4/P7):
- claim payload mode is REQUIRED: absent -> MISSING_MODE, unsupported ->
  UNSUPPORTED_MODE (fail-closed, never an ORIGINAL default);
- runner mode propagation fails closed with terminal codes;
- L3 authorized-binary surfaces (pre-acquisition cache probe, acquisition
  binary win, binary-reuse consumption) are ORIGINAL-only: VIETNAMESE never
  takes the ORIGINAL-only binary shortcut;
- ORIGINAL keeps exact-binary-wins-immediately semantics (ADR-0002).
"""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.integration

from acquisition.models import AcquiredBinary, BINARY_STAGE_DUMP_DOM
from acquisition.pipeline import AcquisitionPipeline
from compute.archive import FinalFontArchive, canonical_source_identity
from compute.binary_cache import AuthorizedBinaryCache, BinaryCacheIdentity
from compute.models import ClaimStyle
from config import Settings
from queue_client import CloudflareQueueClient, QueueMessage
from runner import (
    A23Runner,
    RunnerAction,
    TERMINAL_ERROR_CODES,
    _require_job_mode,
    _safe_error_code,
)
from tests.test_issue71_adversarial import (
    ISSUE71_CONFIG,
    CountingAcquirer,
    _build_real_ttf,
    _runner_state,
    _wire_handlers,
)
from worker_client import ClaimedJob, WorkerJobClient

SOURCE_URL = "https://www.myfonts.com/collections/mode-fam"


def _valid_claim_payload(**overrides):
    payload = {
        "job_id": "job_mode_1",
        "order_id": "ord_mode_1",
        "lease_token": "12345678-1234-1234-1234-123456789abc",
        "lease_expires_at": 9999999999999,
        "source_url": SOURCE_URL,
        "family_name": "Mode Fam",
        "styles": [{"id": "regular", "display_name": "Regular"}],
        "formats": ["TTF"],
        "mode": "ORIGINAL",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Claim payload: mode REQUIRED, fail-closed, no ORIGINAL default (P4).
# ---------------------------------------------------------------------------


def test_claim_payload_missing_mode_fails_closed():
    payload = _valid_claim_payload()
    del payload["mode"]
    with pytest.raises(ValueError, match="MISSING_MODE"):
        ClaimedJob.from_dict(payload)
    with pytest.raises(ValueError, match="MISSING_MODE"):
        ClaimedJob.from_dict(_valid_claim_payload(mode=None))
    with pytest.raises(ValueError, match="MISSING_MODE"):
        ClaimedJob.from_dict(_valid_claim_payload(mode="   "))


def test_claim_payload_unsupported_mode_fails_closed():
    with pytest.raises(ValueError, match="UNSUPPORTED_MODE"):
        ClaimedJob.from_dict(_valid_claim_payload(mode="FRENCH"))
    with pytest.raises(ValueError, match="UNSUPPORTED_MODE"):
        ClaimedJob.from_dict(_valid_claim_payload(mode=123))


def test_claim_payload_supported_modes_parse_and_normalize():
    assert ClaimedJob.from_dict(_valid_claim_payload(mode="VIETNAMESE")).mode == "VIETNAMESE"
    assert ClaimedJob.from_dict(_valid_claim_payload(mode="original")).mode == "ORIGINAL"


def test_claimed_job_construction_requires_mode():
    # Frozen dataclass carries no default: absent mode cannot propagate.
    kwargs = {
        "job_id": "job_mode_1",
        "order_id": "ord_mode_1",
        "lease_token": "12345678-1234-1234-1234-123456789abc",
        "lease_expires_at": 9999999999999,
        "source_url": SOURCE_URL,
        "family_name": None,
        "foundry": None,
        "styles": [ClaimStyle(id="regular", display_name="Regular")],
        "formats": ["TTF"],
    }
    with pytest.raises(TypeError):
        ClaimedJob(**kwargs)
    assert ClaimedJob(**kwargs, mode="VIETNAMESE").mode == "VIETNAMESE"


@pytest.mark.asyncio
async def test_claim_with_missing_mode_is_never_claimed(test_settings: Settings):
    payload = _valid_claim_payload()
    del payload["mode"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = WorkerJobClient(test_settings, client=http_client)
        res = await client.claim("job_mode_1")

    assert res.status == "MALFORMED_PAYLOAD"
    assert res.queue_action == "retry"
    assert res.job is None
    assert res.reason == "malformed_claim_payload_MISSING_MODE"


# ---------------------------------------------------------------------------
# Runner: mode propagation fails closed with terminal codes (P4).
# ---------------------------------------------------------------------------


def test_runner_mode_propagation_fails_closed_with_terminal_codes():
    job = ClaimedJob.from_dict(_valid_claim_payload(mode="VIETNAMESE"))
    object.__setattr__(job, "mode", "")  # simulate corrupted/absent binding
    with pytest.raises(ValueError, match="MISSING_MODE"):
        _require_job_mode(job)
    object.__setattr__(job, "mode", "FRENCH")
    with pytest.raises(ValueError, match="UNSUPPORTED_MODE"):
        _require_job_mode(job)
    assert _require_job_mode(ClaimedJob.from_dict(_valid_claim_payload(mode="vietnamese"))) == "VIETNAMESE"
    # Fail-closed mode errors are terminal: retrying can never fix them.
    assert "MISSING_MODE" in TERMINAL_ERROR_CODES
    assert "UNSUPPORTED_MODE" in TERMINAL_ERROR_CODES
    assert _safe_error_code(ValueError("MISSING_MODE")) == "MISSING_MODE"
    assert _safe_error_code(ValueError("UNSUPPORTED_MODE")) == "UNSUPPORTED_MODE"


# ---------------------------------------------------------------------------
# L3 tiered-reuse harness (shared).
# ---------------------------------------------------------------------------


class _StaticDumpDom:
    def __init__(self, dump: str):
        self.dump = dump
        self.calls = 0

    async def dump_dom(self, url):
        self.calls += 1
        return self.dump


class _FailingDumpDom:
    def __init__(self):
        self.calls = 0

    async def dump_dom(self, url):
        self.calls += 1
        raise RuntimeError("DUMP_DOM_SABOTAGED")


def _l3_dump_identity() -> BinaryCacheIdentity:
    return BinaryCacheIdentity(
        reference_fingerprint=hashlib.sha256(
            canonical_source_identity(SOURCE_URL).encode("utf-8")
        ).hexdigest(),
        family_name="Mode Fam",
        style_id="regular",
        provenance=BINARY_STAGE_DUMP_DOM,
    )


async def _make_mode_runner(test_settings, tmp_path: Path, state, store_dir: Path, binary_cache, pipeline):
    settings = test_settings.model_copy(update={"FONT_ARCHIVE_ROOT": tmp_path / "archive_root"})
    archive = FinalFontArchive(settings.FONT_ARCHIVE_ROOT, settings.SCRATCH_DIR / "archive_idx.sqlite3")
    acquirer = CountingAcquirer(
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
        observation_store_dir=store_dir,
        observation_config=ISSUE71_CONFIG,
    )
    queue_handler, worker_handler = _wire_handlers(state)
    runner = A23Runner(
        settings,
        CloudflareQueueClient(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(queue_handler))),
        WorkerJobClient(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(worker_handler))),
        source_acquirer=acquirer,
        archive=archive,
        acquisition_pipeline=pipeline,
        binary_cache=binary_cache,
    )
    return runner, acquirer


def _mode_state(mode: str) -> dict:
    state = _runner_state(["TTF"])
    state["source_url"] = SOURCE_URL
    state["styles"] = [{"id": "regular", "display_name": "Regular"}]
    state["mode"] = mode
    return state


# ---------------------------------------------------------------------------
# Pre-acquisition L3 cache probe: ORIGINAL-only (P3/P4).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vietnamese_never_takes_l3_cache_hit_and_original_still_wins(
    test_settings: Settings, tmp_path: Path
):
    ttf = _build_real_ttf("Mode Fam", "Regular")
    binary_cache = AuthorizedBinaryCache(tmp_path / "bc", tmp_path / "bc.sqlite3")
    binary_cache.put(_l3_dump_identity(), ttf, "TTF", stage_provenance=BINARY_STAGE_DUMP_DOM)
    store_dir = tmp_path / "obs"
    store_dir.mkdir()

    # VIETNAMESE job against the seeded cache with sabotaged providers: if the
    # L3 binary shortcut were (wrongly) available, the job would complete from
    # the ORIGINAL binary. The ORIGINAL-only gate must skip the probe and the
    # job must fail closed instead.
    state_vi = _mode_state("VIETNAMESE")
    runner_vi, _ = await _make_mode_runner(
        test_settings, tmp_path, state_vi, store_dir, binary_cache,
        AcquisitionPipeline(dump_dom_transport=_FailingDumpDom(), session_provider=None, raster_provider=None),
    )
    msg_vi = QueueMessage(id="m_vi", lease_id="l_vi", body_raw='{"job_id":"job_i71"}', attempts=1, job_id="job_i71")
    res_vi = await runner_vi.process_message(msg_vi)
    assert res_vi.action != RunnerAction.ACKED
    assert len(state_vi["uploads"]) == 0
    assert len(state_vi["completes"]) == 0
    events_vi = [e["event"] for e in runner_vi.last_reuse_trace["events"]]
    assert "L3_CACHE_HIT" not in events_vi

    # ORIGINAL repeat against the same cache: exact binary still wins
    # immediately with zero provider/acquisition calls (ADR-0002 preserved).
    state_or = _mode_state("ORIGINAL")
    dump_or = _FailingDumpDom()
    runner_or, acquirer_or = await _make_mode_runner(
        test_settings, tmp_path, state_or, store_dir, binary_cache,
        AcquisitionPipeline(dump_dom_transport=dump_or, session_provider=None, raster_provider=None),
    )
    msg_or = QueueMessage(id="m_or", lease_id="l_or", body_raw='{"job_id":"job_i71"}', attempts=1, job_id="job_i71")
    res_or = await runner_or.process_message(msg_or)
    assert res_or.action == RunnerAction.ACKED
    assert len(state_or["uploads"]) == 1
    events_or = [e["event"] for e in runner_or.last_reuse_trace["events"]]
    assert "L3_CACHE_HIT" in events_or
    assert dump_or.calls == 0
    assert acquirer_or.acquire_calls == 0


# ---------------------------------------------------------------------------
# Acquisition binary win: ORIGINAL-only (P3/P4).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vietnamese_refuses_acquisition_binary_win_original_wins_and_caches(
    test_settings: Settings, tmp_path: Path
):
    ttf = _build_real_ttf("Mode Fam", "Regular")
    dump_html = '<style src="data:font/ttf;base64,' + base64.b64encode(ttf).decode() + '"></style>'
    binary_cache = AuthorizedBinaryCache(tmp_path / "bc2", tmp_path / "bc2.sqlite3")
    store_dir = tmp_path / "obs2"
    store_dir.mkdir()

    # VIETNAMESE: dump-dom serves a valid authorized binary. The ORIGINAL-only
    # gate must refuse the binary win; the job falls through to observable
    # source acquisition and fails closed there (404 mock), never completing
    # from the binary and never poisoning the shared L3 cache.
    state_vi = _mode_state("VIETNAMESE")
    runner_vi, acquirer_vi = await _make_mode_runner(
        test_settings, tmp_path, state_vi, store_dir, binary_cache,
        AcquisitionPipeline(dump_dom_transport=_StaticDumpDom(dump_html), session_provider=None, raster_provider=None),
    )
    msg_vi = QueueMessage(id="m_vi2", lease_id="l_vi2", body_raw='{"job_id":"job_i71"}', attempts=1, job_id="job_i71")
    res_vi = await runner_vi.process_message(msg_vi)
    assert res_vi.action != RunnerAction.ACKED
    assert len(state_vi["uploads"]) == 0
    assert len(state_vi["completes"]) == 0
    events_vi = {e["event"] for e in runner_vi.last_reuse_trace["events"]}
    assert "BINARY_WIN_REFUSED_MODE" in events_vi
    assert "BINARY_WIN" not in events_vi
    assert acquirer_vi.acquire_calls == 1  # fell through to observable acquisition
    assert binary_cache.get(_l3_dump_identity())[3] == "MISS"

    # ORIGINAL: same dump -> binary wins immediately, is cached, and delivers.
    state_or = _mode_state("ORIGINAL")
    runner_or, _ = await _make_mode_runner(
        test_settings, tmp_path, state_or, store_dir, binary_cache,
        AcquisitionPipeline(dump_dom_transport=_StaticDumpDom(dump_html), session_provider=None, raster_provider=None),
    )
    msg_or = QueueMessage(id="m_or2", lease_id="l_or2", body_raw='{"job_id":"job_i71"}', attempts=1, job_id="job_i71")
    res_or = await runner_or.process_message(msg_or)
    assert res_or.action == RunnerAction.ACKED
    assert len(state_or["uploads"]) == 1
    events_or = {e["event"] for e in runner_or.last_reuse_trace["events"]}
    assert "BINARY_WIN" in events_or
    assert binary_cache.get(_l3_dump_identity())[3] == "HIT"


# ---------------------------------------------------------------------------
# Tiered-reuse L3 consumption: ORIGINAL-only (P3/P4).
# ---------------------------------------------------------------------------


def test_tiered_resolve_refuses_binary_reuse_for_vietnamese_only(test_settings: Settings, tmp_path: Path):
    ttf = _build_real_ttf("Mode Fam", "Regular")
    binary = AcquiredBinary(
        raw_bytes=ttf,
        format="TTF",
        family_name="Mode Fam",
        style_name="Regular",
        provenance=BINARY_STAGE_DUMP_DOM,
    )
    runner = A23Runner(test_settings, queue_client=object(), worker_client=object())
    style = ClaimStyle(id="regular", display_name="Regular")
    reuse_state = {"gated": True, "binaries": {"regular": binary}}

    # VIETNAMESE: the binary shortcut is refused; with no observations the run
    # fails closed instead of delivering the ORIGINAL binary.
    vi_job = ClaimedJob.from_dict(_valid_claim_payload(mode="VIETNAMESE"))
    with pytest.raises(ValueError, match="STAGE9D_GATE_FAILED_TTF"):
        runner._tiered_resolve_artifact(
            job=vi_job,
            style=style,
            family_name="Mode Fam",
            archive_context=None,
            fmt="TTF",
            build_dir=tmp_path / "build_vi",
            reuse_state=reuse_state,
        )
    events_vi = {e["event"] for e in runner.last_reuse_trace["events"]}
    assert "BINARY_REUSE_REFUSED_MODE" in events_vi
    assert "BINARY_REUSE" not in events_vi

    # ORIGINAL: exact binary reuse still wins immediately.
    or_job = ClaimedJob.from_dict(_valid_claim_payload(mode="ORIGINAL"))
    font_file, attestation, provenance, ai_binding = runner._tiered_resolve_artifact(
        job=or_job,
        style=style,
        family_name="Mode Fam",
        archive_context=None,
        fmt="TTF",
        build_dir=tmp_path / "build_or",
        reuse_state=reuse_state,
    )
    assert font_file.format == "TTF"
    assert attestation is not None
    assert provenance != ""
    assert ai_binding == ""
    events_or = {e["event"] for e in runner.last_reuse_trace["events"]}
    assert "BINARY_REUSE" in events_or