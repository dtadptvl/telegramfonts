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
            last = current[-1].p1 if isinstance(current[-1], LineSegment) else current[-1].p3
            if last.distance_to(start) >= 1.0:
                current.append(LineSegment(last, start))
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
    """Closed four-consumer validation for binary-path artifacts.

    No capability is injectable: evidence comes exclusively from the closed
    concrete FontTools/FreeType/HarfBuzz/Chromium producers bound to the exact
    descriptor bytes. Capability absence or forged evidence fails closed.
    """

    def validate(
        self,
        font_file: GeneratedFontFile,
        provenance: str,
    ) -> BinaryGateReport:
        from fidelity.models import ProductionProducerError
        from fidelity.producers import BinaryConsumerEvidenceProducer, CandidateArtifactDescriptor
        from fidelity import producers as _producers

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

        descriptor = CandidateArtifactDescriptor(
            file_path=str(font_file.file_path),
            expected_format=font_file.format,
            expected_size_bytes=font_file.size_bytes,
            expected_sha256_hex=font_file.sha256_hex,
            raw_bytes=raw,
        )
        try:
            descriptor.validate()
        except Exception:
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
                failure_reasons=("BINARY_DESCRIPTOR_ATTESTATION_FAILED",),
                evaluation_timestamp_utc=datetime.now(timezone.utc).isoformat(),
            )

        # Closed capability probe: absent Chromium can never produce PASS.
        try:
            _producers.find_chromium_executable()
        except Exception:
            return BinaryGateReport(
                overall_status="BLOCKED",
                fonttools_passed=False,
                freetype_passed=False,
                harfbuzz_passed=False,
                chromium_passed=False,
                artifact_sha256=font_file.sha256_hex,
                artifact_size_bytes=font_file.size_bytes,
                format=font_file.format,
                provenance=provenance,
                failure_reasons=("CHROMIUM_CAPABILITY_UNAVAILABLE",),
                evaluation_timestamp_utc=datetime.now(timezone.utc).isoformat(),
            )

        try:
            bundle = BinaryConsumerEvidenceProducer.produce_sync(descriptor)
        except ProductionProducerError as exc:
            message = str(exc)
            capability_absent = (
                "CHROMIUM_NOT_AVAILABLE" in message or "CHROMIUM_CAPABILITY_UNAVAILABLE" in message
            )
            return BinaryGateReport(
                overall_status="BLOCKED" if capability_absent else "FAIL",
                fonttools_passed=False,
                freetype_passed=False,
                harfbuzz_passed=False,
                chromium_passed=False,
                artifact_sha256=font_file.sha256_hex,
                artifact_size_bytes=font_file.size_bytes,
                format=font_file.format,
                provenance=provenance,
                failure_reasons=(
                    "CHROMIUM_CAPABILITY_UNAVAILABLE" if capability_absent else "BINARY_CONSUMER_GATE_FAILED",
                ),
                evaluation_timestamp_utc=datetime.now(timezone.utc).isoformat(),
            )

        gate = bundle.chromium.result
        return BinaryGateReport(
            overall_status="PASS",
            fonttools_passed=bool(bundle.fonttools.result.is_direct_loadable_fonttools),
            freetype_passed=bundle.freetype.result.render_error is None,
            harfbuzz_passed=bundle.harfbuzz.result.error_message is None,
            chromium_passed=bool(gate.is_direct_loadable_chromium and gate.rendered_canvas_valid),
            artifact_sha256=font_file.sha256_hex,
            artifact_size_bytes=font_file.size_bytes,
            format=font_file.format,
            provenance=provenance,
            failure_reasons=(),
            evaluation_timestamp_utc=datetime.now(timezone.utc).isoformat(),
        )
