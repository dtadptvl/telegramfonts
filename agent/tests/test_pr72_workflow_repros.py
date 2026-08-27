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

    pages = _raster_pages_for_seed("session_html_v1", md5=ENVELOPE_MD5)
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


def _captured_sprite_bytes(cps: list[int]) -> bytes:
    """Real binary PNG sprite for the captured response shape."""
    import io

    from PIL import Image, ImageDraw

    img = Image.new("RGB", (60 * max(len(cps), 1) + 10, 100), "white")
    draw = ImageDraw.Draw(img)
    for i, _cp in enumerate(cps):
        draw.rectangle([60 * i + 6, 10, 60 * i + 54, 90], fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _captured_expected_slice(cps: list[int], cp: int) -> bytes:
    """Deterministic re-slice of the captured sprite at the layout box."""
    import io

    from PIL import Image

    i = cps.index(cp)
    cell = Image.open(io.BytesIO(_captured_sprite_bytes(cps))).crop((60 * i, 0, 60 * i + 59, 100))
    buf = io.BytesIO()
    cell.save(buf, format="PNG")
    return buf.getvalue()


def _captured_shape_response(
    cps: list[int],
    status: int = 200,
    include_image: bool = True,
    sprite_bytes: bytes | None = None,
) -> dict:
    """Captured real render response shape: status + layout boxes + binary PNG.

    Layout entries carry only the observable provider fields
    (glyph/x/y/width/height/codePoint). The sprite is a real binary PNG.
    """
    if sprite_bytes is None:
        sprite_bytes = _captured_sprite_bytes(cps)
    layout = {
        str(i): {
            "glyph": i,
            "x": 60 * i,
            "y": 0,
            "width": 59,
            "height": 100,
            "codePoint": cp,
        }
        for i, cp in enumerate(cps)
    }
    body: dict = {"status": status, "layout": layout}
    if include_image:
        body["image"] = base64.b64encode(sprite_bytes).decode()
    return body


CAPTURED_HEADERS = {
    "content-type": "application/json; charset=utf-8",
    "max-glyphs-per-page": "100",
    "x-missing-unicodes": "U+00FF",
    "x-tofus-found": "0",
}


def test_MONOTYPE_CAPTURED_HEADERS_binary_png_contract():
    """Captured request contract + consumed binary PNG response evidence.

    GET to the MD5 path with the approved render query/header contract; the
    response is consumed as status/Content-Type/JSON-body with a base64
    binary PNG sprite validated by magic + size, bound to the exact target.
    No invented JSON metrics anywhere.
    """
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_captured_shape_response([65, 66]), headers=CAPTURED_HEADERS)

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

        # Consumed binary PNG response evidence.
        assert page.glyph_count == 2
        assert page.raster_bytes.startswith(b"\x89PNG\r\n\x1a\n")
        assert page.payload["sprite_sha256"] == hashlib.sha256(page.raster_bytes).hexdigest()
        observed = page.payload["observed_headers"]
        assert observed["content_type"] == "application/json; charset=utf-8"
        assert observed["max_glyphs_per_page"] == 100
        assert observed["x_missing_unicodes"] == "U+00FF"
        assert observed["x_tofus_found"] == "0"
        # Glyph binding is the observable codePoint + sprite-cell box only.
        for g in page.payload["glyphs"]:
            assert set(g.keys()) == {"code_point", "glyph_index", "sprite_box"}
            assert set(g["sprite_box"].keys()) == {"x", "y", "width", "height"}
        assert [g["code_point"] for g in page.payload["glyphs"]] == [65, 66]
        # Every page binds the exact MD5/render-size/request parameters.
        assert page.payload["md5"] == ENVELOPE_MD5
        assert page.payload["acs_pt"] == 120
        assert page.payload["request_params"]["acs_pt"] == "120"
        assert page.payload["request_params"]["acs_p"] == "1"
        assert page.payload["request_params"]["rbe"] == "gmap"
        # Raster-only source: never metrics/pairs/features.
        assert page.payload["pairs"] == [] and page.payload["features"] == []
        assert "metrics" not in json.dumps(page.payload)

        # Fail-closed binary/response contract matrix.
        not_json_ct = httpx.MockTransport(lambda r: httpx.Response(
            200, content=json.dumps(_captured_shape_response([65])).encode(),
            headers={"content-type": "image/png"},
        ))
        assert await _fetch_with_transport(client, not_json_ct, {"family": "F", "style": "R", "md5": ENVELOPE_MD5}) is None

        bad_status = httpx.MockTransport(lambda r: httpx.Response(
            200, json=_captured_shape_response([65], status=500), headers=CAPTURED_HEADERS))
        assert await _fetch_with_transport(client, bad_status, {"family": "F", "style": "R", "md5": ENVELOPE_MD5}) is None

        not_png = httpx.MockTransport(lambda r: httpx.Response(
            200,
            json={"status": 200, "layout": {"0": {"glyph": 0, "x": 0, "y": 0, "width": 10, "height": 10, "codePoint": 65}},
                  "image": base64.b64encode(b"not-a-png-sprite").decode()},
            headers=CAPTURED_HEADERS))
        assert await _fetch_with_transport(client, not_png, {"family": "F", "style": "R", "md5": ENVELOPE_MD5}) is None

        no_image = httpx.MockTransport(lambda r: httpx.Response(
            200, json={"status": 200, "layout": {"0": {"codePoint": 65}}}, headers=CAPTURED_HEADERS))
        assert await _fetch_with_transport(client, no_image, {"family": "F", "style": "R", "md5": ENVELOPE_MD5}) is None

        malformed_layout = httpx.MockTransport(lambda r: httpx.Response(
            200, json={"status": 200, "layout": "nope", "image": base64.b64encode(b"\x89PNG").decode()},
            headers=CAPTURED_HEADERS))
        assert await _fetch_with_transport(client, malformed_layout, {"family": "F", "style": "R", "md5": ENVELOPE_MD5}) is None

        bad_box = httpx.MockTransport(lambda r: httpx.Response(
            200, json={"status": 200, "layout": {"0": {"glyph": 0, "x": "wide", "y": 0, "width": 5, "height": 5, "codePoint": 65}},
                       "image": base64.b64encode(b"\x89PNG\r\n\x1a\nrest").decode()},
            headers=CAPTURED_HEADERS))
        assert await _fetch_with_transport(client, bad_box, {"family": "F", "style": "R", "md5": ENVELOPE_MD5}) is None

    import asyncio

    asyncio.run(run())


def test_MONOTYPE_PAGINATION_bounded_coverage_and_termination():
    """Bounded pagination/coverage over the observable provider signals.

    Termination signals are the captured empty layout and the
    max-glyphs-per-page partial-fill header; coverage is exactly the
    observable code points; unmapped glyph slots are skipped, never bound.
    """
    requests_seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(dict(request.url.params)["acs_p"])
        requests_seen.append({"page": page, "path": request.url.path})
        if page == 1:
            # Full page under the declared per-page maximum.
            return httpx.Response(
                200, json=_captured_shape_response([65, 66]),
                headers={**CAPTURED_HEADERS, "max-glyphs-per-page": "2"},
            )
        if page == 2:
            # Partial fill below the maximum: observable final page signal.
            return httpx.Response(
                200, json=_captured_shape_response([67]),
                headers={**CAPTURED_HEADERS, "max-glyphs-per-page": "2"},
            )
        # Captured bounded-completion shape: empty layout + placeholder PNG.
        return httpx.Response(
            200, json=_captured_shape_response([]), headers=CAPTURED_HEADERS
        )

    async def run():
        client = MonotypeRenderClient()
        provider = MonotypeRasterProvider(_TransportBoundClient(client, httpx.MockTransport(handler)))
        target = {"family": "Real Fam", "style": "Regular", "md5": ENVELOPE_MD5}
        pages = await provider.fetch_sprite_pages(target, BinaryAcquisitionPolicy(max_sprite_pages=8))
        assert [p.glyph_count for p in pages] == [2, 1]  # stopped at partial fill
        assert [r["path"] for r in requests_seen] == [
            f"/render/105/font/{ENVELOPE_MD5}",
            f"/render/105/font/{ENVELOPE_MD5}",
        ]
        coverage = sorted(g["code_point"] for p in pages for g in p.payload["glyphs"])
        assert coverage == [65, 66, 67]

        # Empty-layout termination (no max-glyphs-per-page header present).
        requests_seen.clear()

        def empty_after_one(request: httpx.Request) -> httpx.Response:
            page = int(dict(request.url.params)["acs_p"])
            requests_seen.append({"page": page})
            if page == 1:
                return httpx.Response(200, json=_captured_shape_response([65]), headers={"content-type": "application/json"})
            return httpx.Response(200, json=_captured_shape_response([]), headers={"content-type": "application/json"})

        provider2 = MonotypeRasterProvider(_TransportBoundClient(client, httpx.MockTransport(empty_after_one)))
        pages2 = await provider2.fetch_sprite_pages(target, BinaryAcquisitionPolicy(max_sprite_pages=8))
        assert [p.glyph_count for p in pages2] == [1]
        assert [r["page"] for r in requests_seen] == [1, 2]  # bounded completion at empty layout

        # Page budget stays bounded (RASTER_MAX semantics): full-fill pages
        # declare no completion signal, so only the budget terminates.
        page_counter = {"n": 0}

        def endless_handler(request):
            page_counter["n"] += 1
            return httpx.Response(
                200, json=_captured_shape_response([65]),
                headers={**CAPTURED_HEADERS, "max-glyphs-per-page": "1"},
            )

        provider3 = MonotypeRasterProvider(_TransportBoundClient(client, httpx.MockTransport(endless_handler)))
        pages3 = await provider3.fetch_sprite_pages(target, BinaryAcquisitionPolicy(max_sprite_pages=2))
        assert len(pages3) == 2 and page_counter["n"] == 2  # bounded, deterministic

        # Unmapped glyph slots (codePoint <= 0) are skipped, never bound;
        # a page with zero observable bindings fails closed.
        mixed = _captured_shape_response([65])
        mixed["layout"]["slot0"] = {"glyph": 99, "x": 0, "y": 0, "width": 5, "height": 5, "codePoint": 0}
        mixed_handler = httpx.MockTransport(lambda r: httpx.Response(200, json=mixed, headers=CAPTURED_HEADERS))
        mixed_page = await _fetch_with_transport(client, mixed_handler, target)
        assert mixed_page is not None
        assert [g["code_point"] for g in mixed_page.payload["glyphs"]] == [65]
        assert mixed_page.payload["unmapped_glyph_slots"] == 1

        only_unmapped = _captured_shape_response([])
        only_unmapped["layout"] = {"0": {"glyph": 0, "x": 0, "y": 0, "width": 5, "height": 5, "codePoint": 0}}
        unmapped_handler = httpx.MockTransport(lambda r: httpx.Response(200, json=only_unmapped, headers=CAPTURED_HEADERS))
        assert await _fetch_with_transport(client, unmapped_handler, target) is None

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


def test_RASTER_FALLBACK_E2E_cdn_pixels_as_reconstruction_observations(tmp_path: Path):
    """End-to-end fallback-to-observation under the captured contract.

    The production client consumes the real captured response shape; the
    bounds-checked CDN sprite slices are persisted as the reconstruction
    raster observations (one per render size, phase (0.0, 0.0)), bound to
    the exact MD5/page/request parameters; browser evidence supplements
    metrics/pairs/features only.
    """
    from acquisition.capability import PROVIDER_MONOTYPE_RENDER, ProviderRasterCapability
    from acquisition.raster_ingest import ingest_raster_pages
    from measurement.store import ObservationStore
    from tests.test_issue72_review_repros import (
        RASTER_ONLY_CONFIG,
        _browser_supplement_for_seed,
    )

    target = {"family": "E2E Fam", "style": "Regular", "md5": ENVELOPE_MD5}
    supplement = _browser_supplement_for_seed("chromium_e2e_v1", config=RASTER_ONLY_CONFIG)
    capability = ProviderRasterCapability.deterministic_size_schedule(
        PROVIDER_MONOTYPE_RENDER, RASTER_ONLY_CONFIG.resolutions
    )
    pts = list(capability.all_sizes())

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        page = int(params["acs_p"])
        if page == 1:
            return httpx.Response(
                200, json=_captured_shape_response([65, 66]), headers=CAPTURED_HEADERS
            )
        return httpx.Response(
            200, json=_captured_shape_response([]), headers=CAPTURED_HEADERS
        )

    async def run():
        client = MonotypeRenderClient()
        provider = MonotypeRasterProvider(_TransportBoundClient(client, httpx.MockTransport(handler)))
        pages = await provider.fetch_sprite_pages(
            {**target, "acs_pts": pts}, BinaryAcquisitionPolicy(max_sprite_pages=4)
        )
        assert sorted(p.payload["acs_pt"] for p in pages) == sorted(pts)

        store = ObservationStore(tmp_path / "obs_e2e")
        ingested = ingest_raster_pages(
            store, RASTER_ONLY_CONFIG, "e2e_fam", "regular",
            supplement, pages, capability,
            source_url="https://www.myfonts.com/collections/e2e-fam",
        )
        assert ingested == 2
        cfg_h = RASTER_ONLY_CONFIG.compute_hash()
        assert store.get_coverage("e2e_fam", "regular", browser_version="chromium_e2e_v1", config_hash=cfg_h) == [65, 66]
        assert store.is_source_collection_completed(
            "e2e_fam", "regular", config_hash=cfg_h, browser_version="chromium_e2e_v1",
        )
        # Stored rasters are exactly the CDN slices (never recaptured).
        cps = [65, 66]
        for cp in cps:
            obs = store.get_glyph_observations(
                "e2e_fam", "regular", cp, browser_version="chromium_e2e_v1", config_hash=cfg_h,
            )
            assert {r.resolution for r, _ in obs} == set(pts)
            for rec, _ in obs:
                assert rec.subpixel_x == 0.0 and rec.subpixel_y == 0.0
                stored = (store.base_dir / rec.raster_relative_path).read_bytes()
                assert stored == _captured_expected_slice(cps, cp)
            assert obs[0][0].metrics.advance_width_upem in (650.0, 600.0)
        # Pair/feature provenance is the approved chromium canvas path.
        pairs = store.get_pair_observations("e2e_fam", "regular", browser_version="chromium_e2e_v1", config_hash=cfg_h)
        assert pairs and all(p["provenance"] == "chromium:chromium_e2e_v1:canvas_text_metrics" for p in pairs)
        feats = store.get_feature_observations("e2e_fam", "regular", browser_version="chromium_e2e_v1", config_hash=cfg_h)
        assert feats and all(f["provenance"] == "chromium:chromium_e2e_v1:canvas_feature_probe" for f in feats)

    import asyncio

    asyncio.run(run())


def test_RASTER_FALLBACK_fail_closed_matrix(tmp_path: Path):
    """Raster-only pages never complete without supplements; bindings,
    sizes, and capabilities fail closed with the exact cause."""
    from acquisition.capability import FIXED_PHASE, PROVIDER_MONOTYPE_RENDER, ProviderRasterCapability
    from acquisition.raster_ingest import (
        BrowserSupplementalEvidence,
        ingest_raster_pages,
        page_slice_attestation,
    )
    from measurement.store import ObservationStore
    from tests.test_issue72_review_repros import (
        ISSUE71_CONFIG,
        _browser_supplement_for_seed,
        _raster_pages_for_seed,
    )

    capability = ProviderRasterCapability.deterministic_size_schedule(
        PROVIDER_MONOTYPE_RENDER, ISSUE71_CONFIG.resolutions
    )
    pages = [
        _raster_pages_for_seed(MonotypeRenderClient.BROWSER_VERSION, acs_pt=pt)[0]
        for pt in capability.all_sizes()
    ]
    supplement = _browser_supplement_for_seed("chromium_matrix_v1")
    cfg_h = ISSUE71_CONFIG.compute_hash()

    # Empty/invalid target: no request, fail closed (client level).
    async def run_client_matrix():
        client = MonotypeRenderClient()

        def counting_handler(request):
            raise AssertionError("no request may be issued for an invalid target")

        assert await _fetch_with_transport(client, httpx.MockTransport(counting_handler), {"family": "", "style": "", "md5": ""}) is None
        assert await _fetch_with_transport(client, httpx.MockTransport(counting_handler), {"family": "F", "style": "R", "md5": "short"}) is None

    import asyncio

    asyncio.run(run_client_matrix())

    # Raster pages without browser supplement can never complete.
    store = ObservationStore(tmp_path / "obs_matrix_a")
    with pytest.raises(ValueError, match="RASTER_INGEST_BROWSER_SUPPLEMENT_REQUIRED"):
        ingest_raster_pages(store, ISSUE71_CONFIG, "m_fam", "regular", None, pages, capability)
    assert store.is_source_collection_completed(
        "m_fam", "regular", config_hash=cfg_h, browser_version="chromium_matrix_v1"
    ) is False

    # Forged/missing capability descriptor fails closed.
    store_f = ObservationStore(tmp_path / "obs_matrix_f")
    with pytest.raises(ValueError, match="CAPABILITY_FORGED"):
        ingest_raster_pages(store_f, ISSUE71_CONFIG, "m_fam", "regular", supplement, pages, None)
    forged = ProviderRasterCapability(
        provider=PROVIDER_MONOTYPE_RENDER, phase=FIXED_PHASE,
        fit_sizes=(128,), held_out_sizes=(128,),
    )
    with pytest.raises(ValueError, match="CAPABILITY_FORGED"):
        ingest_raster_pages(store_f, ISSUE71_CONFIG, "m_fam", "regular", supplement, pages, forged)

    # Coverage drift between CDN binding and browser supplement fails closed.
    drifted = BrowserSupplementalEvidence(
        browser_version="chromium_matrix_v1",
        metrics={65: supplement.metrics[65]},
        pairs=supplement.pairs,
        features=supplement.features,
    )
    store_b = ObservationStore(tmp_path / "obs_matrix_b")
    with pytest.raises(ValueError, match="RASTER_INGEST_COVERAGE_DRIFT"):
        ingest_raster_pages(store_b, ISSUE71_CONFIG, "m_fam", "regular", drifted, pages, capability)

    # Missing allocated render size fails closed (no silent subset).
    store_m = ObservationStore(tmp_path / "obs_matrix_m")
    with pytest.raises(ValueError, match="RASTER_CAPABILITY_MISSING_SIZES"):
        ingest_raster_pages(
            store_m, ISSUE71_CONFIG, "m_fam", "regular", supplement, pages[:1], capability,
        )

    # Missing request binding fails closed (no relabeling possible).
    unbound = _raster_pages_for_seed(MonotypeRenderClient.BROWSER_VERSION)
    bad_payload = dict(unbound[0].payload)
    bad_payload.pop("md5")
    from acquisition.models import SpriteRasterPage

    unbound_page = SpriteRasterPage(
        page_index=1, glyph_count=2, raster_bytes=unbound[0].raster_bytes,
        next_cursor="2", final=False, payload=bad_payload,
    )
    store_u = ObservationStore(tmp_path / "obs_matrix_u")
    with pytest.raises(ValueError, match="RASTER_INGEST_REQUEST_BINDING_MISSING"):
        ingest_raster_pages(
            store_u, ISSUE71_CONFIG, "m_fam", "regular", supplement, [unbound_page], capability,
        )

    # Corrupt sprite evidence fails closed at slice validation.
    broken = _raster_pages_for_seed(MonotypeRenderClient.BROWSER_VERSION)
    bad_payload = dict(broken[0].payload)
    bad_payload["glyphs"] = [
        {"code_point": 65, "glyph_index": 0, "sprite_box": {"x": 500, "y": 0, "width": 59, "height": 80}},
        {"code_point": 66, "glyph_index": 1, "sprite_box": {"x": 59, "y": 0, "width": 55, "height": 80}},
    ]
    broken_page = SpriteRasterPage(
        page_index=1, glyph_count=2, raster_bytes=broken[0].raster_bytes,
        next_cursor="2", final=False, payload=bad_payload,
    )
    with pytest.raises(ValueError, match="RASTER_INGEST_BOX_OUT_OF_BOUNDS"):
        page_slice_attestation([broken_page])
    store_c = ObservationStore(tmp_path / "obs_matrix_c")
    with pytest.raises(ValueError, match="RASTER_INGEST_BOX_OUT_OF_BOUNDS"):
        ingest_raster_pages(
            store_c, ISSUE71_CONFIG, "m_fam", "regular", supplement, [broken_page], capability,
        )


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


# =========================================================================
# RASTER PRODUCTION HANDOFF (Issue #73 comment 5420825557)
# =========================================================================

_PW_HANDOFF_BOXES = {65: (0, 0, 50, 60), 66: (60, 0, 45, 60)}


def _playwright_shaped_page(md5: str, acs_pt: int, with_binding: bool = True, ink: bool = True) -> SpriteRasterPage:
    """One observable Playwright-stealth-shaped raster page (ink cells)."""
    import io
    from PIL import Image, ImageDraw

    img = Image.new("L", (500, 500), 255)
    draw = ImageDraw.Draw(img)
    glyphs = []
    for idx, cp in enumerate(sorted(_PW_HANDOFF_BOXES)):
        x, y, w, h = _PW_HANDOFF_BOXES[cp]
        if ink:
            draw.rectangle([x, y, x + w - 1, y + h - 1], fill=0)
        glyphs.append(
            {"code_point": cp, "glyph_index": idx + 1, "sprite_box": {"x": x, "y": y, "width": w, "height": h}}
        )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png_bytes = buf.getvalue()
    payload = {
        "browser_version": "playwright_stealth_v1",
        "glyphs": glyphs,
        "pairs": [],
        "features": [],
        "sprite_sha256": hashlib.sha256(png_bytes).hexdigest(),
        "md5": md5,
        "acs_pt": acs_pt,
        "provenance": "playwright_stealth_persistent",
    }
    if with_binding:
        payload["request_params"] = {
            "provider": "playwright_stealth_persistent",
            "style_id": "regular",
            "md5": md5,
            "acs_pt": str(acs_pt),
            "acs_p": "1",
        }
    return SpriteRasterPage(
        page_index=1,
        glyph_count=len(glyphs),
        raster_bytes=png_bytes,
        next_cursor="",
        final=True,
        payload=payload,
    )


def test_DUMP_OR_PLAYWRIGHT_RASTER_PRODUCTION_HANDOFF(tmp_path: Path):
    """DUMP_OR_PLAYWRIGHT_RASTER_PRODUCTION_HANDOFF: authorized Playwright-shaped
    result -> provider-typed capability -> page attestation -> ingest/finalization
    succeeds, bound to the actual producer (never relabeled Monotype)."""
    from acquisition.capability import (
        PROVIDER_MONOTYPE_RENDER,
        PROVIDER_PLAYWRIGHT_STEALTH,
        ProviderRasterCapability,
        resolve_raster_provider,
    )
    from acquisition.raster_ingest import ingest_raster_pages, page_slice_attestation
    from measurement.store import ObservationStore
    from tests.test_issue72_review_repros import RASTER_ONLY_CONFIG, _browser_supplement_for_seed

    capability = ProviderRasterCapability.deterministic_size_schedule(
        PROVIDER_PLAYWRIGHT_STEALTH, RASTER_ONLY_CONFIG.resolutions
    )
    pages = tuple(_playwright_shaped_page(ENVELOPE_MD5, pt) for pt in capability.all_sizes())

    # Provider identity is derived from the pages themselves.
    assert resolve_raster_provider(pages) == PROVIDER_PLAYWRIGHT_STEALTH
    assert resolve_raster_provider(pages) != PROVIDER_MONOTYPE_RENDER

    # Page attestation succeeds with the exact request binding.
    attestation = page_slice_attestation(pages)
    assert attestation["sprite_sha256"] and attestation["bindings"]

    supplement = _browser_supplement_for_seed("chromium_pw_handoff_v1", config=RASTER_ONLY_CONFIG)
    store = ObservationStore(tmp_path / "obs_pw_handoff")
    ingested = ingest_raster_pages(
        store,
        RASTER_ONLY_CONFIG,
        "pw_handoff_fam",
        "regular",
        supplement,
        pages,
        capability,
        source_url="https://www.myfonts.com/collections/pw-handoff-fam",
    )
    assert ingested == 2
    cfg_h = RASTER_ONLY_CONFIG.compute_hash()
    assert store.get_coverage("pw_handoff_fam", "regular", browser_version="chromium_pw_handoff_v1", config_hash=cfg_h) == [65, 66]
    # Finalization completes under the Playwright-bound capability.
    assert store.is_source_collection_completed(
        "pw_handoff_fam", "regular", config_hash=cfg_h, browser_version="chromium_pw_handoff_v1"
    )
    assert PROVIDER_PLAYWRIGHT_STEALTH in capability.to_json()
    assert PROVIDER_MONOTYPE_RENDER not in capability.to_json()


def test_RASTER_TRANSPARENT_OR_BINDING_MISSING(tmp_path: Path):
    """RASTER_TRANSPARENT_OR_BINDING_MISSING: blank (zero-ink) cells or missing
    request binding fail closed at the ingest boundary; never an authorized
    completion."""
    from acquisition.raster_ingest import page_slice_attestation

    # Zero-ink glyph cells carry no ink evidence (alpha>0 is never ink).
    blank_page = _playwright_shaped_page(ENVELOPE_MD5, 120, with_binding=True, ink=False)
    with pytest.raises(ValueError, match="RASTER_INGEST_CELL_NO_INK"):
        page_slice_attestation((blank_page,))

    # Ink cells without the exact request binding cannot be attested.
    unbound_page = _playwright_shaped_page(ENVELOPE_MD5, 120, with_binding=False, ink=True)
    with pytest.raises(ValueError, match="RASTER_INGEST_REQUEST_BINDING_MISSING"):
        page_slice_attestation((unbound_page,))


def _monotype_shaped_page(md5: str, acs_pt: int) -> SpriteRasterPage:
    """One Monotype-CDN-shaped raster page (ink cells, explicit provenance)."""
    import io
    from PIL import Image, ImageDraw

    img = Image.new("L", (500, 500), 255)
    draw = ImageDraw.Draw(img)
    glyphs = []
    for idx, cp in enumerate(sorted(_PW_HANDOFF_BOXES)):
        x, y, w, h = _PW_HANDOFF_BOXES[cp]
        draw.rectangle([x, y, x + w - 1, y + h - 1], fill=0)
        glyphs.append(
            {"code_point": cp, "glyph_index": idx + 1, "sprite_box": {"x": x, "y": y, "width": w, "height": h}}
        )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png_bytes = buf.getvalue()
    return SpriteRasterPage(
        page_index=1,
        glyph_count=len(glyphs),
        raster_bytes=png_bytes,
        next_cursor="",
        final=True,
        payload={
            "browser_version": "monotype_render_105",
            "glyphs": glyphs,
            "pairs": [],
            "features": [],
            "sprite_sha256": hashlib.sha256(png_bytes).hexdigest(),
            "md5": md5,
            "acs_pt": acs_pt,
            "provenance": "monotype_render_105",
            "request_params": {
                "provider": "monotype_render_105",
                "md5": md5,
                "acs_pt": str(acs_pt),
                "acs_p": "1",
            },
        },
    )


def test_RASTER_PROVIDER_UNKNOWN_OR_MIXED_REJECTED():
    """RASTER_PROVIDER_UNKNOWN_OR_MIXED_REJECTED: unknown/absent/mixed page
    provenance and arbitrary capability providers fail closed (no default)."""
    from acquisition.capability import FIXED_PHASE, ProviderRasterCapability, resolve_raster_provider
    from acquisition.raster_ingest import page_slice_attestation

    unknown = _playwright_shaped_page(ENVELOPE_MD5, 120)
    unknown.payload["provenance"] = "some_unknown_renderer"
    with pytest.raises(ValueError, match="RASTER_PROVIDER_UNKNOWN_OR_ABSENT"):
        resolve_raster_provider((unknown,))
    with pytest.raises(ValueError, match="RASTER_PROVIDER_UNKNOWN_OR_ABSENT"):
        page_slice_attestation((unknown,))

    absent = _playwright_shaped_page(ENVELOPE_MD5, 120)
    del absent.payload["provenance"]
    with pytest.raises(ValueError, match="RASTER_PROVIDER_UNKNOWN_OR_ABSENT"):
        resolve_raster_provider((absent,))

    mixed = (_playwright_shaped_page(ENVELOPE_MD5, 120), _monotype_shaped_page(ENVELOPE_MD5, 120))
    with pytest.raises(ValueError, match="RASTER_PROVIDER_MIXED"):
        resolve_raster_provider(mixed)

    # Arbitrary capability provider strings are never admitted.
    with pytest.raises(ValueError, match="CAPABILITY_FORGED"):
        ProviderRasterCapability(
            provider="arbitrary_renderer", phase=FIXED_PHASE, fit_sizes=(120,), held_out_sizes=(240,)
        ).validate()


def test_RASTER_CAPABILITY_PAGE_PROVIDER_MISMATCH_REJECTED(tmp_path: Path):
    """RASTER_CAPABILITY_PAGE_PROVIDER_MISMATCH_REJECTED: pages and capability
    bound to different producers fail closed before persistence."""
    from acquisition.capability import PROVIDER_MONOTYPE_RENDER, ProviderRasterCapability
    from acquisition.raster_ingest import ingest_raster_pages
    from measurement.store import ObservationStore
    from tests.test_issue72_review_repros import RASTER_ONLY_CONFIG, _browser_supplement_for_seed

    pages = (_playwright_shaped_page(ENVELOPE_MD5, 120),)
    supplement = _browser_supplement_for_seed("chromium_mismatch_v1", config=RASTER_ONLY_CONFIG)
    wrong_capability = ProviderRasterCapability.deterministic_size_schedule(
        PROVIDER_MONOTYPE_RENDER, (120, 240)
    )
    store = ObservationStore(tmp_path / "obs_mismatch")
    with pytest.raises(ValueError, match="RASTER_PROVIDER_PAGE_CAPABILITY_MISMATCH"):
        ingest_raster_pages(
            store, RASTER_ONLY_CONFIG, "mismatch_fam", "regular",
            supplement, pages, wrong_capability,
            source_url="https://www.myfonts.com/collections/mismatch-fam",
        )


def test_RASTER_REQUEST_BINDING_VALUE_MISMATCH_REJECTED():
    """RASTER_REQUEST_BINDING_VALUE_MISMATCH_REJECTED: forged provider/MD5/
    render-size/page-index binding values fail closed (presence-only is
    insufficient); a forged style identity is never authorized by the
    pipeline."""
    import asyncio

    from acquisition.capability import PROVIDER_MONOTYPE_RENDER
    from acquisition.models import FamilyDiscoveryEnvelope, StyleDiscoveryRecord
    from acquisition.pipeline import AcquisitionPipeline
    from acquisition.providers import DumpDomTransport, MonotypeRasterProvider
    from acquisition.raster_ingest import page_slice_attestation
    from unittest.mock import AsyncMock, MagicMock

    def _variant(mutate):
        page = _playwright_shaped_page(ENVELOPE_MD5, 120)
        mutate(page)
        return page

    def forge_provider(page):
        page.payload["request_params"]["provider"] = PROVIDER_MONOTYPE_RENDER

    def forge_md5(page):
        page.payload["request_params"]["md5"] = "b" * 32

    def forge_size(page):
        page.payload["request_params"]["acs_pt"] = "240"

    def forge_page_index(page):
        page.payload["request_params"]["acs_p"] = "2"

    with pytest.raises(ValueError, match="RASTER_INGEST_REQUEST_BINDING_PROVIDER_MISMATCH"):
        page_slice_attestation((_variant(forge_provider),))
    with pytest.raises(ValueError, match="RASTER_INGEST_REQUEST_BINDING_MD5_MISMATCH"):
        page_slice_attestation((_variant(forge_md5),))
    with pytest.raises(ValueError, match="RASTER_INGEST_REQUEST_BINDING_SIZE"):
        page_slice_attestation((_variant(forge_size),))
    with pytest.raises(ValueError, match="RASTER_INGEST_REQUEST_BINDING_PAGE"):
        page_slice_attestation((_variant(forge_page_index),))

    # Style identity variant at the acquisition boundary: a page whose exposed
    # style binding does not match the target is never authorized; the lane
    # fails closed and continues.
    forged_style_page = _variant(lambda page: page.payload["request_params"].update({"style_id": "other"}))

    dump_transport = MagicMock(spec=DumpDomTransport)
    dump_transport.dump_dom = AsyncMock(return_value="")
    playwright = MagicMock()
    playwright.available.return_value = True
    playwright.capture_raster_pages = AsyncMock(return_value=(forged_style_page,))
    playwright.capture_binary = AsyncMock(return_value=None)
    algolia = MagicMock()
    algolia.available.return_value = True
    algolia.discover_family = AsyncMock(return_value=None)
    client = MagicMock()
    client.fetch_all_sprite_pages = AsyncMock(return_value=())
    raster_provider = MonotypeRasterProvider(client=client)
    env_md5 = FamilyDiscoveryEnvelope(
        family_name="Style Forge Fam",
        styles={"regular": StyleDiscoveryRecord(style_id="regular", style_name="Regular", md5=ENVELOPE_MD5, provenance="dump_dom_native")},
        provenance="dump_dom_native",
    )
    pipeline = AcquisitionPipeline(
        dump_dom_transport=dump_transport,
        playwright_provider=playwright,
        algolia_provider=algolia,
        raster_provider=raster_provider,
    )
    outcome = asyncio.run(pipeline.acquire(
        source_url="https://www.myfonts.com/collections/style-forge-fam",
        expected_family="Style Forge Fam",
        expected_style="Regular",
        family_envelope=env_md5,
    ))
    assert outcome.kind == "insufficient"
    assert any(
        r.reason_code == "STEALTH_RASTER_REQUEST_BINDING_MISSING" for r in outcome.trace.records
    )


def _handoff_direct_metrics(cp: int, config, adv: float, font_size_px: float | None = None):
    from measurement.models import DirectMetrics

    # font_size_px override carries the exact requested metric size for
    # sealed metric-schedule rows; the config anchor size is the default.
    size_px = float(font_size_px) if font_size_px is not None else float(config.font_size_px)
    scale = size_px / float(config.upem)
    return DirectMetrics(
        code_point=cp,
        character=chr(cp),
        font_size_px=size_px,
        raw_advance_width=adv * scale,
        raw_actual_left=50.0 * scale,
        raw_actual_right=550.0 * scale,
        raw_actual_ascent=700.0 * scale,
        raw_actual_descent=200.0 * scale,
        raw_font_ascent=700.0 * scale,
        raw_font_descent=200.0 * scale,
        advance_width_upem=adv,
        lsb_upem=50.0,
        rsb_upem=adv - 550.0,
        ascent_upem=700.0,
        descent_upem=-200.0,
        bbox_width_upem=500.0,
        bbox_height_upem=650.0,
    )


def test_RASTER_ZERO_INK_SEMANTICS(tmp_path: Path):
    """RASTER_ZERO_INK_SEMANTICS: ordinary printable blank and observable
    tofu/missing evidence reject; an independently proven space-like zero-ink
    glyph passes (bound zero-area cell, never an alpha-only rule)."""
    import io
    from PIL import Image, ImageDraw

    from acquisition.capability import PROVIDER_PLAYWRIGHT_STEALTH, ProviderRasterCapability
    from acquisition.raster_ingest import (
        BrowserSupplementalEvidence,
        ingest_raster_pages,
        page_slice_attestation,
    )
    from measurement.store import ObservationStore
    from tests.test_issue72_review_repros import RASTER_ONLY_CONFIG

    # 1. Ordinary printable blank (zero ink) rejects.
    blank = _playwright_shaped_page(ENVELOPE_MD5, 120, ink=False)
    with pytest.raises(ValueError, match="RASTER_INGEST_CELL_NO_INK"):
        page_slice_attestation((blank,))

    # 2. Opaque-white RGBA blank: alpha>0 everywhere yet zero ink -> rejects.
    img = Image.new("RGBA", (500, 500), (255, 255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png_bytes = buf.getvalue()
    opaque_blank = SpriteRasterPage(
        page_index=1,
        glyph_count=1,
        raster_bytes=png_bytes,
        next_cursor="",
        final=True,
        payload={
            "browser_version": "playwright_stealth_v1",
            "glyphs": [
                {"code_point": 65, "glyph_index": 1, "sprite_box": {"x": 0, "y": 0, "width": 50, "height": 60}},
            ],
            "sprite_sha256": hashlib.sha256(png_bytes).hexdigest(),
            "md5": ENVELOPE_MD5,
            "acs_pt": 120,
            "provenance": "playwright_stealth_persistent",
            "request_params": {
                "provider": "playwright_stealth_persistent",
                "md5": ENVELOPE_MD5,
                "acs_pt": "120",
                "acs_p": "1",
            },
        },
    )
    with pytest.raises(ValueError, match="RASTER_INGEST_CELL_NO_INK"):
        page_slice_attestation((opaque_blank,))

    # 3. Observable tofu and producer-flagged missing glyphs reject.
    tofu = _playwright_shaped_page(ENVELOPE_MD5, 120)
    tofu.payload["observed_headers"] = {"x_tofus_found": "1"}
    missing_flag = _playwright_shaped_page(ENVELOPE_MD5, 120)
    missing_flag.payload["observed_headers"] = {"x_missing_unicodes": "U+0041"}

    supplement66 = BrowserSupplementalEvidence(
        browser_version="chromium_zero_ink_v1",
        metrics={66: _handoff_direct_metrics(66, RASTER_ONLY_CONFIG, 600.0)},
        pairs=(),
        features=[
            {
                "feature_tag": tag,
                "sample_text": text,
                "enabled_advance_upem": 1200.0,
                "disabled_advance_upem": 1200.0,
                "enabled_raster_signature": "a",
                "disabled_raster_signature": "a",
            }
            for tag, text in RASTER_ONLY_CONFIG.feature_probes
        ],
    )
    capability = ProviderRasterCapability.deterministic_size_schedule(
        PROVIDER_PLAYWRIGHT_STEALTH, (120, 240)
    )
    store = ObservationStore(tmp_path / "obs_zero_ink")
    with pytest.raises(ValueError, match="RASTER_INGEST_TOFU_EVIDENCE"):
        ingest_raster_pages(
            store, RASTER_ONLY_CONFIG, "zero_fam", "regular", supplement66,
            (tofu,), capability, source_url="https://www.myfonts.com/collections/zero-fam",
        )
    with pytest.raises(ValueError, match="RASTER_INGEST_MISSING_UNICODE_CP_65"):
        ingest_raster_pages(
            store, RASTER_ONLY_CONFIG, "zero_fam", "regular", supplement66,
            (missing_flag,), capability, source_url="https://www.myfonts.com/collections/zero-fam",
        )

    # 4. Independently proven space-like zero-ink glyph passes (bound
    # zero-area cell with measured advance; no blanket allowlist).
    def _space_page(pt: int) -> SpriteRasterPage:
        img2 = Image.new("L", (500, 500), 255)
        draw = ImageDraw.Draw(img2)
        space_glyphs = []
        for idx, cp in enumerate((32, 65)):
            if cp == 32:
                space_glyphs.append(
                    {"code_point": 32, "glyph_index": idx + 1, "is_space": True,
                     "sprite_box": {"x": 0, "y": 0, "width": 0, "height": 0}}
                )
            else:
                x, y, w, h = _PW_HANDOFF_BOXES[cp]
                draw.rectangle([x, y, x + w - 1, y + h - 1], fill=0)
                space_glyphs.append(
                    {"code_point": cp, "glyph_index": idx + 1,
                     "sprite_box": {"x": x, "y": y, "width": w, "height": h}}
                )
        buf2 = io.BytesIO()
        img2.save(buf2, format="PNG")
        space_png = buf2.getvalue()
        return SpriteRasterPage(
            page_index=1,
            glyph_count=2,
            raster_bytes=space_png,
            next_cursor="",
            final=True,
            payload={
                "browser_version": "playwright_stealth_v1",
                "glyphs": space_glyphs,
                "sprite_sha256": hashlib.sha256(space_png).hexdigest(),
                "md5": ENVELOPE_MD5,
                "acs_pt": pt,
                "provenance": "playwright_stealth_persistent",
                "request_params": {
                    "provider": "playwright_stealth_persistent",
                    "md5": ENVELOPE_MD5,
                    "acs_pt": str(pt),
                    "acs_p": "1",
                },
            },
        )

    space_pages = tuple(_space_page(pt) for pt in capability.all_sizes())
    supplement_space = BrowserSupplementalEvidence(
        browser_version="chromium_zero_ink_v1",
        metrics={
            32: _handoff_direct_metrics(32, RASTER_ONLY_CONFIG, 250.0),
            65: _handoff_direct_metrics(65, RASTER_ONLY_CONFIG, 650.0),
        },
        pairs=(),
        features=supplement66.features,
        # Sealed raw per-size metric evidence across the closed metric
        # schedule for every coverage code point (finalization fails closed).
        metric_schedule={
            cp: {
                float(size): _handoff_direct_metrics(
                    cp, RASTER_ONLY_CONFIG, adv, font_size_px=float(size)
                )
                for size in RASTER_ONLY_CONFIG.metric_sizes_px
            }
            for cp, adv in ((32, 250.0), (65, 650.0))
        },
    )
    attestation = page_slice_attestation(space_pages)
    assert attestation["bindings"]["32"]["zero_ink_proven"] is True
    ingested = ingest_raster_pages(
        store, RASTER_ONLY_CONFIG, "zero_fam", "regular", supplement_space,
        space_pages, capability, source_url="https://www.myfonts.com/collections/zero-fam",
    )
    assert ingested == 2


def test_CDN_AND_PLAYWRIGHT_TYPED_HANDOFF(tmp_path: Path):
    """CDN_AND_PLAYWRIGHT_TYPED_HANDOFF: each exact provider passes its own
    complete handoff under its own typed capability; no default/relabel path."""
    import asyncio

    from acquisition.adapters import MonotypeRenderClient
    from acquisition.capability import (
        PROVIDER_MONOTYPE_RENDER,
        PROVIDER_PLAYWRIGHT_STEALTH,
        ProviderRasterCapability,
        resolve_raster_provider,
    )
    from acquisition.models import BinaryAcquisitionPolicy
    from acquisition.providers import MonotypeRasterProvider
    from acquisition.raster_ingest import ingest_raster_pages, page_slice_attestation
    from measurement.store import ObservationStore
    from tests.test_issue72_review_repros import (
        RASTER_ONLY_CONFIG,
        _browser_supplement_for_seed,
    )

    # Playwright lane: provider identity stays Playwright end to end.
    pw_pages = tuple(
        _playwright_shaped_page(ENVELOPE_MD5, pt)
        for pt in ProviderRasterCapability.deterministic_size_schedule(
            PROVIDER_PLAYWRIGHT_STEALTH, RASTER_ONLY_CONFIG.resolutions
        ).all_sizes()
    )
    pw_capability = ProviderRasterCapability.deterministic_size_schedule(
        PROVIDER_PLAYWRIGHT_STEALTH, RASTER_ONLY_CONFIG.resolutions
    )
    pw_attestation = page_slice_attestation(pw_pages)
    assert pw_attestation["provider"] == PROVIDER_PLAYWRIGHT_STEALTH
    pw_supplement = _browser_supplement_for_seed("chromium_typed_pw_v1", config=RASTER_ONLY_CONFIG)
    pw_store = ObservationStore(tmp_path / "obs_typed_pw")
    assert ingest_raster_pages(
        pw_store, RASTER_ONLY_CONFIG, "typed_pw_fam", "regular",
        pw_supplement, pw_pages, pw_capability,
        source_url="https://www.myfonts.com/collections/typed-pw-fam",
    ) == 2

    # CDN lane: real production client over the captured response shape.
    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        page_no = int(params["acs_p"])
        if page_no == 1:
            return httpx.Response(200, json=_captured_shape_response([65, 66]), headers=CAPTURED_HEADERS)
        return httpx.Response(200, json=_captured_shape_response([]), headers=CAPTURED_HEADERS)

    async def cdn_run():
        client = MonotypeRenderClient()
        provider = MonotypeRasterProvider(_TransportBoundClient(client, httpx.MockTransport(handler)))
        cdn_capability = ProviderRasterCapability.deterministic_size_schedule(
            PROVIDER_MONOTYPE_RENDER, RASTER_ONLY_CONFIG.resolutions
        )
        pages = await provider.fetch_sprite_pages(
            {"family": "Typed CDN Fam", "style": "Regular", "md5": ENVELOPE_MD5,
             "acs_pts": list(cdn_capability.all_sizes())},
            BinaryAcquisitionPolicy(max_sprite_pages=8),
        )
        assert pages
        assert resolve_raster_provider(pages) == PROVIDER_MONOTYPE_RENDER
        attestation = page_slice_attestation(pages)
        assert attestation["provider"] == PROVIDER_MONOTYPE_RENDER
        supplement = _browser_supplement_for_seed("chromium_typed_cdn_v1", config=RASTER_ONLY_CONFIG)
        store = ObservationStore(tmp_path / "obs_typed_cdn")
        ingested = ingest_raster_pages(
            store, RASTER_ONLY_CONFIG, "typed_cdn_fam", "regular",
            supplement, pages, cdn_capability,
            source_url="https://www.myfonts.com/collections/typed-cdn-fam",
        )
        assert ingested == 2

    asyncio.run(cdn_run())
