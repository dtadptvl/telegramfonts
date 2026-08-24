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


@dataclass(frozen=True)
class ConsumerEvidenceBundle:
    """Versioned, typed consumer evidence bundle bound to model hash, config hash, and held-out evidence."""

    schema_version: str
    model_canonical_hash: str
    config_hash: str
    held_out_fingerprint: str
    candidate_artifact_sha: str
    fonttools_result: FormatValidationResult
    freetype_result: RasterComparisonResult
    harfbuzz_result: ShapingTestResult
    chromium_result: ChromiumValidationResult

    def validate_bindings(
        self,
        expected_model_hash: str,
        expected_config_hash: str,
        expected_held_out_fingerprint: str,
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
        if (
            not self.candidate_artifact_sha
            or len(self.candidate_artifact_sha) != 64
            or not all(c in "0123456789abcdefABCDEF" for c in self.candidate_artifact_sha)
        ):
            errors.append(f"BUNDLE_INVALID_CANDIDATE_ARTIFACT_SHA: '{self.candidate_artifact_sha}'")
        elif self.candidate_artifact_sha != self.fonttools_result.sha256_hex:
            errors.append(
                f"BUNDLE_ARTIFACT_SHA_MISMATCH: {self.candidate_artifact_sha} != fonttools {self.fonttools_result.sha256_hex}"
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
            "candidate_artifact_sha": self.candidate_artifact_sha,
            "fonttools": _strip_host_paths(asdict(self.fonttools_result)),
            "freetype": _strip_host_paths(asdict(self.freetype_result)),
            "harfbuzz": _strip_host_paths(asdict(self.harfbuzz_result)),
            "chromium": _strip_host_paths(asdict(self.chromium_result)),
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
