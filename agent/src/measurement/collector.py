"""Observation collector orchestrating direct browser metric extraction and multi-resolution raster storage."""
from __future__ import annotations

import datetime
import hashlib
import logging
import time
from typing import Any, Callable

from measurement.browser_session import ChromiumSession
from measurement.discovery import DiscoveryBudgetExhaustedError, ObservableGlyphDiscovery
from measurement.manifest import create_reproducibility_manifest


def derive_multisize_kerning(evidence: "Sequence[Mapping[str, Any]]") -> int:
    """Deterministic robust kerning derivation from raw per-size evidence.

    The derived value is the lower-median of the per-size inferred kerning;
    caller-authored aggregates are never accepted — consumers must recompute
    from the raw rows.
    """
    rows = list(evidence)
    if not rows:
        raise ValueError("MULTISIZE_KERNING_NO_EVIDENCE")
    values = sorted(int(r["inferred_kerning_upem"]) for r in rows)
    return values[(len(values) - 1) // 2]


def validate_pair_size_schedule(
    evidence: "Sequence[Mapping[str, Any]]",
    expected_sizes: "Sequence[float]",
) -> None:
    """Reject missing/extra/duplicate per-size evidence against the declared
    metric schedule (raw evidence identity is closed)."""
    observed = [float(r["font_size_px"]) for r in evidence]
    if len(observed) != len(set(observed)):
        raise ValueError("MULTISIZE_KERNING_DUPLICATE_SIZE")
    expected = [float(s) for s in expected_sizes]
    if sorted(observed) != sorted(expected):
        missing = sorted(set(expected) - set(observed))
        extra = sorted(set(observed) - set(expected))
        raise ValueError(
            f"MULTISIZE_KERNING_SCHEDULE_MISMATCH:missing={missing}:extra={extra}"
        )


def validate_metric_size_schedule(
    rows: "Sequence[Mapping[str, Any]]",
    expected_sizes: "Sequence[float]",
) -> None:
    """Reject missing/extra/duplicate raw per-size metric evidence against the
    closed metric schedule (raw evidence identity is closed for one code
    point)."""
    observed = [float(r["font_size_px"]) for r in rows]
    if len(observed) != len(set(observed)):
        raise ValueError("MULTISIZE_METRIC_DUPLICATE_SIZE")
    expected = [float(s) for s in expected_sizes]
    if sorted(observed) != sorted(expected):
        missing = sorted(set(expected) - set(observed))
        extra = sorted(set(observed) - set(expected))
        raise ValueError(
            f"MULTISIZE_METRIC_SCHEDULE_MISMATCH:missing={missing}:extra={extra}"
        )
from measurement.models import (
    BrowserFontSelection,
    MetricObservation,
    ObservationConfig,
    ObservationRecord,
    OpenTypeFeatureObservation,
)
from measurement.store import ObservationStore

logger = logging.getLogger("telegramfonts.agent.measurement.collector")


class ObservationCollector:
    """Orchestrates persistent Chromium measurement session and immutable observation storage."""

    def __init__(
        self,
        session: ChromiumSession,
        store: ObservationStore,
        config: ObservationConfig | None = None,
    ) -> None:
        self.session = session
        self.store = store
        self.config = config or ObservationConfig()

    async def initialize(self) -> None:
        """Initialize browser session and store manifest."""
        await self.session.start()
        manifest = create_reproducibility_manifest(
            config=self.config,
            chromium_version=self.session.browser_version,
        )
        self.store.save_manifest(manifest)

    async def collect_font_observations(
        self,
        reference_id: str,
        style_id: str,
        font_family: str | BrowserFontSelection,
        code_points: list[int] | None = None,
        progress_cb: Callable[[int, int], None] | None = None,
        max_consecutive_misses: int = 500,
    ) -> tuple[int, int, float]:
        """Collect direct browser metrics and multi-resolution raster observations for a font style.
        
        Returns:
            (glyphs_count, total_observations_captured_or_resumed, elapsed_time_seconds)
        """
        start_time = time.perf_counter()
        config_hash = self.config.compute_hash()

        # If code_points not explicitly supplied, discover observable glyphs dynamically using authoritative discovery
        if code_points is None:
            code_points, termination = await ObservableGlyphDiscovery.discover_with_termination(
                measure_fn=lambda cp: self.session.is_glyph_supported_in_font(font_family, cp),
                max_consecutive_misses=max_consecutive_misses,
            )
            if termination in ObservableGlyphDiscovery.TERMINAL_BLOCKED:
                # Safety-budget exhaustion is never successful completion:
                # fail closed with partial coverage, never save it as complete.
                raise DiscoveryBudgetExhaustedError(
                    f"DISCOVERY_BUDGET_EXHAUSTED: partial coverage {len(code_points)} glyphs"
                )

        self.store.save_coverage(
            reference_id=reference_id,
            style_id=style_id,
            code_points=code_points,
            browser_version=self.session.browser_version,
            config_hash=config_hash,
        )

        total_rasters = 0
        total_glyphs = len(code_points)

        for idx, cp in enumerate(code_points, start=1):
            # 1. Direct browser metric measurements across the canonical size schedule.
            metrics_by_size = {}
            for metric_size in self.config.metric_sizes_px:
                measured = await self.session.measure_glyph_direct(
                    font_family=font_family,
                    code_point=cp,
                    font_size_px=metric_size,
                    upem=self.config.upem,
                )
                metrics_by_size[metric_size] = measured
                self.store.save_metric_observation(
                    MetricObservation(
                        reference_id=reference_id,
                        style_id=style_id,
                        browser_version=self.session.browser_version,
                        config_hash=config_hash,
                        metrics=measured,
                        created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    )
                )
            direct_metrics = metrics_by_size.get(self.config.font_size_px)
            if direct_metrics is None:
                direct_metrics = await self.session.measure_glyph_direct(
                    font_family=font_family,
                    code_point=cp,
                    font_size_px=self.config.font_size_px,
                    upem=self.config.upem,
                )

            # 2. Determine adaptive subpixel phase schedule based on metric boundary alignment
            subpixel_phases = self.config.get_phases_for_metrics(direct_metrics)

            # 3. Multi-resolution lossless raster captures + adaptive subpixel phase schedule (Fit Evidence)
            for res in self.config.resolutions:
                for sub_x, sub_y in subpixel_phases:
                    cache_key = ObservationRecord.build_cache_key(
                        reference_id=reference_id,
                        style_id=style_id,
                        code_point=cp,
                        browser_version=self.session.browser_version,
                        resolution=res,
                        subpixel_x=sub_x,
                        subpixel_y=sub_y,
                        config_hash=config_hash,
                    )

                    # Resume check: skip already completed observations
                    if self.store.has_observation(cache_key):
                        total_rasters += 1
                        continue

                    # Capture lossless raster from browser Canvas
                    png_bytes = await self.session.capture_lossless_raster(
                        font_family=font_family,
                        code_point=cp,
                        resolution_px=res,
                        subpixel_offset=(sub_x, sub_y),
                    )

                    png_sha256 = hashlib.sha256(png_bytes).hexdigest()
                    browser_hash = hashlib.sha256(self.session.browser_version.encode("utf-8")).hexdigest()
                    env_tag = f"{config_hash}_{browser_hash}"
                    rel_path = f"{reference_id}/{style_id}/{env_tag}/{cp:04X}/{res}px_{cache_key}.png"
                    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

                    record = ObservationRecord(
                        cache_key=cache_key,
                        reference_id=reference_id,
                        style_id=style_id,
                        code_point=cp,
                        resolution=res,
                        subpixel_x=sub_x,
                        subpixel_y=sub_y,
                        raster_relative_path=rel_path,
                        raster_sha256=png_sha256,
                        raster_size_bytes=len(png_bytes),
                        metrics=direct_metrics,
                        created_at=now_iso,
                        browser_version=self.session.browser_version,
                        config_hash=config_hash,
                    )

                    self.store.save_observation(record, png_bytes)
                    total_rasters += 1

            # 4. Multi-resolution disjoint held-out evaluation schedule captures (Evaluation Evidence)
            held_out_sizes = self.config.effective_held_out_sizes()
            for eval_res in held_out_sizes:
                for sub_x, sub_y in self.config.held_out_subpixel_phases:
                    cache_key = ObservationRecord.build_cache_key(
                        reference_id=reference_id,
                        style_id=style_id,
                        code_point=cp,
                        browser_version=self.session.browser_version,
                        resolution=eval_res,
                        subpixel_x=sub_x,
                        subpixel_y=sub_y,
                        config_hash=config_hash,
                    )

                    if self.store.has_observation(cache_key):
                        total_rasters += 1
                        continue

                    png_bytes = await self.session.capture_lossless_raster(
                        font_family=font_family,
                        code_point=cp,
                        resolution_px=eval_res,
                        subpixel_offset=(sub_x, sub_y),
                    )

                    png_sha256 = hashlib.sha256(png_bytes).hexdigest()
                    browser_hash = hashlib.sha256(self.session.browser_version.encode("utf-8")).hexdigest()
                    env_tag = f"{config_hash}_{browser_hash}"
                    rel_path = f"{reference_id}/{style_id}/{env_tag}/{cp:04X}/{eval_res}px_heldout_{cache_key}.png"
                    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

                    record = ObservationRecord(
                        cache_key=cache_key,
                        reference_id=reference_id,
                        style_id=style_id,
                        code_point=cp,
                        resolution=eval_res,
                        subpixel_x=sub_x,
                        subpixel_y=sub_y,
                        raster_relative_path=rel_path,
                        raster_sha256=png_sha256,
                        raster_size_bytes=len(png_bytes),
                        metrics=direct_metrics,
                        created_at=now_iso,
                        browser_version=self.session.browser_version,
                        config_hash=config_hash,
                    )

                    self.store.save_observation(record, png_bytes)
                    total_rasters += 1

            if progress_cb:
                progress_cb(idx, total_glyphs)

        elapsed = time.perf_counter() - start_time
        return (total_glyphs, total_rasters, elapsed)

    async def collect_pair_observations(
        self,
        reference_id: str,
        style_id: str,
        font_family: str | None = None,
        pairs: list[tuple[int, int]] | None = None,
        pair_candidates: list[tuple[int, int]] | None = None,
    ) -> int:
        """Measure explicit pairwise spacing and compute inferred kerning for candidate pairs."""
        from typography.models import BOUNDED_FIT_PAIRS
        bv = self.session.browser_version
        cfg_h = self.config.compute_hash()

        if pairs is None and pair_candidates is None:
            coverage = self.store.get_coverage(reference_id, style_id, browser_version=bv, config_hash=cfg_h)
            if coverage:
                cov_set = set(coverage)
                target_pairs = [p for p in BOUNDED_FIT_PAIRS if p[0] in cov_set and p[1] in cov_set]
            else:
                target_pairs = list(BOUNDED_FIT_PAIRS)
        else:
            target_pairs = list(pairs or pair_candidates or [])

        target_family = font_family or reference_id
        captured = 0
        # Adaptive multi-size kerning: raw per-size evidence across the exact
        # metric schedule; the derived kerning is the deterministic median of
        # per-size inferences and must recompute from these raw rows.
        pair_sizes = tuple(float(s) for s in self.config.metric_sizes_px)
        if not pair_sizes:
            pair_sizes = (float(self.config.font_size_px),)

        for left_cp, right_cp in target_pairs:
            text = chr(left_cp) + chr(right_cp)
            evidence: list[dict[str, float]] = []
            for size in pair_sizes:
                pair_adv = await self.session.measure_text_advance(
                    target_family,
                    text,
                    font_size_px=size,
                    upem=self.config.upem,
                )
                m_left = await self.session.measure_glyph_direct(
                    target_family, left_cp, font_size_px=size, upem=self.config.upem,
                )
                m_right = await self.session.measure_glyph_direct(
                    target_family, right_cp, font_size_px=size, upem=self.config.upem,
                )
                l_adv = m_left.advance_width_upem
                r_adv = m_right.advance_width_upem
                inferred = int(round(pair_adv - (l_adv + r_adv)))
                evidence.append(
                    {
                        "font_size_px": size,
                        "left_advance_upem": l_adv,
                        "right_advance_upem": r_adv,
                        "pair_advance_upem": pair_adv,
                        "inferred_kerning_upem": inferred,
                    }
                )
                self.store.save_pair_size_observation(
                    reference_id=reference_id,
                    style_id=style_id,
                    left_cp=left_cp,
                    right_cp=right_cp,
                    font_size_px=size,
                    left_advance_upem=l_adv,
                    right_advance_upem=r_adv,
                    pair_advance_upem=pair_adv,
                    inferred_kerning_upem=inferred,
                    browser_version=bv,
                    config_hash=cfg_h,
                    provenance=f"chromium:{bv}:canvas_text_metrics",
                )

            derived = derive_multisize_kerning(evidence)
            anchor = next(
                (e for e in evidence if abs(e["font_size_px"] - float(self.config.font_size_px)) < 1e-9),
                evidence[0],
            )

            self.store.save_pair_observation(
                reference_id=reference_id,
                style_id=style_id,
                left_cp=left_cp,
                right_cp=right_cp,
                left_char=chr(left_cp),
                right_char=chr(right_cp),
                left_advance_upem=round(anchor["left_advance_upem"], 2),
                right_advance_upem=round(anchor["right_advance_upem"], 2),
                pair_advance_upem=round(anchor["pair_advance_upem"], 2),
                inferred_kerning_upem=derived,
                confidence=1.0,
                provenance=f"chromium:{bv}:canvas_text_metrics",
                browser_version=bv,
                config_hash=cfg_h,
            )
            captured += 1
        return captured

    async def collect_feature_observations(
        self,
        reference_id: str,
        style_id: str,
        font_family: str | None = None,
    ) -> int:
        """Probe OpenType feature effects in browser and persist feature observations with exact provenance."""
        target_family = font_family or reference_id
        bv = self.session.browser_version
        cfg_h = self.config.compute_hash()
        captured = 0

        for feature_tag, sample_text in self.config.feature_probes:
            probe_data = await self.session.probe_opentype_feature(
                target_family,
                feature_tag,
                sample_text,
                font_size_px=self.config.font_size_px,
                upem=self.config.upem,
            )
            # Detect effect: advance shift or raster difference
            adv_diff = abs(probe_data["enabled_advance_upem"] - probe_data["disabled_advance_upem"]) > 0.01
            rast_diff = probe_data["enabled_raster_signature"] != probe_data["disabled_raster_signature"]
            effect_observed = adv_diff or rast_diff

            obs = OpenTypeFeatureObservation(
                reference_id=reference_id,
                style_id=style_id,
                browser_version=bv,
                config_hash=cfg_h,
                feature_tag=feature_tag,
                sample_text=sample_text,
                enabled_advance_upem=probe_data["enabled_advance_upem"],
                disabled_advance_upem=probe_data["disabled_advance_upem"],
                enabled_raster_signature=probe_data["enabled_raster_signature"],
                disabled_raster_signature=probe_data["disabled_raster_signature"],
                effect_observed=effect_observed,
                provenance=f"chromium:{bv}:canvas_feature_probe",
                created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            )
            self.store.save_feature_observation(obs)
            captured += 1
        return captured

    def finalize_source_collection(
        self,
        reference_id: str,
        style_id: str,
        source_url: str = "direct_browser",
        expected_pairs: list[tuple[int, int]] | tuple[tuple[int, int], ...] | None = None,
        provider_capability: Any = None,
    ) -> None:
        """Mark source collection as fully finalized only after all glyph, pair, feature, and coverage checks pass for exact identity.

        Direct browser collections finalize against the config's phase
        schedule (provider_capability=None). Provider-capability-bound
        collections (e.g. Monotype CDN) finalize against the closed
        descriptor's size schedule at its fixed phase; the descriptor is
        sealed into the completion record as part of the identity.
        """
        if provider_capability is not None:
            provider_capability.validate()
        bv = self.session.browser_version
        cfg_h = self.config.compute_hash()
        expected_pair_prov = f"chromium:{bv}:canvas_text_metrics"
        expected_feat_prov = f"chromium:{bv}:canvas_feature_probe"

        # 1. Coverage verification (exact identity)
        coverage = self.store.get_coverage(reference_id, style_id, browser_version=bv, config_hash=cfg_h)
        if not coverage:
            raise ValueError(f"FINALIZATION_FAILED: no glyph coverage found for {reference_id}:{style_id} under exact identity ({bv}, {cfg_h})")

        # 2. Scoped Glyph observation verification (exact identity & derived equality)
        observed_cps = self.store.get_glyph_observation_code_points(
            reference_id, style_id, browser_version=bv, config_hash=cfg_h
        )
        if set(observed_cps) != set(coverage):
            missing_cov = set(coverage) - set(observed_cps)
            extra_obs = set(observed_cps) - set(coverage)
            raise ValueError(
                f"FINALIZATION_FAILED: declared coverage does not match observed glyphs for {reference_id}:{style_id} "
                f"under exact identity ({bv}, {cfg_h}): missing_in_observations={missing_cov}, extra_observed={extra_obs}"
            )

        expected_obs_keys: set[str] = set()
        held_out_sizes = self.config.effective_held_out_sizes()

        for cp in coverage:
            obs = self.store.get_glyph_observations(
                reference_id, style_id, cp, browser_version=bv, config_hash=cfg_h
            )
            if not obs:
                raise ValueError(
                    f"FINALIZATION_FAILED: missing glyph observations for code point {cp} under exact identity ({bv}, {cfg_h}) in {reference_id}:{style_id}"
                )
            dm = obs[0][0].metrics
            if provider_capability is not None:
                # Closed capability schedule: every observable render size at
                # the provider's fixed phase, nothing else.
                schedule_tuples = [
                    (size, provider_capability.phase[0], provider_capability.phase[1])
                    for size in provider_capability.all_sizes()
                ]
            else:
                # Fit schedule keys
                schedule_tuples = []
                phases = self.config.get_phases_for_metrics(dm)
                for res in self.config.resolutions:
                    for sub_x, sub_y in phases:
                        schedule_tuples.append((res, sub_x, sub_y))
                # Held-out schedule keys: the exact canonical held-out size
                # schedule (single source), never a derived single size.
                for held_res in held_out_sizes:
                    for sub_x, sub_y in self.config.held_out_subpixel_phases:
                        schedule_tuples.append((held_res, sub_x, sub_y))
            for res, sub_x, sub_y in schedule_tuples:
                k = ObservationRecord.build_cache_key(
                    reference_id=reference_id,
                    style_id=style_id,
                    code_point=cp,
                    browser_version=bv,
                    resolution=res,
                    subpixel_x=sub_x,
                    subpixel_y=sub_y,
                    config_hash=cfg_h,
                )
                if not self.store.has_observation(k):
                    raise ValueError(
                        f"FINALIZATION_FAILED: missing or invalid observation {k} for U+{cp:04X} at {res}px phase ({sub_x}, {sub_y}) under ({bv}, {cfg_h})"
                    )
                expected_obs_keys.add(k)

        with self.store._get_connection() as conn:
            stored_rows = conn.execute(
                "SELECT cache_key FROM observations WHERE reference_id = ? AND style_id = ? AND browser_version = ? AND config_hash = ?",
                (reference_id, style_id, bv, cfg_h),
            ).fetchall()
            stored_obs_keys = {r["cache_key"] for r in stored_rows}

        if stored_obs_keys != expected_obs_keys:
            missing_keys = expected_obs_keys - stored_obs_keys
            extra_keys = stored_obs_keys - expected_obs_keys
            raise ValueError(
                f"FINALIZATION_FAILED: observation keys mismatch for {reference_id}:{style_id} under ({bv}, {cfg_h}): "
                f"missing={missing_keys}, extra={extra_keys}"
            )

        # 3. Scoped Pair observation verification (exact identity & derived set equality)
        if expected_pairs is None:
            from typography.models import BOUNDED_FIT_PAIRS
            cov_set = set(coverage)
            target_pairs = [p for p in BOUNDED_FIT_PAIRS if p[0] in cov_set and p[1] in cov_set]
        else:
            target_pairs = list(expected_pairs)

        # Closed raw per-size pair schedule (adaptive multi-size kerning).
        # Every declared pair must have raw per-size evidence across the exact
        # declared metric schedule under the sealed exact collection identity.
        # Absence is fail-closed; caller-authored aggregates cannot substitute.
        expected_pair_sizes = tuple(float(s) for s in self.config.metric_sizes_px)

        stored_pairs = self.store.get_pair_observations(
            reference_id=reference_id,
            style_id=style_id,
            browser_version=bv,
            config_hash=cfg_h,
        )
        stored_pair_set = {(p["left_cp"], p["right_cp"]) for p in stored_pairs}
        expected_pair_set = set(target_pairs)
        if stored_pair_set != expected_pair_set:
            missing_pairs = expected_pair_set - stored_pair_set
            extra_pairs = stored_pair_set - expected_pair_set
            raise ValueError(
                f"FINALIZATION_FAILED: pair observations mismatch for {reference_id}:{style_id}: missing={missing_pairs}, extra={extra_pairs}"
            )
        for p in stored_pairs:
            if p.get("provenance") != expected_pair_prov:
                raise ValueError(
                    f"FINALIZATION_FAILED: untrusted or mismatched pair provenance '{p.get('provenance')}' != '{expected_pair_prov}' for {reference_id}:{style_id}"
                )
            # Raw per-size pair evidence is REQUIRED for every declared pair
            # under the exact collection identity. The stored derived kerning
            # must recompute exactly from that raw evidence; absence or
            # mismatched sizes fail closed (caller-authored aggregates and
            # missing evidence never substitute for the raw rows).
            size_rows = self.store.get_pair_size_observations(
                reference_id, style_id,
                int(p["left_cp"]), int(p["right_cp"]),
                browser_version=bv, config_hash=cfg_h,
            )
            if not size_rows:
                raise ValueError(
                    f"FINALIZATION_FAILED: missing raw per-size pair evidence "
                    f"for ({p['left_cp']},{p['right_cp']}) under ({bv}, {cfg_h}) "
                    f"in {reference_id}:{style_id}"
                )
            try:
                validate_pair_size_schedule(size_rows, expected_pair_sizes)
            except ValueError as exc:
                raise ValueError(
                    f"FINALIZATION_FAILED: {exc} for ({p['left_cp']},{p['right_cp']}) "
                    f"in {reference_id}:{style_id}"
                ) from exc
            recomputed = derive_multisize_kerning(
                [
                    {
                        "font_size_px": float(r["font_size_px"]),
                        "left_advance_upem": float(r["left_advance_upem"]),
                        "right_advance_upem": float(r["right_advance_upem"]),
                        "pair_advance_upem": float(r["pair_advance_upem"]),
                        "inferred_kerning_upem": int(r["inferred_kerning_upem"]),
                    }
                    for r in size_rows
                ]
            )
            if int(p.get("inferred_kerning_upem", 0)) != recomputed:
                raise ValueError(
                    f"FINALIZATION_FAILED: pair ({p['left_cp']},{p['right_cp']}) derived kerning "
                    f"{p.get('inferred_kerning_upem')} does not recompute from raw multi-size "
                    f"evidence ({recomputed}) for {reference_id}:{style_id}"
                )

        # 3b. Closed raw per-size METRIC evidence identity: for every declared
        # coverage code point, the stored metric_observations must equal the
        # exact declared metric schedule under the sealed collection identity.
        # Absence/extra/duplicate/cross-env rows reject (no caller-authored
        # aggregate may substitute for the sealed raw evidence).
        expected_metric_sizes = tuple(float(s) for s in self.config.metric_sizes_px)
        for cp in coverage:
            metric_rows = self.store.get_metric_observations(
                reference_id, style_id,
                browser_version=bv, config_hash=cfg_h,
                code_point=cp,
            )
            if not metric_rows:
                raise ValueError(
                    f"FINALIZATION_FAILED: missing raw per-size metric evidence "
                    f"for U+{cp:04X} under ({bv}, {cfg_h}) in {reference_id}:{style_id}"
                )
            try:
                validate_metric_size_schedule(metric_rows, expected_metric_sizes)
            except ValueError as exc:
                raise ValueError(
                    f"FINALIZATION_FAILED: {exc} for U+{cp:04X} in {reference_id}:{style_id}"
                ) from exc

        # 4. Scoped OpenType feature probe observation verification (exact (tag, sample_text) set equality)
        features = self.store.get_feature_observations(
            reference_id=reference_id,
            style_id=style_id,
            browser_version=bv,
            config_hash=cfg_h,
        )
        expected_feature_probes = set(self.config.feature_probes)
        stored_feature_probes = {(f["feature_tag"], f["sample_text"]) for f in features}
        if stored_feature_probes != expected_feature_probes:
            missing_features = expected_feature_probes - stored_feature_probes
            extra_features = stored_feature_probes - expected_feature_probes
            raise ValueError(
                f"FINALIZATION_FAILED: feature probe observations mismatch for {reference_id}:{style_id}: missing={missing_features}, extra={extra_features}"
            )
        for f in features:
            if f.get("provenance") != expected_feat_prov:
                raise ValueError(
                    f"FINALIZATION_FAILED: untrusted or mismatched feature provenance '{f.get('provenance')}' != '{expected_feat_prov}' for {reference_id}:{style_id}"
                )

        # 5. Record single canonical completion record (capability sealed).
        capability_json = provider_capability.to_json() if provider_capability is not None else ""
        capability_hash = provider_capability.compute_hash() if provider_capability is not None else ""
        self.store.record_source_collection_completed(
            reference_id=reference_id,
            style_id=style_id,
            config_hash=cfg_h,
            browser_version=bv,
            source_url=source_url,
            capability_json=capability_json,
            capability_hash=capability_hash,
        )
