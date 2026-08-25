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
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from PIL import Image

from acquisition.models import SpriteRasterPage
from measurement.collector import ObservationCollector
from measurement.models import DirectMetrics, ObservationConfig, ObservationRecord, OpenTypeFeatureObservation
from measurement.store import ObservationStore

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# Provenance of raster pixels persisted from the authorized raster fallback.
# Distinct from chromium canvas provenance by construction.
RASTER_FALLBACK_PROVENANCE = "monotype_render_105_cdn_sprite"

# The approved render query renders phase (0.0, 0.0) only; no parameter in
# the captured contract exposes subpixel phase control.
OBSERVABLE_RENDER_PHASE = (0.0, 0.0)


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


def _slice_sprite_cells(
    page: SpriteRasterPage,
) -> tuple[dict[int, bytes], dict[int, dict[str, Any]]]:
    """Validate the page sprite and slice each observable glyph cell.

    Consumes the actual binary PNG sprite: every mapped glyph must have a
    bounds-checked cell that re-encodes as a non-empty PNG. Returns the
    slices plus their exact request binding (MD5, page index, acs_pt,
    request parameters, sprite/slice hashes). Any gap fails closed.
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

    slices: dict[int, bytes] = {}
    bindings: dict[int, dict[str, Any]] = {}
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


def _config_phase_requirements(config: ObservationConfig) -> tuple[tuple[float, float], ...]:
    phases = set(config.base_subpixel_phases)
    phases.update(config.expanded_subpixel_phases)
    phases.update(config.held_out_subpixel_phases)
    return tuple(sorted(phases))


def ingest_raster_pages(
    store: ObservationStore,
    config: ObservationConfig,
    reference_id: str,
    style_id: str,
    supplement: BrowserSupplementalEvidence | None,
    pages: Sequence[SpriteRasterPage],
    source_url: str = "authorized_raster_provider",
) -> int:
    """Persist immutable exact-tuple observations from provider raster pages.

    The bounds-checked CDN sprite slices are stored as the raster evidence
    (one observation per code point per observable render size, phase
    (0.0, 0.0)); ``supplement`` contributes browser-measured metrics, pairs,
    and features only. Raises ValueError (fail-closed) on any identity
    drift, schema violation, coverage mismatch, or capability gap. Returns
    the ingested glyph count.
    """
    if not pages:
        raise ValueError("RASTER_INGEST_EMPTY_PAGES")
    if supplement is None:
        raise ValueError("RASTER_INGEST_BROWSER_SUPPLEMENT_REQUIRED")
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
        page_slices, page_bindings = _slice_sprite_cells(page)
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

    # 3. Persist the observable CDN pixel evidence under the exact tuple:
    #    one record per code point per render size, phase (0.0, 0.0). The
    #    resolution label is the requested acs_pt render parameter itself.
    for cp in coverage_cdn:
        direct_metrics = supplement.metrics[cp]
        if not isinstance(direct_metrics, DirectMetrics):
            raise ValueError(f"RASTER_INGEST_BROWSER_METRICS_MISSING_CP_{cp}")
        phases = config.get_phases_for_metrics(direct_metrics)
        if tuple(phases) != (OBSERVABLE_RENDER_PHASE,):
            raise ValueError(
                "RASTER_CAPABILITY_GAP: adaptive subpixel phase expansion "
                f"{tuple(phases)} is not renderable by the approved raster "
                "query (phase (0.0, 0.0) only)"
            )
        for pt, bucket in sorted(slices_by_pt.items()):
            png_bytes = bucket.get(cp)
            if png_bytes is None:
                raise ValueError(
                    f"RASTER_INGEST_CDN_RASTER_MISSING_CP_{cp}_PT_{pt}"
                )
            binding = bindings[cp]
            record = ObservationRecord(
                cache_key=ObservationRecord.build_cache_key(
                    reference_id=reference_id,
                    style_id=style_id,
                    code_point=cp,
                    browser_version=browser_version,
                    resolution=pt,
                    subpixel_x=OBSERVABLE_RENDER_PHASE[0],
                    subpixel_y=OBSERVABLE_RENDER_PHASE[1],
                    config_hash=cfg_h,
                ),
                reference_id=reference_id,
                style_id=style_id,
                code_point=cp,
                resolution=pt,
                subpixel_x=OBSERVABLE_RENDER_PHASE[0],
                subpixel_y=OBSERVABLE_RENDER_PHASE[1],
                raster_relative_path=(
                    f"rasters/{reference_id}/{style_id}/{cp:04X}/"
                    f"{pt}_{OBSERVABLE_RENDER_PHASE[0]}_{OBSERVABLE_RENDER_PHASE[1]}.png"
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

    # 4. Capability gate: the immutable snapshot requires disjoint held-out
    #    raster evidence. The approved render query exposes acs_pt (size) as
    #    the only raster scale control and renders phase (0.0, 0.0) only, so
    #    any non-zero held-out/expanded phase is causally unobtainable. Fail
    #    closed with the exact gap; never synthesize, never recapture.
    unobservable = [
        p for p in _config_phase_requirements(config)
        if tuple(p) != OBSERVABLE_RENDER_PHASE
    ]
    if unobservable:
        gap = ", ".join(f"({x}, {y})" for x, y in unobservable)
        raise ValueError(
            "RASTER_CAPABILITY_GAP: held-out/expanded subpixel phase raster "
            f"evidence {gap} is not exposed by any approved render query "
            "parameter (rbe, acs_pt, acs_w, acs_l, acs_ar, acs_p, acs_gpp); "
            "CDN pixel evidence persisted, snapshot completion blocked"
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
    #    collection. The stored supplemental pair set must match the
    #    declared pair tuples exactly.
    collector = ObservationCollector(
        session=_IngestSessionIdentity(browser_version), store=store, config=config
    )
    collector.finalize_source_collection(
        reference_id, style_id, source_url=source_url, expected_pairs=pair_tuples
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

        metrics: dict[int, DirectMetrics] = {}
        for cp in code_points:
            metrics[cp] = await session.measure_glyph_direct(
                font, cp, font_size_px=config.font_size_px, upem=config.upem
            )

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

        return BrowserSupplementalEvidence(
            browser_version=browser_version,
            metrics=metrics,
            pairs=pairs,
            features=features,
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
