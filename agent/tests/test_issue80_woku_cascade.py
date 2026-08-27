"""Issue #80 / D17: Woku-primary Vietnamese AI cascade (fake-transport pack).

Bounded cascade under fake transports only (live provider requests=0, real
dev.vars reads=0):
- exact Woku `gpt-5.6-luna` PRIMARY -> exact `gemini-3.7-flash` FALLBACK
  -> existing OpenRouter route unchanged as downstream fallback;
- one bounded attempt per exact model, no retry, no substitution;
- closed schema/validator fail-closed at every stage;
- provider/model/route/fallback-reason identities bind provenance;
- ORIGINAL/complete-coverage zero-call paths preserved.
"""
import hashlib
import json
import re

import httpx
import pytest

from compute.openrouter_client import (
    MODEL_ARBITER,
    MODEL_DIFFICULT,
    MODEL_PRIMARY as OR_MODEL_PRIMARY,
    OpenRouterAIClient,
)
from compute.vietnamese import (
    VIETNAMESE_REQUIRED_CODEPOINTS,
    VietnameseAIIntegrityError,
    VietnameseExtensionBinding,
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

ALLOWED_MODELS = {
    WOKU_MODEL_PRIMARY,
    WOKU_MODEL_FALLBACK,
    OR_MODEL_PRIMARY,
    MODEL_DIFFICULT,
    MODEL_ARBITER,
}


def _request(missing):
    return {
        "missing_codepoints": list(missing),
        "units_per_em": 1000,
        "source_hash": "s" * 64,
        "config_hash": "c" * 64,
        "style_evidence": {
            "family_name": "Cascade Fam",
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


def _make_woku_handler(responses, calls, prompts):
    """responses: model -> content str | int status | Exception instance/class."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == WOKU_CHAT_ENDPOINT
        body = json.loads(request.content)
        model = body["model"]
        calls.append(model)
        prompt = body["messages"][0]["content"]
        prompts.append(prompt)
        spec = responses.get(model, 500)
        if isinstance(spec, Exception):
            raise spec
        if isinstance(spec, type) and issubclass(spec, Exception):
            raise spec("transport down")
        if callable(spec):
            found = re.search(r"Missing code points: (\[[^\]]*\])", prompt)
            missing = json.loads(found.group(1)) if found else []
            spec = spec(missing)
        if isinstance(spec, int):
            return httpx.Response(spec, json={"error": "fake"})
        return httpx.Response(200, json={"choices": [{"message": {"content": spec}}]})

    return handler


def _make_cascade(responses, downstream=None):
    calls: list[str] = []
    prompts: list[str] = []
    transport = httpx.MockTransport(_make_woku_handler(responses, calls, prompts))
    cascade = WokuCascadeAIClient(
        "wk-fake-runtime-secret",
        downstream=downstream,
        client=httpx.AsyncClient(transport=transport),
    )
    return cascade, calls, prompts


def _make_downstream(or_body, or_calls):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        or_calls.append(body["model"])
        prompt = body["messages"][0]["content"]
        found = re.search(r"Missing code points: (\[[^\]]*\])", prompt)
        missing = json.loads(found.group(1)) if found else []
        content = or_body(missing) if callable(or_body) else or_body
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    client = OpenRouterAIClient(
        "sk-or-v1-fake-runtime-secret",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return client


MISSING = [0x0110, 0x0111]


@pytest.mark.asyncio
async def test_ISSUE80_WOKU_CASCADE_primary_exact_model_single_bounded_call():
    or_calls: list[str] = []
    downstream = _make_downstream(_valid_candidate_payload, or_calls)
    cascade, calls, _ = _make_cascade({WOKU_MODEL_PRIMARY: _valid_candidate_payload(MISSING)}, downstream)

    specs = await cascade.generate_candidates(_request(MISSING))

    assert len(specs) == 2
    assert calls == [WOKU_MODEL_PRIMARY]  # exact model, exactly once
    assert or_calls == []  # downstream never contacted on PRIMARY success
    trace = cascade.last_route_trace
    assert trace is not None
    assert trace.route == ROUTE_WOKU_PRIMARY
    assert trace.fallback_reason == ""
    assert cascade.route_model_identities() == (WOKU_MODEL_PRIMARY,)


@pytest.mark.asyncio
async def test_ISSUE80_WOKU_CASCADE_primary_unavailable_exact_fallback():
    or_calls: list[str] = []
    downstream = _make_downstream(_valid_candidate_payload, or_calls)
    cascade, calls, _ = _make_cascade(
        {WOKU_MODEL_PRIMARY: 503, WOKU_MODEL_FALLBACK: _valid_candidate_payload(MISSING)},
        downstream,
    )

    specs = await cascade.generate_candidates(_request(MISSING))

    assert len(specs) == 2
    assert calls == [WOKU_MODEL_PRIMARY, WOKU_MODEL_FALLBACK]
    assert or_calls == []
    trace = cascade.last_route_trace
    assert trace.route == ROUTE_WOKU_FALLBACK
    assert trace.fallback_reason == "woku_primary_unavailable"
    assert cascade.route_model_identities() == (WOKU_MODEL_PRIMARY, WOKU_MODEL_FALLBACK)


@pytest.mark.asyncio
async def test_ISSUE80_WOKU_CASCADE_primary_invalid_deterministic_rejection():
    # Schema violation (closed-schema parse fails) -> deterministic rejection.
    cascade, calls, _ = _make_cascade(
        {WOKU_MODEL_PRIMARY: '{"glyphs": "forged"}', WOKU_MODEL_FALLBACK: _valid_candidate_payload(MISSING)},
    )
    await cascade.generate_candidates(_request(MISSING))
    assert calls == [WOKU_MODEL_PRIMARY, WOKU_MODEL_FALLBACK]
    assert cascade.last_route_trace.fallback_reason == "woku_primary_invalid"

    # Incomplete coverage (validator rejects) is also a deterministic rejection.
    incomplete = json.dumps({"glyphs": [json.loads(_valid_candidate_payload(MISSING))["glyphs"][0]]})
    cascade2, calls2, _ = _make_cascade(
        {WOKU_MODEL_PRIMARY: incomplete, WOKU_MODEL_FALLBACK: _valid_candidate_payload(MISSING)},
    )
    await cascade2.generate_candidates(_request(MISSING))
    assert calls2 == [WOKU_MODEL_PRIMARY, WOKU_MODEL_FALLBACK]
    assert cascade2.last_route_trace.fallback_reason == "woku_primary_invalid"


@pytest.mark.asyncio
async def test_ISSUE80_WOKU_CASCADE_downstream_openrouter_route_unchanged():
    or_calls: list[str] = []
    downstream = _make_downstream(_valid_candidate_payload, or_calls)
    cascade, calls, _ = _make_cascade(
        {WOKU_MODEL_PRIMARY: httpx.ConnectError, WOKU_MODEL_FALLBACK: "not-json"},
        downstream,
    )

    specs = await cascade.generate_candidates(_request(MISSING))

    assert len(specs) == 2
    assert calls == [WOKU_MODEL_PRIMARY, WOKU_MODEL_FALLBACK]
    assert or_calls == [OR_MODEL_PRIMARY]  # existing route, unchanged
    trace = cascade.last_route_trace
    assert trace.route == ROUTE_OPENROUTER
    assert trace.fallback_reason == "woku_primary_unavailable+woku_fallback_invalid"
    assert cascade.route_model_identities() == (
        WOKU_MODEL_PRIMARY,
        WOKU_MODEL_FALLBACK,
        OR_MODEL_PRIMARY,
    )


@pytest.mark.asyncio
async def test_ISSUE80_WOKU_CASCADE_fail_closed_no_downstream_and_downstream_rejection():
    # No downstream wired: both Woku models fail -> fail closed, trace recorded.
    cascade, calls, _ = _make_cascade(
        {WOKU_MODEL_PRIMARY: 500, WOKU_MODEL_FALLBACK: 500}, downstream=None
    )
    with pytest.raises(VietnameseAIIntegrityError, match="VI_AI_ALL_ROUTES_FAILED"):
        await cascade.generate_candidates(_request(MISSING))
    trace = cascade.last_route_trace
    assert trace.route == ROUTE_OPENROUTER
    assert trace.fallback_reason == "woku_primary_unavailable+woku_fallback_unavailable"
    assert [c.status for c in trace.calls] == ["UNAVAILABLE", "UNAVAILABLE"]

    # Downstream present but its closed schema rejects everything: the
    # downstream fail-closed error propagates unchanged (no bypass).
    or_calls: list[str] = []
    downstream = _make_downstream("forged-prose-not-json", or_calls)
    cascade2, calls2, _ = _make_cascade(
        {WOKU_MODEL_PRIMARY: 500, WOKU_MODEL_FALLBACK: 500}, downstream
    )
    with pytest.raises(VietnameseAIIntegrityError, match="VI_AI_ALL_ROUTES_FAILED"):
        await cascade2.generate_candidates(_request(MISSING))
    # Unchanged downstream route: PRIMARY invalid -> deterministic DIFFICULT
    # escalation; both rejected -> fail closed propagates unchanged.
    assert or_calls == [OR_MODEL_PRIMARY, MODEL_DIFFICULT]
    assert cascade2.last_route_trace.route == ROUTE_OPENROUTER
    # Downstream call statuses are bound into the cascade trace.
    assert cascade2.last_route_trace.calls[-1].provider == "openrouter"
    assert cascade2.last_route_trace.calls[-1].status == "VALIDATION_FAILED"


@pytest.mark.asyncio
async def test_ISSUE80_WOKU_CASCADE_bounded_no_retry_no_substitution():
    total_requests = {WOKU_MODEL_PRIMARY: 0, WOKU_MODEL_FALLBACK: 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        total_requests[body["model"]] += 1
        if body["model"] == WOKU_MODEL_PRIMARY:
            return httpx.Response(500, json={})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": _valid_candidate_payload(MISSING)}}]}
        )

    cascade = WokuCascadeAIClient(
        "wk-fake-runtime-secret",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    # Two independent requests: exactly one bounded attempt per exact model per
    # request; never a retry of a failed model.
    for _ in range(2):
        await cascade.generate_candidates(_request(MISSING))
    assert total_requests == {WOKU_MODEL_PRIMARY: 2, WOKU_MODEL_FALLBACK: 2}

    # No substitution: only the exact cascade models were ever requested.
    assert set(total_requests) == {WOKU_MODEL_PRIMARY, WOKU_MODEL_FALLBACK}
    assert set(total_requests) <= ALLOWED_MODELS


@pytest.mark.asyncio
async def test_ISSUE80_WOKU_CASCADE_zero_call_paths():
    calls: list[str] = []
    cascade, calls, _ = _make_cascade({})
    # Empty missing coverage -> zero HTTP calls.
    assert await cascade.generate_candidates(_request([])) == []
    assert calls == []

    # Complete VIETNAMESE coverage -> extension service never touches AI.
    glyphs = {
        cp: CalibratedGlyph(
            code_point=cp,
            character=chr(cp),
            advance_width_upem=600.0,
            lsb_upem=50.0,
            rsb_upem=50.0,
            ascent_upem=700.0,
            descent_upem=-200.0,
            bounding_box_upem=(50.0, -200.0, 550.0, 700.0),
            contours=[],
        )
        for cp in VIETNAMESE_REQUIRED_CODEPOINTS
    }
    model = CanonicalFontModel(family_name="Full", style_name="Regular", glyphs=glyphs)
    service = VietnameseExtensionService(cascade, config_hash="c" * 64, source_hash="s" * 64)
    extended, binding = await service.extend(model)
    assert calls == []
    assert extended is model
    assert binding.extended_codepoints == ()
    assert binding.ai_model_id == ""
    assert binding.ai_route == ""


def _minimal_model() -> CanonicalFontModel:
    contour = Contour(
        segments=[
            LineSegment(Point2D(50.0, 50.0), Point2D(550.0, 50.0)),
            LineSegment(Point2D(550.0, 50.0), Point2D(550.0, 700.0)),
            LineSegment(Point2D(550.0, 700.0), Point2D(50.0, 700.0)),
            LineSegment(Point2D(50.0, 700.0), Point2D(50.0, 50.0)),
        ],
        is_hole=False,
    )
    glyph = CalibratedGlyph(
        code_point=65,
        character="A",
        advance_width_upem=600.0,
        lsb_upem=50.0,
        rsb_upem=50.0,
        ascent_upem=700.0,
        descent_upem=-200.0,
        bounding_box_upem=(50.0, -200.0, 550.0, 700.0),
        contours=[contour],
        observation_fingerprints=("f" * 64,),
    )
    return CanonicalFontModel(
        family_name="Cascade Fam",
        style_name="Regular",
        metrics=GlobalFontMetrics(),
        glyphs={65: glyph},
        config_hash="c" * 64,
    )


# =========================================================================
# Provenance/cache identity binding and deterministic reuse
# =========================================================================


class _LegacyFakeProvider:
    """Non-cascade provider shape (no last_route_trace): legacy binding."""

    model_id = "openrouter"
    model_version = "openrouter-route-v1"

    def prompt_hash(self) -> str:
        return hashlib.sha256(b"legacy_prompt").hexdigest()

    async def generate_candidates(self, request):
        from compute.vietnamese import AICandidateSpec, MARK_CODEPOINT_SET

        specs = []
        for cp in request["missing_codepoints"]:
            anchors = (("mark", 300.0, 700.0),) if cp in MARK_CODEPOINT_SET else ()
            specs.append(
                AICandidateSpec(
                    code_point=cp,
                    contours=(((50.0, 50.0), (550.0, 50.0), (550.0, 700.0), (50.0, 700.0)),),
                    advance_width_upem=600.0,
                    lsb_upem=50.0,
                    rsb_upem=50.0,
                    ascent_upem=700.0,
                    descent_upem=-200.0,
                    anchors=anchors,
                )
            )
        return specs


@pytest.mark.asyncio
async def _extend_with(cascade):
    service = VietnameseExtensionService(cascade, config_hash="c" * 64, source_hash="s" * 64)
    return await service.extend(_minimal_model())


@pytest.mark.asyncio
async def test_ISSUE80_WOKU_CASCADE_binding_identity_separation_and_determinism():
    # Scenario A: Woku PRIMARY succeeds.
    cascade_a, _, _ = _make_cascade({WOKU_MODEL_PRIMARY: _valid_candidate_payload})
    _, binding_a = await _extend_with(cascade_a)
    assert binding_a.ai_route == ROUTE_WOKU_PRIMARY
    assert binding_a.ai_fallback_reason == ""
    assert binding_a.ai_route_models == (WOKU_MODEL_PRIMARY,)

    # Scenario B: Woku FALLBACK succeeds after PRIMARY unavailable.
    cascade_b, _, _ = _make_cascade(
        {WOKU_MODEL_PRIMARY: 500, WOKU_MODEL_FALLBACK: _valid_candidate_payload}
    )
    _, binding_b = await _extend_with(cascade_b)
    assert binding_b.ai_route == ROUTE_WOKU_FALLBACK
    assert binding_b.ai_fallback_reason == "woku_primary_unavailable"
    assert binding_b.ai_route_models == (WOKU_MODEL_PRIMARY, WOKU_MODEL_FALLBACK)

    # Scenario C: downstream OpenRouter route after both Woku models fail.
    or_calls: list[str] = []
    downstream = _make_downstream(_valid_candidate_payload, or_calls)
    cascade_c, _, _ = _make_cascade(
        {WOKU_MODEL_PRIMARY: 500, WOKU_MODEL_FALLBACK: 500}, downstream
    )
    _, binding_c = await _extend_with(cascade_c)
    assert binding_c.ai_route == ROUTE_OPENROUTER
    assert binding_c.ai_fallback_reason == "woku_primary_unavailable+woku_fallback_unavailable"
    # Downstream identities are bound too: the large unresolved set triggers
    # the unchanged route's deterministic DIFFICULT escalation.
    assert binding_c.ai_route_models == (
        WOKU_MODEL_PRIMARY,
        WOKU_MODEL_FALLBACK,
        OR_MODEL_PRIMARY,
        MODEL_DIFFICULT,
    )

    # Route/fallback-reason/model identities bind the provenance hash: every
    # route is a distinct cache/provenance identity (no unbound cross-provider
    # reuse), while the extended code point set is identical.
    assert binding_a.extended_codepoints == binding_b.extended_codepoints == binding_c.extended_codepoints
    hashes = {
        binding_a.compute_binding_hash(),
        binding_b.compute_binding_hash(),
        binding_c.compute_binding_hash(),
    }
    assert len(hashes) == 3

    # Deterministic exact-compatible reuse: identical route/outcome reproduces
    # the identical binding hash (skip-safe identity).
    cascade_a2, _, _ = _make_cascade({WOKU_MODEL_PRIMARY: _valid_candidate_payload})
    _, binding_a2 = await _extend_with(cascade_a2)
    assert binding_a2.compute_binding_hash() == binding_a.compute_binding_hash()


@pytest.mark.asyncio
async def test_ISSUE80_WOKU_CASCADE_legacy_binding_shape_unchanged():
    # Non-cascade providers keep the exact legacy binding shape/hash: the new
    # identity dimensions are absent (not merely empty) from the payload.
    provider = _LegacyFakeProvider()
    service = VietnameseExtensionService(provider, config_hash="c" * 64, source_hash="s" * 64)
    _, binding = await service.extend(_minimal_model())
    payload = binding.to_dict()
    assert "ai_route" not in payload
    assert "ai_fallback_reason" not in payload
    assert "ai_route_models" not in payload

    legacy = VietnameseExtensionBinding(
        mode="VIETNAMESE",
        ai_model_id=provider.model_id,
        ai_model_version=provider.model_version,
        ai_prompt_hash=provider.prompt_hash(),
        config_hash="c" * 64,
        source_hash="s" * 64,
        extended_codepoints=binding.extended_codepoints,
        preserved_codepoints=binding.preserved_codepoints,
        deterministic_codepoints=binding.deterministic_codepoints,
    )
    assert legacy.compute_binding_hash() == binding.compute_binding_hash()


@pytest.mark.asyncio
async def test_ISSUE80_WOKU_CASCADE_prompt_schema_identity():
    prompts: list[str] = []
    cascade, _, prompts = _make_cascade(
        {WOKU_MODEL_PRIMARY: 500, WOKU_MODEL_FALLBACK: _valid_candidate_payload(MISSING)},
    )
    await cascade.generate_candidates(_request(MISSING))

    # Both exact Woku models receive the identical closed-schema prompt.
    assert len(prompts) == 2
    assert prompts[0] == prompts[1]
    assert '"glyphs"' in prompts[0]

    # Deterministic prompt hash; distinct from the OpenRouter-only identity.
    assert cascade.prompt_hash() == cascade.prompt_hash()
    or_client = OpenRouterAIClient("sk-or-v1-fake-runtime-secret")
    assert cascade.prompt_hash() != or_client.prompt_hash()


# =========================================================================
# Composition secret boundary and wiring
# =========================================================================


def test_ISSUE80_COMPOSITION_secret_boundary_and_cascade_wiring(tmp_path, test_settings):
    """Fake temporary dev.vars values only; real dev.vars reads=0, live=0."""
    from composition import build_production_components, load_dev_vars_secret

    fake_woku = "wk-fake-key-only-runnable-000000000000000000"
    fake_or = "sk-or-v1-fake-key-only-runnable-000000000000"
    dev_vars = tmp_path / "dev.vars"
    dev_vars.write_text(
        "# temporary fake secret file (test only)\n"
        f"wokushop_api_key = {fake_woku}\n"
        f"openrouter_api_key = {fake_or}\n",
        encoding="utf-8",
    )

    # Lowercase key shapes parse exactly; missing key/file fails closed.
    assert load_dev_vars_secret(dev_vars, "wokushop_api_key") == fake_woku
    assert load_dev_vars_secret(dev_vars, "openrouter_api_key") == fake_or
    assert load_dev_vars_secret(tmp_path / "absent.dev.vars", "wokushop_api_key") == ""

    # Both keys: Woku-primary cascade with the unchanged OpenRouter route
    # wired as downstream fallback.
    components = build_production_components(test_settings, tmp_path / "scratch", dev_vars_path=dev_vars)
    provider = components["vietnamese_ai_provider"]
    assert isinstance(provider, WokuCascadeAIClient)
    assert provider.model_id == "woku-cascade"
    assert isinstance(provider._downstream, OpenRouterAIClient)

    # Woku key only: cascade constructible, downstream absent (fail-closed on
    # all-routes failure instead of an unconfigured OpenRouter route).
    woku_only = tmp_path / "dev.vars.woku"
    woku_only.write_text(f"wokushop_api_key = {fake_woku}\n", encoding="utf-8")
    components2 = build_production_components(test_settings, tmp_path / "scratch2", dev_vars_path=woku_only)
    provider2 = components2["vietnamese_ai_provider"]
    assert isinstance(provider2, WokuCascadeAIClient)
    assert provider2._downstream is None

    # OpenRouter key only: existing route unchanged.
    or_only = tmp_path / "dev.vars.or"
    or_only.write_text(f"openrouter_api_key = {fake_or}\n", encoding="utf-8")
    components3 = build_production_components(test_settings, tmp_path / "scratch3", dev_vars_path=or_only)
    provider3 = components3["vietnamese_ai_provider"]
    assert isinstance(provider3, OpenRouterAIClient)
    assert provider3.model_id == "openrouter"

    # No keys + AI explicitly enabled: fail closed.
    enabled = test_settings.model_copy(update={"VIETNAMESE_AI_ENABLED": True})
    empty_vars = tmp_path / "dev.vars.empty"
    empty_vars.write_text("# no keys\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="COMPOSITION_READINESS_FAILED_OPENROUTER"):
        build_production_components(enabled, tmp_path / "scratch4", dev_vars_path=empty_vars)

    # No dev.vars path: nothing is read and (flag false) no provider exists.
    bare = build_production_components(test_settings, tmp_path / "scratch5")
    assert bare["vietnamese_ai_provider"] is None


def test_ISSUE80_SETTINGS_DEFAULT_NO_WOKU_DEV_VARS_READ(tmp_path, monkeypatch):
    """Direct Settings construction can never open a cwd/repo dev.vars for the
    Woku key either; consumption happens only at the composition boundary."""
    from config import Settings

    (tmp_path / "dev.vars").write_text(
        "wokushop_api_key = wk-sentinel-must-not-load\n"
        "openrouter_api_key = sk-or-v1-sentinel-must-not-load\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WOKUSHOP_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    settings = Settings(
        _env_file=None,
        CF_ACCOUNT_ID="acc1",
        CF_QUEUE_ID="q1",
        CF_QUEUES_TOKEN="tok1",
        EDGE_BASE_URL="http://example.com/edge",
        A23_NODE_SECRET="sec1",
        SCRATCH_DIR=tmp_path,
    )
    assert settings.WOKUSHOP_API_KEY is None
    assert settings.OPENROUTER_API_KEY is None
    assert settings.VIETNAMESE_AI_ENABLED is False
