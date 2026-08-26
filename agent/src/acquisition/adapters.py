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
            cursor = ""
            for _ in range(policy.max_sprite_pages):
                req_copy = {**request, "acs_pt": pt}
                async with semaphore:
                    page = await self.fetch_sprite_page(req_copy, cursor)
                if page is None:
                    break
                size_pages.append(page)
                if page.final or not page.next_cursor or page.glyph_count == 0:
                    break
                cursor = page.next_cursor
            if not size_pages:
                return None
            for p in size_pages:
                for g in (p.payload or {}).get("glyphs", []):
                    box = g.get("sprite_box", {})
                    if box.get("width", 0) <= 0 or box.get("height", 0) <= 0:
                        return None
            return size_pages

        tasks = [fetch_size(int(pt)) for pt in acs_pts]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for res in results:
            if isinstance(res, Exception) or res is None:
                return None
            all_pages.extend(res)

        from acquisition.models import is_complete_raster_pages
        if not is_complete_raster_pages(all_pages, requested_pts=[int(pt) for pt in acs_pts], expected_md5=md5):
            return None
        return tuple(all_pages)


def extract_font_descriptors(style_name: str, style_id: str = "") -> dict[str, str]:
    """Extract closed normalized font descriptors (style, weight, stretch) from style name/id."""
    combined = f"{style_name} {style_id}".lower().replace("-", " ").replace("_", " ")

    # Style
    style = "normal"
    if "italic" in combined or "oblique" in combined:
        style = "italic"

    # Weight (100..900)
    if "extra light" in combined or "extralight" in combined or "ultra light" in combined or "ultralight" in combined or " 200" in combined or combined.endswith("200"):
        weight = "200"
    elif "semi bold" in combined or "semibold" in combined or "demi bold" in combined or "demibold" in combined or " 600" in combined or combined.endswith("600"):
        weight = "600"
    elif "extra bold" in combined or "extrabold" in combined or "ultra bold" in combined or "ultrabold" in combined or " 800" in combined or combined.endswith("800"):
        weight = "800"
    elif "thin" in combined or "hairline" in combined or " 100" in combined or combined.endswith("100"):
        weight = "100"
    elif "light" in combined or "book" in combined or " 300" in combined or combined.endswith("300"):
        weight = "300"
    elif "medium" in combined or " 500" in combined or combined.endswith("500"):
        weight = "500"
    elif "black" in combined or "heavy" in combined or " 900" in combined or combined.endswith("900"):
        weight = "900"
    elif "bold" in combined or " 700" in combined or combined.endswith("700"):
        weight = "700"
    else:
        weight = "400"

    # Stretch
    stretch = "normal"
    if "condensed" in combined or "narrow" in combined:
        stretch = "condensed"
    elif "expanded" in combined or "wide" in combined:
        stretch = "expanded"

    return {
        "style": style,
        "weight": weight,
        "stretch": stretch,
    }


CANVAS_EVALUATOR_SCRIPT: str = """
async (args) => {
    const { requested_sizes, style_name, style_id, expected_md5, observed_font_responses } = args || {};

    function normWeight(w) {
        if (!w) return "400";
        const sw = String(w).toLowerCase();
        if (sw === "normal" || sw === "regular" || sw === "roman" || sw === "plain") return "400";
        if (sw === "bold") return "700";
        if (sw === "bolder" || sw === "extrabold" || sw === "extra bold" || sw === "ultrabold" || sw === "ultra bold") return "800";
        if (sw === "semibold" || sw === "semi bold" || sw === "demibold" || sw === "demi bold") return "600";
        if (sw === "lighter" || sw === "light" || sw === "book") return "300";
        if (sw === "extralight" || sw === "extra light" || sw === "ultralight" || sw === "ultra light") return "200";
        if (sw === "thin" || sw === "hairline") return "100";
        if (sw === "black" || sw === "heavy") return "900";
        if (sw === "medium") return "500";
        const num = parseInt(sw, 10);
        if (!isNaN(num)) return String(num);
        return "400";
    }
    function normStyle(s) {
        if (!s) return "normal";
        const ss = String(s).toLowerCase();
        if (ss.includes("italic") || ss.includes("oblique")) return "italic";
        return "normal";
    }
    function normStretch(st) {
        if (!st) return "normal";
        const sst = String(st).toLowerCase();
        if (sst.includes("condensed") || sst.includes("narrow")) return "condensed";
        if (sst.includes("expanded") || sst.includes("wide")) return "expanded";
        return "normal";
    }

    function extractDescriptors(nameStr, idStr) {
        const combined = (nameStr + " " + idStr).toLowerCase().replace(/[-_]/g, " ");
        let style = "normal";
        if (combined.includes("italic") || combined.includes("oblique")) {
            style = "italic";
        }
        let weight = "400";
        if (combined.includes("extra light") || combined.includes("extralight") || combined.includes("ultra light") || combined.includes("ultralight") || combined.includes(" 200") || combined.endsWith("200")) {
            weight = "200";
        } else if (combined.includes("semi bold") || combined.includes("semibold") || combined.includes("demi bold") || combined.includes("demibold") || combined.includes(" 600") || combined.endsWith("600")) {
            weight = "600";
        } else if (combined.includes("extra bold") || combined.includes("extrabold") || combined.includes("ultra bold") || combined.includes("ultrabold") || combined.includes(" 800") || combined.endsWith("800")) {
            weight = "800";
        } else if (combined.includes("thin") || combined.includes("hairline") || combined.includes(" 100") || combined.endsWith("100")) {
            weight = "100";
        } else if (combined.includes("light") || combined.includes("book") || combined.includes(" 300") || combined.endsWith("300")) {
            weight = "300";
        } else if (combined.includes("medium") || combined.includes(" 500") || combined.endsWith("500")) {
            weight = "500";
        } else if (combined.includes("black") || combined.includes("heavy") || combined.includes(" 900") || combined.endsWith("900")) {
            weight = "900";
        } else if (combined.includes("bold") || combined.includes(" 700") || combined.endsWith("700")) {
            weight = "700";
        } else {
            weight = "400";
        }
        let stretch = "normal";
        if (combined.includes("condensed") || combined.includes("narrow")) {
            stretch = "condensed";
        } else if (combined.includes("expanded") || combined.includes("wide")) {
            stretch = "expanded";
        }
        return { style, weight, stretch };
    }

    const targetDesc = extractDescriptors(style_name || "", style_id || "");
    const normTargetFamily = (style_name || "").toLowerCase().replace(/['"]/g, "").replace(/[^a-z0-9]/g, "");

    // 1. Scan @font-face rules from stylesheets
    const fontFaceRules = [];
    try {
        for (const sheet of document.styleSheets) {
            try {
                const rules = sheet.cssRules || sheet.rules;
                if (!rules) continue;
                for (const r of rules) {
                    if (r instanceof CSSFontFaceRule || r.type === 5 || (r.cssText && r.cssText.startsWith("@font-face"))) {
                        const fStyle = r.style || {};
                        fontFaceRules.push({
                            family: (fStyle.fontFamily || "").replace(/['"]/g, "").trim(),
                            style: normStyle(fStyle.fontStyle),
                            weight: normWeight(fStyle.fontWeight),
                            stretch: normStretch(fStyle.fontStretch),
                            unicodeRange: fStyle.unicodeRange || "",
                            src: fStyle.src || r.cssText || "",
                        });
                    }
                }
            } catch (e) {}
        }
    } catch (e) {}

    // Performance resource URLs for loaded font files
    const perfResourceUrls = [];
    try {
        const perfEntries = performance.getEntriesByType("resource") || [];
        for (const pe of perfEntries) {
            if (pe.name && (pe.initiatorType === "font" || pe.initiatorType === "css" || pe.name.includes(".woff") || pe.name.includes(".ttf") || pe.name.includes(".otf"))) {
                const okStatus = (pe.responseStatus === undefined || pe.responseStatus === 0 || (pe.responseStatus >= 200 && pe.responseStatus < 400));
                const hasBytes = (pe.decodedBodySize > 0 || pe.transferSize > 0 || pe.duration > 0);
                if (okStatus && (hasBytes || pe.responseStatus >= 200)) {
                    perfResourceUrls.push(pe.name);
                }
            }
        }
    } catch (e) {}

    // Sealed observed font responses from Playwright network observer
    const networkFontUrls = (observed_font_responses || [])
        .filter(r => r && r.status >= 200 && r.status < 400 && r.url)
        .map(r => r.url);

    const verifiedLoadedUrls = Array.from(new Set([...perfResourceUrls, ...networkFontUrls]));

    // 2. Resolve matching FontFace candidates
    const candidates = [];
    for (const face of document.fonts) {
        const faceFamNorm = face.family.toLowerCase().replace(/['"]/g, "").replace(/[^a-z0-9]/g, "");
        const faceStyle = normStyle(face.style);
        const faceWeight = normWeight(face.weight);
        const faceStretch = normStretch(face.stretch);

        const familyMatches = (faceFamNorm === normTargetFamily || normTargetFamily.includes(faceFamNorm) || faceFamNorm.includes(normTargetFamily));
        if (!familyMatches) continue;

        // Exact descriptor matching
        if (faceStyle !== targetDesc.style) continue;
        if (faceWeight !== targetDesc.weight) continue;
        if (faceStretch !== targetDesc.stretch) continue;

        // Match with @font-face rule if available
        let matchedRule = null;
        for (const rule of fontFaceRules) {
            const rFamNorm = rule.family.toLowerCase().replace(/['"]/g, "").replace(/[^a-z0-9]/g, "");
            if (rFamNorm === faceFamNorm && rule.weight === faceWeight && rule.style === faceStyle) {
                matchedRule = rule;
                break;
            }
        }

        let ruleSrc = matchedRule ? matchedRule.src : "";
        let ruleUnicode = matchedRule ? matchedRule.unicodeRange : "";
        let resMd5 = "";
        let resUrl = "";

        if (expected_md5) {
            const lowerExpected = expected_md5.toLowerCase();
            // Cryptographic causal attestation:
            // A font resource URL containing expected_md5 MUST have been successfully loaded with 2xx status.
            // CSS rule declaration text alone is metadata, never proof of actual font loading!
            let verifiedUrlMatch = "";
            for (const vUrl of verifiedLoadedUrls) {
                if (vUrl.toLowerCase().includes(lowerExpected)) {
                    verifiedUrlMatch = vUrl;
                    break;
                }
            }

            if (verifiedUrlMatch) {
                resMd5 = lowerExpected;
                resUrl = verifiedUrlMatch;
            } else {
                // If expected_md5 URL failed or was absent, and Chrome fell back to local('...'), reject candidate!
                continue;
            }
        } else {
            // Find any 32-hex in verified loaded URLs
            for (const vUrl of verifiedLoadedUrls) {
                const hexes = vUrl.match(/[0-9a-fA-F]{32}/g);
                if (hexes && hexes.length > 0) {
                    resMd5 = hexes[0].toLowerCase();
                    resUrl = vUrl;
                    break;
                }
            }
        }

        candidates.push({
            face,
            style: faceStyle,
            weight: faceWeight,
            stretch: faceStretch,
            unicodeRange: face.unicodeRange || ruleUnicode || "",
            src: ruleSrc,
            resourceMd5: resMd5,
            resourceUrl: resUrl,
        });
    }

    if (candidates.length === 0) {
        if (expected_md5) {
            return { error: "STEALTH_MD5_RESOURCE_NOT_LOADED" };
        }
        return { error: "NO_MATCHING_LOADED_FONT_FACE" };
    }
    if (candidates.length > 1) {
        return { error: "STEALTH_FACE_IDENTITY_AMBIGUOUS" };
    }

    const matched = candidates[0];
    const matchedFace = matched.face;
    const fontStyle = matched.style;
    const fontWeight = matched.weight;
    const fontStretch = matched.stretch;
    const fontFamily = matchedFace.family.replace(/['"]/g, "");

    const resolvedFace = {
        family: fontFamily,
        style: fontStyle,
        weight: fontWeight,
        stretch: fontStretch,
        unicodeRange: matched.unicodeRange,
        status: matchedFace.status || "loaded",
        src: matched.src,
        resource_md5: matched.resourceMd5,
        resource_url: matched.resourceUrl,
    };

    function getExactFontSpec(ptSize) {
        return `${fontStyle} ${fontWeight} ${ptSize}px "${fontFamily}"`;
    }

    const fontSpec60 = getExactFontSpec(60);
    try {
        await document.fonts.load(fontSpec60);
    } catch (e) {
        return { error: "FONT_LOAD_EXCEPTION" };
    }
    if (!document.fonts.check(fontSpec60)) {
        return { error: "FONT_NOT_LOADED" };
    }

    // 3. Discover target coverage from FontFace unicodeRange with wildcard expansion
    let requiredSourceCps = [];
    const rawUnicodeRange = resolvedFace.unicodeRange;
    if (rawUnicodeRange) {
        const ranges = rawUnicodeRange.split(",");
        const declaredSet = new Set();
        let totalRangeSpan = 0;
        for (const r of ranges) {
            const clean = r.trim().replace(/^U\\+/i, "");
            let start = -1, end = -1;
            if (clean.includes("-")) {
                const parts = clean.split("-");
                start = parseInt(parts[0], 16);
                end = parseInt(parts[1], 16);
            } else if (clean.includes("?")) {
                // Wildcard expansion: U+4?? -> 0x0400..0x04FF
                const sHex = clean.replace(/\\?/g, "0");
                const eHex = clean.replace(/\\?/g, "F");
                start = parseInt(sHex, 16);
                end = parseInt(eHex, 16);
            } else {
                start = parseInt(clean, 16);
                end = start;
            }

            if (isNaN(start) || isNaN(end) || end < start) {
                return { error: "INVALID_UNICODE_RANGE_SPECIFICATION" };
            }

            totalRangeSpan += (end - start + 1);
            if (totalRangeSpan > 1500) {
                return { error: "UNICODE_RANGE_EXCEEDS_BOUNDED_POLICY" };
            }
            for (let cp = start; cp <= end; cp++) {
                declaredSet.add(cp);
            }
        }
        requiredSourceCps = Array.from(declaredSet).sort((a, b) => a - b);
    } else {
        for (let i = 32; i <= 126; i++) {
            requiredSourceCps.push(i);
        }
    }

    const candidateSet = new Set(requiredSourceCps);
    const extraCps = [
        192, 193, 194, 195, 200, 201, 202, 204, 205, 210, 211, 212, 213, 217, 218, 221,
        224, 225, 226, 227, 232, 233, 234, 236, 237, 242, 243, 244, 245, 249, 250, 253,
        272, 273, 416, 417, 431, 432,
        7840, 7841, 7842, 7843, 7844, 7845, 7846, 7847, 7848, 7849, 7850, 7851, 7852, 7853,
        7854, 7855, 7856, 7857, 7858, 7859, 7860, 7861, 7862, 7863, 7864, 7865, 7866, 7867,
        7868, 7869, 7870, 7871, 7872, 7873, 7874, 7875, 7876, 7877, 7878, 7879, 7880, 7881,
        7882, 7883, 7884, 7885, 7886, 7887, 7888, 7889, 7890, 7891, 7892, 7893, 7894, 7895,
        7896, 7897, 7898, 7899, 7900, 7901, 7902, 7903, 7904, 7905, 7906, 7907, 7908, 7909,
        7910, 7911, 7912, 7913, 7914, 7915, 7916, 7917, 7918, 7919, 7920, 7921, 7922, 7923,
        7924, 7925, 7926, 7927, 7928, 7929
    ];
    for (const cp of extraCps) candidateSet.add(cp);
    const candidateCodePoints = Array.from(candidateSet).sort((a, b) => a - b);

    const results = [];
    for (const pt of requested_sizes) {
        const cellDim = Math.ceil(pt * 2.2);
        const cellCanvas = document.createElement("canvas");
        cellCanvas.width = cellDim;
        cellCanvas.height = cellDim;
        const cellCtx = cellCanvas.getContext("2d", { willReadFrequently: true });

        const exactFontSpec = getExactFontSpec(pt);
        const sansFontSpec = `${fontStyle} ${fontWeight} ${pt}px sans-serif`;
        const monoFontSpec = `${fontStyle} ${fontWeight} ${pt}px monospace`;

        const provenGlyphs = [];
        const provenCodePoints = [];
        const rejectedCodePoints = [];

        for (let i = 0; i < candidateCodePoints.length; i++) {
            const cp = candidateCodePoints[i];
            const ch = String.fromCodePoint(cp);

            // 1. Render in target font
            cellCtx.clearRect(0, 0, cellDim, cellDim);
            cellCtx.font = exactFontSpec;
            cellCtx.fillStyle = "#000000";
            cellCtx.textBaseline = "alphabetic";
            const baselineY = Math.floor(pt * 1.3);
            const mTarget = cellCtx.measureText(ch);
            cellCtx.fillText(ch, 10, baselineY);
            const targetData = cellCtx.getImageData(0, 0, cellDim, cellDim).data;

            // 2. Render in fallback sans-serif
            cellCtx.clearRect(0, 0, cellDim, cellDim);
            cellCtx.font = sansFontSpec;
            const mSans = cellCtx.measureText(ch);
            cellCtx.fillText(ch, 10, baselineY);
            const sansData = cellCtx.getImageData(0, 0, cellDim, cellDim).data;

            // 3. Render in fallback monospace
            cellCtx.clearRect(0, 0, cellDim, cellDim);
            cellCtx.font = monoFontSpec;
            const mMono = cellCtx.measureText(ch);
            cellCtx.fillText(ch, 10, baselineY);
            const monoData = cellCtx.getImageData(0, 0, cellDim, cellDim).data;

            let diffSans = 0, diffMono = 0;
            let minX = cellDim, maxX = -1, minY = cellDim, maxY = -1;

            for (let py = 0; py < cellDim; py++) {
                for (let px = 0; px < cellDim; px++) {
                    const idx = (py * cellDim + px) * 4 + 3;
                    const alpha = targetData[idx];
                    if (alpha > 10) {
                        if (px < minX) minX = px;
                        if (px > maxX) maxX = px;
                        if (py < minY) minY = py;
                        if (py > maxY) maxY = py;
                    }
                    if (Math.abs(alpha - sansData[idx]) > 15) diffSans++;
                    if (Math.abs(alpha - monoData[idx]) > 15) diffMono++;
                }
            }

            // Fallback / tofu discrimination
            if (cp !== 32) {
                const isSansFallback = (Math.abs(mTarget.width - mSans.width) < 0.01 && diffSans < 5);
                const isMonoFallback = (Math.abs(mTarget.width - mMono.width) < 0.01 && diffMono < 5);
                if (isSansFallback || isMonoFallback) {
                    rejectedCodePoints.push(cp);
                    continue;
                }
            }

            if (maxX < minX || maxY < minY) {
                if (cp === 32) {
                    const sw = Math.max(1, Math.ceil(mTarget.width));
                    provenGlyphs.push({
                        code_point: 32,
                        glyph_w: sw,
                        glyph_h: Math.max(1, pt),
                        is_space: true,
                    });
                    provenCodePoints.push(32);
                } else {
                    rejectedCodePoints.push(cp);
                }
                continue;
            }

            const glyphW = maxX - minX + 1;
            const glyphH = maxY - minY + 1;

            provenGlyphs.push({
                code_point: cp,
                glyph_w: glyphW,
                glyph_h: glyphH,
                src_min_x: minX,
                src_min_y: minY,
                is_space: false,
            });
            provenCodePoints.push(cp);
        }

        if (provenGlyphs.length === 0) {
            return { error: "NO_PROVEN_TARGET_GLYPHS" };
        }

        // 3. Multi-page pagination
        const PAGE_W = 2048;
        const PAGE_H = 2048;
        const pages = [];

        let currentCanvas = document.createElement("canvas");
        currentCanvas.width = PAGE_W;
        currentCanvas.height = PAGE_H;
        let currentCtx = currentCanvas.getContext("2d", { willReadFrequently: true });
        let currentPageGlyphs = [];
        let curX = 5, curY = 5, currentRowMaxH = 0;

        for (let i = 0; i < provenGlyphs.length; i++) {
            const pg = provenGlyphs[i];
            if (curX + pg.glyph_w + 5 > PAGE_W) {
                curX = 5;
                curY += currentRowMaxH + 5;
                currentRowMaxH = 0;
            }

            if (curY + pg.glyph_h + 5 > PAGE_H) {
                const pIdx = pages.length + 1;
                pages.push({
                    page_index: pIdx,
                    dataUrl: currentCanvas.toDataURL("image/png"),
                    glyphs: currentPageGlyphs,
                    final: false,
                    next_cursor: String(pIdx + 1),
                });
                currentCanvas = document.createElement("canvas");
                currentCanvas.width = PAGE_W;
                currentCanvas.height = PAGE_H;
                currentCtx = currentCanvas.getContext("2d", { willReadFrequently: true });
                currentPageGlyphs = [];
                curX = 5;
                curY = 5;
                currentRowMaxH = 0;
            }

            if (!pg.is_space) {
                cellCtx.clearRect(0, 0, cellDim, cellDim);
                cellCtx.font = exactFontSpec;
                cellCtx.fillStyle = "#000000";
                cellCtx.textBaseline = "alphabetic";
                cellCtx.fillText(String.fromCodePoint(pg.code_point), 10, Math.floor(pt * 1.3));

                currentCtx.drawImage(
                    cellCanvas,
                    pg.src_min_x, pg.src_min_y, pg.glyph_w, pg.glyph_h,
                    curX, curY, pg.glyph_w, pg.glyph_h
                );
            }

            currentPageGlyphs.push({
                code_point: pg.code_point,
                glyph_index: currentPageGlyphs.length + 1,
                sprite_box: {
                    x: Math.floor(curX),
                    y: Math.floor(curY),
                    width: Math.floor(pg.glyph_w),
                    height: Math.floor(pg.glyph_h),
                }
            });

            curX += pg.glyph_w + 5;
            currentRowMaxH = Math.max(currentRowMaxH, pg.glyph_h);
        }

        if (currentPageGlyphs.length > 0) {
            const pIdx = pages.length + 1;
            pages.push({
                page_index: pIdx,
                dataUrl: currentCanvas.toDataURL("image/png"),
                glyphs: currentPageGlyphs,
                final: true,
                next_cursor: "",
            });
        }

        // 4. Measure kerning pairs
        const pairs = [];
        const testPairs = ["AV", "AW", "VA", "To", "Ta", "Te", "Tu", "WA", "We", "Wo", "YA", "Yo"];
        cellCtx.font = exactFontSpec;
        for (const pair of testPairs) {
            const m1 = cellCtx.measureText(pair[0]).width;
            const m2 = cellCtx.measureText(pair[1]).width;
            const mPair = cellCtx.measureText(pair).width;
            const delta = mPair - (m1 + m2);
            if (Math.abs(delta) > 0.05) {
                pairs.push({
                    left_char: pair[0],
                    right_char: pair[1],
                    pair_text: pair,
                    kern_px: delta,
                    provenance: "playwright:canvas_text_metrics"
                });
            }
        }

        // 5. Measure OpenType features using causal DOM elements attached to document.body
        const features = [];
        try {
            const container = document.createElement("div");
            container.style.position = "absolute";
            container.style.left = "-9999px";
            container.style.top = "-9999px";
            container.style.visibility = "hidden";
            document.body.appendChild(container);

            // Test liga (fi)
            const spanLigaOn = document.createElement("span");
            spanLigaOn.style.font = exactFontSpec;
            spanLigaOn.style.fontFeatureSettings = '"liga" 1';
            spanLigaOn.textContent = "fi";
            container.appendChild(spanLigaOn);

            const spanLigaOff = document.createElement("span");
            spanLigaOff.style.font = exactFontSpec;
            spanLigaOff.style.fontFeatureSettings = '"liga" 0';
            spanLigaOff.textContent = "fi";
            container.appendChild(spanLigaOff);

            const rLigaOn = spanLigaOn.getBoundingClientRect();
            const rLigaOff = spanLigaOff.getBoundingClientRect();
            const ligaDelta = rLigaOn.width - rLigaOff.width;
            if (Math.abs(ligaDelta) > 0.05) {
                features.push({
                    feature_tag: "liga",
                    sample_text: "fi",
                    delta_px: ligaDelta,
                    measured: true,
                    provenance: "playwright:dom_feature_probe",
                });
            }

            // Test smcp (Standard)
            const spanSmcpOn = document.createElement("span");
            spanSmcpOn.style.font = exactFontSpec;
            spanSmcpOn.style.fontFeatureSettings = '"smcp" 1';
            spanSmcpOn.textContent = "Standard";
            container.appendChild(spanSmcpOn);

            const spanSmcpOff = document.createElement("span");
            spanSmcpOff.style.font = exactFontSpec;
            spanSmcpOff.style.fontFeatureSettings = '"smcp" 0';
            spanSmcpOff.textContent = "Standard";
            container.appendChild(spanSmcpOff);

            const rSmcpOn = spanSmcpOn.getBoundingClientRect();
            const rSmcpOff = spanSmcpOff.getBoundingClientRect();
            const smcpDelta = rSmcpOn.width - rSmcpOff.width;
            if (Math.abs(smcpDelta) > 0.05) {
                features.push({
                    feature_tag: "smcp",
                    sample_text: "Standard",
                    delta_px: smcpDelta,
                    measured: true,
                    provenance: "playwright:dom_feature_probe",
                });
            }

            document.body.removeChild(container);
        } catch (e) {}

        results.push({
            pt,
            resolved_face: resolvedFace,
            required_source_cps: requiredSourceCps,
            candidate_cps: candidateCodePoints,
            proven_cps: provenCodePoints,
            rejected_cps: rejectedCodePoints,
            pages,
            pairs,
            features,
        });
    }

    return { results };
}
"""


class PlaywrightStealthPersistentSession:
    """Production Playwright Stealth real-Chrome persistent context fallback (Method 2).

    Retains cf_clearance cookies in configured user_data_dir profile.
    Uses persistent context with exact launch args, ignored default args,
    and webdriver/chrome init scripts from canonical specification.
    Recovers FamilyDiscoveryEnvelope, and captures complete raster glyphs across
    all requested acs_pt sizes via persistent Chrome session / offscreen canvas.
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
        user_data_dir: Path | str | None = None,
        timeout_seconds: float = 45.0,
        transport_override: Callable[..., Awaitable[tuple[SpriteRasterPage, ...] | None]] | None = None,
        discovery_override: Callable[[str], Awaitable[Any | None]] | None = None,
        playwright_launcher: Callable[..., Any] | None = None,
    ) -> None:
        self.user_data_dir = Path(user_data_dir).resolve() if user_data_dir else None
        self.timeout_seconds = timeout_seconds
        self._transport_override = transport_override
        self._discovery_override = discovery_override
        self._playwright_launcher = playwright_launcher

    def available(self) -> bool:
        if self._transport_override is not None or self._discovery_override is not None or self._playwright_launcher is not None:
            return True
        return bool(self.user_data_dir and self.user_data_dir.is_dir())

    async def discover_family(self, source_url: str) -> FamilyDiscoveryEnvelope | None:
        """Run stealth persistent session and extract FamilyDiscoveryEnvelope."""
        if self._discovery_override is not None:
            return await self._discovery_override(source_url)
        if self._transport_override is not None:
            return await self._transport_override(source_url)

        if not self.available():
            return None

        from acquisition.providers import parse_family_discovery_from_dump
        from acquisition.models import STAGE_PLAYWRIGHT_STEALTH

        try:
            if self._playwright_launcher is not None:
                context = await self._playwright_launcher(
                    user_data_dir=str(self.user_data_dir or ""),
                    channel="chrome",
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
            else:
                from playwright.async_api import async_playwright
                async with async_playwright() as p:
                    context = await p.chromium.launch_persistent_context(
                        user_data_dir=str(self.user_data_dir),
                        channel="chrome",
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
        except Exception as exc:
            logger.debug("Playwright persistent discovery exception: %s", exc)
            return None

    async def capture_raster_pages(
        self,
        source_url: str,
        style_rec: Any,
        requested_sizes: list[int],
    ) -> tuple[SpriteRasterPage, ...] | None:
        """Capture complete raster glyphs, pairs, and features with font-proof and fallback discrimination."""
        if self._transport_override is not None:
            return await self._transport_override(source_url, style_rec, requested_sizes)

        if not self.available():
            return None

        from acquisition.models import STAGE_PLAYWRIGHT_STEALTH, is_complete_raster_pages

        style_name = getattr(style_rec, "style_name", "") or getattr(style_rec, "style_id", "Regular")
        style_id = getattr(style_rec, "style_id", "")
        expected_md5 = getattr(style_rec, "md5", "")

        observed_font_responses: list[dict[str, Any]] = []

        def on_response(resp: Any) -> None:
            try:
                url = str(getattr(resp, "url", ""))
                status = int(getattr(resp, "status", 200))
                if 200 <= status < 400 and url:
                    observed_font_responses.append({"url": url, "status": status})
            except Exception:
                pass

        try:
            if self._playwright_launcher is not None:
                context = await self._playwright_launcher(
                    user_data_dir=str(self.user_data_dir or ""),
                    channel="chrome",
                    headless=True,
                    args=self.LAUNCH_ARGS,
                    ignore_default_args=self.IGNORED_DEFAULT_ARGS,
                    user_agent=self.DESKTOP_UA,
                    timeout=self.timeout_seconds * 1000,
                )
                try:
                    await context.add_init_script(self.STEALTH_INIT_SCRIPT)
                    page = await context.new_page()
                    if hasattr(page, "on") and callable(page.on):
                        res_on = page.on("response", on_response)
                        if asyncio.iscoroutine(res_on):
                            await res_on
                    await page.goto(source_url, timeout=self.timeout_seconds * 1000, wait_until="domcontentloaded")
                    eval_out = await page.evaluate(
                        CANVAS_EVALUATOR_SCRIPT,
                        {
                            "requested_sizes": requested_sizes,
                            "style_name": style_name,
                            "style_id": style_id,
                            "expected_md5": expected_md5,
                            "observed_font_responses": observed_font_responses,
                        },
                    )
                finally:
                    await context.close()
            else:
                from playwright.async_api import async_playwright
                async with async_playwright() as p:
                    context = await p.chromium.launch_persistent_context(
                        user_data_dir=str(self.user_data_dir),
                        channel="chrome",
                        headless=True,
                        args=self.LAUNCH_ARGS,
                        ignore_default_args=self.IGNORED_DEFAULT_ARGS,
                        user_agent=self.DESKTOP_UA,
                        timeout=self.timeout_seconds * 1000,
                    )
                    try:
                        await context.add_init_script(self.STEALTH_INIT_SCRIPT)
                        page = await context.new_page()
                        if hasattr(page, "on") and callable(page.on):
                            res_on = page.on("response", on_response)
                            if asyncio.iscoroutine(res_on):
                                await res_on
                        await page.goto(source_url, timeout=self.timeout_seconds * 1000, wait_until="domcontentloaded")
                        eval_out = await page.evaluate(
                            CANVAS_EVALUATOR_SCRIPT,
                            {
                                "requested_sizes": requested_sizes,
                                "style_name": style_name,
                                "style_id": style_id,
                                "expected_md5": expected_md5,
                                "observed_font_responses": observed_font_responses,
                            },
                        )
                    finally:
                        await context.close()
            if not eval_out or not isinstance(eval_out, dict) or eval_out.get("error") or not eval_out.get("results"):
                return None

            target_desc = extract_font_descriptors(style_name, style_id)

            out_pages: list[SpriteRasterPage] = []
            for res in eval_out["results"]:
                pt = res.get("pt")
                if pt is None or int(pt) not in requested_sizes:
                    return None

                resolved_face = res.get("resolved_face")
                if not resolved_face or not isinstance(resolved_face, dict):
                    return None
                rf_family = str(resolved_face.get("family", "")).strip().lower().replace("'", "").replace('"', "")
                if not rf_family:
                    return None
                norm_style = style_name.lower().replace("-", " ").replace("_", " ")
                norm_rf = rf_family.replace("-", " ").replace("_", " ")
                if not (norm_rf in norm_style or norm_style in norm_rf or any(p in norm_style for p in norm_rf.split())):
                    return None

                # Exact style, weight, stretch validation in Python
                raw_style = str(resolved_face.get("style", "normal")).lower()
                rf_style = "italic" if ("italic" in raw_style or "oblique" in raw_style) else "normal"

                raw_weight = str(resolved_face.get("weight", "400")).lower().strip()
                if raw_weight in ("normal", "regular", "roman", "plain", "400"):
                    rf_weight = "400"
                elif raw_weight in ("bold", "700"):
                    rf_weight = "700"
                elif raw_weight in ("bolder", "extra bold", "extrabold", "ultra bold", "ultrabold", "800"):
                    rf_weight = "800"
                elif raw_weight in ("semi bold", "semibold", "demi bold", "demibold", "600"):
                    rf_weight = "600"
                elif raw_weight in ("lighter", "light", "book", "300"):
                    rf_weight = "300"
                elif raw_weight in ("extra light", "extralight", "ultra light", "ultralight", "200"):
                    rf_weight = "200"
                elif raw_weight in ("thin", "hairline", "100"):
                    rf_weight = "100"
                elif raw_weight in ("black", "heavy", "900"):
                    rf_weight = "900"
                elif raw_weight in ("medium", "500"):
                    rf_weight = "500"
                elif raw_weight.isdigit():
                    rf_weight = raw_weight
                else:
                    rf_weight = "400"

                raw_stretch = str(resolved_face.get("stretch", "normal")).lower()
                if "condensed" in raw_stretch or "narrow" in raw_stretch:
                    rf_stretch = "condensed"
                elif "expanded" in raw_stretch or "wide" in raw_stretch:
                    rf_stretch = "expanded"
                else:
                    rf_stretch = "normal"

                if rf_style != target_desc["style"]:
                    return None
                if rf_weight != target_desc["weight"]:
                    return None
                if rf_stretch != target_desc["stretch"]:
                    return None

                # MD5 / Resource binding verification
                if expected_md5:
                    rf_md5 = str(resolved_face.get("resource_md5", "")).strip().lower()
                    rf_url = str(resolved_face.get("resource_url", "")).strip().lower()
                    exp_lower = expected_md5.strip().lower()

                    if not rf_md5 or rf_md5 != exp_lower:
                        return None

                    # If observed_font_responses is non-empty, ensure at least one 2xx response matched exp_lower or rf_url
                    if observed_font_responses:
                        matched_observed = any(
                            exp_lower in r.get("url", "").lower() and 200 <= r.get("status", 0) < 400
                            for r in observed_font_responses
                        )
                        if not matched_observed and rf_url and exp_lower not in rf_url:
                            return None

                required_source_cps = res.get("required_source_cps", [])
                candidate_cps = res.get("candidate_cps", [])
                proven_cps = res.get("proven_cps", [])
                rejected_cps = res.get("rejected_cps", [])
                raw_pages = res.get("pages", [])
                pairs = res.get("pairs", [])
                features = res.get("features", [])

                if not required_source_cps or not candidate_cps or not proven_cps or not raw_pages:
                    return None

                set_req = set(int(c) for c in required_source_cps)
                set_cand = set(int(c) for c in candidate_cps)
                set_prov = set(int(c) for c in proven_cps)
                set_rej = set(int(c) for c in rejected_cps)

                # Required source coverage MUST be fully proven
                if not set_req.issubset(set_prov):
                    return None

                # Exact set equality & disjointness checks
                if set_cand != (set_prov | set_rej):
                    return None
                if set_prov & set_rej:
                    return None

                # Validate page sequences and glyph containment
                expected_indices = list(range(1, len(raw_pages) + 1))
                if [p.get("page_index") for p in raw_pages] != expected_indices:
                    return None

                size_collected_cps: list[int] = []
                for p_idx, page_dict in enumerate(raw_pages, start=1):
                    is_last = (p_idx == len(raw_pages))
                    if is_last:
                        if not page_dict.get("final") or page_dict.get("next_cursor"):
                            return None
                    else:
                        if page_dict.get("final") or not page_dict.get("next_cursor"):
                            return None

                    data_url = page_dict.get("dataUrl", "")
                    glyphs = page_dict.get("glyphs", [])
                    if not data_url or not isinstance(glyphs, list):
                        return None
                    if is_last and len(glyphs) == 0 and len(raw_pages) == 1:
                        return None

                    b64 = data_url.split(",", 1)[-1]
                    try:
                        raw_png = base64.b64decode(b64)
                    except Exception:
                        return None
                    if not raw_png.startswith(b"\x89PNG\r\n\x1a\n") or len(raw_png) < 24:
                        return None
                    png_w = int.from_bytes(raw_png[16:20], "big")
                    png_h = int.from_bytes(raw_png[20:24], "big")
                    if png_w <= 0 or png_h <= 0:
                        return None

                    for g in glyphs:
                        cp = g.get("code_point")
                        if cp is None or int(cp) not in set_prov:
                            return None
                        size_collected_cps.append(int(cp))
                        box = g.get("sprite_box", {})
                        x = box.get("x", 0)
                        y = box.get("y", 0)
                        w = box.get("width", 0)
                        h = box.get("height", 0)
                        if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > png_w or y + h > png_h:
                            return None

                    sha = hashlib.sha256(raw_png).hexdigest()
                    out_pages.append(
                        SpriteRasterPage(
                            page_index=p_idx,
                            glyph_count=len(glyphs),
                            raster_bytes=raw_png,
                            next_cursor=page_dict.get("next_cursor", ""),
                            final=bool(page_dict.get("final")),
                            payload={
                                "browser_version": "playwright_stealth_v1",
                                "glyphs": glyphs,
                                "pairs": pairs,
                                "features": [f for f in features if f.get("measured") is True and abs(f.get("delta_px", 0)) > 0.01],
                                "sprite_sha256": sha,
                                "md5": expected_md5,
                                "acs_pt": pt,
                                "provenance": STAGE_PLAYWRIGHT_STEALTH,
                                "resolved_face": resolved_face,
                            },
                        )
                    )

                # Verify all declared proven code points appear exactly once across all pages
                if sorted(size_collected_cps) != sorted(list(set_prov)):
                    return None

            if is_complete_raster_pages(out_pages, requested_sizes, expected_md5=expected_md5):
                return tuple(out_pages)
        except Exception as exc:
            logger.debug("Playwright persistent raster capture exception: %s", exc)
            return None
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
        return bool(self.app_id and self._api_key)

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

    # Legacy generic HTTP-session provider is removed from production composition.
    session_provider = None

    # Method 2: Playwright Stealth persistent context
    playwright_provider = None
    if getattr(settings, "PLAYWRIGHT_STEALTH_ENABLED", True):
        playwright_provider = PlaywrightStealthPersistentSession(
            user_data_dir=getattr(settings, "PLAYWRIGHT_USER_DATA_DIR", None),
        )

    # Method 3: Direct Monotype CDN raster client
    raster_provider = None
    raster_url = str(getattr(settings, "MONOTYPE_RASTER_ENDPOINT_URL", "") or "https://sig.monotype.com")
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
    if algolia_app_id and algolia_key:
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
