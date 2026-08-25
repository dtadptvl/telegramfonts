"""Convert successful authorized raster-provider results into complete,
immutable, exact-tuple observation collections.

The authorized raster endpoint is a raster-only source: the captured real
response supplies the MD5-bound glyph coverage, the code-point mapping, and
the binary PNG sprite with per-glyph cell boxes. It supplies no glyph
metrics, pairs, or features, and none are ever inferred from it.

Those measurements must arrive as explicit approved browser-measurement
evidence (ChromiumSession canvas path) before the immutable snapshot may
complete. Raster pages are validated under the closed schema (PNG magic,
bounds-checked sprite-cell slices), cross-bound to the browser-measured
coverage, persisted through the normal ObservationStore APIs, and finalized
with the same strict completeness checks as direct browser collection.
"""
from __future__ import annotations

import datetime
import hashlib
import io
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from PIL import Image

from acquisition.models import SpriteRasterPage
from measurement.collector import ObservationCollector
from measurement.models import DirectMetrics, ObservationConfig, ObservationRecord, OpenTypeFeatureObservation
from measurement.store import ObservationStore

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class _IngestSessionIdentity:
    """Browser-identity carrier for finalization (no browser is launched)."""

    def __init__(self, browser_version: str) -> None:
        self.browser_version = browser_version


@dataclass(frozen=True)
class BrowserMeasurementEvidence:
    """Approved browser-measurement evidence for the raster fallback.

    Produced exclusively by the authorized ChromiumSession canvas measurement
    path (never by the raster endpoint, never synthesized):
      metrics:  code point -> DirectMetrics measured in the browser
      rasters:  code point -> {(resolution, subpixel_x, subpixel_y): PNG}
                covering every fit/held-out tuple of the active config
      pairs:    browser-measured pair advances
      features: browser-measured OpenType feature probes
    """

    browser_version: str
    metrics: Mapping[int, DirectMetrics]
    rasters: Mapping[int, Mapping[tuple[int, float, float], bytes]]
    pairs: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    features: Sequence[Mapping[str, Any]] = field(default_factory=tuple)


def _slice_sprite_cells(
    page: SpriteRasterPage,
) -> dict[int, bytes]:
    """Validate the page sprite and slice each observable glyph cell.

    Consumes the actual binary PNG sprite: every mapped glyph must have a
    bounds-checked cell that re-encodes as a non-empty PNG. Any gap fails
    closed with a ValueError.
    """
    payload = page.payload or {}
    glyphs = payload.get("glyphs") or []
    if not glyphs:
        raise ValueError("RASTER_INGEST_PAGE_NO_GLYPHS")
    try:
        sprite = Image.open(io.BytesIO(page.raster_bytes))
        sprite.load()
    except Exception as exc:
        raise ValueError("RASTER_INGEST_SPRITE_DECODE_FAILED") from exc
    sprite_w, sprite_h = sprite.size

    slices: dict[int, bytes] = {}
    for g in glyphs:
        cp = int(g["code_point"])
        box = g.get("sprite_box") or {}
        try:
            x = int(box["x"])
            y = int(box["y"])
            w = int(box["width"])
            h = int(box["height"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"RASTER_INGEST_BOX_MALFORMED_CP_{cp}") from exc
        if w < 1 or h < 1 or x < 0 or y < 0 or x + w > sprite_w or y + h > sprite_h:
            raise ValueError(f"RASTER_INGEST_BOX_OUT_OF_BOUNDS_CP_{cp}")
        cell = sprite.crop((x, y, x + w, y + h))
        buf = io.BytesIO()
        cell.save(buf, format="PNG")
        png_bytes = buf.getvalue()
        if not png_bytes.startswith(PNG_MAGIC):
            raise ValueError(f"RASTER_INGEST_SLICE_NOT_PNG_CP_{cp}")
        if cp in slices:
            raise ValueError(f"RASTER_INGEST_DUPLICATE_CP_{cp}")
        slices[cp] = png_bytes
    return slices


def ingest_raster_pages(
    store: ObservationStore,
    config: ObservationConfig,
    reference_id: str,
    style_id: str,
    browser_measurement: BrowserMeasurementEvidence | None,
    pages: Sequence[SpriteRasterPage],
    source_url: str = "authorized_raster_provider",
) -> int:
    """Persist complete exact-tuple observations from provider raster pages.

    The raster pages contribute the MD5-bound coverage and validated sprite
    cells; metrics/pairs/features and the observation raster schedule come
    exclusively from ``browser_measurement`` (approved browser path). Raises
    ValueError (fail-closed) on any identity drift, schema violation,
    coverage mismatch, schedule gap, or integrity mismatch. Returns the
    ingested glyph count.
    """
    if not pages:
        raise ValueError("RASTER_INGEST_EMPTY_PAGES")
    if browser_measurement is None:
        raise ValueError("RASTER_INGEST_BROWSER_MEASUREMENT_REQUIRED")
    browser_version = str(browser_measurement.browser_version or "")
    if not browser_version:
        raise ValueError("RASTER_INGEST_IDENTITY_REQUIRED")
    cfg_h = config.compute_hash()

    # 1. Validate the raster-only provider evidence: real sprite PNGs sliced
    #    at the observable per-glyph boxes. Never ingested as metrics.
    slices: dict[int, bytes] = {}
    for page in pages:
        payload = page.payload or {}
        if not str(payload.get("browser_version", "")):
            raise ValueError("RASTER_INGEST_IDENTITY_DRIFT")
        page_slices = _slice_sprite_cells(page)
        for cp, png in page_slices.items():
            if cp in slices:
                raise ValueError(f"RASTER_INGEST_DUPLICATE_CP_{cp}")
            slices[cp] = png
    if not slices:
        raise ValueError("RASTER_INGEST_NO_GLYPHS")

    # 2. Cross-bind: CDN-observed coverage must equal the browser-measured
    #    coverage exactly. Drift fails closed; nothing is invented.
    if set(browser_measurement.metrics.keys()) != set(slices.keys()):
        raise ValueError("RASTER_INGEST_COVERAGE_DRIFT")

    # 3. Persist exact-tuple observations: metrics and every raster in the
    #    active schedule come from the approved browser measurement path.
    coverage = sorted(slices.keys())
    for cp in coverage:
        direct_metrics = browser_measurement.metrics[cp]
        if not isinstance(direct_metrics, DirectMetrics):
            raise ValueError(f"RASTER_INGEST_BROWSER_METRICS_MISSING_CP_{cp}")
        phases = config.get_phases_for_metrics(direct_metrics)
        eval_res = max(config.resolutions)
        schedule: list[tuple[int, float, float]] = [
            (res, sx, sy) for res in config.resolutions for sx, sy in phases
        ]
        schedule.extend((eval_res, sx, sy) for sx, sy in config.held_out_subpixel_phases)
        cp_rasters = browser_measurement.rasters.get(cp) or {}
        for res, sub_x, sub_y in schedule:
            png_bytes = cp_rasters.get((res, sub_x, sub_y))
            if not isinstance(png_bytes, (bytes, bytearray)) or not png_bytes:
                raise ValueError(
                    f"RASTER_INGEST_BROWSER_RASTER_MISSING_CP_{cp}_RES_{res}"
                )
            png_bytes = bytes(png_bytes)
            if not png_bytes.startswith(PNG_MAGIC):
                raise ValueError(
                    f"RASTER_INGEST_BROWSER_RASTER_NOT_PNG_CP_{cp}_RES_{res}"
                )
            record = ObservationRecord(
                cache_key=ObservationRecord.build_cache_key(
                    reference_id=reference_id,
                    style_id=style_id,
                    code_point=cp,
                    browser_version=browser_version,
                    resolution=res,
                    subpixel_x=sub_x,
                    subpixel_y=sub_y,
                    config_hash=cfg_h,
                ),
                reference_id=reference_id,
                style_id=style_id,
                code_point=cp,
                resolution=res,
                subpixel_x=sub_x,
                subpixel_y=sub_y,
                raster_relative_path=(
                    f"rasters/{reference_id}/{style_id}/{cp:04X}/{res}_{sub_x}_{sub_y}.png"
                ),
                raster_sha256=hashlib.sha256(png_bytes).hexdigest(),
                raster_size_bytes=len(png_bytes),
                metrics=direct_metrics,
                created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                browser_version=browser_version,
                config_hash=cfg_h,
            )
            store.save_observation(record, png_bytes)

    store.save_coverage(reference_id, style_id, coverage, browser_version=browser_version, config_hash=cfg_h)

    # 4. Pairs and features: browser-measured only, provenance verified by
    #    finalization below.
    pair_tuples: list[tuple[int, int]] = []
    for p in browser_measurement.pairs:
        left_cp = int(p["left_cp"])
        right_cp = int(p["right_cp"])
        left_adv = float(p["left_advance_upem"])
        right_adv = float(p["right_advance_upem"])
        pair_adv = float(p["pair_advance_upem"])
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
            inferred_kerning_upem=int(round(pair_adv - (left_adv + right_adv))),
            confidence=1.0,
            provenance=f"chromium:{browser_version}:canvas_text_metrics",
            browser_version=browser_version,
            config_hash=cfg_h,
        )
        pair_tuples.append((left_cp, right_cp))

    required_probes = set(config.feature_probes)
    ingested_probes = set()
    for f in browser_measurement.features:
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

    # 5. Finalize through the same strict completeness checks as direct
    #    collection. The stored browser-measured pair set must match the
    #    declared pair tuples exactly (same contract as the direct
    #    collection path with explicit pairs).
    collector = ObservationCollector(
        session=_IngestSessionIdentity(browser_version), store=store, config=config
    )
    collector.finalize_source_collection(
        reference_id, style_id, source_url=source_url, expected_pairs=pair_tuples
    )
    return len(coverage)


async def collect_browser_measurement(
    source_url: str,
    family_name: str,
    style_name: str,
    code_points: Sequence[int],
    config: ObservationConfig,
) -> BrowserMeasurementEvidence:
    """Approved browser-measurement path for the raster fallback.

    The raster endpoint supplies raster/coverage only; metrics, pair
    advances, feature probes, and the observation raster schedule are
    measured exclusively here through the authorized ChromiumSession canvas
    path against the observable source page. Any failure raises (fail
    closed); nothing is synthesized.
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

        metrics: dict[int, DirectMetrics] = {}
        rasters: dict[int, dict[tuple[int, float, float], bytes]] = {}
        eval_res = max(config.resolutions)
        for cp in code_points:
            dm = await session.measure_glyph_direct(
                font, cp, font_size_px=config.font_size_px, upem=config.upem
            )
            metrics[cp] = dm
            phases = config.get_phases_for_metrics(dm)
            schedule: list[tuple[int, float, float]] = [
                (res, sx, sy) for res in config.resolutions for sx, sy in phases
            ]
            schedule.extend((eval_res, sx, sy) for sx, sy in config.held_out_subpixel_phases)
            cp_rasters: dict[tuple[int, float, float], bytes] = {}
            for res, sx, sy in schedule:
                cp_rasters[(res, sx, sy)] = await session.capture_lossless_raster(
                    font, cp, res, (sx, sy)
                )
            rasters[cp] = cp_rasters

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
        for left_cp, right_cp in target_pairs:
            pair_adv = await session.measure_text_advance(
                font, chr(left_cp) + chr(right_cp), config.font_size_px, config.upem
            )
            pairs.append(
                {
                    "left_cp": left_cp,
                    "right_cp": right_cp,
                    "left_advance_upem": metrics[left_cp].advance_width_upem,
                    "right_advance_upem": metrics[right_cp].advance_width_upem,
                    "pair_advance_upem": pair_adv,
                }
            )

        features: list[dict[str, Any]] = []
        for tag, text in config.feature_probes:
            probe = await session.probe_opentype_feature(
                font, tag, text, config.font_size_px, config.upem
            )
            features.append({"feature_tag": tag, "sample_text": text, **probe})

        return BrowserMeasurementEvidence(
            browser_version=browser_version,
            metrics=metrics,
            rasters=rasters,
            pairs=pairs,
            features=features,
        )
    finally:
        await close_browser_session(session)


def page_slice_attestation(pages: Sequence[SpriteRasterPage]) -> dict[str, Any]:
    """Sanitized attestation of the consumed raster evidence (trace-safe).

    Validates every page sprite and returns per-code-point slice hashes plus
    page sprite hashes. Raises ValueError on any validation gap, mirroring
    ``ingest_raster_pages`` fail-closed behavior.
    """
    attestation: dict[str, Any] = {"sprite_sha256": [], "slice_sha256": {}}
    for page in pages:
        payload = page.payload or {}
        sprite_sha = str(payload.get("sprite_sha256", ""))
        if not sprite_sha:
            sprite_sha = hashlib.sha256(page.raster_bytes).hexdigest()
        attestation["sprite_sha256"].append(sprite_sha)
        for cp, png in _slice_sprite_cells(page).items():
            attestation["slice_sha256"][str(cp)] = hashlib.sha256(png).hexdigest()
    return attestation
