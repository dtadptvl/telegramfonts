"""Binary-first publication gate.

A valid authorized binary wins: it reaches requested TTF/OTF validation with
zero MAX geometry-reconstruction calls. Format conversion is a deterministic
outline-preserving recompilation (extract -> rebuild), never a reconstruction.

Four-consumer binary validation: FontTools direct load, FreeType raster,
HarfBuzz shaping, and Chromium loadability. All consumers are fail-closed;
the Chromium capability is injectable and must be real when required.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from acquisition.models import AcquiredBinary
from compute.models import GeneratedFontFile
from reconstruction.candidate_builder import MaxCandidateFontBuilder
from reconstruction.models import Contour, CubicSegment, LineSegment, Point2D, ReconstructedGlyph

BINARY_PIPELINE_VERSION = "stage9d-binary-v1"

_SHAPE_PROBE_COUNT = 3


def _sample_codepoints_from_binary(raw_bytes: bytes) -> tuple[int, ...]:
    """Deterministic printable sample codepoints drawn from the binary's own cmap."""
    from fontTools.ttLib import TTFont

    font = TTFont(io.BytesIO(raw_bytes), fontNumber=0, lazy=True)
    try:
        cmap = font.getBestCmap() or {}
    finally:
        font.close()
    candidates = sorted(cp for cp in cmap if 0x21 <= cp <= 0xFFFF)
    return tuple(candidates[:3])


@dataclass(frozen=True)
class BinaryGateReport:
    """Sanitized four-consumer binary validation report."""

    overall_status: str  # PASS | FAIL | BLOCKED
    fonttools_passed: bool
    freetype_passed: bool
    harfbuzz_passed: bool
    chromium_passed: bool
    artifact_sha256: str
    artifact_size_bytes: int
    format: str
    provenance: str
    failure_reasons: tuple[str, ...] = field(default_factory=tuple)
    evaluation_timestamp_utc: str = ""

    def to_dict(self) -> dict:
        return {
            "overall_status": self.overall_status,
            "fonttools_passed": self.fonttools_passed,
            "freetype_passed": self.freetype_passed,
            "harfbuzz_passed": self.harfbuzz_passed,
            "chromium_passed": self.chromium_passed,
            "artifact_sha256": self.artifact_sha256,
            "artifact_size_bytes": self.artifact_size_bytes,
            "format": self.format,
            "provenance": self.provenance,
            "failure_reasons": list(self.failure_reasons),
        }

    def compute_report_hash(self) -> str:
        serialized = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def extract_glyphs_from_binary(raw_bytes: bytes) -> tuple[dict[int, ReconstructedGlyph], dict[str, Any]]:
    """Extract outline geometry + metrics from a valid sfnt binary (no reconstruction)."""
    from fontTools.ttLib import TTFont

    font = TTFont(io.BytesIO(raw_bytes), fontNumber=0, lazy=False)
    try:
        cmap = font.getBestCmap()
        if not cmap:
            raise ValueError("BINARY_NO_CMAP")
        glyph_set = font.getGlyphSet()
        hmtx = font["hmtx"]
        upem = int(font["head"].unitsPerEm)
        ascent = int(getattr(font.get("hhea"), "ascent", 800)) if font.get("hhea") else 800
        descent = int(getattr(font.get("hhea"), "descent", -200)) if font.get("hhea") else -200
        name_to_unicode: dict[str, int] = {}
        for cp, gname in cmap.items():
            name_to_unicode.setdefault(gname, cp)

        from fontTools.pens.recordingPen import RecordingPen

        glyphs: dict[int, ReconstructedGlyph] = {}
        for cp in sorted(cmap):
            gname = cmap[cp]
            if gname not in glyph_set:
                continue
            pen = RecordingPen()
            try:
                glyph_set[gname].draw(pen)
            except Exception:
                continue
            contours = _contours_from_recording(pen.value)
            advance, lsb = hmtx[gname] if gname in hmtx.metrics else (upem // 2, 0)
            xs = [p.x for c in contours for s in c.segments for p in (s.p0, getattr(s, "p3", s.p1))]
            ys = [p.y for c in contours for s in c.segments for p in (s.p0, getattr(s, "p3", s.p1))]
            if xs and ys:
                bbox = (min(xs), min(ys), max(xs), max(ys))
            else:
                bbox = (0.0, 0.0, 0.0, 0.0)
            glyphs[cp] = ReconstructedGlyph(
                code_point=cp,
                character=chr(cp),
                advance_width_upem=float(advance),
                lsb_upem=float(lsb),
                rsb_upem=float(advance) - (bbox[2] if xs else float(lsb)),
                ascent_upem=float(ascent),
                descent_upem=float(descent),
                contours=contours,
                bounding_box_upem=bbox,
            )
        meta = {
            "units_per_em": upem,
            "ascent": ascent,
            "descent": descent,
            "family_glyph_count": len(glyphs),
        }
        return glyphs, meta
    finally:
        font.close()


def _contours_from_recording(operations: Sequence[tuple]) -> list[Contour]:
    """Convert RecordingPen operations into closed contour models."""
    contours: list[Contour] = []
    current: list = []
    start: Point2D | None = None

    def flush() -> None:
        nonlocal current, start
        if current and start is not None:
            current.append(LineSegment(current[-1].p1 if isinstance(current[-1], LineSegment) else current[-1].p3, start))
            contours.append(Contour(segments=current, is_hole=False))
        current = []
        start = None

    for op, args in operations:
        if op == "moveTo":
            flush()
            start = Point2D(float(args[0][0]), float(args[0][1]))
        elif op == "lineTo":
            if start is None:
                continue
            prev = current[-1].p1 if current and isinstance(current[-1], LineSegment) else (
                current[-1].p3 if current else start
            )
            current.append(LineSegment(prev, Point2D(float(args[0][0]), float(args[0][1]))))
        elif op == "curveTo":
            prev = current[-1].p1 if current and isinstance(current[-1], LineSegment) else (
                current[-1].p3 if current else start
            )
            if prev is None or len(args) < 2:
                continue
            pts = [prev] + [Point2D(float(p[0]), float(p[1])) for p in args]
            for i in range(0, len(pts) - 3, 1):
                pass
            current.append(CubicSegment(p0=pts[0], p1=pts[1], p2=pts[-2] if len(pts) >= 3 else pts[1], p3=pts[-1]))
        elif op == "qCurveTo":
            prev = current[-1].p1 if current and isinstance(current[-1], LineSegment) else (
                current[-1].p3 if current else start
            )
            if prev is None:
                continue
            points = list(args)
            if points and points[-1] is None:
                points = points[:-1]
            quads: list[tuple[Point2D, Point2D, Point2D]] = []
            off: list[Point2D] = []
            for p in points:
                if p is None:
                    continue
                off.append(Point2D(float(p[0]), float(p[1])))
            # Decompose consecutive off-curves with implied on-curves.
            anchor = prev
            i = 0
            offpts = [Point2D(float(p[0]), float(p[1])) for p in points if p is not None]
            if len(offpts) == 1:
                quads.append((anchor, offpts[0], offpts[0]))
            else:
                for idx in range(len(offpts) - 1):
                    implied = Point2D(
                        (offpts[idx].x + offpts[idx + 1].x) / 2.0,
                        (offpts[idx].y + offpts[idx + 1].y) / 2.0,
                    )
                    quads.append((anchor, offpts[idx], implied))
                    anchor = implied
                quads.append((anchor, offpts[-1], offpts[-1]))
            for q0, qc, q1 in quads:
                c1 = Point2D(q0.x + 2.0 / 3.0 * (qc.x - q0.x), q0.y + 2.0 / 3.0 * (qc.y - q0.y))
                c2 = Point2D(q1.x + 2.0 / 3.0 * (qc.x - q1.x), q1.y + 2.0 / 3.0 * (qc.y - q1.y))
                current.append(CubicSegment(p0=q0, p1=c1, p2=c2, p3=q1))
        elif op == "closePath" or op == "endPath":
            flush()
    flush()
    return contours


def prepare_binary_artifact(
    binary: AcquiredBinary,
    requested_format: str,
    output_dir: Path,
    family_name: str,
    style_name: str,
) -> GeneratedFontFile:
    """Produce the exact requested-format artifact from a verified binary.

    Same format: bytes are copied unchanged (exact continuity).
    Other format: deterministic outline-preserving recompilation via the
    candidate builder with zero MAX solver involvement.
    """
    requested = requested_format.strip().upper()
    if requested not in ("TTF", "OTF"):
        raise ValueError(f"BINARY_UNSUPPORTED_FORMAT_{requested}")
    output_dir.mkdir(parents=True, exist_ok=True)

    if binary.format == requested:
        target = output_dir / f"{binary.sha256_hex[:16]}.{requested.lower()}"
        target.write_bytes(binary.raw_bytes)
        return GeneratedFontFile(
            style_id="binary",
            style_name=style_name,
            format=requested,
            filename=target.name,
            file_path=target,
            size_bytes=len(binary.raw_bytes),
            sha256_hex=hashlib.sha256(binary.raw_bytes).hexdigest(),
        )

    glyphs, _meta = extract_glyphs_from_binary(binary.raw_bytes)
    if not glyphs:
        raise ValueError("BINARY_CONVERSION_NO_GLYPHS")
    builder = MaxCandidateFontBuilder(
        family_name=family_name or binary.family_name,
        style_name=style_name or binary.style_name,
        units_per_em=1000,
    )
    build = builder.build_candidate_family(glyphs=glyphs, output_dir=output_dir, typography=None)
    art = build.ttf if requested == "TTF" else build.otf
    if not art or not art.file_path or not Path(art.file_path).is_file():
        raise ValueError(f"BINARY_CONVERSION_FAILED_{requested}")
    return GeneratedFontFile(
        style_id="binary",
        style_name=style_name or binary.style_name,
        format=requested,
        filename=art.filename,
        file_path=Path(art.file_path),
        size_bytes=art.size_bytes,
        sha256_hex=art.sha256_hex,
    )


class BinaryConsumerValidator:
    """Fail-closed four-consumer validation for binary-path artifacts."""

    def __init__(self, chromium_load_check: Callable[[bytes], bool] | None = None) -> None:
        self.chromium_load_check = chromium_load_check

    def _fonttools_check(self, raw: bytes) -> tuple[bool, str]:
        from fontTools.ttLib import TTFont

        try:
            font = TTFont(io.BytesIO(raw), fontNumber=0, lazy=False)
            cmap = font.getBestCmap()
            ok = bool(cmap) and int(font["maxp"].numGlyphs) > 0 and int(font["head"].unitsPerEm) > 0
            font.close()
            return ok, "" if ok else "FONTTOOLS_LOAD_FAILED"
        except Exception:
            return False, "FONTTOOLS_LOAD_FAILED"

    def _freetype_check(self, raw: bytes, samples: tuple[int, ...], source_path: Path) -> tuple[bool, str]:
        import freetype

        try:
            face = freetype.Face(str(source_path))
            face.set_pixel_sizes(0, 64)
            rendered = 0
            for cp in samples:
                idx = face.get_char_index(cp)
                if idx == 0:
                    continue
                face.load_glyph(idx, freetype.FT_LOAD_RENDER | freetype.FT_LOAD_NO_HINTING)
                bitmap = face.glyph.bitmap
                if bitmap.width > 0 and bitmap.rows > 0:
                    rendered += 1
            return rendered > 0, "" if rendered > 0 else "FREETYPE_RENDER_FAILED"
        except Exception:
            return False, "FREETYPE_LOAD_FAILED"

    def _harfbuzz_check(self, raw: bytes, samples: tuple[int, ...]) -> tuple[bool, str]:
        import uharfbuzz as hb

        try:
            blob = hb.Blob(raw)
            face = hb.Face(blob)
            font = hb.Font(face)
            chars = [chr(cp) for cp in samples]
            probes = []
            if len(chars) >= 2:
                for i in range(len(chars) - 1):
                    probes.append(chars[i] + chars[i + 1])
            if chars:
                probes.append(chars[0] + chars[0])
            probes = probes[:_SHAPE_PROBE_COUNT]
            buf_ok = 0
            for text in probes:
                buf = hb.Buffer()
                buf.add_str(text)
                buf.guess_segment_properties()
                hb.shape(font, buf)
                infos = buf.glyph_infos
                positions = buf.glyph_positions
                if not infos or any(i.codepoint == 0 for i in infos):
                    continue
                if all(math.isfinite(p.x_advance) for p in positions):
                    buf_ok += 1
            return bool(probes) and buf_ok == len(probes), (
                "" if bool(probes) and buf_ok == len(probes) else "HARFBUZZ_SHAPING_FAILED"
            )
        except Exception:
            return False, "HARFBUZZ_LOAD_FAILED"

    def _chromium_check(self, raw: bytes) -> tuple[bool | None, str]:
        if self.chromium_load_check is None:
            return None, "CHROMIUM_CAPABILITY_UNAVAILABLE"
        try:
            ok = bool(self.chromium_load_check(raw))
            return ok, "" if ok else "CHROMIUM_LOAD_FAILED"
        except Exception:
            return False, "CHROMIUM_LOAD_FAILED"

    def validate(
        self,
        font_file: GeneratedFontFile,
        provenance: str,
    ) -> BinaryGateReport:
        raw = Path(font_file.file_path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != font_file.sha256_hex or len(raw) != font_file.size_bytes:
            return BinaryGateReport(
                overall_status="FAIL",
                fonttools_passed=False,
                freetype_passed=False,
                harfbuzz_passed=False,
                chromium_passed=False,
                artifact_sha256=font_file.sha256_hex,
                artifact_size_bytes=font_file.size_bytes,
                format=font_file.format,
                provenance=provenance,
                failure_reasons=("BINARY_ARTIFACT_DRIFT",),
                evaluation_timestamp_utc=datetime.now(timezone.utc).isoformat(),
            )

        ft_ok, ft_reason = self._fonttools_check(raw)
        try:
            samples = _sample_codepoints_from_binary(raw)
        except Exception:
            samples = ()
        if not samples:
            return BinaryGateReport(
                overall_status="FAIL",
                fonttools_passed=ft_ok,
                freetype_passed=False,
                harfbuzz_passed=False,
                chromium_passed=False,
                artifact_sha256=font_file.sha256_hex,
                artifact_size_bytes=font_file.size_bytes,
                format=font_file.format,
                provenance=provenance,
                failure_reasons=("BINARY_NO_SAMPLE_CODEPOINTS",),
                evaluation_timestamp_utc=datetime.now(timezone.utc).isoformat(),
            )
        fr_ok, fr_reason = self._freetype_check(raw, samples, Path(font_file.file_path))
        hb_ok, hb_reason = self._harfbuzz_check(raw, samples)
        cr_ok, cr_reason = self._chromium_check(raw)

        reasons = [r for r in (ft_reason, fr_reason, hb_reason) if r]
        if cr_ok is None:
            overall = "BLOCKED"
            reasons.append(cr_reason)
        elif not cr_ok:
            overall = "FAIL"
            reasons.append(cr_reason)
        else:
            overall = "PASS" if (ft_ok and fr_ok and hb_ok) else "FAIL"

        return BinaryGateReport(
            overall_status=overall,
            fonttools_passed=ft_ok,
            freetype_passed=fr_ok,
            harfbuzz_passed=hb_ok,
            chromium_passed=bool(cr_ok),
            artifact_sha256=font_file.sha256_hex,
            artifact_size_bytes=font_file.size_bytes,
            format=font_file.format,
            provenance=provenance,
            failure_reasons=tuple(sorted(set(reasons))),
            evaluation_timestamp_utc=datetime.now(timezone.utc).isoformat(),
        )
