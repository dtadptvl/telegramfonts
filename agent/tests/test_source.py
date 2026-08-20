"""Tests for source acquisition, URL validation, and preview parsing."""
import httpx
import pytest

from compute.source import SourceAcquirer, validate_myfonts_url
from worker_client import ClaimStyle


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
async def test_source_acquirer_distinct_fixture_inputs():
    acquirer = SourceAcquirer()

    fixture_1 = {
        "source_url": "https://www.myfonts.com/collections/roboto-flex",
        "family_name": "Roboto Flex",
        "styles": [
            {
                "style_id": "reg",
                "style_name": "Regular",
                "glyphs": {
                    ".notdef": {"contours": [[(50, 0), (50, 500), (250, 500), (250, 0)]], "advance_width": 300, "lsb": 50},
                    "A": {"contours": [[(100, 0), (100, 700), (500, 700), (500, 0)]], "advance_width": 600, "lsb": 100},
                },
            }
        ],
    }

    fixture_2 = {
        "source_url": "https://www.myfonts.com/collections/roboto-flex",
        "family_name": "Roboto Flex",
        "styles": [
            {
                "style_id": "reg",
                "style_name": "Regular",
                "glyphs": {
                    ".notdef": {"contours": [[(50, 0), (50, 600), (350, 600), (350, 0)]], "advance_width": 400, "lsb": 50},
                    "A": {"contours": [[(50, 0), (50, 800), (700, 800), (700, 0)]], "advance_width": 800, "lsb": 50},
                },
            }
        ],
    }

    p1 = acquirer.from_fixture(fixture_1)
    p2 = acquirer.from_fixture(fixture_2)

    # Two distinct fixture contents for the same URL/style produce distinct source glyph data (BLOCK B)
    g1 = p1.styles["reg"].glyphs["A"]
    g2 = p2.styles["reg"].glyphs["A"]
    assert g1.advance_width != g2.advance_width
    assert g1.contours != g2.contours


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

    # Malformed fixture fails closed
    with pytest.raises(ValueError, match="MALFORMED_SOURCE_INPUT"):
        acquirer.from_fixture({"source_url": "https://invalid-site.com"})

    with pytest.raises(ValueError, match="MALFORMED_SOURCE_INPUT"):
        acquirer.from_fixture({
            "source_url": "https://www.myfonts.com/collections/valid",
            "styles": [{"style_id": "s1", "style_name": "S1", "glyphs": {}}],  # empty glyphs
        })
