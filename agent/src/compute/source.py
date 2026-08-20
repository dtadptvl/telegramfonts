"""Source font data acquisition and validation."""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse
import httpx

logger = logging.getLogger("telegramfonts.agent.source")

MYFONTS_URL_PATTERN = re.compile(
    r"^https://(www\.)?myfonts\.com/[a-zA-Z0-9_\-\./]+$", re.IGNORECASE
)
MAX_SOURCE_BYTES = 10 * 1024 * 1024  # 10 MB limit


def validate_myfonts_url(url: str) -> bool:
    if not url or not isinstance(url, str):
        return False
    clean = url.strip()
    if not MYFONTS_URL_PATTERN.match(clean):
        return False

    parsed = urlparse(clean)
    if parsed.scheme != "https":
        return False
    if parsed.hostname not in ("www.myfonts.com", "myfonts.com"):
        return False
    return True


class SourceAcquirer:
    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout

    async def acquire_source(
        self, source_url: str, client: httpx.AsyncClient | None = None
    ) -> dict[str, str]:
        """Validate and acquire font source context."""
        if not validate_myfonts_url(source_url):
            raise ValueError("INVALID_SOURCE_URL")

        # In offline / test mode or bounded fetch:
        # Live acquisition can query public preview metadata
        # Return structured metadata context
        parsed = urlparse(source_url.strip())
        path_parts = [p for p in parsed.path.split("/") if p]
        family_slug = path_parts[-1] if path_parts else "custom-font"

        return {
            "source_url": source_url.strip(),
            "family_slug": family_slug,
        }
