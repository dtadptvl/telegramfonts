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
    def _compute_typography_fingerprint(pairs: Sequence[PairKerningObservation]) -> str:
        if not pairs:
            return "empty"
        for p in pairs:
            if p.left_char != chr(p.left_cp) or p.right_char != chr(p.right_cp):
                raise ValueError(
                    f"TYPOGRAPHY_CHAR_CODEPOINT_MISMATCH: left '{p.left_char}' != chr({p.left_cp}) or right '{p.right_char}' != chr({p.right_cp})"
                )
        sorted_pairs = sorted(pairs, key=lambda p: (p.left_cp, p.right_cp, p.left_char, p.right_char, p.provenance))
        payload_items = [
            f"{p.left_cp}:{p.left_char}:{p.right_cp}:{p.right_char}:{p.left_advance_upem:.2f}:{p.right_advance_upem:.2f}:{p.measured_pair_advance_upem:.2f}:{p.inferred_kerning_upem}:{p.provenance}"
            for p in sorted_pairs
        ]
        return hashlib.sha256(";".join(payload_items).encode("utf-8")).hexdigest()

    @classmethod
    def _compute_composite_held_out_fingerprint(
        cls,
        records: Sequence[ObservationRecord],
        pairs: Sequence[PairKerningObservation],
    ) -> str:
        r_fp = cls._compute_records_fingerprint(records)
        t_fp = cls._compute_typography_fingerprint(pairs)
        return hashlib.sha256(f"{r_fp}:{t_fp}".encode("utf-8")).hexdigest()

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
            # Zero-ink evidence: a blank raster perfectly matches a
            # contour-less glyph (e.g. U+0020); ink-carrying contours
            # against blank evidence remain a maximal fail-closed mismatch.
            return 0.0 if not glyph.contours else 1000.0

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
        required_resolutions: "tuple[int, ...] | None" = None,
        extension_codepoints: "frozenset[int]" = frozenset(),
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
        held_out_raster_fp = cls._compute_records_fingerprint(held_out_records)
        held_out_typo_fp = cls._compute_typography_fingerprint(held_out_pairs)
        composite_held_out_fp = cls._compute_composite_held_out_fingerprint(held_out_records, held_out_pairs)

        # ==========================================
        # GATE 0: FIT / HELD-OUT PROVENANCE, LEAKAGE & MODEL BINDING
        # ==========================================
        if not held_out_records:
            failure_reasons.append("ZERO_HELD_OUT_RASTER_SAMPLES: Nonempty held-out raster records required")
        if not held_out_pairs:
            failure_reasons.append("ZERO_HELD_OUT_TYPOGRAPHY_SAMPLES: Nonempty held-out typography pairs required")

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

        # Strict Model-to-Fit Evidence Binding. Glyphs constructed by a
        # provenance-attested extension (VIETNAMESE deterministic/AI) carry
        # no fit observations by construction; every other glyph must bind
        # exactly to its fit raster evidence (fail closed).
        for cp, glyph in model.glyphs.items():
            if cp in extension_codepoints:
                continue
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
                    fit_records, config=config, units_per_em=model.metrics.units_per_em, min_confidence=thresholds.min_confidence,
                    required_resolutions=required_resolutions,
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
                # Zero-ink semantics (production families always contain
                # blank glyphs, e.g. U+0020): a blank held-out raster and a
                # blank model render are a PERFECT match; ink on exactly one
                # side is a fail-closed mismatch.
                ref_ink = int(ref_mask.sum())
                model_ink = int(model_mask.sum())
                if ref_ink == 0 and model_ink == 0:
                    iou = 1.0
                elif ref_ink == 0 or model_ink == 0:
                    iou = 0.0
                else:
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
        consumer_gate, gate_failures = validate_consumer_gate(
            bundle=consumer_bundle,
            model=model,
            config=config,
            held_out_records=held_out_records,
            held_out_pairs=held_out_pairs,
            thresholds=thresholds,
        )
        if gate_failures:
            failure_reasons.extend(gate_failures)
        consumer_status = consumer_gate.status
        bundle_hash = consumer_gate.consumer_bundle_hash

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
        final_held_out_fp = composite_held_out_fp if (held_out_records and held_out_pairs) else held_out_raster_fp
        report_id = hashlib.sha256(
            f"{model_hash}:{config_hash}:{fit_fp}:{final_held_out_fp}:{policy_hash}:{bundle_hash}".encode("utf-8")
        ).hexdigest()[:16]

        now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()

        return FidelityReport(
            schema_version="1.0.0",
            report_id=f"rep_{report_id}",
            model_canonical_hash=model_hash,
            config_hash=config_hash,
            fit_set_fingerprint=fit_fp,
            held_out_set_fingerprint=final_held_out_fp,
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


def validate_consumer_gate(
    bundle: ConsumerEvidenceBundle | None,
    model: CanonicalFontModel,
    config: ObservationConfig,
    held_out_records: Sequence[ObservationRecord],
    held_out_pairs: Sequence[PairKerningObservation] | None = None,
    thresholds: FidelityThresholds | None = None,
) -> tuple[ConsumerGateResult, list[str]]:
    """Validate all four consumer evidence results against model, configuration, held-out sets, and thresholds."""
    if thresholds is None:
        thresholds = FidelityThresholds()

    failure_reasons: list[str] = []
    ft_pass = False
    freetype_pass = False
    hb_pass = False
    chromium_pass = False
    consumer_status = "FAIL"
    bundle_hash = ""

    if bundle is None or not isinstance(bundle, ConsumerEvidenceBundle):
        failure_reasons.append(
            "CONSUMER_GATE_FAIL: MISSING_CONSUMER_BUNDLE"
        )
        return (
            ConsumerGateResult(
                status="FAIL",
                total_consumers_evaluated=0,
                fonttools_passed=False,
                freetype_passed=False,
                harfbuzz_passed=False,
                chromium_passed=False,
                consumer_bundle_hash="",
                details={},
            ),
            failure_reasons,
        )

    model_hash = model.compute_canonical_hash()
    config_hash = config.compute_hash()
    held_out_raster_fp = FidelityEvaluator._compute_records_fingerprint(held_out_records) if held_out_records else None
    held_out_typo_fp = FidelityEvaluator._compute_typography_fingerprint(held_out_pairs) if held_out_pairs else None
    composite_held_out_fp = (
        FidelityEvaluator._compute_composite_held_out_fingerprint(held_out_records, held_out_pairs)
        if (held_out_records and held_out_pairs)
        else (held_out_raster_fp or "")
    )
    expected_bundle_fp = composite_held_out_fp if (held_out_records and held_out_pairs) else (held_out_raster_fp or "")

    binding_errors = bundle.validate_bindings(
        expected_model_hash=model_hash,
        expected_config_hash=config_hash,
        expected_held_out_fingerprint=expected_bundle_fp,
        expected_raster_fingerprint=held_out_raster_fp,
        expected_typography_fingerprint=held_out_typo_fp,
    )
    if binding_errors:
        for err in binding_errors:
            failure_reasons.append(f"CONSUMER_GATE_FAIL: {err}")
    else:
        bundle_hash = bundle.compute_bundle_hash()

        # 1. FontTools Format Validation
        ft = bundle.fonttools.result
        ft_pass = bool(
            getattr(ft, "is_direct_loadable_fonttools", False)
            and getattr(ft, "has_valid_cmap", False)
            and getattr(ft, "has_valid_metrics", False)
            and getattr(ft, "decompression_round_trip", False)
            and getattr(ft, "glyph_count", 0) > 0
            and getattr(ft, "units_per_em", 0) > 0
            and getattr(ft, "validation_error", None) is None
        )
        if not ft_pass:
            failure_reasons.append("CONSUMER_GATE_FAIL: FONTTOOLS_VALIDATION_FAILED")

        # 2. FreeType Raster Validation
        fr = bundle.freetype.result
        fr_samples = getattr(fr, "samples", ())
        fr_samples_ok = True
        if not fr_samples or len(fr_samples) != len(held_out_records):
            fr_samples_ok = False
            failure_reasons.append("CONSUMER_GATE_FAIL: FREETYPE_SAMPLE_COUNT_MISMATCH")
        else:
            sorted_samples = sorted(fr_samples, key=lambda s: s.cache_key)
            sorted_records = sorted(held_out_records, key=lambda r: r.cache_key)
            for s, r in zip(sorted_samples, sorted_records):
                if s.render_error is not None:
                    fr_samples_ok = False
                    failure_reasons.append("CONSUMER_GATE_FAIL: FREETYPE_SAMPLE_RENDER_FAILED")
                elif (
                    s.cache_key != r.cache_key
                    or s.code_point != r.code_point
                    or s.character != chr(r.code_point)
                    or s.resolution != r.resolution
                    or s.raster_sha256 != r.raster_sha256
                    or not math.isfinite(s.raster_iou)
                    or not (0.0 <= s.raster_iou <= 1.0)
                    or s.pixel_delta_count < 0
                ):
                    fr_samples_ok = False
                    failure_reasons.append("CONSUMER_GATE_FAIL: FREETYPE_SAMPLE_VALIDATION_FAILED")
                elif s.raster_iou < thresholds.min_raster_iou:
                    fr_samples_ok = False
                    failure_reasons.append("CONSUMER_GATE_FAIL: FREETYPE_IOU_BELOW_THRESHOLD")

        recomputed_min_iou = min((s.raster_iou for s in fr_samples), default=0.0) if fr_samples else 0.0
        recomputed_mean_iou = float(np.mean([s.raster_iou for s in fr_samples])) if fr_samples else 0.0
        recomputed_pixel_deltas = sum((s.pixel_delta_count for s in fr_samples), 0) if fr_samples else 0

        # Verify aggregate consistency against recomputed sample truth
        fr_aggregates_match = bool(
            fr_samples
            and abs(getattr(fr, "min_raster_iou", -1.0) - recomputed_min_iou) <= 1e-3
            and abs(getattr(fr, "raster_iou", -1.0) - recomputed_mean_iou) <= 1e-3
            and getattr(fr, "pixel_delta_count", -1) == recomputed_pixel_deltas
        )
        if fr_samples and not fr_aggregates_match:
            failure_reasons.append("CONSUMER_GATE_FAIL: FREETYPE_AGGREGATE_MISMATCH")

        freetype_pass = bool(
            getattr(fr, "render_error", None) is None
            and getattr(fr, "render_size_px", 0) > 0
            and math.isfinite(getattr(fr, "raster_iou", 0.0))
            and recomputed_min_iou >= thresholds.min_raster_iou
            and recomputed_mean_iou >= thresholds.min_raster_iou
            and fr_samples_ok
            and fr_aggregates_match
        )

        # 3. HarfBuzz Shaping Validation
        hb = bundle.harfbuzz.result
        hb_samples = getattr(hb, "samples", ())
        hb_samples_ok = True
        if not hb_samples or len(hb_samples) != len(held_out_pairs or []):
            hb_samples_ok = False
            failure_reasons.append("CONSUMER_GATE_FAIL: HARFBUZZ_SAMPLE_COUNT_MISMATCH")
        else:
            sorted_hb_samples = sorted(hb_samples, key=lambda s: (s.left_cp, s.right_cp, s.text))
            sorted_pairs = sorted(
                held_out_pairs or [], key=lambda p: (p.left_cp, p.right_cp, f"{p.left_char}{p.right_char}")
            )
            for s, p in zip(sorted_hb_samples, sorted_pairs):
                expected_text = f"{p.left_char}{p.right_char}"
                expected_total_adv = p.measured_pair_advance_upem
                exp1_adv = model.glyphs[p.left_cp].advance_width_upem + float(
                    model.kerning_pairs.get((p.left_cp, p.right_cp), 0)
                )
                exp2_adv = model.glyphs[p.right_cp].advance_width_upem

                if len(s.positions) == 2:
                    recomputed_cand_adv = s.positions[0].x_advance + s.positions[1].x_advance
                    recomputed_d1 = max(
                        abs(s.positions[0].x_advance - exp1_adv),
                        abs(s.positions[0].y_advance),
                        abs(s.positions[0].x_offset),
                        abs(s.positions[0].y_offset),
                    )
                    recomputed_d2 = max(
                        abs(s.positions[1].x_advance - exp2_adv),
                        abs(s.positions[1].y_advance),
                        abs(s.positions[1].x_offset),
                        abs(s.positions[1].y_offset),
                    )
                    recomputed_pos_delta = max(recomputed_d1, recomputed_d2)
                else:
                    recomputed_cand_adv = -1.0
                    recomputed_pos_delta = 10000.0

                recomputed_adv_delta = abs(recomputed_cand_adv - expected_total_adv)

                if s.error_message is not None:
                    hb_samples_ok = False
                    failure_reasons.append("CONSUMER_GATE_FAIL: HARFBUZZ_SAMPLE_SHAPING_FAILED")
                elif (
                    s.left_cp != p.left_cp
                    or s.right_cp != p.right_cp
                    or s.text != expected_text
                    or not s.in_candidate_cmap
                    or not s.glyph_sequence_match
                    or not math.isfinite(s.candidate_total_advance_upem)
                    or abs(s.expected_total_advance_upem - expected_total_adv) > 1e-3
                    or abs(s.candidate_total_advance_upem - recomputed_cand_adv) > 1e-3
                    or abs(s.advance_delta_upem - recomputed_adv_delta) > 1e-3
                    or abs(s.max_position_delta_upem - recomputed_pos_delta) > 1e-3
                    or len(s.positions) != 2
                    or len(s.clusters) != 2
                    or s.clusters != (0, 1)
                    or len(s.glyph_ids) != 2
                    or any(gid == 0 for gid in s.glyph_ids)
                ):
                    hb_samples_ok = False
                    failure_reasons.append("CONSUMER_GATE_FAIL: HARFBUZZ_SAMPLE_VALIDATION_FAILED")
                elif (
                    s.advance_delta_upem > thresholds.max_kerning_delta_upem
                    or recomputed_pos_delta > thresholds.max_kerning_delta_upem
                ):
                    hb_samples_ok = False
                    failure_reasons.append("CONSUMER_GATE_FAIL: HARFBUZZ_POSITION_DELTA_EXCEEDED")

        recomputed_hb_max_pos_delta = max((s.max_position_delta_upem for s in hb_samples), default=0.0) if hb_samples else 0.0
        recomputed_hb_all_cmap = all(s.in_candidate_cmap for s in hb_samples) if hb_samples else False
        recomputed_hb_all_seq = all(s.glyph_sequence_match for s in hb_samples) if hb_samples else False

        hb_aggregates_match = bool(
            hb_samples
            and getattr(hb, "all_in_cmap", False) == recomputed_hb_all_cmap
            and getattr(hb, "all_sequence_match", False) == recomputed_hb_all_seq
            and abs(getattr(hb, "max_position_delta_upem", -1.0) - recomputed_hb_max_pos_delta) <= 1e-3
        )
        if hb_samples and not hb_aggregates_match:
            failure_reasons.append("CONSUMER_GATE_FAIL: HARFBUZZ_AGGREGATE_MISMATCH")

        hb_pass = bool(
            getattr(hb, "in_candidate_cmap", False)
            and getattr(hb, "glyph_sequence_match", False)
            and getattr(hb, "candidate_glyph_count", 0) > 0
            and math.isfinite(getattr(hb, "candidate_total_advance_upem", 0.0))
            and recomputed_hb_max_pos_delta <= thresholds.max_kerning_delta_upem
            and hb_samples_ok
            and hb_aggregates_match
            and getattr(hb, "error_message", None) is None
        )

        # 4. Chromium Session / Canvas Validation
        cr = bundle.chromium.result
        cr_pair_samples = getattr(cr, "pair_samples", ())
        cr_glyph_samples = getattr(cr, "glyph_samples", ())
        cr_pair_samples_ok = True
        cr_glyph_samples_ok = True

        expected_unique_cps = sorted(list({r.code_point for r in held_out_records if r.code_point in model.glyphs}))
        if len(cr_glyph_samples) != len(expected_unique_cps) or len(cr_glyph_samples) == 0:
            cr_glyph_samples_ok = False
            failure_reasons.append("CONSUMER_GATE_FAIL: CHROMIUM_GLYPH_COUNT_MISMATCH")
        else:
            sorted_cr_glyph_samples = sorted(cr_glyph_samples, key=lambda s: s.code_point)
            for s, cp in zip(sorted_cr_glyph_samples, expected_unique_cps):
                exp_glyph_adv = model.glyphs[cp].advance_width_upem
                exp_glyph_delta = abs(s.candidate_advance_upem - exp_glyph_adv)
                if (
                    s.code_point != cp
                    or s.character != chr(cp)
                    or not math.isfinite(s.candidate_advance_upem)
                    or abs(s.expected_advance_upem - exp_glyph_adv) > 1e-3
                    or abs(s.advance_delta_upem - exp_glyph_delta) > 1e-3
                ):
                    cr_glyph_samples_ok = False
                    failure_reasons.append("CONSUMER_GATE_FAIL: CHROMIUM_GLYPH_SAMPLE_FAILED")
                elif s.advance_delta_upem > thresholds.max_advance_width_delta_upem:
                    cr_glyph_samples_ok = False
                    failure_reasons.append("CONSUMER_GATE_FAIL: CHROMIUM_GLYPH_DELTA_EXCEEDED")

        if not cr_pair_samples or len(cr_pair_samples) != len(held_out_pairs or []):
            cr_pair_samples_ok = False
            failure_reasons.append("CONSUMER_GATE_FAIL: CHROMIUM_PAIR_COUNT_MISMATCH")
        else:
            sorted_cr_samples = sorted(cr_pair_samples, key=lambda s: (s.left_cp, s.right_cp, s.pair))
            sorted_pairs = sorted(
                held_out_pairs or [], key=lambda p: (p.left_cp, p.right_cp, f"{p.left_char}{p.right_char}")
            )
            for s, p in zip(sorted_cr_samples, sorted_pairs):
                expected_pair = f"{p.left_char}{p.right_char}"
                exp_pair_adv = p.measured_pair_advance_upem
                exp_gpos_adj = s.candidate_pair_advance_upem - s.baseline_single_sum_upem
                exp_pair_delta = abs(s.candidate_pair_advance_upem - exp_pair_adv)
                recomputed_base_err = abs(s.baseline_single_sum_upem - exp_pair_adv)
                recomputed_non_reg = bool(
                    exp_pair_delta <= recomputed_base_err + 2.0
                    and exp_pair_delta <= thresholds.max_kerning_delta_upem
                )

                if (
                    s.left_cp != p.left_cp
                    or s.right_cp != p.right_cp
                    or s.pair != expected_pair
                    or not math.isfinite(s.candidate_pair_advance_upem)
                    or abs(s.expected_pair_advance_upem - exp_pair_adv) > 1e-3
                    or abs(s.gpos_applied_adjustment_upem - exp_gpos_adj) > 1e-3
                    or abs(s.advance_delta_upem - exp_pair_delta) > 1e-3
                ):
                    cr_pair_samples_ok = False
                    failure_reasons.append("CONSUMER_GATE_FAIL: CHROMIUM_PAIR_SAMPLE_FAILED")
                elif not s.non_regression or s.non_regression != recomputed_non_reg:
                    cr_pair_samples_ok = False
                    failure_reasons.append("CONSUMER_GATE_FAIL: CHROMIUM_PAIR_REGRESSION")
                elif s.advance_delta_upem > thresholds.max_kerning_delta_upem:
                    cr_pair_samples_ok = False
                    failure_reasons.append("CONSUMER_GATE_FAIL: CHROMIUM_PAIR_DELTA_EXCEEDED")

        recomputed_cr_mean_err = (
            float(np.mean([s.advance_delta_upem for s in cr_glyph_samples])) if cr_glyph_samples else 0.0
        )
        recomputed_cr_all_non_reg = (
            bool(all(s.non_regression for s in cr_pair_samples)) if cr_pair_samples else False
        )

        cr_aggregates_match = bool(
            cr_glyph_samples
            and cr_pair_samples
            and getattr(cr, "measured_glyph_count", 0) == len(expected_unique_cps)
            and getattr(cr, "held_out_pairs_non_regression", False) == recomputed_cr_all_non_reg
            and abs(getattr(cr, "mean_chromium_advance_error_upem", -1.0) - recomputed_cr_mean_err) <= 1e-3
        )
        if (cr_glyph_samples or cr_pair_samples) and not cr_aggregates_match:
            failure_reasons.append("CONSUMER_GATE_FAIL: CHROMIUM_AGGREGATE_MISMATCH")

        chromium_pass = bool(
            getattr(cr, "is_available", False)
            and getattr(cr, "is_direct_loadable_chromium", False)
            and getattr(cr, "fallback_rejection_verified", False)
            and getattr(cr, "rendered_canvas_valid", False)
            and getattr(cr, "error_message", None) is None
            and cr_glyph_samples_ok
            and cr_pair_samples_ok
            and cr_aggregates_match
            and recomputed_cr_all_non_reg
        )
        if getattr(cr, "error_message", None) is not None or not getattr(cr, "rendered_canvas_valid", False):
            failure_reasons.append("CONSUMER_GATE_FAIL: CHROMIUM_ENVIRONMENT_FAILED")

        if ft_pass and freetype_pass and hb_pass and chromium_pass:
            consumer_status = "PASS"
        else:
            failure_reasons.append("CONSUMER_GATE_FAIL: CONSUMERS_FAILED")

    consumer_gate = ConsumerGateResult(
        status=consumer_status,
        total_consumers_evaluated=4 if bundle else 0,
        fonttools_passed=ft_pass,
        freetype_passed=freetype_pass,
        harfbuzz_passed=hb_pass,
        chromium_passed=chromium_pass,
        consumer_bundle_hash=bundle_hash,
        details={"bound_artifact_sha": bundle.candidate_artifact_sha if bundle else ""},
    )
    return consumer_gate, failure_reasons
