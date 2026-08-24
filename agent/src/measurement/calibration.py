"""Deterministic coordinate calibration and design-space transformation engine (Stage 9A).

Converts multi-resolution and subpixel raster observations alongside direct browser
metrics into calibrated font design space (UPEM units) coordinates and metrics.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

import numpy as np

from measurement.models import DirectMetrics, ObservationConfig, ObservationRecord


@dataclass(frozen=True)
class CalibrationTransform:
    """Explicit deterministic affine coordinate transformation between canvas raster and UPEM design space."""

    resolution: int
    font_size_px: float
    units_per_em: int
    scale: float
    x_origin_px: float
    y_origin_px: float
    subpixel_x: float
    subpixel_y: float
    browser_version: str = "chromium"
    confidence: float = 1.0

    @classmethod
    def from_observation(
        cls,
        resolution: int,
        metrics: DirectMetrics,
        subpixel_x: float = 0.0,
        subpixel_y: float = 0.0,
        units_per_em: int = 1000,
        browser_version: str = "chromium",
    ) -> CalibrationTransform:
        """Derive explicit canvas-to-UPEM transform from observation resolution and direct metrics."""
        if resolution <= 0:
            raise ValueError(f"Invalid non-positive resolution: {resolution}")
        if units_per_em <= 0:
            raise ValueError(f"Invalid non-positive UPEM: {units_per_em}")
        if not math.isfinite(metrics.advance_width_upem) or metrics.advance_width_upem < 0:
            raise ValueError(f"Invalid advance width in metrics: {metrics.advance_width_upem}")

        # Standard canvas layout mapping matching ChromiumSession / baseline
        f_size_px = math.floor(resolution * 0.72)
        if f_size_px <= 0:
            raise ValueError(f"Derived font size must be positive, got {f_size_px}")

        scale = f_size_px / float(units_per_em)
        if not math.isfinite(scale) or scale <= 0:
            raise ValueError(f"Derived scale factor is non-finite or non-positive: {scale}")

        adv_px = metrics.advance_width_upem * scale
        ascent_px = metrics.ascent_upem * scale
        descent_px = metrics.descent_upem * scale
        total_h_px = ascent_px + descent_px

        x_origin_px = round((resolution - adv_px) / 2.0)
        y_origin_px = round((resolution - total_h_px) / 2.0 + ascent_px)

        return cls(
            resolution=resolution,
            font_size_px=float(f_size_px),
            units_per_em=units_per_em,
            scale=scale,
            x_origin_px=float(x_origin_px),
            y_origin_px=float(y_origin_px),
            subpixel_x=float(subpixel_x),
            subpixel_y=float(subpixel_y),
            browser_version=browser_version,
            confidence=float(metrics.confidence),
        )

    def forward(self, px_x: float, px_y: float) -> tuple[float, float]:
        """Transform raster pixel coordinate (px_x, px_y) into UPEM design space (X, Y)."""
        if not math.isfinite(px_x) or not math.isfinite(px_y):
            raise ValueError(f"Non-finite pixel coordinates: ({px_x}, {px_y})")
        if self.scale <= 1e-12:
            raise ValueError("Degenerate scale factor in calibration transform")

        # In canvas coordinates, px_y grows downward; font design space Y grows upward from baseline Y=0
        x_upem = (px_x - self.x_origin_px - self.subpixel_x) / self.scale
        y_upem = (self.y_origin_px - px_y - self.subpixel_y) / self.scale
        return (x_upem, y_upem)

    def inverse(self, upem_x: float, upem_y: float) -> tuple[float, float]:
        """Transform UPEM design space coordinate (upem_x, upem_y) into raster pixel space (px_x, px_y)."""
        if not math.isfinite(upem_x) or not math.isfinite(upem_y):
            raise ValueError(f"Non-finite UPEM coordinates: ({upem_x}, {upem_y})")

        px_x = self.x_origin_px + self.subpixel_x + upem_x * self.scale
        px_y = self.y_origin_px - self.subpixel_y - upem_y * self.scale
        return (px_x, px_y)

    def to_dict(self) -> dict[str, Any]:
        return {
            "resolution": self.resolution,
            "font_size_px": round(self.font_size_px, 4),
            "units_per_em": self.units_per_em,
            "scale": round(self.scale, 8),
            "x_origin_px": round(self.x_origin_px, 4),
            "y_origin_px": round(self.y_origin_px, 4),
            "subpixel_x": round(self.subpixel_x, 4),
            "subpixel_y": round(self.subpixel_y, 4),
            "browser_version": self.browser_version,
            "confidence": round(self.confidence, 4),
        }


@dataclass(frozen=True)
class CalibratedGlyphMetrics:
    """Consensus calibrated metrics for a single glyph in font design space (UPEM units)."""

    code_point: int
    character: str
    advance_width_upem: float
    lsb_upem: float
    rsb_upem: float
    ascent_upem: float
    descent_upem: float
    bbox_width_upem: float
    bbox_height_upem: float
    confidence: float
    sample_count: int
    resolution_transforms: tuple[CalibrationTransform, ...] = ()
    observation_fingerprints: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "code_point": self.code_point,
            "character": self.character,
            "advance_width_upem": round(self.advance_width_upem, 2),
            "lsb_upem": round(self.lsb_upem, 2),
            "rsb_upem": round(self.rsb_upem, 2),
            "ascent_upem": round(self.ascent_upem, 2),
            "descent_upem": round(self.descent_upem, 2),
            "bbox_width_upem": round(self.bbox_width_upem, 2),
            "bbox_height_upem": round(self.bbox_height_upem, 2),
            "confidence": round(self.confidence, 4),
            "sample_count": self.sample_count,
            "resolution_transforms": [t.to_dict() for t in self.resolution_transforms],
            "observation_fingerprints": list(self.observation_fingerprints),
        }


class ObservationCalibrator:
    """Order-independent, fail-closed calibration engine for multi-resolution observations."""

    @staticmethod
    def sort_observations(records: Sequence[ObservationRecord]) -> list[ObservationRecord]:
        """Order observation records strictly and deterministically by code point, resolution, and phase."""
        return sorted(
            records,
            key=lambda r: (
                r.code_point,
                r.resolution,
                r.subpixel_x,
                r.subpixel_y,
                r.cache_key,
            ),
        )

    @classmethod
    def calibrate_glyph_observations(
        cls,
        records: Sequence[ObservationRecord],
        config: ObservationConfig | None = None,
        units_per_em: int = 1000,
    ) -> CalibratedGlyphMetrics:
        """Calibrate all observation records for a single glyph into unified UPEM design space metrics."""
        if not records:
            raise ValueError("NO_OBSERVATIONS_FOR_GLYPH")

        # Sort deterministically
        sorted_records = cls.sort_observations(records)
        first_rec = sorted_records[0]
        code_point = first_rec.code_point
        reference_id = first_rec.reference_id
        style_id = first_rec.style_id

        # Validate identity consistency across all records
        for r in sorted_records:
            if r.code_point != code_point:
                raise ValueError(f"Code point mismatch in calibration set: {r.code_point} != {code_point}")
            if r.reference_id != reference_id:
                raise ValueError(f"Reference ID mismatch: {r.reference_id} != {reference_id}")
            if r.style_id != style_id:
                raise ValueError(f"Style ID mismatch: {r.style_id} != {style_id}")
            if not r.raster_sha256 or len(r.raster_sha256) != 64:
                raise ValueError(f"Corrupt or missing raster SHA256 in observation: {r.cache_key}")
            if not math.isfinite(r.metrics.advance_width_upem) or r.metrics.advance_width_upem < 0:
                raise ValueError(f"Invalid non-finite or negative advance width: {r.metrics.advance_width_upem}")
            if not math.isfinite(r.metrics.ascent_upem) or not math.isfinite(r.metrics.descent_upem):
                raise ValueError("Non-finite vertical metrics in observation")

        # Verify required resolution coverage if config specified
        resolutions_present = {r.resolution for r in sorted_records}
        if config is not None:
            required_res = set(config.resolutions)
            missing = required_res - resolutions_present
            if missing:
                raise ValueError(f"Missing required observation resolutions for CP {code_point}: {sorted(missing)}")

        # Build explicit calibration transforms per resolution/phase
        transforms: list[CalibrationTransform] = []
        advances: list[float] = []
        lsbs: list[float] = []
        rsbs: list[float] = []
        ascents: list[float] = []
        descents: list[float] = []
        bbox_ws: list[float] = []
        bbox_hs: list[float] = []
        fingerprints: list[str] = []

        for r in sorted_records:
            t = CalibrationTransform.from_observation(
                resolution=r.resolution,
                metrics=r.metrics,
                subpixel_x=r.subpixel_x,
                subpixel_y=r.subpixel_y,
                units_per_em=units_per_em,
            )
            transforms.append(t)
            advances.append(r.metrics.advance_width_upem)
            lsbs.append(r.metrics.lsb_upem)
            rsbs.append(r.metrics.rsb_upem)
            ascents.append(r.metrics.ascent_upem)
            descents.append(r.metrics.descent_upem)
            bbox_ws.append(r.metrics.bbox_width_upem)
            bbox_hs.append(r.metrics.bbox_height_upem)
            fingerprints.append(r.raster_sha256)

        # Compute consensus / median-filtered design-space metrics
        adv_consensus = float(np.median(advances)) if len(advances) > 1 else advances[0]
        lsb_consensus = float(np.median(lsbs)) if len(lsbs) > 1 else lsbs[0]
        rsb_consensus = float(np.median(rsbs)) if len(rsbs) > 1 else rsbs[0]
        ascent_consensus = float(np.median(ascents)) if len(ascents) > 1 else ascents[0]
        descent_consensus = float(np.median(descents)) if len(descents) > 1 else descents[0]
        bbox_w_consensus = float(np.median(bbox_ws)) if len(bbox_ws) > 1 else bbox_ws[0]
        bbox_h_consensus = float(np.median(bbox_hs)) if len(bbox_hs) > 1 else bbox_hs[0]

        # Calculate consensus confidence (penalizing metric variance)
        var_adv = float(np.var(advances)) if len(advances) > 1 else 0.0
        confidence = max(0.1, min(1.0, 1.0 - math.sqrt(var_adv) / 100.0))

        char_str = chr(code_point) if 0 <= code_point <= 0x10FFFF else "?"

        return CalibratedGlyphMetrics(
            code_point=code_point,
            character=char_str,
            advance_width_upem=round(adv_consensus, 2),
            lsb_upem=round(lsb_consensus, 2),
            rsb_upem=round(rsb_consensus, 2),
            ascent_upem=round(ascent_consensus, 2),
            descent_upem=round(descent_consensus, 2),
            bbox_width_upem=round(bbox_w_consensus, 2),
            bbox_height_upem=round(bbox_h_consensus, 2),
            confidence=round(confidence, 4),
            sample_count=len(sorted_records),
            resolution_transforms=tuple(transforms),
            observation_fingerprints=tuple(sorted(fingerprints)),
        )

    @classmethod
    def calibrate_all(
        cls,
        records: Sequence[ObservationRecord],
        config: ObservationConfig | None = None,
        units_per_em: int = 1000,
    ) -> dict[int, CalibratedGlyphMetrics]:
        """Group and calibrate observations across all glyphs in deterministic code-point order."""
        if not records:
            return {}

        # Group records by code point
        grouped: dict[int, list[ObservationRecord]] = {}
        for r in records:
            grouped.setdefault(r.code_point, []).append(r)

        calibrated: dict[int, CalibratedGlyphMetrics] = {}
        for cp in sorted(grouped.keys()):
            calibrated[cp] = cls.calibrate_glyph_observations(
                grouped[cp], config=config, units_per_em=units_per_em
            )

        return calibrated
