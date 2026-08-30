"""Production transport chain for the FAST_ATLAS_ULTRA_V1 atlas pipeline (R1).

Implements the REAL providers of the approved acquisition chain and the
default production atlas pipeline wrapper:

  1. exact cache / authorized binary wins immediately (atlas/cache.py
     exact-identity semantics; AuthorizedBinaryCache probe, ORIGINAL only);
  2. Chrome native ``--headless=new --dump-dom`` metadata provider:
     family/style/weight/identifiers/MD5 via bounded subprocess;
  3. ONE persistent Playwright/Chromium session, kept for the whole run,
     started LAZILY only when metadata/measureText/browser-atlas is actually
     needed: batched measureText metrics + canvas atlas pages ONLY for
     missing/unattestable observations (atlas/metrics.py batch protocol +
     atlas/paging cell mapping);
  4. Monotype CDN provider: direct HTTP raster source with exact-MD5
     verification; the PRIMARY raster path;
  5. Algolia provider: resolves the style MD5 when missing, then returns to
     the CDN.

Order of operations: 1 -> 2 -> 3 (lazy) -> 4 -> 5 -> 4. NO screenshot, NO
per-glyph navigation, NO per-glyph CDP rendering, NO fake data, NO
unobserved CDN phases: every served byte is accounted by an actually
observed response, and the http_requests / cdp_calls / browser_readbacks
counters record observed transport activity only (never pipeline-internal
call proxies).
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from PIL import Image

from acquisition.adapters import (
    APPROVED_DESKTOP_UA,
    AlgoliaMetadataClient,
    HeadlessDumpDomTransport,
    HttpBinaryFetcher,
    MonotypeRenderClient,
)
from acquisition.models import (
    AcquiredBinary,
    BinaryAcquisitionPolicy,
    DiscoveryEnvelope,
    FamilyDiscoveryEnvelope,
    STAGE_DUMP_DOM_NATIVE,
    StyleDiscoveryRecord,
    is_complete_raster_pages,
)
from acquisition.providers import extract_binary_from_dump_dom, parse_family_discovery_from_dump
from acquisition.verifier import verify_acquired_binary
from compute.binary_cache import AuthorizedBinaryCache, BinaryCacheIdentity
from compute.binary_gate import prepare_binary_artifact
from atlas.cache import (
    AtlasCacheStore,
    AtlasCheckpointStore,
    identity_hash,
    NAMESPACE_FONTS,
    NAMESPACE_REPORTS,
)
from atlas.metrics import build_measure_text_js
from atlas.models import AtlasRunEvidence
from atlas.paging import CELL_PAD_X_PX, CELL_PAD_Y_PX, cell_dimensions, pen_left_px
from atlas.pipeline import AtlasRunResult, AtlasStyleSpec, AtlasUltraPipeline
from atlas.policy import (
    FAST_ATLAS_ULTRA_V1,
    FAST_RASTER_PHASE,
    FAST_RASTER_SIZE_PX,
    AtlasRuntimeDefaults,
    policy_identity_hash,
)
from reconstruction.font_model import CanonicalFontModel

logger = logging.getLogger("telegramfonts.agent.atlas.transport")

# The Monotype render protocol addresses glyph size in points; at 96 dpi one
# pixel is exactly 0.75 pt, so acs_pt = px * 3/4 renders the requested pixel
# em (1024 px -> 768 pt; 2048 px -> 1536 pt).
def cdn_acs_pt_for(size_px: int) -> int:
    return max(1, int(round(int(size_px) * 3 / 4)))


from atlas.marks import is_combining_mark  # noqa: E402 (single source of truth)


@dataclass
class AtlasTransportCounters:
    """OBSERVED transport activity only (honest counters, R1).

    http_requests: actual observed HTTP responses (CDN/Algolia/binary fetch).
    cdp_calls: actual browser evaluate calls on the persistent session.
    browser_readbacks: actual canvas readbacks observed.
    dump_dom_calls: native chrome --dump-dom subprocess runs.
    """

    http_requests: int = 0
    cdp_calls: int = 0
    browser_readbacks: int = 0
    dump_dom_calls: int = 0
    cache_hits: int = 0

    def to_dict(self) -> dict:
        return {
            "http_requests": self.http_requests,
            "cdp_calls": self.cdp_calls,
            "browser_readbacks": self.browser_readbacks,
            "dump_dom_calls": self.dump_dom_calls,
            "cache_hits": self.cache_hits,
        }


# ----------------------------------------------------------------------
# Stage 2: Chrome native --dump-dom metadata provider
# ----------------------------------------------------------------------

class ChromeDumpDomMetadataProvider:
    """Family/style/weight/identifiers/MD5 via native chrome --dump-dom.

    One bounded subprocess per call (HeadlessDumpDomTransport enforces the
    timeout and kills the child). Never fabricates: an absent envelope or
    absent style record fails closed to the next transport stage.
    """

    def __init__(
        self,
        transport: HeadlessDumpDomTransport | None = None,
        binary_fetcher: HttpBinaryFetcher | None = None,
        policy: BinaryAcquisitionPolicy | None = None,
        counters: AtlasTransportCounters | None = None,
    ) -> None:
        self.transport = transport or HeadlessDumpDomTransport()
        self.binary_fetcher = binary_fetcher or HttpBinaryFetcher()
        self.policy = policy or BinaryAcquisitionPolicy()
        self.counters = counters or AtlasTransportCounters()
        self._cached_dumps: dict[str, str] = {}

    async def family_envelope(
        self, source_url: str, expected_family: str = ""
    ) -> FamilyDiscoveryEnvelope | None:
        cached = self._cached_dumps.get(source_url)
        if cached is None:
            try:
                dump = await self.transport.dump_dom(source_url)
            except Exception as exc:
                logger.warning("dump-dom metadata failed: %s", type(exc).__name__)
                self.counters.dump_dom_calls += 1
                return None
            self.counters.dump_dom_calls += 1
            if not dump:
                return None
            self._cached_dumps[source_url] = dump
            cached = dump
        try:
            return parse_family_discovery_from_dump(cached, source_url, STAGE_DUMP_DOM_NATIVE)
        except Exception:
            return None

    async def authorized_binary(
        self,
        source_url: str,
        envelope: FamilyDiscoveryEnvelope,
        expected_family: str,
        expected_style: str,
    ) -> AcquiredBinary | None:
        """Fetch + verify a dump-dom binary candidate (binary wins immediately)."""
        style_rec = envelope.get_style_record(expected_style, expected_style)
        if style_rec is None or not style_rec.binary_candidates:
            return None
        dump = self._cached_dumps.get(source_url, "")
        legacy = DiscoveryEnvelope(
            family_name=envelope.family_name,
            style_name=style_rec.style_name or expected_style,
            binary_candidates=style_rec.binary_candidates,
            provenance=style_rec.provenance,
        )
        try:
            raw = await extract_binary_from_dump_dom(
                dump, self.binary_fetcher.fetch, self.policy, legacy
            )
        except Exception:
            return None
        self.counters.http_requests += 1 if raw is not None else 0
        if raw is None:
            return None
        verification = verify_acquired_binary(
            raw, expected_family, expected_style, self.policy.max_binary_bytes
        )
        if verification.status != "VALID":
            logger.warning(
                "dump-dom binary rejected: %s", verification.reason_code
            )
            return None
        return AcquiredBinary(
            raw_bytes=raw,
            format=verification.format,
            family_name=verification.family_name,
            style_name=verification.style_name,
            provenance=STAGE_DUMP_DOM_NATIVE,
        )


# ----------------------------------------------------------------------
# Stage 4: Monotype CDN primary raster source (exact-MD5 verified)
# ----------------------------------------------------------------------

class MonotypeCdnRasterSource:
    """Direct HTTP raster source with exact-MD5 verification (PRIMARY raster).

    Fetches bounded sprite pages from the authorized Monotype render endpoint
    and crops per-glyph ink observations. Every page binds the exact MD5 in
    its request params; a page whose bound MD5 differs from the expected
    style MD5 is rejected (fail closed -> browser fallback), never relabeled.
    """

    def __init__(
        self,
        client: MonotypeRenderClient | None = None,
        counters: AtlasTransportCounters | None = None,
        policy: BinaryAcquisitionPolicy | None = None,
    ) -> None:
        self.client = client
        self.counters = counters or AtlasTransportCounters()
        self.policy = policy or BinaryAcquisitionPolicy()

    def available(self) -> bool:
        return self.client is not None

    async def fetch_glyph_observations(
        self,
        md5: str,
        family_name: str,
        style_name: str,
        size_px: int = FAST_RASTER_SIZE_PX,
    ) -> dict[int, dict] | None:
        """All bounded CDN pages for one size; returns per-cp ink boxes +
        decoded sprite images, or None when the CDN cannot fully attest.

        Every byte returned was observed in an actual 200 JSON response bound
        to the exact MD5 (no unobserved phases, no invented glyphs).
        """
        if self.client is None:
            return None
        md5_clean = str(md5 or "").strip().lower()
        if len(md5_clean) != 32:
            return None
        acs_pt = cdn_acs_pt_for(size_px)
        request = {
            "family": family_name,
            "style": style_name,
            "md5": md5_clean,
            "acs_pt": acs_pt,
        }
        try:
            pages = await self.client.fetch_all_sprite_pages(request, self.policy)
        except Exception as exc:
            logger.warning("CDN raster crawl failed: %s", type(exc).__name__)
            return None
        if pages:
            # Honest count: one observed HTTP response per consumed page.
            self.counters.http_requests += len(pages)
        if not pages:
            return None
        # Exact-MD5 verification of every observed page (fail closed).
        if not is_complete_raster_pages(pages, [acs_pt], expected_md5=md5_clean):
            return None
        obs: dict[int, dict] = {}
        for page in pages:
            payload = page.payload or {}
            if str(payload.get("md5", "")).strip().lower() != md5_clean:
                return None
            rp = payload.get("request_params") or {}
            if str(rp.get("md5", "")).strip().lower() != md5_clean:
                return None
            try:
                sprite = Image.open(io.BytesIO(page.raster_bytes)).convert("L")
            except Exception:
                return None
            for glyph in payload.get("glyphs", []):
                try:
                    cp = int(glyph["code_point"])
                    box = glyph["sprite_box"]
                    x, y = int(box["x"]), int(box["y"])
                    w, h = int(box["width"]), int(box["height"])
                except (KeyError, TypeError, ValueError):
                    return None
                if w < 1 or h < 1 or x < 0 or y < 0:
                    continue
                if x + w > sprite.width or y + h > sprite.height:
                    return None
                if glyph.get("is_space") is True:
                    obs[cp] = {"space": True}
                    continue
                obs[cp] = {
                    "space": False,
                    "sprite": sprite,
                    "box": (x, y, w, h),
                    "acs_pt": acs_pt,
                    "size_px": int(size_px),
                    "md5": md5_clean,
                }
        return obs or None

    async def fetch_single_cell(
        self,
        md5: str,
        family_name: str,
        style_name: str,
        code_point: int,
        size_px: int,
    ) -> dict | None:
        """Bounded on-demand fetch of ONE glyph's ink box at one size.

        Used by the single-refinement stage (2048 double observation). Pages
        are walked until the code point is observed or bounded completion;
        every consumed byte is an observed CDN response bound to the MD5.
        """
        if self.client is None:
            return None
        md5_clean = str(md5 or "").strip().lower()
        if len(md5_clean) != 32:
            return None
        acs_pt = cdn_acs_pt_for(size_px)
        request = {
            "family": family_name,
            "style": style_name,
            "md5": md5_clean,
            "acs_pt": acs_pt,
        }
        cursor = ""
        for _ in range(self.policy.max_sprite_pages):
            try:
                page = await self.client.fetch_sprite_page(dict(request), cursor)
            except Exception:
                return None
            self.counters.http_requests += 1 if page is not None else 0
            if page is None:
                return None
            payload = page.payload or {}
            if str(payload.get("md5", "")).strip().lower() != md5_clean:
                return None
            for glyph in payload.get("glyphs", []):
                try:
                    if int(glyph["code_point"]) != int(code_point):
                        continue
                    box = glyph["sprite_box"]
                    x, y = int(box["x"]), int(box["y"])
                    w, h = int(box["width"]), int(box["height"])
                except (KeyError, TypeError, ValueError):
                    continue
                if glyph.get("is_space") is True:
                    return {"space": True}
                if w < 1 or h < 1 or x < 0 or y < 0:
                    return None
                try:
                    sprite = Image.open(io.BytesIO(page.raster_bytes)).convert("L")
                except Exception:
                    return None
                if x + w > sprite.width or y + h > sprite.height:
                    return None
                return {
                    "space": False,
                    "sprite": sprite,
                    "box": (x, y, w, h),
                    "acs_pt": acs_pt,
                    "size_px": int(size_px),
                    "md5": md5_clean,
                }
            if page.final or not page.next_cursor or page.glyph_count == 0:
                return None
            cursor = page.next_cursor
        return None


# ----------------------------------------------------------------------
# Stage 5: Algolia MD5 resolution (metadata only, then back to the CDN)
# ----------------------------------------------------------------------

class AlgoliaMd5Resolver:
    """Resolve the exact style MD5 when dump-dom could not attest it."""

    def __init__(
        self,
        client: AlgoliaMetadataClient | None = None,
        counters: AtlasTransportCounters | None = None,
    ) -> None:
        self.client = client
        self.counters = counters or AtlasTransportCounters()

    def available(self) -> bool:
        return self.client is not None and self.client.available()

    async def resolve_md5(
        self, family_name: str, style_name: str, style_id: str, source_url: str
    ) -> str:
        if not self.available():
            return ""
        query = family_name.strip()
        if not query:
            slug = source_url.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
            query = slug.replace("-", " ").replace("_", " ")
        try:
            envelope = await self.client.discover_family(query, source_url)
        except Exception:
            return ""
        self.counters.http_requests += 1 if envelope is not None else 0
        if envelope is None:
            return ""
        rec = envelope.get_style_record(style_id, style_name)
        if rec is None or len(rec.md5) != 32:
            return ""
        return rec.md5.strip().lower()


# ----------------------------------------------------------------------
# Stage 3: ONE persistent Playwright/Chromium session (lazy start, kept)
# ----------------------------------------------------------------------

FACE_RESOLVE_JS = """
async ({ style, weight }) => {
    try { await document.fonts.ready; } catch (e) {}
    const out = [];
    for (const f of document.fonts) {
        out.push({
            family: String(f.family || ""),
            style: String(f.style || "normal"),
            weight: String(f.weight || "400"),
            status: String(f.status || ""),
        });
    }
    return out;
}
"""

COVERAGE_SCAN_JS = """
async () => {
    const ranges = [];
    try {
        for (const sheet of document.styleSheets) {
            try {
                const rules = sheet.cssRules || sheet.rules;
                if (!rules) continue;
                for (const r of rules) {
                    if (r.cssText && r.cssText.indexOf("@font-face") === 0) {
                        const fStyle = r.style || {};
                        ranges.push(String(fStyle.unicodeRange || ""));
                    }
                }
            } catch (e) {}
        }
    } catch (e) {}
    return ranges;
}
"""

PAIR_ADVANCES_JS = """((texts, size, family) => {
  const c = globalThis.__atlasCanvas || (globalThis.__atlasCanvas = document.createElement('canvas'));
  const ctx = c.getContext('2d');
  ctx.font = size + 'px ' + family;
  ctx.textBaseline = 'alphabetic';
  const out = [];
  for (let i = 0; i < texts.length; i++) out.push(ctx.measureText(texts[i]).width);
  return out;
})({texts_json}, {size}, {family_json})"""

CELL_PAGE_JS = """
async ({ cells, page_w, page_h }) => {
    const canvas = document.createElement('canvas');
    canvas.width = page_w;
    canvas.height = page_h;
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    ctx.fillStyle = '#000000';
    ctx.textBaseline = 'alphabetic';
    for (const cell of cells) {
        ctx.font = cell.font_spec;
        ctx.fillText(cell.ch, cell.pen_left + cell.phase_x, cell.baseline_y + cell.phase_y);
    }
    return canvas.toDataURL('image/png');
}
"""


def parse_unicode_ranges_to_codepoints(ranges: list[str], cap: int = 4096) -> list[int]:
    """Deterministic bounded expansion of @font-face unicode-range values."""
    declared: set[int] = set()
    total_span = 0
    for raw in ranges:
        if not raw:
            continue
        for part in str(raw).split(","):
            clean = part.strip().replace("U+", "").replace("u+", "")
            if not clean:
                continue
            start = end = -1
            if "-" in clean:
                a, b = clean.split("-", 1)
                try:
                    start, end = int(a, 16), int(b, 16)
                except ValueError:
                    continue
            elif "?" in clean:
                try:
                    start = int(clean.replace("?", "0"), 16)
                    end = int(clean.replace("?", "F"), 16)
                except ValueError:
                    continue
            else:
                try:
                    start = end = int(clean, 16)
                except ValueError:
                    continue
            if start < 0 or end < start:
                continue
            total_span += end - start + 1
            if total_span > cap:
                raise ValueError("ATLAS_COVERAGE_UNBOUNDED")
            for cp in range(start, end + 1):
                declared.add(cp)
    return sorted(cp for cp in declared if cp > 0x20)


def _chrome_channel_or_none() -> str | None:
    """Use the "chrome" channel only when a real Google Chrome exists.

    The A23 Debian chroot (and the Mini PC target) ships chromium, not
    Google Chrome; forcing channel="chrome" there makes every Playwright
    launch fail closed. Returning None lets Playwright use its managed
    (bundled) Chromium build instead.
    """
    for name in ("google-chrome", "google-chrome-stable"):
        if shutil.which(name):
            return "chrome"
    return None


class PersistentBrowserAtlasSession:
    """The single persistent Chromium session (ADR-0004 browser_sessions=1).

    Started lazily only when metadata/measureText/browser-atlas is actually
    needed; kept for the whole run. Batched measureText metrics reuse the
    atlas/metrics.py JS batch protocol; canvas atlas pages are assembled ONE
    page at a time with ONE readback per page (cells cropped in Python). No
    screenshot, no per-glyph navigation, no per-glyph CDP.
    """

    def __init__(
        self,
        source_url: str,
        family_name: str,
        style_name: str,
        style_id: str = "",
        expected_md5: str = "",
        user_data_dir: Path | str | None = None,
        timeout_seconds: float = 45.0,
        counters: AtlasTransportCounters | None = None,
        playwright_launcher: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        self.source_url = source_url
        self.family_name = family_name
        self.style_name = style_name
        self.style_id = style_id
        self.expected_md5 = str(expected_md5 or "").strip().lower()
        self.user_data_dir = Path(user_data_dir).resolve() if user_data_dir else None
        self.timeout_seconds = timeout_seconds
        self.counters = counters or AtlasTransportCounters()
        self._playwright_launcher = playwright_launcher
        self._pw = None
        self._cdp_browser = None
        self._context = None
        self._page = None
        self.started = False
        self.unusable = False
        self.resolved_family = ""
        self.resolved_style = "normal"
        self.resolved_weight = "400"
        self.attestation = ""
        self.observed_font_responses: list[dict] = []

    # -- lifecycle --------------------------------------------------------

    async def _on_response(self, resp: Any) -> None:
        try:
            url = str(getattr(resp, "url", "") or "")
            status = int(getattr(resp, "status", 0) or 0)
            if not url or not (200 <= status < 300):
                return
            req = getattr(resp, "request", None)
            resource_type = str(getattr(req, "resource_type", "") or "").lower()
            if resource_type != "font":
                return
            self.observed_font_responses.append({"url": url, "status": status})
        except Exception:
            pass

    async def ensure_started(self) -> bool:
        if self.started:
            return True
        if self.unusable:
            return False
        from acquisition.adapters import extract_font_descriptors

        descriptors = extract_font_descriptors(self.style_name, self.style_id)
        try:
            if self._playwright_launcher is not None:
                self._context = await self._playwright_launcher(
                    user_data_dir=str(self.user_data_dir or ""),
                    channel="chrome",
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                    ],
                    user_agent=APPROVED_DESKTOP_UA,
                    timeout=self.timeout_seconds * 1000,
                )
            elif os.environ.get("ATLAS_PLAYWRIGHT_CDP_URL", "").strip():
                # CDP bridge: attach to an ALREADY LAUNCHED Chromium endpoint
                # (e.g. the native /usr/bin/chromium of the A23 Debian chroot)
                # when a Playwright-managed browser is unavailable. The browser
                # process is owned by its launcher; close() terminates it via
                # CDP (Browser.close).
                from playwright.async_api import async_playwright

                self._pw = async_playwright()
                p = await self._pw.start()
                self._cdp_browser = await p.chromium.connect_over_cdp(
                    os.environ["ATLAS_PLAYWRIGHT_CDP_URL"].strip(),
                    timeout=self.timeout_seconds * 1000,
                )
                contexts = list(self._cdp_browser.contexts)
                self._context = (
                    contexts[0]
                    if contexts
                    else await self._cdp_browser.new_context(user_agent=APPROVED_DESKTOP_UA)
                )
            else:
                from playwright.async_api import async_playwright

                self._pw = async_playwright()
                p = await self._pw.start()
                channel = _chrome_channel_or_none()
                launcher = p.chromium.launch_persistent_context
                if self.user_data_dir is not None:
                    launch_kwargs: dict[str, Any] = {
                        "headless": True,
                        "args": [
                            "--disable-blink-features=AutomationControlled",
                            "--no-sandbox",
                            "--disable-dev-shm-usage",
                            "--disable-gpu",
                        ],
                        "user_agent": APPROVED_DESKTOP_UA,
                        "timeout": self.timeout_seconds * 1000,
                    }
                    if channel is not None:
                        launch_kwargs["channel"] = channel
                    self._context = await launcher(
                        user_data_dir=str(self.user_data_dir), **launch_kwargs
                    )
                else:
                    launch_args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
                    if channel is not None:
                        self._context = await p.chromium.launch(
                            channel=channel,
                            headless=True,
                            args=launch_args,
                            timeout=self.timeout_seconds * 1000,
                        )
                    else:
                        self._context = await p.chromium.launch(
                            headless=True,
                            args=launch_args,
                            timeout=self.timeout_seconds * 1000,
                        )
            self._page = await self._context.new_page()
            if hasattr(self._page, "on") and callable(self._page.on):
                sub = self._page.on("response", self._on_response)
                if asyncio.iscoroutine(sub):
                    await sub
            await self._page.goto(
                self.source_url,
                timeout=self.timeout_seconds * 1000,
                wait_until="domcontentloaded",
            )
            faces = await self._evaluate(
                FACE_RESOLVE_JS,
                {"style": descriptors["style"], "weight": descriptors["weight"]},
            )
        except Exception as exc:
            logger.warning("browser session start failed: %s", type(exc).__name__)
            await self.close()
            self.unusable = True
            return False

        # Deterministic face selection: exact style/weight match, loaded,
        # unique. MD5 attestation is enforced when an MD5 is expected.
        candidates = []
        for face in faces or []:
            if not isinstance(face, dict):
                continue
            fam = str(face.get("family", "")).strip()
            style = "italic" if "italic" in str(face.get("style", "")).lower() else "normal"
            raw_weight = str(face.get("weight", "400")).strip()
            weight = raw_weight if raw_weight.isdigit() else "400"
            if style != descriptors["style"]:
                continue
            if weight != descriptors["weight"]:
                continue
            if str(face.get("status", "")) != "loaded":
                continue
            if not fam:
                continue
            candidates.append({"family": fam, "style": style, "weight": weight})
        if len(candidates) != 1:
            logger.warning("browser face selection ambiguous/absent: %d", len(candidates))
            await self.close()
            self.unusable = True
            return False
        selected = candidates[0]
        if self.expected_md5:
            attested = any(
                self.expected_md5 in str(r.get("url", "")).lower()
                for r in self.observed_font_responses
            )
            if not attested:
                logger.warning("browser session MD5 attestation failed")
                await self.close()
                self.unusable = True
                return False
            self.attestation = "md5_attested"
        else:
            self.attestation = "descriptor_match"
        self.resolved_family = selected["family"]
        self.resolved_style = selected["style"]
        self.resolved_weight = selected["weight"]
        self.started = True
        return True

    async def close(self) -> None:
        try:
            if self._cdp_browser is not None:
                await self._cdp_browser.close()
        except Exception:
            pass
        self._cdp_browser = None
        try:
            if self._context is not None:
                await self._context.close()
        except Exception:
            pass
        self._context = None
        self._page = None
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None
        self.started = False

    async def _evaluate(self, expression: str, arg: Any = None) -> Any:
        if self._page is None:
            raise ValueError("ATLAS_BROWSER_SESSION_NOT_STARTED")
        self.counters.cdp_calls += 1
        if arg is None:
            return await self._page.evaluate(expression)
        return await self._page.evaluate(expression, arg)

    # -- batched metrics (atlas/metrics.py protocol) ------------------------

    def _family_css(self) -> str:
        fam = self.resolved_family.replace('"', "")
        return f'"{fam}"'

    def font_spec(self, size_px: int) -> str:
        fam = self.resolved_family.replace('"', "")
        return f"{self.resolved_style} {self.resolved_weight} {int(size_px)}px \"{fam}\""

    async def fetch_rows(self, size_px: int, code_points: list[int]) -> list[list[float]]:
        chars = [chr(cp) for cp in code_points]
        expression = build_measure_text_js(chars, int(size_px), self._family_css())
        rows = await self._evaluate(expression)
        if not isinstance(rows, list):
            raise ValueError("ATLAS_BROWSER_METRICS_INVALID")
        return rows

    async def fetch_pair_advances_px(self, size_px: int, pair_texts: list[str]) -> list[float]:
        import json as _json

        expression = (
            PAIR_ADVANCES_JS.replace(
                "{texts_json}", _json.dumps(pair_texts, ensure_ascii=False)
            )
            .replace("{size}", str(int(size_px)))
            .replace("{family_json}", _json.dumps(self._family_css()))
        )
        rows = await self._evaluate(expression)
        if not isinstance(rows, list) or len(rows) != len(pair_texts):
            raise ValueError("ATLAS_BROWSER_PAIR_ADVANCES_INVALID")
        return [float(v) for v in rows]

    # -- coverage discovery (browser-atlas fallback only) -------------------

    async def scan_coverage(self) -> list[int]:
        ranges = await self._evaluate(COVERAGE_SCAN_JS)
        if not isinstance(ranges, list):
            return []
        try:
            return parse_unicode_ranges_to_codepoints([str(r) for r in ranges])
        except ValueError:
            return []

    # -- canvas atlas pages for missing/unattestable observations -----------

    async def fetch_cell_pages(
        self, cell_specs: list[dict]
    ) -> dict[int, bytes]:
        """Render the given cells on ONE page canvas, ONE readback, crop in
        Python. Each spec: {cp, w, h, y0, pen_left, baseline_y, phase_x,
        phase_y, size_px}. Returns per-cp PNG bytes."""
        import json as _json

        out: dict[int, bytes] = {}
        if not cell_specs:
            return out
        page_w = max(int(s["w"]) for s in cell_specs)
        page_h = sum(int(s["h"]) for s in cell_specs)
        payload_cells = []
        for s in cell_specs:
            payload_cells.append(
                {
                    "ch": chr(int(s["cp"])),
                    "font_spec": self.font_spec(int(s["size_px"])),
                    "pen_left": float(s["pen_left"]),
                    "baseline_y": float(s["y0"]) + float(s["baseline_y"]),
                    "phase_x": float(s.get("phase_x", 0.0)),
                    "phase_y": float(s.get("phase_y", 0.0)),
                    "w": int(s["w"]),
                    "h": int(s["h"]),
                }
            )
        data_url = await self._evaluate(
            CELL_PAGE_JS, {"cells": payload_cells, "page_w": page_w, "page_h": page_h}
        )
        self.counters.browser_readbacks += 1
        if not isinstance(data_url, str) or "," not in data_url:
            return out
        try:
            page_png = base64.b64decode(data_url.split(",", 1)[1])
            page_img = Image.open(io.BytesIO(page_png)).convert("L")
        except Exception:
            return out
        y_cursor = 0
        for s in cell_specs:
            w, h = int(s["w"]), int(s["h"])
            crop = page_img.crop((0, y_cursor, min(page_w, w), min(page_img.height, y_cursor + h)))
            if crop.size != (w, h):
                canvas = Image.new("L", (w, h), 0)
                canvas.paste(crop, (0, 0))
                crop = canvas
            buf = io.BytesIO()
            crop.save(buf, format="PNG")
            out[int(s["cp"])] = buf.getvalue()
            y_cursor += h
        return out


# ----------------------------------------------------------------------
# Production MetricsProvider / RasterProvider over the shared session
# ----------------------------------------------------------------------

class SharedBrowserSession:
    """Holds THE single persistent session; lazy start; one instance per run."""

    def __init__(self, factory: Callable[[], PersistentBrowserAtlasSession]) -> None:
        self._factory = factory
        self._session: PersistentBrowserAtlasSession | None = None

    def set_factory(self, factory: Callable[[], PersistentBrowserAtlasSession]) -> None:
        """Rebind the lazy factory BEFORE first start (MD5 attestation)."""
        if self._session is not None:
            raise ValueError("ATLAS_BROWSER_SESSION_ALREADY_STARTED")
        self._factory = factory

    async def get(self) -> PersistentBrowserAtlasSession | None:
        if self._session is None:
            self._session = self._factory()
        if await self._session.ensure_started():
            return self._session
        return None

    @property
    def session(self) -> PersistentBrowserAtlasSession | None:
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


class ProductionMetricsProvider:
    """Batched measureText over the persistent session (never per-glyph)."""

    def __init__(self, holder: SharedBrowserSession) -> None:
        self._holder = holder

    async def fetch_rows(self, size_px: int, code_points: list[int]) -> list[list[float]]:
        session = await self._holder.get()
        if session is None:
            raise ValueError("ATLAS_RASTER_SOURCE_UNAVAILABLE")
        return await session.fetch_rows(size_px, code_points)

    async def fetch_pair_advances_px(self, size_px: int, pair_texts: list[str]) -> list[float]:
        session = await self._holder.get()
        if session is None:
            raise ValueError("ATLAS_RASTER_SOURCE_UNAVAILABLE")
        return await session.fetch_pair_advances_px(size_px, pair_texts)


def _paste_clipped(dst: Image.Image, src: Image.Image, left: int, top: int) -> None:
    """Paste with deterministic clipping (never raises on overflow)."""
    sw, sh = src.size
    dw, dh = dst.size
    x0, y0 = max(0, left), max(0, top)
    sx0, sy0 = x0 - left, y0 - top
    x1, y1 = min(dw, left + sw), min(dh, top + sh)
    if x1 <= x0 or y1 <= y0 or sx0 >= sw or sy0 >= sh:
        return
    dst.paste(src.crop((sx0, sy0, sx0 + (x1 - x0), sy0 + (y1 - y0))), (x0, y0))


class ProductionRasterProvider:
    """RasterProvider: Monotype CDN PRIMARY; the persistent browser session
    supplements ONLY missing/unattestable observations (canvas atlas pages).
    """

    def __init__(
        self,
        cdn_obs: dict[int, dict],
        cdn_source: MonotypeCdnRasterSource | None,
        holder: SharedBrowserSession,
        counters: AtlasTransportCounters,
        md5: str,
        family_name: str,
        style_name: str,
    ) -> None:
        self._cdn_obs = cdn_obs
        self._cdn = cdn_source
        self._holder = holder
        self.counters = counters
        self._md5 = md5
        self._family = family_name
        self._style = style_name
        self._metrics_upem: dict[int, Any] = {}
        self._ascent_px_by_size: dict[int, float] = {}
        self._descent_px_by_size: dict[int, float] = {}

    # The pipeline binds the regressed metrics after stage 1 so CDN ink
    # placement and browser cell drawing use OBSERVED metrics only.
    def bind_regressed_metrics(
        self,
        regressed: dict[int, Any],
        ascent_px_by_size: dict[int, float],
        descent_px_by_size: dict[int, float],
    ) -> None:
        self._metrics_upem = dict(regressed)
        self._ascent_px_by_size = dict(ascent_px_by_size)
        self._descent_px_by_size = dict(descent_px_by_size)

    def _metrics_px(self, cp: int, size_px: int) -> dict | None:
        r = self._metrics_upem.get(cp)
        if r is None:
            return None
        k = float(size_px) / 1000.0
        font_asc = self._ascent_px_by_size.get(size_px, size_px * 0.8)
        return {
            "left_px": r.lsb_upem * k,
            "ink_ascent_px": r.ascent_upem * k,
            "font_ascent_px": font_asc,
        }

    # -- CDN cropping ------------------------------------------------------

    def _cdn_cell_png(self, cell: Any, size_px: int, phase_x: float, phase_y: float) -> bytes | None:
        if abs(phase_x) > 1e-9 or abs(phase_y) > 1e-9:
            return None
        obs = self._cdn_obs.get(cell.code_point)
        if obs is None:
            return None
        if obs.get("space") is True:
            img = Image.new("L", (cell.w, cell.h), 0)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        if int(obs.get("size_px", 0)) != int(size_px):
            return None
        m = self._metrics_px(cell.code_point, size_px)
        if m is None:
            return None
        pen_left = getattr(cell, "pen_left_px", CELL_PAD_X_PX)
        x, y, w, h = obs["box"]
        ink = obs["sprite"].crop((x, y, x + w, y + h))
        img = Image.new("L", (cell.w, cell.h), 0)
        left = int(round(pen_left + m["left_px"]))
        top = int(round(CELL_PAD_Y_PX + m["font_ascent_px"] - m["ink_ascent_px"]))
        _paste_clipped(img, ink, left, top)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    # -- RasterProvider protocol -------------------------------------------

    async def fetch_page_cells(
        self, cells: list[Any], size_px: int, phase_x: float, phase_y: float
    ) -> dict[int, bytes]:
        out: dict[int, bytes] = {}
        missing: list[Any] = []
        for cell in cells:
            png = self._cdn_cell_png(cell, size_px, phase_x, phase_y)
            if png is not None:
                out[cell.code_point] = png
            else:
                missing.append(cell)
        if missing:
            session = await self._holder.get()
            if session is not None:
                specs = []
                for cell in missing:
                    m = self._metrics_px(cell.code_point, size_px)
                    font_asc = m["font_ascent_px"] if m else size_px * 0.8
                    specs.append(
                        {
                            "cp": cell.code_point,
                            "w": cell.w,
                            "h": cell.h,
                            "y0": 0,
                            "pen_left": getattr(cell, "pen_left_px", CELL_PAD_X_PX),
                            "baseline_y": CELL_PAD_Y_PX + font_asc,
                            "phase_x": phase_x,
                            "phase_y": phase_y,
                            "size_px": size_px,
                        }
                    )
                # One stacked page; the session performs ONE readback.
                for i, s in enumerate(specs):
                    s["y0"] = sum(int(x["h"]) for x in specs[:i])
                out.update(await session.fetch_cell_pages(specs))
        return out

    async def fetch_refinement(
        self, code_point: int, cell_w: int, cell_h: int, pen_left_px: int = CELL_PAD_X_PX
    ) -> tuple[bytes | None, bytes | None, bytes | None]:
        """Exactly the single-refinement set: 1024@0,0 ; 1024@0.5,0 ; 2048@0,0.
        CDN serves the phase-0 observations; the browser serves the shifted
        observation (and any missing/unattestable one). Never 512, never
        4096, never quarter phases."""
        size_px = FAST_RASTER_SIZE_PX
        base = self._cdn_cell_png(
            _CellShape(code_point, cell_w, cell_h), size_px, 0.0, 0.0
        )
        if base is None and self._cdn is not None and self._md5:
            obs = await self._cdn.fetch_single_cell(
                self._md5, self._family, self._style, code_point, size_px
            )
            if obs is not None:
                self._cdn_obs[code_point] = obs
                base = self._cdn_cell_png(
                    _CellShape(code_point, cell_w, cell_h), size_px, 0.0, 0.0
                )

        double = None
        if self._cdn is not None and self._md5:
            obs2 = await self._cdn.fetch_single_cell(
                self._md5, self._family, self._style, code_point, 2048
            )
            if obs2 is not None:
                saved = self._cdn_obs.get(code_point)
                self._cdn_obs[code_point] = obs2
                double = self._cdn_cell_png(
                    _CellShape(code_point, cell_w, cell_h), 2048, 0.0, 0.0
                )
                if saved is not None:
                    self._cdn_obs[code_point] = saved
                else:
                    self._cdn_obs.pop(code_point, None)

        shifted = None
        # The browser renders ONE observation per call (deterministic single
        # readback per observation; no same-cp cell collisions), and only
        # observations that are actually missing.
        for tag, sz, phx in (("base", 1024, 0.0), ("shifted", 1024, 0.5), ("double", 2048, 0.0)):
            have = {"base": base, "shifted": shifted, "double": double}[tag]
            if have is not None:
                continue
            session = await self._holder.get()
            if session is None:
                break
            m = self._metrics_px(code_point, sz)
            font_asc = m["font_ascent_px"] if m else sz * 0.8
            spec = {
                "cp": code_point,
                "w": cell_w,
                "h": cell_h,
                "y0": 0,
                "pen_left": pen_left_px,
                "baseline_y": CELL_PAD_Y_PX + font_asc,
                "phase_x": phx,
                "phase_y": 0.0,
                "size_px": sz,
            }
            rendered = await session.fetch_cell_pages([spec])
            png = rendered.get(code_point)
            if png is None:
                continue
            if tag == "base":
                base = png
            elif tag == "shifted":
                shifted = png
            else:
                double = png
        return base, shifted, double


class _CellShape:
    """Minimal cell shape for internal CDN cropping calls."""

    def __init__(self, code_point: int, w: int, h: int) -> None:
        self.code_point = code_point
        self.w = w
        self.h = h
        self.pen_left_px = CELL_PAD_X_PX


# ----------------------------------------------------------------------
# The default production atlas pipeline (chain orchestrator)
# ----------------------------------------------------------------------

class ProductionAtlasPipeline:
    """DEFAULT production atlas factory payload (R1).

    Chain order: (1) exact cache / authorized binary wins immediately ->
    (2) Chrome native --dump-dom metadata/MD5/binary -> (3) persistent
    Playwright session, started LAZILY only when needed -> (4) Monotype CDN
    PRIMARY raster with exact-MD5 verification -> (5) Algolia MD5 resolution
    then back to (4). Browser atlas pages supplement ONLY missing/
    unattestable observations. Counters record observed transport activity.
    """

    def __init__(
        self,
        *,
        job_id: str,
        mode: str,
        source_url: str,
        family_name: str,
        style_id: str,
        style_name: str,
        build_dir: Path | str,
        deadline: float | None,
        cache_root: Path | str,
        checkpoint_root: Path | str,
        binary_cache: AuthorizedBinaryCache | None = None,
        dump_dom_provider: ChromeDumpDomMetadataProvider | None = None,
        cdn_source: MonotypeCdnRasterSource | None = None,
        algolia_resolver: AlgoliaMd5Resolver | None = None,
        user_data_dir: Path | str | None = None,
        runtime: AtlasRuntimeDefaults | None = None,
        ai_provider: Any = None,
        playwright_launcher: Callable[..., Awaitable[Any]] | None = None,
        counters: AtlasTransportCounters | None = None,
        coverage_cap: int | None = None,
    ) -> None:
        self.job_id = job_id
        self.mode = str(mode).strip().upper()
        self.source_url = source_url.strip()
        self.family_name = family_name
        self.style_id = style_id
        self.style_name = style_name
        self.build_dir = Path(build_dir)
        self.deadline = deadline
        self.cache = AtlasCacheStore(Path(cache_root))
        self.checkpoint_store = AtlasCheckpointStore(Path(checkpoint_root))
        self.binary_cache = binary_cache
        self.counters = counters or AtlasTransportCounters()
        self.dump_dom = dump_dom_provider or ChromeDumpDomMetadataProvider(counters=self.counters)
        self.cdn = cdn_source or MonotypeCdnRasterSource(counters=self.counters)
        self.algolia = algolia_resolver or AlgoliaMd5Resolver(counters=self.counters)
        self.user_data_dir = user_data_dir
        self.runtime = runtime or AtlasRuntimeDefaults()
        self.ai_provider = ai_provider
        self.playwright_launcher = playwright_launcher
        # Bounded live-smoke knob (R5): when set, the observed coverage is
        # capped to the FIRST N code points of the observed set (deterministic
        # subset of OBSERVED glyphs - never invented coverage). Recorded in
        # the run evidence.
        self.coverage_cap = coverage_cap
        self.evidence_extras: dict[str, Any] = {}

    # -- identities ---------------------------------------------------------

    def _font_cache_id(self) -> str:
        return identity_hash(
            {
                "atlas_fonts_v1": True,
                "policy_hash": policy_identity_hash(),
                "source_url": self.source_url,
                "style_id": self.style_id,
                "mode": self.mode,
            }
        )

    def _binary_ref_fingerprint(self) -> str:
        import hashlib

        from compute.archive import canonical_source_identity

        return hashlib.sha256(
            canonical_source_identity(self.source_url).encode("utf-8")
        ).hexdigest()

    # -- stage 1: exact cache / authorized binary win ------------------------

    def _probe_font_cache(self) -> AtlasRunResult | None:
        fcid = self._font_cache_id()
        ttf = self.cache.get_bytes(NAMESPACE_FONTS, fcid + "_ttf", "ttf")
        otf = self.cache.get_bytes(NAMESPACE_FONTS, fcid + "_otf", "otf")
        report = self.cache.get_json(NAMESPACE_REPORTS, fcid)
        if ttf is None or otf is None or report is None:
            return None
        out = self.build_dir / "exact_cache_reuse"
        out.mkdir(parents=True, exist_ok=True)
        ttf_path = out / f"{fcid[:16]}.ttf"
        otf_path = out / f"{fcid[:16]}.otf"
        ttf_path.write_bytes(ttf)
        otf_path.write_bytes(otf)
        self.counters.cache_hits = getattr(self.counters, "cache_hits", 0) + 1
        return self._win_result(ttf_path, otf_path, report, "exact_cache_reuse")

    def _probe_binary_cache(self) -> AcquiredBinary | None:
        if self.mode != "ORIGINAL" or self.binary_cache is None:
            return None
        from acquisition.models import BINARY_PROVENANCE_PROBE_ORDER

        ref_fp = self._binary_ref_fingerprint()
        for prov in BINARY_PROVENANCE_PROBE_ORDER:
            identity = BinaryCacheIdentity(
                reference_fingerprint=ref_fp,
                family_name=self.family_name,
                style_id=self.style_id,
                provenance=prov,
            )
            raw, fmt, cached_prov, status = self.binary_cache.get(identity)
            if status == "CORRUPT":
                raise ValueError("ACQUISITION_BINARY_INTEGRITY_FAILED:L3_CACHE_CORRUPT")
            if status == "HIT" and raw is not None:
                return AcquiredBinary(
                    raw_bytes=raw,
                    format=fmt,
                    family_name=self.family_name,
                    style_name=self.style_name,
                    provenance=cached_prov or prov,
                )
        return None

    def _remember_binary_cache(self, binary: AcquiredBinary) -> None:
        if self.mode != "ORIGINAL" or self.binary_cache is None:
            return
        try:
            self.binary_cache.put(
                BinaryCacheIdentity(
                    reference_fingerprint=self._binary_ref_fingerprint(),
                    family_name=self.family_name,
                    style_id=self.style_id,
                    provenance=binary.provenance,
                ),
                binary.raw_bytes,
                binary.format,
                stage_provenance=binary.provenance,
            )
        except Exception as exc:
            logger.warning("binary cache write skipped: %s", type(exc).__name__)

    def _win_result(
        self, ttf_path: Path, otf_path: Path, report: dict, win_path: str
    ) -> AtlasRunResult:
        evidence = AtlasRunEvidence(
            policy=FAST_ATLAS_ULTRA_V1,
            policy_hash=policy_identity_hash(),
            mode=self.mode,
            glyph_count=0,
        )
        evidence.pages_by_source = {win_path: 1}
        evidence.validation = report
        return AtlasRunResult(
            model=None,
            ttf_path=ttf_path,
            otf_path=otf_path,
            report=report,
            evidence=evidence,
        )

    async def _binary_win(self, binary: AcquiredBinary) -> AtlasRunResult:
        from atlas.validation import _fonttools_structural

        out_dir = self.build_dir / "authorized_binary"
        ttf_file = prepare_binary_artifact(
            binary, "TTF", out_dir / "ttf", self.family_name, self.style_name
        )
        otf_file = prepare_binary_artifact(
            binary, "OTF", out_dir / "otf", self.family_name, self.style_name
        )
        ttf_struct = _fonttools_structural(ttf_file.file_path, "TTF")
        otf_struct = _fonttools_structural(otf_file.file_path, "OTF")
        if not ttf_struct.get("passed") or not otf_struct.get("passed"):
            raise ValueError("FAST30_FAILED")
        report = {
            "passed": True,
            "fonttools_ttf": ttf_struct,
            "fonttools_otf": otf_struct,
            "reasons": [],
            "binary_provenance": binary.provenance,
        }
        fcid = self._font_cache_id()
        self.cache.put_bytes_verified(
            NAMESPACE_FONTS, fcid + "_ttf", ttf_file.file_path.read_bytes(), "ttf"
        )
        self.cache.put_bytes_verified(
            NAMESPACE_FONTS, fcid + "_otf", otf_file.file_path.read_bytes(), "otf"
        )
        self.cache.put_json(NAMESPACE_REPORTS, fcid, report)
        return self._win_result(ttf_file.file_path, otf_file.file_path, report, "authorized_binary")

    # -- run -----------------------------------------------------------------

    async def run(self) -> AtlasRunResult:
        t_start = time.perf_counter()

        # Stage 1: exact cache / authorized binary wins immediately.
        cached = self._probe_font_cache()
        if cached is not None:
            cached.evidence.total_wall_seconds = time.perf_counter() - t_start
            return cached
        binary = self._probe_binary_cache()
        if binary is not None:
            result = await self._binary_win(binary)
            result.evidence.total_wall_seconds = time.perf_counter() - t_start
            return result

        holder = SharedBrowserSession(
            lambda: PersistentBrowserAtlasSession(
                source_url=self.source_url,
                family_name=self.family_name,
                style_name=self.style_name,
                style_id=self.style_id,
                expected_md5="",  # bound after discovery below
                user_data_dir=self.user_data_dir,
                counters=self.counters,
                playwright_launcher=self.playwright_launcher,
            )
        )
        try:
            # Stage 2: Chrome native --dump-dom metadata (family/style/MD5).
            md5 = ""
            envelope = await self.dump_dom.family_envelope(self.source_url, self.family_name)
            if envelope is not None:
                binary = await self.dump_dom.authorized_binary(
                    self.source_url, envelope, self.family_name, self.style_name
                )
                if binary is not None:
                    self._remember_binary_cache(binary)
                    result = await self._binary_win(binary)
                    result.evidence.total_wall_seconds = time.perf_counter() - t_start
                    return result
                rec = envelope.get_style_record(self.style_name, self.style_name) or (
                    envelope.get_style_record(self.style_id, self.style_name)
                )
                if rec is not None and len(rec.md5) == 32:
                    md5 = rec.md5.strip().lower()

            # Stage 5 (resolution only, when MD5 missing) -> back to CDN.
            if not md5 and self.algolia.available():
                md5 = await self.algolia.resolve_md5(
                    self.family_name, self.style_name, self.style_id, self.source_url
                )

            # Bind the resolved MD5 into the lazy session (attestation).
            holder.set_factory(  # bind the resolved MD5 before lazy start
                lambda: PersistentBrowserAtlasSession(
                    source_url=self.source_url,
                    family_name=self.family_name,
                    style_name=self.style_name,
                    style_id=self.style_id,
                    expected_md5=md5,
                    user_data_dir=self.user_data_dir,
                    counters=self.counters,
                    playwright_launcher=self.playwright_launcher,
                )
            )

            # Stage 4: Monotype CDN PRIMARY raster (exact-MD5 verified).
            cdn_obs: dict[int, dict] = {}
            if md5 and self.cdn.available():
                observed = await self.cdn.fetch_glyph_observations(
                    md5, self.family_name, self.style_name, FAST_RASTER_SIZE_PX
                )
                if observed:
                    cdn_obs = observed

            coverage = sorted(cp for cp in cdn_obs.keys())
            if coverage and self.coverage_cap is not None:
                coverage = coverage[: max(1, int(self.coverage_cap))]
                self.evidence_extras["coverage_cap_applied"] = int(self.coverage_cap)
            if not coverage:
                # Stage 3 fallback: the browser session discovers coverage
                # from @font-face unicode-range (bounded); its canvas pages
                # will serve every observation (CDN unattestable).
                session = await holder.get()
                if session is None:
                    raise ValueError("ATLAS_RASTER_SOURCE_UNAVAILABLE")
                coverage = await session.scan_coverage()
                if coverage and self.coverage_cap is not None:
                    coverage = coverage[: max(1, int(self.coverage_cap))]
                    self.evidence_extras["coverage_cap_applied"] = int(self.coverage_cap)
                if not coverage:
                    raise ValueError("ATLAS_RASTER_SOURCE_UNAVAILABLE")

            weight_class = self._weight_class()
            spec = AtlasStyleSpec(
                source_url=self.source_url,
                family_name=self.family_name,
                style_name=self.style_name,
                style_id=self.style_id,
                mode=self.mode,
                code_points=coverage,
                weight_class=weight_class,
            )
            raster_provider = ProductionRasterProvider(
                cdn_obs, self.cdn, holder, self.counters, md5, self.family_name, self.style_name
            )
            pipeline = AtlasUltraPipeline(
                spec=spec,
                runtime=self.runtime,
                metrics_provider=ProductionMetricsProvider(holder),
                raster_provider=raster_provider,
                cache=self.cache,
                checkpoint_store=self.checkpoint_store,
                deadline=self.deadline,
                ai_provider=self.ai_provider,
            )
            result = await pipeline.run()

            # Honest counters: OBSERVED transport activity replaces the
            # pipeline-internal fetch-call proxies.
            result.evidence.http_requests = self.counters.http_requests
            result.evidence.cdp_calls = self.counters.cdp_calls
            result.evidence.browser_readbacks = self.counters.browser_readbacks
            result.evidence.pages_by_source["cdn_primary"] = len(cdn_obs)

            # Persist final artifacts under the exact style identity.
            if result.ttf_path is not None and result.otf_path is not None:
                fcid = self._font_cache_id()
                try:
                    self.cache.put_bytes_verified(
                        NAMESPACE_FONTS, fcid + "_ttf",
                        Path(result.ttf_path).read_bytes(), "ttf",
                    )
                    self.cache.put_bytes_verified(
                        NAMESPACE_FONTS, fcid + "_otf",
                        Path(result.otf_path).read_bytes(), "otf",
                    )
                    self.cache.put_json(NAMESPACE_REPORTS, fcid, result.report)
                except OSError as exc:
                    logger.warning("atlas font cache write skipped: %s", type(exc).__name__)
            result.evidence.total_wall_seconds = time.perf_counter() - t_start
            if self.evidence_extras:
                result.evidence.pages_by_source.update(
                    {f"extra_{k}": v for k, v in self.evidence_extras.items()
                     if isinstance(v, int)}
                )
            return result
        finally:
            await holder.close()

    def _weight_class(self) -> int:
        from acquisition.adapters import extract_font_descriptors

        try:
            return int(extract_font_descriptors(self.style_name, self.style_id)["weight"])
        except Exception:
            return 400


# ----------------------------------------------------------------------
# Composition: the DEFAULT production atlas factory
# ----------------------------------------------------------------------

def build_default_atlas_pipeline_factory(
    settings: Any,
    binary_cache: AuthorizedBinaryCache | None = None,
) -> Callable[..., ProductionAtlasPipeline] | None:
    """Construct the default production atlas pipeline factory (R1 wiring).

    Fail-closed readiness: returns None when the atlas regime or the
    acquisition capability is disabled; raises when an enabled capability is
    not constructible (chromium absent). Shared stateless transports are
    constructed ONCE here; every factory call binds fresh per-run counters.
    """
    if not getattr(settings, "ATLAS_ULTRA_ENABLED", True):
        return None
    if not getattr(settings, "ACQUISITION_ENABLED", False):
        return None

    from measurement.browser_session import find_chromium_executable

    try:
        find_chromium_executable()
    except Exception as exc:
        raise RuntimeError("ATLAS_TRANSPORT_READINESS_FAILED_CHROMIUM") from exc

    from acquisition.adapters import _load_session_cookies

    session_cookies = _load_session_cookies(
        getattr(settings, "AUTHORIZED_SESSION_MATERIAL_FILE", None)
    )
    cdn_base = str(
        getattr(settings, "MONOTYPE_RASTER_ENDPOINT_URL", "") or "https://sig.monotype.com"
    )
    cdn_client = MonotypeRenderClient(session_cookies=session_cookies, base_url=cdn_base)

    algolia_client: AlgoliaMetadataClient | None = None
    algolia_app_id = getattr(settings, "MYFONTS_ALGOLIA_APP_ID", "")
    algolia_key_obj = getattr(settings, "MYFONTS_ALGOLIA_API_KEY", None)
    algolia_key = algolia_key_obj.get_secret_value() if algolia_key_obj is not None else None
    algolia_index = getattr(settings, "MYFONTS_ALGOLIA_INDEX_NAME", "prod_myfonts_fonts")
    if algolia_app_id and algolia_key:
        algolia_client = AlgoliaMetadataClient(
            app_id=algolia_app_id, api_key=algolia_key, index_name=algolia_index
        )

    user_data_dir = getattr(settings, "PLAYWRIGHT_USER_DATA_DIR", None)
    runtime = AtlasRuntimeDefaults(
        browser_sessions=getattr(settings, "ATLAS_BROWSER_SESSIONS", 1),
        http_concurrency=getattr(settings, "ATLAS_HTTP_CONCURRENCY", 8),
        glyph_workers=getattr(settings, "ATLAS_GLYPH_WORKERS", 2),
        atlas_pages_in_memory=getattr(settings, "ATLAS_PAGES_IN_MEMORY", 1),
        atlas_target_mb=getattr(settings, "ATLAS_TARGET_MB", 96),
        atlas_max_mb=getattr(settings, "ATLAS_MAX_MB", 128),
        checkpoint_batch=getattr(settings, "ATLAS_CHECKPOINT_BATCH", 32),
    ).validate()

    def factory(
        *,
        job_id: str,
        mode: str,
        source_url: str,
        family_name: str,
        style_id: str,
        style_name: str,
        build_dir: Path | str,
        deadline: float | None,
        cache_root: Path | str,
        checkpoint_root: Path | str,
        ai_provider: Any = None,
    ) -> ProductionAtlasPipeline:
        counters = AtlasTransportCounters()
        return ProductionAtlasPipeline(
            job_id=job_id,
            mode=mode,
            source_url=source_url,
            family_name=family_name,
            style_id=style_id,
            style_name=style_name,
            build_dir=build_dir,
            deadline=deadline,
            cache_root=cache_root,
            checkpoint_root=checkpoint_root,
            binary_cache=binary_cache,
            dump_dom_provider=ChromeDumpDomMetadataProvider(counters=counters),
            cdn_source=MonotypeCdnRasterSource(cdn_client, counters=counters),
            algolia_resolver=AlgoliaMd5Resolver(algolia_client, counters=counters),
            user_data_dir=user_data_dir,
            runtime=runtime,
            ai_provider=ai_provider,
            counters=counters,
        )

    return factory
