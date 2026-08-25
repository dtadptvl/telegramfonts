"""Vietnamese extension boundary (VIETNAMESE mode only).

Rules:
- ORIGINAL mode never constructs or invokes this module (runner-enforced).
- Existing Vietnamese glyphs/metrics/behavior are preserved; AI work applies
  only to missing coverage.
- AI candidates pass strict deterministic validation; forged, non-finite, or
  incomplete output fails closed with no publish/archive.
- The extension binds AI model/version/prompt/config/source hashes into an
  immutable provenance record.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Protocol, Sequence

from reconstruction.font_model import CalibratedGlyph, CanonicalFontModel
from reconstruction.models import Contour, LineSegment, Point2D

# Canonical Vietnamese extension set: precomposed Latin Extended Additional,
# D-stroke, and the combining marks used by Vietnamese tone/diacritic stacks.
VIETNAMESE_PRECOMPOSED: tuple[int, ...] = tuple(
    sorted(
        list(range(0x1EA0, 0x1EF9 + 1))
        + [0x0110, 0x0111, 0x01A0, 0x01A1, 0x01AF, 0x01B0, 0x02C6, 0x0306, 0x031B]
    )
)
VIETNAMESE_COMBINING_MARKS: tuple[int, ...] = (0x0300, 0x0301, 0x0303, 0x0309, 0x0323)
VIETNAMESE_REQUIRED_CODEPOINTS: tuple[int, ...] = tuple(
    sorted(set(VIETNAMESE_PRECOMPOSED) | set(VIETNAMESE_COMBINING_MARKS))
)

VIETNAMESE_SHAPING_CORPUS: tuple[str, ...] = (
    "Tiếng Việt",
    "Đẹp Xinh",
    "Hòa Bình",
    "Ắt Có",
    "Ước Mơ",
)

MARK_CODEPOINT_SET = frozenset(VIETNAMESE_COMBINING_MARKS)


class VietnameseAIIntegrityError(ValueError):
    """Raised when AI output is forged, non-finite, or incomplete (fail-closed)."""


@dataclass(frozen=True)
class AICandidateSpec:
    """One AI-generated candidate glyph for missing Vietnamese coverage."""

    code_point: int
    contours: tuple[tuple[tuple[float, float], ...], ...]
    advance_width_upem: float
    lsb_upem: float
    rsb_upem: float
    ascent_upem: float
    descent_upem: float
    anchors: tuple[tuple[str, float, float], ...] = ()

    def validate(self) -> None:
        values = [
            self.advance_width_upem,
            self.lsb_upem,
            self.rsb_upem,
            self.ascent_upem,
            self.descent_upem,
        ]
        if not all(math.isfinite(v) for v in values):
            raise VietnameseAIIntegrityError(f"VI_AI_NON_FINITE_METRICS_CP_{self.code_point}")
        if not (0.0 < self.advance_width_upem <= 4000.0):
            raise VietnameseAIIntegrityError(f"VI_AI_INVALID_ADVANCE_CP_{self.code_point}")
        if not self.contours:
            if self.code_point not in MARK_CODEPOINT_SET and self.code_point != 0x20:
                raise VietnameseAIIntegrityError(f"VI_AI_NO_CONTOURS_CP_{self.code_point}")
            return
        for contour_points in self.contours:
            if len(contour_points) < 3:
                raise VietnameseAIIntegrityError(f"VI_AI_DEGENERATE_CONTOUR_CP_{self.code_point}")
            for x, y in contour_points:
                if not (math.isfinite(x) and math.isfinite(y)):
                    raise VietnameseAIIntegrityError(f"VI_AI_NON_FINITE_POINT_CP_{self.code_point}")
                if abs(x) > 4000.0 or abs(y) > 4000.0:
                    raise VietnameseAIIntegrityError(f"VI_AI_OUT_OF_BOUNDS_CP_{self.code_point}")
            area = 0.0
            pts = list(contour_points)
            for i in range(len(pts)):
                x0, y0 = pts[i]
                x1, y1 = pts[(i + 1) % len(pts)]
                area += x0 * y1 - x1 * y0
            if abs(area) < 1.0:
                raise VietnameseAIIntegrityError(f"VI_AI_ZERO_AREA_CONTOUR_CP_{self.code_point}")
        if self.code_point in MARK_CODEPOINT_SET and not self.anchors:
            raise VietnameseAIIntegrityError(f"VI_AI_MARK_MISSING_ANCHORS_CP_{self.code_point}")
        for name, ax, ay in self.anchors:
            if not name.strip() or not (math.isfinite(ax) and math.isfinite(ay)):
                raise VietnameseAIIntegrityError(f"VI_AI_INVALID_ANCHOR_CP_{self.code_point}")


class VietnameseGlyphAIProvider(Protocol):
    """Injectable AI candidate provider. Never called for ORIGINAL mode."""

    model_id: str
    model_version: str

    def prompt_hash(self) -> str:
        """Deterministic hash of the generation prompt/config (no secrets)."""

    async def generate_candidates(
        self, request: dict
    ) -> Sequence[AICandidateSpec]:
        """Generate candidates for exactly the requested missing code points."""


@dataclass(frozen=True)
class VietnameseExtensionBinding:
    """Immutable AI/provenance binding for one extension outcome."""

    mode: str  # always VIETNAMESE here
    ai_model_id: str
    ai_model_version: str
    ai_prompt_hash: str
    config_hash: str
    source_hash: str
    extended_codepoints: tuple[int, ...]
    preserved_codepoints: tuple[int, ...]

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "ai_model_id": self.ai_model_id,
            "ai_model_version": self.ai_model_version,
            "ai_prompt_hash": self.ai_prompt_hash,
            "config_hash": self.config_hash,
            "source_hash": self.source_hash,
            "extended_codepoints": list(self.extended_codepoints),
            "preserved_codepoints": list(self.preserved_codepoints),
        }

    def compute_binding_hash(self) -> str:
        serialized = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def missing_vietnamese_codepoints(model: CanonicalFontModel) -> tuple[int, ...]:
    return tuple(cp for cp in VIETNAMESE_REQUIRED_CODEPOINTS if cp not in model.glyphs)


class VietnameseExtensionService:
    """Deterministic, fail-closed Vietnamese extension over missing coverage."""

    def __init__(
        self,
        ai_provider: VietnameseGlyphAIProvider | None,
        config_hash: str,
        source_hash: str,
    ) -> None:
        self.ai_provider = ai_provider
        self.config_hash = config_hash
        self.source_hash = source_hash

    async def extend(self, model: CanonicalFontModel) -> tuple[CanonicalFontModel, VietnameseExtensionBinding]:
        missing = missing_vietnamese_codepoints(model)
        preserved = tuple(cp for cp in VIETNAMESE_REQUIRED_CODEPOINTS if cp in model.glyphs)

        binding = VietnameseExtensionBinding(
            mode="VIETNAMESE",
            ai_model_id=getattr(self.ai_provider, "model_id", "") if missing else "",
            ai_model_version=getattr(self.ai_provider, "model_version", "") if missing else "",
            ai_prompt_hash=self.ai_provider.prompt_hash() if (missing and self.ai_provider) else "",
            config_hash=self.config_hash,
            source_hash=self.source_hash,
            extended_codepoints=(),
            preserved_codepoints=preserved,
        )
        if not missing:
            # Complete Vietnamese coverage already present: preserve everything;
            # zero AI work.
            return model, binding

        if self.ai_provider is None:
            raise VietnameseAIIntegrityError("VI_AI_PROVIDER_UNAVAILABLE")

        request = {
            "mode": "VIETNAMESE",
            "family_name": model.family_name,
            "style_name": model.style_name,
            "config_hash": self.config_hash,
            "source_hash": self.source_hash,
            "missing_codepoints": list(missing),
            "units_per_em": model.metrics.units_per_em,
        }
        candidates = await self.ai_provider.generate_candidates(request)
        produced = {c.code_point for c in candidates}
        if produced != set(missing):
            raise VietnameseAIIntegrityError("VI_AI_INCOMPLETE_COVERAGE")

        extended_glyphs = dict(model.glyphs)
        for spec in sorted(candidates, key=lambda c: c.code_point):
            spec.validate()
            contours: list[Contour] = []
            for contour_points in spec.contours:
                pts = [Point2D(float(x), float(y)) for x, y in contour_points]
                segments = [
                    LineSegment(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))
                ]
                contours.append(Contour(segments=segments, is_hole=False))
            xs = [p.x for c in contours for s in c.segments for p in (s.p0, s.p1)]
            ys = [p.y for c in contours for s in c.segments for p in (s.p0, s.p1)]
            bbox = (min(xs), min(ys), max(xs), max(ys)) if xs else (0.0, 0.0, 0.0, 0.0)
            extended_glyphs[spec.code_point] = CalibratedGlyph(
                code_point=spec.code_point,
                character=chr(spec.code_point),
                advance_width_upem=spec.advance_width_upem,
                lsb_upem=spec.lsb_upem,
                rsb_upem=spec.rsb_upem,
                ascent_upem=spec.ascent_upem,
                descent_upem=spec.descent_upem,
                bounding_box_upem=bbox,
                contours=contours,
                confidence=1.0,
                observation_fingerprints=(
                    hashlib.sha256(f"vi_ai:{spec.code_point}:{self.source_hash}".encode("utf-8")).hexdigest(),
                ),
            )

        extended_model = replace(model, glyphs=extended_glyphs)
        final_binding = VietnameseExtensionBinding(
            mode="VIETNAMESE",
            ai_model_id=self.ai_provider.model_id,
            ai_model_version=self.ai_provider.model_version,
            ai_prompt_hash=self.ai_provider.prompt_hash(),
            config_hash=self.config_hash,
            source_hash=self.source_hash,
            extended_codepoints=tuple(sorted(produced)),
            preserved_codepoints=preserved,
        )
        return extended_model, final_binding


def validate_nfc_nfd_coverage(model: CanonicalFontModel) -> list[str]:
    """Every precomposed Vietnamese glyph must have its NFD parts in the cmap."""
    failures: list[str] = []
    for cp in VIETNAMESE_PRECOMPOSED:
        if cp not in model.glyphs:
            continue
        decomposed = unicodedata.normalize("NFD", chr(cp))
        for part in decomposed:
            if ord(part) != cp and ord(part) not in model.glyphs:
                failures.append(f"VI_NFC_NFD_GAP_CP_{cp:04X}")
    return failures


def validate_candidate_font_bytes(font_bytes: bytes, model: CanonicalFontModel) -> list[str]:
    """Authoritative post-build checks: corpus shaping, clipping, spacing."""
    import uharfbuzz as hb

    failures: list[str] = []
    try:
        blob = hb.Blob(font_bytes)
        face = hb.Face(blob)
        font = hb.Font(face)
    except Exception:
        return ["VI_FONT_LOAD_FAILED"]

    for text in VIETNAMESE_SHAPING_CORPUS:
        buf = hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        hb.shape(font, buf)
        infos = buf.glyph_infos
        positions = buf.glyph_positions
        if not infos or any(i.codepoint == 0 for i in infos):
            failures.append("VI_CORPUS_SHAPING_NOTDEF")
            continue
        if not all(
            math.isfinite(p.x_advance) and math.isfinite(p.y_advance) for p in positions
        ):
            failures.append("VI_CORPUS_SHAPING_NON_FINITE")

    upem = model.metrics.units_per_em
    clip_limit = max(abs(model.metrics.ascent_upem), abs(model.metrics.descent_upem)) + 0.25 * upem
    for cp, glyph in model.glyphs.items():
        x0, y0, x1, y1 = glyph.bounding_box_upem
        if abs(y1) > clip_limit or abs(y0) > clip_limit:
            failures.append(f"VI_CLIPPING_CP_{cp:04X}")
        if not (math.isfinite(glyph.advance_width_upem) and 0.0 < glyph.advance_width_upem <= 4.0 * upem):
            failures.append(f"VI_SPACING_CP_{cp:04X}")
    return failures
