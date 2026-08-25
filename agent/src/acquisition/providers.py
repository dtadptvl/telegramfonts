"""Injectable provider contracts and bounded default adapters.

No provider ever logs, raises, stores, or embeds secret/session material.
Session material flows through opaque callables and is consumed in-memory only.
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any, Awaitable, Callable, Protocol

from acquisition.models import (
    BinaryAcquisitionPolicy,
    BinaryCandidate,
    DiscoveryEnvelope,
    SpriteRasterPage,
)


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
    """Fallback capability: authorized persistent Chrome/session discovery.

    Returns typed font container bytes discovered through the authorized
    session (never collection-page HTML), or None when absent.
    """

    async def discover(
        self,
        envelope: DiscoveryEnvelope,
        source_url: str,
        session_material: dict[str, Any],
    ) -> bytes | None:
        """Discover and fetch an authorized binary via the persistent session."""


class AuthorizedRasterClient(Protocol):
    """Fallback capability: authorized Monotype raster endpoint."""

    async def fetch_sprite_page(self, request: dict[str, Any], cursor: str) -> SpriteRasterPage | None:
        """Fetch one bounded raster sprite page, or None when unavailable."""


class PersistentSessionBinaryProvider:
    """Authorized-session discovery provider bound to injectable transports."""

    def __init__(
        self,
        session_material_provider: AuthorizedSessionMaterialProvider | None,
        transport: AuthorizedSessionTransport | None,
    ) -> None:
        self.session_material_provider = session_material_provider
        self.transport = transport

    def available(self) -> bool:
        return self.session_material_provider is not None and self.transport is not None

    async def fetch_binary_for_envelope(
        self,
        envelope: DiscoveryEnvelope,
        source_url: str,
        policy: BinaryAcquisitionPolicy,
    ) -> bytes | None:
        if not self.available():
            return None
        material = await self.session_material_provider.material()
        if not material:
            return None
        raw = await self.transport.discover(envelope, source_url, material)
        if raw is None:
            return None
        if len(raw) == 0 or len(raw) > policy.max_binary_bytes:
            return None
        if not looks_like_font_bytes(raw):
            # HTML or unknown payloads are never font candidates.
            return None
        return raw


_DATA_URI_FONT = re.compile(
    r"data:(?:font|application)/(?:x-)?(?:ttf|otf|truetype|opentype|font-sfnt|vnd\.ms-opentype|woff2?)[;,]",
    re.IGNORECASE,
)

_FONT_URL = re.compile(
    r"https?://[^\"'\s<>)]+\.(?:ttf|otf|woff2?)(?=[\"'\s<>)]|$)", re.IGNORECASE
)
_MD5_HEX = re.compile(r"\b[0-9a-f]{32}\b", re.IGNORECASE)
_SFNT_MAGICS = (b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1")
_WOFF_MAGIC = b"wOFF"
_WOFF2_MAGIC = b"wOF2"


def looks_like_font_bytes(raw: bytes | None) -> bool:
    """Closed magic-byte classification; HTML or unknown payloads never pass."""
    if not raw or len(raw) < 8:
        return False
    head = bytes(raw[:4])
    return head in _SFNT_MAGICS or head == _WOFF_MAGIC or head == _WOFF2_MAGIC


def classify_font_container(raw: bytes | None) -> str:
    """Return TTF | OTF | WOFF | WOFF2 or '' when not a font container."""
    if not looks_like_font_bytes(raw):
        return ""
    head = bytes(raw[:4])  # type: ignore[index]
    if head == _WOFF_MAGIC:
        return "WOFF"
    if head == _WOFF2_MAGIC:
        return "WOFF2"
    if head == b"OTTO":
        return "OTF"
    return "TTF"


def _data_uri_format(prefix: str) -> str:
    low = prefix.lower()
    if "woff2" in low:
        return "WOFF2"
    if "woff" in low:
        return "WOFF"
    if "otf" in low or "opentype" in low:
        return "OTF"
    return "TTF"


def parse_discovery_from_dump(dump: str, source_url: str, provenance: str) -> DiscoveryEnvelope:
    """Build a typed discovery envelope from one dump-dom result.

    Extracts canonical family/style identity, authorized binary candidates
    (direct URLs and data URIs, including WOFF/WOFF2 containers), and MD5/raster
    identity for later stages. Deterministic and sanitized: page HTML itself is
    never carried forward.
    """
    if not dump:
        return DiscoveryEnvelope(provenance=provenance)

    family_name = ""
    style_name = ""

    # JSON-LD product metadata first (deterministic first match).
    for block in re.finditer(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        dump,
        re.IGNORECASE | re.DOTALL,
    ):
        try:
            payload = json.loads(block.group(1).strip())
        except (ValueError, TypeError):
            continue
        nodes = payload if isinstance(payload, list) else [payload]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            name = node.get("name") or node.get("familyName")
            if isinstance(name, str) and name.strip():
                family_name = family_name or name.strip()
            variant = node.get("variantName") or node.get("styleName")
            if isinstance(variant, str) and variant.strip():
                style_name = style_name or variant.strip()

    if not family_name:
        og = re.search(r'<meta[^>]*property=["\']og:title["\'][^>]*content=["\']([^"\']+)["\']', dump, re.IGNORECASE)
        if og:
            family_name = og.group(1).strip()

    candidates: list[BinaryCandidate] = []
    seen_urls: set[str] = set()
    for match in _FONT_URL.finditer(dump):
        url = match.group(0)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        suffix = url.lower().rsplit(".", 1)[-1].split("?", 1)[0]
        fmt = {"ttf": "TTF", "otf": "OTF", "woff": "WOFF", "woff2": "WOFF2"}.get(suffix, "")
        if fmt:
            candidates.append(BinaryCandidate(url=url, format=fmt, embedded=False))

    data_uri_matches = [
        m
        for m in re.finditer(r"data:[^\"'\s>)]+;base64,", dump)
        if _DATA_URI_FONT.search(m.group(0))
    ]
    for idx, match in enumerate(data_uri_matches):
        fmt = _data_uri_format(match.group(0))
        marker = f"data-uri:{idx}"
        candidates.append(BinaryCandidate(url=marker, format=fmt, embedded=True))

    # MD5/raster identity: prefer explicit keyed values, then URL-embedded MD5s.
    md5 = ""
    keyed = re.search(r'["\'](?:font_?md5|md5|fontId)["\']\s*[:=]\s*["\']([0-9a-fA-F]{32})["\']', dump)
    if keyed:
        md5 = keyed.group(1).lower()
    else:
        for url_match in _FONT_URL.finditer(dump):
            found = _MD5_HEX.search(url_match.group(0))
            if found:
                md5 = found.group(0).lower()
                break

    return DiscoveryEnvelope(
        family_name=family_name,
        style_name=style_name,
        md5=md5,
        binary_candidates=tuple(candidates),
        raster_identity=md5,
        provenance=provenance,
    )


async def extract_binary_from_dump_dom(
    dump: str,
    binary_fetch: Callable[[str], Awaitable[bytes | None]],
    policy: BinaryAcquisitionPolicy,
    envelope: DiscoveryEnvelope | None = None,
) -> bytes | None:
    """Resolve an authorized binary from a discovery envelope.

    TTF/OTF candidates pass through unchanged; WOFF/WOFF2 containers are
    converted to their sfnt payload by the converter before verification.
    """
    if envelope is None:
        return None

    for candidate in envelope.binary_candidates:
        raw: bytes | None = None
        if candidate.embedded:
            # Embedded candidates carry their font data-URI match index.
            matches = list(re.finditer(r"data:[^\"'\s>)]+;base64,([A-Za-z0-9+/=\s]+?)(?=[\"'\s<)])", dump))
            data_matches = [m for m in matches if _DATA_URI_FONT.search(m.group(0))]
            try:
                embedded_index = int(candidate.url.rsplit(":", 1)[-1])
            except ValueError:
                embedded_index = -1
            if 0 <= embedded_index < len(data_matches):
                try:
                    raw = base64.b64decode(re.sub(r"\s", "", data_matches[embedded_index].group(1)), validate=True)
                except (ValueError, TypeError):
                    raw = None
        else:
            raw = await binary_fetch(candidate.url)
        if not raw or len(raw) > policy.max_binary_bytes:
            continue
        container = classify_font_container(raw)
        if container in ("TTF", "OTF"):
            return raw
        if container in ("WOFF", "WOFF2"):
            from compute.binary_gate import convert_container_to_sfnt

            converted = convert_container_to_sfnt(raw)
            if converted and len(converted) <= policy.max_binary_bytes:
                return converted
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
        """Fetch raster sprite pages with bounded deterministic termination.

        The request must carry the exact family/style/MD5 target; empty or
        incomplete targets fail closed without any request.
        """
        if not self.available():
            return ()
        family = str(request.get("family", "")).strip()
        style = str(request.get("style", "")).strip()
        md5 = str(request.get("md5", "")).strip().lower()
        if not family or not style or not md5 or len(md5) != 32:
            return ()
        pages: list[SpriteRasterPage] = []
        cursor = ""
        for page_index in range(policy.max_sprite_pages):
            page = await self.client.fetch_sprite_page(request, cursor)
            if page is None:
                break
            if page.glyph_count == 0:
                # Empty layout marks bounded completion; the empty page itself
                # carries no evidence and is never ingested.
                break
            pages.append(page)
            if page.final or not page.next_cursor:
                return tuple(pages)
            observed = (page.payload or {}).get("observed_headers") or {}
            max_gpp = observed.get("max_glyphs_per_page")
            if isinstance(max_gpp, int) and max_gpp > 0 and page.glyph_count < max_gpp:
                # Observable page signal: a partial fill below the declared
                # per-page maximum marks the final evidence page.
                return tuple(pages)
            cursor = page.next_cursor
        # Bounded termination: hitting the page budget without a final marker
        # is an insufficient raster outcome, never an infinite crawl.
        return tuple(pages)
