"""Convert successful authorized raster-provider results into complete,
immutable, exact-tuple observation collections.

Raster evidence is never discarded or relabeled: pages are parsed under a closed
schema, verified against the active observation config schedule, persisted
through the normal ObservationStore APIs, and finalized with the same strict
completeness checks as direct browser collection. The resulting completed
collection then feeds the normal Stage 9D raster gate.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
from typing import Sequence

from acquisition.models import SpriteRasterPage
from measurement.collector import ObservationCollector
from measurement.models import DirectMetrics, ObservationConfig, ObservationRecord, OpenTypeFeatureObservation
from measurement.store import ObservationStore


class _IngestSessionIdentity:
    """Browser-identity carrier for finalization (no browser is launched)."""

    def __init__(self, browser_version: str) -> None:
        self.browser_version = browser_version


def _required_metric_fields() -> tuple[str, ...]:
    return (
        "advance_width_px",
        "lsb_px",
        "rsb_px",
        "ascent_px",
        "descent_px",
        "advance_width_upem",
        "lsb_upem",
        "rsb_upem",
        "ascent_upem",
        "descent_upem",
        "bbox_width_upem",
        "bbox_height_upem",
    )


def ingest_raster_pages(
    store: ObservationStore,
    config: ObservationConfig,
    reference_id: str,
    style_id: str,
    browser_version: str,
    pages: Sequence[SpriteRasterPage],
    source_url: str = "authorized_raster_provider",
) -> int:
    """Persist complete exact-tuple observations from provider raster pages.

    Raises ValueError (fail-closed) on any identity drift, schema violation,
    schedule gap, or integrity mismatch. Returns the ingested glyph count.
    """
    if not pages:
        raise ValueError("RASTER_INGEST_EMPTY_PAGES")
    cfg_h = config.compute_hash()
    if not browser_version:
        raise ValueError("RASTER_INGEST_IDENTITY_REQUIRED")

    glyphs: dict[int, list[dict]] = {}
    pairs: list[dict] = []
    features: list[dict] = []

    for page in pages:
        payload = page.payload or {}
        page_bv = str(payload.get("browser_version", ""))
        if page_bv != browser_version:
            raise ValueError("RASTER_INGEST_IDENTITY_DRIFT")

        for g in payload.get("glyphs", []):
            cp = int(g["code_point"])
            resolution = int(g["resolution"])
            sub_x = float(g.get("subpixel_x", 0.0))
            sub_y = float(g.get("subpixel_y", 0.0))
            metrics_payload = g.get("metrics") or {}
            missing = [f for f in _required_metric_fields() if f not in metrics_payload]
            if missing:
                raise ValueError(f"RASTER_INGEST_INCOMPLETE_METRICS_CP_{cp}")
            png_b64 = g.get("png_base64")
            if not isinstance(png_b64, str) or not png_b64:
                raise ValueError(f"RASTER_INGEST_MISSING_RASTER_CP_{cp}")
            try:
                png_bytes = base64.b64decode(png_b64, validate=True)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"RASTER_INGEST_RASTER_DECODE_CP_{cp}") from exc
            if not png_bytes:
                raise ValueError(f"RASTER_INGEST_RASTER_EMPTY_CP_{cp}")

            font_size_px = float(config.font_size_px)
            metrics = DirectMetrics(
                code_point=cp,
                character=chr(cp),
                font_size_px=font_size_px,
                raw_advance_width=float(metrics_payload["advance_width_px"]),
                raw_actual_left=float(metrics_payload["lsb_px"]),
                raw_actual_right=float(metrics_payload["advance_width_px"]) - float(metrics_payload["rsb_px"]),
                raw_actual_ascent=float(metrics_payload["ascent_px"]),
                raw_actual_descent=-float(metrics_payload["descent_px"]),
                raw_font_ascent=float(metrics_payload["ascent_px"]),
                raw_font_descent=-float(metrics_payload["descent_px"]),
                advance_width_upem=float(metrics_payload["advance_width_upem"]),
                lsb_upem=float(metrics_payload["lsb_upem"]),
                rsb_upem=float(metrics_payload["rsb_upem"]),
                ascent_upem=float(metrics_payload["ascent_upem"]),
                descent_upem=float(metrics_payload["descent_upem"]),
                bbox_width_upem=float(metrics_payload["bbox_width_upem"]),
                bbox_height_upem=float(metrics_payload["bbox_height_upem"]),
                sample_count=int(metrics_payload.get("sample_count", 1)),
                confidence=float(metrics_payload.get("confidence", 1.0)),
            )
            record = ObservationRecord(
                cache_key=ObservationRecord.build_cache_key(
                    reference_id=reference_id,
                    style_id=style_id,
                    code_point=cp,
                    browser_version=browser_version,
                    resolution=resolution,
                    subpixel_x=sub_x,
                    subpixel_y=sub_y,
                    config_hash=cfg_h,
                ),
                reference_id=reference_id,
                style_id=style_id,
                code_point=cp,
                resolution=resolution,
                subpixel_x=sub_x,
                subpixel_y=sub_y,
                raster_relative_path=(
                    f"rasters/{reference_id}/{style_id}/{cp:04X}/{resolution}_{sub_x}_{sub_y}.png"
                ),
                raster_sha256=hashlib.sha256(png_bytes).hexdigest(),
                raster_size_bytes=len(png_bytes),
                metrics=metrics,
                created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                browser_version=browser_version,
                config_hash=cfg_h,
            )
            store.save_observation(record, png_bytes)
            glyphs.setdefault(cp, []).append(g)

        for p in payload.get("pairs", []):
            pairs.append(p)
        for f in payload.get("features", []):
            features.append(f)

    if not glyphs:
        raise ValueError("RASTER_INGEST_NO_GLYPHS")

    coverage = sorted(glyphs.keys())
    store.save_coverage(reference_id, style_id, coverage, browser_version=browser_version, config_hash=cfg_h)

    pair_tuples: list[tuple[int, int]] = []
    for p in pairs:
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
    for f in features:
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

    # Finalize through the same strict completeness checks as direct collection.
    collector = ObservationCollector(
        session=_IngestSessionIdentity(browser_version), store=store, config=config
    )
    collector.finalize_source_collection(
        reference_id, style_id, source_url=source_url, expected_pairs=pair_tuples
    )
    return len(coverage)
