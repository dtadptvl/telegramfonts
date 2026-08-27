"""Convert successful authorized raster-provider results into immutable,
exact-tuple observation collections.

The authorized raster endpoint is a raster-only source: the captured real
response supplies the MD5-bound glyph coverage, the code-point mapping, and
the binary PNG sprite with per-glyph cell boxes. It supplies no glyph
metrics, pairs, or features, and none are ever inferred from it.

The bounds-checked CDN sprite slices ARE the reconstruction raster evidence:
they are persisted directly as observation records, bound to the exact
MD5/style/page/request parameters and slice hashes. They are never
relabeled as Chromium raster provenance and never recaptured from the
source page. Browser measurement may supplement observable
metrics/pairs/features (approved ChromiumSession canvas path) but never
replaces CDN pixel evidence.

Render-size capability: the approved render query exposes ``acs_pt``
(point size) as the only observable raster scale control; every render is
phase (0.0, 0.0). When the active config requires raster evidence the CDN
cannot observably produce (e.g. held-out subpixel phases), ingestion fails
closed with the exact capability gap after persisting the observable CDN
pixel evidence — no synthesis, no recapture.
"""
from __future__ import annotations

import datetime
import hashlib
import io
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from PIL import Image

from acquisition.capability import KNOWN_RASTER_PROVIDERS, resolve_raster_provider
from acquisition.capability import ProviderRasterCapability
from acquisition.models import SpriteRasterPage
from measurement.collector import ObservationCollector, derive_multisize_kerning
from measurement.models import (
    DirectMetrics,
    MetricObservation,
    ObservationConfig,
    ObservationRecord,
    OpenTypeFeatureObservation,
)
from measurement.store import ObservationStore

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _empty_cell_png() -> bytes:
    """Deterministic zero-ink cell for independently proven space glyphs."""
    buf = io.BytesIO()
    Image.new("RGBA", (1, 1), (0, 0, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


EMPTY_CELL_PNG = _empty_cell_png()


def _cell_has_ink(cell: "Image.Image") -> bool:
    """Observable ink discriminator; alpha>0 alone is never ink.

    Ink requires a dark component. Transparent blanks, opaque-white blanks
    and fully white cells carry no ink. Alpha is consumed only as a mask
    over white and is never equated with target glyph ink.
    """
    img = cell
    if img.mode in ("RGBA", "LA", "PA"):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        img = background
    lo, _hi = img.convert("L").getextrema()
    return lo < 240


def _page_flagged_codepoints(page: SpriteRasterPage) -> tuple[set[int], bool]:
    """Provider missing/tofu signals observed on the page response.

    Returns the set of code points the producer flagged missing and whether
    any tofu evidence was observed. Absent headers yield no flags.
    """
    observed = (page.payload or {}).get("observed_headers") or {}
    tofu_flag = False
    tofu_raw = str(observed.get("x_tofus_found", "") or "").strip()
    if tofu_raw:
        try:
            tofu_flag = int(tofu_raw) > 0
        except ValueError:
            # Unparseable non-empty tofu signal fails closed.
            tofu_flag = True
    missing: set[int] = set()
    missing_raw = str(observed.get("x_missing_unicodes", "") or "").strip()
    if missing_raw:
        for token in re.split(r"[,;\s]+", missing_raw):
            cleaned = token.strip().upper().replace("U+", "")
            if not cleaned:
                continue
            try:
                missing.add(int(cleaned, 16))
            except ValueError:
                continue
    return missing, tofu_flag

# Provenance of raster pixels persisted from the authorized raster fallback.
# Distinct from chromium canvas provenance by construction.
RASTER_FALLBACK_PROVENANCE = "monotype_render_105_cdn_sprite"


class _IngestSessionIdentity:
    """Browser-identity carrier for finalization (no browser is launched)."""

    def __init__(self, browser_version: str) -> None:
        self.browser_version = browser_version


@dataclass(frozen=True)
class BrowserSupplementalEvidence:
    """Approved browser supplemental evidence for the raster fallback.

    Produced exclusively by the authorized ChromiumSession canvas path
    (never by the raster endpoint, never synthesized). Supplements
    observable metrics/pairs/features only — it never carries raster
    pixels; reconstruction pixels come from the CDN sprite slices.
    """

    browser_version: str
    metrics: Mapping[int, DirectMetrics]
    pairs: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    features: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    # Sealed raw per-size METRIC evidence: one browser-measured DirectMetrics
    # row per coverage code point per declared metric size (cp -> size ->
    # metrics). Persisted as metric_observations under the exact collection
    # identity; finalization enforces the closed schedule (fail-closed).
    metric_schedule: Mapping[int, Mapping[float, DirectMetrics]] = field(default_factory=dict)
    # Sealed raw per-size PAIR evidence: one row per declared pair per
    # declared metric size carrying font_size_px, left/right/pair advances
    # and the per-size inferred kerning ((left, right) -> rows). Persisted as
    # pair_size_observations under the exact collection identity; the stored
    # derived kerning must recompute from these rows (finalization enforces).
    pair_schedule: Mapping[tuple[int, int], Sequence[Mapping[str, Any]]] = field(default_factory=dict)


def _slice_sprite_cells(
    page: SpriteRasterPage,
    missing_cps: "set[int] | frozenset[int]" = frozenset(),
) -> tuple[dict[int, bytes], dict[int, dict[str, Any]]]:
    """Validate the page sprite and slice each observable glyph cell.

    Consumes the actual binary PNG sprite: every mapped glyph must have a
    bounds-checked cell that re-encodes as a non-empty PNG. The exact
    request binding (provider/provenance, MD5, render size, page index) is
    recomputed here — a presence-only dict is insufficient. Blank/tofu/
    missing glyph evidence fails closed; an independently proven space-like
    zero-ink glyph is accepted only via its bound zero-area cell. Returns
    the slices plus their exact request binding. Any gap fails closed.
    """
    payload = page.payload or {}
    glyphs = payload.get("glyphs") or []
    if not glyphs:
        raise ValueError("RASTER_INGEST_PAGE_NO_GLYPHS")
    md5 = str(payload.get("md5", ""))
    acs_pt = payload.get("acs_pt")
    if not md5 or not isinstance(acs_pt, int) or acs_pt < 1:
        raise ValueError("RASTER_INGEST_REQUEST_BINDING_MISSING")
    request_params = payload.get("request_params")
    if not isinstance(request_params, dict) or not request_params:
        raise ValueError("RASTER_INGEST_REQUEST_BINDING_MISSING")

    try:
        sprite = Image.open(io.BytesIO(page.raster_bytes))
        sprite.load()
    except Exception as exc:
        raise ValueError("RASTER_INGEST_SPRITE_DECODE_FAILED") from exc
    sprite_w, sprite_h = sprite.size

    # Recomputed exact request binding: provider/provenance, MD5, render
    # size and page index must all match — never presence-only.
    provenance = str(payload.get("provenance", "")).strip()
    if not provenance or provenance not in KNOWN_RASTER_PROVIDERS:
        raise ValueError("RASTER_PROVIDER_UNKNOWN_OR_ABSENT")
    if str(request_params.get("provider", "")) != provenance:
        raise ValueError("RASTER_INGEST_REQUEST_BINDING_PROVIDER_MISMATCH")
    if str(request_params.get("md5", "")).strip().lower() != md5:
        raise ValueError("RASTER_INGEST_REQUEST_BINDING_MD5_MISMATCH")
    try:
        rp_acs_pt = int(request_params.get("acs_pt", 0))
        rp_acs_p = int(request_params.get("acs_p", 0))
    except (TypeError, ValueError):
        raise ValueError("RASTER_INGEST_REQUEST_BINDING_MISMATCH")
    if rp_acs_pt != acs_pt:
        raise ValueError("RASTER_INGEST_REQUEST_BINDING_SIZE_MISMATCH")
    if rp_acs_p != int(page.page_index):
        raise ValueError("RASTER_INGEST_REQUEST_BINDING_PAGE_MISMATCH")

    slices: dict[int, bytes] = {}
    bindings: dict[int, dict[str, Any]] = {}
    for g in glyphs:
        cp = int(g["code_point"])
        # Provider-flagged missing glyphs are never ingested.
        if cp in missing_cps:
            raise ValueError(f"RASTER_INGEST_MISSING_UNICODE_CP_{cp}")
        box = g.get("sprite_box") or {}
        try:
            x = int(box["x"])
            y = int(box["y"])
            w = int(box["width"])
            h = int(box["height"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"RASTER_INGEST_BOX_MALFORMED_CP_{cp}") from exc
        if g.get("is_space") is True:
            # Independently proven zero-outline glyph (measured space): bound
            # zero-area cell, never sliced pixels, never required ink.
            if cp != 32 or x != 0 or y != 0 or w != 0 or h != 0:
                raise ValueError(f"RASTER_INGEST_SPACE_BINDING_MALFORMED_CP_{cp}")
            if cp in slices:
                raise ValueError(f"RASTER_INGEST_DUPLICATE_CP_{cp}")
            slices[cp] = EMPTY_CELL_PNG
            bindings[cp] = {
                "md5": md5,
                "acs_pt": acs_pt,
                "page_index": int(page.page_index),
                "request_params": dict(request_params),
                "sprite_sha256": str(payload.get("sprite_sha256", ""))
                or hashlib.sha256(page.raster_bytes).hexdigest(),
                "slice_sha256": hashlib.sha256(EMPTY_CELL_PNG).hexdigest(),
                "zero_ink_proven": True,
            }
            continue
        if w < 1 or h < 1 or x < 0 or y < 0 or x + w > sprite_w or y + h > sprite_h:
            raise ValueError(f"RASTER_INGEST_BOX_OUT_OF_BOUNDS_CP_{cp}")
        cell = sprite.crop((x, y, x + w, y + h))
        # Closed ink gate: transparent blanks, opaque-white blanks and any
        # zero-ink printable cell fail closed; alpha>0 alone is never ink.
        if not _cell_has_ink(cell):
            raise ValueError(f"RASTER_INGEST_CELL_NO_INK_CP_{cp}")
        buf = io.BytesIO()
        cell.save(buf, format="PNG")
        png_bytes = buf.getvalue()
        if not png_bytes.startswith(PNG_MAGIC):
            raise ValueError(f"RASTER_INGEST_SLICE_NOT_PNG_CP_{cp}")
        if cp in slices:
            raise ValueError(f"RASTER_INGEST_DUPLICATE_CP_{cp}")
        slices[cp] = png_bytes
        bindings[cp] = {
            "md5": md5,
            "acs_pt": acs_pt,
            "page_index": int(payload.get("request_params", {}).get("acs_p", page.page_index)),
            "request_params": dict(request_params),
            "sprite_sha256": str(payload.get("sprite_sha256", ""))
            or hashlib.sha256(page.raster_bytes).hexdigest(),
            "slice_sha256": hashlib.sha256(png_bytes).hexdigest(),
        }
    return slices, bindings


def ingest_raster_pages(
    store: ObservationStore,
    config: ObservationConfig,
    reference_id: str,
    style_id: str,
    supplement: BrowserSupplementalEvidence | None,
    pages: Sequence[SpriteRasterPage],
    capability: ProviderRasterCapability,
    source_url: str = "authorized_raster_provider",
) -> int:
    """Persist immutable exact-tuple observations from provider raster pages.

    The bounds-checked CDN sprite slices are stored as the raster evidence
    under the closed capability schedule (one observation per code point per
    allocated render size at the provider's fixed phase); ``supplement``
    contributes browser-measured metrics, pairs, and features only. Raises
    ValueError (fail-closed) on any identity drift, schema violation,
    coverage mismatch, missing size, or capability forgery. Returns the
    ingested glyph count.
    """
    if not pages:
        raise ValueError("RASTER_INGEST_EMPTY_PAGES")
    if supplement is None:
        raise ValueError("RASTER_INGEST_BROWSER_SUPPLEMENT_REQUIRED")
    if not isinstance(capability, ProviderRasterCapability):
        raise ValueError("CAPABILITY_FORGED: closed capability descriptor required")
    capability.validate()
    # Independently recomputed provider identity: every page must agree on
    # exactly one known producer, and it must equal the capability provider.
    # Unknown/absent/mixed provenance or a page/capability mismatch fails
    # closed before any persistence (no default, no relabel).
    resolved_provider = resolve_raster_provider(pages)
    if resolved_provider != capability.provider:
        raise ValueError("RASTER_PROVIDER_PAGE_CAPABILITY_MISMATCH")
    browser_version = str(supplement.browser_version or "")
    if not browser_version:
        raise ValueError("RASTER_INGEST_IDENTITY_REQUIRED")
    cfg_h = config.compute_hash()

    # 1. Validate and slice the real CDN sprites (raster pixels consumed).
    slices_by_pt: dict[int, dict[int, bytes]] = {}
    bindings: dict[int, dict[str, Any]] = {}
    for page in pages:
        payload = page.payload or {}
        if not str(payload.get("browser_version", "")):
            raise ValueError("RASTER_INGEST_IDENTITY_DRIFT")
        # Provider missing/tofu signals: tofu evidence or producer-flagged
        # missing glyphs fail closed and are never ingested.
        missing_cps, tofu_flag = _page_flagged_codepoints(page)
        if tofu_flag:
            raise ValueError("RASTER_INGEST_TOFU_EVIDENCE")
        page_slices, page_bindings = _slice_sprite_cells(page, missing_cps)
        pt = int(payload["acs_pt"])
        bucket = slices_by_pt.setdefault(pt, {})
        for cp, png in page_slices.items():
            if cp in bucket:
                raise ValueError(f"RASTER_INGEST_DUPLICATE_CP_{cp}_PT_{pt}")
            bucket[cp] = png
            bindings.setdefault(cp, page_bindings[cp])
    if not slices_by_pt:
        raise ValueError("RASTER_INGEST_NO_GLYPHS")
    coverage_cdn = sorted(set().union(*[set(s.keys()) for s in slices_by_pt.values()]))

    # 2. Cross-bind: CDN-observed coverage must equal the browser-measured
    #    supplemental coverage exactly. Drift fails closed; nothing invented.
    if set(supplement.metrics.keys()) != set(coverage_cdn):
        raise ValueError("RASTER_INGEST_COVERAGE_DRIFT")

    # 3. Persist the observable CDN pixel evidence under the closed
    #    capability schedule: one record per code point per allocated render
    #    size at the provider's fixed phase. The resolution label is the
    #    requested acs_pt render parameter itself.
    required_sizes = capability.all_sizes()
    missing_sizes = [s for s in required_sizes if s not in slices_by_pt]
    if missing_sizes:
        raise ValueError(
            f"RASTER_CAPABILITY_MISSING_SIZES: provider '{capability.provider}' "
            f"pages lack render sizes {missing_sizes}"
        )
    phase = capability.phase
    for cp in coverage_cdn:
        direct_metrics = supplement.metrics[cp]
        if not isinstance(direct_metrics, DirectMetrics):
            raise ValueError(f"RASTER_INGEST_BROWSER_METRICS_MISSING_CP_{cp}")
        for size in required_sizes:
            png_bytes = slices_by_pt[size].get(cp)
            if png_bytes is None:
                raise ValueError(
                    f"RASTER_INGEST_CDN_RASTER_MISSING_CP_{cp}_PT_{size}"
                )
            binding = bindings[cp]
            record = ObservationRecord(
                cache_key=ObservationRecord.build_cache_key(
                    reference_id=reference_id,
                    style_id=style_id,
                    code_point=cp,
                    browser_version=browser_version,
                    resolution=size,
                    subpixel_x=phase[0],
                    subpixel_y=phase[1],
                    config_hash=cfg_h,
                ),
                reference_id=reference_id,
                style_id=style_id,
                code_point=cp,
                resolution=size,
                subpixel_x=phase[0],
                subpixel_y=phase[1],
                raster_relative_path=(
                    f"rasters/{reference_id}/{style_id}/{cp:04X}/"
                    f"{size}_{phase[0]}_{phase[1]}.png"
                ),
                raster_sha256=hashlib.sha256(png_bytes).hexdigest(),
                raster_size_bytes=len(png_bytes),
                metrics=direct_metrics,
                created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                browser_version=browser_version,
                config_hash=cfg_h,
            )
            store.save_observation(record, png_bytes)
            bindings[cp] = {**binding, "observed_at": record.cache_key}

    store.save_coverage(
        reference_id, style_id, coverage_cdn,
        browser_version=browser_version, config_hash=cfg_h,
    )

    # 4b. Closed raw per-size METRIC evidence carried by the approved browser
    #     path: one sealed DirectMetrics row per coverage code point per
    #     declared metric size under the exact collection identity.
    #     Finalization enforces the closed schedule (missing/extra/duplicate
    #     rows fail closed); nothing here relaxes that closure.
    for cp in coverage_cdn:
        per_size = (supplement.metric_schedule or {}).get(int(cp)) or {}
        for size in sorted(per_size):
            dm = per_size[size]
            if not isinstance(dm, DirectMetrics):
                raise ValueError(f"RASTER_INGEST_BROWSER_METRICS_MISSING_CP_{cp}")
            store.save_metric_observation(
                MetricObservation(
                    reference_id=reference_id,
                    style_id=style_id,
                    browser_version=browser_version,
                    config_hash=cfg_h,
                    metrics=dm,
                    created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                )
            )

    # 5. Supplemental pairs/features: browser-measured only, provenance
    #    verified by finalization below.
    pair_tuples: list[tuple[int, int]] = []
    for p in supplement.pairs:
        left_cp = int(p["left_cp"])
        right_cp = int(p["right_cp"])
        left_adv = float(p["left_advance_upem"])
        right_adv = float(p["right_advance_upem"])
        pair_adv = float(p["pair_advance_upem"])
        # Closed raw per-size pair evidence carried by the approved browser
        # path: persisted under the exact collection identity. The stored
        # derived kerning recomputes from these sealed rows; finalization
        # enforces the closed schedule and the exact recompute (fail-closed).
        size_rows = list((supplement.pair_schedule or {}).get((left_cp, right_cp)) or ())
        for row in size_rows:
            store.save_pair_size_observation(
                reference_id=reference_id,
                style_id=style_id,
                left_cp=left_cp,
                right_cp=right_cp,
                font_size_px=float(row["font_size_px"]),
                left_advance_upem=float(row["left_advance_upem"]),
                right_advance_upem=float(row["right_advance_upem"]),
                pair_advance_upem=float(row["pair_advance_upem"]),
                inferred_kerning_upem=int(row["inferred_kerning_upem"]),
                browser_version=browser_version,
                config_hash=cfg_h,
                provenance=f"chromium:{browser_version}:canvas_text_metrics",
            )
        if size_rows:
            inferred_kerning = derive_multisize_kerning(size_rows)
        else:
            inferred_kerning = int(round(pair_adv - (left_adv + right_adv)))
        store.save_pair_observation(
            reference_id=reference_id,
            style_id=style_id,
            left_cp=left_cp,
            right_cp=right_cp,
            left_char=chr(left_cp),
            right_char=chr(right_cp),
            left_advance_upem=round(left_adv, 2),
            right_advance_upem=round(right_adv, 2),
            pair_advance_upem=round(pair_adv, 2),
            inferred_kerning_upem=inferred_kerning,
            confidence=1.0,
            provenance=f"chromium:{browser_version}:canvas_text_metrics",
            browser_version=browser_version,
            config_hash=cfg_h,
        )
        pair_tuples.append((left_cp, right_cp))

    required_probes = set(config.feature_probes)
    ingested_probes = set()
    for f in supplement.features:
        tag = str(f["feature_tag"])
        sample_text = str(f["sample_text"])
        effect_observed = bool(
            abs(float(f["enabled_advance_upem"]) - float(f["disabled_advance_upem"])) > 0.01
            or str(f["enabled_raster_signature"]) != str(f["disabled_raster_signature"])
        )
        obs = OpenTypeFeatureObservation(
            reference_id=reference_id,
            style_id=style_id,
            browser_version=browser_version,
            config_hash=cfg_h,
            feature_tag=tag,
            sample_text=sample_text,
            enabled_advance_upem=float(f["enabled_advance_upem"]),
            disabled_advance_upem=float(f["disabled_advance_upem"]),
            enabled_raster_signature=str(f["enabled_raster_signature"]),
            disabled_raster_signature=str(f["disabled_raster_signature"]),
            effect_observed=effect_observed,
            provenance=f"chromium:{browser_version}:canvas_feature_probe",
            created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )
        store.save_feature_observation(obs)
        ingested_probes.add((tag, sample_text))
    if ingested_probes != required_probes:
        raise ValueError("RASTER_INGEST_FEATURE_PROBES_INCOMPLETE")

    # 6. Finalize through the same strict completeness checks as direct
    #    collection, under the sealed capability schedule. The stored
    #    supplemental pair set must match the declared pair tuples exactly.
    collector = ObservationCollector(
        session=_IngestSessionIdentity(browser_version), store=store, config=config
    )
    collector.finalize_source_collection(
        reference_id, style_id, source_url=source_url,
        expected_pairs=pair_tuples, provider_capability=capability,
    )
    return len(coverage_cdn)


async def collect_browser_measurement(
    source_url: str,
    family_name: str,
    style_name: str,
    code_points: Sequence[int],
    config: ObservationConfig,
) -> BrowserSupplementalEvidence:
    """Approved browser supplemental path for the raster fallback.

    Measures observable metrics, pair advances, and feature probes against
    the source page through the authorized ChromiumSession canvas path.
    NEVER captures rasters: reconstruction pixels come exclusively from the
    CDN sprite slices. Any failure raises (fail closed); nothing is
    synthesized.
    """
    from measurement.browser_session import ChromiumSession, close_browser_session
    from typography.models import BOUNDED_FIT_PAIRS

    if not code_points:
        raise ValueError("RASTER_FALLBACK_NO_CODE_POINTS")
    session = ChromiumSession(timeout_seconds=config.timeout_seconds)
    try:
        await session.start()
        font = await session.observe_source_font(source_url, style_name, family_name)
        browser_version = session.browser_version

        # Sealed raw per-size metric evidence: every coverage code point is
        # measured across the exact declared metric schedule (plus the anchor
        # size when it is not itself a schedule size). The raw rows are
        # carried for persistence under the exact collection identity;
        # caller-authored aggregates never substitute (fail-closed).
        metric_sizes = tuple(float(s) for s in config.metric_sizes_px)
        anchor_size = float(config.font_size_px)
        metrics: dict[int, DirectMetrics] = {}
        metric_schedule: dict[int, dict[float, DirectMetrics]] = {}
        for cp in code_points:
            per_size: dict[float, DirectMetrics] = {}
            for size in metric_sizes:
                per_size[size] = await session.measure_glyph_direct(
                    font, cp, font_size_px=size, upem=config.upem
                )
            anchor = per_size.get(anchor_size)
            if anchor is None:
                anchor = await session.measure_glyph_direct(
                    font, cp, font_size_px=anchor_size, upem=config.upem
                )
            metrics[cp] = anchor
            metric_schedule[cp] = per_size

        coverage = set(code_points)
        # Browser-measured pair set: bounded fit pairs within coverage plus
        # deterministic in-coverage supplements so the gate partition always
        # has disjoint fit/held-out pair evidence. Every pair is measured in
        # the browser; only the selection is deterministic.
        target_pairs: list[tuple[int, int]] = [
            (l, r) for l, r in BOUNDED_FIT_PAIRS if l in coverage and r in coverage
        ]
        if len(target_pairs) < 2:
            sorted_cps = sorted(coverage)
            supplements = [(cp, cp) for cp in sorted_cps]
            supplements.extend(zip(sorted_cps, sorted_cps[1:]))
            for pair in supplements:
                if len(target_pairs) >= 2:
                    break
                if pair not in target_pairs:
                    target_pairs.append(pair)
        pairs: list[dict[str, Any]] = []
        pair_schedule: dict[tuple[int, int], list[dict[str, Any]]] = {}
        for left_cp, right_cp in target_pairs:
            text = chr(left_cp) + chr(right_cp)
            # Sealed raw per-size pair evidence across the exact metric
            # schedule; the derived kerning must recompute from these rows.
            size_rows: list[dict[str, Any]] = []
            anchor_pair_adv: float | None = None
            for size in metric_sizes:
                pair_adv_size = await session.measure_text_advance(
                    font, text, size, config.upem
                )
                if abs(size - anchor_size) < 1e-9:
                    anchor_pair_adv = pair_adv_size
                l_adv = metric_schedule[left_cp][size].advance_width_upem
                r_adv = metric_schedule[right_cp][size].advance_width_upem
                size_rows.append(
                    {
                        "font_size_px": size,
                        "left_advance_upem": l_adv,
                        "right_advance_upem": r_adv,
                        "pair_advance_upem": pair_adv_size,
                        "inferred_kerning_upem": int(round(pair_adv_size - (l_adv + r_adv))),
                    }
                )
            if anchor_pair_adv is None:
                anchor_pair_adv = await session.measure_text_advance(
                    font, text, anchor_size, config.upem
                )
            pair_schedule[(left_cp, right_cp)] = size_rows
            pairs.append(
                {
                    "left_cp": left_cp,
                    "right_cp": right_cp,
                    "left_advance_upem": metrics[left_cp].advance_width_upem,
                    "right_advance_upem": metrics[right_cp].advance_width_upem,
                    "pair_advance_upem": anchor_pair_adv,
                }
            )

        features: list[dict[str, Any]] = []
        for tag, text in config.feature_probes:
            probe = await session.probe_opentype_feature(
                font, tag, text, config.font_size_px, config.upem
            )
            features.append({"feature_tag": tag, "sample_text": text, **probe})

        return BrowserSupplementalEvidence(
            browser_version=browser_version,
            metrics=metrics,
            pairs=pairs,
            features=features,
            metric_schedule=metric_schedule,
            pair_schedule=pair_schedule,
        )
    finally:
        await close_browser_session(session)


def page_slice_attestation(pages: Sequence[SpriteRasterPage]) -> dict[str, Any]:
    """Sanitized attestation of the consumed raster evidence (trace-safe).

    Validates every page sprite and returns per-code-point slice bindings
    (MD5, page index, request parameters, slice hashes) plus page sprite
    hashes. Raises ValueError on any validation gap, mirroring
    ``ingest_raster_pages`` fail-closed behavior.
    """
    attestation: dict[str, Any] = {"sprite_sha256": [], "bindings": {}}
    # Provider identity is resolved fail-closed (known, single, explicit);
    # attestation never relabels or defaults the producer.
    attestation["provider"] = resolve_raster_provider(pages)
    for page in pages:
        payload = page.payload or {}
        sprite_sha = str(payload.get("sprite_sha256", ""))
        if not sprite_sha:
            sprite_sha = hashlib.sha256(page.raster_bytes).hexdigest()
        attestation["sprite_sha256"].append(sprite_sha)
        _slices, page_bindings = _slice_sprite_cells(page)
        for cp, binding in page_bindings.items():
            key = str(cp)
            # Multiple render sizes legitimately bind the same code point;
            # identical render parameters would be an evidence collision.
            existing = attestation["bindings"].get(key)
            if existing is not None and (
                existing["md5"], existing["acs_pt"], existing["page_index"]
            ) == (binding["md5"], binding["acs_pt"], binding["page_index"]):
                raise ValueError(f"RASTER_INGEST_DUPLICATE_CP_{cp}")
            attestation["bindings"][key] = binding
    return attestation
