"""Issue #71 review 5018836759 KNOWN_REPRO pack.

PROD_COMPOSITION, RASTER_HANDOFF, L3_REPEAT, CHROMIUM_FORGE, OPENROUTER_ROUTE.
"""
from __future__ import annotations

import base64
import hashlib
import inspect
import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from acquisition.adapters import (
    AuthorizedSessionHttpTransport,
    AuthorizedSessionMaterialStore,
    HeadlessDumpDomTransport,
    HttpBinaryFetcher,
    MonotypeRasterHttpClient,
)
from acquisition.models import BinaryAcquisitionPolicy, SpriteRasterPage
from acquisition.pipeline import AcquisitionPipeline
from acquisition.providers import MonotypeRasterProvider, PersistentSessionBinaryProvider
from compute.binary_cache import AuthorizedBinaryCache
from compute.binary_gate import BinaryConsumerValidator
from compute.model_cache import CanonicalFontModelCache
from compute.openrouter_client import (
    MODEL_ARBITER,
    MODEL_DIFFICULT,
    MODEL_PRIMARY,
    OpenRouterAIClient,
)
from compute.source import SourceAcquirer
from composition import build_production_components
from config import Settings
from queue_client import CloudflareQueueClient, QueueMessage
from runner import A23Runner, RunnerAction
from tests.test_issue71_adversarial import (
    ISSUE71_CONFIG,
    CountingAcquirer,
    _build_real_ttf,
    _generate_png_bytes,
    _runner_state,
    _wire_handlers,
)
from worker_client import WorkerJobClient


# =========================================================================
# PROD_COMPOSITION
# =========================================================================

def test_PROD_COMPOSITION_real_factory_concrete_dependencies(tmp_path: Path, test_settings: Settings, monkeypatch):
    settings = test_settings.model_copy(update={"ACQUISITION_ENABLED": True})
    components = build_production_components(settings, tmp_path / "scratch")

    pipeline = components["acquisition_pipeline"]
    assert isinstance(pipeline, AcquisitionPipeline)
    assert isinstance(pipeline.dump_dom_transport, HeadlessDumpDomTransport)
    assert isinstance(pipeline.binary_fetch.__self__, HttpBinaryFetcher)
    assert isinstance(components["model_cache"], CanonicalFontModelCache)
    assert isinstance(components["binary_cache"], AuthorizedBinaryCache)
    # Session/raster stages absent without runtime secrets: concrete types only
    # when constructible; nothing test-only is produced.
    assert components["vietnamese_ai_provider"] is None

    # Enabled Vietnamese AI without runtime key fails closed.
    vi_settings = test_settings.model_copy(update={"VIETNAMESE_AI_ENABLED": True})
    with pytest.raises(RuntimeError, match="COMPOSITION_READINESS_FAILED_OPENROUTER"):
        build_production_components(vi_settings, tmp_path / "scratch2")

    # Enabled acquisition without constructible Chromium fails closed.
    def _no_chromium():
        raise RuntimeError("CHROMIUM_EXECUTABLE_NOT_FOUND")

    import acquisition.adapters as adapters_mod

    monkeypatch.setattr(adapters_mod, "find_chromium_executable", _no_chromium)
    with pytest.raises(RuntimeError, match="COMPOSITION_READINESS_FAILED"):
        build_production_components(settings, tmp_path / "scratch3")


# =========================================================================
# RASTER_HANDOFF
# =========================================================================

def _raster_pages_for_seed(browser_version: str) -> list[SpriteRasterPage]:
    """Complete authorized raster page set matching ISSUE71_CONFIG schedules."""
    glyphs = []
    for cp, adv, bbox in ((65, 650.0, (50, 50, 550, 700)), (66, 600.0, (40, 50, 560, 700))):
        for res in ISSUE71_CONFIG.resolutions:
            for sx, sy in ((0.0, 0.0),):
                glyphs.append(_glyph_entry(cp, adv, bbox, res, sx, sy))
        eval_res = max(ISSUE71_CONFIG.resolutions)
        for sx, sy in ISSUE71_CONFIG.held_out_subpixel_phases:
            glyphs.append(_glyph_entry(cp, adv, bbox, eval_res, sx, sy))
    payload = {
        "browser_version": browser_version,
        "glyphs": glyphs,
        "pairs": [
            {"left_cp": 65, "right_cp": 66, "left_advance_upem": 650.0, "right_advance_upem": 600.0, "pair_advance_upem": 1230.0},
            {"left_cp": 66, "right_cp": 65, "left_advance_upem": 600.0, "right_advance_upem": 650.0, "pair_advance_upem": 1240.0},
        ],
        "features": [
            {
                "feature_tag": tag,
                "sample_text": text,
                "enabled_advance_upem": 1200.0,
                "disabled_advance_upem": 1200.0,
                "enabled_raster_signature": "a",
                "disabled_raster_signature": "a",
            }
            for tag, text in ISSUE71_CONFIG.feature_probes
        ],
    }
    return [SpriteRasterPage(page_index=0, glyph_count=len(glyphs), raster_bytes=b"", final=True, payload=payload)]


def _glyph_entry(cp: int, adv: float, bbox, res: int, sx: float, sy: float) -> dict:
    import math

    font_size = math.floor(res * 0.72)
    scale = font_size / 1000.0
    png = _generate_png_bytes(res, bbox, adv, sx, sy)
    return {
        "code_point": cp,
        "resolution": res,
        "subpixel_x": sx,
        "subpixel_y": sy,
        "png_base64": base64.b64encode(png).decode(),
        "metrics": {
            "advance_width_px": round(adv * scale, 2),
            "lsb_px": round(bbox[0] * scale, 2),
            "rsb_px": round((adv - bbox[2]) * scale, 2),
            "ascent_px": round(bbox[3] * scale, 2),
            "descent_px": round(-bbox[1] * scale, 2),
            "advance_width_upem": adv,
            "lsb_upem": float(bbox[0]),
            "rsb_upem": adv - float(bbox[2]),
            "ascent_upem": float(bbox[3]),
            "descent_upem": -200.0,
            "bbox_width_upem": float(bbox[2] - bbox[0]),
            "bbox_height_upem": float(bbox[3] - bbox[1]),
            "sample_count": 1,
            "confidence": 1.0,
        },
    }


class _RasterOnlyClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = 0

    async def fetch_sprite_page(self, request, cursor):
        self.calls += 1
        if cursor:
            return None
        return self.pages[0]


class _FailingDumpDom:
    def __init__(self):
        self.calls = 0

    async def dump_dom(self, url):
        self.calls += 1
        raise RuntimeError("DUMP_DOM_UNAVAILABLE")


@pytest.mark.asyncio
async def test_RASTER_HANDOFF_provider_pages_reach_stage9d_not_legacy_acquirer(
    test_settings: Settings, tmp_path: Path
):
    store_dir = tmp_path / "obs"
    store_dir.mkdir()
    browser_version = "monotype_authorized_v1"
    pages = _raster_pages_for_seed(browser_version)

    class _SabotagedAcquirer(CountingAcquirer):
        async def acquire_source(self, *args, **kwargs):
            self.acquire_calls += 1
            raise AssertionError("legacy acquirer must not run after raster handoff")

    settings = test_settings.model_copy(update={"FONT_ARCHIVE_ROOT": tmp_path / "archive_root"})
    archive_root = settings.FONT_ARCHIVE_ROOT
    from compute.archive import FinalFontArchive

    archive = FinalFontArchive(archive_root, settings.SCRATCH_DIR / "archive_idx.sqlite3")
    pipeline = AcquisitionPipeline(
        dump_dom_transport=_FailingDumpDom(),
        binary_fetch=None,
        session_provider=None,
        raster_provider=MonotypeRasterProvider(_RasterOnlyClient(pages)),
    )
    acquirer = _SabotagedAcquirer(
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
        observation_store_dir=store_dir,
        observation_config=ISSUE71_CONFIG,
    )

    state = _runner_state(["TTF"])
    state["source_url"] = "https://www.myfonts.com/collections/raster-handoff-fam"
    state["styles"] = [{"id": "regular", "display_name": "Regular"}]
    queue_handler, worker_handler = _wire_handlers(state)
    runner = A23Runner(
        settings,
        CloudflareQueueClient(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(queue_handler))),
        WorkerJobClient(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(worker_handler))),
        source_acquirer=acquirer,
        archive=archive,
        acquisition_pipeline=pipeline,
        model_cache=CanonicalFontModelCache(tmp_path / "mc", tmp_path / "mc.sqlite3"),
    )
    msg = QueueMessage(id="m_rh", lease_id="lease_rh", body_raw='{"job_id":"job_i71"}', attempts=1, job_id="job_i71")

    res = await runner.process_message(msg)
    assert res.action == RunnerAction.ACKED
    assert acquirer.acquire_calls == 0  # legacy acquirer never invoked
    assert len(state["uploads"]) == 1
    events = [e for e in runner.last_reuse_trace["events"] if e["event"] == "RASTER_HANDOFF"]
    assert events and events[0]["glyphs"] == 2
    # Observations + completed collection now exist under the exact tuple.
    cov = acquirer.store.get_coverage(
        "raster_handoff_fam", "regular", browser_version=browser_version,
        config_hash=ISSUE71_CONFIG.compute_hash(),
    )
    assert cov == [65, 66]


# =========================================================================
# L3_REPEAT
# =========================================================================

class _StaticDumpDom:
    def __init__(self, dump: str):
        self.dump = dump
        self.calls = 0

    async def dump_dom(self, url):
        self.calls += 1
        return self.dump


async def _make_l3_runner(test_settings, tmp_path, state, store_dir, binary_cache, pipeline):
    settings = test_settings.model_copy(update={"FONT_ARCHIVE_ROOT": tmp_path / "archive_root"})
    from compute.archive import FinalFontArchive

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


@pytest.mark.asyncio
async def test_L3_REPEAT_durable_binary_cache_zero_provider_calls_on_repeat(
    test_settings: Settings, tmp_path: Path
):
    ttf = _build_real_ttf("Cache Fam", "Regular")
    dump = '<style src="data:font/ttf;base64,' + base64.b64encode(ttf).decode() + '"></style>'
    binary_cache = AuthorizedBinaryCache(tmp_path / "bc", tmp_path / "bc.sqlite3")
    store_dir = tmp_path / "obs"
    store_dir.mkdir()

    # First job: provider supplies the binary (dump-dom win).
    state1 = _runner_state(["TTF"])
    state1["source_url"] = "https://www.myfonts.com/collections/cache-fam"
    state1["styles"] = [{"id": "regular", "display_name": "Regular"}]
    dump1 = _StaticDumpDom(dump)
    runner1, acquirer1 = await _make_l3_runner(
        test_settings, tmp_path, state1, store_dir, binary_cache,
        AcquisitionPipeline(dump_dom_transport=dump1, session_provider=None, raster_provider=None),
    )
    msg1 = QueueMessage(id="m1", lease_id="l1", body_raw='{"job_id":"job_i71"}', attempts=1, job_id="job_i71")
    res1 = await runner1.process_message(msg1)
    assert res1.action == RunnerAction.ACKED
    assert dump1.calls == 1

    # Second job (compatible new format/order): providers sabotaged; durable L3
    # cache must serve the binary with zero provider/network/reconstruction calls.
    state2 = _runner_state(["OTF"])
    state2["source_url"] = "https://www.myfonts.com/collections/cache-fam"
    state2["styles"] = [{"id": "regular", "display_name": "Regular"}]
    dump2 = _FailingDumpDom()
    runner2, acquirer2 = await _make_l3_runner(
        test_settings, tmp_path, state2, store_dir, binary_cache,
        AcquisitionPipeline(dump_dom_transport=dump2, session_provider=None, raster_provider=None),
    )
    msg2 = QueueMessage(id="m2", lease_id="l2", body_raw='{"job_id":"job_i71"}', attempts=1, job_id="job_i71")
    res2 = await runner2.process_message(msg2)
    assert res2.action == RunnerAction.ACKED
    assert dump2.calls == 0  # zero provider calls
    assert acquirer2.acquire_calls == 0  # zero acquisition
    events = [e for e in runner2.last_reuse_trace["events"] if e["event"] == "L3_CACHE_HIT"]
    assert events
    assert len(state2["uploads"]) == 1


# =========================================================================
# CHROMIUM_FORGE
# =========================================================================

def test_CHROMIUM_FORGE_no_injectable_pass_and_closed_failure(tmp_path: Path, monkeypatch):
    # Production API exposes no injectable Chromium boolean.
    init_sig = inspect.signature(BinaryConsumerValidator.__init__)
    assert all(p not in init_sig.parameters for p in ("chromium_load_check", "chromium_binary_checker"))
    validate_sig = inspect.signature(BinaryConsumerValidator.validate)
    assert all(p not in validate_sig.parameters for p in ("chromium_load_check", "checker"))

    ttf = _build_real_ttf("Forge Fam", "Regular")
    f = tmp_path / "forge.ttf"
    f.write_bytes(ttf)
    from compute.models import GeneratedFontFile

    ff = GeneratedFontFile(
        style_id="regular", style_name="Regular", format="TTF", filename="forge.ttf",
        file_path=f, size_bytes=len(ttf), sha256_hex=hashlib.sha256(ttf).hexdigest(),
    )

    # Capability absence fails closed (BLOCKED, never PASS).
    def _no_chromium():
        raise RuntimeError("CHROMIUM_EXECUTABLE_NOT_FOUND")

    import fidelity.producers as producers_mod

    monkeypatch.setattr(producers_mod, "find_chromium_executable", _no_chromium)
    report = BinaryConsumerValidator().validate(ff, provenance="p")
    assert report.overall_status == "BLOCKED"
    assert "CHROMIUM_CAPABILITY_UNAVAILABLE" in report.failure_reasons

    # Forged/drifted artifact bytes fail closed (FAIL, never PASS).
    f.write_bytes(ttf + b"TAMPER")
    report2 = BinaryConsumerValidator().validate(ff, provenance="p")
    assert report2.overall_status == "FAIL"
    assert "BINARY_ARTIFACT_DRIFT" in report2.failure_reasons


# =========================================================================
# OPENROUTER_ROUTE
# =========================================================================

def _valid_candidate_payload(missing: list[int], variant: int = 0) -> str:
    glyphs = []
    for cp in missing:
        offset = float(variant)
        glyphs.append({
            "code_point": cp,
            "contours": [[[50.0 + offset, 50.0], [550.0 + offset, 50.0], [550.0 + offset, 700.0], [50.0 + offset, 700.0]]],
            "advance_width_upem": 600.0,
            "lsb_upem": 50.0,
            "rsb_upem": 50.0,
            "ascent_upem": 700.0,
            "descent_upem": -200.0,
            "anchors": [["top", 300.0, 700.0]] if cp in (0x0300, 0x0301, 0x0303, 0x0309, 0x0323) else [],
        })
    return json.dumps({"glyphs": glyphs})


def _openrouter_transport(handler):
    return httpx.MockTransport(handler)


def _make_client(missing: list[int], primary_body, difficult_body=None, arbiter_body=None):
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append({"model": body["model"]})
        model = body["model"]
        if model == MODEL_PRIMARY:
            content = primary_body(missing) if callable(primary_body) else primary_body
        elif model == MODEL_DIFFICULT:
            content = difficult_body(missing) if callable(difficult_body) else difficult_body
        else:
            content = arbiter_body if arbiter_body is not None else '{"choice":"A"}'
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}}]}
        )

    client = OpenRouterAIClient("test-key-runtime-secret", client=httpx.AsyncClient(transport=_openrouter_transport(handler)))
    return client, calls


@pytest.mark.asyncio
async def test_OPENROUTER_ROUTE_fixed_routing_and_zero_call_paths():
    # Routine case: 12B only.
    missing_routine = [0x0110, 0x0111]
    client, calls = _make_client(missing_routine, _valid_candidate_payload)
    specs = await client.generate_candidates(
        {"missing_codepoints": missing_routine, "units_per_em": 1000, "source_hash": "s" * 64}
    )
    assert len(specs) == 2
    assert [c["model"] for c in calls] == [MODEL_PRIMARY]

    # Difficult case (deterministic escalation by glyph count): 12B -> 27B.
    missing_difficult = [0x1EA0, 0x1EA1, 0x1EA2, 0x1EA3, 0x1EA4, 0x1EA5, 0x1EA6]
    client2, calls2 = _make_client(
        missing_difficult, _valid_candidate_payload, difficult_body=_valid_candidate_payload
    )
    await client2.generate_candidates(
        {"missing_codepoints": missing_difficult, "units_per_em": 1000, "source_hash": "s" * 64}
    )
    assert [c["model"] for c in calls2] == [MODEL_PRIMARY, MODEL_DIFFICULT]

    # Unresolved deterministic disagreement after escalation: 12B -> 27B -> arbiter once.
    missing_disagree = [0x1EA0, 0x1EA1, 0x1EA2, 0x1EA3, 0x1EA4, 0x1EA5, 0x1EA6]
    client3, calls3 = _make_client(
        missing_disagree,
        lambda m: _valid_candidate_payload(m, variant=0),
        difficult_body=lambda m: _valid_candidate_payload(m, variant=7),
        arbiter_body='{"choice":"B"}',
    )
    specs3 = await client3.generate_candidates(
        {"missing_codepoints": missing_disagree, "units_per_em": 1000, "source_hash": "s" * 64}
    )
    assert [c["model"] for c in calls3] == [MODEL_PRIMARY, MODEL_DIFFICULT, MODEL_ARBITER]
    assert len(specs3) == 7

    # Zero-call path: no missing glyphs -> no model calls.
    client4, calls4 = _make_client([], _valid_candidate_payload)
    assert await client4.generate_candidates({"missing_codepoints": []}) == []
    assert calls4 == []

    # No substitute models ever.
    allowed = {MODEL_PRIMARY, MODEL_DIFFICULT, MODEL_ARBITER}
    for call_list in (calls, calls2, calls3, calls4):
        assert all(c["model"] in allowed for c in call_list)
