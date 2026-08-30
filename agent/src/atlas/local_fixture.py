"""Local fixture providers for the atlas pipeline (TEST/FIXTURE ONLY).

These providers render atlas cells and batched metrics from a local font
file with native Pillow/FontTools operations. They stand in for the
HTTP/CDN raster source and browser measureText when the authorized network
acquisition path is unavailable (e.g., local speed fixtures). They are
NEVER selectable in the production acquisition cascade: the runner only
ever injects them under an explicit fixture identity, and the substitution
is recorded honestly in the fixture evidence.
"""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from atlas.paging import CELL_PAD_X_PX, CELL_PAD_Y_PX, cell_dimensions


class LocalFontMetricsProvider:
    """Batched metrics from a local font file (measureText analog)."""

    def __init__(self, font_path: Path | str) -> None:
        self.font_path = Path(font_path)

    def _font(self, size_px: int) -> ImageFont.FreeTypeFont:
        return ImageFont.truetype(str(self.font_path), int(size_px))

    def fetch_rows(self, size_px: int, code_points: list[int]) -> list[list[float]]:
        font = self._font(size_px)
        font_ascent, font_descent = font.getmetrics()
        rows: list[list[float]] = []
        for cp in code_points:
            ch = chr(cp)
            width = float(font.getlength(ch))
            try:
                l, t, r, b = font.getbbox(ch, anchor="ls")
            except ValueError:
                l, t, r, b = (0.0, 0.0, 0.0, 0.0)
            rows.append(
                [
                    width,
                    float(l),          # actualBoundingBoxLeft
                    float(r),          # actualBoundingBoxRight
                    float(-t),         # actualBoundingBoxAscent (up positive)
                    float(b),          # actualBoundingBoxDescent (down positive)
                    float(font_ascent),
                    float(font_descent),
                ]
            )
        return rows

    def fetch_pair_advances_px(self, size_px: int, pair_texts: list[str]) -> list[float]:
        font = self._font(size_px)
        return [float(font.getlength(text)) for text in pair_texts]


class LocalFontRasterProvider:
    """Atlas raster cells rendered from a local font file.

    Phase offsets are realized by rendering at 2x resolution with the
    doubled offset and box-downsampling the crop (native Pillow ops only).
    """

    def __init__(self, font_path: Path | str) -> None:
        self.font_path = Path(font_path)
        self._ascents: dict[int, float] = {}

    def _ascent_px(self, size_px: int) -> float:
        if size_px not in self._ascents:
            font = ImageFont.truetype(str(self.font_path), size_px)
            self._ascents[size_px] = float(font.getmetrics()[0])
        return self._ascents[size_px]

    def cell_size(self, code_point: int, size_px: int) -> tuple[int, int]:
        font = ImageFont.truetype(str(self.font_path), size_px)
        ch = chr(code_point)
        length = float(font.getlength(ch))
        asc, desc = font.getmetrics()
        return cell_dimensions(length, float(asc), float(desc), size_px)

    def _render_cell(
        self,
        code_point: int,
        size_px: int,
        phase_x: float,
        phase_y: float,
        dims: tuple[int, int] | None = None,
    ) -> bytes:
        if dims is not None:
            w, h = dims
        else:
            w, h = self.cell_size(code_point, size_px)
        scale = 2 if (phase_x or phase_y) else 1
        rs = size_px * scale
        font = ImageFont.truetype(str(self.font_path), rs)
        asc, _desc = font.getmetrics()
        canvas_w = w * scale
        canvas_h = h * scale
        img = Image.new("L", (canvas_w, canvas_h), 0)
        draw = ImageDraw.Draw(img)
        pen_x = CELL_PAD_X_PX * scale + phase_x * scale
        baseline_y = CELL_PAD_Y_PX * scale + asc + phase_y * scale
        draw.text((pen_x, baseline_y), chr(code_point), font=font, fill=255, anchor="ls")
        if scale > 1:
            img = img.resize((w, h), Image.Resampling.BOX)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def fetch_page_cells(
        self,
        cells: list,
        size_px: int,
        phase_x: float,
        phase_y: float,
    ) -> dict[int, bytes]:
        out: dict[int, bytes] = {}
        for cell in cells:
            out[cell.code_point] = self._render_cell(
                cell.code_point, size_px, phase_x, phase_y, dims=(cell.w, cell.h)
            )
        return out

    def fetch_refinement(
        self, code_point: int, cell_w: int, cell_h: int
    ) -> tuple[bytes | None, bytes | None, bytes | None]:
        """Exactly the single-refinement observation set (1024@0,0 +
        1024@0.5,0 + 2048@0,0); never 512, never 4096, never quarter."""
        dims = (cell_w, cell_h)
        base = self._render_cell(code_point, 1024, 0.0, 0.0, dims=dims)
        shifted = self._render_cell(code_point, 1024, 0.5, 0.0, dims=dims)
        double = self._render_cell(code_point, 2048, 0.0, 0.0, dims=dims)
        return base, shifted, double


def fixture_glyph_set(font_path: Path | str, limit: int | None = None) -> list[int]:
    """Deterministic covered code points from the font's cmap (sorted)."""
    from fontTools.ttLib import TTFont

    font = TTFont(font_path)
    try:
        cmap = font.getBestCmap() or {}
        cps = sorted(cp for cp in cmap.keys() if cp > 0x20)
    finally:
        font.close()
    if limit is not None:
        cps = cps[:limit]
    return cps
