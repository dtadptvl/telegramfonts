"""Concrete production acquisition adapters.

All adapters are real production types (no test seams in production assembly).
Secret/session material is consumed opaquely and never logged, raised, or
embedded in artifacts. Readiness fails closed when an enabled required
capability cannot be constructed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from pathlib import Path
from typing import Any

import base64
import hashlib

import httpx

from acquisition.models import BinaryAcquisitionPolicy, DiscoveryEnvelope, SpriteRasterPage
from acquisition.pipeline import AcquisitionPipeline
from acquisition.providers import (
    MonotypeRasterProvider,
    PersistentSessionBinaryProvider,
    classify_font_container,
    looks_like_font_bytes,
)
from measurement.browser_session import find_chromium_executable

logger = logging.getLogger("telegramfonts.agent.acquisition.adapters")

_FONT_URL_SUFFIXES = (".ttf", ".otf", ".woff", ".woff2")


class HeadlessDumpDomTransport:
    """Primary capability: native Chrome `--headless=new --dump-dom`."""

    def __init__(self, timeout_seconds: float = 45.0) -> None:
        self.timeout_seconds = timeout_seconds

    async def dump_dom(self, url: str) -> str:
        executable = find_chromium_executable()
        timeout_ms = int(self.timeout_seconds * 1000)
        cmd = [
            executable,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-extensions",
            f"--timeout={timeout_ms}",
            "--dump-dom",
            url,
        ]

        def _run() -> str:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds + 10.0,
            )
            if proc.returncode != 0:
                raise RuntimeError("DUMP_DOM_FAILED")
            return proc.stdout

        return await asyncio.to_thread(_run)


class HttpBinaryFetcher:
    """Bounded HTTPS fetcher for binary URLs discovered inside dumps."""

    def __init__(self, timeout_seconds: float = 30.0, max_bytes: int = 10 * 1024 * 1024) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes

    async def fetch(self, url: str) -> bytes | None:
        lower = url.lower().split("?", 1)[0]
        if not url.startswith("https://") or not lower.endswith(_FONT_URL_SUFFIXES):
            return None
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=True) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    return None
                raw = resp.content
                if not raw or len(raw) > self.max_bytes:
                    return None
                return raw
        except Exception:
            return None


class AuthorizedSessionMaterialStore:
    """Opaque authorized-session material from a runtime secret file.

    The file is a deployment secret (path via settings); contents are consumed
    in-memory only and never logged or embedded in artifacts.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path

    async def material(self) -> dict[str, Any] | None:
        if self.path is None:
            return None
        try:
            resolved = Path(self.path).expanduser()
            if not resolved.is_file():
                return None
            payload = json.loads(resolved.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or not payload:
                return None
            return payload
        except Exception:
            return None


class AuthorizedSessionHttpTransport:
    """Fallback capability: authorized persistent Chrome/session discovery.

    Fetches authorized binary candidates discovered in the typed envelope using
    opaque session material. Collection-page HTML is never fetched as a font
    candidate; responses are magic-classified and container-converted before
    being returned.
    """

    def __init__(self, timeout_seconds: float = 30.0, max_bytes: int = 10 * 1024 * 1024) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes

    async def discover(
        self,
        envelope: DiscoveryEnvelope,
        source_url: str,
        session_material: dict[str, Any],
    ) -> bytes | None:
        candidates = [c for c in envelope.binary_candidates if not c.embedded]
        if not candidates:
            return None
        headers = {}
        cookies = {}
        raw_headers = session_material.get("headers")
        if isinstance(raw_headers, dict):
            headers = {str(k): str(v) for k, v in raw_headers.items()}
        raw_cookies = session_material.get("cookies")
        if isinstance(raw_cookies, dict):
            cookies = {str(k): str(v) for k, v in raw_cookies.items()}
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, follow_redirects=True, cookies=cookies or None
            ) as client:
                for candidate in candidates:
                    if not candidate.url.startswith("https://"):
                        continue
                    resp = await client.get(candidate.url, headers=headers or None)
                    if resp.status_code != 200:
                        continue
                    raw = resp.content
                    if not raw or len(raw) > self.max_bytes:
                        continue
                    if not looks_like_font_bytes(raw):
                        # HTML or unknown payloads are never font candidates.
                        continue
                    container = classify_font_container(raw)
                    if container in ("TTF", "OTF"):
                        return raw
                    if container in ("WOFF", "WOFF2"):
                        from compute.binary_gate import convert_container_to_sfnt

                        converted = convert_container_to_sfnt(raw)
                        if converted and len(converted) <= self.max_bytes:
                            return converted
                return None
        except Exception:
            return None


class MonotypeRenderClient:
    """Authorized Monotype CDN raster client (real MD5-bound render protocol).

    Request family: HTTPS GET https://sig.monotype.com/render/105/font/{md5}
    with the approved render query contract (rbe=gmap, acs_pt/acs_w/acs_l/
    acs_ar/acs_p/acs_gpp) and browser render headers (User-Agent plus
    myfonts Referer/Origin). No generic POST/Bearer JSON. Session material is
    runtime-only (opaque cookies) and never logged or embedded in artifacts.

    Response shape (captured live read-only diagnostics, Issue #71 comment
    5412717546): HTTP 200 with Content-Type ``application/json``; body
    ``{"status": 200, "layout": {key: {"glyph", "x", "y", "width", "height",
    "codePoint"}}, "image": "<base64 PNG sprite>"}``. Layout entries carry
    sprite-cell coordinates only — no glyph metrics exist in the real
    response and none are inferred; metrics/pairs/features are never consumed
    from this endpoint (raster-only source). Pages are addressed by ``acs_p``;
    an empty ``layout`` marks bounded completion (captured at an out-of-range
    page). Response headers may expose ``max-glyphs-per-page`` /
    ``X-Missing-Unicodes`` / ``X-Tofus-Found``; present values are preserved
    as observed evidence. Malformed, mismatched, empty, or incomplete results
    fail closed (None).
    """

    RENDER_PATH = "/render/105/font/"
    RENDER_QUERY = (
        ("rbe", "gmap"),
        ("acs_pt", "120"),
        ("acs_w", "1500"),
        ("acs_l", "1"),
        ("acs_ar", "0"),
        ("acs_gpp", "100"),
    )
    RENDER_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    RENDER_REFERER = "https://www.myfonts.com/"
    RENDER_ORIGIN = "https://www.myfonts.com"
    BROWSER_VERSION = "monotype_render_105"
    PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
    MAX_SPRITE_BYTES = 8 * 1024 * 1024

    def __init__(
        self,
        session_cookies: dict[str, str] | None = None,
        base_url: str = "https://sig.monotype.com",
        timeout_seconds: float = 60.0,
    ) -> None:
        self._session_cookies = dict(session_cookies or {})
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.RENDER_USER_AGENT,
            "Referer": self.RENDER_REFERER,
            "Origin": self.RENDER_ORIGIN,
        }

    @classmethod
    def _observed_headers(cls, headers: Any) -> dict[str, Any]:
        """Preserve only the real observed response evidence (never secrets)."""
        observed: dict[str, Any] = {}
        try:
            content_type = headers.get("content-type", "")
        except Exception:
            return observed
        if content_type:
            observed["content_type"] = str(content_type)
        for header, key in (
            ("max-glyphs-per-page", "max_glyphs_per_page"),
            ("x-missing-unicodes", "x_missing_unicodes"),
            ("x-tofus-found", "x_tofus_found"),
        ):
            try:
                value = headers.get(header)
            except Exception:
                value = None
            if value is None or str(value).strip() == "":
                continue
            if key == "max_glyphs_per_page":
                try:
                    observed[key] = int(str(value).strip())
                except ValueError:
                    continue
            else:
                observed[key] = str(value)
        return observed

    @classmethod
    def _parse_page(
        cls, data: Any, headers: Any, page_index: int
    ) -> SpriteRasterPage | None:
        """Parse one bounded captured-shape render response; fail closed on any gap.

        Only observable provider fields are consumed: body ``status``, layout
        entries (glyph/x/y/width/height/codePoint), and the base64 PNG sprite.
        No glyph metrics exist in the real response and none are inferred.
        """
        if not isinstance(data, dict):
            return None
        if data.get("status") != 200:
            return None
        layout = data.get("layout")
        if not isinstance(layout, dict):
            return None
        image_b64 = data.get("image")
        if not isinstance(image_b64, str) or not image_b64:
            return None
        try:
            sprite_bytes = base64.b64decode(image_b64, validate=True)
        except (ValueError, TypeError):
            return None
        if (
            not sprite_bytes
            or len(sprite_bytes) > cls.MAX_SPRITE_BYTES
            or not sprite_bytes.startswith(cls.PNG_MAGIC)
        ):
            return None
        observed = cls._observed_headers(headers)

        if not layout:
            # Captured bounded-completion signal: empty layout at an
            # out-of-range page; the placeholder sprite carries no evidence.
            return SpriteRasterPage(
                page_index=page_index,
                glyph_count=0,
                raster_bytes=sprite_bytes,
                next_cursor="",
                final=True,
                payload={
                    "browser_version": cls.BROWSER_VERSION,
                    "glyphs": [],
                    "pairs": [],
                    "features": [],
                    "sprite_sha256": hashlib.sha256(sprite_bytes).hexdigest(),
                    "observed_headers": observed,
                },
            )

        glyphs: list[dict] = []
        unmapped_slots = 0
        for entry in layout.values():
            if not isinstance(entry, dict):
                return None
            try:
                box = {
                    "x": int(entry["x"]),
                    "y": int(entry["y"]),
                    "width": int(entry["width"]),
                    "height": int(entry["height"]),
                }
                glyph_index = int(entry.get("glyph", -1))
                cp_raw = entry["codePoint"]
            except (KeyError, TypeError, ValueError):
                return None
            if box["width"] < 1 or box["height"] < 1 or box["x"] < 0 or box["y"] < 0:
                return None
            if not isinstance(cp_raw, int) or cp_raw <= 0:
                # Observable unmapped glyph slot (e.g. .notdef): never bound
                # to a code point and never invented.
                unmapped_slots += 1
                continue
            glyphs.append(
                {
                    "code_point": cp_raw,
                    "glyph_index": glyph_index,
                    "sprite_box": box,
                }
            )
        if not glyphs:
            # Non-empty layout with zero observable code-point bindings fails
            # closed; nothing may be ingested or inferred.
            return None

        payload = {
            "browser_version": cls.BROWSER_VERSION,
            "glyphs": glyphs,
            "pairs": [],
            "features": [],
            "sprite_sha256": hashlib.sha256(sprite_bytes).hexdigest(),
            "observed_headers": observed,
            "unmapped_glyph_slots": unmapped_slots,
        }
        return SpriteRasterPage(
            page_index=page_index,
            glyph_count=len(glyphs),
            raster_bytes=sprite_bytes,
            next_cursor=str(page_index + 1),
            final=False,
            payload=payload,
        )

    async def fetch_sprite_page(self, request: dict[str, Any], cursor: str) -> SpriteRasterPage | None:
        family = str(request.get("family", "")).strip()
        style = str(request.get("style", "")).strip()
        md5 = str(request.get("md5", "")).strip().lower()
        if not family or not style or not md5 or len(md5) != 32:
            return None
        try:
            page_index = int(cursor) if cursor else 1
        except ValueError:
            return None
        if page_index < 1:
            return None
        url = f"{self.base_url}{self.RENDER_PATH}{md5}"
        params = list(self.RENDER_QUERY) + [("acs_p", str(page_index))]
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                cookies=self._session_cookies or None,
                follow_redirects=True,
            ) as client:
                resp = await client.get(url, params=params, headers=self._headers())
                if resp.status_code != 200:
                    return None
                content_type = str(resp.headers.get("content-type", ""))
                if not content_type.lower().startswith("application/json"):
                    return None
                data = resp.json()
        except Exception:
            return None
        return self._parse_page(data, resp.headers, page_index)


# Backward-compatible alias for composition wiring.
MonotypeRasterHttpClient = MonotypeRenderClient


def _load_session_cookies(material_path: Any) -> dict[str, str]:
    """Opaque runtime-only session cookies; never logged or embedded."""
    if material_path is None:
        return {}
    try:
        resolved = Path(material_path).expanduser()
        if not resolved.is_file():
            return {}
        payload = json.loads(resolved.read_text(encoding="utf-8"))
        cookies = payload.get("cookies") if isinstance(payload, dict) else None
        if isinstance(cookies, dict):
            return {str(k): str(v) for k, v in cookies.items()}
        return {}
    except Exception:
        return {}


def build_production_acquisition_pipeline(
    settings: Any,
    policy: BinaryAcquisitionPolicy | None = None,
) -> AcquisitionPipeline | None:
    """Construct the production acquisition pipeline; fail closed on readiness gaps."""
    if not getattr(settings, "ACQUISITION_ENABLED", False):
        return None

    # Required primary capability must be constructible (fail closed).
    try:
        find_chromium_executable()
    except Exception as exc:
        raise RuntimeError("ACQUISITION_READINESS_FAILED_CHROMIUM") from exc

    dump_dom = HeadlessDumpDomTransport()
    binary_fetcher = HttpBinaryFetcher()

    session_provider = None
    material_path = getattr(settings, "AUTHORIZED_SESSION_MATERIAL_FILE", None)
    if material_path is not None:
        session_provider = PersistentSessionBinaryProvider(
            AuthorizedSessionMaterialStore(Path(material_path)),
            AuthorizedSessionHttpTransport(),
        )

    raster_provider = None
    raster_url = str(getattr(settings, "MONOTYPE_RASTER_ENDPOINT_URL", "") or "")
    if raster_url:
        session_cookies = _load_session_cookies(getattr(settings, "AUTHORIZED_SESSION_MATERIAL_FILE", None))
        raster_provider = MonotypeRasterProvider(
            MonotypeRenderClient(session_cookies=session_cookies, base_url=raster_url)
        )

    return AcquisitionPipeline(
        dump_dom_transport=dump_dom,
        binary_fetch=binary_fetcher.fetch,
        session_provider=session_provider,
        raster_provider=raster_provider,
        policy=policy or BinaryAcquisitionPolicy(),
    )
