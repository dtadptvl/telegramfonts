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
    import io

    from PIL import Image, ImageDraw

    if sprite_bytes is None:
        img = Image.new("RGB", (60 * max(len(cps), 1) + 10, 100), "white")
        draw = ImageDraw.Draw(img)
        for i, _cp in enumerate(cps):
            draw.rectangle([60 * i + 6, 10, 60 * i + 54, 90], fill="black")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        sprite_bytes = buf.getvalue()
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


def test_RASTER_FALLBACK_E2E_observations_from_browser_measurement(tmp_path: Path):
    """End-to-end fallback-to-observation under the captured contract.

    The production client consumes the real captured response shape; the
    raster-only pages bind coverage/code points; metrics/pairs/features and
    the raster schedule come from approved browser-measurement evidence.
    Raster-only evidence alone can never complete the snapshot.
    """
    from acquisition.raster_ingest import BrowserMeasurementEvidence, ingest_raster_pages
    from measurement.store import ObservationStore
    from tests.test_issue72_review_repros import _browser_measurement_for_seed

    target = {"family": "E2E Fam", "style": "Regular", "md5": ENVELOPE_MD5}
    evidence = _browser_measurement_for_seed("chromium_e2e_v1")

    async def run():
        client = MonotypeRenderClient()
        page = await _fetch_with_transport(
            client,
            httpx.MockTransport(lambda r: httpx.Response(
                200, json=_captured_shape_response([65, 66]), headers=CAPTURED_HEADERS)),
            target,
        )
        assert page is not None

        store = ObservationStore(tmp_path / "obs_e2e")
        ingested = ingest_raster_pages(
            store, ISSUE71_CONFIG, "e2e_fam", "regular",
            evidence, [page], source_url="https://www.myfonts.com/collections/e2e-fam",
        )
        assert ingested == 2
        cfg_h = ISSUE71_CONFIG.compute_hash()
        assert store.get_coverage("e2e_fam", "regular", browser_version="chromium_e2e_v1", config_hash=cfg_h) == [65, 66]
        assert store.is_source_collection_completed(
            "e2e_fam", "regular", config_hash=cfg_h, browser_version="chromium_e2e_v1",
        )
        # Stored rasters/metrics are exactly the browser-measured evidence.
        obs = store.get_glyph_observations("e2e_fam", "regular", 66, browser_version="chromium_e2e_v1", config_hash=cfg_h)
        assert obs and obs[0][0].metrics.advance_width_upem == 600.0
        stored = (store.base_dir / obs[0][0].raster_relative_path).read_bytes()
        assert stored == evidence.rasters[66][obs[0][0].resolution, obs[0][0].subpixel_x, obs[0][0].subpixel_y]
        # Pair/feature provenance is the approved chromium canvas path.
        pairs = store.get_pair_observations("e2e_fam", "regular", browser_version="chromium_e2e_v1", config_hash=cfg_h)
        assert pairs and all(p["provenance"] == "chromium:chromium_e2e_v1:canvas_text_metrics" for p in pairs)
        feats = store.get_feature_observations("e2e_fam", "regular", browser_version="chromium_e2e_v1", config_hash=cfg_h)
        assert feats and all(f["provenance"] == "chromium:chromium_e2e_v1:canvas_feature_probe" for f in feats)

    import asyncio

    asyncio.run(run())


def test_RASTER_FALLBACK_fail_closed_matrix(tmp_path: Path):
    """Raster-only pages never satisfy the snapshot without browser evidence."""
    from acquisition.raster_ingest import (
        BrowserMeasurementEvidence,
        ingest_raster_pages,
        page_slice_attestation,
    )
    from measurement.models import DirectMetrics
    from measurement.store import ObservationStore
    from tests.test_issue72_review_repros import (
        _browser_measurement_for_seed,
        _raster_pages_for_seed,
    )

    pages = _raster_pages_for_seed(MonotypeRenderClient.BROWSER_VERSION)
    evidence = _browser_measurement_for_seed("chromium_matrix_v1")
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

    # Raster pages without browser measurement can never complete.
    store = ObservationStore(tmp_path / "obs_matrix_a")
    with pytest.raises(ValueError, match="RASTER_INGEST_BROWSER_MEASUREMENT_REQUIRED"):
        ingest_raster_pages(store, ISSUE71_CONFIG, "m_fam", "regular", None, pages)
    assert store.is_source_collection_completed(
        "m_fam", "regular", config_hash=cfg_h, browser_version="chromium_matrix_v1"
    ) is False

    # Coverage drift between CDN binding and browser measurement fails closed.
    drifted = BrowserMeasurementEvidence(
        browser_version="chromium_matrix_v1",
        metrics={65: evidence.metrics[65]},
        rasters={65: evidence.rasters[65]},
        pairs=evidence.pairs,
        features=evidence.features,
    )
    store_b = ObservationStore(tmp_path / "obs_matrix_b")
    with pytest.raises(ValueError, match="RASTER_INGEST_COVERAGE_DRIFT"):
        ingest_raster_pages(store_b, ISSUE71_CONFIG, "m_fam", "regular", drifted, pages)

    # Corrupt sprite evidence fails closed at slice validation.
    broken = _raster_pages_for_seed(MonotypeRenderClient.BROWSER_VERSION)
    bad_payload = dict(broken[0].payload)
    bad_payload["glyphs"] = [
        {"code_point": 65, "glyph_index": 0, "sprite_box": {"x": 500, "y": 0, "width": 59, "height": 80}},
        {"code_point": 66, "glyph_index": 1, "sprite_box": {"x": 59, "y": 0, "width": 55, "height": 80}},
    ]
    from acquisition.models import SpriteRasterPage

    broken_page = SpriteRasterPage(
        page_index=1, glyph_count=2, raster_bytes=broken[0].raster_bytes,
        next_cursor="2", final=False, payload=bad_payload,
    )
    with pytest.raises(ValueError, match="RASTER_INGEST_BOX_OUT_OF_BOUNDS"):
        page_slice_attestation([broken_page])
    store_c = ObservationStore(tmp_path / "obs_matrix_c")
    with pytest.raises(ValueError, match="RASTER_INGEST_BOX_OUT_OF_BOUNDS"):
        ingest_raster_pages(store_c, ISSUE71_CONFIG, "m_fam", "regular", evidence, [broken_page])

    # Non-PNG browser raster evidence fails closed.
    bad_rasters = {
        cp: {key: b"not-a-png" for key in tup}
        for cp, tup in evidence.rasters.items()
    }
    poisoned = BrowserMeasurementEvidence(
        browser_version="chromium_matrix_v1",
        metrics=evidence.metrics,
        rasters=bad_rasters,
        pairs=evidence.pairs,
        features=evidence.features,
    )
    store_d = ObservationStore(tmp_path / "obs_matrix_d")
    with pytest.raises(ValueError, match="RASTER_INGEST_BROWSER_RASTER_NOT_PNG"):
        ingest_raster_pages(store_d, ISSUE71_CONFIG, "m_fam", "regular", poisoned, pages)


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
