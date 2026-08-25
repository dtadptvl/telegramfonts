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
from acquisition.adapters import MonotypeRenderClient
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


def test_MONOTYPE_REAL_PROTOCOL_get_md5_path_render_contract():
    """Captured request is HTTPS GET to the MD5 path with the approved render
    query/header contract; no generic POST/Bearer JSON."""
    captured: list[httpx.Request] = []
    sprite_b64 = base64.b64encode(b"\x89PNG-fake-sprite").decode()

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={"layout": {"0": {"codePoint": 65}}, "image": sprite_b64},
        )

    async def run():
        client = MonotypeRenderClient()
        page = await _fetch_with_transport(
            client, httpx.MockTransport(handler),
            {"family": "Real Fam", "style": "Regular", "md5": ENVELOPE_MD5},
        )
        assert page is not None
        req = captured[0]
        assert req.method == "GET"
        assert req.url.scheme == "https"
        assert req.url.host == "sig.monotype.com"
        assert req.url.path == f"/render/105/font/{ENVELOPE_MD5}"
        q = dict(req.url.params)
        assert q["rbe"] == "gmap"
        assert q["acs_pt"] == "120"
        assert q["acs_w"] == "1500"
        assert q["acs_l"] == "1"
        assert q["acs_ar"] == "0"
        assert q["acs_p"] == "1"
        assert q["acs_gpp"] == "100"
        assert "Authorization" not in req.headers
        assert req.headers["Referer"] == "https://www.myfonts.com/"
        assert req.headers["Origin"] == "https://www.myfonts.com"
        assert "Mozilla" in req.headers["User-Agent"]

    import asyncio

    asyncio.run(run())


def _real_shape_response(cps: list[int], page_has_more: bool = False) -> dict:
    """Sanitized real-shape render response: layout map + base64 sprite."""
    sprite_b64 = base64.b64encode(b"\x89PNG-real-shape-sprite").decode()
    layout = {}
    for i, cp in enumerate(cps):
        layout[str(i)] = {
            "codePoint": cp,
            "metrics": {
                "advanceWidthPx": 46.8,
                "lsbPx": 3.6,
                "rsbPx": 3.6,
                "ascentPx": 50.4,
                "descentPx": -14.4,
                "advanceWidthUpem": 600.0,
                "lsbUpem": 50.0,
                "rsbUpem": 50.0,
                "ascentUpem": 700.0,
                "descentUpem": -200.0,
                "bboxWidthUpem": 500.0,
                "bboxHeightUpem": 650.0,
            },
            "box": {"x": 0, "y": 0, "w": 100, "h": 100},
        }
    return {"layout": layout, "image": sprite_b64}


def test_MONOTYPE_REAL_RESPONSE_real_shape_fixture_yields_pages_and_completion():
    requests_seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(dict(request.url.params)["acs_p"])
        requests_seen.append({"page": page, "path": request.url.path})
        if page == 1:
            return httpx.Response(200, json=_real_shape_response([65, 66]))
        # Empty layout marks bounded completion.
        return httpx.Response(200, json={"layout": {}, "image": base64.b64encode(b"x").decode()})

    async def run():
        client = MonotypeRenderClient()
        provider = MonotypeRasterProvider(_TransportBoundClient(client, httpx.MockTransport(handler)))
        target = {"family": "Real Fam", "style": "Regular", "md5": ENVELOPE_MD5}
        pages = await provider.fetch_sprite_pages(target, BinaryAcquisitionPolicy(max_sprite_pages=8))
        assert len(pages) == 1  # bounded completion at empty second page
        page = pages[0]
        assert page.glyph_count == 2
        glyphs = page.payload["glyphs"]
        assert [g["code_point"] for g in glyphs] == [65, 66]
        assert all(g["metrics"]["advance_width_upem"] == 600.0 for g in glyphs)
        assert page.payload["browser_version"] == MonotypeRenderClient.BROWSER_VERSION
        assert page.raster_bytes == b"\x89PNG-real-shape-sprite"
        assert [r["path"] for r in requests_seen] == [
            f"/render/105/font/{ENVELOPE_MD5}",
            f"/render/105/font/{ENVELOPE_MD5}",
        ]

    import asyncio

    asyncio.run(run())


class _TransportBoundClient:
    """Bind a mock transport to the production render client for tests."""

    def __init__(self, client: MonotypeRenderClient, transport):
        self._client = client
        self._transport = transport

    async def fetch_sprite_page(self, request, cursor):
        return await _fetch_with_transport(self._client, self._transport, request, cursor=cursor)


async def _fetch_with_transport(client, transport, request: dict, cursor: str = ""):
    """Bind a mock transport to the production client for one call."""
    import acquisition.adapters as adapters_mod

    original = adapters_mod.httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        kwargs.pop("timeout", None)
        return original(*args, timeout=client.timeout_seconds, **kwargs)

    adapters_mod.httpx.AsyncClient = patched
    try:
        return await client.fetch_sprite_page(request, cursor)
    finally:
        adapters_mod.httpx.AsyncClient = original


def test_MONOTYPE_BAD_TARGET_fail_closed_matrix():
    sprite_b64 = base64.b64encode(b"\x89PNG-sprite").decode()

    async def run():
        client = MonotypeRenderClient()

        # Empty/invalid target: no request, fail closed.
        def counting_handler(request):
            raise AssertionError("no request may be issued for an invalid target")

        assert await _fetch_with_transport(client, httpx.MockTransport(counting_handler), {"family": "", "style": "", "md5": ""}) is None
        assert await _fetch_with_transport(client, httpx.MockTransport(counting_handler), {"family": "F", "style": "R", "md5": "short"}) is None

        # Malformed layout fails closed.
        bad_layout = httpx.MockTransport(lambda r: httpx.Response(200, json={"layout": "nope", "image": sprite_b64}))
        assert await _fetch_with_transport(client, bad_layout, {"family": "F", "style": "R", "md5": ENVELOPE_MD5}) is None

        # Missing raster fails closed.
        no_image = httpx.MockTransport(lambda r: httpx.Response(200, json={"layout": {"0": {"codePoint": 65}}}))
        assert await _fetch_with_transport(client, no_image, {"family": "F", "style": "R", "md5": ENVELOPE_MD5}) is None

        # Cross-style / invalid code point fails closed.
        bad_cp = httpx.MockTransport(lambda r: httpx.Response(200, json={"layout": {"0": {"codePoint": -3}}, "image": sprite_b64}))
        assert await _fetch_with_transport(client, bad_cp, {"family": "F", "style": "R", "md5": ENVELOPE_MD5}) is None

        # Incomplete coverage (pairs/features absent) publishes nothing:
        # ingestion fails closed before completion is recorded.
        from acquisition.raster_ingest import ingest_raster_pages
        from measurement.store import ObservationStore
        from compute.vietnamese import VietnameseAIIntegrityError  # noqa: F401  (import guard only)

        page = MonotypeRenderClient._parse_page(_real_shape_response([65]), 1)
        assert page is not None
        store_dir = Path(tempfile.mkdtemp())
        store = ObservationStore(store_dir)
        with pytest.raises(ValueError):
            ingest_raster_pages(
                store, ISSUE71_CONFIG, "bad_target_fam", "regular",
                MonotypeRenderClient.BROWSER_VERSION, [page],
            )
        assert store.is_source_collection_completed(
            "bad_target_fam", "regular",
            config_hash=ISSUE71_CONFIG.compute_hash(),
            browser_version=MonotypeRenderClient.BROWSER_VERSION,
        ) is False

        # Extra pages stay bounded (budget smaller than available pages).
        page_counter = {"n": 0}

        def endless_handler(request):
            page_counter["n"] += 1
            return httpx.Response(200, json=_real_shape_response([65]))

        provider = MonotypeRasterProvider(_TransportBoundClient(client, httpx.MockTransport(endless_handler)))
        pages = await provider.fetch_sprite_pages(
            {"family": "F", "style": "R", "md5": ENVELOPE_MD5},
            BinaryAcquisitionPolicy(max_sprite_pages=2),
        )
        assert len(pages) == 2 and page_counter["n"] == 2  # bounded, deterministic

    import asyncio
    import tempfile

    asyncio.run(run())


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
