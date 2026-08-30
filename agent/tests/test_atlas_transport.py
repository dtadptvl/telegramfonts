"""R1 focused causal repros: production atlas transport chain (hermetic).

Proves, with injected fake transports (no network, no browser process):
- provider ordering: exact cache and authorized binary win BEFORE any
  network transport is touched;
- CDN exact-MD5 verification: mismatched/absent MD5 binding fails closed
  and falls back to the browser canvas atlas;
- batched metrics call counts (never per-glyph);
- ONE persistent browser session, started lazily, kept for the run;
- Algolia resolves a missing MD5 and the chain returns to the CDN;
- the default production atlas factory wiring (composition/runner).
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import time
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from acquisition.models import (
    BINARY_STAGE_DUMP_DOM,
    FamilyDiscoveryEnvelope,
    SpriteRasterPage,
    StyleDiscoveryRecord,
)
from atlas.cache import AtlasCacheStore, identity_hash, NAMESPACE_FONTS, NAMESPACE_REPORTS
from atlas.metrics import build_metrics_batches, metrics_js_call_count
from atlas.policy import AtlasRuntimeDefaults, policy_identity_hash
from atlas.transport import (
    AlgoliaMd5Resolver,
    AtlasTransportCounters,
    MonotypeCdnRasterSource,
    ProductionAtlasPipeline,
    ProductionMetricsProvider,
    SharedBrowserSession,
    PersistentBrowserAtlasSession,
    build_default_atlas_pipeline_factory,
    cdn_acs_pt_for,
)
from compute.binary_cache import AuthorizedBinaryCache, BinaryCacheIdentity

MD5_OK = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
MD5_OTHER = "11223344556677889900aabbccddeeff"
SOURCE_URL = "https://www.myfonts.com/collections/fake-family"


# ----------------------------------------------------------------------
# Deterministic fake metrics: identical per-glyph ink semantics at any size
# advance=200, lsb=20, ink_right=120, ink_ascent=110, ink_descent=10,
# font_ascent=800, font_descent=200 (UPEM=1000).
# ----------------------------------------------------------------------

def _metric_row(size_px: float) -> list[float]:
    k = float(size_px) / 1000.0
    return [200.0 * k, 20.0 * k, 120.0 * k, 110.0 * k, 10.0 * k, 800.0 * k, 200.0 * k]


# ----------------------------------------------------------------------
# Fake dump-dom provider (duck-typed)
# ----------------------------------------------------------------------

class FakeDumpDom:
    def __init__(self, envelope=None, binary=None):
        self.envelope = envelope
        self.binary = binary
        self.envelope_calls: list[str] = []
        self.binary_calls: list[str] = []

    async def family_envelope(self, source_url, expected_family=""):
        self.envelope_calls.append(source_url)
        return self.envelope

    async def authorized_binary(self, source_url, envelope, family, style):
        self.binary_calls.append(source_url)
        return self.binary


class PoisonedDumpDom:
    async def family_envelope(self, source_url, expected_family=""):
        raise AssertionError("dump-dom touched after cache/binary win")

    async def authorized_binary(self, *a, **k):
        raise AssertionError("dump-dom touched after cache/binary win")


# ----------------------------------------------------------------------
# Fake CDN client (MonotypeRenderClient duck-type)
# ----------------------------------------------------------------------

class FakeCDNClient:
    def __init__(self, pages_by_md5: dict):
        self.pages_by_md5 = pages_by_md5
        self.calls: list[dict] = []

    async def fetch_all_sprite_pages(self, request, policy):
        self.calls.append({"md5": request.get("md5"), "acs_pt": request.get("acs_pt")})
        return self.pages_by_md5.get(request.get("md5"))

    async def fetch_sprite_page(self, request, cursor):
        self.calls.append({"md5": request.get("md5"), "acs_pt": request.get("acs_pt"), "single": True})
        pages = self.pages_by_md5.get(request.get("md5"))
        return pages[0] if pages else None


class PoisonedCDNClient:
    async def fetch_all_sprite_pages(self, request, policy):
        raise AssertionError("CDN touched before cache/binary win")

    async def fetch_sprite_page(self, request, cursor):
        raise AssertionError("CDN touched before cache/binary win")


def make_sprite_page(md5: str, acs_pt: int, boxes: dict[int, tuple[int, int, int, int]]) -> SpriteRasterPage:
    """Real PNG sprite with real ink at the declared boxes; MD5-bound payload."""
    w = max(x + bw for (x, y, bw, bh) in boxes.values()) + 40
    h = max(y + bh for (x, y, bw, bh) in boxes.values()) + 40
    img = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(img)
    for (x, y, bw, bh) in boxes.values():
        draw.rectangle([x, y, x + bw - 1, y + bh - 1], fill=255)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png = buf.getvalue()
    glyphs = [
        {"code_point": cp, "glyph_index": i + 1, "sprite_box": {"x": b[0], "y": b[1], "width": b[2], "height": b[3]}}
        for i, (cp, b) in enumerate(sorted(boxes.items()))
    ]
    return SpriteRasterPage(
        page_index=1,
        glyph_count=len(glyphs),
        raster_bytes=png,
        next_cursor="",
        final=True,
        payload={
            "browser_version": "monotype_render_105",
            "glyphs": glyphs,
            "pairs": [],
            "features": [],
            "sprite_sha256": hashlib.sha256(png).hexdigest(),
            "md5": md5,
            "acs_pt": acs_pt,
            "request_params": {
                "provider": "monotype_authorized_raster",
                "md5": md5,
                "acs_pt": str(acs_pt),
                "acs_p": "1",
            },
            "provenance": "monotype_authorized_raster",
        },
    )


def make_envelope(md5: str = MD5_OK) -> FamilyDiscoveryEnvelope:
    return FamilyDiscoveryEnvelope(
        family_name="Fake Family",
        family_url=SOURCE_URL,
        styles={
            "regular": StyleDiscoveryRecord(
                style_id="regular", style_name="Fake Regular", md5=md5
            )
        },
        provenance="dump_dom_native",
    )


# ----------------------------------------------------------------------
# Fake persistent browser stack (Playwright duck-type)
# ----------------------------------------------------------------------

class FakeFontResponse:
    def __init__(self, url: str, resource_type: str = "font"):
        self.url = url
        self.status = 200

        class _Req:
            pass

        self.request = _Req()
        self.request.resource_type = resource_type


class FakePage:
    def __init__(self, state: dict):
        self.state = state

    def on(self, event: str, handler):
        if event == "response":
            self.state["response_handlers"].append(handler)

    async def goto(self, url, timeout=None, wait_until=None):
        self.state["navigations"].append(url)
        md5 = self.state.get("expected_md5", MD5_OK)
        for handler in self.state["response_handlers"]:
            await handler(FakeFontResponse(f"https://cdn.example/fonts/{md5}.woff2"))

    async def evaluate(self, expression, arg=None):
        self.state["evals"] += 1
        state = self.state
        if arg is not None and isinstance(arg, dict) and "style" in arg and "weight" in arg:
            # FACE_RESOLVE_JS
            return [
                {"family": "Fake Family", "style": "normal", "weight": "400", "status": "loaded"},
            ]
        if arg is not None and isinstance(arg, dict) and "cells" in arg:
            # CELL_PAGE_JS: draw deterministic ink inside every cell slot.
            page_w = int(arg["page_w"])
            page_h = int(arg["page_h"])
            img = Image.new("L", (max(1, page_w), max(1, page_h)), 0)
            draw = ImageDraw.Draw(img)
            y0 = 0
            for cell in arg["cells"]:
                h = int(cell["h"])
                w = int(cell["w"])
                baseline = int(cell["baseline_y"])
                pen = int(cell["pen_left"])
                draw.rectangle(
                    [pen + 10, baseline - 80, min(page_w - 1, pen + 110), min(page_h - 1, baseline + 10)],
                    fill=255,
                )
                y0 += h
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
        if isinstance(expression, str) and "styleSheets" in expression:
            # COVERAGE_SCAN_JS
            return state.get("unicode_ranges", ["U+0041-0042"])
        if isinstance(expression, str) and "actualBoundingBoxLeft" in expression:
            # batched measureText protocol rows
            m = re.search(r"\)\((\[.*?\]),\s*(\d+),", expression, re.DOTALL)
            assert m, f"cannot parse metrics batch expression: {expression[:120]}"
            chars = json.loads(m.group(1))
            size = float(m.group(2))
            return [_metric_row(size) for _ in chars]
        if isinstance(expression, str) and "measureText" in expression:
            m = re.search(r"\)\((\[.*?\]),\s*(\d+),", expression, re.DOTALL)
            assert m, f"cannot parse pair expression: {expression[:120]}"
            texts = json.loads(m.group(1))
            size = float(m.group(2))
            return [400.0 * size / 1000.0 for _ in texts]
        raise AssertionError(f"unexpected evaluate: {str(expression)[:100]}")


class FakeContext:
    def __init__(self, state: dict):
        self.state = state

    async def new_page(self):
        return FakePage(self.state)

    async def add_init_script(self, script):
        pass

    async def close(self):
        self.state["closes"] += 1


def make_launcher(state: dict):
    async def launcher(**kwargs):
        state["starts"] += 1
        state["launch_kwargs"].append(kwargs)
        return FakeContext(state)

    return launcher


def make_browser_state(expected_md5: str = MD5_OK) -> dict:
    return {
        "expected_md5": expected_md5,
        "starts": 0,
        "closes": 0,
        "evals": 0,
        "navigations": [],
        "response_handlers": [],
        "launch_kwargs": [],
        "unicode_ranges": ["U+0041-0042"],
    }


# ----------------------------------------------------------------------
# Pipeline assembly helper (all transports injected; zero real network)
# ----------------------------------------------------------------------

def make_pipeline(
    tmp_path: Path,
    *,
    cdn_pages_by_md5: dict | None,
    envelope=None,
    algolia_client=None,
    state: dict | None = None,
    counters: AtlasTransportCounters | None = None,
    dump_dom=None,
    mode: str = "ORIGINAL",
    binary_cache=None,
) -> tuple[ProductionAtlasPipeline, AtlasTransportCounters, dict]:
    counters = counters or AtlasTransportCounters()
    state = state if state is not None else make_browser_state()
    if cdn_pages_by_md5 is None:
        cdn = MonotypeCdnRasterSource(PoisonedCDNClient(), counters=counters)
    else:
        cdn = MonotypeCdnRasterSource(FakeCDNClient(cdn_pages_by_md5), counters=counters)
    pipeline = ProductionAtlasPipeline(
        job_id="job-r1",
        mode=mode,
        source_url=SOURCE_URL,
        family_name="Fake Family",
        style_id="regular",
        style_name="Fake Regular",
        build_dir=tmp_path / "build",
        deadline=time.monotonic() + 240,
        cache_root=tmp_path / "cache",
        checkpoint_root=tmp_path / "ckpt",
        binary_cache=binary_cache,
        dump_dom_provider=dump_dom if dump_dom is not None else FakeDumpDom(envelope),
        cdn_source=cdn,
        algolia_resolver=AlgoliaMd5Resolver(algolia_client, counters=counters),
        runtime=AtlasRuntimeDefaults(),
        playwright_launcher=make_launcher(state),
        counters=counters,
    )
    return pipeline, counters, state


def _small_ttf_bytes(tmp_path: Path) -> bytes:
    """Build a real minimal TTF (3 glyphs) for cache/binary win repros."""
    from atlas.fontbuild import AtlasFontBuilder, assemble_font_model
    from reconstruction.font_model import CalibratedGlyph
    from reconstruction.models import Contour, LineSegment, Point2D

    def square(x0, y0, x1, y1):
        pts = [Point2D(x0, y0), Point2D(x1, y0), Point2D(x1, y1), Point2D(x0, y1)]
        return Contour(
            segments=[LineSegment(pts[i], pts[(i + 1) % 4]) for i in range(4)],
            is_hole=False,
            area_upem=(x1 - x0) * (y1 - y0),
        )

    glyphs = {
        0x41: CalibratedGlyph(
            code_point=0x41, character="A", advance_width_upem=600.0,
            lsb_upem=10.0, rsb_upem=10.0, ascent_upem=750.0, descent_upem=0.0,
            bounding_box_upem=(10.0, 0.0, 590.0, 750.0),
            contours=[square(10.0, 0.0, 590.0, 750.0)],
            confidence=1.0, observation_fingerprints=(f"{0x41:064x}",),
        ),
        0x61: CalibratedGlyph(
            code_point=0x61, character="a", advance_width_upem=550.0,
            lsb_upem=10.0, rsb_upem=10.0, ascent_upem=750.0, descent_upem=0.0,
            bounding_box_upem=(10.0, 0.0, 540.0, 750.0),
            contours=[square(10.0, 0.0, 540.0, 750.0)],
            confidence=1.0, observation_fingerprints=(f"{0x61:064x}",),
        ),
        0x20: CalibratedGlyph(
            code_point=0x20, character=" ", advance_width_upem=250.0,
            lsb_upem=0.0, rsb_upem=250.0, ascent_upem=750.0, descent_upem=0.0,
            bounding_box_upem=(0.0, 0.0, 0.0, 0.0), contours=[],
            confidence=1.0, observation_fingerprints=(f"{0x20:064x}",),
        ),
    }
    model = assemble_font_model(
        family_name="Cache Proof", style_name="Regular",
        reference_id="d" * 64, style_id="regular", glyphs=glyphs,
        font_ascent_upem=800.0, font_descent_upem=-200.0,
        config_hash="e" * 64, browser_version="test",
        fit_observations_count=len(glyphs),
    )
    builder = AtlasFontBuilder("Cache Proof", "Regular")
    builder.bind_model(model)
    result = builder.build_final(model, tmp_path / "proof_build")
    return result.ttf.file_path.read_bytes()


def _font_cache_identity(source_url: str, style_id: str, mode: str) -> str:
    return identity_hash(
        {
            "atlas_fonts_v1": True,
            "policy_hash": policy_identity_hash(),
            "source_url": source_url,
            "style_id": style_id,
            "mode": mode,
        }
    )


# ----------------------------------------------------------------------
# 1. Provider ordering: exact cache / authorized binary win BEFORE network
# ----------------------------------------------------------------------

async def test_exact_font_cache_wins_before_any_transport(tmp_path):
    ttf_bytes = _small_ttf_bytes(tmp_path)
    otf_bytes = b"OTTO" + ttf_bytes[4:]  # distinct cached OTF bytes
    cache = AtlasCacheStore(tmp_path / "cache")
    fcid = _font_cache_identity(SOURCE_URL, "regular", "ORIGINAL")
    cache.put_bytes_verified(NAMESPACE_FONTS, fcid + "_ttf", ttf_bytes, "ttf")
    cache.put_bytes_verified(NAMESPACE_FONTS, fcid + "_otf", otf_bytes, "otf")
    cache.put_json(NAMESPACE_REPORTS, fcid, {"passed": True, "reasons": []})

    pipeline, counters, state = make_pipeline(
        tmp_path,
        cdn_pages_by_md5=None,  # poisoned: any CDN call fails the test
        dump_dom=PoisonedDumpDom(),
    )
    result = await pipeline.run()
    assert result.evidence.pages_by_source == {"exact_cache_reuse": 1}
    assert result.ttf_path.read_bytes() == ttf_bytes
    assert result.otf_path.read_bytes() == otf_bytes
    assert counters.http_requests == 0
    assert counters.cdp_calls == 0
    assert counters.browser_readbacks == 0
    assert counters.dump_dom_calls == 0
    assert state["starts"] == 0  # the browser session never started


async def test_authorized_binary_cache_wins_before_network(tmp_path):
    ttf_bytes = _small_ttf_bytes(tmp_path)
    binary_cache = AuthorizedBinaryCache(
        tmp_path / "bin_cache", tmp_path / "bin_index.sqlite3"
    )
    ref_fp = hashlib.sha256(SOURCE_URL.encode("utf-8")).hexdigest()
    from compute.archive import canonical_source_identity

    ref_fp = hashlib.sha256(canonical_source_identity(SOURCE_URL).encode("utf-8")).hexdigest()
    binary_cache.put(
        BinaryCacheIdentity(
            reference_fingerprint=ref_fp,
            family_name="Fake Family",
            style_id="regular",
            provenance=BINARY_STAGE_DUMP_DOM,
        ),
        ttf_bytes,
        "TTF",
        stage_provenance=BINARY_STAGE_DUMP_DOM,
    )

    pipeline, counters, state = make_pipeline(
        tmp_path,
        cdn_pages_by_md5=None,  # poisoned
        dump_dom=PoisonedDumpDom(),
        binary_cache=binary_cache,
    )
    result = await pipeline.run()
    assert result.evidence.pages_by_source == {"authorized_binary": 1}
    assert result.ttf_path.exists() and result.otf_path.exists()
    assert result.report["passed"] is True
    assert counters.http_requests == 0
    assert counters.cdp_calls == 0
    assert state["starts"] == 0
    # The binary win is persisted as the exact-identity font cache: the next
    # run wins at stage 1 without touching any binary probe.
    cache = AtlasCacheStore(tmp_path / "cache")
    fcid = _font_cache_identity(SOURCE_URL, "regular", "ORIGINAL")
    assert cache.get_bytes(NAMESPACE_FONTS, fcid + "_ttf", "ttf") is not None


# ----------------------------------------------------------------------
# 2. CDN exact-MD5 verification: mismatch fails closed -> browser fallback
# ----------------------------------------------------------------------

async def test_cdn_md5_mismatch_rejected_falls_back_to_browser(tmp_path):
    acs_pt = cdn_acs_pt_for(1024)
    # Pages bound to the WRONG md5 for the requested identity.
    wrong_pages = (make_sprite_page(MD5_OTHER, acs_pt, {0x41: (10, 10, 143, 123), 0x42: (170, 10, 143, 123)}),)
    source = MonotypeCdnRasterSource(
        FakeCDNClient({MD5_OK: wrong_pages}), counters=AtlasTransportCounters()
    )
    observed = await source.fetch_glyph_observations(MD5_OK, "Fake Family", "Fake Regular", 1024)
    assert observed is None  # MD5 mismatch fails closed

    # Full chain: mismatched CDN -> browser canvas atlas supplements.
    state = make_browser_state(expected_md5=MD5_OK)
    pipeline, counters, state = make_pipeline(
        tmp_path,
        cdn_pages_by_md5={MD5_OK: wrong_pages},
        envelope=make_envelope(MD5_OK),
        state=state,
    )
    result = await pipeline.run()
    assert result.evidence.failed_glyphs == 0
    assert 0x41 in result.frozen_glyphs and 0x42 in result.frozen_glyphs
    assert state["starts"] == 1  # the fallback session started exactly once
    assert counters.browser_readbacks >= 1  # canvas pages observed
    assert result.report["passed"] is True


# ----------------------------------------------------------------------
# 3. CDN PRIMARY full chain: one session (metrics), zero canvas readbacks
# ----------------------------------------------------------------------

async def test_cdn_primary_single_session_honest_counters(tmp_path):
    acs_pt = cdn_acs_pt_for(1024)
    pages = (make_sprite_page(MD5_OK, acs_pt, {0x41: (10, 10, 143, 123), 0x42: (170, 10, 143, 123)}),)
    state = make_browser_state(expected_md5=MD5_OK)
    pipeline, counters, state = make_pipeline(
        tmp_path,
        cdn_pages_by_md5={MD5_OK: pages},
        envelope=make_envelope(MD5_OK),
        state=state,
    )
    result = await pipeline.run()

    assert result.evidence.failed_glyphs == 0
    assert set(result.frozen_glyphs) == {0x41, 0x42}
    # ONE persistent session: started lazily for metrics, kept for the run.
    assert state["starts"] == 1
    # CDN served every raster observation: ZERO canvas readbacks.
    assert counters.browser_readbacks == 0
    # Honest HTTP accounting: one observed response per consumed CDN page.
    assert counters.http_requests == len(pages)
    # Batched metrics only: exactly metrics_js_call_count batched JS calls,
    # plus ONE face-resolution evaluate and ONE bounded kern-pair batch (the
    # deterministic A/B kern candidate). Never per-glyph.
    assert counters.cdp_calls == metrics_js_call_count(2) + 2
    assert result.evidence.metrics_js_calls == metrics_js_call_count(2) + 1
    # Evidence carries the observed truth, not pipeline-internal proxies.
    assert result.evidence.http_requests == len(pages)
    assert result.evidence.cdp_calls == metrics_js_call_count(2) + 2
    assert result.evidence.browser_readbacks == 0
    assert result.evidence.pages_by_source.get("cdn_primary") == 2
    assert result.ttf_path.exists() and result.otf_path.exists()
    assert result.report["passed"] is True

    # The run persisted the exact-identity font cache: an immediate re-run
    # wins at stage 1 with zero transports touched.
    pipeline2, counters2, state2 = make_pipeline(
        tmp_path,
        cdn_pages_by_md5=None,  # poisoned
        dump_dom=PoisonedDumpDom(),
        state=state,
        counters=(counters2 := AtlasTransportCounters()),
    )
    result2 = await pipeline2.run()
    assert result2.evidence.pages_by_source == {"exact_cache_reuse": 1}
    assert counters2.http_requests == 0 and state2["starts"] == 1  # no new start


# ----------------------------------------------------------------------
# 4. Batched metrics call counts (never per-glyph)
# ----------------------------------------------------------------------

async def test_batched_metrics_call_counts_never_per_glyph():
    state = make_browser_state()
    holder = SharedBrowserSession(
        lambda: PersistentBrowserAtlasSession(
            source_url=SOURCE_URL, family_name="Fake Family",
            style_name="Fake Regular", style_id="regular",
            counters=AtlasTransportCounters(),
            playwright_launcher=make_launcher(state),
        )
    )
    provider = ProductionMetricsProvider(holder)
    cps = list(range(0x41, 0x41 + 1200))
    total_evals = 0
    for size_px, chunk in build_metrics_batches(cps):
        rows = await provider.fetch_rows(size_px, chunk)
        assert len(rows) == len(chunk)
    expected = metrics_js_call_count(len(cps))  # 3 sizes * ceil(1200/512) = 9
    assert expected == 9
    assert state["evals"] == expected + 1  # +1 face resolution at start
    assert state["starts"] == 1
    await holder.close()


# ----------------------------------------------------------------------
# 5. Algolia resolves a missing MD5, then the chain returns to the CDN
# ----------------------------------------------------------------------

class FakeAlgoliaClient:
    def __init__(self, md5: str):
        self.md5 = md5
        self.calls: list[str] = []

    def available(self) -> bool:
        return True

    async def discover_family(self, query, source_url=""):
        self.calls.append(query)
        return FamilyDiscoveryEnvelope(
            family_name="Fake Family",
            family_url=source_url,
            styles={
                "regular": StyleDiscoveryRecord(
                    style_id="regular", style_name="Fake Regular", md5=self.md5
                )
            },
            provenance="algolia_metadata_cdn",
        )


async def test_algolia_resolves_missing_md5_then_cdn(tmp_path):
    acs_pt = cdn_acs_pt_for(1024)
    pages = (make_sprite_page(MD5_OK, acs_pt, {0x41: (10, 10, 143, 123), 0x42: (170, 10, 143, 123)}),)
    algolia = FakeAlgoliaClient(MD5_OK)
    # dump-dom envelope WITHOUT md5 (metadata only, no binary candidates).
    envelope = FamilyDiscoveryEnvelope(
        family_name="Fake Family",
        family_url=SOURCE_URL,
        styles={"regular": StyleDiscoveryRecord(style_id="regular", style_name="Fake Regular", md5="")},
        provenance="dump_dom_native",
    )
    cdn_client = FakeCDNClient({MD5_OK: pages})
    state = make_browser_state(expected_md5=MD5_OK)
    counters = AtlasTransportCounters()
    pipeline = ProductionAtlasPipeline(
        job_id="job-algolia", mode="ORIGINAL",
        source_url=SOURCE_URL, family_name="Fake Family",
        style_id="regular", style_name="Fake Regular",
        build_dir=tmp_path / "build", deadline=time.monotonic() + 240,
        cache_root=tmp_path / "cache", checkpoint_root=tmp_path / "ckpt",
        dump_dom_provider=FakeDumpDom(envelope),
        cdn_source=MonotypeCdnRasterSource(cdn_client, counters=counters),
        algolia_resolver=AlgoliaMd5Resolver(algolia, counters=counters),
        runtime=AtlasRuntimeDefaults(),
        playwright_launcher=make_launcher(state),
        counters=counters,
    )
    result = await pipeline.run()
    assert algolia.calls == ["Fake Family"]  # exactly one bounded resolution
    assert cdn_client.calls and cdn_client.calls[0]["md5"] == MD5_OK
    assert cdn_client.calls[0]["acs_pt"] == acs_pt
    assert result.evidence.failed_glyphs == 0
    assert result.report["passed"] is True


# ----------------------------------------------------------------------
# 6. Default production factory wiring
# ----------------------------------------------------------------------

def test_factory_disabled_returns_none(test_settings):
    assert test_settings.ACQUISITION_ENABLED is False
    assert build_default_atlas_pipeline_factory(test_settings) is None


def test_factory_enabled_builds_production_pipeline(test_settings, tmp_path):
    settings = test_settings.model_copy(update={"ACQUISITION_ENABLED": True})
    factory = build_default_atlas_pipeline_factory(settings)
    assert callable(factory)
    pipeline = factory(
        job_id="job-factory",
        mode="ORIGINAL",
        source_url=SOURCE_URL,
        family_name="Fake Family",
        style_id="regular",
        style_name="Fake Regular",
        build_dir=tmp_path / "build",
        deadline=time.monotonic() + 60,
        cache_root=tmp_path / "cache",
        checkpoint_root=tmp_path / "ckpt",
    )
    assert isinstance(pipeline, ProductionAtlasPipeline)
    assert pipeline.mode == "ORIGINAL"


def test_composition_wires_factory_into_runner_components(test_settings, tmp_path):
    from composition import build_production_components

    settings = test_settings.model_copy(
        update={"ACQUISITION_ENABLED": True, "SCRATCH_DIR": tmp_path / "scratch"}
    )
    components = build_production_components(settings, tmp_path / "scratch", dev_vars_path=None)
    assert "atlas_pipeline_factory" in components
    assert callable(components["atlas_pipeline_factory"])
