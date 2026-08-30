"""R4 LIVE VIETNAMESE smoke (T-FAST-ATLAS-PROD-READY-01, ADR-0003/ADR-0004).

Exactly ONE bounded VIETNAMESE run (mode=VIETNAMESE) of the atlas pipeline
with the REAL R3 cascade built from the explicit runtime dev.vars path
(E:\\cv\\telefont\\dev.vars; key NAMES only: wokushop_api_key /
openrouter_api_key - values are passed in-memory to the clients and NEVER
logged, printed, or written anywhere).

Bounds (contract R4):
- <= ~20 glyphs across a few diacritic classes from the Be Vietnam Pro
  ground truth;
- HARD call cap of 3 MODEL calls total, enforced at the HTTP transport:
  the 4th model call raises BEFORE firing (cap enforcement recorded);
- wall <= 12 minutes (hard-kill watchdog); pipeline deadline 11 minutes;
- NO retry: the run executes exactly once and whatever happens is recorded
  as evidence (fail-closed provider behavior is an honest outcome).

Transport substitution (recorded honestly, same as E-00024 U8): raster and
metrics come from LocalFont* file-read providers over the repo benchmark
ground truth binary; the AI cascade is the REAL production runtime path.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import httpx  # noqa: E402

from atlas.cache import AtlasCacheStore, AtlasCheckpointStore  # noqa: E402
from atlas.local_fixture import (  # noqa: E402
    LocalFontMetricsProvider,
    LocalFontRasterProvider,
)
from atlas.pipeline import AtlasStyleSpec, AtlasUltraPipeline  # noqa: E402
from atlas.policy import AtlasRuntimeDefaults  # noqa: E402
from atlas.vietnamese import glyph_class  # noqa: E402
from compute.ai_secret_loader import load_ai_secrets  # noqa: E402
from compute.openrouter_client import OpenRouterAIClient  # noqa: E402
from compute.vietnamese import (  # noqa: E402
    VietnameseAIIntegrityError,
    VietnameseExtensionService,
    missing_vietnamese_codepoints,
    validate_candidate_font_bytes,
    validate_nfc_nfd_coverage,
)
from compute.woku_client import WokuCascadeAIClient  # noqa: E402
from reconstruction.font_model import (  # noqa: E402
    CalibratedGlyph,
    CanonicalFontModel,
    GlobalFontMetrics,
)

DEV_VARS_PATH = Path(r"E:\cv\telefont\dev.vars")  # explicit runtime boundary
FIXTURE_FONT = (
    Path(__file__).resolve().parent.parent
    / "benchmark_data"
    / "ground_truth"
    / "BeVietnamPro-Regular.ttf"
)
CACHE_ROOT = Path(os.environ.get(
    "ATLAS_VN_LIVE_SMOKE_CACHE",
    str(Path.home() / ".telefont-atlas-vn-live-smoke-cache"),
))
WATCHDOG_KILL_SECONDS = 720    # 12-minute hard wall (contract R4)
PIPELINE_DEADLINE_SECONDS = 660  # inside the hard wall
MAX_MODEL_CALLS = 3            # hard cap; the 4th raises BEFORE firing

# BOUNDED subset (20 glyphs): base letters, the Vietnamese tone combining
# marks, single-mark donors, and representative precomposed glyphs across a
# few diacritic classes (circumflex-stack, horn-stack, grave/acute/hook/dot).
SUBSET = sorted({
    0x20, 0x61, 0x65, 0x6F, 0x75,
    0x300, 0x301, 0x303, 0x309, 0x323,
    0xE0, 0xE1, 0xE3, 0x1EA3, 0x1EA1,
    0x1EA5, 0x1EC1, 0x1EDF, 0x1EE9, 0x1EAD,
})


class VnSmokeCapExceeded(RuntimeError):
    """Hard model-call cap reached: the next call must not fire."""


class CappedModelCallTransport(httpx.AsyncBaseTransport):
    """Counts REAL model calls (chat/completions requests to any provider)
    and raises BEFORE firing once the cap is reached."""

    def __init__(self) -> None:
        self._inner = httpx.AsyncHTTPTransport()
        self.model_calls = 0
        self.models_called: list[str] = []
        self.cap_enforced = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "chat/completions" in url:
            if self.model_calls >= MAX_MODEL_CALLS:
                self.cap_enforced = True
                raise VnSmokeCapExceeded(
                    "VN_SMOKE_MODEL_CALL_CAP_EXCEEDED_BEFORE_FIRING"
                )
            self.model_calls += 1
            try:
                body = json.loads(request.content)
                self.models_called.append(str(body.get("model", ""))[:100])
            except Exception:
                self.models_called.append("")
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


class RecordingProviderWrapper:
    """Counts provider-level generate_candidates invocations (batching proof)."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.provider_calls = 0
        self.batch_sizes: list[int] = []

    @property
    def model_id(self):
        return self._inner.model_id

    @property
    def model_version(self):
        return self._inner.model_version

    def prompt_hash(self) -> str:
        return self._inner.prompt_hash()

    async def generate_candidates(self, request: dict):
        self.provider_calls += 1
        self.batch_sizes.append(len(request.get("missing_codepoints", [])))
        return await self._inner.generate_candidates(request)

    @property
    def last_route_trace(self):
        return getattr(self._inner, "last_route_trace", None)


def _watchdog(start_monotonic: float) -> None:
    while True:
        if time.monotonic() - start_monotonic > WATCHDOG_KILL_SECONDS:
            print("VN_LIVE_SMOKE_WATCHDOG_KILL: 12-minute wall exceeded", flush=True)
            os._exit(3)
        time.sleep(1.0)


def _yaml(obj, indent: int = 0) -> str:
    pad = "  " * indent
    if isinstance(obj, dict):
        if not obj:
            return pad + "{}\n"
        out = ""
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                out += f"{pad}{k}:\n{_yaml(v, indent + 1)}"
            else:
                out += f"{pad}{k}: {json.dumps(v)}\n"
        return out
    if isinstance(obj, list):
        if not obj:
            return pad + "[]\n"
        out = ""
        for v in obj:
            if isinstance(v, (dict, list)):
                out += f"{pad}-\n{_yaml(v, indent + 1)}"
            else:
                out += f"{pad}- {json.dumps(v)}\n"
        return out
    return pad + json.dumps(obj) + "\n"


def _cp_list(cps) -> list[str]:
    return [f"U+{cp:04X}" for cp in sorted(cps)]


def _build_runtime_provider():
    """REAL R3 cascade from the explicit dev.vars path; values stay in
    memory and are never logged/printed/written."""
    secrets = load_ai_secrets(DEV_VARS_PATH)
    woku_key = secrets.get("wokushop_api_key", "")
    or_key = secrets.get("openrouter_api_key", "")
    if not woku_key and not or_key:
        return None, "VN_BLOCKED: neither wokushop_api_key nor openrouter_api_key present"
    transport = CappedModelCallTransport()
    shared_client = httpx.AsyncClient(transport=transport, timeout=120.0)
    downstream = OpenRouterAIClient(or_key, client=shared_client) if or_key else None
    if woku_key:
        cascade = WokuCascadeAIClient(woku_key, downstream=downstream, client=shared_client)
    else:
        cascade = downstream
    return RecordingProviderWrapper(cascade), transport


def main() -> int:
    t_boot = time.monotonic()
    threading.Thread(target=_watchdog, args=(t_boot,), daemon=True).start()

    ev: dict = {
        "task": "T-FAST-ATLAS-PROD-READY-01",
        "acceptance": "R4",
        "run_label": "vn_live_smoke_single",
        "bounds": {
            "watchdog_kill_seconds": WATCHDOG_KILL_SECONDS,
            "pipeline_deadline_seconds": PIPELINE_DEADLINE_SECONDS,
            "max_model_calls": MAX_MODEL_CALLS,
            "retry": "none (exactly one run)",
            "glyph_subset_count": len(SUBSET),
            "glyph_subset": _cp_list(SUBSET),
        },
        "transport_substitution": (
            "raster/metrics: LocalFont* file-read providers over the repo "
            "benchmark ground truth BeVietnamPro-Regular.ttf (same authorized "
            "substitution as E-00024 U8); AI cascade: REAL production runtime "
            "path from the explicit dev.vars boundary"
        ),
        "secret_boundary": {
            "dev_vars_path": str(DEV_VARS_PATH),
            "keys_loaded_names_only": sorted(
                load_ai_secrets(DEV_VARS_PATH).keys()
            ),
            "values_logged_or_written": False,
        },
    }

    provider, transport_or_reason = _build_runtime_provider()
    if provider is None:
        ev["outcome"] = "VN_BLOCKED"
        ev["blocked_reason"] = transport_or_reason
        _save_and_print(ev, t_boot)
        return 0
    transport = transport_or_reason

    spec = AtlasStyleSpec(
        source_url="smoke://vn-live/be-vietnam-pro",
        family_name="VN Live Smoke",
        style_name="Regular",
        style_id="regular",
        mode="VIETNAMESE",
        code_points=SUBSET,
    )
    cache = AtlasCacheStore(CACHE_ROOT / "cache")
    ck_store = AtlasCheckpointStore(CACHE_ROOT / "checkpoints")
    pipeline = AtlasUltraPipeline(
        spec=spec,
        runtime=AtlasRuntimeDefaults(),
        metrics_provider=LocalFontMetricsProvider(FIXTURE_FONT),
        raster_provider=LocalFontRasterProvider(FIXTURE_FONT),
        cache=cache,
        checkpoint_store=ck_store,
        deadline=time.monotonic() + PIPELINE_DEADLINE_SECONDS,
        ai_provider=provider,
    )

    t0 = time.monotonic()
    run_section: dict = {}
    result = None
    try:
        result = asyncio.run(pipeline.run())
        run_section["outcome"] = "COMPLETED"
        run_section["validation_passed"] = bool(result.report.get("passed"))
        run_section["validation_reasons"] = list(result.report.get("reasons", []))
        run_section["normalization"] = result.report.get("normalization")
        run_section["ttf_bytes"] = result.ttf_path.stat().st_size if result.ttf_path else 0
        run_section["otf_bytes"] = result.otf_path.stat().st_size if result.otf_path else 0
    except VietnameseAIIntegrityError as exc:
        run_section["outcome"] = "FAIL_CLOSED"
        run_section["error_type"] = type(exc).__name__
        run_section["error"] = str(exc)
        run_section["note"] = (
            "provider unavailable/invalid or cap-exhausted: the cascade "
            "failed CLOSED (no publish, no retry) - honest VN runtime evidence"
        )
    except Exception as exc:  # honest capture, no blind retry
        run_section["outcome"] = "ERROR"
        run_section["error_type"] = type(exc).__name__
        run_section["error"] = str(exc)
    run_section["wall_seconds"] = round(time.monotonic() - t0, 3)
    ev["run"] = run_section

    # ---- AI runtime evidence (counts/models/classes; never secret values) --
    trace = provider.last_route_trace
    ev["ai_runtime"] = {
        "model_calls_made": transport.model_calls,
        "models_called": transport.models_called,
        "cap_enforced_before_firing": transport.cap_enforced,
        "provider_generate_calls": provider.provider_calls,
        "provider_batch_sizes": provider.batch_sizes,
        "route": trace.route if trace else "",
        "fallback_reason": trace.fallback_reason if trace else "",
        "cascade_calls": (
            [
                {
                    "provider": c.provider,
                    "model": c.model,
                    "role": c.role,
                    "status": c.status,
                    "served_model": c.served_model,
                }
                for c in trace.calls
            ]
            if trace
            else []
        ),
    }

    # ---- Class accounting: deterministic vs AI-resolved --------------------
    if result is not None and result.model is not None:
        frozen_cps = set(result.frozen_glyphs)
        extended_cps = set(result.model.glyphs)
        added = sorted(extended_cps - frozen_cps)

        # Reconstruct the pre-extension (frozen-only) model to recompute the
        # deterministic resolution set with the identical stage-5b service.
        frozen_model = CanonicalFontModel(
            family_name=spec.family_name,
            style_name=spec.style_name,
            reference_id="ab" * 32,
            style_id=spec.style_id,
            metrics=GlobalFontMetrics(),
            glyphs=dict(result.frozen_glyphs),
            config_hash="cd" * 32,
            browser_version="vn_live_smoke",
            fit_observations_count=max(1, len(frozen_cps)),
            calibration_fingerprint="ef" * 32,
        )
        service = VietnameseExtensionService(
            ai_provider=None, config_hash="cd" * 32, source_hash="ab" * 32
        )
        deterministic_cps = []
        ai_required_cps = []
        for cp in sorted(missing_vietnamese_codepoints(frozen_model)):
            try:
                g = service._deterministic_glyph(frozen_model, cp)
            except Exception:
                g = None
            (deterministic_cps if g is not None else ai_required_cps).append(cp)
        ai_resolved_cps = sorted(set(added) - set(deterministic_cps))
        ev["classes"] = {
            "frozen_raster_glyphs": len(frozen_cps),
            "missing_required_before_extension": len(
                missing_vietnamese_codepoints(frozen_model)
            ),
            "resolved_deterministic": {
                "count": len(deterministic_cps),
                "classes": sorted({glyph_class(cp) for cp in deterministic_cps}),
                "codepoints": _cp_list(deterministic_cps),
            },
            "resolved_ai": {
                "count": len(ai_resolved_cps),
                "classes": sorted({glyph_class(cp) for cp in ai_resolved_cps}),
                "codepoints": _cp_list(ai_resolved_cps),
            },
            "still_missing_after_extension": _cp_list(
                set(missing_vietnamese_codepoints(result.model))
            ),
        }
        try:
            ttf_bytes = result.ttf_path.read_bytes() if result.ttf_path else b""
            ev["vn_gate_on_final_ttf"] = (
                validate_candidate_font_bytes(ttf_bytes, result.model)
                if ttf_bytes
                else ["NO_TTF"]
            )
        except Exception as exc:
            ev["vn_gate_on_final_ttf"] = [f"{type(exc).__name__}"]
        ev["nfc_nfd_post_extension_failures"] = validate_nfc_nfd_coverage(result.model)

    _save_and_print(ev, t_boot)
    return 0


def _save_and_print(ev: dict, t_boot: float) -> None:
    ev["timing"] = {
        "total_wall_seconds": round(time.monotonic() - t_boot, 3),
        "wall_cap_seconds": WATCHDOG_KILL_SECONDS,
        "within_cap": bool(time.monotonic() - t_boot < WATCHDOG_KILL_SECONDS),
    }
    out_path = (
        Path(__file__).resolve().parent.parent.parent
        / ".prime" / "tasks" / "T-FAST-ATLAS-PROD-READY-01"
        / "vn-live-smoke-evidence.yaml"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(_yaml(ev), encoding="utf-8")
    print(_yaml(ev))
    print(f"EVIDENCE_SAVED: {out_path}")


if __name__ == "__main__":
    raise SystemExit(main())
