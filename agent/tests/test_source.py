"""Tests for source acquisition, live HTML/image preview resolution, and raster extraction."""
import io
import httpx
import pytest
from PIL import Image, ImageDraw

from compute.models import ClaimStyle
from compute.source import (
    SourceAcquirer,
    extract_contours_from_raster_image,
    extract_preview_url_from_html,
    validate_myfonts_url,
)


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
        payload = await acquirer.acquire_source("https://www.myfonts.com/collections/roboto-flex", styles)
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
            await acquirer.acquire_source("https://www.myfonts.com/collections/roboto-flex", styles)

    # 2. Blocked page (403) fails closed (SOURCE_ACQUISITION_BLOCKED)
    def handler_blocked(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Forbidden", headers={"content-type": "text/html"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_blocked)) as http_client:
        acquirer = SourceAcquirer(client=http_client)
        with pytest.raises(ValueError, match="SOURCE_ACQUISITION_BLOCKED"):
            await acquirer.acquire_source("https://www.myfonts.com/collections/roboto-flex", styles)


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
