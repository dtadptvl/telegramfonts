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


class MonotypeRasterHttpClient:
    """Authorized Monotype raster endpoint client (MD5/style-bound closed schema).

    Every page request carries the exact family/style/MD5 target; responses
    must echo the same target identity or they are rejected fail-closed.
    """

    def __init__(self, endpoint_url: str, token: str, timeout_seconds: float = 60.0) -> None:
        if not endpoint_url or not token:
            raise ValueError("MONOTYPE_RASTER_CONFIG_REQUIRED")
        self.endpoint_url = endpoint_url
        self._token = token
        self.timeout_seconds = timeout_seconds

    async def fetch_sprite_page(self, request: dict[str, Any], cursor: str) -> SpriteRasterPage | None:
        family = str(request.get("family", "")).strip()
        style = str(request.get("style", "")).strip()
        md5 = str(request.get("md5", "")).strip().lower()
        if not family or not style or not md5 or len(md5) != 32:
            return None
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.post(
                    self.endpoint_url,
                    headers={"Authorization": f"Bearer {self._token}"},
                    json={
                        "family": family,
                        "style": style,
                        "md5": md5,
                        "cursor": cursor,
                    },
                )
                if resp.status_code != 200:
                    return None
                data = resp.json()
                if not isinstance(data, dict):
                    return None
                # Cross-style/wrong-target responses fail closed.
                if str(data.get("family", family)).strip() != family:
                    return None
                if str(data.get("style", style)).strip() != style:
                    return None
                if str(data.get("md5", md5)).strip().lower() != md5:
                    return None
                payload = data.get("payload")
                if not isinstance(payload, dict):
                    return None
                glyphs = payload.get("glyphs")
                if not isinstance(glyphs, list):
                    return None
                return SpriteRasterPage(
                    page_index=int(data.get("page_index", 0)),
                    glyph_count=len(glyphs),
                    raster_bytes=b"",
                    next_cursor=str(data.get("next_cursor", "")),
                    final=bool(data.get("final", True)),
                    payload=payload,
                )
        except Exception:
            return None


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
    raster_token = getattr(settings, "MONOTYPE_RASTER_TOKEN", None)
    token_value = raster_token.get_secret_value() if raster_token is not None else ""
    if raster_url and token_value:
        raster_provider = MonotypeRasterProvider(MonotypeRasterHttpClient(raster_url, token_value))

    return AcquisitionPipeline(
        dump_dom_transport=dump_dom,
        binary_fetch=binary_fetcher.fetch,
        session_provider=session_provider,
        raster_provider=raster_provider,
        policy=policy or BinaryAcquisitionPolicy(),
    )
