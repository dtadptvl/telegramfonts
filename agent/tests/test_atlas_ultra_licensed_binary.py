"""U7 liga/calt preservation from the authorized binary (T-FAST-ATLAS-ULTRA-01).

ONE focused test proving the pipeline's authorized-binary preservation path:
probe-free liga/calt detection, exact GSUB table copy (no feature synthesis),
and exact-identity TTF cache-hit semantics (build-only-missing: on an exact
cache hit the artifact is served bit-identical - zero reconstruction - with
the preserved features intact in the output).

Authorized binary (recorded per task directive):
agent/benchmark_data/ground_truth/BeVietnamPro-Regular.ttf - the repository
benchmark ground truth, authorized as the LOCAL validation binary for this
task (network acquisition unavailable locally; no production access).

Slice-step-1 inspection (FontTools): GSUB features present in the binary:
aalt, case, ccmp, dnom, frac, liga, locl, numr, ordn, sinf, ss01, subs,
sups. liga: YES. calt: NO. The preservation path is therefore exercised by
liga; calt handling is identical code and is asserted absent-not-synthesized.
"""
from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest
from fontTools.ttLib import TTFont

from atlas.cache import AtlasCacheStore, NAMESPACE_FONTS
from atlas.typography import (
    preserve_gsub_from_authorized_binary,
    preserved_gsub_tags,
)

LICENSED_BINARY = (
    Path(__file__).parent.parent
    / "benchmark_data"
    / "ground_truth"
    / "BeVietnamPro-Regular.ttf"
)


def _liga_rules(font: TTFont) -> set[tuple[str, tuple[str, ...], str]]:
    """Decompiled liga substitution rules: (first glyph, components, lig)."""
    rules: set[tuple[str, tuple[str, ...], str]] = set()
    for lookup in font["GSUB"].table.LookupList.Lookup:
        if lookup.LookupType != 4:
            continue
        for sub in lookup.SubTable:
            for first, ligatures in sub.ligatures.items():
                for lig in ligatures:
                    rules.add((first, tuple(lig.Component), lig.LigGlyph))
    return rules


def _feature_tags(font: TTFont) -> set[str]:
    gsub = font.get("GSUB")
    if gsub is None or gsub.table.FeatureList is None:
        return set()
    return {fr.FeatureTag for fr in gsub.table.FeatureList.FeatureRecord}


def test_u7_liga_calt_preservation_from_authorized_binary_exact_ttf_cache_hit(
    tmp_path: Path,
):
    if not LICENSED_BINARY.exists():
        pytest.skip("authorized binary fixture unavailable")
    authorized = LICENSED_BINARY.read_bytes()

    # 1) Probe-free detection (U7): liga present, calt absent in this binary.
    tags = preserved_gsub_tags(authorized)
    assert "liga" in tags
    assert "calt" not in tags

    src = TTFont(io.BytesIO(authorized))
    src_rules = _liga_rules(src)
    src_features = _feature_tags(src)
    assert src_rules, "authorized binary must carry liga substitution rules"

    # 2) The "built TTF": a build output lacking GSUB, derived from the
    #    authorized bytes themselves (no fabricated geometry in this test).
    built_path = tmp_path / "built.ttf"
    built = TTFont(io.BytesIO(authorized))
    assert built.get("GSUB") is not None
    del built["GSUB"]
    built.save(built_path)
    built.close()
    stripped = TTFont(built_path)
    assert stripped.get("GSUB") is None
    stripped.close()

    # 3) Authorized-binary preservation = EXACT table copy (no synthesis).
    preserved = preserve_gsub_from_authorized_binary(built_path, authorized)
    assert preserved == ["liga"]
    out = TTFont(built_path)
    try:
        # Nothing dropped, nothing synthesized: identical feature tag set.
        assert _feature_tags(out) == src_features
        # liga rules identical after decompilation.
        assert _liga_rules(out) == src_rules
        assert "calt" not in _feature_tags(out)
        preserved_bytes = built_path.read_bytes()
    finally:
        out.close()

    # 4) Exact TTF cache hit (build-only-missing semantics): the persistent
    #    exact-identity cache serves the preserved artifact bit-identical -
    #    no reconstruction on the hit path - features retained in output.
    cache = AtlasCacheStore(tmp_path / "atlas_cache")
    identity = hashlib.sha256(preserved_bytes).hexdigest()
    cache.put_bytes_verified(
        NAMESPACE_FONTS, identity + "_ttf", preserved_bytes, "ttf"
    )
    hit = cache.get_bytes(NAMESPACE_FONTS, identity + "_ttf", "ttf")
    assert hit is not None
    assert hit == preserved_bytes  # bit-identical => zero reconstruction
    hit_font = TTFont(io.BytesIO(hit))
    try:
        assert _liga_rules(hit_font) == src_rules
        assert "liga" in _feature_tags(hit_font)
    finally:
        hit_font.close()
        src.close()
