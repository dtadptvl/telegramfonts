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

    Response shape (sanitized real shape): JSON with a `layout` map keyed by
    glyph entries carrying `codePoint` (plus optional metrics/box fields) and
    an `image` base64 sprite. Pages are addressed by `acs_p`; an empty layout
    marks bounded completion. Malformed, mismatched, empty, or incomplete
    results fail closed (None).
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
    def _parse_page(cls, data: Any, page_index: int) -> SpriteRasterPage | None:
        """Parse one bounded real-shape render response; fail closed on any gap."""
        if not isinstance(data, dict):
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
        if not sprite_bytes:
            return None

        glyphs: list[dict] = []
        for entry in layout.values():
            if not isinstance(entry, dict):
                return None
            cp_raw = entry.get("codePoint")
            if not isinstance(cp_raw, int) or cp_raw <= 0:
                return None
            metrics_raw = entry.get("metrics") if isinstance(entry.get("metrics"), dict) else entry
            try:
                metrics = {
                    "advance_width_px": float(metrics_raw.get("advanceWidthPx", metrics_raw.get("aw", 0.0))),
                    "lsb_px": float(metrics_raw.get("lsbPx", metrics_raw.get("lsb", 0.0))),
                    "rsb_px": float(metrics_raw.get("rsbPx", metrics_raw.get("rsb", 0.0))),
                    "ascent_px": float(metrics_raw.get("ascentPx", metrics_raw.get("asc", 0.0))),
                    "descent_px": float(metrics_raw.get("descentPx", metrics_raw.get("desc", 0.0))),
                    "advance_width_upem": float(metrics_raw.get("advanceWidthUpem", metrics_raw.get("awu", 0.0))),
                    "lsb_upem": float(metrics_raw.get("lsbUpem", metrics_raw.get("lsbu", 0.0))),
                    "rsb_upem": float(metrics_raw.get("rsbUpem", metrics_raw.get("rsbu", 0.0))),
                    "ascent_upem": float(metrics_raw.get("ascentUpem", metrics_raw.get("ascu", 0.0))),
                    "descent_upem": float(metrics_raw.get("descentUpem", metrics_raw.get("descu", 0.0))),
                    "bbox_width_upem": float(metrics_raw.get("bboxWidthUpem", metrics_raw.get("bw", 0.0))),
                    "bbox_height_upem": float(metrics_raw.get("bboxHeightUpem", metrics_raw.get("bh", 0.0))),
                }
            except (TypeError, ValueError):
                return None
            glyph_entry: dict = {
                "code_point": cp_raw,
                "resolution": 120,
                "subpixel_x": 0.0,
                "subpixel_y": 0.0,
                "png_base64": image_b64,
                "metrics": metrics,
            }
            box = entry.get("box") if isinstance(entry.get("box"), dict) else None
            if box is not None:
                glyph_entry["sprite_box"] = box
            glyphs.append(glyph_entry)

        payload = {
            "browser_version": cls.BROWSER_VERSION,
            "glyphs": glyphs,
            "pairs": [],
            "features": [],
            "sprite_sha256": hashlib.sha256(sprite_bytes).hexdigest(),
        }
        return SpriteRasterPage(
            page_index=page_index,
            glyph_count=len(glyphs),
            raster_bytes=sprite_bytes,
            next_cursor=str(page_index + 1) if glyphs else "",
            final=not glyphs,
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
                data = resp.json()
        except Exception:
            return None
        return self._parse_page(data, page_index)


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
