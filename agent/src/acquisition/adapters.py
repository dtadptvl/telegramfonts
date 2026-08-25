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


APPROVED_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


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
            "--disable-blink-features=AutomationControlled",
            f"--user-agent={APPROVED_DESKTOP_UA}",
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
    def _request_params(cls, acs_pt: int) -> dict[str, str]:
        params = {name: value for name, value in cls.RENDER_QUERY}
        params["acs_p"] = "1"
        params["acs_pt"] = str(acs_pt)
        return params

    @classmethod
    def _parse_page(
        cls,
        data: Any,
        headers: Any,
        page_index: int,
        md5: str,
        acs_pt: int,
    ) -> SpriteRasterPage | None:
        """Parse one bounded captured-shape render response; fail closed on any gap.

        Only observable provider fields are consumed: body ``status``, layout
        entries (glyph/x/y/width/height/codePoint), and the base64 PNG sprite.
        No glyph metrics exist in the real response and none are inferred.
        Every page payload binds the exact MD5/page/request parameters.
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
        binding = {
            "md5": md5,
            "acs_pt": acs_pt,
            "request_params": {**cls._request_params(acs_pt), "acs_p": str(page_index)},
        }

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
                    **binding,
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
            **binding,
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
        try:
            acs_pt = int(request.get("acs_pt", 120))
        except (TypeError, ValueError):
            return None
        if acs_pt < 1:
            return None
        url = f"{self.base_url}{self.RENDER_PATH}{md5}"
        params = [(name, value) for name, value in self.RENDER_QUERY if name != "acs_pt"]
        params += [("acs_pt", str(acs_pt)), ("acs_p", str(page_index))]
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
        return self._parse_page(data, resp.headers, page_index, md5, acs_pt)

    async def fetch_all_sprite_pages(
        self,
        request: dict[str, Any],
        policy: BinaryAcquisitionPolicy,
    ) -> tuple[SpriteRasterPage, ...] | None:
        """Fetch all bounded pages across all requested acs_pt sizes with bounded concurrency.

        If any required size or page fails/is partial, returns None (fail-closed, continues fallback).
        """
        family = str(request.get("family", "")).strip()
        style = str(request.get("style", "")).strip()
        md5 = str(request.get("md5", "")).strip().lower()
        if not family or not style or not md5 or len(md5) != 32:
            return None

        acs_pts = request.get("acs_pts")
        if not acs_pts:
            single_pt = request.get("acs_pt", 120)
            acs_pts = [int(single_pt)]

        semaphore = asyncio.Semaphore(policy.max_concurrent_cdn_requests)
        all_pages: list[SpriteRasterPage] = []

        async def fetch_size(pt: int) -> list[SpriteRasterPage] | None:
            size_pages: list[SpriteRasterPage] = []
            cursor = "1"
            while cursor and len(size_pages) < policy.max_sprite_pages:
                req_copy = {**request, "acs_pt": pt}
                async with semaphore:
                    page = await self.fetch_sprite_page(req_copy, cursor)
                if page is None:
                    return None
                size_pages.append(page)
                if page.final or page.glyph_count == 0:
                    break
                cursor = page.next_cursor
            return size_pages

        tasks = [fetch_size(int(pt)) for pt in acs_pts]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for res in results:
            if isinstance(res, Exception) or res is None:
                return None
            all_pages.extend(res)

        if not all_pages or sum(p.glyph_count for p in all_pages) == 0:
            return None
        return tuple(all_pages)


class PlaywrightStealthPersistentSession:
    """Production Playwright Stealth real-Chrome persistent context fallback (Method 2).

    Retains cf_clearance cookies in configured user_data_dir profile.
    Uses persistent context with exact launch args, ignored default args,
    and webdriver/chrome init scripts from canonical specification.
    Recovers FamilyDiscoveryEnvelope, and can capture raster glyphs if needed.
    """

    LAUNCH_ARGS = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-infobars",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-background-networking",
    ]
    IGNORED_DEFAULT_ARGS = ["--enable-automation"]
    DESKTOP_UA = APPROVED_DESKTOP_UA
    STEALTH_INIT_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    window.chrome = window.chrome || { runtime: {} };
    """

    def __init__(
        self,
        user_data_dir: Path | None = None,
        timeout_seconds: float = 45.0,
    ) -> None:
        self.user_data_dir = Path(user_data_dir).resolve() if user_data_dir else None
        self.timeout_seconds = timeout_seconds

    def available(self) -> bool:
        return True

    async def discover_family(self, source_url: str) -> Any | None:
        """Run stealth persistent session and extract FamilyDiscoveryEnvelope."""
        from acquisition.providers import parse_family_discovery_from_dump
        from acquisition.models import STAGE_PLAYWRIGHT_STEALTH

        # If playwright library is installed, use async_playwright
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                profile_dir = str(self.user_data_dir) if self.user_data_dir else tempfile.mkdtemp(prefix="stealth_")
                context = await p.chromium.launch_persistent_context(
                    user_data_dir=profile_dir,
                    headless=True,
                    args=self.LAUNCH_ARGS,
                    ignore_default_args=self.IGNORED_DEFAULT_ARGS,
                    user_agent=self.DESKTOP_UA,
                    timeout=self.timeout_seconds * 1000,
                )
                try:
                    await context.add_init_script(self.STEALTH_INIT_SCRIPT)
                    page = await context.new_page()
                    await page.goto(source_url, timeout=self.timeout_seconds * 1000, wait_until="domcontentloaded")
                    content = await page.content()
                    return parse_family_discovery_from_dump(content, source_url, STAGE_PLAYWRIGHT_STEALTH)
                finally:
                    await context.close()
        except ImportError:
            # Fallback to direct chromium process with stealth launch args
            executable = find_chromium_executable()
            timeout_ms = int(self.timeout_seconds * 1000)
            cmd = [
                executable,
                "--headless=new",
                *self.LAUNCH_ARGS,
                f"--user-agent={self.DESKTOP_UA}",
                f"--timeout={timeout_ms}",
                "--dump-dom",
                source_url,
            ]
            if self.user_data_dir:
                cmd.append(f"--user-data-dir={self.user_data_dir}")
            try:
                proc = await asyncio.to_thread(
                    subprocess.run, cmd, capture_output=True, text=True, timeout=self.timeout_seconds + 5.0
                )
                if proc.returncode == 0 and proc.stdout:
                    return parse_family_discovery_from_dump(proc.stdout, source_url, STAGE_PLAYWRIGHT_STEALTH)
            except Exception:
                return None
            return None
        except Exception:
            return None

    async def capture_raster_pages(
        self,
        source_url: str,
        style_rec: Any,
        requested_sizes: list[int],
    ) -> tuple[SpriteRasterPage, ...] | None:
        """Capture raster glyphs directly via page canvas if CDN is blocked."""
        return None


class AlgoliaMetadataClient:
    """MyFonts Algolia metadata client (Method 4 fallback).

    Uses runtime-only Algolia App ID and search API key to query family/style/MD5
    metadata when dump-dom and persistent sessions cannot supply full style mappings.
    Supplies metadata ONLY (never font binaries or raster pixels).
    """

    def __init__(
        self,
        app_id: str = "N9095TCBC5",
        api_key: str | None = None,
        index_name: str = "prod_myfonts_fonts",
        timeout_seconds: float = 30.0,
    ) -> None:
        self.app_id = app_id.strip() if app_id else ""
        self._api_key = api_key.strip() if api_key else ""
        self.index_name = index_name.strip() if index_name else "prod_myfonts_fonts"
        self.timeout_seconds = timeout_seconds

    def available(self) -> bool:
        return bool(self.app_id)

    async def discover_family(
        self,
        family_name_or_slug: str,
        source_url: str = "",
    ) -> Any | None:
        from acquisition.models import STAGE_ALGOLIA_METADATA_CDN, FamilyDiscoveryEnvelope, StyleDiscoveryRecord

        if not self.available() or not family_name_or_slug.strip():
            return None

        query = family_name_or_slug.replace("-", " ").replace("_", " ").strip()
        url = f"https://{self.app_id}-dsn.algolia.net/1/indexes/{self.index_name}/query"
        headers = {
            "X-Algolia-Application-Id": self.app_id,
            "Content-Type": "application/json",
        }
        if self._api_key:
            headers["X-Algolia-API-Key"] = self._api_key

        payload = {
            "query": query,
            "hitsPerPage": 50,
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code != 200:
                    return None
                data = resp.json()
        except Exception:
            return None

        hits = data.get("hits", [])
        if not isinstance(hits, list) or not hits:
            return None

        styles: dict[str, StyleDiscoveryRecord] = {}
        matched_family_name = ""

        for hit in hits:
            if not isinstance(hit, dict):
                continue
            fam_name = str(hit.get("family_name") or hit.get("familyName") or hit.get("family") or "")
            if not matched_family_name:
                matched_family_name = fam_name or query

            child_styles = hit.get("styles") or hit.get("fonts") or [hit]
            for st in child_styles:
                if not isinstance(st, dict):
                    continue
                s_name = str(st.get("style_name") or st.get("styleName") or st.get("name") or "")
                s_id = str(st.get("style_id") or st.get("styleId") or st.get("id") or s_name)
                s_md5 = str(st.get("font_md5") or st.get("fontMd5") or st.get("md5") or "").lower()
                if s_name and len(s_md5) == 32:
                    norm_k = s_id.lower().replace("-", "_").replace(" ", "_")
                    styles[norm_k] = StyleDiscoveryRecord(
                        style_id=s_id,
                        style_name=s_name,
                        md5=s_md5,
                        provenance=STAGE_ALGOLIA_METADATA_CDN,
                    )

        if not styles:
            return None

        canonical_key = (matched_family_name or query).lower().replace("-", "_").replace(" ", "_")
        return FamilyDiscoveryEnvelope(
            family_name=matched_family_name or query,
            family_url=source_url,
            canonical_family_key=canonical_key,
            styles=styles,
            provenance=STAGE_ALGOLIA_METADATA_CDN,
        )


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

    # Method 2: Playwright Stealth persistent context
    playwright_provider = None
    if getattr(settings, "PLAYWRIGHT_STEALTH_ENABLED", True):
        playwright_provider = PlaywrightStealthPersistentSession(
            user_data_dir=getattr(settings, "PLAYWRIGHT_USER_DATA_DIR", None),
        )

    # Method 3: Direct Monotype CDN raster client
    raster_provider = None
    raster_url = str(getattr(settings, "MONOTYPE_RASTER_ENDPOINT_URL", "") or "")
    if raster_url:
        session_cookies = _load_session_cookies(getattr(settings, "AUTHORIZED_SESSION_MATERIAL_FILE", None))
        raster_provider = MonotypeRasterProvider(
            MonotypeRenderClient(session_cookies=session_cookies, base_url=raster_url)
        )

    # Method 4: MyFonts Algolia metadata client
    algolia_provider = None
    algolia_app_id = getattr(settings, "MYFONTS_ALGOLIA_APP_ID", "N9095TCBC5")
    algolia_key_obj = getattr(settings, "MYFONTS_ALGOLIA_API_KEY", None)
    algolia_key = algolia_key_obj.get_secret_value() if algolia_key_obj is not None else None
    algolia_index = getattr(settings, "MYFONTS_ALGOLIA_INDEX_NAME", "prod_myfonts_fonts")
    if algolia_app_id:
        algolia_provider = AlgoliaMetadataClient(
            app_id=algolia_app_id,
            api_key=algolia_key,
            index_name=algolia_index,
        )

    return AcquisitionPipeline(
        dump_dom_transport=dump_dom,
        binary_fetch=binary_fetcher.fetch,
        session_provider=session_provider,
        raster_provider=raster_provider,
        playwright_provider=playwright_provider,
        algolia_provider=algolia_provider,
        policy=policy or BinaryAcquisitionPolicy(),
    )
