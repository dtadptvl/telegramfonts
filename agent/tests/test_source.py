"""Tests for source acquisition, live HTML/image preview resolution, and raster extraction."""
import hashlib
import io
import httpx
import pytest
import shutil
from pathlib import Path
from PIL import Image, ImageDraw

from compute.models import ClaimStyle
from compute.source import (
    SourceAcquirer,
    extract_contours_from_raster_image,
    extract_preview_url_from_html,
    validate_myfonts_url,
)
from measurement.models import BrowserFontSelection, DirectMetrics, ObservationConfig


class FakeObservableBrowser:
    """Observable-source test double with no binary injection surface."""

    browser_version = "Chromium/Test"

    def __init__(self) -> None:
        self.start_count = 0
        self.observe_count = 0
        self.closed = False

    async def start(self) -> None:
        self.start_count += 1

    async def observe_source_font(self, source_url: str, style_name: str, family_name: str):
        self.observe_count += 1
        return BrowserFontSelection(family=family_name, weight="400")

    async def is_glyph_supported_in_font(self, font, code_point: int) -> bool:
        return code_point in {65, 66, 79}

    async def measure_glyph_direct(self, font_family, code_point: int, font_size_px=200.0, upem=1000):
        return DirectMetrics.from_browser_measurements(
            code_point,
            chr(code_point),
            font_size_px,
            {
                "width": font_size_px * 0.6,
                "actualBoundingBoxLeft": 0.0,
                "actualBoundingBoxRight": font_size_px * 0.55,
                "actualBoundingBoxAscent": font_size_px * 0.7,
                "actualBoundingBoxDescent": font_size_px * 0.1,
                "fontBoundingBoxAscent": font_size_px * 0.8,
                "fontBoundingBoxDescent": font_size_px * 0.2,
            },
            upem,
        )

    async def capture_lossless_raster(self, font_family, code_point: int, resolution_px: int, subpixel_offset=(0.0, 0.0)):
        return _make_test_image_bytes(20, 60)

    async def measure_text_advance(self, font, text: str, font_size_px=200.0, upem=1000):
        return len(text) * 600.0

    async def probe_opentype_feature(self, font, feature_tag: str, sample_text: str, font_size_px=200.0, upem=1000):
        return {
            "enabled_advance_upem": 1000.0,
            "disabled_advance_upem": 1000.0,
            "enabled_raster_signature": f"{feature_tag}-on",
            "disabled_raster_signature": f"{feature_tag}-off",
        }

    def close(self) -> None:
        self.closed = True


def _make_test_image_bytes(stroke_x0: int, stroke_x1: int) -> bytes:
    img = Image.new("L", (100, 100), color=255)
    draw = ImageDraw.Draw(img)
    draw.rectangle([stroke_x0, 10, stroke_x1, 90], fill=0)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_validate_myfonts_url():
    # Valid canonical MyFonts URLs
    assert validate_myfonts_url("https://www.myfonts.com/collections/roboto-flex") is True
    assert validate_myfonts_url("https://myfonts.com/collections/helvetica-now") is True
    assert validate_myfonts_url("https://www.myfonts.com/fonts/foundry/family-name") is True

    # Invalid / off-domain / sibling / insecure URLs (BLOCK C)
    assert validate_myfonts_url("http://www.myfonts.com/collections/roboto") is False
    assert validate_myfonts_url("https://evilmyfonts.com/collections/roboto") is False
    assert validate_myfonts_url("https://myfonts.com.evil.com/font") is False
    assert validate_myfonts_url("not-a-url") is False
    assert validate_myfonts_url("") is False


def test_extract_preview_url_from_html():
    # 1. OpenGraph meta tag
    html_og = '<meta property="og:image" content="https://www.myfonts.com/preview_og.png">'
    assert extract_preview_url_from_html(html_og) == "https://www.myfonts.com/preview_og.png"

    # 2. Preview img tag
    html_img = '<img class="font-preview-render" src="https://www.myfonts.com/preview_render.png" />'
    assert extract_preview_url_from_html(html_img) == "https://www.myfonts.com/preview_render.png"

    # 3. No preview
    html_none = "<html><body><h1>Sample Page</h1><p>No preview here</p></body></html>"
    assert extract_preview_url_from_html(html_none) is None


@pytest.mark.asyncio
async def test_live_preview_fetch_via_html_page():
    preview_img_bytes = _make_test_image_bytes(20, 60)
    styles = [ClaimStyle(id="reg", display_name="Regular")]

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str == "https://www.myfonts.com/collections/roboto-flex":
            html = '<meta property="og:image" content="https://www.myfonts.com/img/sample.png">'
            return httpx.Response(200, text=html, headers={"content-type": "text/html"})
        if url_str == "https://www.myfonts.com/img/sample.png":
            return httpx.Response(200, content=preview_img_bytes, headers={"content-type": "image/png"})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        acquirer = SourceAcquirer(client=http_client)
        payload = await acquirer.acquire_source(
            "https://www.myfonts.com/collections/roboto-flex", styles, allow_web_fallback=True
        )
        assert payload.family_name == "Roboto Flex"
        assert "reg" in payload.styles
        assert len(payload.styles["reg"].glyphs["A"].contours) > 0


@pytest.mark.asyncio
async def test_live_preview_missing_or_blocked_fails_closed():
    styles = [ClaimStyle(id="reg", display_name="Regular")]

    # 1. Page with no preview fails closed (NO_PUBLIC_PREVIEW_FOUND)
    def handler_no_preview(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>No preview</body></html>", headers={"content-type": "text/html"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_no_preview)) as http_client:
        acquirer = SourceAcquirer(client=http_client)
        with pytest.raises(ValueError, match="NO_PUBLIC_PREVIEW_FOUND"):
            await acquirer.acquire_source(
                "https://www.myfonts.com/collections/roboto-flex", styles, allow_web_fallback=True
            )

    # 2. Blocked page (403) fails closed (SOURCE_ACQUISITION_BLOCKED)
    def handler_blocked(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Forbidden", headers={"content-type": "text/html"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_blocked)) as http_client:
        acquirer = SourceAcquirer(client=http_client)
        with pytest.raises(ValueError, match="SOURCE_ACQUISITION_BLOCKED"):
            await acquirer.acquire_source(
                "https://www.myfonts.com/collections/roboto-flex", styles, allow_web_fallback=True
            )


@pytest.mark.asyncio
async def test_production_cache_miss_collects_persists_and_reconstructs_without_font_binary(tmp_path: Path):
    styles = [ClaimStyle(id="reg", display_name="Regular")]
    browser = FakeObservableBrowser()
    config = ObservationConfig(
        resolutions=(32,),
        base_subpixel_phases=((0.0, 0.0),),
        expanded_subpixel_phases=((0.0, 0.0),),
        metric_sizes_px=(32.0, 64.0),
        feature_probes=(("kern", "AV"),),
    )
    acquirer = SourceAcquirer(
        observation_store_dir=tmp_path / "runtime_observations",
        browser_session_factory=lambda: browser,
        observation_config=config,
    )

    payload = await acquirer.acquire_source(
        "https://www.myfonts.com/collections/unknown-font", styles
    )

    assert set(payload.styles["reg"].reconstructed_glyphs) == {65, 66, 79}
    assert browser.observe_count == 1
    cfg_h = acquirer.observation_config.compute_hash()
    bv = browser.browser_version
    assert len(acquirer.store.get_metric_observations("unknown_font", "reg")) == 6
    assert len(acquirer.store.get_pair_observations("unknown_font", "reg", browser_version=bv, config_hash=cfg_h)) > 0
    assert len(acquirer.store.get_feature_observations("unknown_font", "reg", browser_version=bv, config_hash=cfg_h)) == 1
    assert not hasattr(browser, "load_font_data")


@pytest.mark.asyncio
async def test_production_acquire_source_known_store_hit_zero_http_calls(tmp_path: Path):
    """Verify that a known store hit makes exactly 0 HTTP requests (REQ2 zero-recrawl preservation)."""
    http_call_count = 0

    def fail_on_http(request: httpx.Request) -> httpx.Response:
        nonlocal http_call_count
        http_call_count += 1
        raise AssertionError(f"Unexpected HTTP request made to {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail_on_http)) as http_client:
        fixture_store = tmp_path / "benchmark_fixture"
        fixture_store.mkdir()
        shutil.copy2("observations/benchmark/index.sqlite3", fixture_store / "index.sqlite3")
        acquirer = SourceAcquirer(
            client=http_client,
            observation_store_dir=fixture_store,
        )
        cfg_h = acquirer.observation_config.compute_hash()
        bv = "chromium"
        bv_hash = hashlib.sha256(bv.encode("utf-8")).hexdigest()
        shutil.copy2(
            "observations/benchmark/reconstructed_be_vietnam_pro_regular.pkl",
            fixture_store / f"reconstructed_be_vietnam_pro_regular_{bv_hash}_{cfg_h}.pkl",
        )
        with acquirer.store._get_connection() as conn:
            conn.execute(
                """
                UPDATE unicode_coverage SET browser_version = ?, config_hash = ?
                WHERE reference_id = 'be_vietnam_pro' AND style_id = 'regular'
                """,
                (bv, cfg_h),
            )
            conn.commit()

        acquirer.store.record_source_collection_completed(
            reference_id="be_vietnam_pro",
            style_id="regular",
            config_hash=cfg_h,
            browser_version=bv,
        )
        styles = [ClaimStyle(id="regular", display_name="Regular")]
        payload = await acquirer.acquire_source(
            "https://www.myfonts.com/collections/be-vietnam-pro",
            styles,
        )
        assert payload.family_name == "Be Vietnam Pro"
        assert "regular" in payload.styles
        assert len(payload.styles["regular"].reconstructed_glyphs) > 0
        assert http_call_count == 0


@pytest.mark.asyncio
async def test_completed_source_collection_reuses_cache_without_browser_recrawl(tmp_path: Path):
    browser = FakeObservableBrowser()
    config = ObservationConfig(
        resolutions=(32,),
        base_subpixel_phases=((0.0, 0.0),),
        expanded_subpixel_phases=((0.0, 0.0),),
        metric_sizes_px=(32.0,),
        feature_probes=(("kern", "AV"),),
    )
    store_dir = tmp_path / "runtime_observations"
    styles = [ClaimStyle(id="regular", display_name="Regular")]
    first = SourceAcquirer(
        observation_store_dir=store_dir,
        browser_session_factory=lambda: browser,
        observation_config=config,
    )
    await first.acquire_source("https://www.myfonts.com/collections/cache-font", styles)

    def fail_if_recrawled():
        raise AssertionError("completed source was recrawled")

    second = SourceAcquirer(
        observation_store_dir=store_dir,
        browser_session_factory=fail_if_recrawled,
        observation_config=config,
    )
    payload = await second.acquire_source(
        "https://www.myfonts.com/collections/cache-font", styles
    )
    assert len(payload.styles["regular"].reconstructed_glyphs) == 3
    assert second.last_cache_hit is True


@pytest.mark.asyncio
async def test_distinct_preview_contents_produce_distinct_glyphs_with_same_url():
    acquirer = SourceAcquirer()
    styles = [ClaimStyle(id="reg", display_name="Regular")]
    url = "https://www.myfonts.com/collections/roboto-flex"

    preview_img_1 = _make_test_image_bytes(10, 30)  # Thin stroke
    preview_img_2 = _make_test_image_bytes(10, 80)  # Wide stroke

    # Same URL/style with two distinct preview contents (BLOCK B)
    p1 = await acquirer.acquire_source(url, styles, preview_input=preview_img_1)
    p2 = await acquirer.acquire_source(url, styles, preview_input=preview_img_2)

    g1 = p1.styles["reg"].glyphs["A"]
    g2 = p2.styles["reg"].glyphs["A"]
    assert g1.advance_width != g2.advance_width
    assert g1.contours != g2.contours


@pytest.mark.asyncio
async def test_changing_only_url_with_identical_content_produces_identical_glyphs():
    acquirer = SourceAcquirer()
    styles = [ClaimStyle(id="reg", display_name="Regular")]
    preview_img = _make_test_image_bytes(20, 50)

    url_1 = "https://www.myfonts.com/collections/roboto-flex"
    url_2 = "https://www.myfonts.com/collections/helvetica-now"

    p1 = await acquirer.acquire_source(url_1, styles, preview_input=preview_img)
    p2 = await acquirer.acquire_source(url_2, styles, preview_input=preview_img)

    # Identical content yields identical reconstructed glyph contours (no URL hashing)
    g1 = p1.styles["reg"].glyphs["A"]
    g2 = p2.styles["reg"].glyphs["A"]
    assert g1.advance_width == g2.advance_width
    assert g1.contours == g2.contours


def test_raster_preview_and_fixture_fail_closed():
    acquirer = SourceAcquirer()

    # Empty bytes fails closed
    with pytest.raises(ValueError, match="MALFORMED_SOURCE_INPUT"):
        acquirer.parse_raster_preview(b"")

    # Corrupt image bytes fails closed
    with pytest.raises(ValueError, match="MALFORMED_SOURCE_INPUT"):
        acquirer.parse_raster_preview(b"GARBAGE_NON_IMAGE_BYTES")

    with pytest.raises(ValueError, match="MALFORMED_SOURCE_INPUT"):
        extract_contours_from_raster_image(b"CORRUPT_BYTES")

    # Malformed fixture fails closed
    with pytest.raises(ValueError, match="MALFORMED_SOURCE_INPUT"):
        acquirer.from_fixture({"source_url": "https://invalid-site.com"})

    with pytest.raises(ValueError, match="MALFORMED_SOURCE_INPUT"):
        acquirer.from_fixture({
            "source_url": "https://www.myfonts.com/collections/valid",
            "styles": [{"style_id": "s1", "style_name": "S1", "glyphs": {}}],
        })


def test_extract_catalog_metadata_from_json_ld_and_html():
    from compute.source import extract_catalog_metadata_from_html

    # 1. JSON-LD schema parsing
    json_ld_html = """
    <html>
      <head>
        <meta property="og:title" content="Helvetica Now Font | Monotype | MyFonts">
        <meta name="author" content="Monotype">
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "Product",
          "name": "Helvetica Now",
          "hasVariant": [
            {"@type": "ProductModel", "name": "Helvetica Now Light", "price": 45},
            {"@type": "ProductModel", "name": "Helvetica Now Regular", "price": 50},
            {"@type": "ProductModel", "name": "Helvetica Now Bold", "price": 50}
          ]
        }
        </script>
      </head>
      <body></body>
    </html>
    """

    res = extract_catalog_metadata_from_html(json_ld_html, "https://www.myfonts.com/collections/helvetica-now-monotype")
    assert res["family_name"] == "Helvetica Now"
    assert res["foundry"] == "Monotype"
    assert len(res["styles"]) == 3
    assert res["styles"][0]["display_name"] == "Helvetica Now Light"
    # External JSON-LD price of 45 does NOT become 45 VND; app-defined VND price of 5000 is authoritative
    assert res["styles"][0]["price"] == 5000
    assert res["styles"][1]["id"] == "helvetica_now_regular"

    # 2. HTML data attributes parsing
    data_attr_html = """
    <html>
      <head>
        <title>Futura Now - Monotype</title>
      </head>
      <body>
        <div data-style-name="Futura Now Book"></div>
        <div data-style-name="Futura Now Bold"></div>
      </body>
    </html>
    """
    res2 = extract_catalog_metadata_from_html(data_attr_html, "https://www.myfonts.com/collections/futura-now")
    assert res2["family_name"] == "Futura Now"
    assert len(res2["styles"]) == 2
    assert res2["styles"][0]["id"] == "futura_now_book"
    assert res2["styles"][1]["id"] == "futura_now_bold"


def test_extract_catalog_metadata_collection_page_ignores_breadcrumbs():
    from compute.source import extract_catalog_metadata_from_html

    # HTML with both BreadcrumbList and CollectionPage with ItemList
    html = """
    <html>
      <head>
        <meta property="og:title" content="Neurath Mono Font | René Bieder | MyFonts">
        <meta name="author" content="René Bieder">
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "BreadcrumbList",
          "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Home", "item": "https://www.myfonts.com/"},
            {"@type": "ListItem", "position": 2, "name": "René Bieder", "item": "https://www.myfonts.com/foundry/rene-bieder/"},
            {"@type": "ListItem", "position": 3, "name": "Neurath Mono", "item": "https://www.myfonts.com/collections/neurath-mono-font-rene-bieder"}
          ]
        }
        </script>
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "CollectionPage",
          "name": "Neurath Mono",
          "mainEntity": {
            "@type": "ItemList",
            "itemListElement": [
              {"@type": "ListItem", "position": 1, "name": "Neurath Mono Thin", "item": {"@type": "Product", "name": "Neurath Mono Thin"}},
              {"@type": "ListItem", "position": 2, "name": "Neurath Mono Regular", "item": {"@type": "Product", "name": "Neurath Mono Regular"}},
              {"@type": "ListItem", "position": 3, "name": "Neurath Mono Bold", "item": {"@type": "Product", "name": "Neurath Mono Bold"}}
            ]
          }
        }
        </script>
      </head>
      <body></body>
    </html>
    """
    res = extract_catalog_metadata_from_html(html, "https://www.myfonts.com/collections/neurath-mono-font-rene-bieder")
    assert res["family_name"] == "Neurath Mono"
    assert res["foundry"] == "René Bieder"
    assert len(res["styles"]) == 3
    # Breadcrumbs (Home, René Bieder) must NOT appear in styles
    style_names = [s["display_name"] for s in res["styles"]]
    assert "Home" not in style_names
    assert "René Bieder" not in style_names
    assert "Neurath Mono Thin" in style_names
    assert "Neurath Mono Regular" in style_names
    assert "Neurath Mono Bold" in style_names


def test_extract_catalog_metadata_fails_closed_without_synthetic_styles():
    from compute.source import extract_catalog_metadata_from_html

    # HTML without any styles fails closed and does NOT fabricate synthetic styles
    empty_styles_html = "<html><head><title>Empty Font</title></head><body><p>No styles available</p></body></html>"
    with pytest.raises(ValueError, match="NO_CATALOG_STYLES_FOUND"):
        extract_catalog_metadata_from_html(empty_styles_html, "https://www.myfonts.com/collections/empty-font")

