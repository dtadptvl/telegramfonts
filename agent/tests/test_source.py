"""Tests for source acquisition, preview raster/vector reconstruction, and URL validation."""
import io
import httpx
import pytest
from PIL import Image, ImageDraw

from compute.models import ClaimStyle
from compute.source import (
    SourceAcquirer,
    extract_contours_from_raster_image,
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


@pytest.mark.asyncio
async def test_source_acquirer_network_limits():
    acquirer = SourceAcquirer(timeout=5.0)
    styles = [ClaimStyle(id="reg", display_name="Regular")]

    # 1. Payload too large (>10MB)
    def handler_large(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"A" * (11 * 1024 * 1024), headers={"content-type": "text/html"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_large)) as http_client:
        with pytest.raises(ValueError, match="SOURCE_PAYLOAD_TOO_LARGE"):
            await acquirer.acquire_source("https://www.myfonts.com/collections/roboto", styles, client=http_client)

    # 2. Unsupported content-type
    def handler_bad_type(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"content", headers={"content-type": "application/x-executable"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_bad_type)) as http_client:
        with pytest.raises(ValueError, match="UNSUPPORTED_CONTENT_TYPE"):
            await acquirer.acquire_source("https://www.myfonts.com/collections/roboto", styles, client=http_client)


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
            "styles": [{"style_id": "s1", "style_name": "S1", "glyphs": {}}],  # empty glyphs
        })
