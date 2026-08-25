"""Data structures and configuration models for Stage 9A fail-closed fidelity reporting."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any

from reconstruction.candidate_validator import (
    ChromiumValidationResult,
    FormatValidationResult,
    RasterComparisonResult,
    ShapingTestResult,
)


@dataclass(frozen=True)
class FidelityThresholds:
    """Immutable, versioned threshold policy for authoritative fidelity gating."""

    policy_version: str = "1.0.0"
    min_unicode_coverage_count: int = 1
    min_core_coverage_rate: float = 1.0
    min_raster_iou: float = 0.85
    max_chamfer_distance_upem: float = 25.0
    max_advance_width_delta_upem: float = 10.0
    max_metric_rms_upem: float = 15.0
    max_kerning_delta_upem: float = 15.0
    min_confidence: float = 0.80

    def validate(self) -> None:
        if self.policy_version != "1.0.0":
            raise ValueError(f"Unsupported policy_version: {self.policy_version}")
        if self.min_unicode_coverage_count < 1:
            raise ValueError("min_unicode_coverage_count must be at least 1")
        if not (0.0 <= self.min_core_coverage_rate <= 1.0):
            raise ValueError("min_core_coverage_rate must be in [0.0, 1.0]")
        if not (0.0 <= self.min_raster_iou <= 1.0):
            raise ValueError("min_raster_iou must be in [0.0, 1.0]")
        for val, name in [
            (self.max_chamfer_distance_upem, "max_chamfer_distance_upem"),
            (self.max_advance_width_delta_upem, "max_advance_width_delta_upem"),
            (self.max_metric_rms_upem, "max_metric_rms_upem"),
            (self.max_kerning_delta_upem, "max_kerning_delta_upem"),
            (self.min_confidence, "min_confidence"),
        ]:
            if not math.isfinite(val) or val < 0:
                raise ValueError(f"Invalid non-finite or negative threshold for {name}: {val}")

    def compute_policy_hash(self) -> str:
        """Compute deterministic SHA-256 digest of the threshold policy."""
        d = asdict(self)
        serialized = json.dumps(d, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProductionProducerError(Exception):
    """Raised when one or more consumer evidence producers fail during candidate production."""
    pass


@dataclass(frozen=True)
class FreeTypeSampleEvidence:
    """Per-sample FreeType raster rendering verification."""

    cache_key: str
    code_point: int
    character: str
    resolution: int
    raster_sha256: str
    raster_iou: float
    pixel_delta_count: int
    render_error: str | None = None

    def __post_init__(self) -> None:
        if not self.cache_key:
            raise ValueError("FreeTypeSampleEvidence cache_key cannot be empty")
        if self.code_point <= 0:
            raise ValueError(f"FreeTypeSampleEvidence invalid code_point: {self.code_point}")
        if self.character != chr(self.code_point):
            raise ValueError(f"FreeTypeSampleEvidence character drift: '{self.character}' != chr({self.code_point})")
        if self.resolution <= 0:
            raise ValueError(f"FreeTypeSampleEvidence resolution must be positive: {self.resolution}")
        if len(self.raster_sha256) != 64:
            raise ValueError(f"FreeTypeSampleEvidence invalid raster_sha256 length: {len(self.raster_sha256)}")
        if not math.isfinite(self.raster_iou) or not (0.0 <= self.raster_iou <= 1.0):
            raise ValueError(f"FreeTypeSampleEvidence non-finite or out-of-range raster_iou: {self.raster_iou}")
        if self.pixel_delta_count < 0:
            raise ValueError(f"FreeTypeSampleEvidence pixel_delta_count cannot be negative: {self.pixel_delta_count}")


@dataclass(frozen=True)
class HarfBuzzPositionVector:
    """Explicit 2D shaping position and advance vector for a glyph."""

    x_advance: float
    y_advance: float
    x_offset: float
    y_offset: float

    def __post_init__(self) -> None:
        if not (
            math.isfinite(self.x_advance)
            and math.isfinite(self.y_advance)
            and math.isfinite(self.x_offset)
            and math.isfinite(self.y_offset)
        ):
            raise ValueError("HarfBuzzPositionVector components must all be finite floats")


@dataclass(frozen=True)
class HarfBuzzSampleEvidence:
    """Per-sample HarfBuzz text shaping verification."""

    left_cp: int
    right_cp: int
    text: str
    in_candidate_cmap: bool
    glyph_sequence_match: bool
    glyph_ids: tuple[int, ...]
    clusters: tuple[int, ...]
    positions: tuple[HarfBuzzPositionVector, ...]
    candidate_total_advance_upem: float
    expected_total_advance_upem: float
    advance_delta_upem: float
    max_position_delta_upem: float
    error_message: str | None = None

    def __post_init__(self) -> None:
        if self.left_cp <= 0 or self.right_cp <= 0:
            raise ValueError(f"HarfBuzzSampleEvidence invalid code points: ({self.left_cp}, {self.right_cp})")
        expected_text = f"{chr(self.left_cp)}{chr(self.right_cp)}"
        if self.text != expected_text:
            raise ValueError(f"HarfBuzzSampleEvidence text drift: '{self.text}' != '{expected_text}'")
        if not (
            math.isfinite(self.candidate_total_advance_upem)
            and math.isfinite(self.expected_total_advance_upem)
            and math.isfinite(self.advance_delta_upem)
            and math.isfinite(self.max_position_delta_upem)
        ):
            raise ValueError("HarfBuzzSampleEvidence numeric fields must be finite floats")
        if self.advance_delta_upem < 0 or self.max_position_delta_upem < 0:
            raise ValueError("HarfBuzzSampleEvidence deltas cannot be negative")
        if self.error_message is None:
            if len(self.positions) != 2:
                raise ValueError(f"HarfBuzzSampleEvidence expected 2 position vectors, got {len(self.positions)}")
            if len(self.clusters) != 2 or self.clusters != (0, 1):
                raise ValueError(f"HarfBuzzSampleEvidence invalid clusters: {self.clusters}")
            if len(self.glyph_ids) != 2:
                raise ValueError(f"HarfBuzzSampleEvidence expected 2 glyph IDs, got {len(self.glyph_ids)}")


@dataclass(frozen=True)
class ChromiumGlyphSampleEvidence:
    """Per-glyph Chromium Canvas 2D direct measurement verification."""

    code_point: int
    character: str
    candidate_advance_upem: float
    expected_advance_upem: float
    advance_delta_upem: float

    def __post_init__(self) -> None:
        if self.code_point <= 0:
            raise ValueError(f"ChromiumGlyphSampleEvidence invalid code_point: {self.code_point}")
        if self.character != chr(self.code_point):
            raise ValueError(f"ChromiumGlyphSampleEvidence character drift: '{self.character}' != chr({self.code_point})")
        if not (
            math.isfinite(self.candidate_advance_upem)
            and math.isfinite(self.expected_advance_upem)
            and math.isfinite(self.advance_delta_upem)
        ):
            raise ValueError("ChromiumGlyphSampleEvidence advances and deltas must be finite floats")
        if self.advance_delta_upem < 0:
            raise ValueError("ChromiumGlyphSampleEvidence advance_delta_upem cannot be negative")


@dataclass(frozen=True)
class ChromiumPairSampleEvidence:
    """Per-pair Chromium Canvas 2D measurement and GPOS delta evaluation."""

    left_cp: int
    right_cp: int
    pair: str
    baseline_single_sum_upem: float
    candidate_pair_advance_upem: float
    expected_pair_advance_upem: float
    gpos_applied_adjustment_upem: float
    advance_delta_upem: float
    non_regression: bool

    def __post_init__(self) -> None:
        if self.left_cp <= 0 or self.right_cp <= 0:
            raise ValueError(f"ChromiumPairSampleEvidence invalid code points: ({self.left_cp}, {self.right_cp})")
        expected_pair = f"{chr(self.left_cp)}{chr(self.right_cp)}"
        if self.pair != expected_pair:
            raise ValueError(f"ChromiumPairSampleEvidence pair text drift: '{self.pair}' != '{expected_pair}'")
        if not (
            math.isfinite(self.baseline_single_sum_upem)
            and math.isfinite(self.candidate_pair_advance_upem)
            and math.isfinite(self.expected_pair_advance_upem)
            and math.isfinite(self.gpos_applied_adjustment_upem)
            and math.isfinite(self.advance_delta_upem)
        ):
            raise ValueError("ChromiumPairSampleEvidence numeric fields must be finite floats")
        if self.advance_delta_upem < 0:
            raise ValueError("ChromiumPairSampleEvidence advance_delta_upem cannot be negative")


@dataclass(frozen=True)
class BoundFontToolsEvidence:
    """FontTools format/table validation evidence bound to candidate artifact SHA-256."""

    candidate_artifact_sha: str
    result: FormatValidationResult

    def __post_init__(self) -> None:
        if not self.candidate_artifact_sha or len(self.candidate_artifact_sha) != 64:
            raise ValueError("BoundFontToolsEvidence candidate_artifact_sha must be a 64-char hex digest")
        if self.candidate_artifact_sha != self.result.sha256_hex:
            raise ValueError(
                f"BoundFontToolsEvidence SHA mismatch: {self.candidate_artifact_sha} != result {self.result.sha256_hex}"
            )


@dataclass(frozen=True)
class BoundFreeTypeEvidence:
    """FreeType raster comparison evidence bound to candidate artifact SHA-256."""

    candidate_artifact_sha: str
    result: RasterComparisonResult

    def __post_init__(self) -> None:
        if not self.candidate_artifact_sha or len(self.candidate_artifact_sha) != 64:
            raise ValueError("BoundFreeTypeEvidence candidate_artifact_sha must be a 64-char hex digest")


@dataclass(frozen=True)
class BoundHarfBuzzEvidence:
    """HarfBuzz text shaping evidence bound to candidate artifact SHA-256."""

    candidate_artifact_sha: str
    result: ShapingTestResult

    def __post_init__(self) -> None:
        if not self.candidate_artifact_sha or len(self.candidate_artifact_sha) != 64:
            raise ValueError("BoundHarfBuzzEvidence candidate_artifact_sha must be a 64-char hex digest")


@dataclass(frozen=True)
class BoundChromiumEvidence:
    """Chromium session/canvas validation evidence bound to candidate artifact SHA-256."""

    candidate_artifact_sha: str
    result: ChromiumValidationResult

    def __post_init__(self) -> None:
        if not self.candidate_artifact_sha or len(self.candidate_artifact_sha) != 64:
            raise ValueError("BoundChromiumEvidence candidate_artifact_sha must be a 64-char hex digest")


@dataclass(frozen=True)
class ConsumerEvidenceBundle:
    """Versioned, typed consumer evidence bundle bound to model hash, config hash, and candidate artifact."""

    schema_version: str
    model_canonical_hash: str
    config_hash: str
    held_out_fingerprint: str
    candidate_artifact_sha: str
    fonttools: BoundFontToolsEvidence
    freetype: BoundFreeTypeEvidence
    harfbuzz: BoundHarfBuzzEvidence
    chromium: BoundChromiumEvidence
    held_out_raster_fingerprint: str = ""
    held_out_typography_fingerprint: str = ""

    def validate_bindings(
        self,
        expected_model_hash: str,
        expected_config_hash: str,
        expected_held_out_fingerprint: str,
        expected_raster_fingerprint: str | None = None,
        expected_typography_fingerprint: str | None = None,
    ) -> list[str]:
        """Validate that the bundle is bound to the exact evaluated model, config, and held-out set."""
        errors: list[str] = []
        if self.schema_version != "1.0.0":
            errors.append(f"UNSUPPORTED_BUNDLE_SCHEMA: {self.schema_version}")
        if self.model_canonical_hash != expected_model_hash:
            errors.append(f"BUNDLE_MODEL_HASH_MISMATCH: {self.model_canonical_hash} != {expected_model_hash}")
        if self.config_hash != expected_config_hash:
            errors.append(f"BUNDLE_CONFIG_HASH_MISMATCH: {self.config_hash} != {expected_config_hash}")
        if self.held_out_fingerprint != expected_held_out_fingerprint:
            errors.append(f"BUNDLE_HELD_OUT_FP_MISMATCH: {self.held_out_fingerprint} != {expected_held_out_fingerprint}")
        if expected_raster_fingerprint:
            if not self.held_out_raster_fingerprint:
                errors.append(
                    "MISSING_RASTER_FINGERPRINT: held_out_raster_fingerprint is required when raster evidence is evaluated"
                )
            elif self.held_out_raster_fingerprint != expected_raster_fingerprint:
                errors.append(
                    f"BUNDLE_RASTER_FP_MISMATCH: {self.held_out_raster_fingerprint} != {expected_raster_fingerprint}"
                )
        if expected_typography_fingerprint:
            if not self.held_out_typography_fingerprint:
                errors.append(
                    "MISSING_TYPOGRAPHY_FINGERPRINT: held_out_typography_fingerprint is required when typography evidence is evaluated"
                )
            elif self.held_out_typography_fingerprint != expected_typography_fingerprint:
                errors.append(
                    f"BUNDLE_TYPOGRAPHY_FP_MISMATCH: {self.held_out_typography_fingerprint} != {expected_typography_fingerprint}"
                )
        if (
            not self.candidate_artifact_sha
            or len(self.candidate_artifact_sha) != 64
            or not all(c in "0123456789abcdefABCDEF" for c in self.candidate_artifact_sha)
        ):
            errors.append(f"BUNDLE_INVALID_CANDIDATE_ARTIFACT_SHA: '{self.candidate_artifact_sha}'")
        else:
            if self.fonttools.candidate_artifact_sha != self.candidate_artifact_sha:
                errors.append(
                    f"CROSS_ARTIFACT_CONSUMER_EVIDENCE: fonttools {self.fonttools.candidate_artifact_sha} != bundle {self.candidate_artifact_sha}"
                )
            if self.freetype.candidate_artifact_sha != self.candidate_artifact_sha:
                errors.append(
                    f"CROSS_ARTIFACT_CONSUMER_EVIDENCE: freetype {self.freetype.candidate_artifact_sha} != bundle {self.candidate_artifact_sha}"
                )
            if self.harfbuzz.candidate_artifact_sha != self.candidate_artifact_sha:
                errors.append(
                    f"CROSS_ARTIFACT_CONSUMER_EVIDENCE: harfbuzz {self.harfbuzz.candidate_artifact_sha} != bundle {self.candidate_artifact_sha}"
                )
            if self.chromium.candidate_artifact_sha != self.candidate_artifact_sha:
                errors.append(
                    f"CROSS_ARTIFACT_CONSUMER_EVIDENCE: chromium {self.chromium.candidate_artifact_sha} != bundle {self.candidate_artifact_sha}"
                )
        return errors

    def compute_bundle_hash(self) -> str:
        """Compute deterministic SHA-256 digest of the consumer evidence bundle, excluding host paths."""
        def _strip_host_paths(val: Any) -> Any:
            if isinstance(val, dict):
                return {
                    k: _strip_host_paths(v)
                    for k, v in val.items()
                    if k not in ("file_path", "font_path", "path", "timestamp", "created_at", "evaluation_timestamp_utc")
                }
            if isinstance(val, (list, tuple)):
                return [_strip_host_paths(item) for item in val]
            return val

        d = {
            "schema_version": self.schema_version,
            "model_canonical_hash": self.model_canonical_hash,
            "config_hash": self.config_hash,
            "held_out_fingerprint": self.held_out_fingerprint,
            "held_out_raster_fingerprint": self.held_out_raster_fingerprint,
            "held_out_typography_fingerprint": self.held_out_typography_fingerprint,
            "candidate_artifact_sha": self.candidate_artifact_sha,
            "fonttools": _strip_host_paths(asdict(self.fonttools)),
            "freetype": _strip_host_paths(asdict(self.freetype)),
            "harfbuzz": _strip_host_paths(asdict(self.harfbuzz)),
            "chromium": _strip_host_paths(asdict(self.chromium)),
        }
        serialized = json.dumps(d, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CoverageGateResult:
    status: str
    total_glyphs: int
    required_core_glyphs: int
    missing_core_glyphs: tuple[int, ...]
    coverage_rate: float
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TopologyGateResult:
    status: str
    total_contours: int
    unclosed_contours_count: int
    degenerate_segments_count: int
    topology_pass_rate: float
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GeometryRasterGateResult:
    status: str
    mean_iou: float
    min_iou: float
    mean_chamfer_upem: float
    max_chamfer_upem: float
    evaluated_glyphs_count: int
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MetricsGateResult:
    status: str
    advance_width_mean_delta_upem: float
    advance_width_max_delta_upem: float
    advance_width_rms_delta_upem: float
    lsb_max_delta_upem: float
    rsb_max_delta_upem: float
    ascent_max_delta_upem: float
    descent_max_delta_upem: float
    overall_metrics_rms_upem: float
    evaluated_metrics_count: int
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TypographyGateResult:
    status: str
    total_pairs_evaluated: int
    max_kerning_delta_upem: float
    mean_kerning_delta_upem: float
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConsumerGateResult:
    status: str
    total_consumers_evaluated: int
    fonttools_passed: bool
    freetype_passed: bool
    harfbuzz_passed: bool
    chromium_passed: bool
    consumer_bundle_hash: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class FidelityReport:
    """Authoritative fail-closed fidelity report covering coverage, topology, geometry, metrics, typography, and consumers."""

    schema_version: str = "1.0.0"
    report_id: str = ""
    model_canonical_hash: str = ""
    config_hash: str = ""
    fit_set_fingerprint: str = ""
    held_out_set_fingerprint: str = ""
    policy_hash: str = ""
    policy: dict[str, Any] = field(default_factory=dict)
    overall_status: str = "FAIL"
    coverage_gate: CoverageGateResult = field(
        default_factory=lambda: CoverageGateResult(status="FAIL", total_glyphs=0, required_core_glyphs=0, missing_core_glyphs=(), coverage_rate=0.0)
    )
    topology_gate: TopologyGateResult = field(
        default_factory=lambda: TopologyGateResult(status="FAIL", total_contours=0, unclosed_contours_count=0, degenerate_segments_count=0, topology_pass_rate=0.0)
    )
    geometry_raster_gate: GeometryRasterGateResult = field(
        default_factory=lambda: GeometryRasterGateResult(status="FAIL", mean_iou=0.0, min_iou=0.0, mean_chamfer_upem=0.0, max_chamfer_upem=0.0, evaluated_glyphs_count=0)
    )
    metrics_gate: MetricsGateResult = field(
        default_factory=lambda: MetricsGateResult(status="FAIL", advance_width_mean_delta_upem=0.0, advance_width_max_delta_upem=0.0, advance_width_rms_delta_upem=0.0, lsb_max_delta_upem=0.0, rsb_max_delta_upem=0.0, ascent_max_delta_upem=0.0, descent_max_delta_upem=0.0, overall_metrics_rms_upem=0.0, evaluated_metrics_count=0)
    )
    typography_gate: TypographyGateResult = field(
        default_factory=lambda: TypographyGateResult(status="FAIL", total_pairs_evaluated=0, max_kerning_delta_upem=0.0, mean_kerning_delta_upem=0.0)
    )
    consumer_gate: ConsumerGateResult = field(
        default_factory=lambda: ConsumerGateResult(status="FAIL", total_consumers_evaluated=0, fonttools_passed=False, freetype_passed=False, harfbuzz_passed=False, chromium_passed=False)
    )
    failure_reasons: list[str] = field(default_factory=list)
    evaluation_timestamp_utc: str = ""

    def compute_report_hash(self) -> str:
        """Calculate deterministic SHA-256 hash digest of the fidelity report excluding timestamp."""
        d = self.to_dict()
        d_clean = {k: v for k, v in d.items() if k != "evaluation_timestamp_utc"}
        serialized = json.dumps(d_clean, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "report_id": self.report_id,
            "model_canonical_hash": self.model_canonical_hash,
            "config_hash": self.config_hash,
            "fit_set_fingerprint": self.fit_set_fingerprint,
            "held_out_set_fingerprint": self.held_out_set_fingerprint,
            "policy_hash": self.policy_hash,
            "policy": self.policy,
            "overall_status": self.overall_status,
            "coverage_gate": asdict(self.coverage_gate),
            "topology_gate": asdict(self.topology_gate),
            "geometry_raster_gate": asdict(self.geometry_raster_gate),
            "metrics_gate": asdict(self.metrics_gate),
            "typography_gate": asdict(self.typography_gate),
            "consumer_gate": asdict(self.consumer_gate),
            "failure_reasons": sorted(self.failure_reasons),
            "evaluation_timestamp_utc": self.evaluation_timestamp_utc,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, indent=2, ensure_ascii=False)
