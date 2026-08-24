"""Fail-closed fidelity evaluator and multi-gate verification engine (Stage 9A)."""
from __future__ import annotations

import datetime
import hashlib
import io
import math
from typing import Callable, Sequence

import numpy as np
from PIL import Image, ImageDraw
import scipy.ndimage

from fidelity.models import (
    ConsumerEvidenceBundle,
    ConsumerGateResult,
    CoverageGateResult,
    FidelityReport,
    FidelityThresholds,
    GeometryRasterGateResult,
    MetricsGateResult,
    TopologyGateResult,
    TypographyGateResult,
)
from measurement.calibration import CalibrationTransform, ObservationCalibrator
from measurement.models import ObservationConfig, ObservationRecord
from reconstruction.font_model import CanonicalFontModel
from typography.models import PairKerningObservation


class FidelityEvaluator:
    """Evaluates CanonicalFontModel against fit vs held-out evidence with strict separation gates."""

    @staticmethod
    def _compute_records_fingerprint(records: Sequence[ObservationRecord]) -> str:
        if not records:
            return "empty"
        sorted_keys = sorted(r.cache_key for r in records)
        return hashlib.sha256(":".join(sorted_keys).encode("utf-8")).hexdigest()

    @staticmethod
    def _rasterize_glyph_contours(
        glyph,
        transform: CalibrationTransform,
        resolution: int,
    ) -> np.ndarray:
        """Render glyph contours to binary pixel mask at given resolution using calibration transform."""
        img = Image.new("L", (resolution, resolution), 0)
        draw = ImageDraw.Draw(img)

        outer_contours = [c for c in glyph.contours if not c.is_hole]
        hole_contours = [c for c in glyph.contours if c.is_hole]

        for c in outer_contours:
            pts = c.sample_points(samples_per_segment=16)
            if len(pts) < 3:
                continue
            poly_px = [transform.inverse(p.x, p.y) for p in pts]
            draw.polygon(poly_px, fill=255)

        for c in hole_contours:
            pts = c.sample_points(samples_per_segment=16)
            if len(pts) < 3:
                continue
            poly_px = [transform.inverse(p.x, p.y) for p in pts]
            draw.polygon(poly_px, fill=0)

        arr = np.array(img, dtype=np.uint8)
        return (arr > 127).astype(np.uint8)

    @classmethod
    def _compute_chamfer_distance_upem(
        cls,
        glyph,
        transform: CalibrationTransform,
        ref_mask: np.ndarray,
        resolution: int,
    ) -> float:
        """Compute Euclidean chamfer distance in font design space (UPEM units) between model contour and raster."""
        if not np.any(ref_mask):
            return 1000.0

        dt_ref = scipy.ndimage.distance_transform_edt(1 - ref_mask)
        sample_distances_upem: list[float] = []
        scale = max(transform.scale, 1e-6)

        for c in glyph.contours:
            pts = c.sample_points(samples_per_segment=12)
            for p in pts:
                px_x, px_y = transform.inverse(p.x, p.y)
                ix = int(round(px_x))
                iy = int(round(px_y))
                if 0 <= ix < resolution and 0 <= iy < resolution:
                    d_px = float(dt_ref[iy, ix])
                    sample_distances_upem.append(d_px / scale)
                else:
                    sample_distances_upem.append(100.0)

        return float(np.mean(sample_distances_upem)) if sample_distances_upem else 0.0

    @classmethod
    def evaluate(
        cls,
        model: CanonicalFontModel,
        config: ObservationConfig,
        fit_records: Sequence[ObservationRecord],
        held_out_records: Sequence[ObservationRecord],
        fit_pairs: Sequence[PairKerningObservation] = (),
        held_out_pairs: Sequence[PairKerningObservation] = (),
        consumer_bundle: ConsumerEvidenceBundle | None = None,
        thresholds: FidelityThresholds | None = None,
        raster_provider: Callable[[ObservationRecord], bytes] | None = None,
    ) -> FidelityReport:
        """Execute full fail-closed multi-gate evaluation and generate immutable FidelityReport."""
        if thresholds is None:
            thresholds = FidelityThresholds()
        thresholds.validate()

        failure_reasons: list[str] = []

        # Validate CanonicalFontModel strictly
        try:
            model.validate()
            model_hash = model.compute_canonical_hash()
        except Exception as e:
            failure_reasons.append(f"MODEL_VALIDATION_ERROR: {e}")
            model_hash = "invalid_model_hash"

        config_hash = config.compute_hash()
        fit_fp = cls._compute_records_fingerprint(fit_records)
        held_out_fp = cls._compute_records_fingerprint(held_out_records)

        # ==========================================
        # GATE 0: FIT / HELD-OUT PROVENANCE, LEAKAGE & MODEL BINDING
        # ==========================================
        if model.config_hash != config_hash:
            failure_reasons.append(
                f"CONFIG_HASH_MISMATCH: Model config hash ({model.config_hash}) does not match active config hash ({config_hash})"
            )

        if model.fit_observations_count != len(fit_records):
            failure_reasons.append(
                f"FIT_OBSERVATIONS_COUNT_MISMATCH: Model declared {model.fit_observations_count} != actual fit records {len(fit_records)}"
            )

        fit_keys = {r.cache_key for r in fit_records}
        held_out_keys = {r.cache_key for r in held_out_records}
        key_overlap = fit_keys & held_out_keys
        if key_overlap:
            failure_reasons.append(
                f"LEAKAGE_DETECTED: Fit and held-out observation sets share {len(key_overlap)} cache keys"
            )

        fit_raster_shas = {r.raster_sha256 for r in fit_records if r.raster_sha256}
        held_out_raster_shas = {r.raster_sha256 for r in held_out_records if r.raster_sha256}
        sha_overlap = fit_raster_shas & held_out_raster_shas
        if sha_overlap:
            failure_reasons.append(
                f"LEAKAGE_DETECTED: Fit and held-out sets share {len(sha_overlap)} raster SHA256 digests"
            )

        fit_pair_keys = {(p.left_cp, p.right_cp) for p in fit_pairs}
        held_out_pair_keys = {(p.left_cp, p.right_cp) for p in held_out_pairs}
        pair_overlap = fit_pair_keys & held_out_pair_keys
        if pair_overlap:
            failure_reasons.append(
                f"LEAKAGE_DETECTED: Fit and held-out typography pairs share {len(pair_overlap)} pairs"
            )

        # Check drift across records
        for r in list(fit_records) + list(held_out_records):
            if r.reference_id != model.reference_id:
                failure_reasons.append(f"REFERENCE_ID_DRIFT: Record reference_id '{r.reference_id}' != model '{model.reference_id}'")
            if r.style_id != model.style_id:
                failure_reasons.append(f"STYLE_ID_DRIFT: Record style_id '{r.style_id}' != model '{model.style_id}'")
            if r.browser_version != model.browser_version:
                failure_reasons.append(f"BROWSER_VERSION_DRIFT: Record browser '{r.browser_version}' != model '{model.browser_version}'")
            if r.config_hash != config_hash:
                failure_reasons.append(f"CONFIG_HASH_DRIFT: Record config_hash '{r.config_hash}' != '{config_hash}'")
            if not r.validate_cache_key():
                failure_reasons.append(f"INVALID_CACHE_KEY: Cache key mismatch for record: {r.cache_key}")

        # Strict Model-to-Fit Evidence Binding
        for cp, glyph in model.glyphs.items():
            glyph_fit_records = [r for r in fit_records if r.code_point == cp]
            if not glyph_fit_records:
                failure_reasons.append(f"MISSING_FIT_EVIDENCE: Glyph {cp} in model has no corresponding fit observation records")
                continue
            expected_glyph_fps = tuple(sorted(r.raster_sha256 for r in glyph_fit_records))
            if glyph.observation_fingerprints != expected_glyph_fps:
                failure_reasons.append(
                    f"GLYPH_FINGERPRINT_MISMATCH: Glyph {cp} fingerprints ({glyph.observation_fingerprints}) != expected fit rasters ({expected_glyph_fps})"
                )

        if fit_records:
            try:
                expected_calib_fp = ObservationCalibrator.compute_calibration_fingerprint(
                    fit_records, config=config, units_per_em=model.metrics.units_per_em, min_confidence=thresholds.min_confidence
                )
                if model.calibration_fingerprint != expected_calib_fp:
                    failure_reasons.append(
                        f"CALIBRATION_FINGERPRINT_MISMATCH: Model calibration fingerprint ({model.calibration_fingerprint}) != recomputed ({expected_calib_fp})"
                    )
            except Exception as e:
                failure_reasons.append(f"CALIBRATION_RECOMPUTATION_ERROR: {e}")

        # Typography pair validation
        expected_pair_prov = f"chromium:{model.browser_version}:canvas_text_metrics"
        for p in list(fit_pairs) + list(held_out_pairs):
            if p.provenance != expected_pair_prov:
                failure_reasons.append(
                    f"UNTRUSTED_TYPOGRAPHY: Pair ({p.left_cp}, {p.right_cp}) provenance '{p.provenance}' does not match expected production provenance '{expected_pair_prov}'"
                )
            if p.left_char != chr(p.left_cp) or p.right_char != chr(p.right_cp):
                failure_reasons.append(f"INVALID_TYPOGRAPHY_PAIR: Character/codepoint mismatch ({p.left_cp}:{p.left_char}, {p.right_cp}:{p.right_char})")
            if not (math.isfinite(p.left_advance_upem) and math.isfinite(p.right_advance_upem) and math.isfinite(p.measured_pair_advance_upem) and math.isfinite(p.inferred_kerning_upem)):
                failure_reasons.append(f"NON_FINITE_TYPOGRAPHY_METRIC in pair ({p.left_cp}, {p.right_cp})")
            if p.confidence < thresholds.min_confidence:
                failure_reasons.append(f"LOW_CONFIDENCE_TYPOGRAPHY in pair ({p.left_cp}, {p.right_cp}): {p.confidence} < {thresholds.min_confidence}")

        # ==========================================
        # GATE 1: COVERAGE GATE
        # ==========================================
        total_model_glyphs = len(model.glyphs)
        all_observed_cps = {r.code_point for r in fit_records} | {r.code_point for r in held_out_records}
        if not all_observed_cps:
            all_observed_cps = set(model.glyphs.keys())

        missing_cps = sorted(list(all_observed_cps - set(model.glyphs.keys())))
        coverage_rate = (
            (len(all_observed_cps) - len(missing_cps)) / max(len(all_observed_cps), 1)
            if all_observed_cps
            else 0.0
        )

        cov_status = "PASS"
        if total_model_glyphs < thresholds.min_unicode_coverage_count:
            cov_status = "FAIL"
            failure_reasons.append(
                f"COVERAGE_GATE_FAIL: Total glyphs ({total_model_glyphs}) below min threshold ({thresholds.min_unicode_coverage_count})"
            )
        if coverage_rate < thresholds.min_core_coverage_rate:
            cov_status = "FAIL"
            failure_reasons.append(
                f"COVERAGE_GATE_FAIL: Coverage rate ({coverage_rate:.4f}) below required ({thresholds.min_core_coverage_rate})"
            )

        coverage_gate = CoverageGateResult(
            status=cov_status,
            total_glyphs=total_model_glyphs,
            required_core_glyphs=len(all_observed_cps),
            missing_core_glyphs=tuple(missing_cps),
            coverage_rate=round(coverage_rate, 4),
            details={"model_glyphs_count": total_model_glyphs},
        )

        # ==========================================
        # GATE 2: TOPOLOGY GATE
        # ==========================================
        total_contours = 0
        unclosed_count = 0
        degenerate_seg_count = 0

        for cp, glyph in model.glyphs.items():
            total_contours += len(glyph.contours)
            for c in glyph.contours:
                if not c.is_closed:
                    unclosed_count += 1
                for s in c.segments:
                    if s.approximate_length() < 1e-4:
                        degenerate_seg_count += 1

        top_pass_rate = 1.0 if total_contours == 0 else max(0.0, 1.0 - (unclosed_count + degenerate_seg_count) / max(total_contours, 1))
        top_status = "PASS"
        if unclosed_count > 0:
            top_status = "FAIL"
            failure_reasons.append(f"TOPOLOGY_GATE_FAIL: {unclosed_count} unclosed contours detected")
        if degenerate_seg_count > 0:
            top_status = "FAIL"
            failure_reasons.append(f"TOPOLOGY_GATE_FAIL: {degenerate_seg_count} degenerate segments detected")

        topology_gate = TopologyGateResult(
            status=top_status,
            total_contours=total_contours,
            unclosed_contours_count=unclosed_count,
            degenerate_segments_count=degenerate_seg_count,
            topology_pass_rate=round(top_pass_rate, 4),
            details={"total_glyphs_checked": len(model.glyphs)},
        )

        # ==========================================
        # GATE 3: GEOMETRY & RASTER GATE (FAIL-CLOSED)
        # ==========================================
        ious: list[float] = []
        chamfers: list[float] = []

        if not held_out_records:
            geom_status = "FAIL"
            failure_reasons.append("GEOMETRY_RASTER_GATE_FAIL: ZERO_HELD_OUT_SAMPLES: No held-out observation records provided")
        elif raster_provider is None:
            geom_status = "FAIL"
            failure_reasons.append("GEOMETRY_RASTER_GATE_FAIL: MISSING_RASTER_PROVIDER: Independently supplied held-out raster provider is required")
        else:
            for r in held_out_records:
                if r.code_point not in model.glyphs:
                    failure_reasons.append(f"GEOMETRY_RASTER_GATE_FAIL: Missing glyph {r.code_point} in model")
                    continue

                glyph = model.glyphs[r.code_point]
                raw_bytes = raster_provider(r)
                if not isinstance(raw_bytes, bytes) or len(raw_bytes) == 0:
                    failure_reasons.append(f"GEOMETRY_RASTER_GATE_FAIL: Empty raster bytes supplied for held-out record {r.cache_key}")
                    continue

                if len(raw_bytes) != r.raster_size_bytes:
                    failure_reasons.append(
                        f"GEOMETRY_RASTER_GATE_FAIL: Raster byte size mismatch for {r.cache_key}: {len(raw_bytes)} != {r.raster_size_bytes}"
                    )
                    continue

                actual_sha = hashlib.sha256(raw_bytes).hexdigest()
                if actual_sha != r.raster_sha256:
                    failure_reasons.append(
                        f"GEOMETRY_RASTER_GATE_FAIL: CORRUPT_RASTER_EVIDENCE: SHA256 mismatch for held-out record {r.cache_key}: {actual_sha} != {r.raster_sha256}"
                    )
                    continue

                try:
                    img = Image.open(io.BytesIO(raw_bytes)).convert("L")
                    if img.size != (r.resolution, r.resolution):
                        failure_reasons.append(
                            f"GEOMETRY_RASTER_GATE_FAIL: Raster image size mismatch: {img.size} != ({r.resolution}, {r.resolution})"
                        )
                        continue
                    arr = np.array(img, dtype=np.float32) / 255.0
                    ref_mask = ((1.0 - arr) >= 0.5).astype(np.uint8)
                except Exception as e:
                    failure_reasons.append(f"GEOMETRY_RASTER_GATE_FAIL: Raster decode error: {e}")
                    continue

                transform = CalibrationTransform.from_observation(
                    resolution=r.resolution,
                    metrics=r.metrics,
                    subpixel_x=r.subpixel_x,
                    subpixel_y=r.subpixel_y,
                    units_per_em=model.metrics.units_per_em,
                )

                model_mask = cls._rasterize_glyph_contours(glyph, transform, r.resolution)
                intersection = int(np.logical_and(model_mask, ref_mask).sum())
                union = int(np.logical_or(model_mask, ref_mask).sum())
                iou = float(intersection) / max(union, 1)
                ious.append(iou)

                chamfer = cls._compute_chamfer_distance_upem(glyph, transform, ref_mask, r.resolution)
                chamfers.append(chamfer)

            mean_iou = float(np.mean(ious)) if ious else 0.0
            min_iou = float(np.min(ious)) if ious else 0.0
            mean_chamfer = float(np.mean(chamfers)) if chamfers else 1000.0
            max_chamfer = float(np.max(chamfers)) if chamfers else 1000.0

            geom_status = "PASS" if ious and not any("GEOMETRY_RASTER_GATE_FAIL" in r for r in failure_reasons) else "FAIL"
            if len(ious) == 0:
                geom_status = "FAIL"
                failure_reasons.append("GEOMETRY_RASTER_GATE_FAIL: ZERO_HELD_OUT_SAMPLES: Zero raster samples evaluated")
            elif mean_iou < thresholds.min_raster_iou:
                geom_status = "FAIL"
                failure_reasons.append(
                    f"GEOMETRY_RASTER_GATE_FAIL: Mean IoU ({mean_iou:.4f}) below threshold ({thresholds.min_raster_iou})"
                )
            elif max_chamfer > thresholds.max_chamfer_distance_upem:
                geom_status = "FAIL"
                failure_reasons.append(
                    f"GEOMETRY_RASTER_GATE_FAIL: Max chamfer distance ({max_chamfer:.2f} UPEM) exceeds threshold ({thresholds.max_chamfer_distance_upem})"
                )

        geometry_raster_gate = GeometryRasterGateResult(
            status=geom_status,
            mean_iou=round(float(np.mean(ious)), 4) if ious else 0.0,
            min_iou=round(float(np.min(ious)), 4) if ious else 0.0,
            mean_chamfer_upem=round(float(np.mean(chamfers)), 2) if chamfers else 0.0,
            max_chamfer_upem=round(float(np.max(chamfers)), 2) if chamfers else 0.0,
            evaluated_glyphs_count=len(ious),
            details={"samples_count": len(ious)},
        )

        # ==========================================
        # GATE 4: METRICS & CONFIDENCE GATE (FAIL-CLOSED)
        # ==========================================
        adv_deltas: list[float] = []
        lsb_deltas: list[float] = []
        rsb_deltas: list[float] = []
        ascent_deltas: list[float] = []
        descent_deltas: list[float] = []
        all_metric_deltas: list[float] = []
        low_confidence_count = 0

        for cp, glyph in model.glyphs.items():
            if glyph.confidence < thresholds.min_confidence:
                low_confidence_count += 1

        if low_confidence_count > 0:
            failure_reasons.append(
                f"METRICS_GATE_FAIL: {low_confidence_count} glyphs have confidence below threshold ({thresholds.min_confidence})"
            )

        if not held_out_records:
            metrics_status = "FAIL"
            failure_reasons.append("METRICS_GATE_FAIL: ZERO_HELD_OUT_METRICS: No held-out metric samples provided")
        else:
            for r in held_out_records:
                if r.code_point not in model.glyphs:
                    failure_reasons.append(f"METRICS_GATE_FAIL: Held-out metric for missing glyph {r.code_point}")
                    continue
                glyph = model.glyphs[r.code_point]
                d_adv = abs(glyph.advance_width_upem - r.metrics.advance_width_upem)
                d_lsb = abs(glyph.lsb_upem - r.metrics.lsb_upem)
                d_rsb = abs(glyph.rsb_upem - r.metrics.rsb_upem)
                d_asc = abs(glyph.ascent_upem - r.metrics.ascent_upem)
                d_desc = abs(glyph.descent_upem - r.metrics.descent_upem)

                adv_deltas.append(d_adv)
                lsb_deltas.append(d_lsb)
                rsb_deltas.append(d_rsb)
                ascent_deltas.append(d_asc)
                descent_deltas.append(d_desc)
                all_metric_deltas.extend([d_adv, d_lsb, d_rsb, d_asc, d_desc])

            mean_adv_delta = float(np.mean(adv_deltas)) if adv_deltas else 0.0
            max_adv_delta = float(np.max(adv_deltas)) if adv_deltas else 0.0
            rms_adv_delta = float(np.sqrt(np.mean(np.square(adv_deltas)))) if adv_deltas else 0.0
            overall_rms = float(np.sqrt(np.mean(np.square(all_metric_deltas)))) if all_metric_deltas else 0.0

            metrics_status = "PASS"
            if len(adv_deltas) == 0:
                metrics_status = "FAIL"
                failure_reasons.append("METRICS_GATE_FAIL: ZERO_HELD_OUT_METRICS: Zero metric samples evaluated")
            elif max_adv_delta > thresholds.max_advance_width_delta_upem:
                metrics_status = "FAIL"
                failure_reasons.append(
                    f"METRICS_GATE_FAIL: Max advance delta ({max_adv_delta:.2f} UPEM) exceeds threshold ({thresholds.max_advance_width_delta_upem})"
                )
            elif overall_rms > thresholds.max_metric_rms_upem:
                metrics_status = "FAIL"
                failure_reasons.append(
                    f"METRICS_GATE_FAIL: Overall metrics RMS delta ({overall_rms:.2f} UPEM) exceeds threshold ({thresholds.max_metric_rms_upem})"
                )

        if low_confidence_count > 0:
            metrics_status = "FAIL"

        metrics_gate = MetricsGateResult(
            status=metrics_status,
            advance_width_mean_delta_upem=round(float(np.mean(adv_deltas)), 2) if adv_deltas else 0.0,
            advance_width_max_delta_upem=round(float(np.max(adv_deltas)), 2) if adv_deltas else 0.0,
            advance_width_rms_delta_upem=round(float(np.sqrt(np.mean(np.square(adv_deltas)))), 2) if adv_deltas else 0.0,
            lsb_max_delta_upem=round(float(np.max(lsb_deltas)), 2) if lsb_deltas else 0.0,
            rsb_max_delta_upem=round(float(np.max(rsb_deltas)), 2) if rsb_deltas else 0.0,
            ascent_max_delta_upem=round(float(np.max(ascent_deltas)), 2) if ascent_deltas else 0.0,
            descent_max_delta_upem=round(float(np.max(descent_deltas)), 2) if descent_deltas else 0.0,
            overall_metrics_rms_upem=round(overall_rms, 2) if all_metric_deltas else 0.0,
            evaluated_metrics_count=len(adv_deltas),
            details={"total_held_out_metrics": len(adv_deltas), "low_confidence_count": low_confidence_count},
        )

        # ==========================================
        # GATE 5: TYPOGRAPHY / KERNING GATE (FAIL-CLOSED)
        # ==========================================
        kerning_deltas: list[float] = []
        if not held_out_pairs:
            typo_status = "FAIL"
            failure_reasons.append("TYPOGRAPHY_GATE_FAIL: ZERO_HELD_OUT_TYPOGRAPHY: No held-out typography pairs provided")
        else:
            for p in held_out_pairs:
                pair_key = (p.left_cp, p.right_cp)
                model_kern = model.kerning_pairs.get(pair_key, 0)
                delta = abs(model_kern - p.inferred_kerning_upem)
                kerning_deltas.append(float(delta))

            max_kern_delta = float(np.max(kerning_deltas)) if kerning_deltas else 0.0
            mean_kern_delta = float(np.mean(kerning_deltas)) if kerning_deltas else 0.0

            typo_status = "PASS"
            if len(kerning_deltas) == 0:
                typo_status = "FAIL"
                failure_reasons.append("TYPOGRAPHY_GATE_FAIL: ZERO_HELD_OUT_TYPOGRAPHY: Zero pair samples evaluated")
            elif max_kern_delta > thresholds.max_kerning_delta_upem:
                typo_status = "FAIL"
                failure_reasons.append(
                    f"TYPOGRAPHY_GATE_FAIL: Max kerning delta ({max_kern_delta:.2f} UPEM) exceeds threshold ({thresholds.max_kerning_delta_upem})"
                )

        typography_gate = TypographyGateResult(
            status=typo_status,
            total_pairs_evaluated=len(kerning_deltas),
            max_kerning_delta_upem=round(float(np.max(kerning_deltas)), 2) if kerning_deltas else 0.0,
            mean_kerning_delta_upem=round(float(np.mean(kerning_deltas)), 2) if kerning_deltas else 0.0,
            details={"held_out_pairs_count": len(held_out_pairs)},
        )

        # ==========================================
        # GATE 6: INDEPENDENT CONSUMERS GATE (MANDATORY & TYPED)
        # ==========================================
        ft_pass = False
        freetype_pass = False
        hb_pass = False
        chromium_pass = False
        consumer_status = "FAIL"
        bundle_hash = ""

        if consumer_bundle is None or not isinstance(consumer_bundle, ConsumerEvidenceBundle):
            failure_reasons.append("CONSUMER_GATE_FAIL: MISSING_CONSUMER_BUNDLE: Production evaluation requires a valid bound ConsumerEvidenceBundle")
        else:
            binding_errors = consumer_bundle.validate_bindings(
                expected_model_hash=model_hash,
                expected_config_hash=config_hash,
                expected_held_out_fingerprint=held_out_fp,
            )
            if binding_errors:
                for err in binding_errors:
                    failure_reasons.append(f"CONSUMER_GATE_FAIL: {err}")
            else:
                bundle_hash = consumer_bundle.compute_bundle_hash()
                ft = consumer_bundle.fonttools_result
                ft_pass = bool(
                    ft.is_direct_loadable_fonttools
                    and ft.has_valid_cmap
                    and ft.has_valid_metrics
                    and ft.decompression_round_trip
                    and getattr(ft, "validation_error", None) is None
                )

                fr = consumer_bundle.freetype_result
                freetype_pass = bool(
                    fr.render_error is None
                    and fr.raster_iou >= thresholds.min_raster_iou
                )

                hb = consumer_bundle.harfbuzz_result
                hb_pass = bool(
                    hb.in_candidate_cmap
                    and hb.glyph_sequence_match
                )

                cr = consumer_bundle.chromium_result
                chromium_pass = bool(
                    cr.is_available
                    and cr.is_direct_loadable_chromium
                    and cr.fallback_rejection_verified
                    and cr.rendered_canvas_valid
                    and cr.error_message is None
                    and cr.measured_glyph_count > 0
                    and cr.held_out_pairs_non_regression
                )

                if ft_pass and freetype_pass and hb_pass and chromium_pass:
                    consumer_status = "PASS"
                else:
                    failure_reasons.append(
                        f"CONSUMER_GATE_FAIL: Consumers failed: fonttools={ft_pass}, freetype={freetype_pass}, harfbuzz={hb_pass}, chromium={chromium_pass}"
                    )

        consumer_gate = ConsumerGateResult(
            status=consumer_status,
            total_consumers_evaluated=4 if consumer_bundle else 0,
            fonttools_passed=ft_pass,
            freetype_passed=freetype_pass,
            harfbuzz_passed=hb_pass,
            chromium_passed=chromium_pass,
            consumer_bundle_hash=bundle_hash,
            details={"bound_artifact_sha": consumer_bundle.candidate_artifact_sha if consumer_bundle else ""},
        )

        # ==========================================
        # OVERALL STATUS
        # ==========================================
        all_passed = (
            cov_status == "PASS"
            and top_status == "PASS"
            and geom_status == "PASS"
            and metrics_status == "PASS"
            and typo_status == "PASS"
            and consumer_status == "PASS"
            and not failure_reasons
        )
        overall_status = "PASS" if all_passed else "FAIL"

        policy_hash = thresholds.compute_policy_hash()
        report_id = hashlib.sha256(
            f"{model_hash}:{config_hash}:{fit_fp}:{held_out_fp}:{policy_hash}:{bundle_hash}".encode("utf-8")
        ).hexdigest()[:16]

        now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()

        return FidelityReport(
            schema_version="1.0.0",
            report_id=f"rep_{report_id}",
            model_canonical_hash=model_hash,
            config_hash=config_hash,
            fit_set_fingerprint=fit_fp,
            held_out_set_fingerprint=held_out_fp,
            policy_hash=policy_hash,
            policy=thresholds.to_dict(),
            overall_status=overall_status,
            coverage_gate=coverage_gate,
            topology_gate=topology_gate,
            geometry_raster_gate=geometry_raster_gate,
            metrics_gate=metrics_gate,
            typography_gate=typography_gate,
            consumer_gate=consumer_gate,
            failure_reasons=sorted(list(set(failure_reasons))),
            evaluation_timestamp_utc=now_utc,
        )
