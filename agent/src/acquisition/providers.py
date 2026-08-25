"""Injectable provider contracts and bounded default adapters.

No provider ever logs, raises, stores, or embeds secret/session material.
Session material flows through opaque callables and is consumed in-memory only.
"""
from __future__ import annotations

import base64
import re
from typing import Any, Awaitable, Callable, Protocol

from acquisition.models import BinaryAcquisitionPolicy, SpriteRasterPage


class DumpDomTransport(Protocol):
    """Primary capability: native Chrome `--headless=new --dump-dom` page dump."""

    async def dump_dom(self, url: str) -> str:
        """Return the serialized DOM for one URL using the headless engine."""


class AuthorizedSessionMaterialProvider(Protocol):
    """Supplies opaque authorized-session material without exposing it.

    Implementations must never return secrets in stringified/logged contexts;
    the material is consumed only by the authorized transport below.
    """

    async def material(self) -> dict[str, Any] | None:
        """Return opaque session material, or None when unavailable."""


class AuthorizedSessionTransport(Protocol):
    """Fallback capability: authorized persistent Chrome/session retrieval."""

    async def fetch_binary(self, url: str, session_material: dict[str, Any]) -> bytes | None:
        """Fetch font bytes through the authorized session, or None when absent."""


class AuthorizedRasterClient(Protocol):
    """Fallback capability: authorized Monotype raster endpoint."""

    async def fetch_sprite_page(self, request: dict[str, Any], cursor: str) -> SpriteRasterPage | None:
        """Fetch one bounded raster sprite page, or None when unavailable."""


class PersistentSessionBinaryProvider:
    """Authorized-session binary provider bound to injectable transports."""

    def __init__(
        self,
        session_material_provider: AuthorizedSessionMaterialProvider | None,
        transport: AuthorizedSessionTransport | None,
    ) -> None:
        self.session_material_provider = session_material_provider
        self.transport = transport

    def available(self) -> bool:
        return self.session_material_provider is not None and self.transport is not None

    async def fetch_binary(self, url: str, policy: BinaryAcquisitionPolicy) -> bytes | None:
        if not self.available():
            return None
        material = await self.session_material_provider.material()
        if not material:
            return None
        raw = await self.transport.fetch_binary(url, material)
        if raw is None:
            return None
        if len(raw) == 0 or len(raw) > policy.max_binary_bytes:
            return None
        return raw


_DATA_URI_FONT = re.compile(
    r"data:(?:font|application)/(?:x-)?(?:ttf|otf|truetype|opentype|font-sfnt|vnd\.ms-opentype)[;,]",
    re.IGNORECASE,
)


async def extract_binary_from_dump_dom(
    dump: str,
    binary_fetch: Callable[[str], Awaitable[bytes | None]],
    policy: BinaryAcquisitionPolicy,
) -> bytes | None:
    """Extract an authorized binary from a dump-dom result.

    Recognizes embedded base64 font data URIs and same-provider font URLs;
    every candidate byte stream still passes full binary verification upstream.
    """
    if not dump:
        return None

    for match in re.finditer(r"data:[^\"'\s>)]+;base64,([A-Za-z0-9+/=\s]+?)(?=[\"'\s<)])", dump):
        if not _DATA_URI_FONT.search(match.group(0)):
            continue
        try:
            raw = base64.b64decode(re.sub(r"\s", "", match.group(1)), validate=True)
        except (ValueError, TypeError):
            continue
        if raw and len(raw) <= policy.max_binary_bytes:
            return raw

    for match in re.finditer(r"https?://[^\"'\s<>)]+\.(?:ttf|otf)(?=[\"'\s<>)]|$)", dump, re.IGNORECASE):
        raw = await binary_fetch(match.group(0))
        if raw and len(raw) <= policy.max_binary_bytes:
            return raw

    return None


class MonotypeRasterProvider:
    """Bounded authorized raster endpoint adapter with sprite-page termination."""

    def __init__(self, client: AuthorizedRasterClient | None) -> None:
        self.client = client

    def available(self) -> bool:
        return self.client is not None

    async def fetch_sprite_pages(
        self,
        request: dict[str, Any],
        policy: BinaryAcquisitionPolicy,
    ) -> tuple[SpriteRasterPage, ...]:
        """Fetch raster sprite pages with bounded deterministic termination."""
        if not self.available():
            return ()
        pages: list[SpriteRasterPage] = []
        cursor = ""
        for page_index in range(policy.max_sprite_pages):
            page = await self.client.fetch_sprite_page(request, cursor)
            if page is None:
                break
            pages.append(page)
            if page.final or not page.next_cursor:
                return tuple(pages)
            cursor = page.next_cursor
        # Bounded termination: hitting the page budget without a final marker
        # is an insufficient raster outcome, never an infinite crawl.
        return tuple(pages)
