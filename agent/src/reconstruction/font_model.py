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
    anchors: tuple[tuple[str, float, float], ...] = ()

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
        """Strictly validate glyph metrics, code point, character identity, and contour topology."""
        if not (0 <= self.code_point <= 0x10FFFF):
            raise ValueError(f"Invalid Unicode code point: {self.code_point}")
        if self.character != chr(self.code_point):
            raise ValueError(f"Glyph character mismatch: '{self.character}' != chr({self.code_point})")

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

        if self.advance_width_upem < 0:
            raise ValueError(f"Negative advance width: {self.advance_width_upem}")
        if not (0.1 <= self.confidence <= 1.0):
            raise ValueError(f"Invalid glyph confidence outside [0.1, 1.0]: {self.confidence}")

        if len(self.bounding_box_upem) != 4:
            raise ValueError(f"Bounding box must have 4 coordinates, got {len(self.bounding_box_upem)}")
        for b in self.bounding_box_upem:
            if not math.isfinite(b):
                raise ValueError(f"Non-finite bounding box coordinate in glyph: {b}")

        if not self.observation_fingerprints:
            raise ValueError(f"Empty observation fingerprints for glyph {self.code_point}")
        for fp in self.observation_fingerprints:
            if not isinstance(fp, str) or len(fp) != 64 or not all(c in "0123456789abcdefABCDEF" for c in fp):
                raise ValueError(f"Malformed observation fingerprint in glyph {self.code_point}: {fp}")

        for anchor in self.anchors:
            if not isinstance(anchor, tuple) or len(anchor) != 3:
                raise ValueError(f"Malformed anchor entry in glyph {self.code_point}: {anchor!r}")
            name, ax, ay = anchor
            if not str(name).strip() or not (math.isfinite(float(ax)) and math.isfinite(float(ay))):
                raise ValueError(f"Invalid anchor in glyph {self.code_point}: {anchor!r}")

        # Non-whitespace glyphs must have contours
        if self.code_point != 0x20 and not self.contours:
            raise ValueError(f"Non-whitespace glyph {self.code_point} must have at least one contour")

        for c_idx, c in enumerate(self.contours):
            if not c.segments:
                raise ValueError(f"Empty contour {c_idx} in glyph {self.code_point}")
            if not c.is_closed:
                raise ValueError(f"Unclosed contour {c_idx} in glyph {self.code_point}")
            for s in c.segments:
                if s.approximate_length() < 1e-4:
                    raise ValueError(f"Degenerate segment (< 1e-4) in glyph {self.code_point}")
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
            "anchors": sorted(
                [[str(name), round(float(ax), 2), round(float(ay), 2)] for name, ax, ay in self.anchors]
            ),
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
        required_keys = ["code_point", "character", "advance_width_upem", "lsb_upem", "rsb_upem", "ascent_upem", "descent_upem", "bounding_box_upem", "observation_fingerprints"]
        for k in required_keys:
            if k not in d:
                raise ValueError(f"Missing required field in CalibratedGlyph: {k}")

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

        bbox_raw = d["bounding_box_upem"]
        bbox = (float(bbox_raw[0]), float(bbox_raw[1]), float(bbox_raw[2]), float(bbox_raw[3]))

        glyph = cls(
            code_point=int(d["code_point"]),
            character=str(d["character"]),
            advance_width_upem=float(d["advance_width_upem"]),
            lsb_upem=float(d["lsb_upem"]),
            rsb_upem=float(d["rsb_upem"]),
            ascent_upem=float(d["ascent_upem"]),
            descent_upem=float(d["descent_upem"]),
            bounding_box_upem=bbox,
            contours=contours,
            confidence=float(d.get("confidence", 1.0)),
            observation_fingerprints=tuple(d["observation_fingerprints"]),
            anchors=tuple(
                (str(a[0]), float(a[1]), float(a[2])) for a in d.get("anchors", [])
            ),
        )
        glyph.validate()
        return glyph


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
        required = [
            "units_per_em", "ascent_upem", "descent_upem", "line_gap_upem",
            "cap_height_upem", "x_height_upem", "max_advance_width_upem",
            "avg_char_width_upem", "underline_position_upem", "underline_thickness_upem",
        ]
        for k in required:
            if k not in d:
                raise ValueError(f"Missing required field in GlobalFontMetrics: {k}")
        metrics = cls(
            units_per_em=int(d["units_per_em"]),
            ascent_upem=float(d["ascent_upem"]),
            descent_upem=float(d["descent_upem"]),
            line_gap_upem=float(d["line_gap_upem"]),
            cap_height_upem=float(d["cap_height_upem"]),
            x_height_upem=float(d["x_height_upem"]),
            max_advance_width_upem=float(d["max_advance_width_upem"]),
            avg_char_width_upem=float(d["avg_char_width_upem"]),
            underline_position_upem=float(d["underline_position_upem"]),
            underline_thickness_upem=float(d["underline_thickness_upem"]),
        )
        metrics.validate()
        return metrics


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
        """Strictly validate entire font model for integrity and completeness."""
        if self.schema_version != "1.0.0":
            raise ValueError(f"Unsupported schema_version: '{self.schema_version}' (expected '1.0.0')")
        if not self.family_name or not isinstance(self.family_name, str):
            raise ValueError("FontModel family_name cannot be empty")
        if not self.style_name or not isinstance(self.style_name, str):
            raise ValueError("FontModel style_name cannot be empty")
        if not self.reference_id or not isinstance(self.reference_id, str):
            raise ValueError("FontModel reference_id cannot be empty")
        if not self.style_id or not isinstance(self.style_id, str):
            raise ValueError("FontModel style_id cannot be empty")
        if not self.browser_version or not isinstance(self.browser_version, str):
            raise ValueError("FontModel browser_version cannot be empty")
        for name, val in [("config_hash", self.config_hash), ("calibration_fingerprint", self.calibration_fingerprint)]:
            if not isinstance(val, str) or len(val) != 64 or not all(c in "0123456789abcdefABCDEF" for c in val):
                raise ValueError(f"FontModel {name} must be a 64-char hex digest, got: '{val}'")
        if self.fit_observations_count <= 0:
            raise ValueError(f"FontModel fit_observations_count must be positive, got: {self.fit_observations_count}")
        if not self.glyphs:
            raise ValueError("FontModel glyphs dictionary cannot be empty")

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
        """Compute authoritative SHA-256 hash over the canonical JSON representation."""
        self.validate()
        canonical_json = self.to_canonical_json()
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    def to_canonical_dict(self) -> dict[str, Any]:
        """Export deterministic dictionary representation with sorted keys and lists."""
        return {
            "schema_version": self.schema_version,
            "family_name": self.family_name,
            "style_name": self.style_name,
            "reference_id": self.reference_id,
            "style_id": self.style_id,
            "config_hash": self.config_hash,
            "browser_version": self.browser_version,
            "fit_observations_count": self.fit_observations_count,
            "calibration_fingerprint": self.calibration_fingerprint,
            "fit_provenance": self.fit_provenance,
            "metrics": self.metrics.to_canonical_dict(),
            "feature_tags": sorted(list(self.feature_tags)),
            "kerning_pairs": [
                {
                    "left_cp": l_cp,
                    "right_cp": r_cp,
                    "kerning_upem": self.kerning_pairs[(l_cp, r_cp)],
                }
                for (l_cp, r_cp) in sorted(self.kerning_pairs.keys())
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
        """Deserialize and strictly validate from dictionary representation."""
        required_keys = [
            "schema_version", "family_name", "style_name", "reference_id",
            "style_id", "metrics", "glyphs", "config_hash",
            "browser_version", "fit_observations_count", "calibration_fingerprint"
        ]
        for k in required_keys:
            if k not in d or d[k] is None or d[k] == "":
                raise ValueError(f"Missing required field in CanonicalFontModel: {k}")

        metrics = GlobalFontMetrics.from_dict(d["metrics"])
        glyphs: dict[int, CalibratedGlyph] = {}
        seen_cps: set[int] = set()
        for g_data in d.get("glyphs", []):
            g = CalibratedGlyph.from_dict(g_data)
            if g.code_point in seen_cps:
                raise ValueError(f"Duplicate glyph entry for code point {g.code_point}")
            seen_cps.add(g.code_point)
            glyphs[g.code_point] = g

        kerning_pairs: dict[tuple[int, int], int] = {}
        seen_pairs: set[tuple[int, int]] = set()
        for k_data in d.get("kerning_pairs", []):
            pair_key = (int(k_data["left_cp"]), int(k_data["right_cp"]))
            if pair_key in seen_pairs:
                raise ValueError(f"Duplicate kerning pair entry for {pair_key}")
            seen_pairs.add(pair_key)
            kerning_pairs[pair_key] = int(k_data["kerning_upem"])

        model = cls(
            schema_version=str(d["schema_version"]),
            family_name=str(d["family_name"]),
            style_name=str(d["style_name"]),
            reference_id=str(d["reference_id"]),
            style_id=str(d["style_id"]),
            metrics=metrics,
            glyphs=glyphs,
            kerning_pairs=kerning_pairs,
            feature_tags=tuple(d.get("feature_tags", ())),
            config_hash=str(d["config_hash"]),
            browser_version=str(d["browser_version"]),
            fit_observations_count=int(d["fit_observations_count"]),
            calibration_fingerprint=str(d["calibration_fingerprint"]),
            fit_provenance=str(d.get("fit_provenance", "browser_observed_multi_res")),
        )
        model.validate()
        return model

    @classmethod
    def from_canonical_json(cls, raw_json: str) -> CanonicalFontModel:
        """Deserialize from canonical JSON string."""
        data = json.loads(raw_json)
        return cls.from_canonical_dict(data)

    def seal(self) -> "SealedFontModel":
        """Seal this model into a deeply immutable attested handle."""
        return SealedFontModel.seal(self)


@dataclass(frozen=True)
class SealedFontModel:
    """Deeply immutable attested canonical model handle.

    Holds only the validated canonical JSON bytes and their SHA-256 seal.
    ``unwrap()`` always materializes a FRESH validated model from the sealed
    bytes: mutation of any unwrapped copy can never alter the seal, and any
    tampering with the sealed bytes is detected by ``verify()`` before use.
    TTF and OTF builds must bind to the identical sealed model hash.
    """

    canonical_json: str
    model_hash: str
    schema_version: str = "1.0.0"

    @classmethod
    def seal(cls, model: CanonicalFontModel) -> "SealedFontModel":
        model.validate()
        canonical = model.to_canonical_json()
        return cls(
            canonical_json=canonical,
            model_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            schema_version=model.schema_version,
        )

    def verify(self) -> str:
        """Recompute the seal hash; raise on any drift (fail closed)."""
        recomputed = hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()
        if recomputed != self.model_hash:
            raise ValueError("SEALED_FONT_MODEL_HASH_DRIFT")
        return recomputed

    def unwrap(self) -> CanonicalFontModel:
        """Materialize a fresh validated model from the sealed bytes."""
        self.verify()
        return CanonicalFontModel.from_canonical_json(self.canonical_json)
