"""Issue #71 adversarial pack: tiered reuse, binary-first, fallback order,
cache isolation, Vietnamese AI boundaries, held-out leakage, partial orders.

Named/mapped repros: FINAL_REPEAT, LOWER_REUSE, BINARY_FIRST, BINARY_TAMPER,
FALLBACK_ORDER, SPRITE_TERMINATION, CACHE_CROSS_TUPLE, ORIGINAL_NO_AI,
VI_PRESERVE, VI_AI_FORGED, HELDOUT_LEAK, PARTIAL_ORDER.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from acquisition.models import BinaryAcquisitionPolicy, SpriteRasterPage
from acquisition.pipeline import AcquisitionPipeline
from acquisition.providers import (
    MonotypeRasterProvider,
    PersistentSessionBinaryProvider,
    extract_binary_from_dump_dom,
)
from acquisition.verifier import verify_acquired_binary
from compute.archive import (
    PROVENANCE_BINARY_DUMP_DOM,
    PROVENANCE_STAGE9D_RASTER,
    PROVENANCE_VIETNAMESE_AI,
    ArchiveIdentity,
    FinalFontArchive,
)
from compute.model_cache import CanonicalFontModelCache, FontModelCacheIdentity
from compute.models import ClaimStyle, GeneratedFontFile
from compute.source import SourceAcquirer
from compute.vietnamese import (
    AICandidateSpec,
    VietnameseAIIntegrityError,
    VietnameseExtensionService,
    VIETNAMESE_REQUIRED_CODEPOINTS,
    missing_vietnamese_codepoints,
)
from config import Settings
from fidelity.pipeline import ObservationStoreSnapshot, partition_snapshot
from fidelity.release_gate import Stage9DReleaseGate
from measurement.browser_session import ChromiumSession
from measurement.collector import ObservationCollector
from measurement.models import ObservationConfig
from queue_client import CloudflareQueueClient, QueueMessage
from reconstruction.candidate_builder import MaxCandidateFontBuilder
from reconstruction.font_model import CalibratedGlyph, CanonicalFontModel, GlobalFontMetrics
from reconstruction.solver import MaxReconstructionSolver
from runner import A23Runner, RunnerAction
from tests.test_stage9c_pipeline import _generate_png_bytes, _make_dummy_metrics
from worker_client import WorkerJobClient

ISSUE71_CONFIG = ObservationConfig(
    resolutions=(128, 256),
    base_subpixel_phases=((0.0, 0.0),),
    expanded_subpixel_phases=((0.0, 0.0),),
    held_out_subpixel_phases=((0.25, 0.25),),
    metric_sizes_px=(32.0, 64.0),
    feature_probes=(("kern", "AV"),),
)


def _rect_contours():
    from reconstruction.models import Contour, LineSegment, Point2D

    pts = [Point2D(50, 50), Point2D(550, 50), Point2D(550, 700), Point2D(50, 700)]
    segs = [LineSegment(pts[i], pts[(i + 1) % 4]) for i in range(4)]
    return [Contour(segments=segs, is_hole=False)]


def _rect_glyph(code_point: int = 65, advance: float = 650.0):
    from reconstruction.models import ReconstructedGlyph

    return ReconstructedGlyph(
        code_point=code_point,
        character=chr(code_point),
        advance_width_upem=advance,
        lsb_upem=50.0,
        rsb_upem=advance - 550.0,
        ascent_upem=700.0,
        descent_upem=-200.0,
        contours=_rect_contours(),
        bounding_box_upem=(50.0, 50.0, 550.0, 700.0),
    )


def _build_real_ttf(family_name: str = "Binary Fam", style_name: str = "Regular") -> bytes:
    """Build a real, valid TTF binary for binary-first tests (no ground truth)."""
    with io.BytesIO() as _:
        pass
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        builder = MaxCandidateFontBuilder(family_name=family_name, style_name=style_name, units_per_em=1000)
        build = builder.build_candidate_family(
            glyphs={65: _rect_glyph(65, 650.0), 66: _rect_glyph(66, 600.0)},
            output_dir=Path(td),
            typography=None,
        )
        return Path(build.ttf.file_path).read_bytes()


async def _seed_store(store_dir: Path, reference_id: str, style_id: str, code_points=(65, 66), pairs=((65, 66), (66, 65))):
    from measurement.store import ObservationStore

    store = ObservationStore(store_dir)
    config = ISSUE71_CONFIG
    session = MagicMock(spec=ChromiumSession)
    session.browser_version = "chromium_issue71_test"
    session.start = AsyncMock()

    def fake_measure_glyph(font_family, code_point, font_size_px, upem):
        adv = 650.0 if code_point == 65 else 600.0
        return _make_dummy_metrics(code_point=code_point, resolution=int(font_size_px), advance_width_upem=adv, font_size_px=float(font_size_px))

    def fake_capture_raster(font_family, code_point, resolution_px, subpixel_offset=(0.0, 0.0)):
        adv = 650.0 if code_point == 65 else 600.0
        bbox = (50, 50, 550, 700) if code_point == 65 else (40, 50, 560, 700)
        return _generate_png_bytes(resolution_px, bbox, adv, subpixel_offset[0], subpixel_offset[1])

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
        reference_id=reference_id, style_id=style_id, font_family="Seed", code_points=list(code_points)
    )
    await collector.collect_pair_observations(
        reference_id=reference_id, style_id=style_id, font_family="Seed", pairs=list(pairs)
    )
    await collector.collect_feature_observations(reference_id, style_id, "Seed")
    collector.finalize_source_collection(reference_id, style_id, expected_pairs=list(pairs))
    return store, config, session.browser_version


class CountingAcquirer(SourceAcquirer):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.acquire_calls = 0

    async def acquire_source(self, *args, **kwargs):
        self.acquire_calls += 1
        return await super().acquire_source(*args, **kwargs)


def _runner_state(formats):
    return {
        "formats": formats,
        "uploads": [],
        "zip_contents": [],
        "completes": [],
        "fails": [],
        "acks": [],
        "retries": [],
    }


def _wire_handlers(state):
    def queue_handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        if "acks" in data:
            state["acks"].extend([a["lease_id"] for a in data["acks"]])
        if "retries" in data:
            state["retries"].extend([r["lease_id"] for r in data["retries"]])
        return httpx.Response(200, json={"success": True})

    def worker_handler(request: httpx.Request) -> httpx.Response:
        if "claim" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "job_id": "job_i71",
                    "order_id": "order_i71",
                    "lease_token": "12345678-1234-1234-1234-123456789abc",
                    "lease_expires_at": 9999999999999,
                    "source_url": state["source_url"],
                    "family_name": state.get("family_name"),
                    "styles": state["styles"],
                    "formats": state["formats"],
                    "mode": state.get("mode", "ORIGINAL"),
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
                    "artifact_key": f"artifacts/order_i71/job_i71/{request.headers['X-Artifact-SHA256']}.zip",
                    "sha256": request.headers["X-Artifact-SHA256"],
                    "size": len(request.content),
                },
            )
        if "complete" in request.url.path:
            state["completes"].append("job_i71")
            return httpx.Response(
                200, json={"success": True, "status": "COMPLETED", "queue_action": "ack", "completed_at": 1}
            )
        if "fail" in request.url.path:
            state["fails"].append(json.loads(request.content).get("reason_code"))
            return httpx.Response(200, json={"success": True, "queue_action": "ack"})
        return httpx.Response(404)

    return queue_handler, worker_handler


async def _make_runner(tmp_path: Path, test_settings: Settings, state, store_dir: Path, **runner_kwargs):
    settings = test_settings.model_copy(update={"FONT_ARCHIVE_ROOT": tmp_path / "archive_root"})
    archive = FinalFontArchive(settings.FONT_ARCHIVE_ROOT, settings.SCRATCH_DIR / "archive_idx.sqlite3")
    acquirer = CountingAcquirer(
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
        observation_store_dir=store_dir,
        observation_config=ISSUE71_CONFIG,
    )
    queue_handler, worker_handler = _wire_handlers(state)
    q_http = httpx.AsyncClient(transport=httpx.MockTransport(queue_handler))
    w_http = httpx.AsyncClient(transport=httpx.MockTransport(worker_handler))

    class _Builder:
        def __init__(self):
            self.build_calls = 0

        def build_font(self, *a, **k):
            self.build_calls += 1
            raise AssertionError("legacy builder must not run on gated path")

    builder = _Builder()
    runner = A23Runner(
        settings,
        CloudflareQueueClient(settings, client=q_http),
        WorkerJobClient(settings, client=w_http),
        source_acquirer=acquirer,
        font_builder=builder,
        archive=archive,
        **runner_kwargs,
    )
    msg = QueueMessage(
        id="m_i71", lease_id="lease_i71", body_raw='{"job_id":"job_i71"}', attempts=1, job_id="job_i71"
    )
    return runner, state, acquirer, builder, archive, msg


# =========================================================================
# Acquisition pipeline unit repros
# =========================================================================

class _DumpDom:
    def __init__(self, dump: str):
        self.dump = dump
        self.calls = 0

    async def dump_dom(self, url: str) -> str:
        self.calls += 1
        return self.dump


class _SessionMaterial:
    def __init__(self, material):
        self._material = material

    async def material(self):
        return self._material


class _SessionTransport:
    def __init__(self, payload: bytes | None):
        self.payload = payload
        self.calls = 0

    async def discover(self, envelope, source_url, session_material):
        self.calls += 1
        assert session_material  # opaque material consumed, never inspected here
        return self.payload


class _RasterClient:
    def __init__(self, pages: list, final_at: int | None = None):
        self.pages = pages
        self.final_at = final_at
        self.calls = 0

    async def fetch_sprite_page(self, request, cursor):
        idx = self.calls
        self.calls += 1
        if idx >= len(self.pages):
            return None
        page = self.pages[idx]
        final = self.final_at is not None and idx >= self.final_at
        return SpriteRasterPage(
            page_index=idx,
            glyph_count=page,
            raster_bytes=b"x",
            next_cursor=f"c{idx + 1}",
            final=final,
        )


def test_FALLBACK_ORDER_deterministic_trace_and_no_premature_fallback():
    async def run():
        ttf = _build_real_ttf()
        session_transport = _SessionTransport(ttf)
        pipeline = AcquisitionPipeline(
            dump_dom_transport=_DumpDom("<html>no font here</html>"),
            session_provider=PersistentSessionBinaryProvider(_SessionMaterial({"opaque": True}), session_transport),
            raster_provider=MonotypeRasterProvider(_RasterClient([])),
        )
        outcome = await pipeline.acquire("https://www.myfonts.com/collections/binary-fam", "Binary Fam", "Regular")
        assert outcome.kind == "binary"
        # dump-dom produced no binary, session stage won; no premature fallback.
        assert outcome.trace.stage_order() == ("dump_dom_binary", "authorized_session_binary")
        assert outcome.binary.provenance == "authorized_session_binary"

        # Full exhaustion trace when every stage is insufficient.
        pipeline2 = AcquisitionPipeline(
            dump_dom_transport=_DumpDom(""),
            session_provider=PersistentSessionBinaryProvider(_SessionMaterial(None), _SessionTransport(None)),
            raster_provider=MonotypeRasterProvider(_RasterClient([])),
        )
        outcome2 = await pipeline2.acquire(
            "https://www.myfonts.com/collections/x", "X", "Regular",
            raster_request={"family": "X", "style": "Regular", "md5": "ab" * 16},
        )
        assert outcome2.kind == "insufficient"
        assert outcome2.terminal_reason_code == "ACQUISITION_INSUFFICIENT"
        assert outcome2.trace.stage_order() == (
            "dump_dom_binary",
            "authorized_session_binary",
            "monotype_authorized_raster",
        )

    import asyncio

    asyncio.run(run())


def test_BINARY_TAMPER_integrity_failure_is_terminal_not_fallback():
    async def run():
        # A font container with mismatched family identity: integrity failure
        # is terminal and never falls through to later stages. (Non-font
        # payloads such as HTML are magic-filtered earlier and cannot reach
        # verification at all; see SESSION_HTML_NOT_FONT.)
        wrong_family_ttf = _build_real_ttf("Other Family", "Regular")
        session_transport = _SessionTransport(_build_real_ttf())
        pipeline = AcquisitionPipeline(
            dump_dom_transport=_DumpDom(
                '<a href="data:font/ttf;base64,' + base64.b64encode(wrong_family_ttf).decode() + '">x</a>'
            ),
            session_provider=PersistentSessionBinaryProvider(_SessionMaterial({"opaque": True}), session_transport),
            raster_provider=MonotypeRasterProvider(_RasterClient([4])),
        )
        outcome = await pipeline.acquire("https://www.myfonts.com/collections/binary-fam", "Binary Fam", "Regular")
        assert outcome.kind == "insufficient"
        assert outcome.terminal_reason_code.startswith("ACQUISITION_BINARY_INTEGRITY_FAILED")
        # Integrity failure never falls through to later stages.
        assert session_transport.calls == 0
        assert outcome.trace.stage_order() == ("dump_dom_binary",)

    import asyncio

    asyncio.run(run())


def test_SPRITE_TERMINATION_bounded_page_budget():
    async def run():
        target = {"family": "Sprite Fam", "style": "Regular", "md5": "ab" * 16}
        policy = BinaryAcquisitionPolicy(max_sprite_pages=3)
        client = _RasterClient(pages=[2, 2, 2, 2, 2], final_at=None)  # never final
        provider = MonotypeRasterProvider(client)
        pages = await provider.fetch_sprite_pages(target, policy)
        assert len(pages) == 3  # bounded termination, no infinite crawl
        assert client.calls == 3

        client2 = _RasterClient(pages=[2, 2, 2], final_at=1)
        pages2 = await MonotypeRasterProvider(client2).fetch_sprite_pages(target, BinaryAcquisitionPolicy(max_sprite_pages=8))
        assert len(pages2) == 2  # stops at the final marker

        # Missing target fails closed without any request.
        client3 = _RasterClient(pages=[2], final_at=0)
        pages3 = await MonotypeRasterProvider(client3).fetch_sprite_pages({}, policy)
        assert pages3 == () and client3.calls == 0

    import asyncio

    asyncio.run(run())


def test_binary_verifier_identity_and_flavor():
    ttf = _build_real_ttf("Binary Fam", "Regular")
    ok = verify_acquired_binary(ttf, "Binary Fam", "Regular", 10 * 1024 * 1024)
    assert ok.status == "VALID" and ok.format == "TTF"

    mismatch = verify_acquired_binary(ttf, "Other Family", "Regular", 10 * 1024 * 1024)
    assert mismatch.status == "INTEGRITY_FAILED" and mismatch.reason_code == "BINARY_FAMILY_MISMATCH"

    absent = verify_acquired_binary(None, "Binary Fam", "Regular", 10 * 1024 * 1024)
    assert absent.status == "ABSENT"

    corrupt = verify_acquired_binary(b"\x00\x01\x00\x00deadbeef", "Binary Fam", "Regular", 10 * 1024 * 1024)
    assert corrupt.status == "INTEGRITY_FAILED"


def test_CACHE_CROSS_TUPLE_model_cache_identity_matrix(tmp_path: Path):
    cache = CanonicalFontModelCache(tmp_path / "mc", tmp_path / "mc_index.sqlite3")

    metrics = GlobalFontMetrics(
        units_per_em=1000, ascent_upem=700.0, descent_upem=-200.0, line_gap_upem=0.0,
        cap_height_upem=700.0, x_height_upem=500.0, max_advance_width_upem=650.0,
        avg_char_width_upem=620.0, underline_position_upem=-100.0, underline_thickness_upem=50.0,
    )
    glyph = CalibratedGlyph(
        code_point=65, character="A", advance_width_upem=650.0, lsb_upem=50.0, rsb_upem=50.0,
        ascent_upem=700.0, descent_upem=-200.0, bounding_box_upem=(50.0, 50.0, 550.0, 700.0),
        contours=_rect_contours(), confidence=1.0, observation_fingerprints=("a" * 64,),
    )
    model = CanonicalFontModel(
        schema_version="1.0.0", family_name="Demo", style_name="Regular",
        reference_id="demo", style_id="regular", metrics=metrics, glyphs={65: glyph},
        config_hash="c" * 64, browser_version="chromium", fit_observations_count=1,
        calibration_fingerprint="d" * 64, kerning_pairs={},
    )

    base = FontModelCacheIdentity(
        reference_fingerprint="r" * 64, family_name="Demo", style_id="regular", mode="ORIGINAL",
        coverage_fingerprint="cov", provenance=PROVENANCE_STAGE9D_RASTER,
    )
    cache.put(base, model, metadata={"snapshot_fingerprint": "s" * 64})
    assert cache.get(base) is not None

    # Cross-tuple negative matrix: every dimension drift is a fail-closed miss.
    drifts = [
        dict(reference_fingerprint="x" * 64),
        dict(family_name="Other"),
        dict(style_id="bold"),
        dict(mode="VIETNAMESE"),
        dict(coverage_fingerprint="other"),
        dict(provenance=PROVENANCE_VIETNAMESE_AI),
    ]
    for drift in drifts:
        identity = FontModelCacheIdentity(
            reference_fingerprint=drift.get("reference_fingerprint", "r" * 64),
            family_name=drift.get("family_name", "Demo"),
            style_id=drift.get("style_id", "regular"),
            mode=drift.get("mode", "ORIGINAL"),
            coverage_fingerprint=drift.get("coverage_fingerprint", "cov"),
            provenance=drift.get("provenance", PROVENANCE_STAGE9D_RASTER),
        )
        assert cache.get(identity) is None, f"cross-tuple leak: {drift}"

    # Tampered payload is a fail-closed miss.
    entry_row = None
    with cache._connect() as conn:
        conn.execute(
            "UPDATE canonical_font_models SET payload_sha256 = ? WHERE cache_key = ?",
            ("f" * 64, base.cache_key),
        )
        conn.commit()
    assert cache.get(base) is None


# =========================================================================
# Runner-level repros
# =========================================================================

class _SpyAIProvider:
    model_id = "spy-model"
    model_version = "v0"

    def __init__(self, candidates_factory=None):
        self.calls = 0
        self._factory = candidates_factory

    def prompt_hash(self) -> str:
        return "p" * 64

    async def generate_candidates(self, request):
        self.calls += 1
        if self._factory is not None:
            return self._factory(request)
        return []


@pytest.mark.asyncio
async def test_FINAL_REPEAT_exact_l1_repeat_zero_work(test_settings: Settings, tmp_path: Path, monkeypatch):
    store_dir = tmp_path / "obs"
    store_dir.mkdir()
    await _seed_store(store_dir, "final_repeat_fam", "regular")
    state = _runner_state(["TTF"])
    state["source_url"] = "https://www.myfonts.com/collections/final-repeat-fam"
    state["styles"] = [{"id": "regular", "display_name": "Regular"}]
    model_cache = CanonicalFontModelCache(tmp_path / "mc", tmp_path / "mc.sqlite3")
    runner, state, acquirer, builder, archive, msg = await _make_runner(
        tmp_path, test_settings, state, store_dir, model_cache=model_cache
    )

    res = await runner.process_message(msg)
    assert res.action == RunnerAction.ACKED
    first_zip_sha = state["uploads"][0]
    assert acquirer.acquire_calls == 0

    # Second run: exact L1 repeat. Gate/consumer/reconstruction must not run.
    def _gate_must_not_run(*a, **k):
        raise AssertionError("gate must not run on exact L1 repeat")

    async def _consumers_must_not_run(*a, **k):
        raise AssertionError("consumers must not run on exact L1 repeat")

    monkeypatch.setattr(Stage9DReleaseGate, "execute_sync", _gate_must_not_run)
    monkeypatch.setattr(Stage9DReleaseGate, "execute", _consumers_must_not_run)
    from fidelity.producers import ProductionConsumerEvidenceProducer

    monkeypatch.setattr(ProductionConsumerEvidenceProducer, "produce_bundle", classmethod(lambda cls, **k: (_ for _ in ()).throw(AssertionError("consumers must not run"))))

    res2 = await runner.process_message(msg)
    assert res2.action == RunnerAction.ACKED
    assert state["uploads"][1] == first_zip_sha  # byte-identical package
    assert acquirer.acquire_calls == 0
    assert builder.build_calls == 0


@pytest.mark.asyncio
async def test_LOWER_REUSE_l2_model_cache_skips_acquisition_reconstruction_optimization(
    test_settings: Settings, tmp_path: Path, monkeypatch
):
    store_dir = tmp_path / "obs"
    store_dir.mkdir()
    await _seed_store(store_dir, "lower_reuse_fam", "regular")
    state = _runner_state(["TTF"])
    state["source_url"] = "https://www.myfonts.com/collections/lower-reuse-fam"
    state["styles"] = [{"id": "regular", "display_name": "Regular"}]
    model_cache = CanonicalFontModelCache(tmp_path / "mc", tmp_path / "mc.sqlite3")
    runner, state, acquirer, builder, archive, msg = await _make_runner(
        tmp_path, test_settings, state, store_dir, model_cache=model_cache
    )

    res = await runner.process_message(msg)
    assert res.action == RunnerAction.ACKED
    first_shas = {f.sha256_hex for f in res.manifest.files}

    # Remove L1 entries so the repeat must resolve via L2.
    with archive._connect() as conn:
        conn.execute("DELETE FROM final_fonts")
        conn.commit()

    # Reconstruction and optimization are causally replaced by the L2 model:
    # any invocation fails the repro.
    def _solver_must_not_run(self, *a, **k):
        raise AssertionError("reconstruction must not run on L2 reuse")

    def _optimizer_must_not_run(self, *a, **k):
        raise AssertionError("optimization must not run on L2 reuse")

    monkeypatch.setattr(MaxReconstructionSolver, "reconstruct_glyph", _solver_must_not_run)
    from fidelity.optimizer import FitOnlyGlyphOptimizer

    monkeypatch.setattr(FitOnlyGlyphOptimizer, "optimize", _optimizer_must_not_run)

    res2 = await runner.process_message(msg)
    assert res2.action == RunnerAction.ACKED
    assert acquirer.acquire_calls == 0  # no browser/source acquisition
    events = [e["event"] for e in runner.last_reuse_trace["events"] if e["key"].startswith("L2_")]
    assert "HIT" in events
    assert {f.sha256_hex for f in res2.manifest.files} == first_shas  # identical artifact bytes


@pytest.mark.asyncio
async def test_BINARY_FIRST_valid_binary_zero_geometry_reconstruction(
    test_settings: Settings, tmp_path: Path, monkeypatch
):
    store_dir = tmp_path / "obs"
    store_dir.mkdir()
    ttf = _build_real_ttf("Binary Fam", "Regular")
    dump = '<style src="data:font/ttf;base64,' + base64.b64encode(ttf).decode() + '"></style>'
    pipeline = AcquisitionPipeline(
        dump_dom_transport=_DumpDom(dump),
        session_provider=None,
        raster_provider=None,
    )
    state = _runner_state(["TTF", "OTF"])
    state["source_url"] = "https://www.myfonts.com/collections/binary-fam"
    state["styles"] = [{"id": "regular", "display_name": "Regular"}]
    runner, state, acquirer, builder, archive, msg = await _make_runner(
        tmp_path, test_settings, state, store_dir, acquisition_pipeline=pipeline,
    )

    # Raster gate / reconstruction must never run on the binary path.
    def _gate_must_not_run(*a, **k):
        raise AssertionError("raster gate must not run on binary-first path")

    def _solver_must_not_run(self, *a, **k):
        raise AssertionError("geometry reconstruction must not run on binary-first path")

    monkeypatch.setattr(Stage9DReleaseGate, "execute_sync", _gate_must_not_run)
    monkeypatch.setattr(MaxReconstructionSolver, "reconstruct_glyph", _solver_must_not_run)

    res = await runner.process_message(msg)
    assert res.action == RunnerAction.ACKED
    assert len(res.manifest.files) == 2
    formats = {f.format for f in res.manifest.files}
    assert formats == {"TTF", "OTF"}
    assert acquirer.acquire_calls == 0
    # TTF artifact is the exact acquired binary (byte continuity).
    ttf_files = [f for f in res.manifest.files if f.format == "TTF"]
    assert ttf_files[0].sha256_hex == hashlib.sha256(ttf).hexdigest()


@pytest.mark.asyncio
async def test_ORIGINAL_NO_AI_zero_ai_work_in_original_trace(test_settings: Settings, tmp_path: Path):
    store_dir = tmp_path / "obs"
    store_dir.mkdir()
    await _seed_store(store_dir, "original_no_ai_fam", "regular")
    state = _runner_state(["TTF"])
    state["source_url"] = "https://www.myfonts.com/collections/original-no-ai-fam"
    state["styles"] = [{"id": "regular", "display_name": "Regular"}]
    state["mode"] = "ORIGINAL"
    spy = _SpyAIProvider()
    runner, state, acquirer, builder, archive, msg = await _make_runner(
        tmp_path, test_settings, state, store_dir,
        vietnamese_ai_provider=spy,
        model_cache=CanonicalFontModelCache(tmp_path / "mc", tmp_path / "mc.sqlite3"),
    )
    res = await runner.process_message(msg)
    assert res.action == RunnerAction.ACKED
    assert spy.calls == 0  # ORIGINAL call trace contains zero AI work


@pytest.mark.asyncio
async def test_VI_PRESERVE_complete_vietnamese_coverage_no_ai():
    metrics = GlobalFontMetrics(
        units_per_em=1000, ascent_upem=700.0, descent_upem=-200.0, line_gap_upem=0.0,
        cap_height_upem=700.0, x_height_upem=500.0, max_advance_width_upem=650.0,
        avg_char_width_upem=620.0, underline_position_upem=-100.0, underline_thickness_upem=50.0,
    )
    glyphs = {}
    for cp in list(VIETNAMESE_REQUIRED_CODEPOINTS) + [65]:
        glyphs[cp] = CalibratedGlyph(
            code_point=cp, character=chr(cp), advance_width_upem=600.0, lsb_upem=50.0,
            rsb_upem=50.0, ascent_upem=700.0, descent_upem=-200.0,
            bounding_box_upem=(50.0, 50.0, 550.0, 700.0), contours=_rect_contours(),
            confidence=1.0, observation_fingerprints=("a" * 64,),
        )
    model = CanonicalFontModel(
        schema_version="1.0.0", family_name="Demo", style_name="Regular",
        reference_id="demo", style_id="regular", metrics=metrics, glyphs=glyphs,
        config_hash="c" * 64, browser_version="chromium", fit_observations_count=1,
        calibration_fingerprint="d" * 64, kerning_pairs={},
    )
    assert missing_vietnamese_codepoints(model) == ()

    spy = _SpyAIProvider()
    service = VietnameseExtensionService(ai_provider=spy, config_hash="c" * 64, source_hash="s" * 64)
    extended, binding = await service.extend(model)
    assert extended.compute_canonical_hash() == model.compute_canonical_hash()  # preserved
    assert binding.extended_codepoints == ()
    assert binding.preserved_codepoints == tuple(sorted(VIETNAMESE_REQUIRED_CODEPOINTS))
    assert spy.calls == 0  # zero AI work when coverage is complete


@pytest.mark.asyncio
async def test_VI_AI_FORGED_fail_closed_no_publish(test_settings: Settings, tmp_path: Path):
    # Service-level: forged (non-finite) and incomplete AI output fail closed.
    metrics = GlobalFontMetrics(
        units_per_em=1000, ascent_upem=700.0, descent_upem=-200.0, line_gap_upem=0.0,
        cap_height_upem=700.0, x_height_upem=500.0, max_advance_width_upem=650.0,
        avg_char_width_upem=620.0, underline_position_upem=-100.0, underline_thickness_upem=50.0,
    )
    model = CanonicalFontModel(
        schema_version="1.0.0", family_name="Demo", style_name="Regular",
        reference_id="demo", style_id="regular", metrics=metrics,
        glyphs={65: CalibratedGlyph(
            code_point=65, character="A", advance_width_upem=600.0, lsb_upem=50.0,
            rsb_upem=50.0, ascent_upem=700.0, descent_upem=-200.0,
            bounding_box_upem=(50.0, 50.0, 550.0, 700.0), contours=_rect_contours(),
            confidence=1.0, observation_fingerprints=("a" * 64,),
        )},
        config_hash="c" * 64, browser_version="chromium", fit_observations_count=1,
        calibration_fingerprint="d" * 64, kerning_pairs={},
    )
    missing = missing_vietnamese_codepoints(model)
    assert missing

    def forged_factory(request):
        specs = []
        for cp in request["missing_codepoints"]:
            specs.append(AICandidateSpec(
                code_point=cp,
                contours=(((0.0, 0.0), (100.0, 0.0), (100.0, 100.0)),),
                advance_width_upem=float("nan"),  # forged/non-finite
                lsb_upem=10.0, rsb_upem=10.0, ascent_upem=700.0, descent_upem=-200.0,
                anchors=(("top", 50.0, 700.0),) if cp in (0x0300, 0x0301, 0x0303, 0x0309, 0x0323) else (),
            ))
        return specs

    service = VietnameseExtensionService(
        ai_provider=_SpyAIProvider(forged_factory), config_hash="c" * 64, source_hash="s" * 64
    )
    with pytest.raises(VietnameseAIIntegrityError):
        await service.extend(model)

    def incomplete_factory(request):
        return []  # incomplete coverage

    service2 = VietnameseExtensionService(
        ai_provider=_SpyAIProvider(incomplete_factory), config_hash="c" * 64, source_hash="s" * 64
    )
    with pytest.raises(VietnameseAIIntegrityError):
        await service2.extend(model)

    # Runner-level: VIETNAMESE job with forged provider -> terminal sanitized
    # failure, zero upload/completion/archive side effects.
    store_dir = tmp_path / "obs"
    store_dir.mkdir()
    await _seed_store(store_dir, "vi_forged_fam", "regular")
    state = _runner_state(["TTF"])
    state["source_url"] = "https://www.myfonts.com/collections/vi-forged-fam"
    state["styles"] = [{"id": "regular", "display_name": "Regular"}]
    state["mode"] = "VIETNAMESE"
    runner, state, acquirer, builder, archive, msg = await _make_runner(
        tmp_path, test_settings, state, store_dir,
        vietnamese_ai_provider=_SpyAIProvider(forged_factory),
        model_cache=CanonicalFontModelCache(tmp_path / "mc", tmp_path / "mc.sqlite3"),
    )
    res = await runner.process_message(msg)
    assert res.action == RunnerAction.FAILED_TERMINAL
    assert res.reason == "VIETNAMESE_EXTENSION_FAILED"
    assert state["uploads"] == []
    assert state["completes"] == []
    with archive._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM final_fonts").fetchone()[0] == 0


def test_HELDOUT_LEAK_optimizer_reads_fit_keys_only():
    from fidelity.optimizer import FitOnlyGlyphOptimizer
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
        glyphs=glyphs, fit_records=partition.fit_records, raster_provider=spy_provider, units_per_em=1000
    )
    assert set(accessed) <= fit_keys
    assert not (set(accessed) & held_out_keys)
    assert trace.input_fingerprint == optimizer.compute_input_fingerprint(partition.fit_records)


@pytest.mark.asyncio
async def test_PARTIAL_ORDER_one_failing_item_archives_nothing(test_settings: Settings, tmp_path: Path):
    store_dir = tmp_path / "obs"
    store_dir.mkdir()
    await _seed_store(store_dir, "partial_fam", "regular")
    await _seed_store(store_dir, "partial_fam", "bold")
    # Corrupt one held-out raster of the bold style so its gate fails closed.
    held_out = sorted(p for p in store_dir.rglob("*heldout*.png") if "bold" in str(p))
    assert held_out
    data = bytearray(held_out[0].read_bytes())
    data[len(data) // 2] ^= 0xFF
    held_out[0].write_bytes(bytes(data))

    state = _runner_state(["TTF", "OTF"])
    state["source_url"] = "https://www.myfonts.com/collections/partial-fam"
    state["styles"] = [
        {"id": "regular", "display_name": "Regular"},
        {"id": "bold", "display_name": "Bold"},
    ]
    runner, state, acquirer, builder, archive, msg = await _make_runner(
        tmp_path, test_settings, state, store_dir,
        model_cache=CanonicalFontModelCache(tmp_path / "mc", tmp_path / "mc.sqlite3"),
    )
    res = await runner.process_message(msg)
    assert res.action == RunnerAction.FAILED_TERMINAL
    assert res.reason == "STAGE9D_GATE_FAILED"
    assert state["uploads"] == []
    assert state["completes"] == []
    # Partial PASS archived nothing, including the style whose gate passed.
    with archive._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM final_fonts").fetchone()[0] == 0
