"""Speed-first final validation, run ONCE (ADR-0004, U10).

Consumers (single pass, bounded, representative):
  - FontTools structural/load/table/cmap/metrics/outline checks (TTF after
    the temporary build; cheap structural/load checks on BOTH final TTF and
    OTF after the final build).
  - HarfBuzz representative shaping/positioning/kern checks.
  - FreeType representative loading/rendering.
  - bounded NFC/NFD + Vietnamese representative corpus checks ONLY when the
    VIETNAMESE mode is selected.

PROHIBITED and absent from this module by construction: exhaustive Chromium
final validation, the legacy four-consumer MAX gate, the complete MAX
held-out schedule, the full repository suite, soak, retired
FULLMAX/BALANCEDMAX E2E, and any second heavy validation pass.
"""
from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from fontTools.ttLib import TTFont

REQUIRED_TABLES = {"head", "hhea", "maxp", "name", "OS/2", "cmap", "post"}
MAX_SHAPE_CALLS = 32
MAX_RENDER_GLYPHS = 32
MAX_KERN_PROBE_PAIRS = 64
MAX_NORMALIZATION_PROBES = 64

VIETNAMESE_REPRESENTATIVE_CORPUS = (
    "Tiếng Việt có dấu",
    "Ắc quy \u0111iện",
    "học phần",
    "nghệ thuật",
    "quốc ngữ",
    "thủy triều",
    "đường phố",
    "ổn định",
)


class ValidationAlreadyRan(RuntimeError):
    """The speed-first final validation runs exactly once per identity."""

    def __init__(self) -> None:
        super().__init__("ATLAS_VALIDATION_ALREADY_RAN")


@dataclass
class ValidationReport:
    passed: bool
    fonttools_ttf: dict = field(default_factory=dict)
    fonttools_otf: dict = field(default_factory=dict)
    harfbuzz: dict = field(default_factory=dict)
    freetype: dict = field(default_factory=dict)
    normalization: dict = field(default_factory=dict)
    low_confidence_glyph_ids: list[int] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "fonttools_ttf": self.fonttools_ttf,
            "fonttools_otf": self.fonttools_otf,
            "harfbuzz": self.harfbuzz,
            "freetype": self.freetype,
            "normalization": self.normalization,
            "low_confidence_glyph_ids": sorted(self.low_confidence_glyph_ids),
            "reasons": list(self.reasons),
        }


def _fonttools_structural(path: Path, expected_format: str) -> dict:
    """FontTools structural/load/table/cmap/metrics/outline checks."""
    result: dict = {"path": path.name, "format": expected_format, "checks": {}}
    try:
        font = TTFont(path)
    except Exception as exc:
        result["checks"]["load"] = f"FAIL:{type(exc).__name__}"
        result["passed"] = False
        return result
    checks = result["checks"]
    try:
        tables = set(font.keys())
        checks["tables"] = "PASS" if REQUIRED_TABLES.issubset(tables) else "FAIL"
        cmap = font.getBestCmap() or {}
        checks["cmap"] = "PASS" if cmap else "FAIL"
        order = font.getGlyphOrder()
        checks["glyph_count"] = "PASS" if len(order) >= 2 else "FAIL"
        hmtx = font.get("hmtx")
        checks["metrics"] = (
            "PASS"
            if hmtx is not None and all(w >= 0 for w, _ in hmtx.metrics.values())
            else "FAIL"
        )
        # Outline load: iterate every glyph outline once (bounds + load).
        outline_ok = True
        if "glyf" in tables:
            glyf = font["glyf"]
            for name in order:
                g = glyf[name]
                if g.numberOfContours != 0:
                    _ = g.xMin, g.yMin, g.xMax, g.yMax
        elif "CFF " in tables:
            cff = font["CFF "]
            top = cff.cff.topDictIndex[0]
            cs = top.CharStrings
            for name in order:
                cs[name]  # load charstring
        else:
            outline_ok = False
        checks["outline"] = "PASS" if outline_ok else "FAIL"
        head = font["head"]
        checks["head_bbox"] = (
            "PASS"
            if head.xMin <= head.xMax and head.yMin <= head.yMax
            else "FAIL"
        )
        magic_ok = (
            path.read_bytes()[:4] == b"OTTO"
            if expected_format == "OTF"
            else path.read_bytes()[:4] in (b"\x00\x01\x00\x00", b"true")
        )
        checks["magic"] = "PASS" if magic_ok else "FAIL"
        result["glyph_count"] = len(order)
        result["cmap_count"] = len(cmap)
    finally:
        font.close()
    result["passed"] = all(v == "PASS" for v in checks.values())
    return result


def _representative_strings(code_points: list[int]) -> list[str]:
    """Deterministic bounded representative shaping corpus."""
    chars = [chr(cp) for cp in sorted(code_points) if cp > 0x20]
    strings: list[str] = []
    for i in range(0, len(chars), 16):
        strings.append("".join(chars[i:i + 16]))
        if len(strings) >= MAX_SHAPE_CALLS:
            break
    return strings


def _harfbuzz_checks(ttf_path: Path, code_points: list[int], kern_pairs: list[tuple[int, int]]) -> dict:
    import uharfbuzz as hb

    result: dict = {"checks": {}}
    data = ttf_path.read_bytes()
    blob = hb.Blob(data)
    face = hb.Face(blob)
    font = hb.Font(face)
    font.scale = (1000, 1000)
    cmap = face.unicodes_to_glyphs(code_points) if hasattr(face, "unicodes_to_glyphs") else None

    shaped = 0
    notdef_failures = 0
    nonfinite_positions = 0
    for text in _representative_strings(code_points):
        buf = hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        hb.shape(font, buf)
        infos = buf.glyph_infos
        positions = buf.glyph_positions
        shaped += 1
        for info in infos:
            if info.codepoint == 0:
                notdef_failures += 1
        for pos in positions:
            values = (pos.x_advance, pos.y_advance, pos.x_offset, pos.y_offset)
            if any(not math.isfinite(float(v)) for v in values):
                nonfinite_positions += 1
    result["checks"]["shaping"] = "PASS" if shaped > 0 and notdef_failures == 0 else "FAIL"
    result["checks"]["positions"] = "PASS" if nonfinite_positions == 0 else "FAIL"
    result["shape_calls"] = shaped

    # Representative kern probes: only declared pairs, bounded count.
    if kern_pairs:
        probes = kern_pairs[:MAX_KERN_PROBE_PAIRS]
        deltas_observed = 0
        kern_ok = True
        for l_cp, r_cp in probes:
            text = chr(l_cp) + chr(r_cp)
            buf_k = hb.Buffer()
            buf_k.add_str(text)
            buf_k.guess_segment_properties()
            hb.shape(font, buf_k)
            adv_kern = sum(p.x_advance for p in buf_k.glyph_positions)
            buf_n = hb.Buffer()
            buf_n.add_str(text)
            buf_n.guess_segment_properties()
            hb.shape(font, buf_n, {"kern": False})
            adv_nokern = sum(p.x_advance for p in buf_n.glyph_positions)
            if adv_kern != adv_nokern:
                deltas_observed += 1
        # Material deltas were declared; at least one must be observable.
        kern_ok = deltas_observed > 0
        result["checks"]["kern"] = "PASS" if kern_ok else "FAIL"
        result["kern_probes"] = len(probes)
        result["kern_deltas_observed"] = deltas_observed
    else:
        result["checks"]["kern"] = "PASS_NONE_DECLARED"

    result["passed"] = all(
        v.startswith("PASS") for v in result["checks"].values()
    )
    return result


def _freetype_checks(ttf_path: Path, code_points: list[int]) -> dict:
    import freetype

    result: dict = {"checks": {}}
    face = freetype.Face(str(ttf_path))
    face.set_char_size(48 * 64)
    rendered = 0
    empty_ink_failures = 0
    inked_cps = [cp for cp in sorted(code_points) if cp > 0x20][:MAX_RENDER_GLYPHS]
    for cp in inked_cps:
        try:
            face.load_char(chr(cp), freetype.FT_LOAD_RENDER)
            bitmap = face.glyph.bitmap
            ink = sum(bitmap.buffer)
            rendered += 1
            if ink <= 0:
                empty_ink_failures += 1
        except Exception:
            empty_ink_failures += 1
            rendered += 1
    result["checks"]["load"] = "PASS" if rendered > 0 else "FAIL"
    result["checks"]["render"] = (
        "PASS" if empty_ink_failures == 0 else f"FAIL:{empty_ink_failures}_EMPTY"
    )
    result["rendered"] = rendered
    result["passed"] = result["checks"]["load"] == "PASS" and result["checks"]["render"] == "PASS"
    return result


def _normalization_checks(code_points: list[int], vietnamese: bool) -> dict:
    """Bounded NFC/NFD equivalence + Vietnamese representative corpus."""
    result: dict = {"checks": {}, "vietnamese": vietnamese}
    probes = sorted(code_points)[:MAX_NORMALIZATION_PROBES]
    drift = 0
    for cp in probes:
        ch = chr(cp)
        if unicodedata.normalize("NFC", ch) != unicodedata.normalize("NFD", ch):
            # Decomposable: both forms must map into the covered set.
            for part in unicodedata.normalize("NFD", ch):
                if ord(part) not in set(code_points):
                    drift += 1
    result["checks"]["nfc_nfd_coverage"] = "PASS" if drift == 0 else f"FAIL:{drift}"
    if vietnamese:
        covered = set(code_points)
        missing = 0
        for text in VIETNAMESE_REPRESENTATIVE_CORPUS:
            for ch in unicodedata.normalize("NFC", text):
                if ch.strip() and ord(ch) not in covered:
                    missing += 1
        result["vn_corpus_strings"] = len(VIETNAMESE_REPRESENTATIVE_CORPUS)
        result["checks"]["vn_corpus_coverage"] = (
            "PASS" if missing == 0 else f"PARTIAL:{missing}_MISSING"
        )
        # VN coverage shortfalls are reported, not hard failures: failed
        # glyph classes stay FAILED_GLYPH without a global rerun.
    result["passed"] = result["checks"]["nfc_nfd_coverage"] == "PASS"
    return result


def run_speed_first_validation(
    ttf_path: Path,
    code_points: list[int],
    kern_pairs: list[tuple[int, int]],
    mode: str,
    low_confidence_glyph_ids: list[int] | None = None,
    already_ran: bool = False,
) -> ValidationReport:
    """The single speed-first final validation run (U10)."""
    if already_ran:
        raise ValidationAlreadyRan()

    report = ValidationReport(passed=False)
    report.fonttools_ttf = _fonttools_structural(ttf_path, "TTF")
    report.harfbuzz = _harfbuzz_checks(ttf_path, code_points, kern_pairs)
    report.freetype = _freetype_checks(ttf_path, code_points)
    report.normalization = _normalization_checks(
        code_points, vietnamese=(str(mode).strip().upper() == "VIETNAMESE")
    )
    report.low_confidence_glyph_ids = sorted(low_confidence_glyph_ids or [])

    reasons: list[str] = []
    for name, section in (
        ("fonttools_ttf", report.fonttools_ttf),
        ("harfbuzz", report.harfbuzz),
        ("freetype", report.freetype),
        ("normalization", report.normalization),
    ):
        if not section.get("passed", False):
            reasons.append(f"{name}_FAILED")
    report.reasons = reasons
    report.passed = len(reasons) == 0
    return report


def cheap_final_checks(ttf_path: Path, otf_path: Path, report: ValidationReport) -> ValidationReport:
    """Cheap FontTools structural/load checks on BOTH final artifacts.

    Runs after the temporary TTF passed the full single validation and the
    final TTF+OTF were built from the identical sealed model.
    """
    report.fonttools_ttf = _fonttools_structural(ttf_path, "TTF")
    report.fonttools_otf = _fonttools_structural(otf_path, "OTF")
    if not report.fonttools_ttf.get("passed") or not report.fonttools_otf.get("passed"):
        if "final_artifact_structural_FAILED" not in report.reasons:
            report.reasons.append("final_artifact_structural_FAILED")
        report.passed = False
    return report
