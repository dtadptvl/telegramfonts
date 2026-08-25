"""Source font data acquisition, live preview resolution, raster/vector reconstruction, and fixture contracts."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import pickle
import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse
import httpx
from PIL import Image, ImageDraw

from compute.archive import canonical_source_identity
from compute.models import ArchiveSourceContext, ClaimStyle, GlyphVector, SourcePayload, StyleSourceData
from measurement.browser_session import ChromiumSession, close_browser_session
from measurement.collector import ObservationCollector
from measurement.models import ObservationConfig
from measurement.store import ObservationStore
from reconstruction.models import Contour, LineSegment, Point2D, ReconstructedGlyph, ReconstructionConfig
from reconstruction.solver import MaxReconstructionSolver

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
                schema_type = str(item.get("@type", ""))
                # Explicitly skip BreadcrumbList (never treat breadcrumb links as font styles)
                if "Breadcrumb" in schema_type:
                    continue

                variants: list[Any] = []
                # 1. CollectionPage with mainEntity ItemList
                if schema_type == "CollectionPage" and isinstance(item.get("mainEntity"), dict):
                    me = item["mainEntity"]
                    if me.get("@type") == "ItemList" and isinstance(me.get("itemListElement"), list):
                        variants.extend(me["itemListElement"])
                # 2. Direct ItemList (non-breadcrumb)
                elif schema_type == "ItemList" and isinstance(item.get("itemListElement"), list):
                    variants.extend(item["itemListElement"])

                # 3. Product / ProductModel variants or offers
                if isinstance(item.get("hasVariant"), list):
                    variants.extend(item["hasVariant"])
                if isinstance(item.get("offers"), list):
                    variants.extend(item["offers"])

                for v in variants:
                    if isinstance(v, dict):
                        s_name: str | None = None
                        if isinstance(v.get("item"), dict) and isinstance(v["item"].get("name"), str):
                            s_name = v["item"]["name"]
                        elif isinstance(v.get("name"), str):
                            s_name = v["name"]

                        if s_name and isinstance(s_name, str):
                            s_name_clean = s_name.strip()
                            s_id = re.sub(r'[^a-zA-Z0-9_-]+', '_', s_name_clean.lower()).strip('_')
                            if s_id and s_id not in seen_style_ids:
                                seen_style_ids.add(s_id)
                                styles_list.append({
                                    "id": s_id,
                                    "display_name": s_name_clean,
                                    "price": 5000,
                                })
        except Exception:
            pass

    # Pattern B: Embedded productVariants in JSON hydration scripts
    if not styles_list:
        pv_matches = re.findall(r'"productVariants"\s*:\s*(\[.*?\])\s*,\s*"(?:collection|product)"', html_text, re.DOTALL | re.IGNORECASE)
        for pv_json in pv_matches:
            try:
                p_list = json.loads(pv_json)
                if isinstance(p_list, list):
                    for p in p_list:
                        if isinstance(p, dict):
                            prod = p.get("product") if isinstance(p.get("product"), dict) else p
                            s_name = prod.get("title") or prod.get("name")
                            if isinstance(s_name, str) and s_name.strip():
                                s_name_clean = s_name.strip()
                                s_id = re.sub(r'[^a-zA-Z0-9_-]+', '_', s_name_clean.lower()).strip('_')
                                if s_id and s_id not in seen_style_ids:
                                    seen_style_ids.add(s_id)
                                    styles_list.append({
                                        "id": s_id,
                                        "display_name": s_name_clean,
                                        "price": 5000,
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
                    "price": 5000,
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
                    "price": 5000,
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


_RECONSTRUCTED_GLYPH_CACHE: dict[tuple[str, str], dict[int, ReconstructedGlyph]] = {}


class SourceAcquirer:
    def __init__(
        self,
        timeout: float = 20.0,
        client: httpx.AsyncClient | None = None,
        cache_dir: Path | str | None = None,
        observation_store_dir: Path | str | None = None,
        browser_session_factory: Callable[[], ChromiumSession] | None = None,
        observation_config: ObservationConfig | None = None,
    ) -> None:
        self.timeout = timeout
        self._external_client = client is not None
        self.client = client or httpx.AsyncClient(timeout=timeout)
        self.cache_dir = Path(cache_dir).resolve() if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_hits: int = 0
        self.cache_misses: int = 0
        self.last_cache_hit: bool = False

        if observation_store_dir:
            self.store_dir = Path(observation_store_dir)
        elif self.cache_dir:
            self.store_dir = self.cache_dir.parent / "observations"
        else:
            self.store_dir = Path("observations/runtime")

        self.store = ObservationStore(self.store_dir)
        self.browser_session_factory = browser_session_factory or (
            lambda: ChromiumSession(timeout_seconds=self.timeout)
        )
        self.observation_config = observation_config or ObservationConfig()
        self.solver = MaxReconstructionSolver(config=ReconstructionConfig())

    def get_archive_context(
        self,
        source_url: str,
        styles: list[ClaimStyle],
    ) -> ArchiveSourceContext | None:
        """Resolve cheap local observation identity without browser acquisition or reconstruction."""
        if not validate_myfonts_url(source_url) or not styles or self.store is None:
            return None

        parsed = urlparse(source_url.strip())
        path_parts = [p for p in parsed.path.split("/") if p]
        family_name = path_parts[-1].replace("-", " ").lower() if path_parts else "custom_font"
        family_key = family_name.replace(" ", "_").replace("-", "_")

        manifest: dict[str, Any] = {}
        manifest_path = self.store_dir / "manifest.json"
        if manifest_path.exists():
            try:
                raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(raw_manifest, dict):
                    manifest = raw_manifest
            except (OSError, ValueError):
                manifest = {}

        manifest_identity = {
            key: manifest.get(key, "")
            for key in (
                "git_commit",
                "git_is_dirty",
                "config_hash",
                "chromium_version",
                "fonttools_version",
            )
        }
        observation_config_hash = self.observation_config.compute_hash()
        active_browser_ver = str(manifest.get("chromium_version", "")).strip() or "unspecified_browser"
        style_observation_identities: list[tuple[str, str]] = []
        for style in styles:
            style_key = style.id.lower().replace(" ", "_").replace("-", "_")
            coverage = self.store.get_coverage(
                family_key, style_key, browser_version=active_browser_ver, config_hash=observation_config_hash
            )
            if not coverage:
                return None
            style_identity_payload = {
                "family_key": family_key,
                "requested_style_id": style.id,
                "requested_style_name": style.display_name,
                "resolved_style_id": style_key,
                "coverage_sha256": hashlib.sha256(
                    json.dumps(coverage, separators=(",", ":")).encode("utf-8")
                ).hexdigest(),
                "manifest": manifest_identity,
                "observation_config_hash": observation_config_hash,
            }
            style_observation_identities.append(
                (
                    style.id,
                    hashlib.sha256(
                        json.dumps(style_identity_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
                    ).hexdigest(),
                )
            )

        config_version = str(manifest.get("config_hash") or observation_config_hash)
        return ArchiveSourceContext(
            source_identity=canonical_source_identity(source_url),
            style_observation_identities=tuple(sorted(style_observation_identities)),
            config_version=config_version,
        )

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

        meta_cache_file = (
            self.cache_dir / f"meta_{re.sub(r'[^a-zA-Z0-9_-]', '_', source_url.strip())}.json"
        ) if self.cache_dir else None

        if meta_cache_file and meta_cache_file.exists():
            try:
                self.last_cache_hit = True
                self.cache_hits += 1
                return json.loads(meta_cache_file.read_text(encoding="utf-8"))
            except Exception:
                pass

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
        if 400 <= resp.status_code < 500:
            raise ValueError(f"SOURCE_HTTP_ERROR_{resp.status_code}")
        if resp.status_code >= 500:
            resp.raise_for_status()

        meta = extract_catalog_metadata_from_html(resp.text, source_url)
        if meta_cache_file:
            try:
                meta_cache_file.write_text(json.dumps(meta), encoding="utf-8")
            except Exception:
                pass

        self.last_cache_hit = False
        self.cache_misses += 1
        return meta

    async def acquire_source(
        self,
        source_url: str,
        styles: list[ClaimStyle],
        preview_input: bytes | dict[str, Any] | None = None,
        allow_web_fallback: bool = False,
    ) -> SourcePayload:
        """Validate source and acquire structured source payload from MAX observations exclusively (BLOCK B)."""
        if not validate_myfonts_url(source_url):
            raise ValueError("INVALID_SOURCE_URL")

        parsed = urlparse(source_url.strip())
        path_parts = [p for p in parsed.path.split("/") if p]
        family_name = path_parts[-1].replace("-", " ").title() if path_parts else "Custom Font"

        # 1. Offline structured fixture dict (test/fixture-only path)
        if isinstance(preview_input, dict):
            return self.from_fixture(preview_input)

        # 2. In-memory raster preview bytes (test-only path with explicit test input)
        if isinstance(preview_input, bytes):
            return self._build_from_raster_preview_bytes(source_url, family_name, styles, preview_input)

        # 3. Production path: reuse durable observations or collect authorized browser evidence on a miss.
        if not allow_web_fallback:
            family_key = family_name.lower().replace(" ", "_").replace("-", "_")
            browser_session: ChromiumSession | None = None
            collector: ObservationCollector | None = None
            collected_any = False
            style_data_map: dict[str, StyleSourceData] = {}
            try:
                for s in styles:
                    s_lower = s.display_name.lower()
                    is_bold = "bold" in s_lower or "black" in s_lower
                    is_italic = "italic" in s_lower or "oblique" in s_lower
                    style_key = s.id.lower().replace(" ", "_").replace("-", "_")
                    active_cfg_hash = self.observation_config.compute_hash()
                    matching = [
                        (b, c)
                        for b, c in self.store.get_completed_collection_identities(family_key, style_key)
                        if c == active_cfg_hash and self.store.is_source_collection_completed(family_key, style_key, c, b)
                    ]
                    has_completed_cache = False
                    if len(matching) == 1:
                        cand_bv, cand_cfg = matching[0]
                        cov = self.store.get_coverage(
                            family_key, style_key, browser_version=cand_bv, config_hash=cand_cfg
                        )
                        if cov:
                            has_completed_cache = True
                            coverage = cov
                            active_browser_ver = cand_bv
                            active_cfg_hash = cand_cfg

                    if not has_completed_cache:
                        attempt_key = f"{source_url.strip()}\0{s.id}\0{s.display_name}\0{active_cfg_hash}"
                        self.store.mark_source_collection_started(attempt_key)
                        if browser_session is None:
                            browser_session = self.browser_session_factory()
                            collector = ObservationCollector(
                                browser_session,
                                self.store,
                                self.observation_config,
                            )
                            await collector.initialize()
                        assert collector is not None
                        selected_font = await browser_session.observe_source_font(
                            source_url.strip(), s.display_name, family_name
                        )
                        await collector.collect_font_observations(
                            family_key, style_key, selected_font
                        )
                        await collector.collect_pair_observations(
                            family_key, style_key, selected_font
                        )
                        await collector.collect_feature_observations(
                            family_key, style_key, selected_font
                        )
                        active_browser_ver = browser_session.browser_version
                        active_cfg_hash = self.observation_config.compute_hash()
                        coverage = self.store.get_coverage(
                            family_key, style_key, browser_version=active_browser_ver, config_hash=active_cfg_hash
                        )
                        if not coverage:
                            raise ValueError(f"NO_OBSERVABLE_GLYPHS_FOR_{family_key}_{style_key}")
                        collector.finalize_source_collection(family_key, style_key, source_url=source_url.strip())
                        collected_any = True
                    else:
                        active_browser_ver, active_cfg_hash = matching[0]

                    cache_key = (family_key, style_key, active_browser_ver, active_cfg_hash)
                    if collected_any:
                        _RECONSTRUCTED_GLYPH_CACHE.pop(cache_key, None)
                    if cache_key in _RECONSTRUCTED_GLYPH_CACHE:
                        glyph_models = _RECONSTRUCTED_GLYPH_CACHE[cache_key]
                    else:
                        glyph_models = {}
                        safe_fam = re.sub(r"[^a-zA-Z0-9_-]", "_", family_key)
                        safe_style = re.sub(r"[^a-zA-Z0-9_-]", "_", style_key)
                        bv_hash = hashlib.sha256(active_browser_ver.encode("utf-8")).hexdigest()
                        cache_filename = f"reconstructed_{safe_fam}_{safe_style}_{bv_hash}_{active_cfg_hash}.pkl"
                        disk_cache_file = (self.store_dir / cache_filename).resolve()
                        if not disk_cache_file.is_relative_to(self.store_dir.resolve()):
                            raise ValueError(f"Reconstruction disk cache path escaped store directory: {cache_filename}")
                        if disk_cache_file.exists() and not collected_any:
                            try:
                                raw_cached = pickle.loads(disk_cache_file.read_bytes())
                                if isinstance(raw_cached, dict):
                                    if "glyph_models" in raw_cached:
                                        if (raw_cached.get("reference_id") == family_key and
                                            raw_cached.get("style_id") == style_key and
                                            raw_cached.get("browser_version") == active_browser_ver and
                                            raw_cached.get("config_hash") == active_cfg_hash and
                                            set(raw_cached.get("coverage", [])) == set(coverage) and
                                            isinstance(raw_cached.get("glyph_models"), dict) and
                                            set(raw_cached["glyph_models"].keys()) == set(coverage)):
                                            glyph_models = raw_cached["glyph_models"]
                                    elif set(raw_cached.keys()) == set(coverage):
                                        glyph_models = raw_cached
                            except Exception:
                                glyph_models = {}
                        if not glyph_models:
                            for cp in coverage:
                                observations = self.store.get_glyph_observations(
                                    family_key,
                                    style_key,
                                    cp,
                                    browser_version=active_browser_ver,
                                    config_hash=active_cfg_hash,
                                )
                                if observations:
                                    glyph_models[cp] = self.solver.reconstruct_glyph(observations)
                            if glyph_models and set(glyph_models.keys()) == set(coverage):
                                envelope = {
                                    "reference_id": family_key,
                                    "style_id": style_key,
                                    "browser_version": active_browser_ver,
                                    "config_hash": active_cfg_hash,
                                    "coverage": sorted(coverage),
                                    "glyph_models": glyph_models,
                                }
                                try:
                                    disk_cache_file.write_bytes(pickle.dumps(envelope))
                                except Exception:
                                    pass
                        _RECONSTRUCTED_GLYPH_CACHE[cache_key] = glyph_models

                    if not glyph_models:
                        raise ValueError(f"NO_MAX_RECONSTRUCTION_FOR_{family_key}_{style_key}")
                    style_data_map[s.id] = StyleSourceData(
                        style_id=s.id,
                        style_name=s.display_name,
                        weight_class=700 if is_bold else 400,
                        is_italic=is_italic,
                        reconstructed_glyphs=glyph_models,
                        observation_reference_id=family_key,
                        observation_style_id=style_key,
                        observation_browser_version=active_browser_ver,
                        observation_config_hash=active_cfg_hash,
                    )
            finally:
                if browser_session is not None:
                    await close_browser_session(browser_session)

            if not style_data_map:
                raise ValueError(f"NO_MAX_STYLES_COMPILED_FOR_{family_name}")

            self.last_cache_hit = not collected_any
            if collected_any:
                self.cache_misses += 1
            else:
                self.cache_hits += 1
            return SourcePayload(
                source_url=source_url.strip(),
                family_name=family_name,
                styles=style_data_map,
                archive_context=self.get_archive_context(source_url, styles),
            )

        # 4. Web scraping fallback (explicitly enabled for test_source.py web preview tests only)
        resp = await self.client.get(source_url.strip())
        if resp.status_code in (403, 429):
            raise ValueError(f"SOURCE_ACQUISITION_BLOCKED_{resp.status_code}")
        if 400 <= resp.status_code < 500:
            raise ValueError(f"SOURCE_HTTP_ERROR_{resp.status_code}")
        if resp.status_code >= 500:
            resp.raise_for_status()

        raw_preview_bytes = None
        content_type = resp.headers.get("content-type", "")
        if "image" in content_type:
            raw_preview_bytes = resp.content
        elif "text/html" in content_type:
            og_match = re.search(r'<meta\s+[^>]*property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']', resp.text, re.IGNORECASE)
            if not og_match:
                og_match = re.search(r'<meta\s+[^>]*content=["\']([^"\']+)["\'][^>]*property=["\']og:image["\']', resp.text, re.IGNORECASE)
            if og_match:
                img_url = urljoin(source_url, og_match.group(1).strip())
                img_resp = await self.client.get(img_url)
                if img_resp.status_code == 200:
                    raw_preview_bytes = img_resp.content
            if not raw_preview_bytes:
                raise ValueError("NO_PUBLIC_PREVIEW_FOUND")

        if raw_preview_bytes:
            return self._build_from_raster_preview_bytes(source_url, family_name, styles, raw_preview_bytes)

        raise ValueError(f"NO_MAX_OBSERVATIONS_FOUND_FOR_{family_name}")

    def _build_from_raster_preview_bytes(
        self,
        source_url: str,
        family_name: str,
        styles: list[ClaimStyle],
        raw_preview_bytes: bytes,
    ) -> SourcePayload:
        """Helper to build test payloads from explicit test raster preview bytes."""
        preview_identity = hashlib.sha256(raw_preview_bytes).hexdigest()
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

            reconstructed_contours: list[Contour] = []
            if len(char_contours) >= 2:
                segs = [
                    LineSegment(Point2D(char_contours[i][0], char_contours[i + 1][0]), Point2D(char_contours[i + 2][0], char_contours[i + 2][1]))
                    for i in range(len(char_contours) - 2)
                ]
                segs.append(LineSegment(Point2D(char_contours[-1][0], char_contours[-1][1]), Point2D(char_contours[0][0], char_contours[0][1])))
                reconstructed_contours.append(Contour(segments=segs, is_hole=False))

            glyphs_map: dict[str, GlyphVector] = {}
            reconstructed_map: dict[int, ReconstructedGlyph] = {}
            for ch, cp in [(".notdef", 0), ("space", 0x20), ("A", 0x41), ("B", 0x42), ("a", 0x61), ("b", 0x62)]:
                if ch == "space":
                    glyphs_map[ch] = GlyphVector(character=ch, contours=[], advance_width=300, lsb=0)
                    reconstructed_map[cp] = ReconstructedGlyph(
                        code_point=cp,
                        character=" ",
                        advance_width_upem=300.0,
                        lsb_upem=0.0,
                        rsb_upem=300.0,
                        ascent_upem=800.0,
                        descent_upem=-200.0,
                        contours=[],
                    )
                else:
                    glyphs_map[ch] = GlyphVector(
                        character=ch,
                        contours=char_contours,
                        advance_width=adv,
                        lsb=lsb,
                    )
                    reconstructed_map[cp] = ReconstructedGlyph(
                        code_point=cp,
                        character=ch if len(ch) == 1 else "",
                        advance_width_upem=float(adv),
                        lsb_upem=float(lsb),
                        rsb_upem=float(max(0, adv - lsb - 100)),
                        ascent_upem=800.0,
                        descent_upem=-200.0,
                        contours=reconstructed_contours,
                    )

            family_key = family_name.lower().replace(" ", "_").replace("-", "_")
            style_key = s.id.lower().replace(" ", "_").replace("-", "_")
            browser_ver = "chromium"
            style_data_map[s.id] = StyleSourceData(
                style_id=s.id,
                style_name=s.display_name,
                weight_class=700 if is_bold else 400,
                is_italic=is_italic,
                glyphs=glyphs_map,
                reconstructed_glyphs=reconstructed_map,
            )

        return SourcePayload(
            source_url=source_url.strip(),
            family_name=family_name,
            styles=style_data_map,
            archive_context=ArchiveSourceContext(
                source_identity=canonical_source_identity(source_url),
                style_observation_identities=tuple(
                    sorted(
                        (
                            style.id,
                            hashlib.sha256(
                                json.dumps(
                                    {
                                        "preview_identity": preview_identity,
                                        "style_id": style.id,
                                        "style_name": style.display_name,
                                    },
                                    sort_keys=True,
                                    separators=(",", ":"),
                                ).encode("utf-8")
                            ).hexdigest(),
                        )
                        for style in styles
                    )
                ),
                config_version="preview-v1",
            ),
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
        fixture_identity = hashlib.sha256(
            json.dumps(fixture_dict, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()
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
            reconstructed_glyphs: dict[int, ReconstructedGlyph] = {}
            for ch_name, g_data in raw_glyphs.items():
                if not isinstance(g_data, dict) or "contours" not in g_data:
                    raise ValueError(f"MALFORMED_GLYPH_DATA: {ch_name}")
                contours_raw = g_data["contours"]
                if not isinstance(contours_raw, list):
                    raise ValueError("MALFORMED_CONTOURS_NOT_LIST")

                adv = int(g_data.get("advance_width", 600))
                lsb = int(g_data.get("lsb", 50))
                glyphs[ch_name] = GlyphVector(
                    character=ch_name,
                    contours=contours_raw,
                    advance_width=adv,
                    lsb=lsb,
                )

                cp = ord(ch_name[0]) if len(ch_name) == 1 else (0x20 if ch_name == "space" else 0)
                reconstructed_contours: list[Contour] = []
                for loop in contours_raw:
                    if len(loop) >= 2:
                        segs = [
                            LineSegment(Point2D(loop[i][0], loop[i][1]), Point2D(loop[i + 1][0], loop[i + 1][1]))
                            for i in range(len(loop) - 1)
                        ]
                        segs.append(LineSegment(Point2D(loop[-1][0], loop[-1][1]), Point2D(loop[0][0], loop[0][1])))
                        reconstructed_contours.append(Contour(segments=segs, is_hole=False))

                reconstructed_glyphs[cp] = ReconstructedGlyph(
                    code_point=cp,
                    character=ch_name if len(ch_name) == 1 else "",
                    advance_width_upem=float(adv),
                    lsb_upem=float(lsb),
                    rsb_upem=float(max(0, adv - lsb - 100)),
                    ascent_upem=800.0,
                    descent_upem=-200.0,
                    contours=reconstructed_contours,
                )

            styles_map[s_id] = StyleSourceData(
                style_id=s_id,
                style_name=s_name,
                weight_class=int(s.get("weight_class", 400)),
                is_italic=bool(s.get("is_italic", False)),
                glyphs=glyphs,
                reconstructed_glyphs=reconstructed_glyphs,
            )

        return SourcePayload(
            source_url=source_url,
            family_name=family_name,
            styles=styles_map,
            archive_context=ArchiveSourceContext(
                source_identity=canonical_source_identity(source_url),
                style_observation_identities=tuple(
                    sorted(
                        (
                            style_id,
                            hashlib.sha256(
                                json.dumps(
                                    {
                                        "fixture_identity": fixture_identity,
                                        "style_id": style_id,
                                        "style_name": style_data.style_name,
                                    },
                                    sort_keys=True,
                                    separators=(",", ":"),
                                ).encode("utf-8")
                            ).hexdigest(),
                        )
                        for style_id, style_data in styles_map.items()
                    )
                ),
                config_version="fixture-v1",
            ),
        )
