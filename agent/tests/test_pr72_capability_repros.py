"""Issue #71 comment 5413523258 KNOWN_REPRO pack (provider capability).

CAPABILITY_IDENTITY (sealed descriptor / forged / cross-provider reuse),
CAPABILITY_PARTITION (disjoint size-axis fit/held-out, no leak), and
tamper/threshold-bypass fail-closed checks. The positive publish proof is
test_RASTER_HANDOFF_cdn_pixels_immutable_no_browser_recapture (Stage 9D
publishes only after held-out sizes + four consumers PASS).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from acquisition.capability import FIXED_PHASE, PROVIDER_MONOTYPE_RENDER, ProviderRasterCapability
from acquisition.raster_ingest import ingest_raster_pages
from fidelity.pipeline import ObservationStoreSnapshot, partition_snapshot
from measurement.store import ObservationStore
from tests.test_issue71_adversarial import ISSUE71_CONFIG
from tests.test_issue72_review_repros import (
    _browser_supplement_for_seed,
    _expected_seed_slice,
    _raster_pages_for_seed,
)

CAPABILITY = ProviderRasterCapability.deterministic_size_schedule(
    PROVIDER_MONOTYPE_RENDER, ISSUE71_CONFIG.resolutions
)


def _ingest_seed(store_dir: Path, browser_version: str = "chromium_cap_v1") -> ObservationStore:
    store = ObservationStore(store_dir)
    pages = [
        _raster_pages_for_seed("monotype_render_105", acs_pt=pt)[0]
        for pt in CAPABILITY.all_sizes()
    ]
    supplement = _browser_supplement_for_seed(browser_version)
    ingest_raster_pages(
        store, ISSUE71_CONFIG, "cap_fam", "regular", supplement, pages, CAPABILITY,
        source_url="https://www.myfonts.com/collections/cap-fam",
    )
    return store


def test_CAPABILITY_PARTITION_disjoint_sizes_and_no_held_out_leak(tmp_path: Path):
    store = _ingest_seed(tmp_path / "obs_partition")
    snapshot = ObservationStoreSnapshot.load_from_store(
        store=store,
        reference_id="cap_fam",
        style_id="regular",
        family_name="Cap Fam",
        style_name="Regular",
        config=ISSUE71_CONFIG,
        browser_version="chromium_cap_v1",
        expected_capability=CAPABILITY,
    )
    assert snapshot.provider_capability == CAPABILITY

    partition = partition_snapshot(snapshot)
    fit_sizes = {r.resolution for r in partition.fit_records}
    held_sizes = {r.resolution for r in partition.held_out_records}
    # Fit pixels are exactly the fit sizes; held-out CDN pixels stay sealed
    # in the disjoint set (never read by fitting/optimization).
    assert fit_sizes == set(CAPABILITY.fit_sizes)
    assert held_sizes == set(CAPABILITY.held_out_sizes)
    assert not (fit_sizes & held_sizes)
    assert partition.fit_records and partition.held_out_records
    fit_keys = {r.cache_key for r in partition.fit_records}
    held_keys = {r.cache_key for r in partition.held_out_records}
    assert not (fit_keys & held_keys)
    # Held-out pixels are the unseen CDN slices at held-out sizes.
    for r in partition.held_out_records:
        assert snapshot.get_raster_bytes(r.cache_key) == _expected_seed_slice(
            r.code_point, r.resolution
        )
    # Pair partition stays disjoint and non-empty on both sides.
    assert partition.fit_pairs and partition.held_out_pairs
    assert not (
        {(p.left_cp, p.right_cp) for p in partition.fit_pairs}
        & {(p.left_cp, p.right_cp) for p in partition.held_out_pairs}
    )


def test_CAPABILITY_PARTITION_drift_and_missing_size_fail_closed(tmp_path: Path):
    """Records outside the sealed schedule (wrong size or phase) fail closed."""
    import hashlib

    from measurement.models import ObservationRecord

    store = _ingest_seed(tmp_path / "obs_drift")
    cfg_h = ISSUE71_CONFIG.compute_hash()

    base_records = []
    raster_map = {}
    for cp in (65, 66):
        for rec, _ in store.get_glyph_observations(
            "cap_fam", "regular", cp, browser_version="chromium_cap_v1", config_hash=cfg_h
        ):
            base_records.append(rec)
            raster_map[rec.cache_key] = (store.base_dir / rec.raster_relative_path).read_bytes()
    pairs = ()

    def _snapshot_with(extra: ObservationRecord, png: bytes):
        raster_map_ext = dict(raster_map)
        raster_map_ext[extra.cache_key] = png
        return ObservationStoreSnapshot(
            reference_id="cap_fam",
            style_id="regular",
            family_name="Cap Fam",
            style_name="Regular",
            browser_version="chromium_cap_v1",
            config=ISSUE71_CONFIG,
            records=tuple(base_records + [extra]),
            raster_bytes_map=raster_map_ext,
            pairs=pairs,
            provider_capability=CAPABILITY,
        )

    def _rogue_record(resolution: int, sx: float, sy: float) -> tuple[ObservationRecord, bytes]:
        png = b"\x89PNG\r\n\x1a\n" + b"x" * 8
        base = base_records[0]
        return (
            ObservationRecord(
                cache_key=ObservationRecord.build_cache_key(
                    reference_id="cap_fam", style_id="regular", code_point=65,
                    browser_version="chromium_cap_v1", resolution=resolution,
                    subpixel_x=sx, subpixel_y=sy, config_hash=cfg_h,
                ),
                reference_id="cap_fam", style_id="regular", code_point=65,
                resolution=resolution, subpixel_x=sx, subpixel_y=sy,
                raster_relative_path=f"rasters/cap_fam/regular/0041/{resolution}_{sx}_{sy}.png",
                raster_sha256=hashlib.sha256(png).hexdigest(),
                raster_size_bytes=len(png),
                metrics=base.metrics, created_at=base.created_at,
                browser_version="chromium_cap_v1", config_hash=cfg_h,
            ),
            png,
        )

    # Unallocated size drifts against the sealed capability.
    rogue, png = _rogue_record(999, 0.0, 0.0)
    with pytest.raises(ValueError, match="CAPABILITY_DRIFT"):
        partition_snapshot(_snapshot_with(rogue, png))

    # Non-fixed phase drifts against the size-axis-only capability.
    phased, png2 = _rogue_record(CAPABILITY.fit_sizes[0], 0.25, 0.25)
    with pytest.raises(ValueError, match="CAPABILITY_DRIFT"):
        partition_snapshot(_snapshot_with(phased, png2))


def test_CAPABILITY_IDENTITY_cross_provider_and_tamper_fail_closed(tmp_path: Path):
    """Forged, drifted, and cross-provider capability reuse fail closed at
    snapshot load; direct-browser collections reject sealed capabilities."""
    store = _ingest_seed(tmp_path / "obs_identity")

    # Correct expectation loads.
    ObservationStoreSnapshot.load_from_store(
        store=store, reference_id="cap_fam", style_id="regular",
        family_name="Cap Fam", style_name="Regular", config=ISSUE71_CONFIG,
        browser_version="chromium_cap_v1", expected_capability=CAPABILITY,
    )

    # No expectation against a sealed collection = cross-provider reuse attempt.
    with pytest.raises(ValueError, match="STORE_LOAD_ERROR"):
        ObservationStoreSnapshot.load_from_store(
            store=store, reference_id="cap_fam", style_id="regular",
            family_name="Cap Fam", style_name="Regular", config=ISSUE71_CONFIG,
            browser_version="chromium_cap_v1", expected_capability=None,
        )

    # A different provider/schedule (forged capability) fails closed.
    forged = ProviderRasterCapability(
        provider="other_provider_v9", phase=FIXED_PHASE,
        fit_sizes=(128,), held_out_sizes=(256,),
    )
    with pytest.raises(ValueError, match="STORE_LOAD_ERROR"):
        ObservationStoreSnapshot.load_from_store(
            store=store, reference_id="cap_fam", style_id="regular",
            family_name="Cap Fam", style_name="Regular", config=ISSUE71_CONFIG,
            browser_version="chromium_cap_v1", expected_capability=forged,
        )

    # Tampered sealed hash (threshold bypass attempt) fails closed.
    cfg_h = ISSUE71_CONFIG.compute_hash()
    with store._get_connection() as conn:
        conn.execute(
            "UPDATE source_collections SET capability_hash = ? WHERE collection_key = ?",
            ("f" * 64, "cap_fam:regular:chromium_cap_v1:" + cfg_h),
        )
        conn.commit()
    with pytest.raises(ValueError, match="STORE_LOAD_ERROR"):
        ObservationStoreSnapshot.load_from_store(
            store=store, reference_id="cap_fam", style_id="regular",
            family_name="Cap Fam", style_name="Regular", config=ISSUE71_CONFIG,
            browser_version="chromium_cap_v1", expected_capability=CAPABILITY,
        )


def test_CAPABILITY_IDENTITY_direct_browser_rejects_sealed_capability(tmp_path: Path):
    """Direct-browser collections carry no descriptor; expecting one fails
    closed (no capability laundering into the phase-held-out path)."""
    from tests.test_issue71_adversarial import _seed_store

    store_dir = tmp_path / "obs_direct"
    store_dir.mkdir()
    import asyncio

    asyncio.run(_seed_store(store_dir, "direct_fam", "regular"))
    store = ObservationStore(store_dir)

    # No expectation loads fine (phase-held-out partition preserved).
    snapshot = ObservationStoreSnapshot.load_from_store(
        store=store, reference_id="direct_fam", style_id="regular",
        family_name="Direct Fam", style_name="Regular", config=ISSUE71_CONFIG,
        browser_version="chromium_issue71_test", expected_capability=None,
    )
    assert snapshot.provider_capability is None
    partition = partition_snapshot(snapshot)
    assert partition.fit_records and partition.held_out_records

    # Expecting a provider capability on a direct-browser collection fails.
    with pytest.raises(ValueError, match="STORE_LOAD_ERROR"):
        ObservationStoreSnapshot.load_from_store(
            store=store, reference_id="direct_fam", style_id="regular",
            family_name="Direct Fam", style_name="Regular", config=ISSUE71_CONFIG,
            browser_version="chromium_issue71_test", expected_capability=CAPABILITY,
        )
