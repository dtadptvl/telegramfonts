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
from reconstruction.models import Contour, CubicSegment, LineSegment, Point2D

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
    deterministic_codepoints: tuple[int, ...] = ()
    # Cascade route identity (Woku-primary cascade). Empty for non-cascade
    # providers so legacy binding hashes are preserved exactly; non-empty
    # values bind provider/model/route/fallback-reason identities into the
    # binding hash (and thus cache/provenance identity).
    ai_route: str = ""
    ai_fallback_reason: str = ""
    ai_route_models: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        payload = {
            "mode": self.mode,
            "ai_model_id": self.ai_model_id,
            "ai_model_version": self.ai_model_version,
            "ai_prompt_hash": self.ai_prompt_hash,
            "config_hash": self.config_hash,
            "source_hash": self.source_hash,
            "extended_codepoints": list(self.extended_codepoints),
            "preserved_codepoints": list(self.preserved_codepoints),
            "deterministic_codepoints": list(self.deterministic_codepoints),
        }
        if self.ai_route:
            payload["ai_route"] = self.ai_route
        if self.ai_fallback_reason:
            payload["ai_fallback_reason"] = self.ai_fallback_reason
        if self.ai_route_models:
            payload["ai_route_models"] = list(self.ai_route_models)
        return payload

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

    @staticmethod
    def _build_style_evidence(model: CanonicalFontModel, max_sample_glyphs: int = 4) -> dict:
        """Bounded, sanitized source style evidence for AI candidate generation.

        Carries deterministic source glyph geometry, metrics, raster sample
        fingerprints, and global stroke/contrast statistics. Never carries
        secrets or full observation payloads.
        """
        sample_cps = sorted(model.glyphs.keys())[:max_sample_glyphs]
        sample_glyphs = []
        for cp in sample_cps:
            g = model.glyphs[cp]
            contours = []
            for c in g.contours[:4]:
                pts = c.sample_points(samples_per_segment=4)[:24]
                contours.append([[round(p.x, 1), round(p.y, 1)] for p in pts])
            sample_glyphs.append(
                {
                    "code_point": cp,
                    "contours": contours,
                    "advance_width_upem": round(g.advance_width_upem, 2),
                    "lsb_upem": round(g.lsb_upem, 2),
                    "rsb_upem": round(g.rsb_upem, 2),
                    "ascent_upem": round(g.ascent_upem, 2),
                    "descent_upem": round(g.descent_upem, 2),
                    "bounding_box_upem": [round(v, 1) for v in g.bounding_box_upem],
                    "raster_sample_hashes": list(g.observation_fingerprints)[:4],
                }
            )
        advances = [g.advance_width_upem for g in model.glyphs.values()]
        heights = [
            g.bounding_box_upem[3] - g.bounding_box_upem[1]
            for g in model.glyphs.values()
            if len(g.bounding_box_upem) == 4
        ]
        widths = [
            g.bounding_box_upem[2] - g.bounding_box_upem[0]
            for g in model.glyphs.values()
            if len(g.bounding_box_upem) == 4
        ]
        mean_height = sum(heights) / len(heights) if heights else 0.0
        mean_width = sum(widths) / len(widths) if widths else 0.0
        return {
            "family_name": model.family_name,
            "style_name": model.style_name,
            "units_per_em": model.metrics.units_per_em,
            "ascent_upem": round(model.metrics.ascent_upem, 2),
            "descent_upem": round(model.metrics.descent_upem, 2),
            "glyph_count": len(model.glyphs),
            "mean_advance_upem": round(sum(advances) / len(advances), 2) if advances else 0.0,
            "stroke_contrast_proxy": round(mean_height / mean_width, 3) if mean_width else 0.0,
            "sample_glyphs": sample_glyphs,
        }


    @staticmethod
    def _translate_contours(contours: Sequence[Contour], dx: float, dy: float) -> list[Contour]:
        def tp(p: Point2D) -> Point2D:
            return Point2D(p.x + dx, p.y + dy)

        moved: list[Contour] = []
        for c in contours:
            segments = []
            for s in c.segments:
                if isinstance(s, CubicSegment):
                    segments.append(CubicSegment(p0=tp(s.p0), p1=tp(s.p1), p2=tp(s.p2), p3=tp(s.p3)))
                else:
                    segments.append(LineSegment(p0=tp(s.p0), p1=tp(s.p1)))
            moved.append(
                Contour(segments=segments, is_hole=c.is_hole, parent_index=c.parent_index, area_upem=c.area_upem)
            )
        return moved

    @staticmethod
    def _contour_bbox(contours: Sequence[Contour]) -> tuple[float, float, float, float]:
        xs: list[float] = []
        ys: list[float] = []
        for c in contours:
            for s in c.segments:
                pts = (s.p0, s.p1, s.p2, s.p3) if isinstance(s, CubicSegment) else (s.p0, s.p1)
                for p in pts:
                    xs.append(p.x)
                    ys.append(p.y)
        if not xs:
            return (0.0, 0.0, 0.0, 0.0)
        return (min(xs), min(ys), max(xs), max(ys))

    def _deterministic_glyph(
        self, model: CanonicalFontModel, cp: int
    ) -> CalibratedGlyph | None:
        """Deterministic first construction from existing glyph/mark evidence.

        Transplants the exact mark contours carried by an existing donor
        composite (NFD decomposition with the identical mark multiset) onto
        the existing base glyph, or extracts standalone combining-mark
        geometry. Purely evidence-driven: returns None (-> AI gate) whenever
        the source model does not observably contain the required evidence.
        """
        if cp in MARK_CODEPOINT_SET:
            marks_needed: tuple[int, ...] = (cp,)
            base_cp: int | None = None
        else:
            decomposed = [ord(c) for c in unicodedata.normalize("NFD", chr(cp))]
            if len(decomposed) < 2:
                return None
            base_cp = decomposed[0]
            marks_needed = tuple(decomposed[1:])
            if base_cp not in model.glyphs:
                return None

        donor: CalibratedGlyph | None = None
        donor_base: CalibratedGlyph | None = None
        for d_cp in sorted(model.glyphs):
            if d_cp == cp:
                continue
            d_dec = [ord(c) for c in unicodedata.normalize("NFD", chr(d_cp))]
            if len(d_dec) < 2:
                continue
            if tuple(d_dec[1:]) != marks_needed:
                continue
            if d_dec[0] not in model.glyphs:
                continue
            if base_cp is not None and d_dec[0] == base_cp:
                continue
            donor = model.glyphs[d_cp]
            donor_base = model.glyphs[d_dec[0]]
            break
        if donor is None or donor_base is None:
            return None
        base_contour_count = len(donor_base.contours)
        if len(donor.contours) <= base_contour_count:
            return None
        mark_contours = donor.contours[base_contour_count:]
        fingerprint = hashlib.sha256(
            f"vi_det:{cp}:{self.source_hash}".encode("utf-8")
        ).hexdigest()

        if cp in MARK_CODEPOINT_SET:
            bbox = self._contour_bbox(mark_contours)
            anchor_x = round((bbox[0] + bbox[2]) / 2.0, 2)
            anchor_y = round(bbox[1], 2)
            glyph = CalibratedGlyph(
                code_point=cp,
                character=chr(cp),
                # Standalone combining marks carry zero advance (attachment
                # geometry is anchor-driven); the closed VI spacing gate
                # accepts zero advance for combining marks only.
                advance_width_upem=0.0,
                lsb_upem=0.0,
                rsb_upem=0.0,
                ascent_upem=round(bbox[3], 2),
                descent_upem=round(bbox[1], 2),
                bounding_box_upem=bbox,
                contours=list(mark_contours),
                confidence=1.0,
                observation_fingerprints=(fingerprint,),
                anchors=(("mark", anchor_x, anchor_y),),
            )
        else:
            base_glyph = model.glyphs[base_cp]  # type: ignore[index]
            dx = round((base_glyph.advance_width_upem - donor_base.advance_width_upem) / 2.0, 2)  # type: ignore[union-attr]
            dy = round(base_glyph.ascent_upem - donor_base.ascent_upem, 2)  # type: ignore[union-attr]
            moved_marks = self._translate_contours(mark_contours, dx, dy)
            combined = list(base_glyph.contours) + moved_marks
            combined_bbox = self._contour_bbox(combined)
            glyph = CalibratedGlyph(
                code_point=cp,
                character=chr(cp),
                advance_width_upem=base_glyph.advance_width_upem,
                lsb_upem=base_glyph.lsb_upem,
                rsb_upem=base_glyph.rsb_upem,
                ascent_upem=base_glyph.ascent_upem,
                descent_upem=base_glyph.descent_upem,
                bounding_box_upem=combined_bbox,
                contours=combined,
                confidence=1.0,
                observation_fingerprints=(fingerprint,),
                anchors=base_glyph.anchors,
            )
        glyph.validate()
        return glyph

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

        # Deterministic-first: construct every missing glyph that the source
        # evidence can deterministically prove, BEFORE any AI contact.
        deterministic_glyphs: dict[int, CalibratedGlyph] = {}
        unresolved: list[int] = []
        for cp in sorted(missing):
            det_glyph = self._deterministic_glyph(model, cp)
            if det_glyph is not None:
                deterministic_glyphs[cp] = det_glyph
            else:
                unresolved.append(cp)

        ai_glyphs: dict[int, CalibratedGlyph] = {}
        if unresolved:
            if self.ai_provider is None:
                raise VietnameseAIIntegrityError("VI_AI_PROVIDER_UNAVAILABLE")

            request = {
                "mode": "VIETNAMESE",
                "family_name": model.family_name,
                "style_name": model.style_name,
                "config_hash": self.config_hash,
                "source_hash": self.source_hash,
                "missing_codepoints": list(unresolved),
                "units_per_em": model.metrics.units_per_em,
                "style_evidence": self._build_style_evidence(model),
            }
            candidates = await self.ai_provider.generate_candidates(request)
            produced = {c.code_point for c in candidates}
            if produced != set(unresolved):
                raise VietnameseAIIntegrityError("VI_AI_INCOMPLETE_COVERAGE")

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
                ai_glyphs[spec.code_point] = CalibratedGlyph(
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
                    # Anchors survive into the built glyph/OpenType behavior.
                    anchors=tuple(spec.anchors),
                )

        extended_glyphs = dict(model.glyphs)
        extended_glyphs.update(deterministic_glyphs)
        extended_glyphs.update(ai_glyphs)

        # Cascade route identity (bounded, sanitized): only cascade-shaped
        # traces carry route/fallback dimensions; non-cascade providers keep
        # the exact legacy binding shape/hash.
        trace = getattr(self.ai_provider, "last_route_trace", None) if unresolved else None
        if trace is not None and hasattr(trace, "route"):
            ai_route = trace.route
            ai_fallback_reason = trace.fallback_reason
            ai_route_models = tuple(c.model for c in trace.calls)
        else:
            ai_route = ""
            ai_fallback_reason = ""
            ai_route_models = ()

        extended_model = replace(model, glyphs=extended_glyphs)
        final_binding = VietnameseExtensionBinding(
            mode="VIETNAMESE",
            ai_model_id=self.ai_provider.model_id if unresolved else "",
            ai_model_version=self.ai_provider.model_version if unresolved else "",
            ai_prompt_hash=self.ai_provider.prompt_hash() if unresolved else "",
            config_hash=self.config_hash,
            source_hash=self.source_hash,
            extended_codepoints=tuple(sorted(list(deterministic_glyphs) + list(ai_glyphs))),
            preserved_codepoints=preserved,
            deterministic_codepoints=tuple(sorted(deterministic_glyphs)),
            ai_route=ai_route,
            ai_fallback_reason=ai_fallback_reason,
            ai_route_models=ai_route_models,
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
        adv = glyph.advance_width_upem
        # Combining marks legitimately carry zero advance (attachment is
        # anchor/GPOS-driven, and direct browser metrics quantize sub-pixel
        # standalone-mark advances to zero px); every other glyph requires a
        # strictly positive, bounded advance. Negative/non-finite/oversized
        # advances always fail closed.
        combining_mark = unicodedata.combining(chr(cp)) != 0
        if (
            not math.isfinite(adv)
            or adv < 0.0
            or adv > 4.0 * upem
            or (adv == 0.0 and not combining_mark)
        ):
            failures.append(f"VI_SPACING_CP_{cp:04X}")
    return failures
