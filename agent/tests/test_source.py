"""Tests for source acquisition and URL validation."""
import pytest

from compute.source import SourceAcquirer, validate_myfonts_url


def test_validate_myfonts_url():
    # Valid canonical MyFonts URLs
    assert validate_myfonts_url("https://www.myfonts.com/collections/roboto-flex") is True
    assert validate_myfonts_url("https://myfonts.com/collections/helvetica-now") is True
    assert validate_myfonts_url("https://www.myfonts.com/fonts/foundry/family-name") is True

    # Invalid / off-domain / insecure URLs
    assert validate_myfonts_url("http://www.myfonts.com/collections/roboto") is False  # Insecure HTTP
    assert validate_myfonts_url("https://evil.com/collections/roboto") is False
    assert validate_myfonts_url("https://myfonts.com.evil.com/font") is False
    assert validate_myfonts_url("not-a-url") is False
    assert validate_myfonts_url("") is False


@pytest.mark.asyncio
async def test_source_acquirer():
    acquirer = SourceAcquirer()

    # Valid URL
    res = await acquirer.acquire_source("https://www.myfonts.com/collections/roboto-flex")
    assert res["family_slug"] == "roboto-flex"

    # Invalid URL raises ValueError
    with pytest.raises(ValueError, match="INVALID_SOURCE_URL"):
        await acquirer.acquire_source("https://attacker.com/malicious")
