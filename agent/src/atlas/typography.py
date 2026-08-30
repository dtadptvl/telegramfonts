"""Typography under the speed-first policy (ADR-0004, U7).

- Selective BOUNDED kerning pairs only (never N^2).
- GPOS kern ONLY for measured material deltas.
- liga/calt preserved from authorized binaries (exact table copy).
- Reconstructed liga/calt only on exact observable mapping (the atlas pass
  never observes substitution evidence, so it reconstructs none).
- NO ss01-ss20 probing: any stylistic-set probe fails closed.
"""
from __future__ import annotations

import io
from pathlib import Path

from fontTools.ttLib import TTFont

from typography.models import PairKerningObservation, TypographyDataset

MAX_KERN_CANDIDATES = 256
MIN_MATERIAL_DELTA_UPEM = 10

# Deterministic high-value Latin kerning candidates (bounded, non-N^2).
_CANDIDATE_LEFT = "AVWYLTPOBF"
_CANDIDATE_RIGHT = "Aavo.,y*%"

_STYLISTIC_SET_TAGS = frozenset(f"ss{i:02d}" for i in range(1, 21))


def assert_no_stylistic_set_probing(tags: set[str] | frozenset[str] | tuple[str, ...]) -> None:
    """ss01-ss20 probing is prohibited under the speed-first policy."""
    probe = set(tags) & _STYLISTIC_SET_TAGS
    if probe:
        raise ValueError(f"STYLISTIC_SET_PROBING_PROHIBITED:{sorted(probe)}")


def candidate_kern_pairs(code_points: set[int]) -> list[tuple[int, int]]:
    """Deterministic bounded candidate pair set (selective, never N^2)."""
    covered = set(code_points)
    pairs: list[tuple[int, int]] = []
    for l_ch in _CANDIDATE_LEFT:
        for r_ch in _CANDIDATE_RIGHT:
            if l_ch == r_ch:
                continue
            if ord(l_ch) in covered and ord(r_ch) in covered:
                pairs.append((ord(l_ch), ord(r_ch)))
            if len(pairs) >= MAX_KERN_CANDIDATES:
                return pairs
    return pairs[:MAX_KERN_CANDIDATES]


def kerning_batch_texts(pairs: list[tuple[int, int]]) -> list[str]:
    """Pair strings for one batched pair-advance measurement call."""
    return [chr(l) + chr(r) for (l, r) in pairs]


def select_material_kerning(
    pair_deltas_upem: dict[tuple[int, int], float],
) -> dict[tuple[int, int], int]:
    """GPOS kern ONLY for measured material deltas (bounded).

    Deltas below MIN_MATERIAL_DELTA_UPEM (either sign) are noise and never
    become kern pairs. Values are rounded to whole UPEM units.
    """
    selected: dict[tuple[int, int], int] = {}
    for (l_cp, r_cp), delta in sorted(pair_deltas_upem.items()):
        if abs(delta) < MIN_MATERIAL_DELTA_UPEM:
            continue
        selected[(int(l_cp), int(r_cp))] = int(round(delta))
        if len(selected) >= MAX_KERN_CANDIDATES:
            break
    return selected


def build_typography_dataset(
    family_name: str,
    style_name: str,
    kern_pairs: dict[tuple[int, int], int],
    observations: list[PairKerningObservation] | None = None,
    provenance: str = "atlas_ultra_batched_pair_metrics",
) -> TypographyDataset:
    return TypographyDataset(
        family_name=family_name,
        style_name=style_name,
        units_per_em=1000,
        kerning_pairs=dict(kern_pairs),
        observations=observations or [],
        total_pairs_probed=len(observations) if observations else len(kern_pairs),
        active_kerning_pairs_count=len(kern_pairs),
        provenance=provenance,
        fit_rows_count=len(kern_pairs),
    )


def preserved_gsub_tags(authorized_binary: bytes | None) -> tuple[str, ...]:
    """liga/calt tags present in the authorized binary (probe-free)."""
    if not authorized_binary:
        return ()
    try:
        font = TTFont(io.BytesIO(authorized_binary))
    except Exception:
        return ()
    try:
        gsub = font.get("GSUB")
        if gsub is None:
            return ()
        features = gsub.table.FeatureList.FeatureRecord if gsub.table.FeatureList else []
        tags = tuple(
            sorted({f.FeatureTag for f in features if f.FeatureTag in ("liga", "calt")})
        )
        return tags
    finally:
        font.close()


def preserve_gsub_from_authorized_binary(
    built_font_path: Path,
    authorized_binary: bytes | None,
) -> list[str]:
    """Preserve liga/calt from the authorized binary into the built font.

    Exact table copy (no feature synthesis, no ss01-ss20 probing). The
    atlas raster pass reconstructs liga/calt only on exact observable
    mapping; it observes no substitution evidence, so reconstruction adds
    nothing and preservation is the only liga/calt source.
    """
    if not authorized_binary:
        return []
    try:
        source = TTFont(io.BytesIO(authorized_binary))
    except Exception:
        return []
    try:
        gsub = source.get("GSUB")
        if gsub is None:
            return []
        features = gsub.table.FeatureList.FeatureRecord if gsub.table.FeatureList else []
        tags = {f.FeatureTag for f in features if f.FeatureTag in ("liga", "calt")}
        if not tags:
            return []
        target = TTFont(built_font_path)
        try:
            target["GSUB"] = source["GSUB"]
            target.save(built_font_path)
        finally:
            target.close()
        return sorted(tags)
    finally:
        source.close()
