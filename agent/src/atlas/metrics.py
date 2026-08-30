"""Batched measureText metrics + multi-size regression to UPEM=1000 (U3).

ADR-0004: batch measureText for the COMPLETE glyph set at sizes 512/1024/
2048 (width, actual bbox, ascent/descent, font bbox), normalized by
multi-size regression to UPEM=1000, with a FEW batched JS calls only -
never per-glyph calls. Metrics acquisition is concurrent with HTTP atlas
acquisition (the pipeline schedules them as sibling tasks).

The transport is injectable: production binds the single persistent
Chromium session (started lazily only when actually needed); focused tests
bind deterministic fakes. The batch protocol is identical either way.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from atlas.models import GlyphMetricsObservation, GlobalMetricsRegression, RegressedMetrics
from atlas.policy import METRICS_SIZES_PX

# One batched JS call carries at most this many glyphs; the complete glyph
# set therefore consumes ceil(n / METRICS_BATCH_MAX) calls per size.
METRICS_BATCH_MAX = 512

# JS batch protocol: one evaluate() per (size, chunk). Returns a compact
# array-of-arrays to minimize serialization overhead. Never per-glyph.
MEASURE_TEXT_JS_TEMPLATE = """((chars, size, font) => {
  const c = globalThis.__atlasCanvas || (globalThis.__atlasCanvas = document.createElement('canvas'));
  const ctx = c.getContext('2d', { willReadFrequently: false });
  ctx.font = size + 'px ' + font;
  ctx.textBaseline = 'alphabetic';
  const out = [];
  for (let i = 0; i < chars.length; i++) {
    const m = ctx.measureText(chars[i]);
    out.push([
      m.width,
      m.actualBoundingBoxLeft, m.actualBoundingBoxRight,
      m.actualBoundingBoxAscent, m.actualBoundingBoxDescent,
      m.fontBoundingBoxAscent, m.fontBoundingBoxDescent,
    ]);
  }
  return out;
})({chars_json}, {size}, {font_json})"""


def build_metrics_batches(
    code_points: list[int],
    sizes: tuple[int, ...] = METRICS_SIZES_PX,
    max_batch: int = METRICS_BATCH_MAX,
) -> list[tuple[int, list[int]]]:
    """Deterministic batch plan: (size, chunk-of-codepoints) JS calls.

    Total call count is len(sizes) * ceil(n / max_batch): a few batched
    calls for the complete glyph set (never per-glyph).
    """
    if max_batch <= 0:
        raise ValueError("METRICS_BATCH_MAX_INVALID")
    ordered = sorted(set(int(cp) for cp in code_points))
    batches: list[tuple[int, list[int]]] = []
    for size in sizes:
        for i in range(0, len(ordered), max_batch):
            batches.append((int(size), ordered[i:i + max_batch]))
    return batches


def metrics_js_call_count(glyph_count: int, sizes: tuple[int, ...] = METRICS_SIZES_PX) -> int:
    """Exact batched-call count for evidence records (never per-glyph)."""
    if glyph_count <= 0:
        return 0
    chunks = (glyph_count + METRICS_BATCH_MAX - 1) // METRICS_BATCH_MAX
    return chunks * len(sizes)


def build_measure_text_js(chars: list[str], size_px: int, font_family: str) -> str:
    """Render the batched measureText JS payload for one (size, chunk).

    Substitution uses str.replace (never str.format): the JS template body
    carries literal braces that format() would misparse.
    """
    import json

    return (
        MEASURE_TEXT_JS_TEMPLATE.replace("{chars_json}", json.dumps(chars, ensure_ascii=False))
        .replace("{size}", str(int(size_px)))
        .replace("{font_json}", json.dumps(font_family))
    )


def parse_measure_text_rows(
    code_points: list[int],
    size_px: float,
    rows: list[list[float]],
) -> list[GlyphMetricsObservation]:
    """Decode batched JS rows into per-glyph observations (fail closed)."""
    if len(rows) != len(code_points):
        raise ValueError("METRICS_BATCH_ROW_COUNT_MISMATCH")
    out: list[GlyphMetricsObservation] = []
    for cp, row in zip(code_points, rows):
        if len(row) != 7 or any(v is None or not math.isfinite(float(v)) for v in row):
            raise ValueError(f"METRICS_BATCH_ROW_INVALID_CP_{cp:04X}")
        (w, al, ar, aa, ad, fa, fd) = (float(v) for v in row)
        out.append(
            GlyphMetricsObservation(
                code_point=int(cp),
                size_px=float(size_px),
                width_px=w,
                actual_left_px=al,
                actual_right_px=ar,
                actual_ascent_px=aa,
                actual_descent_px=ad,
                font_ascent_px=fa,
                font_descent_px=fd,
            )
        )
    return out


def _slope_through_origin(sizes: list[float], values: list[float]) -> tuple[float, float]:
    """Least-squares slope through the origin + normalized RMS residual.

    value_px ~= k * size_px; residual is RMS of (value - k*size)/size over
    the sizes (dimensionless; 0.0 is a perfect linear fit).
    """
    denom = sum(s * s for s in sizes)
    if denom <= 0:
        raise ValueError("METRICS_REGRESSION_NO_SIZES")
    k = sum(s * v for s, v in zip(sizes, values)) / denom
    resid_sq = 0.0
    for s, v in zip(sizes, values):
        if s <= 0:
            continue
        r = (v - k * s) / s
        resid_sq += r * r
    resid = math.sqrt(resid_sq / len(sizes)) if sizes else float("inf")
    return k, resid


@dataclass(frozen=True)
class _MetricSeries:
    sizes: list[float]
    width: list[float]
    left: list[float]
    right: list[float]
    ascent: list[float]
    descent: list[float]
    font_ascent: list[float]
    font_descent: list[float]


def regress_glyph_metrics(
    observations: list[GlyphMetricsObservation],
    sizes: tuple[int, ...] = METRICS_SIZES_PX,
) -> RegressedMetrics:
    """Multi-size regression of one glyph's metrics to UPEM=1000.

    Every declared size must be observed (fail closed). Each metric is fit
    through the origin across sizes; the UPEM value is slope * 1000. The
    glyph's regression residual is the worst normalized component residual
    (width/lsb/ascent/descent), used by the cheap FIT-confidence check.
    """
    if not observations:
        raise ValueError("METRICS_REGRESSION_NO_OBSERVATIONS")
    cp = observations[0].code_point
    by_size = {round(o.size_px, 3): o for o in observations}
    missing = [s for s in sizes if float(s) not in by_size]
    if missing:
        raise ValueError(f"METRICS_REGRESSION_MISSING_SIZES_CP_{cp:04X}")

    ordered = [by_size[float(s)] for s in sizes]
    sz = [float(s) for s in sizes]
    series = _MetricSeries(
        sizes=sz,
        width=[o.width_px for o in ordered],
        left=[o.actual_left_px for o in ordered],
        right=[o.actual_right_px for o in ordered],
        ascent=[o.actual_ascent_px for o in ordered],
        descent=[o.actual_descent_px for o in ordered],
        font_ascent=[o.font_ascent_px for o in ordered],
        font_descent=[o.font_descent_px for o in ordered],
    )

    k_adv, r_adv = _slope_through_origin(sz, series.width)
    k_lsb, r_lsb = _slope_through_origin(sz, series.left)
    k_right, _ = _slope_through_origin(sz, series.right)
    k_asc, r_asc = _slope_through_origin(sz, series.ascent)
    k_desc, r_desc = _slope_through_origin(sz, series.descent)

    advance = k_adv * 1000.0
    lsb = k_lsb * 1000.0
    ascent = k_asc * 1000.0
    descent = -abs(k_desc * 1000.0)
    bbox_w = (k_lsb + k_right) * 1000.0
    bbox_h = (k_asc + k_desc) * 1000.0
    rsb = advance - lsb - bbox_w

    residual = max(r_adv, r_lsb, r_asc, r_desc)
    return RegressedMetrics(
        code_point=cp,
        advance_width_upem=advance,
        lsb_upem=lsb,
        rsb_upem=rsb,
        ascent_upem=ascent,
        descent_upem=descent,
        bbox_upem=(lsb, ascent - bbox_h, lsb + bbox_w, ascent),
        regression_residual=residual,
    )


def regress_global_metrics(
    observations: list[GlyphMetricsObservation],
    sizes: tuple[int, ...] = METRICS_SIZES_PX,
) -> GlobalMetricsRegression:
    """Font-bbox ascent/descent regression across the batched observations.

    Uses the MAX font-bbox per size across all observed glyphs (the font
    bounding box is font-global; the batch rows repeat it per glyph, so any
    glyph carries it - the max is the deterministic choice).
    """
    by_size: dict[float, tuple[float, float]] = {}
    for o in observations:
        s = round(o.size_px, 3)
        fa, fd = by_size.get(s, (0.0, 0.0))
        by_size[s] = (max(fa, o.font_ascent_px), max(fd, o.font_descent_px))
    missing = [s for s in sizes if float(s) not in by_size]
    if missing:
        raise ValueError("GLOBAL_METRICS_REGRESSION_MISSING_SIZES")
    sz = [float(s) for s in sizes]
    fas = [by_size[float(s)][0] for s in sizes]
    fds = [by_size[float(s)][1] for s in sizes]
    k_asc, r_asc = _slope_through_origin(sz, fas)
    k_desc, r_desc = _slope_through_origin(sz, fds)
    return GlobalMetricsRegression(
        font_ascent_upem=k_asc * 1000.0,
        font_descent_upem=-abs(k_desc * 1000.0),
        regression_residual=max(r_asc, r_desc),
    )
