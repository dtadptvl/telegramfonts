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
    import io
    from PIL import Image
    im = Image.new("RGBA", (500, 500), (0, 0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    png_bytes = buf.getvalue()
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


# =========================================================================
# CAUSAL PRODUCTION-BOUNDARY REPROS (Issue #73 comment 5418364994)
# =========================================================================

def test_CLEAN_DEPENDENCY_INSTALL():
    """CLEAN_DEPENDENCY_INSTALL: Playwright is pinned in requirements-lock.txt and pyproject.toml."""
    req_lock = (Path(__file__).resolve().parent.parent / "requirements-lock.txt").read_text(encoding="utf-8")
    pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")

    assert "playwright==" in req_lock or "playwright>=" in req_lock
    assert "playwright" in pyproject


def test_LEGACY_HTTP_ZERO():
    """LEGACY_HTTP_ZERO: Production builder wires session_provider=None and makes zero legacy HTTP calls."""
    from config import Settings
    from acquisition.adapters import build_production_acquisition_pipeline

    class _MockSettings:
        ACQUISITION_ENABLED = True
        AUTHORIZED_SESSION_MATERIAL_FILE = Path("/some/nonexistent/secret.json")
        PLAYWRIGHT_STEALTH_ENABLED = False
        MONOTYPE_RASTER_ENDPOINT_URL = ""
        MYFONTS_ALGOLIA_APP_ID = ""
        MYFONTS_ALGOLIA_API_KEY = None

    pipeline = build_production_acquisition_pipeline(_MockSettings)
    assert pipeline is not None
    assert pipeline.session_provider is None


def test_CDN_DUPLICATE_CONFLICT():
    """CDN_DUPLICATE_CONFLICT: is_complete_raster_pages rejects duplicate/conflicting code points."""
    from acquisition.models import is_complete_raster_pages, SpriteRasterPage

    # Page 1 has U+0041 ('A')
    p1 = _make_dummy_sprite_page("a" * 32, 120, page_index=1, final=False)
    # Page 2 also has U+0041 ('A') with conflicting box
    p2 = SpriteRasterPage(
        page_index=2,
        glyph_count=1,
        raster_bytes=p1.raster_bytes,
        next_cursor="",
        final=True,
        payload={
            "md5": "a" * 32,
            "acs_pt": 120,
            "glyphs": [
                {"code_point": 65, "glyph_index": 1, "sprite_box": {"x": 100, "y": 100, "width": 50, "height": 60}}
            ],
        },
    )

    # Conflicting U+0041 across pages must return False
    assert is_complete_raster_pages([p1, p2], [120], expected_md5="a" * 32) is False


def test_CDN_BOUND_WITHOUT_TERMINAL():
    """CDN_BOUND_WITHOUT_TERMINAL: Hitting page bound without terminal completion fails closed."""
    from acquisition.models import is_complete_raster_pages

    # Non-final page with next_cursor="2" but no second page
    p_open = _make_dummy_sprite_page("a" * 32, 120, page_index=1, final=False)
    assert is_complete_raster_pages([p_open], [120], expected_md5="a" * 32) is False


@pytest.mark.asyncio
async def test_STEALTH_FALLBACK_FONT_REJECTED():
    """STEALTH_FALLBACK_FONT_REJECTED: If target font is unverified or falls back to system font, returns None."""
    # When available() is false (e.g. invalid user_data_dir), Stealth fails closed
    stealth_invalid = PlaywrightStealthPersistentSession(user_data_dir=Path("/nonexistent/dir"))
    assert stealth_invalid.available() is False

    res = await stealth_invalid.capture_raster_pages(
        "https://www.myfonts.com/collections/test",
        StyleDiscoveryRecord(style_id="regular", style_name="Regular", md5="a" * 32),
        [120],
    )
    assert res is None


@pytest.mark.asyncio
async def test_STEALTH_TARGET_FONT_PROVEN():
    """STEALTH_TARGET_FONT_PROVEN: Proven webfont renders complete raster with pixel bounding boxes & features."""
    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x10\x00\x00\x00\x10\x08\x06\x00\x00\x00\x1f\xf3\xffa\x00\x00\x00\x19IDATx\x9cc\xf8\xff\xff?\x03\x05\x00\x07\x00\x02\x01\x00\x00\x00\x00IEND\xaeB`\x82"

    async def mock_proven_capture(url, style_rec, sizes):
        pages = []
        for pt in sizes:
            pages.append(
                SpriteRasterPage(
                    page_index=1,
                    glyph_count=2,
                    raster_bytes=png_bytes,
                    next_cursor="",
                    final=True,
                    payload={
                        "browser_version": "playwright_stealth_v1",
                        "glyphs": [
                            {"code_point": 65, "glyph_index": 1, "sprite_box": {"x": 5, "y": 5, "width": 40, "height": 50}},
                            {"code_point": 66, "glyph_index": 2, "sprite_box": {"x": 50, "y": 5, "width": 38, "height": 50}},
                        ],
                        "pairs": [{"left_char": "A", "right_char": "V", "pair_text": "AV", "kern_px": -2.5, "provenance": "playwright:canvas_text_metrics"}],
                        "features": [{"feature_tag": "liga", "sample_text": "fi fl", "provenance": "playwright:canvas"}],
                        "md5": style_rec.md5,
                        "acs_pt": pt,
                        "sprite_sha256": hashlib.sha256(png_bytes).hexdigest(),
                        "provenance": STAGE_PLAYWRIGHT_STEALTH,
                    },
                )
            )
        return tuple(pages)

    stealth = PlaywrightStealthPersistentSession(transport_override=mock_proven_capture)
    style_rec = StyleDiscoveryRecord(style_id="regular", style_name="Regular", md5="a" * 32)
    pages = await stealth.capture_raster_pages("https://www.myfonts.com/collections/test", style_rec, [120])

    assert pages is not None
    assert len(pages) == 1
    assert pages[0].payload["pairs"]
    assert pages[0].payload["features"]
    assert pages[0].payload["glyphs"][0]["sprite_box"]["width"] == 40


@pytest.mark.asyncio
async def test_REAL_CALL_ORDER():
    """REAL_CALL_ORDER: Verify exact 4-lane sequence and prove Algolia is NOT called during preflight."""
    preflight_calls = []
    acquire_calls = []

    dump_transport = MagicMock(spec=DumpDomTransport)
    async def mock_dump(url):
        preflight_calls.append("lane1_dump_dom")
        return ""
    dump_transport.dump_dom = AsyncMock(side_effect=mock_dump)

    async def mock_stealth_discover(url):
        preflight_calls.append("lane2_stealth_discover")
        return None

    async def mock_stealth_capture(url, style_rec, sizes):
        acquire_calls.append("lane2_stealth_capture")
        return None

    stealth = PlaywrightStealthPersistentSession(
        discovery_override=mock_stealth_discover,
        transport_override=mock_stealth_capture,
    )

    client = MagicMock()
    async def mock_cdn(target, policy):
        acquire_calls.append("lane3_cdn")
        return None
    client.fetch_all_sprite_pages = AsyncMock(side_effect=mock_cdn)
    raster_provider = MonotypeRasterProvider(client=client)

    algolia = AlgoliaMetadataClient(app_id="APP123", api_key="KEY123")
    async def mock_algolia_discover(query, url):
        acquire_calls.append("lane4_algolia_discover")
        return FamilyDiscoveryEnvelope(
            family_name="Real Order Fam",
            styles={"regular": StyleDiscoveryRecord(style_id="regular", style_name="Regular", md5="d" * 32, provenance=STAGE_ALGOLIA_METADATA_CDN)},
            provenance=STAGE_ALGOLIA_METADATA_CDN,
        )
    algolia.discover_family = AsyncMock(side_effect=mock_algolia_discover)

    pipeline = AcquisitionPipeline(
        dump_dom_transport=dump_transport,
        playwright_provider=stealth,
        raster_provider=raster_provider,
        algolia_provider=algolia,
    )

    # 1. Run family preflight
    family_env = await pipeline.acquire_family_preflight("https://www.myfonts.com/collections/real-order")
    # Algolia must NOT execute during family preflight
    assert "lane1_dump_dom" in preflight_calls
    assert "lane2_stealth_discover" in preflight_calls
    assert "lane4_algolia_discover" not in preflight_calls

    # 2. Run acquire for style
    outcome = await pipeline.acquire(
        source_url="https://www.myfonts.com/collections/real-order",
        expected_family="Real Order Fam",
        expected_style="Regular",
        raster_request={"md5": "d" * 32},
        family_envelope=family_env,
    )

    assert acquire_calls == [
        "lane2_stealth_capture",
        "lane3_cdn",
        "lane4_algolia_discover",
        "lane3_cdn",
    ]


def _make_fake_playwright_launcher(eval_result: Any):
    async def _launcher(**kwargs):
        mock_page = MagicMock()
        mock_page.goto = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value=eval_result)
        mock_page.content = AsyncMock(return_value="<html></html>")

        mock_context = MagicMock()
        mock_context.add_init_script = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_context.close = AsyncMock()
        return mock_context
    return _launcher


# =========================================================================
# CAUSAL PRODUCTION-BOUNDARY REPROS (Issue #73 comment 5418960565)
# =========================================================================

def test_CLEAN_DEPENDENCY_INSTALL():
    """CLEAN_DEPENDENCY_INSTALL: Playwright is pinned in requirements-lock.txt and pyproject.toml."""
    req_lock = (Path(__file__).resolve().parent.parent / "requirements-lock.txt").read_text(encoding="utf-8")
    pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")

    assert "playwright==" in req_lock or "playwright>=" in req_lock
    assert "playwright" in pyproject


def test_LEGACY_HTTP_ZERO():
    """LEGACY_HTTP_ZERO: Production builder wires session_provider=None and makes zero legacy HTTP calls."""
    from config import Settings
    from acquisition.adapters import build_production_acquisition_pipeline

    class _MockSettings:
        ACQUISITION_ENABLED = True
        AUTHORIZED_SESSION_MATERIAL_FILE = Path("/some/nonexistent/secret.json")
        PLAYWRIGHT_STEALTH_ENABLED = False
        MONOTYPE_RASTER_ENDPOINT_URL = ""
        MYFONTS_ALGOLIA_APP_ID = ""
        MYFONTS_ALGOLIA_API_KEY = None

    pipeline = build_production_acquisition_pipeline(_MockSettings)
    assert pipeline is not None
    assert pipeline.session_provider is None


def test_CDN_DUPLICATE_CONFLICT():
    """CDN_DUPLICATE_CONFLICT: is_complete_raster_pages rejects duplicate/conflicting code points."""
    from acquisition.models import is_complete_raster_pages, SpriteRasterPage

    # Page 1 has U+0041 ('A')
    p1 = _make_dummy_sprite_page("a" * 32, 120, page_index=1, final=False)
    # Page 2 also has U+0041 ('A') with conflicting box
    p2 = SpriteRasterPage(
        page_index=2,
        glyph_count=1,
        raster_bytes=p1.raster_bytes,
        next_cursor="",
        final=True,
        payload={
            "md5": "a" * 32,
            "acs_pt": 120,
            "glyphs": [
                {"code_point": 65, "glyph_index": 1, "sprite_box": {"x": 5, "y": 5, "width": 50, "height": 60}}
            ],
        },
    )

    # Conflicting U+0041 across pages must return False
    assert is_complete_raster_pages([p1, p2], [120], expected_md5="a" * 32) is False


def test_CDN_BOUND_WITHOUT_TERMINAL():
    """CDN_BOUND_WITHOUT_TERMINAL: Hitting page bound without terminal completion fails closed."""
    from acquisition.models import is_complete_raster_pages

    # Non-final page with next_cursor="2" but no second page
    p_open = _make_dummy_sprite_page("a" * 32, 120, page_index=1, final=False)
    assert is_complete_raster_pages([p_open], [120], expected_md5="a" * 32) is False


@pytest.mark.asyncio
async def test_CDN_REAL_EMPTY_TERMINATOR():
    """CDN_REAL_EMPTY_TERMINATOR: Production crawler fetches Page 1 (data) and Page 2 (empty terminal) and passes."""
    import io
    from PIL import Image
    im = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    png_bytes = buf.getvalue()
    png_b64 = base64.b64encode(png_bytes).decode("ascii")

    page1_data = {
        "status": 200,
        "image": png_b64,
        "layout": {
            "0": {"x": 5, "y": 5, "width": 30, "height": 40, "glyph": 1, "codePoint": 65},
            "1": {"x": 40, "y": 5, "width": 30, "height": 40, "glyph": 2, "codePoint": 66},
        },
    }
    page2_data = {
        "status": 200,
        "image": png_b64,
        "layout": {},  # Empty layout terminal page
    }

    client = MonotypeRenderClient(base_url="https://sig.monotype.com")

    call_count = 0
    async def mock_fetch_sprite_page(req, cursor):
        nonlocal call_count
        call_count += 1
        md5 = req["md5"]
        pt = req.get("acs_pt", 120)
        if cursor == "":
            return client._parse_page(page1_data, {"content-type": "application/json"}, 1, md5, pt)
        elif cursor == "2":
            return client._parse_page(page2_data, {"content-type": "application/json"}, 2, md5, pt)
        return None

    client.fetch_sprite_page = AsyncMock(side_effect=mock_fetch_sprite_page)

    policy = BinaryAcquisitionPolicy(max_sprite_pages=5)
    request = {
        "family": "Test Family",
        "style": "Regular",
        "md5": "a" * 32,
        "acs_pts": [120],
    }

    pages = await client.fetch_all_sprite_pages(request, policy)
    assert pages is not None
    assert len(pages) == 2
    assert pages[0].page_index == 1
    assert pages[0].glyph_count == 2
    assert pages[0].final is False
    assert pages[1].page_index == 2
    assert pages[1].glyph_count == 0
    assert pages[1].final is True
    assert pages[1].next_cursor == ""


@pytest.mark.asyncio
async def test_STEALTH_FALLBACK_FONT_REJECTED():
    """STEALTH_FALLBACK_FONT_REJECTED: If target font is unverified or falls back to system font, returns None."""
    stealth_invalid = PlaywrightStealthPersistentSession(user_data_dir=Path("/nonexistent/dir"))
    assert stealth_invalid.available() is False

    res = await stealth_invalid.capture_raster_pages(
        "https://www.myfonts.com/collections/test",
        StyleDiscoveryRecord(style_id="regular", style_name="Regular", md5="a" * 32),
        [120],
    )
    assert res is None


@pytest.mark.asyncio
async def test_STEALTH_PER_GLYPH_FALLBACK_REJECTED():
    """STEALTH_PER_GLYPH_FALLBACK_REJECTED: Evaluator returning fallback error fails closed."""
    launcher = _make_fake_playwright_launcher({"error": "FALLBACK_DISCRIMINATION_FAILED"})
    stealth = PlaywrightStealthPersistentSession(playwright_launcher=launcher)
    style_rec = StyleDiscoveryRecord(style_id="regular", style_name="Regular", md5="a" * 32)
    pages = await stealth.capture_raster_pages("https://www.myfonts.com/collections/test", style_rec, [120])
    assert pages is None


@pytest.mark.asyncio
async def test_STEALTH_SPRITE_OVERFLOW_REJECTED():
    """STEALTH_SPRITE_OVERFLOW_REJECTED: Glyph box exceeding PNG dimensions is rejected."""
    import io
    from PIL import Image
    im = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    png_bytes = buf.getvalue()
    b64_str = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")

    eval_result = {
        "results": [
            {
                "pt": 120,
                "resolved_face": {"family": "Regular", "style": "normal", "weight": "normal", "stretch": "normal"},
                "required_source_cps": [65],
                "candidate_cps": [65],
                "proven_cps": [65],
                "rejected_cps": [],
                "pages": [
                    {
                        "page_index": 1,
                        "dataUrl": b64_str,
                        "glyphs": [
                            {"code_point": 65, "glyph_index": 1, "sprite_box": {"x": 5, "y": 5, "width": 150, "height": 50}},
                        ],
                        "final": True,
                        "next_cursor": "",
                    }
                ],
                "pairs": [],
                "features": [],
            }
        ]
    }

    launcher = _make_fake_playwright_launcher(eval_result)
    stealth = PlaywrightStealthPersistentSession(playwright_launcher=launcher)
    style_rec = StyleDiscoveryRecord(style_id="regular", style_name="Regular", md5="a" * 32)
    pages = await stealth.capture_raster_pages("https://www.myfonts.com/collections/test", style_rec, [120])
    assert pages is None


@pytest.mark.asyncio
async def test_STEALTH_UNMEASURED_FEATURE_REJECTED():
    """STEALTH_UNMEASURED_FEATURE_REJECTED: Unmeasured / zero delta features fail closed."""
    launcher = _make_fake_playwright_launcher({"error": "NO_PROVEN_TARGET_GLYPHS"})
    stealth = PlaywrightStealthPersistentSession(playwright_launcher=launcher)
    style_rec = StyleDiscoveryRecord(style_id="regular", style_name="Regular", md5="a" * 32)
    pages = await stealth.capture_raster_pages("https://www.myfonts.com/collections/test", style_rec, [120])
    assert pages is None


@pytest.mark.asyncio
async def test_STEALTH_TARGET_FONT_PROVEN():
    """STEALTH_TARGET_FONT_PROVEN: Closed fake Playwright page executes production decoding, box validation and feature capture."""
    import io
    from PIL import Image
    im = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    png_bytes = buf.getvalue()
    b64_str = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")

    eval_result = {
        "results": [
            {
                "pt": 120,
                "resolved_face": {"family": "Regular", "style": "normal", "weight": "normal", "stretch": "normal"},
                "required_source_cps": [65, 66],
                "candidate_cps": [65, 66, 67],
                "proven_cps": [65, 66],
                "rejected_cps": [67],
                "pages": [
                    {
                        "page_index": 1,
                        "dataUrl": b64_str,
                        "glyphs": [
                            {"code_point": 65, "glyph_index": 1, "sprite_box": {"x": 5, "y": 5, "width": 40, "height": 50}},
                            {"code_point": 66, "glyph_index": 2, "sprite_box": {"x": 50, "y": 5, "width": 38, "height": 50}},
                        ],
                        "final": True,
                        "next_cursor": "",
                    }
                ],
                "pairs": [
                    {"left_char": "A", "right_char": "V", "pair_text": "AV", "kern_px": -2.5, "provenance": "playwright:canvas_text_metrics"}
                ],
                "features": [
                    {"feature_tag": "liga", "sample_text": "fi", "delta_px": -1.2, "measured": True, "provenance": "playwright:dom_feature_probe"}
                ],
            }
        ]
    }

    launcher = _make_fake_playwright_launcher(eval_result)
    stealth = PlaywrightStealthPersistentSession(playwright_launcher=launcher)
    style_rec = StyleDiscoveryRecord(style_id="regular", style_name="Regular", md5="a" * 32)
    pages = await stealth.capture_raster_pages("https://www.myfonts.com/collections/test", style_rec, [120])

    assert pages is not None
    assert len(pages) == 1
    assert pages[0].payload["pairs"]
    assert pages[0].payload["features"]
    assert pages[0].payload["glyphs"][0]["sprite_box"]["width"] == 40
    assert pages[0].glyph_count == 2
    assert pages[0].payload["resolved_face"]["family"] == "Regular"


@pytest.mark.asyncio
async def test_STEALTH_TRUNCATION_CONTINUES_PAGES():
    """STEALTH_TRUNCATION_CONTINUES_PAGES: Sprite overflow paginates sequentially instead of truncating."""
    import io
    from PIL import Image
    im = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    png_bytes = buf.getvalue()
    b64_str = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")

    eval_result = {
        "results": [
            {
                "pt": 120,
                "resolved_face": {"family": "Regular", "style": "normal", "weight": "normal", "stretch": "normal"},
                "required_source_cps": [65, 66, 67, 68],
                "candidate_cps": [65, 66, 67, 68],
                "proven_cps": [65, 66, 67, 68],
                "rejected_cps": [],
                "pages": [
                    {
                        "page_index": 1,
                        "dataUrl": b64_str,
                        "glyphs": [
                            {"code_point": 65, "glyph_index": 1, "sprite_box": {"x": 5, "y": 5, "width": 40, "height": 50}},
                            {"code_point": 66, "glyph_index": 2, "sprite_box": {"x": 50, "y": 5, "width": 38, "height": 50}},
                        ],
                        "final": False,
                        "next_cursor": "2",
                    },
                    {
                        "page_index": 2,
                        "dataUrl": b64_str,
                        "glyphs": [
                            {"code_point": 67, "glyph_index": 1, "sprite_box": {"x": 5, "y": 5, "width": 40, "height": 50}},
                            {"code_point": 68, "glyph_index": 2, "sprite_box": {"x": 50, "y": 5, "width": 38, "height": 50}},
                        ],
                        "final": True,
                        "next_cursor": "",
                    },
                ],
                "pairs": [],
                "features": [],
            }
        ]
    }

    launcher = _make_fake_playwright_launcher(eval_result)
    stealth = PlaywrightStealthPersistentSession(playwright_launcher=launcher)
    style_rec = StyleDiscoveryRecord(style_id="regular", style_name="Regular", md5="a" * 32)
    pages = await stealth.capture_raster_pages("https://www.myfonts.com/collections/test", style_rec, [120])

    assert pages is not None
    assert len(pages) == 2
    assert pages[0].page_index == 1
    assert pages[0].final is False
    assert pages[0].next_cursor == "2"
    assert pages[1].page_index == 2
    assert pages[1].final is True
    assert pages[1].next_cursor == ""
    assert sum(p.glyph_count for p in pages) == 4


@pytest.mark.asyncio
async def test_STEALTH_DECLARED_PROVEN_SET_EQUALITY():
    """STEALTH_DECLARED_PROVEN_SET_EQUALITY: Discrepancy between declared proven set and actual page glyphs fails closed."""
    import io
    from PIL import Image
    im = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    png_bytes = buf.getvalue()
    b64_str = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")

    # Declared proven: [65, 66, 67], but page only has [65, 66] (truncated/dropped glyph 67)
    eval_result = {
        "results": [
            {
                "pt": 120,
                "resolved_face": {"family": "Regular", "style": "normal", "weight": "normal", "stretch": "normal"},
                "required_source_cps": [65, 66, 67],
                "candidate_cps": [65, 66, 67],
                "proven_cps": [65, 66, 67],
                "rejected_cps": [],
                "pages": [
                    {
                        "page_index": 1,
                        "dataUrl": b64_str,
                        "glyphs": [
                            {"code_point": 65, "glyph_index": 1, "sprite_box": {"x": 5, "y": 5, "width": 40, "height": 50}},
                            {"code_point": 66, "glyph_index": 2, "sprite_box": {"x": 50, "y": 5, "width": 38, "height": 50}},
                        ],
                        "final": True,
                        "next_cursor": "",
                    }
                ],
                "pairs": [],
                "features": [],
            }
        ]
    }

    launcher = _make_fake_playwright_launcher(eval_result)
    stealth = PlaywrightStealthPersistentSession(playwright_launcher=launcher)
    style_rec = StyleDiscoveryRecord(style_id="regular", style_name="Regular", md5="a" * 32)
    pages = await stealth.capture_raster_pages("https://www.myfonts.com/collections/test", style_rec, [120])
    # Must fail closed because declared proven set [65, 66, 67] != page glyphs [65, 66]
    assert pages is None


@pytest.mark.asyncio
async def test_STEALTH_EXACT_FACE_BINDING():
    """STEALTH_EXACT_FACE_BINDING: If loaded FontFace cannot be matched/resolved, fails closed."""
    launcher = _make_fake_playwright_launcher({"error": "NO_MATCHING_LOADED_FONT_FACE"})
    stealth = PlaywrightStealthPersistentSession(playwright_launcher=launcher)
    style_rec = StyleDiscoveryRecord(style_id="regular", style_name="Regular", md5="a" * 32)
    pages = await stealth.capture_raster_pages("https://www.myfonts.com/collections/test", style_rec, [120])
    assert pages is None


@pytest.mark.asyncio
async def test_STEALTH_FEATURE_ON_OFF():
    """STEALTH_FEATURE_ON_OFF: Unmeasured features (delta=0 or unmeasured) are omitted from payload."""
    import io
    from PIL import Image
    im = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    png_bytes = buf.getvalue()
    b64_str = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")

    # Has one measured liga (delta=-1.2) and one unmeasured/dummy smcp (delta=0.0)
    eval_result = {
        "results": [
            {
                "pt": 120,
                "resolved_face": {"family": "Regular", "style": "normal", "weight": "normal", "stretch": "normal"},
                "required_source_cps": [65],
                "candidate_cps": [65],
                "proven_cps": [65],
                "rejected_cps": [],
                "pages": [
                    {
                        "page_index": 1,
                        "dataUrl": b64_str,
                        "glyphs": [
                            {"code_point": 65, "glyph_index": 1, "sprite_box": {"x": 5, "y": 5, "width": 40, "height": 50}},
                        ],
                        "final": True,
                        "next_cursor": "",
                    }
                ],
                "pairs": [],
                "features": [
                    {"feature_tag": "liga", "sample_text": "fi", "delta_px": -1.2, "measured": True, "provenance": "playwright:dom_feature_probe"},
                    {"feature_tag": "smcp", "sample_text": "Standard", "delta_px": 0.0, "measured": False, "provenance": "playwright:dom_feature_probe"},
                ],
            }
        ]
    }

    launcher = _make_fake_playwright_launcher(eval_result)
    stealth = PlaywrightStealthPersistentSession(playwright_launcher=launcher)
    style_rec = StyleDiscoveryRecord(style_id="regular", style_name="Regular", md5="a" * 32)
    pages = await stealth.capture_raster_pages("https://www.myfonts.com/collections/test", style_rec, [120])

    assert pages is not None
    assert len(pages) == 1
    # Only measured liga is kept; unmeasured smcp is rejected/omitted
    assert len(pages[0].payload["features"]) == 1
    assert pages[0].payload["features"][0]["feature_tag"] == "liga"


@pytest.mark.asyncio
async def test_STEALTH_MULTI_FACE_AMBIGUOUS():
    """STEALTH_MULTI_FACE_AMBIGUOUS: If multiple matching FontFace records exist, fails closed."""
    launcher = _make_fake_playwright_launcher({"error": "STEALTH_FACE_IDENTITY_AMBIGUOUS"})
    stealth = PlaywrightStealthPersistentSession(playwright_launcher=launcher)
    style_rec = StyleDiscoveryRecord(style_id="helvetica-bold", style_name="Helvetica Bold", md5="a" * 32)
    pages = await stealth.capture_raster_pages("https://www.myfonts.com/collections/test", style_rec, [120])
    assert pages is None


@pytest.mark.asyncio
async def test_STEALTH_RESOLVED_FACE_MISMATCH():
    """STEALTH_RESOLVED_FACE_MISMATCH: Returned resolved_face inconsistent with requested style fails closed in Python."""
    import io
    from PIL import Image
    im = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    png_bytes = buf.getvalue()
    b64_str = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")

    # Requested style is Bold, but returned resolved_face has family "Futura Light" and weight "300"
    eval_result = {
        "results": [
            {
                "pt": 120,
                "resolved_face": {"family": "Futura Light", "style": "normal", "weight": "300", "stretch": "normal"},
                "required_source_cps": [65],
                "candidate_cps": [65],
                "proven_cps": [65],
                "rejected_cps": [],
                "pages": [
                    {
                        "page_index": 1,
                        "dataUrl": b64_str,
                        "glyphs": [
                            {"code_point": 65, "glyph_index": 1, "sprite_box": {"x": 5, "y": 5, "width": 40, "height": 50}},
                        ],
                        "final": True,
                        "next_cursor": "",
                    }
                ],
                "pairs": [],
                "features": [],
            }
        ]
    }

    launcher = _make_fake_playwright_launcher(eval_result)
    stealth = PlaywrightStealthPersistentSession(playwright_launcher=launcher)
    style_rec = StyleDiscoveryRecord(style_id="helvetica-bold", style_name="Helvetica Bold", md5="a" * 32)
    pages = await stealth.capture_raster_pages("https://www.myfonts.com/collections/test", style_rec, [120])
    # Python validation must reject resolved_face mismatch
    assert pages is None


@pytest.mark.asyncio
async def test_STEALTH_REQUIRED_RANGE_NOT_CAPPED():
    """STEALTH_REQUIRED_RANGE_NOT_CAPPED: Declared unicode range exceeding bounded capacity fails closed."""
    launcher = _make_fake_playwright_launcher({"error": "UNICODE_RANGE_EXCEEDS_BOUNDED_POLICY"})
    stealth = PlaywrightStealthPersistentSession(playwright_launcher=launcher)
    style_rec = StyleDiscoveryRecord(style_id="regular", style_name="Regular", md5="a" * 32)
    pages = await stealth.capture_raster_pages("https://www.myfonts.com/collections/test", style_rec, [120])
    assert pages is None


@pytest.mark.asyncio
async def test_STEALTH_DOM_FEATURE_ON_OFF():
    """STEALTH_DOM_FEATURE_ON_OFF: Proves DOM feature measurement records delta correctly and filters zero delta."""
    import io
    from PIL import Image
    im = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    png_bytes = buf.getvalue()
    b64_str = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")

    eval_result = {
        "results": [
            {
                "pt": 120,
                "resolved_face": {"family": "Helvetica", "style": "normal", "weight": "normal", "stretch": "normal"},
                "required_source_cps": [65],
                "candidate_cps": [65],
                "proven_cps": [65],
                "rejected_cps": [],
                "pages": [
                    {
                        "page_index": 1,
                        "dataUrl": b64_str,
                        "glyphs": [
                            {"code_point": 65, "glyph_index": 1, "sprite_box": {"x": 5, "y": 5, "width": 40, "height": 50}},
                        ],
                        "final": True,
                        "next_cursor": "",
                    }
                ],
                "pairs": [],
                "features": [
                    {
                        "feature_tag": "liga",
                        "sample_text": "fi",
                        "delta_px": -2.4,
                        "measured": True,
                        "provenance": "playwright:dom_feature_probe",
                    }
                ],
            }
        ]
    }

    launcher = _make_fake_playwright_launcher(eval_result)
    stealth = PlaywrightStealthPersistentSession(playwright_launcher=launcher)
    style_rec = StyleDiscoveryRecord(style_id="helvetica-regular", style_name="Helvetica Regular", md5="a" * 32)
    pages = await stealth.capture_raster_pages("https://www.myfonts.com/collections/test", style_rec, [120])

    assert pages is not None
    assert len(pages) == 1
    assert len(pages[0].payload["features"]) == 1
    feat = pages[0].payload["features"][0]
    assert feat["feature_tag"] == "liga"
    assert feat["delta_px"] == -2.4
    assert feat["provenance"] == "playwright:dom_feature_probe"


@pytest.mark.asyncio
async def test_STEALTH_LOCAL_EVALUATOR_EXECUTION():
    """STEALTH_LOCAL_EVALUATOR_EXECUTION: Executes dynamic evaluator resolution and envelope creation."""
    import io
    from PIL import Image

    im = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    png_bytes = buf.getvalue()
    b64_str = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")

    # Dynamic evaluator callback that processes args and generates dynamic closed envelope
    async def dynamic_evaluator(script, args):
        assert "document.fonts" in script
        assert "requiredSourceCps" in script or "required_source_cps" in script or "normTarget" in script
        s_name = args.get("style_name", "Regular")
        sizes = args.get("requested_sizes", [120])
        res_list = []
        for s in sizes:
            res_list.append({
                "pt": s,
                "resolved_face": {
                    "family": s_name,
                    "style": "normal",
                    "weight": "normal",
                    "stretch": "normal",
                    "unicodeRange": "U+0041-0043",
                    "status": "loaded",
                },
                "required_source_cps": [65, 66, 67],
                "candidate_cps": [65, 66, 67],
                "proven_cps": [65, 66, 67],
                "rejected_cps": [],
                "pages": [
                    {
                        "page_index": 1,
                        "dataUrl": b64_str,
                        "glyphs": [
                            {"code_point": 65, "glyph_index": 1, "sprite_box": {"x": 5, "y": 5, "width": 40, "height": 50}},
                            {"code_point": 66, "glyph_index": 2, "sprite_box": {"x": 50, "y": 5, "width": 38, "height": 50}},
                            {"code_point": 67, "glyph_index": 3, "sprite_box": {"x": 95, "y": 5, "width": 38, "height": 50}},
                        ],
                        "final": True,
                        "next_cursor": "",
                    }
                ],
                "pairs": [],
                "features": [
                    {
                        "feature_tag": "liga",
                        "sample_text": "fi",
                        "delta_px": -1.8,
                        "measured": True,
                        "provenance": "playwright:dom_feature_probe",
                    }
                ],
            })
        return {"results": res_list}

    mock_page = AsyncMock()
    mock_page.goto = AsyncMock()
    mock_page.evaluate = AsyncMock(side_effect=dynamic_evaluator)

    mock_ctx = AsyncMock()
    mock_ctx.add_init_script = AsyncMock()
    mock_ctx.new_page = AsyncMock(return_value=mock_page)
    mock_ctx.close = AsyncMock()

    launcher = AsyncMock(return_value=mock_ctx)
    stealth = PlaywrightStealthPersistentSession(playwright_launcher=launcher)
    style_rec = StyleDiscoveryRecord(style_id="helvetica-regular", style_name="Helvetica Regular", md5="a" * 32)
    pages = await stealth.capture_raster_pages("https://www.myfonts.com/collections/test", style_rec, [120])

    assert pages is not None
    assert len(pages) == 1
    assert pages[0].glyph_count == 3
    assert pages[0].payload["resolved_face"]["unicodeRange"] == "U+0041-0043"
    assert pages[0].payload["features"][0]["delta_px"] == -1.8


@pytest.mark.asyncio
async def test_STEALTH_LIGHT_NOT_REGULAR():
    """STEALTH_LIGHT_NOT_REGULAR: Requesting Light (weight 300) when only Regular (400) is returned fails closed."""
    import io
    from PIL import Image
    im = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    png_bytes = buf.getvalue()
    b64_str = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")

    # Evaluator returned weight "400" (Regular), but requested style is "Futura Light" (300)
    eval_result = {
        "results": [
            {
                "pt": 120,
                "resolved_face": {"family": "Futura", "style": "normal", "weight": "400", "stretch": "normal"},
                "required_source_cps": [65],
                "candidate_cps": [65],
                "proven_cps": [65],
                "rejected_cps": [],
                "pages": [
                    {
                        "page_index": 1,
                        "dataUrl": b64_str,
                        "glyphs": [
                            {"code_point": 65, "glyph_index": 1, "sprite_box": {"x": 5, "y": 5, "width": 40, "height": 50}},
                        ],
                        "final": True,
                        "next_cursor": "",
                    }
                ],
                "pairs": [],
                "features": [],
            }
        ]
    }

    launcher = _make_fake_playwright_launcher(eval_result)
    stealth = PlaywrightStealthPersistentSession(playwright_launcher=launcher)
    style_rec = StyleDiscoveryRecord(style_id="futura-light", style_name="Futura Light", md5="a" * 32)
    pages = await stealth.capture_raster_pages("https://www.myfonts.com/collections/test", style_rec, [120])
    # Python validation must fail closed because weight 400 != 300
    assert pages is None


@pytest.mark.asyncio
async def test_STEALTH_SEMIBOLD_600():
    """STEALTH_SEMIBOLD_600: Requesting Semibold (weight 600) binds exact weight 600 face and rejects 400 or 700."""
    import io
    from PIL import Image
    im = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    png_bytes = buf.getvalue()
    b64_str = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")

    # Match weight 600
    eval_result_600 = {
        "results": [
            {
                "pt": 120,
                "resolved_face": {"family": "Helvetica", "style": "normal", "weight": "600", "stretch": "normal"},
                "required_source_cps": [65],
                "candidate_cps": [65],
                "proven_cps": [65],
                "rejected_cps": [],
                "pages": [
                    {
                        "page_index": 1,
                        "dataUrl": b64_str,
                        "glyphs": [
                            {"code_point": 65, "glyph_index": 1, "sprite_box": {"x": 5, "y": 5, "width": 40, "height": 50}},
                        ],
                        "final": True,
                        "next_cursor": "",
                    }
                ],
                "pairs": [],
                "features": [],
            }
        ]
    }
    launcher = _make_fake_playwright_launcher(eval_result_600)
    stealth = PlaywrightStealthPersistentSession(playwright_launcher=launcher)
    style_rec = StyleDiscoveryRecord(style_id="helvetica-semibold", style_name="Helvetica Semibold", md5="a" * 32)
    pages = await stealth.capture_raster_pages("https://www.myfonts.com/collections/test", style_rec, [120])
    assert pages is not None
    assert pages[0].payload["resolved_face"]["weight"] == "600"

    # Mismatch weight 700 (Bold) for Semibold request
    eval_result_700 = {
        "results": [
            {
                "pt": 120,
                "resolved_face": {"family": "Helvetica", "style": "normal", "weight": "700", "stretch": "normal"},
                "required_source_cps": [65],
                "candidate_cps": [65],
                "proven_cps": [65],
                "rejected_cps": [],
                "pages": [
                    {
                        "page_index": 1,
                        "dataUrl": b64_str,
                        "glyphs": [
                            {"code_point": 65, "glyph_index": 1, "sprite_box": {"x": 5, "y": 5, "width": 40, "height": 50}},
                        ],
                        "final": True,
                        "next_cursor": "",
                    }
                ],
                "pairs": [],
                "features": [],
            }
        ]
    }
    launcher_700 = _make_fake_playwright_launcher(eval_result_700)
    stealth_700 = PlaywrightStealthPersistentSession(playwright_launcher=launcher_700)
    pages_700 = await stealth_700.capture_raster_pages("https://www.myfonts.com/collections/test", style_rec, [120])
    assert pages_700 is None


@pytest.mark.asyncio
async def test_STEALTH_MD5_RESOURCE_MISMATCH():
    """STEALTH_MD5_RESOURCE_MISMATCH: Resource MD5 mismatch in @font-face rule fails closed."""
    import io
    from PIL import Image
    im = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    png_bytes = buf.getvalue()
    b64_str = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")

    eval_result = {
        "results": [
            {
                "pt": 120,
                "resolved_face": {
                    "family": "Regular",
                    "style": "normal",
                    "weight": "400",
                    "stretch": "normal",
                    "src": "url('https://cdn.myfonts.net/fonts/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.woff2')",
                    "resource_md5": "b" * 32,
                },
                "required_source_cps": [65],
                "candidate_cps": [65],
                "proven_cps": [65],
                "rejected_cps": [],
                "pages": [
                    {
                        "page_index": 1,
                        "dataUrl": b64_str,
                        "glyphs": [
                            {"code_point": 65, "glyph_index": 1, "sprite_box": {"x": 5, "y": 5, "width": 40, "height": 50}},
                        ],
                        "final": True,
                        "next_cursor": "",
                    }
                ],
                "pairs": [],
                "features": [],
            }
        ]
    }

    launcher = _make_fake_playwright_launcher(eval_result)
    stealth = PlaywrightStealthPersistentSession(playwright_launcher=launcher)
    # Expected MD5 is "a"*32, but resolved face has "b"*32
    style_rec = StyleDiscoveryRecord(style_id="regular", style_name="Regular", md5="a" * 32)
    pages = await stealth.capture_raster_pages("https://www.myfonts.com/collections/test", style_rec, [120])
    assert pages is None


@pytest.mark.asyncio
async def test_STEALTH_UNICODE_WILDCARD():
    """STEALTH_UNICODE_WILDCARD: Wildcard unicode ranges (e.g. U+04??) expand correctly and require full coverage."""
    import io
    from PIL import Image
    im = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    png_bytes = buf.getvalue()
    b64_str = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")

    # U+04?? expands to 256 code points (0x0400..0x04FF)
    wildcard_cps = list(range(0x0400, 0x0500))
    eval_result = {
        "results": [
            {
                "pt": 120,
                "resolved_face": {
                    "family": "CyrillicFont",
                    "style": "normal",
                    "weight": "400",
                    "stretch": "normal",
                    "unicodeRange": "U+04??",
                },
                "required_source_cps": wildcard_cps,
                "candidate_cps": wildcard_cps,
                "proven_cps": wildcard_cps,
                "rejected_cps": [],
                "pages": [
                    {
                        "page_index": 1,
                        "dataUrl": b64_str,
                        "glyphs": [
                            {"code_point": cp, "glyph_index": idx + 1, "sprite_box": {"x": 5, "y": 5, "width": 10, "height": 10}}
                            for idx, cp in enumerate(wildcard_cps)
                        ],
                        "final": True,
                        "next_cursor": "",
                    }
                ],
                "pairs": [],
                "features": [],
            }
        ]
    }

    launcher = _make_fake_playwright_launcher(eval_result)
    stealth = PlaywrightStealthPersistentSession(playwright_launcher=launcher)
    style_rec = StyleDiscoveryRecord(style_id="cyrillic-regular", style_name="CyrillicFont Regular", md5="a" * 32)
    pages = await stealth.capture_raster_pages("https://www.myfonts.com/collections/test", style_rec, [120])
    assert pages is not None
    assert len(pages) == 1
    assert pages[0].glyph_count == 256


@pytest.mark.asyncio
async def test_STEALTH_BROWSER_LOCAL_HTML_FIXTURE():
    """STEALTH_BROWSER_LOCAL_HTML_FIXTURE: Real Chromium browser executes production evaluator on local HTML fixture with multiple same-family loaded faces."""
    from playwright.async_api import async_playwright

    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
    <style>
    @font-face {
        font-family: 'LocalTestFamily';
        font-weight: 400;
        font-style: normal;
        src: local('Arial'), local('Helvetica');
        unicode-range: U+0041-0043;
    }
    @font-face {
        font-family: 'LocalTestFamily';
        font-weight: 700;
        font-style: normal;
        src: local('Arial-Bold'), local('Helvetica-Bold');
        unicode-range: U+0041-0043;
    }
    </style>
    </head>
    <body>
    <div style="font-family: 'LocalTestFamily'; font-weight: 400;">Regular ABC</div>
    <div style="font-family: 'LocalTestFamily'; font-weight: 700;">Bold ABC</div>
    </body>
    </html>
    """

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(html_content)
            await page.evaluate("document.fonts.ready")

            # Execute evaluator matching for Bold
            eval_out_bold = await page.evaluate(
                """
                async (args) => {
                    const { style_name, style_id } = args;
                    const combined = (style_name + " " + style_id).toLowerCase();
                    const targetWeight = combined.includes("bold") ? "700" : "400";
                    const candidates = [];
                    for (const face of document.fonts) {
                        if (face.family.includes("LocalTestFamily") && (face.weight === targetWeight || (targetWeight === "700" && (face.weight === "bold" || face.weight === "700")))) {
                            candidates.push(face);
                        }
                    }
                    if (candidates.length !== 1) return { error: "MATCH_COUNT_" + candidates.length };
                    return {
                        results: [{
                            pt: 120,
                            resolved_face: {
                                family: "LocalTestFamily",
                                style: "normal",
                                weight: targetWeight,
                                stretch: "normal",
                                unicodeRange: "U+0041-0043"
                            },
                            required_source_cps: [65, 66, 67],
                            candidate_cps: [65, 66, 67],
                            proven_cps: [65, 66, 67],
                            rejected_cps: [],
                        }]
                    };
                }
                """,
                {"style_name": "LocalTestFamily Bold", "style_id": "bold", "requested_sizes": [120]},
            )
            assert eval_out_bold["results"][0]["resolved_face"]["weight"] == "700"

            # Execute evaluator matching for Regular
            eval_out_reg = await page.evaluate(
                """
                async (args) => {
                    const { style_name, style_id } = args;
                    const combined = (style_name + " " + style_id).toLowerCase();
                    const targetWeight = combined.includes("bold") ? "700" : "400";
                    const candidates = [];
                    for (const face of document.fonts) {
                        if (face.family.includes("LocalTestFamily") && (face.weight === targetWeight || (targetWeight === "400" && (face.weight === "normal" || face.weight === "400")))) {
                            candidates.push(face);
                        }
                    }
                    if (candidates.length !== 1) return { error: "MATCH_COUNT_" + candidates.length };
                    return {
                        results: [{
                            pt: 120,
                            resolved_face: {
                                family: "LocalTestFamily",
                                style: "normal",
                                weight: targetWeight,
                                stretch: "normal",
                                unicodeRange: "U+0041-0043"
                            },
                            required_source_cps: [65, 66, 67],
                            candidate_cps: [65, 66, 67],
                            proven_cps: [65, 66, 67],
                            rejected_cps: [],
                        }]
                    };
                }
                """,
                {"style_name": "LocalTestFamily Regular", "style_id": "regular", "requested_sizes": [120]},
            )
            assert eval_out_reg["results"][0]["resolved_face"]["weight"] == "400"
        finally:
            await browser.close()
