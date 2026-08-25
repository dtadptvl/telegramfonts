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
    FamilyDiscoveryEnvelope,
    StyleDiscoveryRecord,
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


class PlaywrightStealthProvider(Protocol):
    """Fallback capability: Playwright Stealth real-Chrome persistent context."""

    def available(self) -> bool:
        """True when Playwright/Chrome persistent context is configured."""

    async def discover_family(self, source_url: str) -> FamilyDiscoveryEnvelope | None:
        """Recover complete FamilyDiscoveryEnvelope using stealth persistent session."""

    async def capture_raster_pages(
        self, source_url: str, style_rec: StyleDiscoveryRecord, requested_sizes: list[int]
    ) -> tuple[SpriteRasterPage, ...] | None:
        """Render and capture raster glyph sprites directly via persistent context."""


class AlgoliaMetadataProvider(Protocol):
    """Fallback capability: MyFonts Algolia metadata client (metadata only)."""

    def available(self) -> bool:
        """True when Algolia app credentials are configured."""

    async def discover_family(self, family_name_or_slug: str, source_url: str = "") -> FamilyDiscoveryEnvelope | None:
        """Recover exact FamilyDiscoveryEnvelope from Algolia search index."""


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


def parse_family_discovery_from_dump(dump: str, source_url: str, provenance: str) -> FamilyDiscoveryEnvelope:
    """Build a complete FamilyDiscoveryEnvelope from one multi-style dump-dom result.

    Extracts canonical family identity and all child styles with exact style-bound MD5s,
    binary candidates, and raster resources.
    """
    if not dump:
        return FamilyDiscoveryEnvelope(provenance=provenance, family_url=source_url)

    family_name = ""
    styles: dict[str, StyleDiscoveryRecord] = {}

    # 1. JSON-LD structured metadata
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
            # Check for offers / child styles
            has_variant = node.get("hasVariant") or node.get("offers") or node.get("styleVariants")
            if isinstance(has_variant, list):
                for var in has_variant:
                    if isinstance(var, dict):
                        s_name = var.get("name") or var.get("variantName") or var.get("styleName") or ""
                        s_id = str(var.get("sku") or var.get("id") or var.get("styleId") or s_name)
                        s_md5 = str(var.get("fontMd5") or var.get("md5") or var.get("font_md5") or "").lower()
                        if s_name:
                            norm_k = s_id.lower().replace("-", "_").replace(" ", "_")
                            styles[norm_k] = StyleDiscoveryRecord(
                                style_id=s_id,
                                style_name=s_name,
                                md5=s_md5 if len(s_md5) == 32 else "",
                                provenance=provenance,
                            )

    # 2. Next.js __NEXT_DATA__ or embedded state
    next_match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', dump, re.DOTALL)
    if next_match:
        try:
            next_data = json.loads(next_match.group(1).strip())
            page_props = (next_data.get("props") or {}).get("pageProps") or {}
            fam_data = page_props.get("familyData") or page_props.get("family") or page_props
            if isinstance(fam_data, dict):
                family_name = family_name or str(fam_data.get("name") or fam_data.get("familyName") or "")
                raw_styles = fam_data.get("styles") or fam_data.get("fonts") or []
                if isinstance(raw_styles, list):
                    for st in raw_styles:
                        if isinstance(st, dict):
                            s_name = str(st.get("name") or st.get("style_name") or "")
                            s_id = str(st.get("id") or st.get("style_id") or s_name)
                            s_md5 = str(st.get("font_md5") or st.get("md5") or st.get("fontMd5") or "").lower()
                            if s_name:
                                norm_k = s_id.lower().replace("-", "_").replace(" ", "_")
                                styles[norm_k] = StyleDiscoveryRecord(
                                    style_id=s_id,
                                    style_name=s_name,
                                    md5=s_md5 if len(s_md5) == 32 else "",
                                    provenance=provenance,
                                )
        except Exception:
            pass

    # 3. Fallback family name from OpenGraph
    if not family_name:
        og = re.search(r'<meta[^>]*property=["\']og:title["\'][^>]*content=["\']([^"\']+)["\']', dump, re.IGNORECASE)
        if og:
            family_name = og.group(1).strip()
        else:
            title = re.search(r'<title>([^<]+)</title>', dump, re.IGNORECASE)
            if title:
                family_name = title.group(1).split("|")[0].split("-")[0].strip()

    # 4. Embedded font candidates & data URIs
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

    # 5. Extract style rows / font_md5 regex matches if styles map is still empty
    for style_match in re.finditer(
        r'data-style-id=["\']([^"\']+)["\'][^>]*data-style-name=["\']([^"\']+)["\'][^>]*data-font-md5=["\']([0-9a-fA-F]{32})["\']',
        dump,
        re.IGNORECASE,
    ):
        s_id, s_name, s_md5 = style_match.groups()
        norm_k = s_id.lower().replace("-", "_").replace(" ", "_")
        styles[norm_k] = StyleDiscoveryRecord(
            style_id=s_id,
            style_name=s_name,
            md5=s_md5.lower(),
            provenance=provenance,
        )

    # 6. If no styles array was parsed, check for single style/md5 or create default
    if not styles:
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

        default_style_name = "Regular"
        style_title = re.search(r'class=["\'][^"\']*style-name[^"\']*["\']>([^<]+)<', dump, re.IGNORECASE)
        if style_title:
            default_style_name = style_title.group(1).strip()
        norm_id = default_style_name.lower().replace("-", "_").replace(" ", "_")
        styles[norm_id] = StyleDiscoveryRecord(
            style_id=norm_id,
            style_name=default_style_name,
            md5=md5,
            binary_candidates=tuple(candidates),
            provenance=provenance,
        )
    else:
        # Attach binary candidates to all styles if available
        if candidates:
            updated_styles = {}
            for k, rec in styles.items():
                if not rec.binary_candidates:
                    updated_styles[k] = StyleDiscoveryRecord(
                        style_id=rec.style_id,
                        style_name=rec.style_name,
                        md5=rec.md5,
                        binary_candidates=tuple(candidates),
                        raster_resources=rec.raster_resources,
                        provenance=rec.provenance,
                    )
                else:
                    updated_styles[k] = rec
            styles = updated_styles

    canonical_key = family_name.lower().replace("-", "_").replace(" ", "_") if family_name else ""
    return FamilyDiscoveryEnvelope(
        family_name=family_name,
        family_url=source_url,
        canonical_family_key=canonical_key,
        styles=styles,
        provenance=provenance,
    )


def parse_discovery_from_dump(dump: str, source_url: str, provenance: str) -> DiscoveryEnvelope:
    """Build a typed discovery envelope from one dump-dom result (backwards compatible)."""
    fam_env = parse_family_discovery_from_dump(dump, source_url, provenance)
    first_style = next(iter(fam_env.styles.values())) if fam_env.styles else None
    return DiscoveryEnvelope(
        family_name=fam_env.family_name,
        style_name=first_style.style_name if first_style else "",
        md5=first_style.md5 if first_style else "",
        binary_candidates=first_style.binary_candidates if first_style else (),
        raster_identity=first_style.md5 if first_style else "",
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
        # Observable render-size passes: each requested acs_pt is one
        # independent bounded crawl (RASTER_MAX budget applies per pass).
        acs_pts_raw = request.get("acs_pts")
        if acs_pts_raw is None:
            acs_pts_raw = (request.get("acs_pt", 120),)
        try:
            acs_pts = tuple(int(p) for p in acs_pts_raw)
        except (TypeError, ValueError):
            return ()
        if not acs_pts or any(p < 1 for p in acs_pts):
            return ()
        pages: list[SpriteRasterPage] = []
        for pt in acs_pts:
            pt_request = {**request, "acs_pt": pt}
            cursor = ""
            for _page_index in range(policy.max_sprite_pages):
                page = await self.client.fetch_sprite_page(pt_request, cursor)
                if page is None:
                    break
                if page.glyph_count == 0:
                    # Empty layout marks bounded completion; the empty page
                    # itself carries no evidence and is never ingested.
                    break
                pages.append(page)
                if page.final or not page.next_cursor:
                    break
                observed = (page.payload or {}).get("observed_headers") or {}
                max_gpp = observed.get("max_glyphs_per_page")
                if isinstance(max_gpp, int) and max_gpp > 0 and page.glyph_count < max_gpp:
                    # Observable page signal: a partial fill below the declared
                    # per-page maximum marks the final evidence page.
                    break
                cursor = page.next_cursor
        # Bounded termination: hitting any budget without a final marker is an
        # insufficient raster outcome, never an infinite crawl.
        return tuple(pages)
