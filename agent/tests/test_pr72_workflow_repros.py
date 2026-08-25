"""PR #72 review 5019323134 KNOWN_REPRO pack (workflow conformance).

DUMP_DOM_ENVELOPE, SESSION_HTML_NOT_FONT, MONOTYPE_TARGET, AI_STYLE_PAYLOAD,
L3_PROVENANCE.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from acquisition.models import BinaryAcquisitionPolicy, DiscoveryEnvelope, SpriteRasterPage
from acquisition.pipeline import AcquisitionPipeline
from acquisition.providers import (
    MonotypeRasterProvider,
    PersistentSessionBinaryProvider,
    parse_discovery_from_dump,
)
from acquisition.adapters import MonotypeRasterHttpClient
from compute.binary_cache import AuthorizedBinaryCache, BinaryCacheIdentity
from compute.openrouter_client import MODEL_ARBITER, MODEL_PRIMARY, OpenRouterAIClient
from compute.vietnamese import VietnameseAIIntegrityError
from acquisition.models import BINARY_STAGE_AUTHORIZED_SESSION, BINARY_STAGE_DUMP_DOM
from queue_client import CloudflareQueueClient, QueueMessage
from runner import A23Runner, RunnerAction
from tests.test_issue71_adversarial import ISSUE71_CONFIG, CountingAcquirer, _build_real_ttf, _runner_state, _wire_handlers
from tests.test_issue72_review_repros import RASTER_HANDOFF_MD5, _raster_pages_for_seed
from worker_client import WorkerJobClient

ENVELOPE_MD5 = "0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d"


def _realistic_dump(family: str, ttf_b64: str, md5: str) -> str:
    return (
        '<script type="application/ld+json">'
        '{"@type": "Product", "name": "' + family + '", "variantName": "Regular",'
        ' "font_md5": "' + md5 + '"}</script>'
        '<link href="https://cdn.provider.example/fonts/' + md5 + '/family.woff2">'
        '<style>@font-face { src: url(data:font/ttf;base64,' + ttf_b64 + ') format("truetype"); }</style>'
    )


# =========================================================================
# DUMP_DOM_ENVELOPE
# =========================================================================

def test_DUMP_DOM_ENVELOPE_typed_identities_from_realistic_dump():
    ttf = _build_real_ttf("Envelope Fam", "Regular")
    ttf_b64 = base64.b64encode(ttf).decode()
    dump = _realistic_dump("Envelope Fam", ttf_b64, ENVELOPE_MD5)

    envelope = parse_discovery_from_dump(dump, "https://www.myfonts.com/collections/envelope-fam", BINARY_STAGE_DUMP_DOM)
    assert envelope.family_name == "Envelope Fam"
    assert envelope.style_name == "Regular"
    assert envelope.md5 == ENVELOPE_MD5
    assert envelope.raster_identity == ENVELOPE_MD5
    formats = {c.format for c in envelope.binary_candidates}
    assert "WOFF2" in formats  # authorized container candidate discovered
    assert "TTF" in formats  # embedded data-URI candidate discovered
    assert envelope.has_raster_target()

    async def run():
        fetched: list[str] = []

        async def binary_fetch(url: str):
            fetched.append(url)
            return None

        pipeline = AcquisitionPipeline(
            dump_dom_transport=_StaticDump(dump),
            binary_fetch=binary_fetch,
            session_provider=None,
            raster_provider=None,
        )
        outcome = await pipeline.acquire(
            "https://www.myfonts.com/collections/envelope-fam", "Envelope Fam", "Regular"
        )
        assert outcome.kind == "binary"
        assert outcome.binary is not None
        assert outcome.binary.format == "TTF"
        assert outcome.binary.provenance == BINARY_STAGE_DUMP_DOM
        assert outcome.discovery is not None
        assert outcome.discovery.md5 == ENVELOPE_MD5
        # Embedded candidate resolved without network fetch of page URL.
        assert all("collections" not in u for u in fetched)

    import asyncio

    asyncio.run(run())


class _StaticDump:
    def __init__(self, dump: str):
        self.dump = dump
        self.calls = 0

    async def dump_dom(self, url: str) -> str:
        self.calls += 1
        return self.dump


# =========================================================================
# SESSION_HTML_NOT_FONT
# =========================================================================

class _HtmlSessionTransport:
    """Session discovery that can only produce collection-page HTML."""

    def __init__(self):
        self.calls = 0

    async def discover(self, envelope: DiscoveryEnvelope, source_url: str, session_material):
        self.calls += 1
        return b"<!doctype html><html><body>collection page</body></html>"


class _RecordingRasterClient:
    def __init__(self, pages):
        self.pages = pages
        self.requests: list[dict] = []

    async def fetch_sprite_page(self, request, cursor):
        self.requests.append(dict(request))
        if cursor:
            return None
        return self.pages[0]


@pytest.mark.asyncio
async def test_SESSION_HTML_NOT_FONT_html_never_poisons_binary_verification():
    ttf_b64 = base64.b64encode(_build_real_ttf("Html Fam", "Regular")).decode()
    # Dump has metadata + a URL candidate (not embedded): session stage will try it.
    dump = (
        '<script type="application/ld+json">{"name": "Html Fam", "variantName": "Regular",'
        ' "font_md5": "' + ENVELOPE_MD5 + '"}</script>'
        '<a href="https://cdn.provider.example/fonts/' + ENVELOPE_MD5 + '/font.woff2">font</a>'
    )

    class _SessionMaterial:
        async def material(self):
            return {"cookies": {"cf_clearance": "opaque-runtime-secret"}}

    pages = _raster_pages_for_seed("session_html_v1")
    raster_client = _RecordingRasterClient(pages)
    session_transport = _HtmlSessionTransport()

    pipeline = AcquisitionPipeline(
        dump_dom_transport=_StaticDump(dump),
        binary_fetch=None,  # no binary bytes obtainable from dump stage
        session_provider=PersistentSessionBinaryProvider(_SessionMaterial(), session_transport),
        raster_provider=MonotypeRasterProvider(raster_client),
    )
    outcome = await pipeline.acquire("https://www.myfonts.com/collections/html-fam", "Html Fam", "Regular")

    # HTML under a valid session never becomes a font candidate and never
    # terminally poisons verification; typed discovery continues to raster.
    assert session_transport.calls == 1
    assert outcome.kind == "raster_authorized"
    assert outcome.raster_pages
    outcomes = [r.outcome for r in outcome.trace.records]
    assert "INTEGRITY_FAILED" not in outcomes
    # Raster stage received the exact MD5-bound target.
    assert raster_client.requests[0]["md5"] == ENVELOPE_MD5


# =========================================================================
# MONOTYPE_TARGET
# =========================================================================

def test_MONOTYPE_TARGET_exact_family_style_md5_on_every_request():
    async def run():
        pages = _raster_pages_for_seed("monotype_target_v1")
        client = _RecordingRasterClient(pages)
        provider = MonotypeRasterProvider(client)
        policy = BinaryAcquisitionPolicy(max_sprite_pages=4)
        target = {"family": "Target Fam", "style": "Regular", "md5": ENVELOPE_MD5}
        result = await provider.fetch_sprite_pages(target, policy)
        assert result
        assert client.requests, "no page request issued"
        for req in client.requests:
            assert req["family"] == "Target Fam"
            assert req["style"] == "Regular"
            assert req["md5"] == ENVELOPE_MD5

        # Empty/incomplete target fails closed without any request.
        client2 = _RecordingRasterClient(pages)
        provider2 = MonotypeRasterProvider(client2)
        assert await provider2.fetch_sprite_pages({"family": "X", "style": "Regular", "md5": ""}, policy) == ()
        assert await provider2.fetch_sprite_pages({}, policy) == ()
        assert client2.requests == []

    import asyncio

    asyncio.run(run())


def test_MONOTYPE_TARGET_production_client_cross_style_echo_fails_closed():
    page_payload = {"browser_version": "bv", "glyphs": [{"code_point": 65}]}

    def handler_factory(response_overrides: dict):
        captured: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            captured.append(body)
            data = {
                "family": body.get("family"),
                "style": body.get("style"),
                "md5": body.get("md5"),
                "final": True,
                "payload": page_payload,
            }
            data.update(response_overrides)
            return httpx.Response(200, json=data)

        return handler, captured

    async def run():
        # Correct echo: page accepted with the exact target on the request.
        handler, captured = handler_factory({})
        client = MonotypeRasterHttpClient("https://provider.example/raster", "token", timeout_seconds=5)
        page = await _fetch_with_transport(client, httpx.MockTransport(handler), {"family": "Target Fam", "style": "Regular", "md5": ENVELOPE_MD5})
        assert page is not None
        assert captured[0]["family"] == "Target Fam"
        assert captured[0]["style"] == "Regular"
        assert captured[0]["md5"] == ENVELOPE_MD5

        # Cross-style echo (wrong md5) fails closed.
        handler2, captured2 = handler_factory({"md5": "f" * 32})
        page2 = await _fetch_with_transport(client, httpx.MockTransport(handler2), {"family": "Target Fam", "style": "Regular", "md5": ENVELOPE_MD5})
        assert page2 is None

        # Empty target never issues a request.
        handler3, captured3 = handler_factory({})
        page3 = await _fetch_with_transport(client, httpx.MockTransport(handler3), {"family": "", "style": "", "md5": ""})
        assert page3 is None
        assert captured3 == []

    import asyncio

    asyncio.run(run())


async def _fetch_with_transport(client: MonotypeRasterHttpClient, transport, request: dict):
    """Bind a mock transport to the production client for one call."""
    import acquisition.adapters as adapters_mod

    original = adapters_mod.httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        kwargs.pop("timeout", None)
        return original(*args, timeout=client.timeout_seconds, **kwargs)

    adapters_mod.httpx.AsyncClient = patched
    try:
        return await client.fetch_sprite_page(request, "")
    finally:
        adapters_mod.httpx.AsyncClient = original


# =========================================================================
# AI_STYLE_PAYLOAD
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


@pytest.mark.asyncio
async def test_AI_STYLE_PAYLOAD_style_evidence_and_candidate_scores():
    style_evidence = {
        "family_name": "Style Fam",
        "style_name": "Regular",
        "units_per_em": 1000,
        "glyph_count": 3,
        "stroke_contrast_proxy": 1.31,
        "sample_glyphs": [
            {
                "code_point": 65,
                "contours": [[[50.0, 50.0], [550.0, 50.0], [550.0, 700.0], [50.0, 700.0]]],
                "advance_width_upem": 600.0,
                "lsb_upem": 50.0,
                "raster_sample_hashes": ["b" * 64],
            }
        ],
    }
    missing = [0x1EA0, 0x1EA1, 0x1EA2, 0x1EA3, 0x1EA4, 0x1EA5, 0x1EA6]
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        model = body["model"]
        if model == MODEL_PRIMARY:
            content = _valid_candidate_payload(missing, variant=0)
        elif model == MODEL_ARBITER:
            content = '{"choice":"B"}'
        else:
            content = _valid_candidate_payload(missing, variant=9)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    client = OpenRouterAIClient(
        "test-key-runtime-secret", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    request = {
        "missing_codepoints": missing,
        "units_per_em": 1000,
        "source_hash": "s" * 64,
        "style_evidence": style_evidence,
    }
    await client.generate_candidates(request)

    # 12B/27B requests carry bounded source glyph/raster/metric/style evidence.
    generation_calls = [c for c in captured if c["model"] in (MODEL_PRIMARY,)]
    assert generation_calls
    primary_prompt = generation_calls[0]["messages"][0]["content"]
    assert "sample_glyphs" in primary_prompt
    assert "raster_sample_hashes" in primary_prompt
    assert "stroke_contrast_proxy" in primary_prompt
    assert "advance_width_upem" in primary_prompt

    # Arbiter request carries comparable candidate evidence + deterministic
    # scores, never opaque hashes alone.
    arbiter_calls = [c for c in captured if c["model"] == MODEL_ARBITER]
    assert arbiter_calls
    arbiter_prompt = arbiter_calls[0]["messages"][0]["content"]
    assert "glyph_count" in arbiter_prompt
    assert "mean_advance_upem" in arbiter_prompt
    assert "total_outline_area" in arbiter_prompt
    assert "sample_glyph" in arbiter_prompt
    assert "sample_glyphs" in arbiter_prompt  # source style evidence for comparison

    # Missing style evidence fails closed.
    with pytest.raises(VietnameseAIIntegrityError):
        await client.generate_candidates(
            {"missing_codepoints": [0x0110], "units_per_em": 1000, "source_hash": "s" * 64}
        )


# =========================================================================
# L3_PROVENANCE
# =========================================================================

@pytest.mark.asyncio
async def test_L3_PROVENANCE_stage_bound_identity_and_compatible_repeat(test_settings, tmp_path: Path):
    from config import Settings
    from compute.archive import FinalFontArchive, canonical_source_identity

    ttf = _build_real_ttf("Prov Fam", "Regular")
    dump = _realistic_dump("Prov Fam", base64.b64encode(ttf).decode(), ENVELOPE_MD5)

    binary_cache = AuthorizedBinaryCache(tmp_path / "bc", tmp_path / "bc.sqlite3")
    store_dir = tmp_path / "obs"
    store_dir.mkdir()

    settings = test_settings.model_copy(update={"FONT_ARCHIVE_ROOT": tmp_path / "archive_root"})
    archive = FinalFontArchive(settings.FONT_ARCHIVE_ROOT, settings.SCRATCH_DIR / "archive_idx.sqlite3")

    def make_runner(state, pipeline):
        acquirer = CountingAcquirer(
            client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
            observation_store_dir=store_dir,
            observation_config=ISSUE71_CONFIG,
        )
        queue_handler, worker_handler = _wire_handlers(state)
        return A23Runner(
            settings,
            CloudflareQueueClient(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(queue_handler))),
            WorkerJobClient(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(worker_handler))),
            source_acquirer=acquirer,
            archive=archive,
            acquisition_pipeline=pipeline,
            binary_cache=binary_cache,
        ), acquirer

    # First job: dump-dom binary win -> stored under dump-dom stage provenance.
    state1 = _runner_state(["TTF"])
    state1["source_url"] = "https://www.myfonts.com/collections/prov-fam"
    state1["styles"] = [{"id": "regular", "display_name": "Regular"}]
    dump1 = _StaticDump(dump)
    runner1, _ = make_runner(
        state1, AcquisitionPipeline(dump_dom_transport=dump1, session_provider=None, raster_provider=None)
    )
    msg1 = QueueMessage(id="m1", lease_id="l1", body_raw='{"job_id":"job_i71"}', attempts=1, job_id="job_i71")
    res1 = await runner1.process_message(msg1)
    assert res1.action == RunnerAction.ACKED

    ref_fp = hashlib.sha256(
        canonical_source_identity("https://www.myfonts.com/collections/prov-fam").encode("utf-8")
    ).hexdigest()
    dump_identity = BinaryCacheIdentity(
        reference_fingerprint=ref_fp, family_name="Prov Fam", style_id="regular",
        provenance=BINARY_STAGE_DUMP_DOM,
    )
    session_identity = BinaryCacheIdentity(
        reference_fingerprint=ref_fp, family_name="Prov Fam", style_id="regular",
        provenance=BINARY_STAGE_AUTHORIZED_SESSION,
    )
    # Provenance is bound to the actual stage, not a collapsed constant.
    assert binary_cache.get(dump_identity)[3] == "HIT"
    assert binary_cache.get(session_identity)[3] == "MISS"  # cross-stage collision rejected

    # Exact compatible repeat: sabotaged providers, zero network/reconstruction.
    state2 = _runner_state(["TTF"])
    state2["source_url"] = "https://www.myfonts.com/collections/prov-fam"
    state2["styles"] = [{"id": "regular", "display_name": "Regular"}]
    dump2 = _StaticDump("<html>no metadata</html>")
    dump2.calls = 0
    runner2, acquirer2 = make_runner(
        state2, AcquisitionPipeline(dump_dom_transport=dump2, session_provider=None, raster_provider=None)
    )
    msg2 = QueueMessage(id="m2", lease_id="l2", body_raw='{"job_id":"job_i71"}', attempts=1, job_id="job_i71")
    res2 = await runner2.process_message(msg2)
    assert res2.action == RunnerAction.ACKED
    assert dump2.calls == 0  # zero provider/network calls on compatible repeat
    assert acquirer2.acquire_calls == 0
    events = [e for e in runner2.last_reuse_trace["events"] if e["event"] == "L3_CACHE_HIT"]
    assert events and events[0]["provenance"] == BINARY_STAGE_DUMP_DOM
