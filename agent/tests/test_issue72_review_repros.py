"""Issue #71 review 5018836759 KNOWN_REPRO pack.

PROD_COMPOSITION, RASTER_HANDOFF, L3_REPEAT, CHROMIUM_FORGE, OPENROUTER_ROUTE.
"""
from __future__ import annotations

import base64
import hashlib
import inspect
import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from acquisition.adapters import (
    AuthorizedSessionHttpTransport,
    AuthorizedSessionMaterialStore,
    HeadlessDumpDomTransport,
    HttpBinaryFetcher,
    MonotypeRasterHttpClient,
)
from acquisition.models import BinaryAcquisitionPolicy, SpriteRasterPage
from acquisition.pipeline import AcquisitionPipeline
from acquisition.providers import MonotypeRasterProvider, PersistentSessionBinaryProvider
from compute.binary_cache import AuthorizedBinaryCache
from compute.binary_gate import BinaryConsumerValidator
from compute.model_cache import CanonicalFontModelCache
from compute.openrouter_client import (
    MODEL_ARBITER,
    MODEL_DIFFICULT,
    MODEL_PRIMARY,
    OpenRouterAIClient,
)
from compute.source import SourceAcquirer
from composition import build_production_components
from config import Settings
from measurement.models import ObservationConfig
from queue_client import CloudflareQueueClient, QueueMessage
from runner import A23Runner, RunnerAction
from tests.test_issue71_adversarial import (
    ISSUE71_CONFIG,
    CountingAcquirer,
    _build_real_ttf,
    _generate_png_bytes,
    _runner_state,
    _wire_handlers,
)
from worker_client import WorkerJobClient


# =========================================================================
# PROD_COMPOSITION
# =========================================================================

def test_PROD_COMPOSITION_real_factory_concrete_dependencies(tmp_path: Path, test_settings: Settings, monkeypatch):
    settings = test_settings.model_copy(update={"ACQUISITION_ENABLED": True})
    components = build_production_components(settings, tmp_path / "scratch")

    pipeline = components["acquisition_pipeline"]
    assert isinstance(pipeline, AcquisitionPipeline)
    assert isinstance(pipeline.dump_dom_transport, HeadlessDumpDomTransport)
    assert isinstance(pipeline.binary_fetch.__self__, HttpBinaryFetcher)
    assert isinstance(components["model_cache"], CanonicalFontModelCache)
    assert isinstance(components["binary_cache"], AuthorizedBinaryCache)
    # Session/raster stages absent without runtime secrets: concrete types only
    # when constructible; nothing test-only is produced.
    assert components["vietnamese_ai_provider"] is None

    # Enabled Vietnamese AI without runtime key fails closed.
    vi_settings = test_settings.model_copy(update={"VIETNAMESE_AI_ENABLED": True})
    with pytest.raises(RuntimeError, match="COMPOSITION_READINESS_FAILED_OPENROUTER"):
        build_production_components(vi_settings, tmp_path / "scratch2")

    # Enabled acquisition without constructible Chromium fails closed.
    def _no_chromium():
        raise RuntimeError("CHROMIUM_EXECUTABLE_NOT_FOUND")

    import acquisition.adapters as adapters_mod

    monkeypatch.setattr(adapters_mod, "find_chromium_executable", _no_chromium)
    with pytest.raises(RuntimeError, match="COMPOSITION_READINESS_FAILED"):
        build_production_components(settings, tmp_path / "scratch3")


# =========================================================================
# RASTER_HANDOFF
# =========================================================================

# Raster-only schedule the approved CDN render can observably satisfy:
# size axis only, fixed phase (0.0, 0.0); fit/held-out split across sizes.
RASTER_ONLY_CONFIG = ObservationConfig(
    resolutions=(120, 240),
    base_subpixel_phases=((0.0, 0.0),),
    expanded_subpixel_phases=((0.0, 0.0),),
    held_out_subpixel_phases=(),
    metric_sizes_px=(32.0, 64.0),
    feature_probes=(("kern", "AV"),),
)

SEED_ADVS = {65: 650.0, 66: 600.0}
SEED_BBOXES = {65: (50, 50, 550, 700), 66: (40, 50, 560, 700)}


def _seed_boxes(acs_pt: int) -> dict[int, tuple[int, int, int, int]]:
    return {65: (0, 0, acs_pt, acs_pt), 66: (acs_pt, 0, acs_pt, acs_pt)}


def _expected_seed_slice(cp: int, acs_pt: int) -> bytes:
    """The exact CDN glyph cell: the proven fixture raster at the render size."""
    return _generate_png_bytes(acs_pt, SEED_BBOXES[cp], SEED_ADVS[cp], 0.0, 0.0)


def _build_seed_sprite(acs_pt: int) -> bytes:
    """Real binary PNG sprite tiling one observable glyph cell per code point.

    Built in the fixture's own 8-bit grayscale mode so that slicing the
    sprite at the observable boxes re-encodes byte-identical cells.
    """
    import io

    from PIL import Image

    img = Image.new("L", (2 * acs_pt, acs_pt), 255)
    for cp, (x, y, _w, _h) in _seed_boxes(acs_pt).items():
        cell = Image.open(io.BytesIO(_expected_seed_slice(cp, acs_pt)))
        img.paste(cell.convert("L"), (x, y))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


RASTER_HANDOFF_MD5 = "ab12cd34ef56ab78cd90ef12ab34cd56"


def _raster_pages_for_seed(
    browser_version: str,
    acs_pt: int = 128,
    md5: str = RASTER_HANDOFF_MD5,
    final: bool = True,
    next_cursor: str = "",
) -> list[SpriteRasterPage]:
    """Captured-shape raster page: real binary PNG sprite + observable boxes.

    Raster-only provider evidence bound to the exact MD5/page/request
    parameters. Never metrics, pairs, or features.
    """
    sprite = _build_seed_sprite(acs_pt)
    payload = {
        "browser_version": browser_version,
        "glyphs": [
            {
                "code_point": cp,
                "glyph_index": i,
                "sprite_box": {"x": x, "y": y, "width": bw, "height": bh},
            }
            for i, (cp, (x, y, bw, bh)) in enumerate(_seed_boxes(acs_pt).items())
        ],
        "pairs": [],
        "features": [],
        "sprite_sha256": hashlib.sha256(sprite).hexdigest(),
        "observed_headers": {"content_type": "application/json; charset=utf-8"},
        "md5": md5,
        "acs_pt": acs_pt,
        "provenance": "monotype_render_105",
        "request_params": {
            "rbe": "gmap", "acs_pt": str(acs_pt), "acs_w": "1500",
            "acs_l": "1", "acs_ar": "0", "acs_p": "1", "acs_gpp": "100",
            "provider": "monotype_render_105", "md5": md5,
        },
    }
    return [SpriteRasterPage(page_index=1, glyph_count=len(SEED_ADVS), raster_bytes=sprite, next_cursor=next_cursor, final=final, payload=payload)]


def _browser_supplement_for_seed(browser_version: str, config=ISSUE71_CONFIG):
    """Deterministic approved-path-shaped browser supplemental evidence.

    Carries exactly what the production ChromiumSession canvas path
    produces: per-glyph DirectMetrics, bounded-fit pairs within coverage,
    and feature probes. Never raster pixels.
    """
    from acquisition.raster_ingest import BrowserSupplementalEvidence
    from measurement.models import DirectMetrics

    advs = {65: 650.0, 66: 600.0}
    bboxes = {65: (50, 50, 550, 700), 66: (40, 50, 560, 700)}
    scale = float(config.font_size_px) / float(config.upem)
    metrics: dict[int, DirectMetrics] = {}
    for cp, adv in advs.items():
        bbox = bboxes[cp]
        metrics[cp] = DirectMetrics(
            code_point=cp,
            character=chr(cp),
            font_size_px=float(config.font_size_px),
            raw_advance_width=adv * scale,
            raw_actual_left=float(bbox[0]) * scale,
            raw_actual_right=float(bbox[2]) * scale,
            raw_actual_ascent=float(bbox[3]) * scale,
            raw_actual_descent=200.0 * scale,
            raw_font_ascent=float(bbox[3]) * scale,
            raw_font_descent=200.0 * scale,
            advance_width_upem=adv,
            lsb_upem=float(bbox[0]),
            rsb_upem=adv - float(bbox[2]),
            ascent_upem=float(bbox[3]),
            descent_upem=-200.0,
            bbox_width_upem=float(bbox[2] - bbox[0]),
            bbox_height_upem=float(bbox[3] - bbox[1]),
        )
    pairs = [
        {"left_cp": 65, "right_cp": 66, "left_advance_upem": 650.0,
         "right_advance_upem": 600.0, "pair_advance_upem": 1230.0},
        {"left_cp": 66, "right_cp": 65, "left_advance_upem": 600.0,
         "right_advance_upem": 650.0, "pair_advance_upem": 1240.0},
    ]
    features = [
        {
            "feature_tag": tag,
            "sample_text": text,
            "enabled_advance_upem": 1200.0,
            "disabled_advance_upem": 1200.0,
            "enabled_raster_signature": "a",
            "disabled_raster_signature": "a",
        }
        for tag, text in config.feature_probes
    ]
    return BrowserSupplementalEvidence(
        browser_version=browser_version, metrics=metrics, pairs=pairs, features=features,
    )


class _RasterOnlyClient:
    def __init__(self, browser_version: str = "monotype_render_105"):
        self.browser_version = browser_version
        self.calls = 0
        self.requests: list[dict] = []

    async def fetch_sprite_page(self, request, cursor):
        self.calls += 1
        self.requests.append(dict(request))
        if cursor:
            return None
        # One full page per requested render size (acs_pt).
        return _raster_pages_for_seed(self.browser_version, int(request.get("acs_pt", 128)))[0]


RASTER_HANDOFF_MD5 = "ab12cd34ef56ab78cd90ef12ab34cd56"


class _FailingDumpDom:
    def __init__(self):
        self.calls = 0

    async def dump_dom(self, url):
        self.calls += 1
        raise RuntimeError("DUMP_DOM_UNAVAILABLE")


class _MetadataDumpDom:
    """Dump carrying typed family/style/MD5 discovery identity, no binaries."""

    def __init__(self, family: str, md5: str):
        self.family = family
        self.md5 = md5
        self.calls = 0

    async def dump_dom(self, url):
        self.calls += 1
        return (
            '<script type="application/ld+json">{"name": "' + self.family
            + '", "variantName": "Regular", "font_md5": "' + self.md5 + '"}</script>'
        )


class _StubChromiumSession:
    """Deterministic stand-in for the approved ChromiumSession canvas path.

    Raster capture is sabotaged: any call proves the forbidden recapture.
    """

    capture_calls = 0
    observed: list[tuple] = []

    def __init__(self, executable_path=None, timeout_seconds=10.0, port=0):
        self.browser_version = "chromium_stub_v1"
        self.timeout_seconds = timeout_seconds

    async def start(self):
        pass

    async def observe_source_font(self, source_url, style_name, family_name=None):
        _StubChromiumSession.observed.append((source_url, style_name, family_name))
        return "Stub Fam"

    async def measure_glyph_direct(self, font, code_point, font_size_px=200.0, upem=1000):
        adv = 650.0 if code_point == 65 else 600.0
        bbox = (50, 50, 550, 700) if code_point == 65 else (40, 50, 560, 700)
        from measurement.models import DirectMetrics

        scale = float(font_size_px) / float(upem)
        return DirectMetrics(
            code_point=code_point,
            character=chr(code_point),
            font_size_px=float(font_size_px),
            raw_advance_width=adv * scale,
            raw_actual_left=float(bbox[0]) * scale,
            raw_actual_right=float(bbox[2]) * scale,
            raw_actual_ascent=float(bbox[3]) * scale,
            raw_actual_descent=200.0 * scale,
            raw_font_ascent=float(bbox[3]) * scale,
            raw_font_descent=200.0 * scale,
            advance_width_upem=adv,
            lsb_upem=float(bbox[0]),
            rsb_upem=adv - float(bbox[2]),
            ascent_upem=float(bbox[3]),
            descent_upem=-200.0,
            bbox_width_upem=float(bbox[2] - bbox[0]),
            bbox_height_upem=float(bbox[3] - bbox[1]),
        )

    async def measure_text_advance(self, font, text, font_size_px=200.0, upem=1000):
        # Zero-kerning ground truth: pair advance equals the sum of the
        # per-glyph advances (A=650, B=600).
        advs = {"A": 650.0, "B": 600.0}
        return sum(advs.get(ch, 600.0) for ch in text)

    async def probe_opentype_feature(self, font, feature_tag, sample_text, font_size_px=200.0, upem=1000):
        return {
            "enabled_advance_upem": 1200.0,
            "disabled_advance_upem": 1200.0,
            "enabled_raster_signature": "a",
            "disabled_raster_signature": "a",
        }

    async def capture_lossless_raster(self, *args, **kwargs):
        _StubChromiumSession.capture_calls += 1
        raise AssertionError("browser raster capture is forbidden on the Monotype path")


@pytest.mark.asyncio
async def test_RASTER_HANDOFF_cdn_pixels_immutable_no_browser_recapture(
    test_settings: Settings, tmp_path: Path, monkeypatch
):
    """Sabotage + positive repro: on the Monotype fallback the browser never
    captures rasters; the bounds-checked CDN sprite slices reach immutable
    observations under the sealed size-axis capability, and Stage 9D
    publishes only after the held-out sizes and four consumers PASS.
    """
    import measurement.browser_session as browser_session_mod

    from acquisition.capability import PROVIDER_MONOTYPE_RENDER, ProviderRasterCapability

    _StubChromiumSession.capture_calls = 0
    _StubChromiumSession.observed = []
    monkeypatch.setattr(browser_session_mod, "ChromiumSession", _StubChromiumSession)

    async def _noop_close(session):
        return None

    monkeypatch.setattr(browser_session_mod, "close_browser_session", _noop_close)

    store_dir = tmp_path / "obs"
    store_dir.mkdir()

    class _SabotagedAcquirer(CountingAcquirer):
        async def acquire_source(self, *args, **kwargs):
            self.acquire_calls += 1
            raise AssertionError("legacy acquirer must not run after raster handoff")

    settings = test_settings.model_copy(update={"FONT_ARCHIVE_ROOT": tmp_path / "archive_root"})
    from compute.archive import FinalFontArchive

    archive = FinalFontArchive(settings.FONT_ARCHIVE_ROOT, settings.SCRATCH_DIR / "archive_idx.sqlite3")
    pipeline = AcquisitionPipeline(
        dump_dom_transport=_MetadataDumpDom("Raster Handoff Fam", RASTER_HANDOFF_MD5),
        binary_fetch=None,
        session_provider=None,
        raster_provider=MonotypeRasterProvider(_RasterOnlyClient()),
    )
    acquirer = _SabotagedAcquirer(
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
        observation_store_dir=store_dir,
        observation_config=ISSUE71_CONFIG,
    )

    state = _runner_state(["TTF"])
    state["source_url"] = "https://www.myfonts.com/collections/raster-handoff-fam"
    state["styles"] = [{"id": "regular", "display_name": "Regular"}]
    queue_handler, worker_handler = _wire_handlers(state)
    runner = A23Runner(
        settings,
        CloudflareQueueClient(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(queue_handler))),
        WorkerJobClient(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(worker_handler))),
        source_acquirer=acquirer,
        archive=archive,
        acquisition_pipeline=pipeline,
        model_cache=CanonicalFontModelCache(tmp_path / "mc", tmp_path / "mc.sqlite3"),
    )
    msg = QueueMessage(id="m_rh", lease_id="lease_rh", body_raw='{"job_id":"job_i71"}', attempts=1, job_id="job_i71")

    res = await runner.process_message(msg)

    # Sabotage proof: the real supplemental path ran against the stubbed
    # session and never captured a raster.
    assert _StubChromiumSession.capture_calls == 0
    assert _StubChromiumSession.observed == [
        ("https://www.myfonts.com/collections/raster-handoff-fam", "Regular", "Raster Handoff Fam")
    ]
    assert acquirer.acquire_calls == 0  # legacy acquirer never invoked

    # Observable CDN schedule completes Stage 9D and publishes.
    assert res.action == RunnerAction.ACKED
    assert len(state["uploads"]) == 1

    # Sealed capability partition bound into the collection identity.
    capability = ProviderRasterCapability.deterministic_size_schedule(
        PROVIDER_MONOTYPE_RENDER, ISSUE71_CONFIG.resolutions
    )
    cfg_h = ISSUE71_CONFIG.compute_hash()
    sealed_json, sealed_hash = acquirer.store.get_source_collection_capability(
        "raster_handoff_fam", "regular", browser_version="chromium_stub_v1", config_hash=cfg_h,
    )
    assert sealed_hash == capability.compute_hash()
    assert ProviderRasterCapability.from_json(sealed_json) == capability

    # CDN pixels reached immutable observations under the exact tuples:
    # fit and held-out sizes are disjoint, phase (0.0, 0.0) only.
    all_sizes = capability.all_sizes()
    cov = acquirer.store.get_coverage(
        "raster_handoff_fam", "regular", browser_version="chromium_stub_v1", config_hash=cfg_h,
    )
    assert cov == [65, 66]
    for cp in (65, 66):
        obs = acquirer.store.get_glyph_observations(
            "raster_handoff_fam", "regular", cp, browser_version="chromium_stub_v1", config_hash=cfg_h,
        )
        assert {r.resolution for r, _ in obs} == set(all_sizes)
        for rec, _ in obs:
            assert rec.subpixel_x == 0.0 and rec.subpixel_y == 0.0
            stored = (acquirer.store.base_dir / rec.raster_relative_path).read_bytes()
            assert stored == _expected_seed_slice(cp, rec.resolution)  # CDN pixels, never recaptured

    # Trace attests the capability split and CDN provenance.
    events = [e for e in runner.last_reuse_trace["events"] if e["event"] == "RASTER_HANDOFF"]
    assert events and events[0]["glyphs"] == 2
    assert events[0]["raster_provenance"] == "monotype_render_105_cdn_sprite"
    assert events[0]["capability_fit_sizes"] == list(capability.fit_sizes)
    assert events[0]["capability_held_out_sizes"] == list(capability.held_out_sizes)
    fit_sizes = set(capability.fit_sizes)
    held_sizes = set(capability.held_out_sizes)
    assert not (fit_sizes & held_sizes)


@pytest.mark.asyncio
async def test_BROWSER_SUPPLEMENT_never_captures_rasters_on_monotype_path():
    """The production supplemental path measures metrics/pairs/features only."""
    import measurement.browser_session as browser_session_mod

    from acquisition.raster_ingest import collect_browser_measurement

    _StubChromiumSession.capture_calls = 0
    _StubChromiumSession.observed = []

    async def _noop_close(session):
        return None

    original_session = browser_session_mod.ChromiumSession
    original_close = browser_session_mod.close_browser_session
    browser_session_mod.ChromiumSession = _StubChromiumSession
    browser_session_mod.close_browser_session = _noop_close
    try:
        supplement = await collect_browser_measurement(
            "https://www.myfonts.com/collections/sabotage-fam",
            "Sabotage Fam",
            "Regular",
            [65, 66],
            ISSUE71_CONFIG,
        )
    finally:
        browser_session_mod.ChromiumSession = original_session
        browser_session_mod.close_browser_session = original_close

    assert _StubChromiumSession.capture_calls == 0
    assert supplement.browser_version == "chromium_stub_v1"
    assert sorted(supplement.metrics.keys()) == [65, 66]
    assert len(supplement.pairs) >= 2
    assert {(f["feature_tag"], f["sample_text"]) for f in supplement.features} == set(
        ISSUE71_CONFIG.feature_probes
    )
    assert not hasattr(supplement, "rasters")


def test_RASTER_CAPABILITY_overlap_and_forged_matrix():
    """Forged/overlapping/insufficient capability descriptors fail closed."""
    from acquisition.capability import FIXED_PHASE, ProviderRasterCapability

    # Same-size overlap is forbidden.
    with pytest.raises(ValueError, match="CAPABILITY_FORGED"):
        ProviderRasterCapability(
            provider="p", phase=FIXED_PHASE, fit_sizes=(120, 240), held_out_sizes=(240,)
        ).validate()
    # Duplicate sizes, unsorted sizes, non-positive sizes fail closed.
    with pytest.raises(ValueError, match="CAPABILITY_FORGED"):
        ProviderRasterCapability(
            provider="p", phase=FIXED_PHASE, fit_sizes=(120, 120), held_out_sizes=(240,)
        ).validate()
    with pytest.raises(ValueError, match="CAPABILITY_FORGED"):
        ProviderRasterCapability(
            provider="p", phase=FIXED_PHASE, fit_sizes=(240, 120), held_out_sizes=(360,)
        ).validate()
    with pytest.raises(ValueError, match="CAPABILITY_FORGED"):
        ProviderRasterCapability(
            provider="p", phase=FIXED_PHASE, fit_sizes=(0,), held_out_sizes=(240,)
        ).validate()
    # Forged phase (CDN exposes size axis only at fixed phase).
    with pytest.raises(ValueError, match="CAPABILITY_FORGED"):
        ProviderRasterCapability(
            provider="p", phase=(0.25, 0.25), fit_sizes=(120,), held_out_sizes=(240,)
        ).validate()
    # Empty partition / single size: no zero-held-out bypass.
    with pytest.raises(ValueError, match="CAPABILITY_FORGED"):
        ProviderRasterCapability(
            provider="p", phase=FIXED_PHASE, fit_sizes=(120,), held_out_sizes=()
        ).validate()
    with pytest.raises(ValueError, match="CAPABILITY_INSUFFICIENT_SIZES"):
        ProviderRasterCapability.deterministic_size_schedule("p", (120,))
    with pytest.raises(ValueError, match="CAPABILITY_INSUFFICIENT_SIZES"):
        ProviderRasterCapability.deterministic_size_schedule("p", (120, 120))


def test_RASTER_ONLY_CONFIG_cdn_pixels_complete_observable_snapshot(tmp_path: Path):
    """Under the sealed size-axis capability, persisted CDN slices complete
    the immutable collection with browser supplements."""
    from acquisition.capability import PROVIDER_MONOTYPE_RENDER, ProviderRasterCapability
    from acquisition.raster_ingest import ingest_raster_pages
    from measurement.store import ObservationStore

    capability = ProviderRasterCapability.deterministic_size_schedule(
        PROVIDER_MONOTYPE_RENDER, RASTER_ONLY_CONFIG.resolutions
    )
    pages = [
        _raster_pages_for_seed("monotype_render_105", acs_pt=pt)[0]
        for pt in capability.all_sizes()
    ]
    supplement = _browser_supplement_for_seed("chromium_observable_v1", config=RASTER_ONLY_CONFIG)
    store = ObservationStore(tmp_path / "obs_observable")
    cfg_h = RASTER_ONLY_CONFIG.compute_hash()

    ingested = ingest_raster_pages(
        store, RASTER_ONLY_CONFIG, "obs_fam", "regular", supplement, pages,
        capability, source_url="https://www.myfonts.com/collections/obs-fam",
    )
    assert ingested == 2
    assert store.is_source_collection_completed(
        "obs_fam", "regular", config_hash=cfg_h, browser_version="chromium_observable_v1",
    )
    sealed_json, sealed_hash = store.get_source_collection_capability(
        "obs_fam", "regular", browser_version="chromium_observable_v1", config_hash=cfg_h,
    )
    assert sealed_hash == capability.compute_hash()
    for cp in (65, 66):
        obs = store.get_glyph_observations(
            "obs_fam", "regular", cp, browser_version="chromium_observable_v1", config_hash=cfg_h,
        )
        assert {r.resolution for r, _ in obs} == set(capability.all_sizes())
        for rec, _ in obs:
            stored = (store.base_dir / rec.raster_relative_path).read_bytes()
            assert stored == _expected_seed_slice(cp, rec.resolution)
    pairs = store.get_pair_observations(
        "obs_fam", "regular", browser_version="chromium_observable_v1", config_hash=cfg_h,
    )
    assert pairs and all(
        p["provenance"] == "chromium:chromium_observable_v1:canvas_text_metrics" for p in pairs
    )


# =========================================================================
# L3_REPEAT
# =========================================================================

class _StaticDumpDom:
    def __init__(self, dump: str):
        self.dump = dump
        self.calls = 0

    async def dump_dom(self, url):
        self.calls += 1
        return self.dump


async def _make_l3_runner(test_settings, tmp_path, state, store_dir, binary_cache, pipeline):
    settings = test_settings.model_copy(update={"FONT_ARCHIVE_ROOT": tmp_path / "archive_root"})
    from compute.archive import FinalFontArchive

    archive = FinalFontArchive(settings.FONT_ARCHIVE_ROOT, settings.SCRATCH_DIR / "archive_idx.sqlite3")
    acquirer = CountingAcquirer(
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
        observation_store_dir=store_dir,
        observation_config=ISSUE71_CONFIG,
    )
    queue_handler, worker_handler = _wire_handlers(state)
    runner = A23Runner(
        settings,
        CloudflareQueueClient(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(queue_handler))),
        WorkerJobClient(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(worker_handler))),
        source_acquirer=acquirer,
        archive=archive,
        acquisition_pipeline=pipeline,
        binary_cache=binary_cache,
    )
    return runner, acquirer


@pytest.mark.asyncio
async def test_L3_REPEAT_durable_binary_cache_zero_provider_calls_on_repeat(
    test_settings: Settings, tmp_path: Path
):
    ttf = _build_real_ttf("Cache Fam", "Regular")
    dump = '<style src="data:font/ttf;base64,' + base64.b64encode(ttf).decode() + '"></style>'
    binary_cache = AuthorizedBinaryCache(tmp_path / "bc", tmp_path / "bc.sqlite3")
    store_dir = tmp_path / "obs"
    store_dir.mkdir()

    # First job: provider supplies the binary (dump-dom win).
    state1 = _runner_state(["TTF"])
    state1["source_url"] = "https://www.myfonts.com/collections/cache-fam"
    state1["styles"] = [{"id": "regular", "display_name": "Regular"}]
    dump1 = _StaticDumpDom(dump)
    runner1, acquirer1 = await _make_l3_runner(
        test_settings, tmp_path, state1, store_dir, binary_cache,
        AcquisitionPipeline(dump_dom_transport=dump1, session_provider=None, raster_provider=None),
    )
    msg1 = QueueMessage(id="m1", lease_id="l1", body_raw='{"job_id":"job_i71"}', attempts=1, job_id="job_i71")
    res1 = await runner1.process_message(msg1)
    assert res1.action == RunnerAction.ACKED
    assert dump1.calls == 1

    # Second job (compatible new format/order): providers sabotaged; durable L3
    # cache must serve the binary with zero provider/network/reconstruction calls.
    state2 = _runner_state(["OTF"])
    state2["source_url"] = "https://www.myfonts.com/collections/cache-fam"
    state2["styles"] = [{"id": "regular", "display_name": "Regular"}]
    dump2 = _FailingDumpDom()
    runner2, acquirer2 = await _make_l3_runner(
        test_settings, tmp_path, state2, store_dir, binary_cache,
        AcquisitionPipeline(dump_dom_transport=dump2, session_provider=None, raster_provider=None),
    )
    msg2 = QueueMessage(id="m2", lease_id="l2", body_raw='{"job_id":"job_i71"}', attempts=1, job_id="job_i71")
    res2 = await runner2.process_message(msg2)
    assert res2.action == RunnerAction.ACKED
    assert dump2.calls == 0  # zero provider calls
    assert acquirer2.acquire_calls == 0  # zero acquisition
    events = [e for e in runner2.last_reuse_trace["events"] if e["event"] == "L3_CACHE_HIT"]
    assert events
    assert len(state2["uploads"]) == 1


# =========================================================================
# CHROMIUM_FORGE
# =========================================================================

def test_CHROMIUM_FORGE_no_injectable_pass_and_closed_failure(tmp_path: Path, monkeypatch):
    # Production API exposes no injectable Chromium boolean.
    init_sig = inspect.signature(BinaryConsumerValidator.__init__)
    assert all(p not in init_sig.parameters for p in ("chromium_load_check", "chromium_binary_checker"))
    validate_sig = inspect.signature(BinaryConsumerValidator.validate)
    assert all(p not in validate_sig.parameters for p in ("chromium_load_check", "checker"))

    ttf = _build_real_ttf("Forge Fam", "Regular")
    f = tmp_path / "forge.ttf"
    f.write_bytes(ttf)
    from compute.models import GeneratedFontFile

    ff = GeneratedFontFile(
        style_id="regular", style_name="Regular", format="TTF", filename="forge.ttf",
        file_path=f, size_bytes=len(ttf), sha256_hex=hashlib.sha256(ttf).hexdigest(),
    )

    # Capability absence fails closed (BLOCKED, never PASS).
    def _no_chromium():
        raise RuntimeError("CHROMIUM_EXECUTABLE_NOT_FOUND")

    import fidelity.producers as producers_mod

    monkeypatch.setattr(producers_mod, "find_chromium_executable", _no_chromium)
    report = BinaryConsumerValidator().validate(ff, provenance="p")
    assert report.overall_status == "BLOCKED"
    assert "CHROMIUM_CAPABILITY_UNAVAILABLE" in report.failure_reasons

    # Forged/drifted artifact bytes fail closed (FAIL, never PASS).
    f.write_bytes(ttf + b"TAMPER")
    report2 = BinaryConsumerValidator().validate(ff, provenance="p")
    assert report2.overall_status == "FAIL"
    assert "BINARY_ARTIFACT_DRIFT" in report2.failure_reasons


# =========================================================================
# OPENROUTER_ROUTE
# =========================================================================

def _valid_candidate_payload(missing: list[int], variant: int = 0) -> str:
    glyphs = []
    for cp in missing:
        offset = float(variant)
        glyphs.append({
            "code_point": cp,
            "contours": [[[50.0 + offset, 50.0], [550.0 + offset, 50.0], [550.0 + offset, 700.0], [50.0 + offset, 700.0]]],
            "advance_width_upem": 600.0,
            "lsb_upem": 50.0,
            "rsb_upem": 50.0,
            "ascent_upem": 700.0,
            "descent_upem": -200.0,
            "anchors": [["top", 300.0, 700.0]] if cp in (0x0300, 0x0301, 0x0303, 0x0309, 0x0323) else [],
        })
    return json.dumps({"glyphs": glyphs})


def _openrouter_transport(handler):
    return httpx.MockTransport(handler)


def _make_client(missing: list[int], primary_body, difficult_body=None, arbiter_body=None):
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append({"model": body["model"]})
        model = body["model"]
        if model == MODEL_PRIMARY:
            content = primary_body(missing) if callable(primary_body) else primary_body
        elif model == MODEL_DIFFICULT:
            content = difficult_body(missing) if callable(difficult_body) else difficult_body
        else:
            content = arbiter_body if arbiter_body is not None else '{"choice":"A"}'
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}}]}
        )

    client = OpenRouterAIClient("test-key-runtime-secret", client=httpx.AsyncClient(transport=_openrouter_transport(handler)))
    return client, calls


@pytest.mark.asyncio
async def test_OPENROUTER_ROUTE_fixed_routing_and_zero_call_paths():
    style_evidence = {
        "family_name": "Route Fam",
        "style_name": "Regular",
        "units_per_em": 1000,
        "glyph_count": 2,
        "sample_glyphs": [
            {"code_point": 65, "contours": [[[50.0, 50.0], [550.0, 50.0], [550.0, 700.0]]],
             "advance_width_upem": 600.0, "raster_sample_hashes": ["a" * 64]}
        ],
    }

    # Routine case: 12B only.
    missing_routine = [0x0110, 0x0111]
    client, calls = _make_client(missing_routine, _valid_candidate_payload)
    specs = await client.generate_candidates(
        {"missing_codepoints": missing_routine, "units_per_em": 1000, "source_hash": "s" * 64,
         "style_evidence": style_evidence}
    )
    assert len(specs) == 2
    assert [c["model"] for c in calls] == [MODEL_PRIMARY]

    # Difficult case (deterministic escalation by glyph count): 12B -> 27B.
    missing_difficult = [0x1EA0, 0x1EA1, 0x1EA2, 0x1EA3, 0x1EA4, 0x1EA5, 0x1EA6]
    client2, calls2 = _make_client(
        missing_difficult, _valid_candidate_payload, difficult_body=_valid_candidate_payload
    )
    await client2.generate_candidates(
        {"missing_codepoints": missing_difficult, "units_per_em": 1000, "source_hash": "s" * 64,
         "style_evidence": style_evidence}
    )
    assert [c["model"] for c in calls2] == [MODEL_PRIMARY, MODEL_DIFFICULT]

    # Unresolved deterministic disagreement after escalation: 12B -> 27B -> arbiter once.
    missing_disagree = [0x1EA0, 0x1EA1, 0x1EA2, 0x1EA3, 0x1EA4, 0x1EA5, 0x1EA6]
    client3, calls3 = _make_client(
        missing_disagree,
        lambda m: _valid_candidate_payload(m, variant=0),
        difficult_body=lambda m: _valid_candidate_payload(m, variant=7),
        arbiter_body='{"choice":"B"}',
    )
    specs3 = await client3.generate_candidates(
        {"missing_codepoints": missing_disagree, "units_per_em": 1000, "source_hash": "s" * 64,
         "style_evidence": style_evidence}
    )
    assert [c["model"] for c in calls3] == [MODEL_PRIMARY, MODEL_DIFFICULT, MODEL_ARBITER]
    assert len(specs3) == 7

    # Zero-call path: no missing glyphs -> no model calls.
    client4, calls4 = _make_client([], _valid_candidate_payload)
    assert await client4.generate_candidates({"missing_codepoints": []}) == []
    assert calls4 == []

    # No substitute models ever.
    allowed = {MODEL_PRIMARY, MODEL_DIFFICULT, MODEL_ARBITER}
    for call_list in (calls, calls2, calls3, calls4):
        assert all(c["model"] in allowed for c in call_list)
