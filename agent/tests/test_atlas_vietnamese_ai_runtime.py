"""R3 focused repros: Vietnamese AI runtime (secret boundary + cascade wiring).

Hermetic: fake transports only (httpx.MockTransport / duck-typed providers).
Zero live provider calls, zero real dev.vars reads except the explicit temp
files created by these tests. Proves:
- runtime secret loader reads ONLY the exact ADR-0003 key names;
- redaction guards: a secret canary can never surface in logs/reports;
- cascade selection order + capacity fallback + fail-closed provider errors;
- per-CLASS (never per-glyph) AI call batching through the service/adapter;
- the atlas pipeline stage 5b consumes the injected runtime provider.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest

from atlas.marks import is_combining_mark
from atlas.vietnamese import AtlasVietnameseAdapter, glyph_class
from compute.ai_secret_loader import (
    ALLOWED_SECRET_KEYS,
    default_dev_vars_path,
    load_ai_secrets,
    redact_text,
)
from compute.openrouter_client import OpenRouterAIClient
from compute.vietnamese import (
    AICandidateSpec,
    VIETNAMESE_REQUIRED_CODEPOINTS,
    VietnameseAIIntegrityError,
    VietnameseExtensionService,
)
from compute.woku_client import (
    ROUTE_OPENROUTER,
    ROUTE_WOKU_FALLBACK,
    ROUTE_WOKU_PRIMARY,
    WOKU_CHAT_ENDPOINT,
    WOKU_MODEL_FALLBACK,
    WOKU_MODEL_PRIMARY,
    WokuCascadeAIClient,
)
from reconstruction.font_model import CalibratedGlyph, CanonicalFontModel, GlobalFontMetrics
from reconstruction.models import Contour, LineSegment, Point2D
from tests.test_issue72_review_repros import _valid_candidate_payload

CANARY = "canary-wk-secret-9f3e6a1b7c8d"
CANARY_OR = "canary-or-secret-4d2a8e5f0b1c"


# ----------------------------------------------------------------------
# 1. Runtime secret loader: exact key names only
# ----------------------------------------------------------------------

def test_loader_reads_only_exact_key_names(tmp_path: Path):
    dev_vars = tmp_path / "dev.vars"
    dev_vars.write_text(
        "\n".join(
            [
                "# runtime secrets (never committed)",
                f"wokushop_api_key = {CANARY}",
                f"openrouter_api_key={CANARY_OR}",
                "SOME_OTHER_KEY=must-not-load",
                "WOKUSHOP_API_KEY=case-folded-names-are-not-exact",
                "openrouter_api_key",
                "",
            ]
        ),
        encoding="utf-8",
    )
    secrets = load_ai_secrets(dev_vars)
    assert set(secrets.keys()) == set(ALLOWED_SECRET_KEYS)
    assert secrets["wokushop_api_key"] == CANARY
    assert secrets["openrouter_api_key"] == CANARY_OR
    # Non-exact names never load.
    assert "SOME_OTHER_KEY" not in secrets
    assert len(secrets) == 2


def test_loader_missing_file_fails_closed(tmp_path: Path):
    assert load_ai_secrets(tmp_path / "absent.vars") == {}


def test_default_dev_vars_path_resolves_repo_root():
    """The default resolution is portable (checkout root, then ancestors for
    worktrees) - never a hardcoded absolute path."""
    path = default_dev_vars_path()
    if path is None:
        pytest.skip("no dev.vars anywhere above this checkout")
    assert path.name == "dev.vars"
    assert path.is_file()


# ----------------------------------------------------------------------
# 2. Redaction guards: the canary can never surface
# ----------------------------------------------------------------------

def test_secret_canary_never_appears_in_logs(tmp_path: Path, caplog):
    dev_vars = tmp_path / "dev.vars"
    dev_vars.write_text(f"wokushop_api_key={CANARY}\n", encoding="utf-8")
    secrets = load_ai_secrets(dev_vars)
    assert secrets["wokushop_api_key"] == CANARY

    logger = logging.getLogger("telegramfonts.agent.canary_probe")
    with caplog.at_level(logging.DEBUG):
        logger.info("attempting to leak %s", CANARY)
        logger.warning("another leak: " + CANARY)

    flat = "\n".join(record.getMessage() for record in caplog.records)
    assert CANARY not in flat
    assert "[REDACTED]" in flat
    # The loader itself never reports values.
    assert CANARY not in redact_text(f"key={CANARY}")


# ----------------------------------------------------------------------
# 3. Cascade: selection order, capacity fallback, fail-closed
# ----------------------------------------------------------------------

def _missing_from_prompt(prompt: str) -> list[int]:
    import re

    found = re.search(r"Missing code points: (\[[^\]]*\])", prompt)
    assert found, f"missing-codepoints marker absent in prompt: {prompt[:120]}"
    return json.loads(found.group(1))


def _woku_ok(model: str, missing: list[int]) -> dict:
    return {
        "model": model,
        "choices": [
            {"message": {"content": _valid_candidate_payload(missing)}}
        ],
    }


def _make_cascade(woku_responses: dict, or_handler=None):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == WOKU_CHAT_ENDPOINT
        body = json.loads(request.content)
        model = body["model"]
        calls.append(model)
        missing = _missing_from_prompt(body["messages"][0]["content"])
        spec = woku_responses.get(model, 500)
        if isinstance(spec, int):
            return httpx.Response(spec, json={"error": "fake"})
        if isinstance(spec, Exception):
            raise spec
        if callable(spec):
            spec = spec(model, missing)
        return httpx.Response(200, json=spec)

    cascade = WokuCascadeAIClient(
        "wk-runtime-secret",
        downstream=None,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return cascade, calls


def _request(missing: list[int]) -> dict:
    return {
        "missing_codepoints": list(missing),
        "units_per_em": 1000,
        "source_hash": "s" * 64,
        "config_hash": "c" * 64,
        "style_evidence": {
            "family_name": "R3 Fam",
            "style_name": "Regular",
            "units_per_em": 1000,
            "glyph_count": 1,
            "sample_glyphs": [
                {
                    "code_point": 65,
                    "contours": [[[50.0, 50.0], [550.0, 50.0], [550.0, 700.0]]],
                    "advance_width_upem": 600.0,
                    "raster_sample_hashes": ["a" * 64],
                }
            ],
        },
    }


async def test_cascade_primary_first_single_call():
    cascade, calls = _make_cascade({WOKU_MODEL_PRIMARY: _woku_ok, WOKU_MODEL_FALLBACK: _woku_ok})
    specs = await cascade.generate_candidates(_request([0x1EBF]))
    assert calls == [WOKU_MODEL_PRIMARY]  # exact PRIMARY, one bounded call
    assert cascade.last_route_trace.route == ROUTE_WOKU_PRIMARY
    assert cascade.last_route_trace.fallback_reason == ""
    assert {s.code_point for s in specs} == {0x1EBF}


async def test_cascade_capacity_fallback_to_exact_second_model():
    # Capacity/unavailability on PRIMARY (429/503) -> exact fallback model.
    for status in (429, 503):
        cascade, calls = _make_cascade(
            {WOKU_MODEL_PRIMARY: status, WOKU_MODEL_FALLBACK: _woku_ok}
        )
        specs = await cascade.generate_candidates(_request([0x1EA5]))
        assert calls == [WOKU_MODEL_PRIMARY, WOKU_MODEL_FALLBACK]
        trace = cascade.last_route_trace
        assert trace.route == ROUTE_WOKU_FALLBACK
        assert trace.fallback_reason == "woku_primary_unavailable"
        assert {s.code_point for s in specs} == {0x1EA5}


async def test_cascade_fail_closed_on_provider_error():
    cascade, calls = _make_cascade(
        {
            WOKU_MODEL_PRIMARY: httpx.ConnectError("down"),
            WOKU_MODEL_FALLBACK: httpx.ConnectError("down"),
        }
    )
    with pytest.raises(VietnameseAIIntegrityError, match="VI_AI_ALL_ROUTES_FAILED"):
        await cascade.generate_candidates(_request([0x1EA5]))
    assert calls == [WOKU_MODEL_PRIMARY, WOKU_MODEL_FALLBACK]


async def test_cascade_downstream_openrouter_route_unchanged():
    or_calls: list[str] = []

    def or_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        or_calls.append(body["model"])
        missing = _missing_from_prompt(body["messages"][0]["content"])
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": _valid_candidate_payload(missing)}}]},
        )

    downstream = OpenRouterAIClient(
        "or-runtime-secret", client=httpx.AsyncClient(transport=httpx.MockTransport(or_handler))
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "woku down"})

    cascade = WokuCascadeAIClient(
        "wk-runtime-secret",
        downstream=downstream,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    specs = await cascade.generate_candidates(_request([0x1EDF]))
    assert or_calls == ["google/gemma-3-12b-it"]  # exact downstream PRIMARY
    assert cascade.last_route_trace.route == ROUTE_OPENROUTER
    assert "woku_primary_unavailable" in cascade.last_route_trace.fallback_reason
    assert {s.code_point for s in specs} == {0x1EDF}


# ----------------------------------------------------------------------
# 4. Per-CLASS batching (never per-glyph) through service + adapter
# ----------------------------------------------------------------------

class RecordingAIProvider:
    model_id = "fake-runtime-provider"
    model_version = "r3-v1"

    def __init__(self):
        self.calls: list[list[int]] = []

    def prompt_hash(self) -> str:
        return "f" * 64

    async def generate_candidates(self, request: dict):
        missing = sorted(request["missing_codepoints"])
        self.calls.append(list(missing))
        specs = []
        for cp in missing:
            mark = is_combining_mark(cp)
            specs.append(
                AICandidateSpec(
                    code_point=cp,
                    contours=(((50.0, 500.0), (250.0, 500.0), (250.0, 700.0), (50.0, 700.0)),),
                    advance_width_upem=0.0 if mark else 600.0,
                    lsb_upem=10.0,
                    rsb_upem=10.0,
                    ascent_upem=700.0,
                    descent_upem=0.0,
                    anchors=(("mark", 150.0, 500.0),) if mark else (),
                )
            )
        return specs


def _square(x0, y0, x1, y1):
    pts = [Point2D(x0, y0), Point2D(x1, y0), Point2D(x1, y1), Point2D(x0, y1)]
    return Contour(
        segments=[LineSegment(pts[i], pts[(i + 1) % 4]) for i in range(4)],
        is_hole=False,
        area_upem=(x1 - x0) * (y1 - y0),
    )


def _glyph(cp, contours, advance=600.0, anchors=()):
    xs = [p.x for c in contours for s in c.segments for p in (s.p0, s.p1)]
    ys = [p.y for c in contours for s in c.segments for p in (s.p0, s.p1)]
    bbox = (min(xs), min(ys), max(xs), max(ys)) if xs else (0.0, 0.0, 0.0, 0.0)
    return CalibratedGlyph(
        code_point=cp,
        character=chr(cp),
        advance_width_upem=advance,
        lsb_upem=10.0,
        rsb_upem=10.0,
        ascent_upem=700.0,
        descent_upem=0.0,
        bounding_box_upem=bbox,
        contours=list(contours),
        confidence=1.0,
        observation_fingerprints=(f"{cp:064x}",),
        anchors=anchors,
    )


def _synthetic_model() -> CanonicalFontModel:
    base_a = _glyph(0x61, [_square(50.0, 0.0, 550.0, 500.0)])
    # a-grave donor: base contour + one mark contour (deterministic source).
    a_grave = _glyph(0xE0, [_square(50.0, 0.0, 550.0, 500.0), _square(200.0, 600.0, 300.0, 700.0)])
    glyphs = {0x61: base_a, 0xE0: a_grave}
    return CanonicalFontModel(
        family_name="R3 Synthetic",
        style_name="Regular",
        reference_id="ab" * 32,
        style_id="regular",
        metrics=GlobalFontMetrics(),
        glyphs=glyphs,
        config_hash="cd" * 32,
        browser_version="test",
        fit_observations_count=len(glyphs),
        calibration_fingerprint="ef" * 32,
    )


async def test_ai_calls_are_per_class_never_per_glyph():
    model = _synthetic_model()
    provider = RecordingAIProvider()
    service = VietnameseExtensionService(
        ai_provider=provider, config_hash="c" * 64, source_hash="s" * 64
    )
    # a-acute (0xE1) resolves deterministically from the a-grave donor;
    # stacked/horn glyphs without donors stay for the AI gate.
    unresolved_expected = sorted(
        cp
        for cp in VIETNAMESE_REQUIRED_CODEPOINTS
        if cp not in model.glyphs and service._deterministic_glyph(model, cp) is None
    )
    # The a-grave donor deterministically proves U+0300 (its exact mark
    # contour); Vietnamese precomposed/horn/D-stroke glyphs without donors
    # stay for the AI gate.
    assert 0x300 not in unresolved_expected
    assert len(unresolved_expected) > 1

    adapter = AtlasVietnameseAdapter(service)
    extended, evidence = await adapter.extend(model)

    # Exactly ONE batched call for ALL unresolved glyphs (per-class ceiling
    # honored: one StyleProfile request; never one call per glyph).
    assert len(provider.calls) == 1
    assert provider.calls[0] == unresolved_expected
    assert evidence["ai_calls_used"] == 1
    assert evidence["ai_calls_budget"] >= 1
    assert evidence["ai_calls_used"] <= evidence["ai_calls_budget"]
    # Deterministic-first code points are recorded separately.
    assert 0x300 in evidence["deterministic_codepoints"]
    # Binding carries the sanitized route identity (no secrets).
    assert len(evidence["binding_hash"]) == 64


async def test_fail_closed_provider_error_propagates():
    class ExplodingProvider(RecordingAIProvider):
        async def generate_candidates(self, request):
            raise VietnameseAIIntegrityError("VI_AI_ALL_ROUTES_FAILED")

    model = _synthetic_model()
    service = VietnameseExtensionService(
        ai_provider=ExplodingProvider(), config_hash="c" * 64, source_hash="s" * 64
    )
    adapter = AtlasVietnameseAdapter(service)
    with pytest.raises(VietnameseAIIntegrityError):
        await adapter.extend(model)


# ----------------------------------------------------------------------
# 5. Pipeline stage 5b consumes the injected runtime provider (no hardcode)
# ----------------------------------------------------------------------

FIXTURE_FONT = (
    Path(__file__).parent.parent
    / "benchmark_data"
    / "ground_truth"
    / "BeVietnamPro-Regular.ttf"
)


def test_pipeline_stage5b_uses_injected_runtime_provider(tmp_path: Path):
    """VIETNAMESE run with the runtime AI provider injected: deterministic
    composition runs first; every remaining glyph CLASS reaches the provider
    in ONE batched call (never per-glyph); the run completes end-to-end."""
    import asyncio
    import time

    from atlas.cache import AtlasCacheStore, AtlasCheckpointStore
    from atlas.local_fixture import LocalFontMetricsProvider, LocalFontRasterProvider
    from atlas.pipeline import AtlasStyleSpec, AtlasUltraPipeline
    from atlas.policy import AtlasRuntimeDefaults

    if not FIXTURE_FONT.exists():
        pytest.skip("ground-truth binary absent")

    subset = sorted(
        [0x20, 0x41, 0x61, 0x65, 0x6F, 0x75, 0xE0, 0xE1, 0xE3, 0xE8, 0xE9,
         0x1EA3, 0x1EA1]
        + [0x300, 0x301, 0x303, 0x309, 0x323]
    )
    provider = RecordingAIProvider()
    spec = AtlasStyleSpec(
        source_url="runtime://vn-stage5b",
        family_name="VN Runtime Proof",
        style_name="Regular",
        style_id="regular",
        mode="VIETNAMESE",
        code_points=subset,
    )
    pipeline = AtlasUltraPipeline(
        spec=spec,
        runtime=AtlasRuntimeDefaults(),
        metrics_provider=LocalFontMetricsProvider(FIXTURE_FONT),
        raster_provider=LocalFontRasterProvider(FIXTURE_FONT),
        cache=AtlasCacheStore(tmp_path / "cache"),
        checkpoint_store=AtlasCheckpointStore(tmp_path / "ckpt"),
        deadline=time.monotonic() + 300,
        ai_provider=provider,
    )
    result = asyncio.run(pipeline.run())

    # The injected runtime provider WAS reached (stage-5b hardcode removed)...
    assert len(provider.calls) == 1, "AI must be invoked per CLASS, not per glyph"
    batched = provider.calls[0]
    assert len(batched) > 1, "one batched call carries every unresolved glyph"
    assert len(set(batched)) == len(batched)
    # ...and never for glyphs already covered by the raster/deterministic path.
    frozen = set(result.frozen_glyphs)
    assert not (set(batched) & frozen)

    ev = result.evidence
    assert ev.failed_glyphs == 0
    assert result.report["passed"] is True
    assert result.ttf_path.exists() and result.otf_path.exists()
    # The built outputs carry the extended Vietnamese coverage.
    from fontTools.ttLib import TTFont

    font = TTFont(result.ttf_path)
    try:
        cmap = font.getBestCmap() or {}
        for cp in batched:
            assert cp in cmap, f"AI-resolved U+{cp:04X} missing from TTF cmap"
    finally:
        font.close()
