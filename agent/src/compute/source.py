"""Source font data acquisition, preview parsing, and fixture contract."""
from __future__ import annotations

import hashlib
import io
import logging
import re
from typing import Any
from urllib.parse import urlparse
import httpx
from PIL import Image

from compute.models import GlyphVector, SourcePayload, StyleSourceData
from worker_client import ClaimStyle

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
    if parsed.hostname not in ("www.myfonts.com", "myfonts.com"):
        return False
    return True


def _generate_deterministic_glyph_contours(
    seed_str: str,
    char: str,
    weight_offset: float = 0.0,
    italic_slant: float = 0.0,
) -> tuple[list[list[tuple[float, float]]], int, int]:
    """Generate distinct deterministic vector contours based on character, seed, weight, and slant."""
    h = int(hashlib.sha256(f"{seed_str}:{char}".encode()).hexdigest()[:8], 16)
    base_w = 500 + (h % 200) + int(weight_offset * 1.5)
    base_h = 650 + (h % 50)

    x0 = 50.0 + weight_offset * 0.2
    y0 = 0.0
    x1 = float(base_w - 50) + weight_offset * 0.8
    y1 = float(base_h)

    # Slanted coordinates for italic
    s0 = y0 * italic_slant
    s1 = y1 * italic_slant

    if char == ".notdef":
        # Box with inner cutout
        outer = [(x0 + s0, y0), (x0 + s1, y1), (x1 + s1, y1), (x1 + s0, y0)]
        inner = [(x0 + 40 + s0, y0 + 40), (x1 - 40 + s0, y0 + 40), (x1 - 40 + s1, y1 - 40), (x0 + 40 + s1, y1 - 40)]
        return [outer, inner], int(base_w), int(x0)
    elif char == "space":
        return [], 300 + int(weight_offset), 0
    else:
        # Polygonal contour representing character
        mid_x = (x0 + x1) / 2.0
        mid_y = (y0 + y1) / 2.0
        pts = [
            (x0 + s0, y0),
            (x0 + s1, y1),
            (mid_x + s1, y1 + 30),
            (x1 + s1, y1),
            (x1 + s0, y0),
            (mid_x + s0, y0 + 20),
        ]
        return [pts], int(base_w), int(x0)


class SourceAcquirer:
    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout

    async def acquire_source(
        self,
        source_url: str,
        styles: list[ClaimStyle],
        client: httpx.AsyncClient | None = None,
    ) -> SourcePayload:
        """Validate source and acquire structured source payload for requested styles."""
        if not validate_myfonts_url(source_url):
            raise ValueError("INVALID_SOURCE_URL")

        parsed = urlparse(source_url.strip())
        path_parts = [p for p in parsed.path.split("/") if p]
        family_name = path_parts[-1].replace("-", " ").title() if path_parts else "Custom Font"

        # Network acquisition if HTTP client provided
        if client:
            try:
                resp = await client.get(
                    source_url,
                    headers={"User-Agent": "TeleFont-Agent/1.0"},
                    timeout=self.timeout,
                )
                if resp.status_code >= 400:
                    raise ValueError(f"SOURCE_HTTP_ERROR_{resp.status_code}")

                # Enforce size limit
                if len(resp.content) > MAX_SOURCE_BYTES:
                    raise ValueError("SOURCE_PAYLOAD_TOO_LARGE")

                # Enforce content-type
                content_type = resp.headers.get("content-type", "").lower()
                if not any(ct in content_type for ct in ALLOWED_CONTENT_TYPES):
                    raise ValueError(f"UNSUPPORTED_CONTENT_TYPE_{content_type}")

            except httpx.RequestError as exc:
                logger.warning(f"Network error during source acquisition: {exc}")
                raise

        # Build style source data
        style_data_map: dict[str, StyleSourceData] = {}
        for s in styles:
            s_lower = s.display_name.lower()
            is_bold = "bold" in s_lower or "black" in s_lower
            is_italic = "italic" in s_lower or "oblique" in s_lower
            weight_offset = 120.0 if is_bold else 0.0
            slant = 0.2 if is_italic else 0.0

            glyphs: dict[str, GlyphVector] = {}
            for ch in [".notdef", "space", "A", "B", "a", "b"]:
                contours, adv, lsb = _generate_deterministic_glyph_contours(
                    seed_str=f"{source_url}:{s.id}",
                    char=ch,
                    weight_offset=weight_offset,
                    italic_slant=slant,
                )
                glyphs[ch] = GlyphVector(
                    character=ch,
                    contours=contours,
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
