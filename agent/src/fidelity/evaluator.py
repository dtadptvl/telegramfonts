"""Fail-closed fidelity evaluator and multi-gate verification engine (Stage 9A)."""
from __future__ import annotations

import datetime
import hashlib
import io
import math
from typing import Any, Callable, Sequence

import numpy as np
from PIL import Image, ImageDraw

from fidelity.models import (
    CoverageGateResult,
    FidelityReport,
    FidelityThresholds,
    GeometryRasterGateResult,
    MetricsGateResult,
    TopologyGateResult,
    TypographyGateResult,
)
from measurement.calibration import CalibrationTransform
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

        # Sort contours so outer contours are drawn first (value 255), then holes (value 0)
        outer_contours = [c for c in glyph.contours if not c.is_hole]
        hole_contours = [c for c in glyph.contours if c.is_hole]

        for c in outer_contours:
            pts = c.sample_points(samples_per_segment=12)
            if len(pts) < 3:
                continue
            poly_px = [transform.inverse(p.x, p.y) for p in pts]
            draw.polygon(poly_px, fill=255)

        for c in hole_contours:
            pts = c.sample_points(samples_per_segment=12)
            if len(pts) < 3:
                continue
            poly_px = [transform.inverse(p.x, p.y) for p in pts]
            draw.polygon(poly_px, fill=0)

        arr = np.array(img, dtype=np.uint8)
        return (arr > 127).astype(np.uint8)

    @classmethod
    def evaluate(
        cls,
        model: CanonicalFontModel,
        config: ObservationConfig,
        fit_records: Sequence[ObservationRecord],
        held_out_records: Sequence[ObservationRecord],
        fit_pairs: Sequence[PairKerningObservation] = (),
        held_out_pairs: Sequence[PairKerningObservation] = (),
        thresholds: FidelityThresholds | None = None,
        raster_provider: Callable[[ObservationRecord], bytes | np.ndarray] | None = None,
    ) -> FidelityReport:
        """Execute full fail-closed multi-gate evaluation and generate immutable FidelityReport."""
        if thresholds is None:
            thresholds = FidelityThresholds()
        thresholds.validate()

        failure_reasons: list[str] = []
        model_hash = model.compute_canonical_hash()
        config_hash = config.compute_hash()

        fit_fp = cls._compute_records_fingerprint(fit_records)
        held_out_fp = cls._compute_records_fingerprint(held_out_records)

        # ==========================================
        # GATE 0: FIT / HELD-OUT SEPARATION & LEAKAGE CHECK
        # ==========================================
        fit_keys = {r.cache_key for r in fit_records}
        held_out_keys = {r.cache_key for r in held_out_records}
        key_overlap = fit_keys & held_out_keys
        if key_overlap:
            failure_reasons.append(
                f"LEAKAGE_DETECTED: Fit and held-out observation sets share {len(key_overlap)} cache keys"
            )

        fit_pair_keys = {(p.left_cp, p.right_cp) for p in fit_pairs}
        held_out_pair_keys = {(p.left_cp, p.right_cp) for p in held_out_pairs}
        pair_overlap = fit_pair_keys & held_out_pair_keys
        if pair_overlap:
            failure_reasons.append(
                f"LEAKAGE_DETECTED: Fit and held-out typography pairs share {len(pair_overlap)} pairs"
            )

        if not held_out_records and not held_out_pairs:
            failure_reasons.append("MISSING_HELD_OUT_EVIDENCE: No held-out records or pairs provided")

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
                if not c.is_closed and not thresholds.allow_unclosed_contours:
                    unclosed_count += 1
                for s in c.segments:
                    if s.approximate_length() < 1e-4:
                        degenerate_seg_count += 1

        top_pass_rate = 1.0 if total_contours == 0 else max(0.0, 1.0 - (unclosed_count + degenerate_seg_count) / max(total_contours, 1))
        top_status = "PASS"
        if unclosed_count > 0 and not thresholds.allow_unclosed_contours:
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
        # GATE 3: GEOMETRY & RASTER GATE (HELD-OUT)
        # ==========================================
        ious: list[float] = []
        for r in held_out_records:
            if r.code_point not in model.glyphs:
                continue
            glyph = model.glyphs[r.code_point]
            if not glyph.contours:
                continue

            transform = CalibrationTransform.from_observation(
                resolution=r.resolution,
                metrics=r.metrics,
                subpixel_x=r.subpixel_x,
                subpixel_y=r.subpixel_y,
                units_per_em=model.metrics.units_per_em,
            )

            # Get reference held-out raster
            ref_mask: np.ndarray | None = None
            if raster_provider is not None:
                raw_data = raster_provider(r)
                if isinstance(raw_data, bytes):
                    img = Image.open(io.BytesIO(raw_data)).convert("L")
                    arr = np.array(img, dtype=np.float32) / 255.0
                    ref_mask = ((1.0 - arr) >= 0.5).astype(np.uint8)
                elif isinstance(raw_data, np.ndarray):
                    ref_mask = (raw_data > 0).astype(np.uint8)

            if ref_mask is not None:
                model_mask = cls._rasterize_glyph_contours(glyph, transform, r.resolution)
                intersection = int(np.logical_and(model_mask, ref_mask).sum())
                union = int(np.logical_or(model_mask, ref_mask).sum())
                iou = float(intersection) / max(union, 1)
                ious.append(iou)
            else:
                # If no raster bytes provided, compute geometric bbox IoU
                gw = max(glyph.bounding_box_upem[2] - glyph.bounding_box_upem[0], 1.0)
                gh = max(glyph.bounding_box_upem[3] - glyph.bounding_box_upem[1], 1.0)
                mw = max(r.metrics.bbox_width_upem, 1.0)
                mh = max(r.metrics.bbox_height_upem, 1.0)
                w_ratio = min(gw, mw) / max(gw, mw)
                h_ratio = min(gh, mh) / max(gh, mh)
                ious.append(w_ratio * h_ratio)

        mean_iou = float(np.mean(ious)) if ious else 1.0
        min_iou = float(np.min(ious)) if ious else 1.0

        geom_status = "PASS"
        if ious and mean_iou < thresholds.min_raster_iou:
            geom_status = "FAIL"
            failure_reasons.append(
                f"GEOMETRY_RASTER_GATE_FAIL: Mean IoU ({mean_iou:.4f}) below threshold ({thresholds.min_raster_iou})"
            )

        geometry_raster_gate = GeometryRasterGateResult(
            status=geom_status,
            mean_iou=round(mean_iou, 4),
            min_iou=round(min_iou, 4),
            evaluated_glyphs_count=len(ious),
            details={"samples_count": len(ious)},
        )

        # ==========================================
        # GATE 4: METRICS GATE (HELD-OUT)
        # ==========================================
        adv_deltas: list[float] = []
        for r in held_out_records:
            if r.code_point not in model.glyphs:
                continue
            glyph = model.glyphs[r.code_point]
            delta = abs(glyph.advance_width_upem - r.metrics.advance_width_upem)
            adv_deltas.append(delta)

        mean_adv_delta = float(np.mean(adv_deltas)) if adv_deltas else 0.0
        max_adv_delta = float(np.max(adv_deltas)) if adv_deltas else 0.0
        rms_adv_delta = float(np.sqrt(np.mean(np.square(adv_deltas)))) if adv_deltas else 0.0

        metrics_status = "PASS"
        if adv_deltas:
            if max_adv_delta > thresholds.max_advance_width_delta_upem:
                metrics_status = "FAIL"
                failure_reasons.append(
                    f"METRICS_GATE_FAIL: Max advance delta ({max_adv_delta:.2f} UPEM) exceeds threshold ({thresholds.max_advance_width_delta_upem})"
                )
            if rms_adv_delta > thresholds.max_metric_rms_upem:
                metrics_status = "FAIL"
                failure_reasons.append(
                    f"METRICS_GATE_FAIL: RMS advance delta ({rms_adv_delta:.2f} UPEM) exceeds threshold ({thresholds.max_metric_rms_upem})"
                )

        metrics_gate = MetricsGateResult(
            status=metrics_status,
            advance_width_mean_delta_upem=round(mean_adv_delta, 2),
            advance_width_max_delta_upem=round(max_adv_delta, 2),
            advance_width_rms_delta_upem=round(rms_adv_delta, 2),
            evaluated_metrics_count=len(adv_deltas),
            details={"total_held_out_metrics": len(adv_deltas)},
        )

        # ==========================================
        # GATE 5: TYPOGRAPHY / KERNING GATE (HELD-OUT)
        # ==========================================
        kerning_deltas: list[float] = []
        for p in held_out_pairs:
            pair_key = (p.left_cp, p.right_cp)
            model_kern = model.kerning_pairs.get(pair_key, 0)
            delta = abs(model_kern - p.inferred_kerning_upem)
            kerning_deltas.append(float(delta))

        max_kern_delta = float(np.max(kerning_deltas)) if kerning_deltas else 0.0
        mean_kern_delta = float(np.mean(kerning_deltas)) if kerning_deltas else 0.0

        typo_status = "PASS"
        if kerning_deltas and max_kern_delta > thresholds.max_kerning_delta_upem:
            typo_status = "FAIL"
            failure_reasons.append(
                f"TYPOGRAPHY_GATE_FAIL: Max kerning delta ({max_kern_delta:.2f} UPEM) exceeds threshold ({thresholds.max_kerning_delta_upem})"
            )

        typography_gate = TypographyGateResult(
            status=typo_status,
            total_pairs_evaluated=len(kerning_deltas),
            max_kerning_delta_upem=round(max_kern_delta, 2),
            mean_kerning_delta_upem=round(mean_kern_delta, 2),
            details={"held_out_pairs_count": len(held_out_pairs)},
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
            and not failure_reasons
        )
        overall_status = "PASS" if all_passed else "FAIL"

        report_id = hashlib.sha256(
            f"{model_hash}:{config_hash}:{fit_fp}:{held_out_fp}".encode("utf-8")
        ).hexdigest()[:16]

        now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()

        return FidelityReport(
            schema_version="1.0.0",
            report_id=f"rep_{report_id}",
            model_canonical_hash=model_hash,
            config_hash=config_hash,
            fit_set_fingerprint=fit_fp,
            held_out_set_fingerprint=held_out_fp,
            overall_status=overall_status,
            coverage_gate=coverage_gate,
            topology_gate=topology_gate,
            geometry_raster_gate=geometry_raster_gate,
            metrics_gate=metrics_gate,
            typography_gate=typography_gate,
            failure_reasons=failure_reasons,
            evaluation_timestamp_utc=now_utc,
        )
