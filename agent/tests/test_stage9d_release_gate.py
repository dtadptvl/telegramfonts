"""Stage 9D Release Gate test suite: runner release gate, fit-only convergence, attestation.

Covers Issue #71 ACCEPT reproductions:
- Deterministic bounded fit-only optimization with non-increasing trace and fail-closed non-convergence.
- Release gate PASS with authentic attestation bound to exact artifact bytes.
- Two-run convergence determinism (input fingerprint, objective trace, stop reason,
  model hash, candidate SHA, report hash).
- Fail-closed rejection: snapshot failure, drift, unsupported format, non-convergence.
- Archive attestation semantics: attested hit, legacy/tampered miss.
- Runner archive-miss integration: exact SHA continuity (gate -> archive -> package),
  attested repeat hit bypassing all work, and negative reproductions stopping
  before archive/package/upload/complete.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.integration

from compute.archive import ArchiveIdentity, FinalFontArchive
from compute.models import ArchiveSourceContext
from config import Settings
from fidelity.optimizer import (
    FitOnlyGlyphOptimizer,
    OptimizerNonConvergenceError,
    OptimizerPolicy,
)
from fidelity.release_gate import Stage9DAttestation, Stage9DReleaseGate
from measurement.browser_session import ChromiumSession
from measurement.collector import ObservationCollector
from measurement.models import ObservationConfig
from measurement.store import ObservationStore
from queue_client import CloudflareQueueClient, QueueMessage
from reconstruction.models import Contour, LineSegment, Point2D, ReconstructedGlyph
from runner import A23Runner, RunnerAction
from tests.test_stage9c_pipeline import (
    _generate_png_bytes,
    _make_dummy_metrics,
)
from worker_client import WorkerJobClient

STAGE9D_CONFIG = ObservationConfig(
    resolutions=(128, 256),
    base_subpixel_phases=((0.0, 0.0),),
    expanded_subpixel_phases=((0.0, 0.0),),
    held_out_subpixel_phases=((0.25, 0.25),),
)


def _rect_contour(x0: float, y0: float, x1: float, y1: float) -> Contour:
    return Contour(
        segments=[
            LineSegment(Point2D(x0, y0), Point2D(x1, y0)),
            LineSegment(Point2D(x1, y0), Point2D(x1, y1)),
            LineSegment(Point2D(x1, y1), Point2D(x0, y1)),
            LineSegment(Point2D(x0, y1), Point2D(x0, y0)),
        ],
        is_hole=False,
        area_upem=(x1 - x0) * (y1 - y0),
    )


def _rect_glyph(
    code_point: int = 65,
    advance_width_upem: float = 650.0,
    bbox: tuple[float, float, float, float] = (50.0, 50.0, 550.0, 700.0),
) -> ReconstructedGlyph:
    return ReconstructedGlyph(
        code_point=code_point,
        character=chr(code_point),
        advance_width_upem=advance_width_upem,
        lsb_upem=bbox[0],
        rsb_upem=advance_width_upem - bbox[2],
        ascent_upem=bbox[3],
        descent_upem=-200.0,
        contours=[_rect_contour(*bbox)],
        bounding_box_upem=bbox,
    )


def _make_fit_record(reference_id: str, style_id: str, cfg_hash: str, browser_version: str,
                     code_point: int = 65, resolution: int = 128,
                     subpixel_x: float = 0.0, subpixel_y: float = 0.0,
                     advance_width_upem: float = 650.0,
                     bbox: tuple[float, float, float, float] = (50.0, 50.0, 550.0, 700.0)):
    """Build one observation record with matching raster bytes for optimizer input."""
    from measurement.models import ObservationRecord

    png_bytes = _generate_png_bytes(resolution, bbox, advance_width_upem, subpixel_x, subpixel_y)
    metrics = _make_dummy_metrics(
        code_point=code_point,
        resolution=resolution,
        advance_width_upem=advance_width_upem,
        bbox_upem=bbox,
    )
    rec = ObservationRecord(
        cache_key=ObservationRecord.build_cache_key(
            reference_id=reference_id,
            style_id=style_id,
            code_point=code_point,
            browser_version=browser_version,
            resolution=resolution,
            subpixel_x=subpixel_x,
            subpixel_y=subpixel_y,
            config_hash=cfg_hash,
        ),
        reference_id=reference_id,
        style_id=style_id,
        code_point=code_point,
        resolution=resolution,
        subpixel_x=subpixel_x,
        subpixel_y=subpixel_y,
        raster_relative_path=f"rasters/{reference_id}/{style_id}/{code_point}/{resolution}_{subpixel_x}_{subpixel_y}.png",
        raster_sha256=hashlib.sha256(png_bytes).hexdigest(),
        raster_size_bytes=len(png_bytes),
        metrics=metrics,
        created_at="2026-08-25T00:00:00Z",
        browser_version=browser_version,
        config_hash=cfg_hash,
    )
    return rec, png_bytes


async def _seed_completed_store(store_dir: Path, config: ObservationConfig | None = None):
    """Seed a completed, verified observation collection through the REAL collector."""
    if config is None:
        config = STAGE9D_CONFIG
    store = ObservationStore(store_dir)

    session = MagicMock(spec=ChromiumSession)
    session.browser_version = "chromium_stage9d_test"
    session.start = AsyncMock()

    def fake_measure_glyph(font_family, code_point, font_size_px, upem):
        adv_upem = 650.0 if code_point == 65 else 600.0
        return _make_dummy_metrics(code_point=code_point, resolution=int(font_size_px), advance_width_upem=adv_upem, font_size_px=float(font_size_px))

    def fake_capture_raster(font_family, code_point, resolution_px, subpixel_offset=(0.0, 0.0)):
        adv_upem = 650.0 if code_point == 65 else 600.0
        bbox = (50, 50, 550, 700) if code_point == 65 else (40, 50, 560, 700)
        return _generate_png_bytes(resolution_px, bbox, adv_upem, subpixel_offset[0], subpixel_offset[1])

    def fake_measure_advance(font_family, text, font_size_px, upem):
        return 1230.0 if text == "AB" else 1240.0

    def fake_probe_feature(font_family, feature_tag, sample_text, font_size_px, upem):
        return {
            "enabled_advance_upem": 1200.0,
            "disabled_advance_upem": 1200.0,
            "enabled_raster_signature": "a",
            "disabled_raster_signature": "a",
        }

    session.measure_glyph_direct = AsyncMock(side_effect=fake_measure_glyph)
    session.capture_lossless_raster = AsyncMock(side_effect=fake_capture_raster)
    session.measure_text_advance = AsyncMock(side_effect=fake_measure_advance)
    session.probe_opentype_feature = AsyncMock(side_effect=fake_probe_feature)

    collector = ObservationCollector(session=session, store=store, config=config)
    await collector.initialize()
    await collector.collect_font_observations(
        reference_id="stage9d_family", style_id="regular", font_family="Stage9DFamily", code_points=[65, 66]
    )
    await collector.collect_pair_observations(
        reference_id="stage9d_family", style_id="regular", font_family="Stage9DFamily", pairs=[(65, 66), (66, 65)]
    )
    await collector.collect_feature_observations("stage9d_family", "regular", "Stage9DFamily")
    collector.finalize_source_collection("stage9d_family", "regular", expected_pairs=[(65, 66), (66, 65)])
    return store, config, session.browser_version


# =========================================================================
# Optimizer unit tests
# =========================================================================

def test_optimizer_deterministic_convergence_non_increasing_trace():
    cfg_hash = STAGE9D_CONFIG.compute_hash()
    rec, png = _make_fit_record("opt_family", "regular", cfg_hash, "chromium")
    glyph = _rect_glyph()
    optimizer = FitOnlyGlyphOptimizer()

    runs = []
    for _ in range(2):
        optimized, trace = optimizer.optimize(
            glyphs={65: glyph},
            fit_records=[rec],
            raster_provider=lambda r, _png=png: _png,
            units_per_em=1000,
        )
        runs.append((optimized, trace))

    opt_a, trace_a = runs[0]
    opt_b, trace_b = runs[1]

    assert trace_a.converged is True
    assert trace_a.stop_reason == "ALL_CONVERGED"
    # Determinism: identical traces and identical optimized geometry.
    assert trace_a.compute_trace_hash() == trace_b.compute_trace_hash()
    assert trace_a.total_iterations == trace_b.total_iterations
    sa = opt_a[65].contours[0].segments[0].p0
    sb = opt_b[65].contours[0].segments[0].p0
    assert (sa.x, sa.y) == (sb.x, sb.y)

    record = trace_a.records[0]
    # Non-increasing accepted objective trace with finite values.
    trace_vals = record.accepted_objective_trace
    assert len(trace_vals) >= 1
    assert all(v == v for v in trace_vals)  # finite
    assert all(trace_vals[i + 1] <= trace_vals[i] for i in range(len(trace_vals) - 1))
    assert record.final_objective <= record.initial_objective
    assert record.stop_reason == "CONVERGED"
    # Fit-only binding: input fingerprint covers exactly the fit evidence.
    assert trace_a.input_fingerprint == optimizer.compute_input_fingerprint([rec])


def test_optimizer_budget_exhaustion_fails_closed():
    cfg_hash = STAGE9D_CONFIG.compute_hash()
    # Raster shifted relative to the contour: improvements exist, so a tiny
    # budget cannot reach the step-exhaustion convergence criterion.
    rec, png = _make_fit_record(
        "opt_family", "regular", cfg_hash, "chromium",
        bbox=(110.0, 50.0, 610.0, 700.0),
    )
    glyph = _rect_glyph()
    optimizer = FitOnlyGlyphOptimizer(policy=OptimizerPolicy(max_iterations=2))
    with pytest.raises(OptimizerNonConvergenceError):
        optimizer.optimize(
            glyphs={65: glyph},
            fit_records=[rec],
            raster_provider=lambda r, _png=png: _png,
            units_per_em=1000,
        )


def test_optimizer_rejects_mixed_identity_and_missing_evidence():
    cfg_hash = STAGE9D_CONFIG.compute_hash()
    rec_a, png_a = _make_fit_record("family_a", "regular", cfg_hash, "chromium")
    rec_b, png_b = _make_fit_record("family_b", "regular", cfg_hash, "chromium")
    optimizer = FitOnlyGlyphOptimizer()

    with pytest.raises(ValueError, match="OPTIMIZER_MIXED_EVIDENCE_IDENTITY"):
        optimizer.optimize(
            glyphs={65: _rect_glyph()},
            fit_records=[rec_a, rec_b],
            raster_provider=lambda r: png_a if r.reference_id == "family_a" else png_b,
        )

    with pytest.raises(ValueError, match="OPTIMIZER_MISSING_FIT_EVIDENCE_CP_66"):
        optimizer.optimize(
            glyphs={65: _rect_glyph(), 66: _rect_glyph(code_point=66)},
            fit_records=[rec_a],
            raster_provider=lambda r, _png=png_a: _png,
        )


def test_optimizer_raster_tamper_fails_closed():
    cfg_hash = STAGE9D_CONFIG.compute_hash()
    rec, png = _make_fit_record("opt_family", "regular", cfg_hash, "chromium")
    optimizer = FitOnlyGlyphOptimizer()
    tampered = bytearray(png)
    tampered[-5] ^= 0xFF
    with pytest.raises(ValueError, match="OPTIMIZER_RASTER_SHA_MISMATCH"):
        optimizer.optimize(
            glyphs={65: _rect_glyph()},
            fit_records=[rec],
            raster_provider=lambda r, _b=bytes(tampered): _b,
        )


# =========================================================================
# Release gate library tests (real consumers)
# =========================================================================

@pytest.mark.asyncio
async def test_release_gate_pass_attestation_and_artifact_binding():
    with tempfile.TemporaryDirectory() as store_dir, tempfile.TemporaryDirectory() as out_dir:
        store, config, bv = await _seed_completed_store(Path(store_dir))

        result = await Stage9DReleaseGate.execute(
            store=store,
            config=config,
            reference_id="stage9d_family",
            style_id="regular",
            family_name="Stage9DFamily",
            style_name="Regular",
            browser_version=bv,
            format_type="TTF",
            output_dir=out_dir,
        )

        assert result.is_publishable is True
        assert result.status == "PASS"
        assert result.trace is not None and result.trace.converged is True
        att = result.attestation
        assert att is not None
        assert att.overall_status == "PASS"
        assert att.optimizer_converged is True

        # Exact artifact binding: attested SHA/size equals on-disk bytes.
        artifact = Path(result.candidate_file_path)
        on_disk = artifact.read_bytes()
        assert hashlib.sha256(on_disk).hexdigest() == result.candidate_artifact_sha == att.artifact_sha256
        assert len(on_disk) == result.candidate_size_bytes == att.artifact_size_bytes

        # Evidence/model/report identity bound into the attestation.
        assert att.reference_id == "stage9d_family"
        assert att.style_id == "regular"
        assert att.browser_version == bv
        assert att.config_hash == config.compute_hash()
        assert att.model_hash == result.model_hash != ""
        assert att.report_hash == result.report_hash == result.report.compute_report_hash()
        assert att.fit_set_fingerprint == result.fit_set_fingerprint != ""
        assert att.held_out_set_fingerprint == result.held_out_set_fingerprint != ""
        assert att.optimizer_trace_hash == result.trace.compute_trace_hash() != ""
        # Attestation hash is recomputable from its canonical payload.
        assert Stage9DAttestation.canonical_hash(att.to_dict()) == att.compute_hash()
        result.cleanup()


@pytest.mark.asyncio
async def test_release_gate_convergence_determinism_two_runs():
    with tempfile.TemporaryDirectory() as store_dir, tempfile.TemporaryDirectory() as out_a, \
            tempfile.TemporaryDirectory() as out_b:
        store, config, bv = await _seed_completed_store(Path(store_dir))

        kwargs = dict(
            store=store,
            config=config,
            reference_id="stage9d_family",
            style_id="regular",
            family_name="Stage9DFamily",
            style_name="Regular",
            browser_version=bv,
            format_type="TTF",
        )
        res_a = await Stage9DReleaseGate.execute(output_dir=out_a, **kwargs)
        res_b = await Stage9DReleaseGate.execute(output_dir=out_b, **kwargs)

        assert res_a.is_publishable and res_b.is_publishable
        # Deterministic convergence proof across two runs.
        assert res_a.trace.input_fingerprint == res_b.trace.input_fingerprint
        assert res_a.trace.compute_trace_hash() == res_b.trace.compute_trace_hash()
        assert [r.accepted_objective_trace for r in res_a.trace.records] == \
               [r.accepted_objective_trace for r in res_b.trace.records]
        assert [r.stop_reason for r in res_a.trace.records] == [r.stop_reason for r in res_b.trace.records]
        assert res_a.model_hash == res_b.model_hash
        assert res_a.candidate_artifact_sha == res_b.candidate_artifact_sha
        assert res_a.report_hash == res_b.report_hash
        res_a.cleanup()
        res_b.cleanup()


@pytest.mark.asyncio
async def test_release_gate_snapshot_failure_fail_closed():
    with tempfile.TemporaryDirectory() as store_dir:
        store = ObservationStore(Path(store_dir))
        result = await Stage9DReleaseGate.execute(
            store=store,
            config=STAGE9D_CONFIG,
            reference_id="missing_family",
            style_id="regular",
            family_name="MissingFamily",
            style_name="Regular",
            browser_version="chromium",
            format_type="TTF",
        )
        assert result.is_publishable is False
        assert result.status == "FAIL"
        assert result.failure_reasons == ("PIPELINE_ERROR: SNAPSHOT_LOAD_FAILED",)


@pytest.mark.asyncio
async def test_release_gate_fast30_fit_evidence_tamper_fail_closed():
    """FAST_30 regime (ADR-0001): corrupted sealed FIT raster evidence
    fails closed at the snapshot integrity boundary; there is no
    fallback/escalation path."""
    with tempfile.TemporaryDirectory() as store_dir:
        store, config, bv = await _seed_completed_store(Path(store_dir))
        # Tamper one fit raster on disk (collector names held-out rasters
        # *heldout*; everything else is fit evidence).
        fit_rasters = [
            f for f in sorted(Path(store_dir).rglob("*.png"))
            if "heldout" not in f.name
        ]
        assert fit_rasters, "expected fit raster files in seeded store"
        target = fit_rasters[0]
        data = bytearray(target.read_bytes())
        data[len(data) // 2] ^= 0xFF
        target.write_bytes(bytes(data))

        result = await Stage9DReleaseGate.execute(
            store=store,
            config=config,
            reference_id="stage9d_family",
            style_id="regular",
            family_name="Stage9DFamily",
            style_name="Regular",
            browser_version=bv,
            format_type="TTF",
        )
        assert result.is_publishable is False
        assert result.status == "FAIL"
        assert result.reconstruction_profile == "FAST_30"
        # The sealed snapshot verifies raster integrity before any
        # formation work: tampered evidence fails closed here.
        assert result.failure_reasons == ("PIPELINE_ERROR: SNAPSHOT_LOAD_FAILED",)
        # Retired escalation record fields no longer exist (A2).
        assert not hasattr(result, "escalated_from_profile")
        assert not hasattr(result, "escalation_reason")


@pytest.mark.asyncio
async def test_release_gate_unsupported_format_rejected():
    with tempfile.TemporaryDirectory() as store_dir:
        store, config, bv = await _seed_completed_store(Path(store_dir))
        result = await Stage9DReleaseGate.execute(
            store=store,
            config=config,
            reference_id="stage9d_family",
            style_id="regular",
            family_name="Stage9DFamily",
            style_name="Regular",
            browser_version=bv,
            format_type="WOFF2",
        )
        assert result.is_publishable is False
        assert result.failure_reasons == ("PIPELINE_ERROR: UNSUPPORTED_FORMAT",)


@pytest.mark.asyncio
async def test_release_gate_artifact_drift_detected(monkeypatch):
    with tempfile.TemporaryDirectory() as store_dir, tempfile.TemporaryDirectory() as out_dir:
        store, config, bv = await _seed_completed_store(Path(store_dir))

        from fidelity import release_gate as rg

        original = rg.CandidateArtifact.from_descriptor.__func__

        def tampering_from_descriptor(cls, descriptor):
            art = original(cls, descriptor)
            # Simulate post-attestation mutation of the exact PASS-bound bytes.
            Path(art.file_path).write_bytes(Path(art.file_path).read_bytes() + b"TAMPER")
            return art

        monkeypatch.setattr(
            rg.CandidateArtifact, "from_descriptor", classmethod(tampering_from_descriptor)
        )

        result = await Stage9DReleaseGate.execute(
            store=store,
            config=config,
            reference_id="stage9d_family",
            style_id="regular",
            family_name="Stage9DFamily",
            style_name="Regular",
            browser_version=bv,
            format_type="TTF",
            output_dir=out_dir,
        )
        assert result.is_publishable is False
        assert result.failure_reasons == ("PIPELINE_ERROR: ARTIFACT_DRIFT_DETECTED",)


# =========================================================================
# Archive attestation semantics
# =========================================================================

def _archive_identity(fmt: str = "TTF") -> ArchiveIdentity:
    return ArchiveIdentity(
        source_identity="https://www.myfonts.com/collections/stage9d-demo",
        family_name="Stage9DDemo",
        style_id="regular",
        style_name="Regular",
        mode="ORIGINAL",
        format=fmt,
        observation_identity="observations-stage9d",
        config_version="config-v1",
    )


def _stage9d_attestation_bytes(fmt: str, sha: str, size: int) -> tuple[str, str]:
    attestation = Stage9DAttestation(
        schema_version=1,
        format=fmt,
        artifact_sha256=sha,
        artifact_size_bytes=size,
        reference_id="stage9d_family",
        style_id="regular",
        browser_version="chromium",
        config_hash="0" * 64,
        snapshot_fingerprint="s" * 64,
        fit_set_fingerprint="f" * 64,
        held_out_set_fingerprint="h" * 64,
        model_hash="m" * 64,
        policy_hash="p" * 64,
        report_id="rep_stage9d",
        report_hash="r" * 64,
        consumer_bundle_hash="c" * 64,
        optimizer_trace_hash="t" * 64,
        optimizer_converged=True,
        overall_status="PASS",
    )
    return (
        json.dumps(attestation.to_dict(), sort_keys=True, separators=(",", ":")),
        attestation.compute_hash(),
    )


def test_archive_attested_hit_legacy_and_tampered_miss(tmp_path: Path):
    from compute.models import GeneratedFontFile

    archive = FinalFontArchive(tmp_path / "root", tmp_path / "index.sqlite3")
    content = b"stage9d-validated-font-bytes"
    src = tmp_path / "src.ttf"
    src.write_bytes(content)
    font_file = GeneratedFontFile(
        style_id="regular",
        style_name="Regular",
        format="TTF",
        filename="Stage9DDemo-Regular.ttf",
        file_path=src,
        size_bytes=len(content),
        sha256_hex=hashlib.sha256(content).hexdigest(),
    )
    identity = _archive_identity("TTF")
    att_json, att_hash = _stage9d_attestation_bytes("TTF", font_file.sha256_hex, font_file.size_bytes)

    # Attested entry is a verified hit.
    archive.put_attested(identity, font_file, att_json, att_hash)
    assert archive.get_attested(identity) is not None

    # Tampered attestation payload breaks hash recomputation -> miss.
    with archive._connect() as conn:
        conn.execute(
            "UPDATE final_fonts SET attestation_json = REPLACE(attestation_json, 'PASS', 'FAKE') WHERE cache_key = ?",
            (identity.cache_key,),
        )
        conn.commit()
    assert archive.get_attested(identity) is None

    # Restore attestation; tamper artifact bytes -> byte verification miss.
    with archive._connect() as conn:
        conn.execute(
            "UPDATE final_fonts SET attestation_json = ?, attestation_hash = ? WHERE cache_key = ?",
            (att_json, att_hash, identity.cache_key),
        )
        conn.commit()
    assert archive.get_attested(identity) is not None
    entry = archive.get(identity)
    entry.file_path.write_bytes(b"corrupted-artifact")
    assert archive.get_attested(identity) is None

    # Legacy unattested entry (plain put) is never an attested hit.
    legacy_identity = _archive_identity("OTF")
    src2 = tmp_path / "src.otf"
    src2.write_bytes(b"legacy-font-bytes")
    legacy_file = GeneratedFontFile(
        style_id="regular",
        style_name="Regular",
        format="OTF",
        filename="Stage9DDemo-Regular.otf",
        file_path=src2,
        size_bytes=len(b"legacy-font-bytes"),
        sha256_hex=hashlib.sha256(b"legacy-font-bytes").hexdigest(),
    )
    archive.put(legacy_identity, legacy_file)
    assert archive.get(legacy_identity) is not None
    assert archive.get_attested(legacy_identity) is None


@pytest.mark.asyncio
async def test_release_gate_wrong_tuple_rejected():
    """Wrong browser/config tuple cannot load the snapshot (fail-closed)."""
    with tempfile.TemporaryDirectory() as store_dir:
        store, config, bv = await _seed_completed_store(Path(store_dir))
        result = await Stage9DReleaseGate.execute(
            store=store,
            config=config,
            reference_id="stage9d_family",
            style_id="regular",
            family_name="Stage9DFamily",
            style_name="Regular",
            browser_version="chromium_foreign_browser",
            format_type="TTF",
        )
        assert result.is_publishable is False
        assert result.failure_reasons == ("PIPELINE_ERROR: SNAPSHOT_LOAD_FAILED",)


def test_optimizer_never_consumes_held_out_evidence():
    """Structural held-out leakage guard: optimization reads fit keys only."""
    from fidelity.pipeline import partition_snapshot
    from tests.test_stage9c_pipeline import _build_valid_snapshot

    snapshot = _build_valid_snapshot()
    partition = partition_snapshot(snapshot)
    fit_keys = {r.cache_key for r in partition.fit_records}
    held_out_keys = {r.cache_key for r in partition.held_out_records}
    assert not (fit_keys & held_out_keys)

    accessed: list[str] = []

    def spy_provider(rec):
        accessed.append(rec.cache_key)
        return snapshot.get_raster_bytes(rec.cache_key)

    from reconstruction.solver import MaxReconstructionSolver

    solver = MaxReconstructionSolver()
    fit_by_cp: dict[int, list] = {}
    for r in partition.fit_records:
        fit_by_cp.setdefault(r.code_point, []).append(r)
    glyphs = {
        cp: solver.reconstruct_glyph([(r, snapshot.get_raster_bytes(r.cache_key)) for r in recs])
        for cp, recs in fit_by_cp.items()
    }

    optimizer = FitOnlyGlyphOptimizer()
    _, trace = optimizer.optimize(
        glyphs=glyphs,
        fit_records=partition.fit_records,
        raster_provider=spy_provider,
        units_per_em=1000,
    )
    assert set(accessed) <= fit_keys
    assert not (set(accessed) & held_out_keys)
    assert trace.input_fingerprint == optimizer.compute_input_fingerprint(partition.fit_records)


# =========================================================================
# Runner archive-miss integration (Stage 9D gate, attestation, repeat hit)
# =========================================================================

import httpx  # noqa: E402

from compute.models import ClaimStyle  # noqa: E402
from compute.source import SourceAcquirer  # noqa: E402
from queue_client import QueueMessage  # noqa: E402

STAGE9D_SOURCE_URL = "https://www.myfonts.com/collections/stage9d-family"


class CountingAcquirer(SourceAcquirer):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.acquire_calls = 0

    async def acquire_source(self, *args, **kwargs):
        self.acquire_calls += 1
        return await super().acquire_source(*args, **kwargs)


class FailingBuilder:
    def __init__(self):
        self.build_calls = 0

    def build_font(self, *args, **kwargs):
        self.build_calls += 1
        raise AssertionError("Stage 9D gated path must never use the legacy builder")


def _stage9d_worker_handler(state: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        if "claim" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "job_id": "job_stage9d",
                    "order_id": "order_stage9d",
                    "lease_token": "12345678-1234-1234-1234-123456789abc",
                    "lease_expires_at": 9999999999999,
                    "source_url": STAGE9D_SOURCE_URL,
                    "family_name": "Stage9DFamily",
                    "styles": [{"id": "regular", "display_name": "Regular"}],
                    "formats": state["formats"],
                },
            )
        if "heartbeat" in request.url.path:
            return httpx.Response(200, json={"success": True, "lease_expires_at": 9999999999999})
        if "artifact" in request.url.path:
            state["uploads"].append(request.headers["X-Artifact-SHA256"])
            state["zip_contents"].append(request.content)
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "artifact_key": f"artifacts/order_stage9d/job_stage9d/{request.headers['X-Artifact-SHA256']}.zip",
                    "sha256": request.headers["X-Artifact-SHA256"],
                    "size": len(request.content),
                },
            )
        if "complete" in request.url.path:
            state["completes"].append("job_stage9d")
            return httpx.Response(
                200,
                json={"success": True, "status": "COMPLETED", "queue_action": "ack", "completed_at": 1},
            )
        if "fail" in request.url.path:
            state["fails"].append(json.loads(request.content).get("reason_code"))
            return httpx.Response(200, json={"success": True, "queue_action": "ack"})
        return httpx.Response(404)

    return handler


def _queue_handler(state: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        if "acks" in data:
            state["acks"].extend([a["lease_id"] for a in data["acks"]])
        if "retries" in data:
            state["retries"].extend([r["lease_id"] for r in data["retries"]])
        return httpx.Response(200, json={"success": True})

    return handler


async def _make_stage9d_runner(tmp_path: Path, test_settings: Settings, formats: list[str]):
    store_dir = tmp_path / "obs_store"
    store_dir.mkdir()
    store, config, bv = await _seed_completed_store(store_dir)
    # Manifest binds the browser version used by archive context resolution.
    (store_dir / "manifest.json").write_text(
        json.dumps({"chromium_version": bv, "config_hash": config.compute_hash()}),
        encoding="utf-8",
    )

    settings = test_settings.model_copy(update={"FONT_ARCHIVE_ROOT": tmp_path / "archive_root"})
    archive = FinalFontArchive(settings.FONT_ARCHIVE_ROOT, settings.SCRATCH_DIR / "archive_idx.sqlite3")

    state = {
        "formats": formats,
        "uploads": [],
        "zip_contents": [],
        "completes": [],
        "fails": [],
        "acks": [],
        "retries": [],
    }

    async def no_http(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected HTTP request: {request.url}")

    q_http = httpx.AsyncClient(transport=httpx.MockTransport(_queue_handler(state)))
    w_http = httpx.AsyncClient(transport=httpx.MockTransport(_stage9d_worker_handler(state)))
    s_http = httpx.AsyncClient(transport=httpx.MockTransport(no_http))

    acquirer = CountingAcquirer(
        client=s_http,
        observation_store_dir=store_dir,
        observation_config=config,
    )
    builder = FailingBuilder()
    runner = A23Runner(
        settings,
        CloudflareQueueClient(settings, client=q_http),
        WorkerJobClient(settings, client=w_http),
        source_acquirer=acquirer,
        font_builder=builder,
        archive=archive,
    )
    msg = QueueMessage(
        id="m_stage9d",
        lease_id="lease_stage9d",
        body_raw='{"job_id":"job_stage9d"}',
        attempts=1,
        job_id="job_stage9d",
    )
    return runner, state, acquirer, builder, archive, msg, config, bv


@pytest.mark.asyncio
async def test_runner_stage9d_sha_continuity_and_attested_repeat_hit(test_settings: Settings, tmp_path: Path):
    """Positive archive-miss integration: exact SHA continuity across
    Stage 9 result -> generated file -> archive entry -> package manifest for
    TTF and OTF, then a verified attested repeat hit invoking zero
    acquisition/optimization/build/consumer work."""
    runner, state, acquirer, builder, archive, msg, config, bv = await _make_stage9d_runner(
        tmp_path, test_settings, ["TTF", "OTF"]
    )

    res = await runner.process_message(msg)
    assert res.action == RunnerAction.ACKED
    assert len(state["uploads"]) == 1
    assert len(state["completes"]) == 1
    assert builder.build_calls == 0  # legacy builder never used on gated path
    # Completed observation collection is reused without source acquisition.
    assert acquirer.acquire_calls == 0
    manifest = res.manifest
    assert manifest is not None
    assert len(manifest.files) == 2

    # SHA continuity: uploaded package zip entry bytes == manifest == archive
    # entry == attested artifact. (Job scratch dir is cleaned after ACK, so the
    # package bytes are verified from the exact uploaded artifact payload.)
    import io as _io

    uploaded_zip = state["zip_contents"][0]
    first_zip_sha = state["uploads"][0]
    assert hashlib.sha256(uploaded_zip).hexdigest() == first_zip_sha
    context = acquirer.get_archive_context(STAGE9D_SOURCE_URL, [ClaimStyle("regular", "Regular")])
    assert context is not None
    with zipfile.ZipFile(_io.BytesIO(uploaded_zip)) as zf:
        for f in manifest.files:
            zip_bytes = zf.read(f.filename)
            assert hashlib.sha256(zip_bytes).hexdigest() == f.sha256_hex
            assert len(zip_bytes) == f.size_bytes
            identity = ArchiveIdentity(
                source_identity=context.source_identity,
                family_name="Stage9DFamily",
                style_id="regular",
                style_name="Regular",
                mode="ORIGINAL",
                format=f.format,
                observation_identity=context.observation_identity_for("regular"),
                config_version=context.config_version,
            )
            entry = archive.get_attested(identity)
            assert entry is not None, f"attested archive hit missing for {f.format}"
            assert entry.sha256_hex == f.sha256_hex
            assert entry.size_bytes == f.size_bytes
            assert entry.file_path.read_bytes() == zip_bytes

    # Verified repeat: identical job hits the attested archive and bypasses
    # acquisition, optimization, candidate build, and consumer evaluation.
    res2 = await runner.process_message(msg)
    assert res2.action == RunnerAction.ACKED
    assert acquirer.acquire_calls == 0  # no acquisition on repeat
    assert builder.build_calls == 0
    assert len(state["uploads"]) == 2
    assert len(state["completes"]) == 2
    assert state["uploads"][1] == first_zip_sha  # identical package bytes
    assert res2.manifest is not None
    assert {f.sha256_hex for f in res2.manifest.files} == {f.sha256_hex for f in manifest.files}
    await runner.close()


@pytest.mark.asyncio
async def test_runner_stage9d_corrupted_evidence_stops_before_side_effects(test_settings: Settings, tmp_path: Path):
    """Negative repro: corrupted held-out raster evidence makes the gate fail
    closed; the job stops before archive/package/upload/complete."""
    runner, state, acquirer, builder, archive, msg, config, bv = await _make_stage9d_runner(
        tmp_path, test_settings, ["TTF"]
    )

    # Tamper one held-out raster file on disk (collector names them *heldout*).
    held_out = sorted(acquirer.store_dir.rglob("*heldout*.png"))
    assert held_out, "expected held-out raster files in seeded store"
    target = held_out[0]
    data = bytearray(target.read_bytes())
    data[len(data) // 2] ^= 0xFF
    target.write_bytes(bytes(data))

    res = await runner.process_message(msg)
    assert res.action == RunnerAction.FAILED_TERMINAL
    # FAST_30 regime (ADR-0001): quality failure returns FAST30_FAILED and
    # stops; no fallback/escalation exists.
    assert res.reason == "FAST30_FAILED"
    assert state["fails"] == ["FAST30_FAILED"]
    assert state["uploads"] == []
    assert state["completes"] == []
    # Terminal gate failure acknowledges the message out of the queue (existing
    # terminal semantics) but performs no upload/complete/archive side effect.
    assert state["acks"] == ["lease_stage9d"]
    # No archive side effect was produced.
    context = acquirer.get_archive_context(STAGE9D_SOURCE_URL, [ClaimStyle("regular", "Regular")])
    assert context is not None
    identity = ArchiveIdentity(
        source_identity=context.source_identity,
        family_name="Stage9DFamily",
        style_id="regular",
        style_name="Regular",
        mode="ORIGINAL",
        format="TTF",
        observation_identity=context.observation_identity_for("regular"),
        config_version=context.config_version,
    )
    assert archive.get_attested(identity) is None
    assert archive.get(identity) is None
    await runner.close()


@pytest.mark.asyncio
async def test_runner_stage9d_partial_multi_artifact_failure_archives_nothing(
    test_settings: Settings, tmp_path: Path, monkeypatch
):
    """Negative repro: one failing gate in a multi-artifact job (TTF PASS,
    OTF fail-closed) produces no archive/package/upload/completion side effect."""
    runner, state, acquirer, builder, archive, msg, config, bv = await _make_stage9d_runner(
        tmp_path, test_settings, ["TTF", "OTF"]
    )

    import runner as runner_module
    from fidelity.release_gate import ReleaseGateResult

    real_execute_sync = runner_module.Stage9DReleaseGate.execute_sync

    def partial_failure_execute_sync(*args, **kwargs):
        result = real_execute_sync(*args, **kwargs)
        if kwargs.get("format_type", "").upper() == "OTF":
            return ReleaseGateResult(
                is_publishable=False,
                status="FAIL",
                family_name=result.family_name,
                style_name=result.style_name,
                reference_id=result.reference_id,
                style_id=result.style_id,
                format="OTF",
                model_hash=result.model_hash,
                candidate_file_path="",
                candidate_size_bytes=0,
                candidate_artifact_sha="",
                failure_reasons=("PIPELINE_ERROR: FIDELITY_GATE_FAILED",),
            )
        return result

    monkeypatch.setattr(
        runner_module.Stage9DReleaseGate, "execute_sync", staticmethod(partial_failure_execute_sync)
    )

    res = await runner.process_message(msg)
    assert res.action == RunnerAction.FAILED_TERMINAL
    # FAST_30 regime (ADR-0001): quality failure returns FAST30_FAILED and
    # stops; no fallback/escalation exists.
    assert res.reason == "FAST30_FAILED"
    assert state["uploads"] == []
    assert state["completes"] == []
    assert state["acks"] == ["lease_stage9d"]
    # Partial PASS archived nothing, including the format whose gate passed.
    context = acquirer.get_archive_context(STAGE9D_SOURCE_URL, [ClaimStyle("regular", "Regular")])
    assert context is not None
    for fmt in ("TTF", "OTF"):
        identity = ArchiveIdentity(
            source_identity=context.source_identity,
            family_name="Stage9DFamily",
            style_id="regular",
            style_name="Regular",
            mode="ORIGINAL",
            format=fmt,
            observation_identity=context.observation_identity_for("regular"),
            config_version=context.config_version,
        )
        assert archive.get_attested(identity) is None
        assert archive.get(identity) is None
    await runner.close()
