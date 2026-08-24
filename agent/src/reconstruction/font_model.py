"""Canonical FontModel: Versioned, strictly validated, and deterministically hashed font representation.

Packages calibrated glyph geometries, global font design metrics, typography, and
cryptographic observation/calibration fingerprints into an immutable production model.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any

from reconstruction.models import Contour, CubicSegment, LineSegment, Point2D


@dataclass
class CalibratedGlyph:
    """Calibrated glyph representation with master contour geometry and design-space metrics."""

    code_point: int
    character: str
    advance_width_upem: float
    lsb_upem: float
    rsb_upem: float
    ascent_upem: float
    descent_upem: float
    bounding_box_upem: tuple[float, float, float, float]
    contours: list[Contour] = field(default_factory=list)
    confidence: float = 1.0
    observation_fingerprints: tuple[str, ...] = ()

    @property
    def total_contours(self) -> int:
        return len(self.contours)

    @property
    def outer_contours_count(self) -> int:
        return sum(1 for c in self.contours if not c.is_hole)

    @property
    def holes_count(self) -> int:
        return sum(1 for c in self.contours if c.is_hole)

    def validate(self) -> None:
        """Validate glyph metrics, code point, and contour topology."""
        if not (0 <= self.code_point <= 0x10FFFF):
            raise ValueError(f"Invalid Unicode code point: {self.code_point}")
        for val, name in [
            (self.advance_width_upem, "advance_width_upem"),
            (self.lsb_upem, "lsb_upem"),
            (self.rsb_upem, "rsb_upem"),
            (self.ascent_upem, "ascent_upem"),
            (self.descent_upem, "descent_upem"),
            (self.confidence, "confidence"),
        ]:
            if not math.isfinite(val):
                raise ValueError(f"Non-finite value in glyph {name}: {val}")

        for b in self.bounding_box_upem:
            if not math.isfinite(b):
                raise ValueError(f"Non-finite bounding box coordinate in glyph: {b}")

        for c_idx, c in enumerate(self.contours):
            if not c.segments:
                raise ValueError(f"Empty contour {c_idx} in glyph {self.code_point}")
            for s in c.segments:
                if isinstance(s, CubicSegment):
                    for pt in (s.p0, s.p1, s.p2, s.p3):
                        if not math.isfinite(pt.x) or not math.isfinite(pt.y):
                            raise ValueError(f"Non-finite cubic control point in glyph {self.code_point}: {pt}")
                elif isinstance(s, LineSegment):
                    for pt in (s.p0, s.p1):
                        if not math.isfinite(pt.x) or not math.isfinite(pt.y):
                            raise ValueError(f"Non-finite line point in glyph {self.code_point}: {pt}")

    def to_canonical_dict(self) -> dict[str, Any]:
        """Export deterministic dictionary representation with stable rounded float precisions."""
        return {
            "code_point": self.code_point,
            "character": self.character,
            "advance_width_upem": round(self.advance_width_upem, 2),
            "lsb_upem": round(self.lsb_upem, 2),
            "rsb_upem": round(self.rsb_upem, 2),
            "ascent_upem": round(self.ascent_upem, 2),
            "descent_upem": round(self.descent_upem, 2),
            "bounding_box_upem": [round(b, 2) for b in self.bounding_box_upem],
            "confidence": round(self.confidence, 4),
            "observation_fingerprints": sorted(list(self.observation_fingerprints)),
            "contours": [
                {
                    "is_hole": c.is_hole,
                    "parent_index": c.parent_index,
                    "area_upem": round(c.area_upem, 2),
                    "segments": [
                        {
                            "type": "cubic" if isinstance(s, CubicSegment) else "line",
                            "p0": [round(s.p0.x, 3), round(s.p0.y, 3)],
                            "p1": [round(s.p1.x, 3), round(s.p1.y, 3)],
                            **({
                                "p2": [round(s.p2.x, 3), round(s.p2.y, 3)],
                                "p3": [round(s.p3.x, 3), round(s.p3.y, 3)],
                            } if isinstance(s, CubicSegment) else {}),
                        }
                        for s in c.segments
                    ],
                }
                for c in self.contours
            ],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CalibratedGlyph:
        contours: list[Contour] = []
        for c_data in d.get("contours", []):
            segments: list[CubicSegment | LineSegment] = []
            for s_data in c_data.get("segments", []):
                s_type = s_data.get("type", "line")
                p0 = Point2D(float(s_data["p0"][0]), float(s_data["p0"][1]))
                p1 = Point2D(float(s_data["p1"][0]), float(s_data["p1"][1]))
                if s_type == "cubic":
                    p2 = Point2D(float(s_data["p2"][0]), float(s_data["p2"][1]))
                    p3 = Point2D(float(s_data["p3"][0]), float(s_data["p3"][1]))
                    segments.append(CubicSegment(p0, p1, p2, p3))
                else:
                    segments.append(LineSegment(p0, p1))
            contours.append(
                Contour(
                    segments=segments,
                    is_hole=bool(c_data.get("is_hole", False)),
                    parent_index=c_data.get("parent_index"),
                    area_upem=float(c_data.get("area_upem", 0.0)),
                )
            )

        bbox_raw = d.get("bounding_box_upem", [0.0, 0.0, 0.0, 0.0])
        bbox = (float(bbox_raw[0]), float(bbox_raw[1]), float(bbox_raw[2]), float(bbox_raw[3]))

        return cls(
            code_point=int(d["code_point"]),
            character=str(d.get("character", chr(d["code_point"]))),
            advance_width_upem=float(d["advance_width_upem"]),
            lsb_upem=float(d.get("lsb_upem", 0.0)),
            rsb_upem=float(d.get("rsb_upem", 0.0)),
            ascent_upem=float(d.get("ascent_upem", 0.0)),
            descent_upem=float(d.get("descent_upem", 0.0)),
            bounding_box_upem=bbox,
            contours=contours,
            confidence=float(d.get("confidence", 1.0)),
            observation_fingerprints=tuple(d.get("observation_fingerprints", ())),
        )


@dataclass(frozen=True)
class GlobalFontMetrics:
    """Global font typography metrics in UPEM design space units."""

    units_per_em: int = 1000
    ascent_upem: float = 800.0
    descent_upem: float = -200.0
    line_gap_upem: float = 0.0
    cap_height_upem: float = 700.0
    x_height_upem: float = 500.0
    max_advance_width_upem: float = 1000.0
    avg_char_width_upem: float = 500.0
    underline_position_upem: float = -100.0
    underline_thickness_upem: float = 50.0

    def validate(self) -> None:
        if self.units_per_em <= 0:
            raise ValueError(f"UPEM must be positive, got {self.units_per_em}")
        for val, name in [
            (self.ascent_upem, "ascent_upem"),
            (self.descent_upem, "descent_upem"),
            (self.line_gap_upem, "line_gap_upem"),
            (self.cap_height_upem, "cap_height_upem"),
            (self.x_height_upem, "x_height_upem"),
            (self.max_advance_width_upem, "max_advance_width_upem"),
            (self.avg_char_width_upem, "avg_char_width_upem"),
            (self.underline_position_upem, "underline_position_upem"),
            (self.underline_thickness_upem, "underline_thickness_upem"),
        ]:
            if not math.isfinite(val):
                raise ValueError(f"Non-finite global metric {name}: {val}")

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "units_per_em": self.units_per_em,
            "ascent_upem": round(self.ascent_upem, 2),
            "descent_upem": round(self.descent_upem, 2),
            "line_gap_upem": round(self.line_gap_upem, 2),
            "cap_height_upem": round(self.cap_height_upem, 2),
            "x_height_upem": round(self.x_height_upem, 2),
            "max_advance_width_upem": round(self.max_advance_width_upem, 2),
            "avg_char_width_upem": round(self.avg_char_width_upem, 2),
            "underline_position_upem": round(self.underline_position_upem, 2),
            "underline_thickness_upem": round(self.underline_thickness_upem, 2),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GlobalFontMetrics:
        return cls(
            units_per_em=int(d.get("units_per_em", 1000)),
            ascent_upem=float(d.get("ascent_upem", 800.0)),
            descent_upem=float(d.get("descent_upem", -200.0)),
            line_gap_upem=float(d.get("line_gap_upem", 0.0)),
            cap_height_upem=float(d.get("cap_height_upem", 700.0)),
            x_height_upem=float(d.get("x_height_upem", 500.0)),
            max_advance_width_upem=float(d.get("max_advance_width_upem", 1000.0)),
            avg_char_width_upem=float(d.get("avg_char_width_upem", 500.0)),
            underline_position_upem=float(d.get("underline_position_upem", -100.0)),
            underline_thickness_upem=float(d.get("underline_thickness_upem", 50.0)),
        )


@dataclass
class CanonicalFontModel:
    """Versioned, canonical production model for an entire observed font family/style."""

    schema_version: str = "1.0.0"
    family_name: str = ""
    style_name: str = ""
    reference_id: str = ""
    style_id: str = ""
    metrics: GlobalFontMetrics = field(default_factory=GlobalFontMetrics)
    glyphs: dict[int, CalibratedGlyph] = field(default_factory=dict)
    kerning_pairs: dict[tuple[int, int], int] = field(default_factory=dict)
    feature_tags: tuple[str, ...] = ()
    config_hash: str = ""
    browser_version: str = ""
    fit_observations_count: int = 0
    calibration_fingerprint: str = ""
    fit_provenance: str = "browser_observed_multi_res"

    def validate(self) -> None:
        """Validate entire font model for integrity and completeness."""
        if not self.family_name:
            raise ValueError("FontModel family_name cannot be empty")
        if not self.style_name:
            raise ValueError("FontModel style_name cannot be empty")
        self.metrics.validate()
        for cp, g in self.glyphs.items():
            if g.code_point != cp:
                raise ValueError(f"Glyph key mismatch: {cp} != {g.code_point}")
            g.validate()
        for (l_cp, r_cp), kern in self.kerning_pairs.items():
            if not (0 <= l_cp <= 0x10FFFF) or not (0 <= r_cp <= 0x10FFFF):
                raise ValueError(f"Invalid kerning pair code points: ({l_cp}, {r_cp})")
            if not math.isfinite(kern):
                raise ValueError(f"Non-finite kerning value for ({l_cp}, {r_cp}): {kern}")

    def compute_canonical_hash(self) -> str:
        """Compute authoritative deterministic SHA-256 hash digest of the production font model."""
        self.validate()
        canon_json = self.to_canonical_json()
        return hashlib.sha256(canon_json.encode("utf-8")).hexdigest()

    def to_canonical_dict(self) -> dict[str, Any]:
        """Serialize model into sorted, canonical dictionary strictly excluding timestamps and paths."""
        return {
            "schema_version": self.schema_version,
            "family_name": self.family_name,
            "style_name": self.style_name,
            "reference_id": self.reference_id,
            "style_id": self.style_id,
            "metrics": self.metrics.to_canonical_dict(),
            "config_hash": self.config_hash,
            "browser_version": self.browser_version,
            "fit_observations_count": self.fit_observations_count,
            "calibration_fingerprint": self.calibration_fingerprint,
            "fit_provenance": self.fit_provenance,
            "feature_tags": sorted(list(self.feature_tags)),
            "kerning_pairs": [
                {
                    "left_cp": left_cp,
                    "right_cp": right_cp,
                    "kerning_upem": int(val),
                }
                for (left_cp, right_cp), val in sorted(self.kerning_pairs.items())
            ],
            "glyphs": [
                self.glyphs[cp].to_canonical_dict()
                for cp in sorted(self.glyphs.keys())
            ],
        }

    def to_canonical_json(self) -> str:
        """Generate strictly sorted, deterministic UTF-8 JSON string."""
        data = self.to_canonical_dict()
        return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_canonical_dict(cls, d: dict[str, Any]) -> CanonicalFontModel:
        """Deserialize from dictionary representation."""
        metrics = GlobalFontMetrics.from_dict(d.get("metrics", {}))
        glyphs: dict[int, CalibratedGlyph] = {}
        for g_data in d.get("glyphs", []):
            g = CalibratedGlyph.from_dict(g_data)
            glyphs[g.code_point] = g

        kerning_pairs: dict[tuple[int, int], int] = {}
        for k_data in d.get("kerning_pairs", []):
            pair_key = (int(k_data["left_cp"]), int(k_data["right_cp"]))
            kerning_pairs[pair_key] = int(k_data["kerning_upem"])

        model = cls(
            schema_version=str(d.get("schema_version", "1.0.0")),
            family_name=str(d.get("family_name", "")),
            style_name=str(d.get("style_name", "")),
            reference_id=str(d.get("reference_id", "")),
            style_id=str(d.get("style_id", "")),
            metrics=metrics,
            glyphs=glyphs,
            kerning_pairs=kerning_pairs,
            feature_tags=tuple(d.get("feature_tags", ())),
            config_hash=str(d.get("config_hash", "")),
            browser_version=str(d.get("browser_version", "")),
            fit_observations_count=int(d.get("fit_observations_count", 0)),
            calibration_fingerprint=str(d.get("calibration_fingerprint", "")),
            fit_provenance=str(d.get("fit_provenance", "browser_observed_multi_res")),
        )
        model.validate()
        return model

    @classmethod
    def from_canonical_json(cls, raw_json: str) -> CanonicalFontModel:
        """Deserialize from canonical JSON string."""
        data = json.loads(raw_json)
        return cls.from_canonical_dict(data)
