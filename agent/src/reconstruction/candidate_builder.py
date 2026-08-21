"""Candidate Font Builder (MAX Pipeline C).

Constructs canonical OpenType (OTF/CFF), TrueType (TTF via cu2qu), and WOFF2
candidate font binaries directly from Phase-B reconstructed cubic master glyphs.
"""
from __future__ import annotations

import hashlib
import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.cu2quPen import Cu2QuPen
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

from reconstruction.models import (
    Contour,
    CubicSegment,
    LineSegment,
    Point2D,
    ReconstructedGlyph,
)
from typography.gpos_builder import attach_gpos_to_font
from typography.models import TypographyDataset

logger = logging.getLogger("telegramfonts.agent.reconstruction.candidate_builder")


@dataclass(frozen=True)
class CandidateFontArtifact:
    """Represents a generated candidate font binary."""
    format: str
    filename: str
    file_path: Path
    size_bytes: int
    sha256_hex: str
    glyph_count: int
    units_per_em: int = 1000


@dataclass(frozen=True)
class CandidateFamilyBuildResult:
    """Set of candidate font binaries derived from the same cubic master."""
    otf: CandidateFontArtifact
    ttf: CandidateFontArtifact
    woff2: CandidateFontArtifact
    glyph_count: int
    family_name: str
    style_name: str


def get_glyph_name_for_codepoint(cp: int) -> str:
    """Deterministic, standard glyph name mapping."""
    if cp == 0x20:
        return "space"
    # Basic ASCII letters
    if 0x41 <= cp <= 0x5A or 0x61 <= cp <= 0x7A:
        return chr(cp)
    # Digits
    digit_names = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
    if 0x30 <= cp <= 0x39:
        return digit_names[cp - 0x30]
    # Common symbols
    symbol_map = {
        ord("@"): "at",
        ord("%"): "percent",
        ord("."): "period",
        ord(","): "comma",
        ord(":"): "colon",
        ord(";"): "semicolon",
        ord("!"): "exclam",
        ord("?"): "question",
        ord("-"): "hyphen",
        ord("_"): "underscore",
        ord("("): "parenleft",
        ord(")"): "parenright",
        ord("["): "bracketleft",
        ord("]"): "bracketright",
        ord("{"): "braceleft",
        ord("}"): "braceright",
        ord("/"): "slash",
        ord("\\"): "backslash",
        ord("#"): "numbersign",
        ord("$"): "dollar",
        ord("&"): "ampersand",
        ord("+"): "plus",
        ord("="): "equal",
        ord("<"): "less",
        ord(">"): "greater",
        ord("\""): "quotedbl",
        ord("'"): "quotesingle",
    }
    if cp in symbol_map:
        return symbol_map[cp]
    # Standard Unicode hex naming
    if cp <= 0xFFFF:
        return f"uni{cp:04X}"
    return f"u{cp:06X}"


def draw_reconstructed_glyph_to_pen(glyph: ReconstructedGlyph, pen: Any) -> None:
    """Draw master cubic / line contour geometry to any FontTools-compatible pen."""
    for contour in glyph.contours:
        if not contour.segments:
            continue
        first_seg = contour.segments[0]
        pen.moveTo((round(first_seg.p0.x, 2), round(first_seg.p0.y, 2)))
        for seg in contour.segments:
            if isinstance(seg, CubicSegment):
                pen.curveTo(
                    (round(seg.p1.x, 2), round(seg.p1.y, 2)),
                    (round(seg.p2.x, 2), round(seg.p2.y, 2)),
                    (round(seg.p3.x, 2), round(seg.p3.y, 2)),
                )
            elif isinstance(seg, LineSegment):
                pen.lineTo((round(seg.p1.x, 2), round(seg.p1.y, 2)))
        pen.closePath()


class MaxCandidateFontBuilder:
    """Deterministic candidate font builder producing OTF (CFF), TTF (cu2qu), and WOFF2 binaries."""

    def __init__(
        self,
        family_name: str = "TeleFont MAX",
        style_name: str = "Regular",
        weight_class: int = 400,
        units_per_em: int = 1000,
        cu2qu_max_err: float = 1.0,
    ) -> None:
        self.family_name = family_name
        self.style_name = style_name
        self.weight_class = weight_class
        self.units_per_em = units_per_em
        self.cu2qu_max_err = cu2qu_max_err

    def build_candidate_family(
        self,
        glyphs: list[ReconstructedGlyph] | dict[int, ReconstructedGlyph],
        output_dir: Path | str,
        typography: TypographyDataset | None = None,
    ) -> CandidateFamilyBuildResult:
        """Build OTF, TTF, and WOFF2 candidate fonts from the same cubic masters."""
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        glyph_map = glyphs if isinstance(glyphs, dict) else {g.code_point: g for g in glyphs}

        otf_art = self.build_candidate_otf(glyph_map, out_dir, typography=typography)
        ttf_art = self.build_candidate_ttf(glyph_map, out_dir, typography=typography)
        woff2_art = self.derive_candidate_woff2(ttf_art.file_path, out_dir)

        return CandidateFamilyBuildResult(
            otf=otf_art,
            ttf=ttf_art,
            woff2=woff2_art,
            glyph_count=len(glyph_map),
            family_name=self.family_name,
            style_name=self.style_name,
        )

    def build_candidate_otf(
        self,
        glyph_map: dict[int, ReconstructedGlyph],
        output_dir: Path,
        typography: TypographyDataset | None = None,
    ) -> CandidateFontArtifact:
        """Build canonical OpenType-CFF font (.otf) preserving master cubic Béziers."""
        sorted_cps = sorted(glyph_map.keys())
        if 0x20 not in sorted_cps:
            sorted_cps = [0x20] + sorted_cps
        glyph_order = [".notdef"] + [get_glyph_name_for_codepoint(cp) for cp in sorted_cps]
        cmap = {cp: get_glyph_name_for_codepoint(cp) for cp in sorted_cps}

        # Global metrics estimation from glyphs
        ascent = int(max((g.ascent_upem for g in glyph_map.values()), default=800))
        descent = int(min((-abs(g.descent_upem) for g in glyph_map.values()), default=-200))
        win_ascent = max(ascent, abs(descent))
        win_descent = abs(descent)

        metrics_dict: dict[str, tuple[int, int]] = {".notdef": (500, 50), "space": (250, 0)}
        for cp, g in glyph_map.items():
            g_name = get_glyph_name_for_codepoint(cp)
            adv = int(round(g.advance_width_upem))
            lsb = int(round(g.lsb_upem))
            metrics_dict[g_name] = (adv, lsb)

        fb = FontBuilder(unitsPerEm=self.units_per_em, isTTF=False)
        fb.setupGlyphOrder(glyph_order)
        fb.setupCharacterMap(cmap)

        # CFF CharStrings with native cubic curve commands
        charstrings: dict[str, Any] = {}

        # 1. .notdef glyph
        notdef_pen = T2CharStringPen(500, None)
        notdef_pen.moveTo((50, 0))
        notdef_pen.lineTo((450, 0))
        notdef_pen.lineTo((450, 700))
        notdef_pen.lineTo((50, 700))
        notdef_pen.closePath()
        notdef_pen.moveTo((100, 50))
        notdef_pen.lineTo((100, 650))
        notdef_pen.lineTo((400, 650))
        notdef_pen.lineTo((400, 50))
        notdef_pen.closePath()
        charstrings[".notdef"] = notdef_pen.getCharString()

        # 2. space glyph (if not explicitly drawn)
        if "space" not in charstrings:
            space_pen = T2CharStringPen(metrics_dict["space"][0], None)
            charstrings["space"] = space_pen.getCharString()

        # 3. Master cubic glyphs
        for cp in sorted_cps:
            if cp == 0x20 and cp not in glyph_map:
                continue
            g = glyph_map[cp]
            g_name = get_glyph_name_for_codepoint(cp)
            adv = metrics_dict[g_name][0]
            pen = T2CharStringPen(adv, None)
            draw_reconstructed_glyph_to_pen(g, pen)
            charstrings[g_name] = pen.getCharString()

        ps_name = f"{self.family_name.replace(' ', '')}-{self.style_name.replace(' ', '')}"
        fb.setupCFF(
            psName=ps_name,
            fontInfo={"FullName": f"{self.family_name} {self.style_name}", "FamilyName": self.family_name},
            charStringsDict=charstrings,
            privateDict={},
        )
        fb.setupHorizontalMetrics(metrics_dict)
        fb.setupHorizontalHeader(ascent=ascent, descent=descent)
        self._setup_standard_tables(fb, ps_name, ascent, descent, win_ascent, win_descent)

        # Attach OpenType GPOS kerning table if typography dataset provided
        if typography:
            attach_gpos_to_font(fb.font, typography, cmap)

        # Deterministic timestamps
        fb.font["head"].created = 0
        fb.font["head"].modified = 0

        filename = f"{ps_name}.otf"
        out_path = output_dir / filename
        buffer = io.BytesIO()
        fb.save(buffer)
        raw_bytes = buffer.getvalue()
        out_path.write_bytes(raw_bytes)

        return CandidateFontArtifact(
            format="OTF",
            filename=filename,
            file_path=out_path,
            size_bytes=len(raw_bytes),
            sha256_hex=hashlib.sha256(raw_bytes).hexdigest(),
            glyph_count=len(glyph_order),
            units_per_em=self.units_per_em,
        )

    def build_candidate_ttf(
        self,
        glyph_map: dict[int, ReconstructedGlyph],
        output_dir: Path,
        typography: TypographyDataset | None = None,
    ) -> CandidateFontArtifact:
        """Build TrueType font (.ttf) by deriving quadratic Béziers via cu2qu."""
        sorted_cps = sorted(glyph_map.keys())
        if 0x20 not in sorted_cps:
            sorted_cps = [0x20] + sorted_cps
        glyph_order = [".notdef"] + [get_glyph_name_for_codepoint(cp) for cp in sorted_cps]
        cmap = {cp: get_glyph_name_for_codepoint(cp) for cp in sorted_cps}

        ascent = int(max((g.ascent_upem for g in glyph_map.values()), default=800))
        descent = int(min((-abs(g.descent_upem) for g in glyph_map.values()), default=-200))
        win_ascent = max(ascent, abs(descent))
        win_descent = abs(descent)

        metrics_dict: dict[str, tuple[int, int]] = {".notdef": (500, 50), "space": (250, 0)}
        for cp, g in glyph_map.items():
            g_name = get_glyph_name_for_codepoint(cp)
            adv = int(round(g.advance_width_upem))
            lsb = int(round(g.lsb_upem))
            metrics_dict[g_name] = (adv, lsb)

        fb = FontBuilder(unitsPerEm=self.units_per_em, isTTF=True)
        fb.setupGlyphOrder(glyph_order)
        fb.setupCharacterMap(cmap)

        glyphs_dict: dict[str, Any] = {}

        # 1. .notdef
        notdef_pen = TTGlyphPen(None)
        notdef_pen.moveTo((50, 0))
        notdef_pen.lineTo((450, 0))
        notdef_pen.lineTo((450, 700))
        notdef_pen.lineTo((50, 700))
        notdef_pen.closePath()
        notdef_pen.moveTo((100, 50))
        notdef_pen.lineTo((100, 650))
        notdef_pen.lineTo((400, 650))
        notdef_pen.lineTo((400, 50))
        notdef_pen.closePath()
        glyphs_dict[".notdef"] = notdef_pen.glyph()

        # 2. space (empty glyph)
        if "space" not in glyphs_dict:
            space_pen = TTGlyphPen(None)
            glyphs_dict["space"] = space_pen.glyph()

        # 3. Master cubic to TrueType quadratic conversion via Cu2QuPen
        for cp in sorted_cps:
            if cp == 0x20 and cp not in glyph_map:
                continue
            g = glyph_map[cp]
            g_name = get_glyph_name_for_codepoint(cp)
            tt_pen = TTGlyphPen(None)
            cu2qu_pen = Cu2QuPen(tt_pen, max_err=self.cu2qu_max_err)
            draw_reconstructed_glyph_to_pen(g, cu2qu_pen)
            glyphs_dict[g_name] = tt_pen.glyph()

        fb.setupGlyf(glyphs_dict)
        fb.setupHorizontalMetrics(metrics_dict)
        fb.setupHorizontalHeader(ascent=ascent, descent=descent)
        ps_name = f"{self.family_name.replace(' ', '')}-{self.style_name.replace(' ', '')}"
        self._setup_standard_tables(fb, ps_name, ascent, descent, win_ascent, win_descent)

        # Attach OpenType GPOS kerning table if typography dataset provided
        if typography:
            attach_gpos_to_font(fb.font, typography, cmap)

        # Deterministic timestamps
        fb.font["head"].created = 0
        fb.font["head"].modified = 0

        filename = f"{ps_name}.ttf"
        out_path = output_dir / filename
        buffer = io.BytesIO()
        fb.save(buffer)
        raw_bytes = buffer.getvalue()
        out_path.write_bytes(raw_bytes)

        return CandidateFontArtifact(
            format="TTF",
            filename=filename,
            file_path=out_path,
            size_bytes=len(raw_bytes),
            sha256_hex=hashlib.sha256(raw_bytes).hexdigest(),
            glyph_count=len(glyph_order),
            units_per_em=self.units_per_em,
        )

    def derive_candidate_woff2(
        self,
        sfnt_path: Path,
        output_dir: Path,
    ) -> CandidateFontArtifact:
        """Derive compressed WOFF2 candidate font from validated SFNT binary."""
        font = TTFont(sfnt_path)
        font.flavor = "woff2"
        if "head" in font:
            font["head"].created = 0
            font["head"].modified = 0

        ps_name = f"{self.family_name.replace(' ', '')}-{self.style_name.replace(' ', '')}"
        filename = f"{ps_name}.woff2"
        out_path = output_dir / filename

        buffer = io.BytesIO()
        font.save(buffer)
        raw_bytes = buffer.getvalue()
        out_path.write_bytes(raw_bytes)

        glyph_order = font.getGlyphOrder()

        return CandidateFontArtifact(
            format="WOFF2",
            filename=filename,
            file_path=out_path,
            size_bytes=len(raw_bytes),
            sha256_hex=hashlib.sha256(raw_bytes).hexdigest(),
            glyph_count=len(glyph_order),
            units_per_em=font["head"].unitsPerEm,
        )

    def _setup_standard_tables(
        self,
        fb: FontBuilder,
        ps_name: str,
        ascent: int,
        descent: int,
        win_ascent: int,
        win_descent: int,
    ) -> None:
        """Setup standard name, OS/2, and post tables deterministically."""
        full_name = f"{self.family_name} {self.style_name}".strip()
        name_strings = {
            "familyName": self.family_name,
            "styleName": self.style_name,
            "uniqueFontIdentifier": f"1.000;TeleFontMAX;{ps_name}",
            "fullName": full_name,
            "version": "Version 1.000;MAX-Pipeline-C",
            "psName": ps_name,
        }
        fb.setupNameTable(name_strings)

        is_italic = "italic" in self.style_name.lower()
        is_bold = "bold" in self.style_name.lower() or self.weight_class >= 700
        fs_selection = 0
        if is_italic:
            fs_selection |= 0x01
        if is_bold:
            fs_selection |= 0x20
        if not is_italic and not is_bold:
            fs_selection |= 0x40

        fb.setupOS2(
            sTypoAscender=ascent,
            sTypoDescender=descent,
            sTypoLineGap=200,
            usWinAscent=win_ascent,
            usWinDescent=win_descent,
            sxHeight=500,
            sCapHeight=700,
            usWeightClass=self.weight_class,
            usWidthClass=5,
            fsSelection=fs_selection,
            ulCodePageRange1=1,
            ulUnicodeRange1=1,
        )
        fb.setupPost()
