"""Data models and configuration for MAX pipeline observation, direct measurement, and benchmark verification."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class ObservationConfig:
    """Immutable configuration for multi-resolution raster and adaptive subpixel phase observations."""

    resolutions: tuple[int, ...] = (128, 256, 512)
    base_subpixel_phases: tuple[tuple[float, float], ...] = ((0.0, 0.0),)
    expanded_subpixel_phases: tuple[tuple[float, float], ...] = (
        (0.0, 0.0),
        (0.25, 0.0),
        (0.5, 0.0),
        (0.75, 0.0),
        (0.0, 0.25),
        (0.0, 0.5),
        (0.0, 0.75),
    )
    held_out_subpixel_phases: tuple[tuple[float, float], ...] = ((0.25, 0.25),)
    adaptive_expansion_threshold: float = 0.05
    font_size_px: float = 200.0
    metric_sizes_px: tuple[float, ...] = (32.0, 64.0, 128.0, 200.0)
    feature_probes: tuple[tuple[str, str], ...] = (
        ("kern", "AV"),
        ("liga", "ffi"),
        ("calt", "->"),
    )
    upem: int = 1000
    timeout_seconds: float = 10.0
    max_retries: int = 3
    config_version: str = "1.1.0"

    def get_phases_for_metrics(self, metrics: DirectMetrics) -> tuple[tuple[float, float], ...]:
        """Determine adaptive subpixel phases based on fractional metric uncertainty/boundary alignment."""
        adv_frac = abs(metrics.raw_advance_width - round(metrics.raw_advance_width))
        left_frac = abs(metrics.raw_actual_left - round(metrics.raw_actual_left))
        if adv_frac >= self.adaptive_expansion_threshold or left_frac >= self.adaptive_expansion_threshold:
            return self.expanded_subpixel_phases
        return self.base_subpixel_phases

    def compute_hash(self) -> str:
        """Calculate deterministic SHA-256 hash digest of this configuration."""
        raw_dict = {
            "resolutions": list(self.resolutions),
            "base_subpixel_phases": [list(p) for p in self.base_subpixel_phases],
            "expanded_subpixel_phases": [list(p) for p in self.expanded_subpixel_phases],
            "held_out_subpixel_phases": [list(p) for p in self.held_out_subpixel_phases],
            "adaptive_expansion_threshold": self.adaptive_expansion_threshold,
            "font_size_px": self.font_size_px,
            "metric_sizes_px": list(self.metric_sizes_px),
            "feature_probes": [list(p) for p in self.feature_probes],
            "upem": self.upem,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "config_version": self.config_version,
        }
        serialized = json.dumps(raw_dict, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BrowserFontSelection:
    """Observable page font descriptors selected without acquiring font binaries."""

    family: str
    style: str = "normal"
    weight: str = "400"
    stretch: str = "normal"


@dataclass(frozen=True)
class MetricObservation:
    """A direct browser metric sample at one font size."""

    reference_id: str
    style_id: str
    browser_version: str
    config_hash: str
    metrics: DirectMetrics
    created_at: str


@dataclass(frozen=True)
class OpenTypeFeatureObservation:
    """Observable OpenType feature on/off probe from browser shaping and raster output."""

    reference_id: str
    style_id: str
    feature_tag: str
    sample_text: str
    enabled_advance_upem: float
    disabled_advance_upem: float
    enabled_raster_signature: str
    disabled_raster_signature: str
    effect_observed: bool
    provenance: str
    created_at: str


@dataclass(frozen=True)
class DirectMetrics:
    """Direct glyph metrics measured from browser CanvasRenderingContext2D / DOM APIs."""

    code_point: int
    character: str
    font_size_px: float
    raw_advance_width: float
    raw_actual_left: float
    raw_actual_right: float
    raw_actual_ascent: float
    raw_actual_descent: float
    raw_font_ascent: float
    raw_font_descent: float
    advance_width_upem: float
    lsb_upem: float
    rsb_upem: float
    ascent_upem: float
    descent_upem: float
    bbox_width_upem: float
    bbox_height_upem: float
    sample_count: int = 1
    confidence: float = 1.0

    @classmethod
    def from_browser_measurements(
        cls,
        code_point: int,
        char: str,
        font_size_px: float,
        m: dict[str, float],
        upem: int = 1000,
        sample_count: int = 1,
        confidence: float = 1.0,
    ) -> DirectMetrics:
        """Convert raw browser TextMetrics dictionary into normalized UPEM direct metrics."""
        raw_adv = float(m.get("width", 0.0))
        raw_left = float(m.get("actualBoundingBoxLeft", 0.0))
        raw_right = float(m.get("actualBoundingBoxRight", 0.0))
        raw_ascent = float(m.get("actualBoundingBoxAscent", 0.0))
        raw_descent = float(m.get("actualBoundingBoxDescent", 0.0))
        raw_f_ascent = float(m.get("fontBoundingBoxAscent", 0.0))
        raw_f_descent = float(m.get("fontBoundingBoxDescent", 0.0))

        scale = float(upem) / max(font_size_px, 1.0)

        # In Font Units (UPEM):
        # advance = width * scale
        # lsb = -actualBoundingBoxLeft * scale
        # rsb = (width - actualBoundingBoxRight) * scale
        # ascent = actualBoundingBoxAscent * scale
        # descent = -actualBoundingBoxDescent * scale
        adv_upem = round(raw_adv * scale, 2)
        lsb_upem = round(-raw_left * scale, 2)
        rsb_upem = round((raw_adv - raw_right) * scale, 2)
        ascent_upem = round(raw_ascent * scale, 2)
        descent_upem = round(-raw_descent * scale, 2)
        bbox_w_upem = round((raw_left + raw_right) * scale, 2)
        bbox_h_upem = round((raw_ascent + raw_descent) * scale, 2)

        return cls(
            code_point=code_point,
            character=char,
            font_size_px=font_size_px,
            raw_advance_width=raw_adv,
            raw_actual_left=raw_left,
            raw_actual_right=raw_right,
            raw_actual_ascent=raw_ascent,
            raw_actual_descent=raw_descent,
            raw_font_ascent=raw_f_ascent,
            raw_font_descent=raw_f_descent,
            advance_width_upem=adv_upem,
            lsb_upem=lsb_upem,
            rsb_upem=rsb_upem,
            ascent_upem=ascent_upem,
            descent_upem=descent_upem,
            bbox_width_upem=bbox_w_upem,
            bbox_height_upem=bbox_h_upem,
            sample_count=sample_count,
            confidence=confidence,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert DirectMetrics to dictionary representation."""
        return asdict(self)


@dataclass(frozen=True)
class ObservationRecord:
    """Immutable persistent observation record for a single glyph at a specific resolution & subpixel phase."""

    cache_key: str
    reference_id: str
    style_id: str
    code_point: int
    resolution: int
    subpixel_x: float
    subpixel_y: float
    raster_relative_path: str
    raster_sha256: str
    raster_size_bytes: int
    metrics: DirectMetrics
    created_at: str
    browser_version: str
    config_hash: str

    def __post_init__(self) -> None:
        if not self.browser_version:
            raise ValueError("ObservationRecord browser_version cannot be empty")
        for name, val in [("config_hash", self.config_hash), ("raster_sha256", self.raster_sha256), ("cache_key", self.cache_key)]:
            if not isinstance(val, str) or len(val) != 64 or not all(c in "0123456789abcdefABCDEF" for c in val):
                raise ValueError(f"ObservationRecord {name} must be a 64-char hexadecimal SHA256 digest, got: '{val}'")
        if self.raster_size_bytes <= 0:
            raise ValueError(f"ObservationRecord raster_size_bytes must be positive, got: {self.raster_size_bytes}")

    @staticmethod
    def build_cache_key(
        reference_id: str,
        style_id: str,
        code_point: int,
        browser_version: str,
        resolution: int,
        subpixel_x: float,
        subpixel_y: float,
        config_hash: str,
    ) -> str:
        """Compute authoritative deterministic cache key."""
        payload = f"{reference_id}:{style_id}:{code_point}:{browser_version}:{resolution}:{subpixel_x:.4f}:{subpixel_y:.4f}:{config_hash}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def validate_cache_key(self) -> bool:
        """Verify that cache_key matches recomputed key from record fields."""
        if not self.config_hash or not self.browser_version:
            return False
        expected = self.build_cache_key(
            reference_id=self.reference_id,
            style_id=self.style_id,
            code_point=self.code_point,
            browser_version=self.browser_version,
            resolution=self.resolution,
            subpixel_x=self.subpixel_x,
            subpixel_y=self.subpixel_y,
            config_hash=self.config_hash,
        )
        return self.cache_key == expected

    def to_dict(self) -> dict[str, Any]:
        """Convert ObservationRecord to dictionary representation with canonical metric fields."""
        d = asdict(self)
        d["metrics"] = self.metrics.to_dict()
        return d


@dataclass
class BenchmarkResult:
    """Summary of ground-truth benchmark comparison, error statistics, and resource metrics."""

    family_name: str
    style_name: str
    total_glyphs_observed: int
    total_raster_observations: int
    coverage_count: int
    expected_coverage_count: int
    missing_glyphs_count: int
    extra_glyphs_count: int
    coverage_precision: float
    coverage_recall: float
    coverage_match_rate: float
    advance_width_mean_delta_upem: float
    advance_width_max_delta_upem: float
    advance_width_rms_delta_upem: float
    lsb_mean_delta_upem: float
    lsb_max_delta_upem: float
    rsb_mean_delta_upem: float
    rsb_max_delta_upem: float
    ascent_mean_delta_upem: float
    descent_mean_delta_upem: float
    observation_time_seconds: float
    glyphs_per_second: float
    peak_rss_mb: float
    total_storage_bytes: int
    bytes_per_glyph: float
    reproducibility_manifest: dict[str, Any] = field(default_factory=dict)
