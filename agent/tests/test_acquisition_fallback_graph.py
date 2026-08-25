"""Comprehensive test suite for the 4-lane acquisition fallback graph.

Repros and invariants covered:
- DUMP_DOM_FAMILY_MAP: One multi-style DOM -> parsed into complete exact style->MD5 map.
- DUMP_DOM_COMPLETE: Native lane completes -> Playwright/Algolia make zero calls.
- DUMP_DOM_PARTIAL_TO_PLAYWRIGHT: Native partial -> Playwright Stealth completes family/raster.
- DUMP_DOM_PARTIAL_TO_CDN: Exact MD5 -> direct Monotype CDN multi-page / multi-size crawl.
- DUMP_DOM_BLOCKED_TO_ALGOLIA_CDN: Dump/Stealth blocked -> Algolia metadata -> CDN completes.
- FALLBACK_EXHAUSTION: Incomplete lanes continue; terminal insufficient only after exhaustion.
- BINARY_WINS_ALL_METHODS: Valid binary wins immediately; zero later raster work.
- MULTI_STYLE_ISOLATION: Single shared preflight for all styles; zero cross-style MD5/raster contamination.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from acquisition.models import (
    AcquisitionOutcome,
    AcquiredBinary,
    BinaryAcquisitionPolicy,
    BinaryCandidate,
    DiscoveryEnvelope,
    FamilyDiscoveryEnvelope,
    StyleDiscoveryRecord,
    SpriteRasterPage,
    STAGE_DUMP_DOM_NATIVE,
    STAGE_PLAYWRIGHT_STEALTH,
    STAGE_DIRECT_MONOTYPE_CDN,
    STAGE_ALGOLIA_METADATA_CDN,
)
from acquisition.pipeline import AcquisitionPipeline
from acquisition.providers import (
    DumpDomTransport,
    MonotypeRasterProvider,
    PersistentSessionBinaryProvider,
    parse_family_discovery_from_dump,
)
from acquisition.adapters import (
    HeadlessDumpDomTransport,
    MonotypeRenderClient,
    PlaywrightStealthPersistentSession,
    AlgoliaMetadataClient,
)
from tests.test_issue71_adversarial import _build_real_ttf


SAMPLE_MULTI_STYLE_HTML = """
<!DOCTYPE html>
<html>
<head>
  <title>Helvetica Now | MyFonts</title>
  <meta property="og:title" content="Helvetica Now" />
  <script type="application/ld+json">
  {
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "Helvetica Now",
    "hasVariant": [
      {
        "name": "Helvetica Now Regular",
        "sku": "helvetica_now_regular",
        "fontMd5": "a1b2c3d4e5f60718293a4b5c6d7e8f90"
      },
      {
        "name": "Helvetica Now Bold",
        "sku": "helvetica_now_bold",
        "fontMd5": "11223344556677889900aabbccddeeff"
      },
      {
        "name": "Helvetica Now Light",
        "sku": "helvetica_now_light",
        "fontMd5": "fedcba9876543210fedcba9876543210"
      }
    ]
  }
  </script>
</head>
<body>
  <div class="styles-table">
    <div class="style-row" data-style-id="helvetica_now_regular" data-style-name="Helvetica Now Regular" data-font-md5="a1b2c3d4e5f60718293a4b5c6d7e8f90"></div>
    <div class="style-row" data-style-id="helvetica_now_bold" data-style-name="Helvetica Now Bold" data-font-md5="11223344556677889900aabbccddeeff"></div>
    <div class="style-row" data-style-id="helvetica_now_light" data-style-name="Helvetica Now Light" data-font-md5="fedcba9876543210fedcba9876543210"></div>
  </div>
</body>
</html>
"""


def _make_dummy_sprite_page(md5: str, acs_pt: int, page_index: int = 1, final: bool = True) -> SpriteRasterPage:
    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x10\x00\x00\x00\x10\x08\x06\x00\x00\x00\x1f\xf3\xffa\x00\x00\x00\x19IDATx\x9cc\xf8\xff\xff?\x03\x05\x00\x07\x00\x02\x01\x00\x00\x00\x00IEND\xaeB`\x82"
    return SpriteRasterPage(
        page_index=page_index,
        glyph_count=2,
        raster_bytes=png_bytes,
        next_cursor="" if final else str(page_index + 1),
        final=final,
        payload={
            "browser_version": "monotype_render_105",
            "glyphs": [
                {"code_point": 65, "glyph_index": 1, "sprite_box": {"x": 0, "y": 0, "width": 50, "height": 60}},
                {"code_point": 66, "glyph_index": 2, "sprite_box": {"x": 50, "y": 0, "width": 45, "height": 60}},
            ],
            "md5": md5,
            "acs_pt": acs_pt,
            "sprite_sha256": hashlib.sha256(png_bytes).hexdigest(),
        },
    )


def test_DUMP_DOM_FAMILY_MAP():
    """DUMP_DOM_FAMILY_MAP: One multi-style DOM produces a complete FamilyDiscoveryEnvelope."""
    env = parse_family_discovery_from_dump(SAMPLE_MULTI_STYLE_HTML, "https://www.myfonts.com/collections/helvetica-now-font-monotype", "dump_dom_native")
    assert env.family_name.lower() == "helvetica now"
    assert len(env.styles) == 3
    valid, err = env.validate_integrity()
    assert valid is True
    assert err == ""

    reg = env.get_style_record("helvetica_now_regular")
    assert reg is not None
    assert reg.style_name.lower() == "helvetica now regular"
    assert reg.md5 == "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    assert reg.is_complete_metadata() is True

    bold = env.get_style_record("helvetica_now_bold")
    assert bold is not None
    assert bold.md5 == "11223344556677889900aabbccddeeff"

    light = env.get_style_record("helvetica_now_light")
    assert light is not None
    assert light.md5 == "fedcba9876543210fedcba9876543210"

    # Cross-style isolation: each style has distinct MD5
    assert reg.md5 != bold.md5 != light.md5


@pytest.mark.asyncio
async def test_DUMP_DOM_COMPLETE():
    """DUMP_DOM_COMPLETE: Native dump-dom succeeds; fallback lanes make 0 calls."""
    dump_transport = MagicMock(spec=DumpDomTransport)
    dump_transport.dump_dom = AsyncMock(return_value=SAMPLE_MULTI_STYLE_HTML)

    playwright = MagicMock()
    playwright.available.return_value = True
    playwright.discover_family = AsyncMock()

    algolia = MagicMock()
    algolia.available.return_value = True
    algolia.discover_family = AsyncMock()

    raster_provider = MagicMock()
    raster_provider.available.return_value = True
    raster_provider.client = MagicMock()
    raster_provider.client.fetch_all_sprite_pages = AsyncMock(
        return_value=(_make_dummy_sprite_page("a1b2c3d4e5f60718293a4b5c6d7e8f90", 120),)
    )

    pipeline = AcquisitionPipeline(
        dump_dom_transport=dump_transport,
        playwright_provider=playwright,
        algolia_provider=algolia,
        raster_provider=raster_provider,
    )

    outcome = await pipeline.acquire(
        source_url="https://www.myfonts.com/collections/helvetica-now-font-monotype",
        expected_family="Helvetica Now",
        expected_style="Helvetica Now Regular",
    )

    assert outcome.kind == "raster_authorized"
    assert len(outcome.raster_pages) == 1
    # Fallback lanes made zero discovery calls
    assert playwright.discover_family.call_count == 0
    assert algolia.discover_family.call_count == 0


@pytest.mark.asyncio
async def test_DUMP_DOM_PARTIAL_TO_PLAYWRIGHT():
    """DUMP_DOM_PARTIAL_TO_PLAYWRIGHT: Native dump fails/partial -> Playwright Stealth completes."""
    dump_transport = MagicMock(spec=DumpDomTransport)
    dump_transport.dump_dom = AsyncMock(side_effect=RuntimeError("CHROME_BLOCKED_403"))

    playwright_env = FamilyDiscoveryEnvelope(
        family_name="Helvetica Now",
        family_url="https://www.myfonts.com/collections/helvetica-now",
        styles={
            "regular": StyleDiscoveryRecord(
                style_id="regular",
                style_name="Regular",
                md5="a1b2c3d4e5f60718293a4b5c6d7e8f90",
                provenance="playwright_stealth_persistent",
            )
        },
        provenance="playwright_stealth_persistent",
    )

    playwright = MagicMock()
    playwright.available.return_value = True
    playwright.discover_family = AsyncMock(return_value=playwright_env)
    playwright.capture_raster_pages = AsyncMock(
        return_value=(_make_dummy_sprite_page("a1b2c3d4e5f60718293a4b5c6d7e8f90", 120),)
    )

    algolia = MagicMock()
    algolia.available.return_value = True
    algolia.discover_family = AsyncMock()

    pipeline = AcquisitionPipeline(
        dump_dom_transport=dump_transport,
        playwright_provider=playwright,
        algolia_provider=algolia,
        raster_provider=None,
    )

    outcome = await pipeline.acquire(
        source_url="https://www.myfonts.com/collections/helvetica-now",
        expected_family="Helvetica Now",
        expected_style="Regular",
    )

    assert outcome.kind == "raster_authorized"
    assert playwright.discover_family.call_count == 1
    assert algolia.discover_family.call_count == 0


@pytest.mark.asyncio
async def test_DUMP_DOM_PARTIAL_TO_CDN():
    """DUMP_DOM_PARTIAL_TO_CDN: Exact style MD5 is known -> direct CDN crawls all sizes."""
    dump_transport = MagicMock(spec=DumpDomTransport)
    dump_transport.dump_dom = AsyncMock(return_value=SAMPLE_MULTI_STYLE_HTML)

    client = MonotypeRenderClient(base_url="https://sig.monotype.com")

    pages_returned = []
    async def mock_fetch_sprite_page(req, cursor):
        pt = req.get("acs_pt", 120)
        p = _make_dummy_sprite_page(req["md5"], pt, int(cursor) if cursor else 1, final=True)
        pages_returned.append(p)
        return p

    client.fetch_sprite_page = AsyncMock(side_effect=mock_fetch_sprite_page)
    raster_provider = MonotypeRasterProvider(client=client)

    pipeline = AcquisitionPipeline(
        dump_dom_transport=dump_transport,
        raster_provider=raster_provider,
    )

    outcome = await pipeline.acquire(
        source_url="https://www.myfonts.com/collections/helvetica-now",
        expected_family="Helvetica Now",
        expected_style="Helvetica Now Bold",
        raster_request={"acs_pts": [64, 120, 256]},
    )

    assert outcome.kind == "raster_authorized"
    assert len(outcome.raster_pages) == 3
    pts_seen = {p.payload["acs_pt"] for p in outcome.raster_pages}
    assert pts_seen == {64, 120, 256}


@pytest.mark.asyncio
async def test_DUMP_DOM_BLOCKED_TO_ALGOLIA_CDN():
    """DUMP_DOM_BLOCKED_TO_ALGOLIA_CDN: Dump and persistent blocked -> Algolia metadata -> CDN."""
    dump_transport = MagicMock(spec=DumpDomTransport)
    dump_transport.dump_dom = AsyncMock(side_effect=RuntimeError("BLOCKED_CLOUDFLARE"))

    playwright = MagicMock()
    playwright.available.return_value = True
    playwright.discover_family = AsyncMock(return_value=None)
    playwright.capture_raster_pages = AsyncMock(return_value=None)

    algolia_env = FamilyDiscoveryEnvelope(
        family_name="Futura Now",
        styles={
            "headline": StyleDiscoveryRecord(
                style_id="headline",
                style_name="Headline",
                md5="99887766554433221100aabbccddeeff",
                provenance=STAGE_ALGOLIA_METADATA_CDN,
            )
        },
        provenance=STAGE_ALGOLIA_METADATA_CDN,
    )

    algolia = MagicMock()
    algolia.available.return_value = True
    algolia.discover_family = AsyncMock(return_value=algolia_env)

    client = MagicMock()
    client.fetch_all_sprite_pages = AsyncMock(
        return_value=(_make_dummy_sprite_page("99887766554433221100aabbccddeeff", 120),)
    )
    raster_provider = MonotypeRasterProvider(client=client)

    pipeline = AcquisitionPipeline(
        dump_dom_transport=dump_transport,
        playwright_provider=playwright,
        algolia_provider=algolia,
        raster_provider=raster_provider,
    )

    outcome = await pipeline.acquire(
        source_url="https://www.myfonts.com/collections/futura-now",
        expected_family="Futura Now",
        expected_style="Headline",
    )

    assert outcome.kind == "raster_authorized"
    assert algolia.discover_family.call_count == 1
    assert client.fetch_all_sprite_pages.call_count == 1


@pytest.mark.asyncio
async def test_FALLBACK_EXHAUSTION():
    """FALLBACK_EXHAUSTION: Failing/partial lanes continue in strict order until exhaustion."""
    dump_transport = MagicMock(spec=DumpDomTransport)
    dump_transport.dump_dom = AsyncMock(side_effect=RuntimeError("ERR1"))

    playwright = MagicMock()
    playwright.available.return_value = True
    playwright.discover_family = AsyncMock(return_value=None)
    playwright.capture_raster_pages = AsyncMock(return_value=None)

    algolia = MagicMock()
    algolia.available.return_value = True
    algolia.discover_family = AsyncMock(return_value=None)

    client = MagicMock()
    client.fetch_all_sprite_pages = AsyncMock(return_value=None)
    raster_provider = MonotypeRasterProvider(client=client)

    pipeline = AcquisitionPipeline(
        dump_dom_transport=dump_transport,
        playwright_provider=playwright,
        algolia_provider=algolia,
        raster_provider=raster_provider,
    )

    outcome = await pipeline.acquire(
        source_url="https://www.myfonts.com/collections/unavailable-font",
        expected_family="Unavailable",
        expected_style="Regular",
    )

    assert outcome.kind == "insufficient"
    assert outcome.terminal_reason_code == "ACQUISITION_INSUFFICIENT"


@pytest.mark.asyncio
async def test_BINARY_WINS_ALL_METHODS():
    """BINARY_WINS_ALL_METHODS: Valid binary halts all later raster and reconstruction work."""
    raw_ttf = _build_real_ttf("Binary Win Fam", "Regular")

    envelope_with_binary = FamilyDiscoveryEnvelope(
        family_name="Binary Win Fam",
        styles={
            "regular": StyleDiscoveryRecord(
                style_id="regular",
                style_name="Regular",
                md5="12345678901234567890123456789012",
                binary_candidates=(BinaryCandidate(url="https://cdn.example.com/font.ttf", format="TTF"),),
                provenance="dump_dom_native",
            )
        },
        provenance="dump_dom_native",
    )

    dump_transport = MagicMock(spec=DumpDomTransport)
    dump_transport.dump_dom = AsyncMock(return_value="")

    binary_fetch = AsyncMock(return_value=raw_ttf)

    client = MagicMock()
    client.fetch_all_sprite_pages = AsyncMock()
    raster_provider = MonotypeRasterProvider(client=client)

    pipeline = AcquisitionPipeline(
        dump_dom_transport=dump_transport,
        binary_fetch=binary_fetch,
        raster_provider=raster_provider,
    )

    outcome = await pipeline.acquire(
        source_url="https://www.myfonts.com/collections/binary-win",
        expected_family="Binary Win Fam",
        expected_style="Regular",
        family_envelope=envelope_with_binary,
    )

    assert outcome.kind == "binary"
    assert outcome.binary is not None
    assert outcome.binary.format == "TTF"
    # Zero raster calls made
    assert client.fetch_all_sprite_pages.call_count == 0


@pytest.mark.asyncio
async def test_MULTI_STYLE_ISOLATION():
    """MULTI_STYLE_ISOLATION: One family preflight serves all styles with zero cross-style leakage."""
    dump_transport = MagicMock(spec=DumpDomTransport)
    dump_transport.dump_dom = AsyncMock(return_value=SAMPLE_MULTI_STYLE_HTML)

    client = MagicMock()
    def mock_fetch(req, policy):
        return (_make_dummy_sprite_page(req["md5"], 120),)
    client.fetch_all_sprite_pages = AsyncMock(side_effect=mock_fetch)
    raster_provider = MonotypeRasterProvider(client=client)

    pipeline = AcquisitionPipeline(
        dump_dom_transport=dump_transport,
        raster_provider=raster_provider,
    )

    # 1. Preflight once for the whole family
    family_env = await pipeline.acquire_family_preflight("https://www.myfonts.com/collections/helvetica-now")
    assert dump_transport.dump_dom.call_count == 1
    assert len(family_env.styles) == 3

    # 2. Acquire style 1 (Regular)
    out_reg = await pipeline.acquire(
        "https://www.myfonts.com/collections/helvetica-now",
        "Helvetica Now",
        "Helvetica Now Regular",
        family_envelope=family_env,
    )
    assert out_reg.kind == "raster_authorized"
    assert out_reg.raster_pages[0].payload["md5"] == "a1b2c3d4e5f60718293a4b5c6d7e8f90"

    # 3. Acquire style 2 (Bold)
    out_bold = await pipeline.acquire(
        "https://www.myfonts.com/collections/helvetica-now",
        "Helvetica Now",
        "Helvetica Now Bold",
        family_envelope=family_env,
    )
    assert out_bold.kind == "raster_authorized"
    assert out_bold.raster_pages[0].payload["md5"] == "11223344556677889900aabbccddeeff"

    # Verify dump_dom was still called exactly once
    assert dump_transport.dump_dom.call_count == 1


def test_ALGOLIA_AVAILABLE_requires_both_app_id_and_key():
    """Algolia client reports available only when BOTH app_id and api_key are present."""
    c1 = AlgoliaMetadataClient(app_id="APP123", api_key=None)
    assert c1.available() is False

    c2 = AlgoliaMetadataClient(app_id="", api_key="KEY123")
    assert c2.available() is False

    c3 = AlgoliaMetadataClient(app_id="APP123", api_key="KEY123")
    assert c3.available() is True


def test_CLOSED_RASTER_COMPLETION_contract():
    """is_complete_raster_pages enforces terminal signals, bounding boxes, and size coverage."""
    from acquisition.models import is_complete_raster_pages

    # 1. Valid single-size page
    p1 = _make_dummy_sprite_page("a" * 32, 120, 1, final=True)
    assert is_complete_raster_pages([p1], [120], expected_md5="a" * 32) is True

    # 2. Missing requested size fails closed
    assert is_complete_raster_pages([p1], [120, 240], expected_md5="a" * 32) is False

    # 3. MD5 mismatch fails closed
    assert is_complete_raster_pages([p1], [120], expected_md5="b" * 32) is False

    # 4. Incomplete page without terminal final signal fails closed
    p_non_final = _make_dummy_sprite_page("a" * 32, 120, 1, final=False)
    assert is_complete_raster_pages([p_non_final], [120]) is False

    # 5. Invalid/negative bounding box fails closed
    bad_box_page = SpriteRasterPage(
        page_index=1,
        glyph_count=1,
        raster_bytes=p1.raster_bytes,
        final=True,
        payload={"acs_pt": 120, "glyphs": [{"code_point": 65, "sprite_box": {"x": -1, "y": 0, "width": 10, "height": 10}}]}
    )
    assert is_complete_raster_pages([bad_box_page], [120]) is False


@pytest.mark.asyncio
async def test_STRICT_ORDER_native_to_playwright_to_cdn_to_algolia():
    """Verify strict 4-lane fallback order: Native -> Playwright -> CDN -> Algolia-to-CDN."""
    execution_order = []

    family_envelope = FamilyDiscoveryEnvelope(
        family_name="Test Fam",
        styles={"regular": StyleDiscoveryRecord(style_id="regular", style_name="Regular", md5="a" * 32, provenance=STAGE_DUMP_DOM_NATIVE)},
        provenance=STAGE_DUMP_DOM_NATIVE,
    )

    dump_transport = MagicMock(spec=DumpDomTransport)

    async def mock_playwright_capture(url, style_rec, sizes):
        execution_order.append("lane2_playwright")
        return None  # Lane 2 returns None -> continue to Lane 3

    playwright = PlaywrightStealthPersistentSession(
        transport_override=mock_playwright_capture
    )

    client = MagicMock()
    async def mock_cdn_crawl(target, policy):
        execution_order.append("lane3_cdn")
        return None  # Lane 3 returns None -> continue to Lane 4
    client.fetch_all_sprite_pages = AsyncMock(side_effect=mock_cdn_crawl)
    raster_provider = MonotypeRasterProvider(client=client)

    algolia = AlgoliaMetadataClient(app_id="APP123", api_key="KEY123")
    async def mock_algolia_discover(query, url):
        execution_order.append("lane4_algolia")
        return FamilyDiscoveryEnvelope(
            family_name="Test Fam",
            styles={"regular": StyleDiscoveryRecord(style_id="regular", style_name="Regular", md5="c" * 32, provenance=STAGE_ALGOLIA_METADATA_CDN)},
            provenance=STAGE_ALGOLIA_METADATA_CDN,
        )
    algolia.discover_family = AsyncMock(side_effect=mock_algolia_discover)

    pipeline = AcquisitionPipeline(
        dump_dom_transport=dump_transport,
        playwright_provider=playwright,
        raster_provider=raster_provider,
        algolia_provider=algolia,
    )

    outcome = await pipeline.acquire(
        source_url="https://www.myfonts.com/collections/test-fam",
        expected_family="Test Fam",
        expected_style="Regular",
        raster_request={"md5": "a" * 32},
        family_envelope=family_envelope,
    )

    assert outcome.kind == "insufficient"
    assert execution_order == [
        "lane2_playwright",
        "lane3_cdn",
        "lane4_algolia",
        "lane3_cdn",  # Algolia invokes the same CDN crawler
    ]
    assert outcome.trace.stage_order() == (
        STAGE_DUMP_DOM_NATIVE,
        STAGE_PLAYWRIGHT_STEALTH,
        STAGE_DIRECT_MONOTYPE_CDN,
        STAGE_ALGOLIA_METADATA_CDN,
    )

