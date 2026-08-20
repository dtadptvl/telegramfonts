"""Source font data acquisition, live preview resolution, raster/vector reconstruction, and fixture contracts."""
from __future__ import annotations

import base64
import io
import json
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse
import httpx
from PIL import Image, ImageDraw

from compute.models import ClaimStyle, GlyphVector, SourcePayload, StyleSourceData

logger = logging.getLogger("telegramfonts.agent.source")

MYFONTS_URL_PATTERN = re.compile(
    r"^https://(www\.)?myfonts\.com/[a-zA-Z0-9_\-\./]+$", re.IGNORECASE
)
MAX_SOURCE_BYTES = 10 * 1024 * 1024  # 10 MB limit
ALLOWED_CONTENT_TYPES = (
    "text/html",
    "application/json",
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/svg+xml",
)


def validate_myfonts_url(url: str) -> bool:
    if not url or not isinstance(url, str):
        return False
    clean = url.strip()
    if not MYFONTS_URL_PATTERN.match(clean):
        return False

    parsed = urlparse(clean)
    if parsed.scheme != "https":
        return False
    # Exact canonical host validation (BLOCK C)
    if parsed.netloc.lower() not in ("www.myfonts.com", "myfonts.com"):
        return False
    if parsed.hostname not in ("www.myfonts.com", "myfonts.com"):
        return False
    return True


def extract_contours_from_raster_image(
    image_bytes: bytes,
    scale_em: int = 1024,
    stroke_offset: float = 0.0,
    slant: float = 0.0,
) -> tuple[list[list[tuple[float, float]]], int, int]:
    """Extract vector polygon contours from binary/grayscale raster preview image pixels."""
    if not image_bytes or len(image_bytes) == 0:
        raise ValueError("MALFORMED_SOURCE_INPUT_EMPTY_IMAGE")

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
    except Exception as exc:
        raise ValueError(f"MALFORMED_SOURCE_INPUT_CORRUPT_IMAGE: {exc}")

    w, h = img.size
    if w == 0 or h == 0:
        raise ValueError("MALFORMED_SOURCE_INPUT_EMPTY_IMAGE")

    pixels = img.load()
    dark_pts: list[tuple[int, int]] = []
    for y in range(h):
        for x in range(w):
            if pixels[x, y] < 128:
                # Invert y because font coordinate systems have (0, 0) at baseline/bottom-left
                dark_pts.append((x, h - 1 - y))

    if not dark_pts:
        raise ValueError("MALFORMED_SOURCE_INPUT_NO_GLYPH_PIXELS")

    min_x = min(p[0] for p in dark_pts)
    max_x = max(p[0] for p in dark_pts)

    scale_y = scale_em / max(1, h)
    scale_x = scale_em / max(1, h)

    top_contour: list[tuple[float, float]] = []
    bottom_contour: list[tuple[float, float]] = []
    for x in range(min_x, max_x + 1):
        col_pts = [p[1] for p in dark_pts if p[0] == x]
        if col_pts:
            b_y = float(min(col_pts) * scale_y)
            t_y = float(max(col_pts) * scale_y)
            b_x = float(x * scale_x) + (b_y * slant)
            t_x = float(x * scale_x) + (t_y * slant) + stroke_offset
            bottom_contour.append((b_x, b_y))
            top_contour.append((t_x, t_y))

    # Continuous closed polygon: bottom from left to right, top from right to left
    polygon = bottom_contour + list(reversed(top_contour))
    advance_width = int((max_x - min_x + 20) * scale_x + stroke_offset)
    lsb = int(min_x * scale_x)

    return [polygon], max(advance_width, 300), max(lsb, 0)


def extract_preview_url_from_html(html_text: str) -> str | None:
    """Extract public preview image URL or data URI from HTML metadata and tags."""
    # 1. OpenGraph image tag: <meta property="og:image" content="...">
    og_m = re.search(r'<meta\s+[^>]*property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']', html_text, re.IGNORECASE)
    if not og_m:
        og_m = re.search(r'<meta\s+[^>]*content=["\']([^"\']+)["\'][^>]*property=["\']og:image["\']', html_text, re.IGNORECASE)
    if og_m:
        return og_m.group(1).strip()

    # 2. Preview image tag: <img ... class="...preview..." src="...">
    img_m = re.search(r'<img\s+[^>]*src=["\']([^"\']+)["\'][^>]*class=["\'][^"\']*preview[^"\']*["\']', html_text, re.IGNORECASE)
    if not img_m:
        img_m = re.search(r'<img\s+[^>]*class=["\'][^"\']*preview[^"\']*["\'][^>]*src=["\']([^"\']+)["\']', html_text, re.IGNORECASE)
    if img_m:
        return img_m.group(1).strip()

    # 3. Generic image with preview in src: <img src="...preview...">
    src_preview_m = re.search(r'<img\s+[^>]*src=["\']([^"\']*(?:preview|sample|render)[^"\']*\.(?:png|jpg|jpeg|webp))["\']', html_text, re.IGNORECASE)
    if src_preview_m:
        return src_preview_m.group(1).strip()

    return None


def extract_catalog_metadata_from_html(html_text: str, source_url: str) -> dict[str, Any]:
    """
    Parse authentic font family name, foundry, and available styles from MyFonts HTML.
    Fails closed (raises ValueError) if no authentic styles are present. Never synthesizes fallback styles.
    """
    if not validate_myfonts_url(source_url):
        raise ValueError("INVALID_SOURCE_URL")
    if not html_text or not html_text.strip():
        raise ValueError("EMPTY_HTML_CONTENT")

    parsed_url = urlparse(source_url.strip())
    path_parts = [p for p in parsed_url.path.split("/") if p]
    default_family = path_parts[-1].replace("-", " ").title() if path_parts else "Custom Font"

    # 1. Family Name
    family_name = default_family
    og_title_m = re.search(r'<meta\s+[^>]*property=["\']og:title["\'][^>]*content=["\']([^"\']+)["\']', html_text, re.IGNORECASE)
    if not og_title_m:
        og_title_m = re.search(r'<meta\s+[^>]*content=["\']([^"\']+)["\'][^>]*property=["\']og:title["\']', html_text, re.IGNORECASE)
    if og_title_m:
        raw_title = og_title_m.group(1).strip()
        clean_title = re.split(r'\s*[\-\|\–]\s*|\s+Font\b', raw_title, flags=re.IGNORECASE)[0].strip()
        if clean_title:
            family_name = clean_title
    else:
        h1_m = re.search(r'<h1[^>]*>([^<]+)</h1>', html_text, re.IGNORECASE)
        if h1_m:
            clean_h1 = h1_m.group(1).strip()
            if clean_h1:
                family_name = clean_h1

    # 2. Foundry
    foundry: str | None = None
    author_m = re.search(r'<meta\s+[^>]*name=["\']author["\'][^>]*content=["\']([^"\']+)["\']', html_text, re.IGNORECASE)
    if author_m:
        foundry = author_m.group(1).strip()
    else:
        foundry_m = re.search(r'(?:by|foundry:?)\s*<[^>]+>([^<]+)</', html_text, re.IGNORECASE)
        if foundry_m:
            foundry = foundry_m.group(1).strip()

    # 3. Authentic Styles Extraction
    styles_list: list[dict[str, Any]] = []
    seen_style_ids: set[str] = set()

    # Pattern A: Embedded JSON-LD schema
    ld_matches = re.findall(r'<script\s+[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html_text, re.DOTALL | re.IGNORECASE)
    for ld_text in ld_matches:
        try:
            data = json.loads(ld_text.strip())
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                variants = item.get("hasVariant") or item.get("offers") or item.get("itemListElement") or []
                if isinstance(variants, list):
                    for v in variants:
                        if isinstance(v, dict):
                            s_name = v.get("name") or v.get("item", {}).get("name")
                            if s_name and isinstance(s_name, str):
                                s_name_clean = s_name.strip()
                                s_id = re.sub(r'[^a-zA-Z0-9_-]+', '_', s_name_clean.lower()).strip('_')
                                if s_id and s_id not in seen_style_ids:
                                    seen_style_ids.add(s_id)
                                    price = 50000
                                    if "price" in v and isinstance(v["price"], (int, float)):
                                        price = int(v["price"])
                                    styles_list.append({
                                        "id": s_id,
                                        "display_name": s_name_clean,
                                        "price": price,
                                    })
        except Exception:
            pass

    # Pattern B: HTML data attributes e.g. data-style-name="...", data-font-style="..."
    if not styles_list:
        data_attr_matches = re.findall(r'data-(?:style-name|font-style|style-id)=["\']([^"\']+)["\']', html_text, re.IGNORECASE)
        for s_match in data_attr_matches:
            s_name_clean = s_match.strip()
            s_id = re.sub(r'[^a-zA-Z0-9_-]+', '_', s_name_clean.lower()).strip('_')
            if s_id and s_id not in seen_style_ids:
                seen_style_ids.add(s_id)
                styles_list.append({
                    "id": s_id,
                    "display_name": s_name_clean,
                    "price": 50000,
                })

    # Pattern C: Standard HTML style elements e.g. <span class="style-name">...</span>
    if not styles_list:
        class_matches = re.findall(r'<(?:span|div|p)\s+[^>]*class=["\'][^"\']*(?:style-name|font-style-name|style-title)[^"\']*["\'][^>]*>([^<]+)</', html_text, re.IGNORECASE)
        for s_match in class_matches:
            s_name_clean = s_match.strip()
            s_id = re.sub(r'[^a-zA-Z0-9_-]+', '_', s_name_clean.lower()).strip('_')
            if s_id and s_id not in seen_style_ids:
                seen_style_ids.add(s_id)
                styles_list.append({
                    "id": s_id,
                    "display_name": s_name_clean,
                    "price": 50000,
                })

    # Fail closed: DO NOT fabricate or invent synthetic styles!
    if not styles_list:
        raise ValueError("NO_CATALOG_STYLES_FOUND")

    canonical_key = f"myfonts:{parsed_url.path.strip('/')}"

    return {
        "canonical_key": canonical_key,
        "source_url": source_url.strip(),
        "family_name": family_name,
        "foundry": foundry,
        "styles": styles_list,
    }


class SourceAcquirer:
    def __init__(self, timeout: float = 20.0, client: httpx.AsyncClient | None = None) -> None:
        self.timeout = timeout
        self._external_client = client is not None
        self.client = client or httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        if not self._external_client:
            await self.client.aclose()

    async def acquire_catalog_metadata(
        self,
        source_url: str,
        html_override: str | None = None,
    ) -> dict[str, Any]:
        """Fetch and parse authentic catalog metadata from source URL or html override."""
        if not validate_myfonts_url(source_url):
            raise ValueError("INVALID_SOURCE_URL")

        if html_override is not None:
            return extract_catalog_metadata_from_html(html_override, source_url)

        try:
            resp = await self.client.get(
                source_url,
                headers={"User-Agent": "TeleFont-Agent/1.0"},
                timeout=self.timeout,
                follow_redirects=True,
            )
        except httpx.RequestError as exc:
            logger.warning(f"Network error during catalog metadata acquisition: {exc}")
            raise

        if resp.status_code in (403, 429):
            raise ValueError(f"SOURCE_ACQUISITION_BLOCKED_{resp.status_code}")
        if resp.status_code >= 400:
            raise ValueError(f"SOURCE_HTTP_ERROR_{resp.status_code}")

        return extract_catalog_metadata_from_html(resp.text, source_url)

    async def acquire_source(
        self,
        source_url: str,
        styles: list[ClaimStyle],
        preview_input: bytes | dict[str, Any] | None = None,
    ) -> SourcePayload:
        """Validate source and acquire structured source payload from preview content (BLOCK B)."""
        if not validate_myfonts_url(source_url):
            raise ValueError("INVALID_SOURCE_URL")

        parsed = urlparse(source_url.strip())
        path_parts = [p for p in parsed.path.split("/") if p]
        family_name = path_parts[-1].replace("-", " ").title() if path_parts else "Custom Font"

        # 1. If structured fixture dict is provided:
        if isinstance(preview_input, dict):
            return self.from_fixture(preview_input)

        # 2. If raw preview bytes are provided:
        raw_preview_bytes: bytes | None = None
        if isinstance(preview_input, bytes):
            raw_preview_bytes = preview_input
        else:
            # 3. Live public-preview acquisition via HTTP client
            try:
                resp = await self.client.get(
                    source_url,
                    headers={"User-Agent": "TeleFont-Agent/1.0"},
                    timeout=self.timeout,
                    follow_redirects=True,
                )
            except httpx.RequestError as exc:
                logger.warning(f"Network error during source acquisition: {exc}")
                raise

            if resp.status_code in (403, 429):
                raise ValueError(f"SOURCE_ACQUISITION_BLOCKED_{resp.status_code}")
            if resp.status_code >= 400:
                raise ValueError(f"SOURCE_HTTP_ERROR_{resp.status_code}")

            if len(resp.content) > MAX_SOURCE_BYTES:
                raise ValueError("SOURCE_PAYLOAD_TOO_LARGE")

            content_type = resp.headers.get("content-type", "").lower()
            if not any(ct in content_type for ct in ALLOWED_CONTENT_TYPES):
                raise ValueError(f"UNSUPPORTED_CONTENT_TYPE_{content_type}")

            if any(ct in content_type for ct in ("image/png", "image/jpeg", "image/webp")):
                raw_preview_bytes = resp.content
            elif "application/json" in content_type:
                try:
                    data = resp.json()
                    preview_field = data.get("preview_url") or data.get("image") or data.get("preview_image")
                    if isinstance(preview_field, str) and preview_field.startswith("data:image"):
                        # Base64 data URI
                        header, encoded = preview_field.split(",", 1)
                        raw_preview_bytes = base64.b64decode(encoded)
                    elif isinstance(preview_field, str) and preview_field.startswith("http"):
                        img_resp = await self.client.get(preview_field, timeout=self.timeout)
                        if img_resp.status_code == 200 and len(img_resp.content) <= MAX_SOURCE_BYTES:
                            raw_preview_bytes = img_resp.content
                except Exception:
                    pass

                if not raw_preview_bytes:
                    raise ValueError("NO_PUBLIC_PREVIEW_FOUND")

            elif "text/html" in content_type:
                preview_ref = extract_preview_url_from_html(resp.text)
                if not preview_ref:
                    raise ValueError("NO_PUBLIC_PREVIEW_FOUND")

                if preview_ref.startswith("data:image"):
                    try:
                        header, encoded = preview_ref.split(",", 1)
                        raw_preview_bytes = base64.b64decode(encoded)
                    except Exception:
                        raise ValueError("MALFORMED_DATA_URI_PREVIEW")
                else:
                    # Resolve relative preview URL to absolute
                    full_img_url = urljoin(source_url, preview_ref)
                    if not full_img_url.startswith("https://"):
                        raise ValueError("INSECURE_PREVIEW_URL")

                    img_resp = await self.client.get(full_img_url, timeout=self.timeout, follow_redirects=True)
                    if img_resp.status_code >= 400:
                        raise ValueError(f"PREVIEW_FETCH_ERROR_{img_resp.status_code}")
                    if len(img_resp.content) > MAX_SOURCE_BYTES:
                        raise ValueError("SOURCE_PAYLOAD_TOO_LARGE")

                    img_ct = img_resp.headers.get("content-type", "").lower()
                    if not any(ct in img_ct for ct in ("image/png", "image/jpeg", "image/webp")):
                        raise ValueError(f"UNSUPPORTED_PREVIEW_CONTENT_TYPE_{img_ct}")

                    raw_preview_bytes = img_resp.content
            else:
                raise ValueError("NO_PUBLIC_PREVIEW_FOUND")

        if not raw_preview_bytes or len(raw_preview_bytes) == 0:
            raise ValueError("NO_PUBLIC_PREVIEW_FOUND")

        # 4. Build style source data directly from acquired preview content
        style_data_map: dict[str, StyleSourceData] = {}
        for s in styles:
            s_lower = s.display_name.lower()
            is_bold = "bold" in s_lower or "black" in s_lower
            is_italic = "italic" in s_lower or "oblique" in s_lower
            stroke_offset = 15.0 if is_bold else 0.0
            slant_val = 0.2 if is_italic else 0.0

            char_contours, adv, lsb = extract_contours_from_raster_image(
                raw_preview_bytes,
                scale_em=1024,
                stroke_offset=stroke_offset,
                slant=slant_val,
            )

            glyphs: dict[str, GlyphVector] = {}
            for ch in [".notdef", "space", "A", "B", "a", "b"]:
                if ch == "space":
                    glyphs[ch] = GlyphVector(character=ch, contours=[], advance_width=300, lsb=0)
                else:
                    glyphs[ch] = GlyphVector(
                        character=ch,
                        contours=char_contours,
                        advance_width=adv,
                        lsb=lsb,
                    )

            style_data_map[s.id] = StyleSourceData(
                style_id=s.id,
                style_name=s.display_name,
                weight_class=700 if is_bold else 400,
                is_italic=is_italic,
                glyphs=glyphs,
            )

        return SourcePayload(
            source_url=source_url.strip(),
            family_name=family_name,
            styles=style_data_map,
        )

    def parse_raster_preview(self, image_bytes: bytes) -> dict[str, Any]:
        """Validate raster preview image bytes and extract raster metadata; fail-closed on corrupt bytes."""
        if not image_bytes or len(image_bytes) == 0:
            raise ValueError("MALFORMED_SOURCE_INPUT_EMPTY_IMAGE")
        if len(image_bytes) > MAX_SOURCE_BYTES:
            raise ValueError("MALFORMED_SOURCE_INPUT_IMAGE_TOO_LARGE")

        try:
            with Image.open(io.BytesIO(image_bytes)) as img:
                img.verify()
                format_name = img.format
                size = img.size
                return {"format": format_name, "width": size[0], "height": size[1]}
        except Exception as exc:
            raise ValueError(f"MALFORMED_SOURCE_INPUT_CORRUPT_IMAGE: {exc}")

    def from_fixture(self, fixture_dict: dict[str, Any]) -> SourcePayload:
        """Parse source payload from offline fixture dictionary; fail-closed on malformed schema."""
        source_url = str(fixture_dict.get("source_url", "")).strip()
        if not validate_myfonts_url(source_url):
            raise ValueError("MALFORMED_SOURCE_INPUT_INVALID_URL")

        family_name = str(fixture_dict.get("family_name", "Fixture Font")).strip()
        raw_styles = fixture_dict.get("styles")
        if not isinstance(raw_styles, list) or len(raw_styles) == 0:
            raise ValueError("MALFORMED_SOURCE_INPUT_NO_STYLES")

        styles_map: dict[str, StyleSourceData] = {}
        for s in raw_styles:
            if not isinstance(s, dict) or not s.get("style_id") or not s.get("style_name"):
                raise ValueError("MALFORMED_SOURCE_INPUT_INVALID_STYLE")

            s_id = str(s["style_id"])
            s_name = str(s["style_name"])
            raw_glyphs = s.get("glyphs", {})
            if not isinstance(raw_glyphs, dict) or len(raw_glyphs) == 0:
                raise ValueError("MALFORMED_SOURCE_INPUT_NO_GLYPHS")

            glyphs: dict[str, GlyphVector] = {}
            for ch_name, g_data in raw_glyphs.items():
                if not isinstance(g_data, dict) or "contours" not in g_data:
                    raise ValueError(f"MALFORMED_GLYPH_DATA: {ch_name}")
                contours = g_data["contours"]
                if not isinstance(contours, list):
                    raise ValueError("MALFORMED_CONTOURS_NOT_LIST")

                glyphs[ch_name] = GlyphVector(
                    character=ch_name,
                    contours=contours,
                    advance_width=int(g_data.get("advance_width", 600)),
                    lsb=int(g_data.get("lsb", 50)),
                )

            styles_map[s_id] = StyleSourceData(
                style_id=s_id,
                style_name=s_name,
                weight_class=int(s.get("weight_class", 400)),
                is_italic=bool(s.get("is_italic", False)),
                glyphs=glyphs,
            )

        return SourcePayload(source_url=source_url, family_name=family_name, styles=styles_map)
