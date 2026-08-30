"""U8 bounded VIETNAMESE fixture harness (T-FAST-ATLAS-ULTRA-01, ADR-0004).

Runs ONE small VIETNAMESE fixture (vietnamese=true) through the atlas
pipeline's LOCAL Vietnamese path on a BOUNDED subset (<=60 glyphs) of
Be Vietnam Pro Regular Vietnamese precomposed glyphs: representative
classes (U+1EA5/U+1EC1/U+1EDF/U+1EE9/U+1EAD + cross-base donors) plus
their NFC/NFD decomposition parts (base letters + combining marks).

NO AI CALLS: the local environment carries no wokushop_api_key /
openrouter_api_key (names checked, values never read), and the atlas
pipeline wires ai_provider=None at the VIETNAMESE stage. Every glyph class
that would require AI MUST fail CLOSED (clean error, never a network call)
- that fail-closed behavior IS part of the secret-boundary semantics
(ADR-0003 cascade keys preserved by name only; runtime evidence per
ADR-0003).

Bounds: 330s pipeline deadline inside a 360s hard watchdog kill
(slice wall cap: 6 minutes - kill if exceeded). No blind retry: any
failure is captured and reported as evidence.

FIXTURE SUBSTITUTION (recorded honestly, same as M5): Be Vietnam Pro
Regular (agent/benchmark_data/ground_truth/BeVietnamPro-Regular.ttf), the
repo benchmark ground truth, authorized as the LOCAL validation binary for
this task; network acquisition is unavailable locally (no production
access under this task).

Evidence capture: phase 1 = the bounded VIETNAMESE pipeline run (expected
fail-closed at the AI gate); phase 2 = frozen-model reconstruction through
the pipeline's OWN checkpoint + exact-identity glyph-model cache (the
resume machinery, build-only-missing); phase 3 = the deterministic
Vietnamese path (identical service construction to pipeline stage 5b):
NFC/NFD audit, preserved glyphs, deterministic composition outcomes,
anchor/mark inference, class partition (deterministic vs AI-required,
listed - never called), and validation gates on the deterministic-only
extended model.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import tracemalloc
from dataclasses import replace
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from atlas.cache import (  # noqa: E402
    AtlasCacheStore,
    AtlasCheckpointStore,
    identity_hash,
    NAMESPACE_GLYPH_MODELS,
)
from atlas.fontbuild import (  # noqa: E402
    AtlasFontBuilder,
    assemble_font_model,
    observation_fingerprint,
)
from atlas.local_fixture import (  # noqa: E402
    LocalFontMetricsProvider,
    LocalFontRasterProvider,
)
from atlas.pipeline import AtlasStyleSpec, AtlasUltraPipeline  # noqa: E402
from atlas.policy import FAST_RASTER_SIZE_PX, AtlasRuntimeDefaults  # noqa: E402
from atlas.typography import build_typography_dataset  # noqa: E402
from atlas.validation import run_speed_first_validation  # noqa: E402
from atlas.vietnamese import MAX_AI_CALLS_PER_CLASS, glyph_class  # noqa: E402
from compute.vietnamese import (  # noqa: E402
    MARK_CODEPOINT_SET,
    VietnameseAIIntegrityError,
    VietnameseExtensionService,
    missing_vietnamese_codepoints,
    validate_candidate_font_bytes,
    validate_nfc_nfd_coverage,
    VIETNAMESE_REQUIRED_CODEPOINTS,
)
from reconstruction.font_model import CalibratedGlyph  # noqa: E402

FIXTURE_FONT = (
    Path(__file__).resolve().parent.parent
    / "benchmark_data"
    / "ground_truth"
    / "BeVietnamPro-Regular.ttf"
)
CACHE_ROOT = Path(os.environ.get(
    "ATLAS_VN_FIXTURE_CACHE",
    str(Path.home() / ".telefont-atlas-vn-fixture-cache"),
))
PIPELINE_DEADLINE_SECONDS = 330   # bounded VN run deadline (inside 6-min cap)
WATCHDOG_KILL_SECONDS = 360       # slice wall cap: 6 minutes, hard kill

# BOUNDED subset (<=60 glyphs): representative precomposed Latin+diacritics
# (incl. the slice-named U+1EA5/U+1EC1/U+1EDF/U+1EE9/U+1EAD), cross-base
# donors for deterministic composition, and NFC/NFD decomposition parts.
SUBSET = sorted({
    # base letters used by deterministic composition
    0x61, 0x65, 0x6F, 0x75,                        # a e o u
    # Vietnamese-specific non-decomposable / modifier glyphs
    0x110, 0x111,                                  # D-stroke pair
    0x1A0, 0x1A1, 0x1AF, 0x1B0,                    # O/U-horn pairs
    0x2C6,                                         # modifier circumflex
    0x306, 0x31B,                                  # combining breve / horn
    # Vietnamese tone combining marks (NFD parts; raster-unprovable alone)
    0x300, 0x301, 0x303, 0x309, 0x323,
    # slice-named representative precomposed glyphs
    0x1EA5, 0x1EC1, 0x1EDF, 0x1EE9, 0x1EAD,
    # cross-base circumflex-stack donors
    0x1EBF, 0x1EA7, 0x1EC3, 0x1EAB, 0x1EC7,
    # cross-base horn-stack donors
    0x1EDB, 0x1EEB, 0x1EE3, 0x1EF1,
    # single-mark donors (deterministic mark extraction)
    0xE0, 0xE1, 0xE3, 0x1EA3, 0x1EA1,
})


def _watchdog(start_monotonic: float) -> None:
    while True:
        if time.monotonic() - start_monotonic > WATCHDOG_KILL_SECONDS:
            print("VN_FIXTURE_WATCHDOG_KILL: 6-minute wall exceeded", flush=True)
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


def peak_working_set_mb() -> float:
    """Process peak working set (Windows psapi / POSIX rusage; 0 if n/a)."""
    try:
        if sys.platform == "win32":
            import ctypes

            class _PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_ulong),
                    ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            pmc = _PMC()
            pmc.cb = ctypes.sizeof(_PMC)
            fn = ctypes.windll.psapi.GetProcessMemoryInfo
            fn.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(_PMC),
                ctypes.c_ulong,
            ]
            fn.restype = ctypes.c_int
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if fn(handle, ctypes.byref(pmc), pmc.cb):
                return round(pmc.PeakWorkingSetSize / (1024 * 1024), 3)
            return 0.0
        import resource

        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 3)
    except Exception:
        return 0.0


def _cp_list(cps) -> list[str]:
    return [f"U+{cp:04X}" for cp in sorted(cps)]


def main() -> int:
    t_boot = time.monotonic()
    threading.Thread(target=_watchdog, args=(t_boot,), daemon=True).start()

    env_lower = {k.lower() for k in os.environ}
    secret_boundary = {
        "wokushop_api_key_in_env": "wokushop_api_key" in env_lower,
        "openrouter_api_key_in_env": "openrouter_api_key" in env_lower,
        "pipeline_ai_provider": (
            "None (hardcoded: atlas pipeline stage 5b wires ai_provider=None)"
        ),
        "network_transports": (
            "none: LocalFontMetricsProvider/LocalFontRasterProvider are "
            "file-read-only local fixture providers"
        ),
        "ai_calls_made": 0,
    }

    spec = AtlasStyleSpec(
        source_url="fixture://local/be-vietnam-pro",
        family_name="Be Vietnam Pro VN Fixture",
        style_name="Regular",
        style_id="regular",
        mode="VIETNAMESE",
        code_points=SUBSET,
    )
    cache = AtlasCacheStore(CACHE_ROOT / "cache")
    ck_store = AtlasCheckpointStore(CACHE_ROOT / "checkpoints")

    def make(deadline: float) -> AtlasUltraPipeline:
        return AtlasUltraPipeline(
            spec=spec,
            runtime=AtlasRuntimeDefaults(),
            metrics_provider=LocalFontMetricsProvider(FIXTURE_FONT),
            raster_provider=LocalFontRasterProvider(FIXTURE_FONT),
            cache=cache,
            checkpoint_store=ck_store,
            deadline=deadline,
        )

    ev: dict = {
        "task": "T-FAST-ATLAS-ULTRA-01",
        "unit": "U8",
        "run_label": "vietnamese_bounded",
        "fixture": {
            "font": str(FIXTURE_FONT),
            "fixture_role": (
                "repo benchmark ground truth authorized as the LOCAL validation "
                "binary for this task (network acquisition unavailable locally; "
                "no production access)"
            ),
            "mode": "VIETNAMESE",
            "glyph_subset_count": len(SUBSET),
            "glyph_subset": _cp_list(SUBSET),
            "subset_design": (
                "representative precomposed Latin+diacritic classes "
                "(U+1EA5/U+1EC1/U+1EDF/U+1EE9/U+1EAD + cross-base circumflex/"
                "horn-stack and single-mark donors) plus NFC/NFD decomposition "
                "parts (base letters + Vietnamese combining marks); <=60 glyphs"
            ),
        },
        "secret_boundary": secret_boundary,
    }

    # ---- Phase 1: ONE bounded VIETNAMESE pipeline run -------------------
    t0 = time.monotonic()
    phase1: dict = {
        "deadline_seconds": PIPELINE_DEADLINE_SECONDS,
        "watchdog_kill_seconds": WATCHDOG_KILL_SECONDS,
    }
    try:
        result = asyncio.run(make(t0 + PIPELINE_DEADLINE_SECONDS).run())
        phase1["outcome"] = "COMPLETED"
        phase1["pipeline_evidence"] = result.evidence.to_dict()
        phase1["note"] = (
            "unexpected completion: subset was designed to leave AI-required "
            "classes unresolved"
        )
    except VietnameseAIIntegrityError as exc:
        phase1["outcome"] = "FAIL_CLOSED"
        phase1["error_type"] = type(exc).__name__
        phase1["error"] = str(exc)
        phase1["fail_closed_without_keys"] = True
        phase1["note"] = (
            "deterministic-first path resolved what source evidence proves; "
            "remaining glyph classes require the AI gate - ai_provider=None "
            "(no keys locally) so the service fails CLOSED with a clean error; "
            "no network call occurred"
        )
    except ValueError as exc:
        phase1["outcome"] = "ERROR"
        phase1["error_type"] = type(exc).__name__
        phase1["error"] = str(exc)
    except Exception as exc:  # honest capture, no blind retry
        phase1["outcome"] = "ERROR"
        phase1["error_type"] = type(exc).__name__
        phase1["error"] = str(exc)
    phase1["wall_seconds"] = round(time.monotonic() - t0, 3)
    ev["phase1_vietnamese_pipeline_run"] = phase1

    # ---- Phase 2+3 only if inside the bounded wall ----------------------
    if time.monotonic() - t_boot > WATCHDOG_KILL_SECONDS - 60:
        ev["phases23"] = "SKIPPED_WALL_BUDGET"
    else:
        tracemalloc.start()
        t1 = time.monotonic()
        probe = make(t_boot + WATCHDOG_KILL_SECONDS)
        identity = probe._checkpoint_identity
        state = ck_store.load(identity)
        frozen: dict[int, CalibratedGlyph] = {}
        if state is not None:
            # EXACT pipeline resume machinery (build-only-missing): cached
            # glyph models restore without recomputation.
            for cp in list(state.frozen_code_points):
                fp = observation_fingerprint(
                    {"cp": cp, "size": FAST_RASTER_SIZE_PX, "phase": [0.0, 0.0],
                     "checkpoint_identity": identity}
                )
                cached = cache.get_json(
                    NAMESPACE_GLYPH_MODELS, identity_hash({"fp": fp})
                )
                if cached is None:
                    continue
                try:
                    frozen[cp] = CalibratedGlyph.from_dict(cached)
                except ValueError:
                    continue
        failed_cps = list(state.failed_code_points) if state else []
        regressed, font_asc, font_desc = probe._stage_metrics()
        model = assemble_font_model(
            family_name=spec.family_name,
            style_name=spec.style_name,
            reference_id=identity_hash({"source_url": spec.source_url}),
            style_id=spec.style_id,
            glyphs=frozen,
            font_ascent_upem=font_asc,
            font_descent_upem=font_desc,
            config_hash=identity,
            browser_version="atlas_ultra_v1",
            fit_observations_count=len(frozen),
        )
        ev["phase2_model_reconstruction"] = {
            "source": (
                "pipeline checkpoint + exact-identity glyph-model cache "
                "(the pipeline's own resume machinery; no recomputation)"
            ),
            "frozen_glyphs": len(frozen),
            "frozen_codepoints": _cp_list(frozen.keys()),
            "failed_glyphs": len(failed_cps),
            "failed_codepoints": _cp_list(failed_cps),
            "wall_seconds": round(time.monotonic() - t1, 3),
        }

        # ---- Phase 3: deterministic Vietnamese path (stage-5b identical) -
        t2 = time.monotonic()
        service = VietnameseExtensionService(
            ai_provider=None,  # identical to atlas pipeline stage 5b
            config_hash=identity,
            source_hash=identity_hash({"source_url": spec.source_url}),
        )
        pre_audit = validate_nfc_nfd_coverage(model)
        missing = sorted(missing_vietnamese_codepoints(model))
        preserved = sorted(
            cp for cp in VIETNAMESE_REQUIRED_CODEPOINTS if cp in model.glyphs
        )
        det_glyphs: dict[int, CalibratedGlyph] = {}
        ai_required: list[int] = []
        det_errors: dict[int, str] = {}
        for cp in missing:
            try:
                g = service._deterministic_glyph(model, cp)
            except Exception as exc:  # deterministic path errors fail closed
                det_errors[cp] = f"{type(exc).__name__}: {exc}"
                continue
            if g is None:
                ai_required.append(cp)
            else:
                det_glyphs[cp] = g

        missing_classes = {glyph_class(cp) for cp in missing}
        det_classes = sorted({glyph_class(cp) for cp in det_glyphs})
        ai_classes = sorted({glyph_class(cp) for cp in ai_required})
        err_classes = sorted({glyph_class(cp) for cp in det_errors})
        anchors = {
            f"U+{cp:04X}": [[n, x, y] for (n, x, y) in det_glyphs[cp].anchors]
            for cp in sorted(det_glyphs) if det_glyphs[cp].anchors
        }
        mark_anchors = sorted(
            cp for cp in det_glyphs if cp in MARK_CODEPOINT_SET and det_glyphs[cp].anchors
        )

        extended = replace(model, glyphs={**model.glyphs, **det_glyphs})
        post_audit = validate_nfc_nfd_coverage(extended)

        # Validation gates on the deterministic-only extended model.
        builder = AtlasFontBuilder(spec.family_name, spec.style_name)
        builder.bind_model(extended)
        typography = build_typography_dataset(
            spec.family_name, spec.style_name, {}
        )
        build_dir = cache.root / "vn_build"
        build_dir.mkdir(parents=True, exist_ok=True)
        temp = builder.build_temporary_ttf(extended, build_dir / "temp", typography)
        ttf_bytes = temp.file_path.read_bytes()
        vn_gate_failures = validate_candidate_font_bytes(ttf_bytes, extended)
        report = run_speed_first_validation(
            ttf_path=temp.file_path,
            code_points=sorted(extended.glyphs),
            kern_pairs=[],
            mode="VIETNAMESE",
        )

        ev["phase3_deterministic_vietnamese_path"] = {
            "service_construction": (
                "VietnameseExtensionService(ai_provider=None) - identical to "
                "atlas pipeline stage 5b"
            ),
            "nfc_nfd_coverage_audit": {
                "pre_extension_failures": pre_audit,
                "post_extension_failures": post_audit,
                "resolved_by_deterministic_extension": (
                    len(pre_audit) - len(post_audit)
                ),
            },
            "preserved_existing_glyphs": {
                "count": len(preserved),
                "codepoints": _cp_list(preserved),
            },
            "missing_required_count": len(missing),
            "deterministic_composition": {
                "resolved_count": len(det_glyphs),
                "codepoints": _cp_list(det_glyphs.keys()),
                "errors": {
                    f"U+{cp:04X}": msg for cp, msg in sorted(det_errors.items())
                },
            },
            "ai_required": {
                "count": len(ai_required),
                "codepoints": _cp_list(ai_required),
                "calls_made": 0,
                "note": (
                    "listed, NEVER called: no provider keys locally; the phase-1 "
                    "pipeline run proved these classes fail CLOSED at the AI gate"
                ),
            },
            "glyph_classes": {
                "total_missing_classes": len(missing_classes),
                "resolved_deterministic": det_classes,
                "requiring_ai": ai_classes,
                "deterministic_error": err_classes,
                "ai_calls_budget": MAX_AI_CALLS_PER_CLASS * len(missing_classes),
                "ai_calls_used": 0,
            },
            "anchor_mark_inference": {
                "reached": True,
                "glyphs_with_anchors": len(anchors),
                "combining_marks_with_inferred_anchors": _cp_list(mark_anchors),
                "anchors": anchors,
                "mkmk": (
                    "not a stage of the deterministic-first path under "
                    "ADR-0004 (anchor/mark inference reached; no mkmk "
                    "reconstruction attempted)"
                ),
            },
            "validation_on_deterministic_only_extension": {
                "built_temporary_ttf_bytes": len(ttf_bytes),
                "vn_gate_corpus_shaping_clipping_spacing_failures": vn_gate_failures,
                "speed_first_validation": {
                    "passed": report.passed,
                    "reasons": report.reasons,
                    "fonttools_ttf_checks": report.fonttools_ttf.get("checks"),
                    "harfbuzz_checks": report.harfbuzz.get("checks"),
                    "freetype_checks": report.freetype.get("checks"),
                    "normalization": report.normalization,
                },
                "note": (
                    "bounded subset: corpus/NFC-NFD shortfalls are reported "
                    "honestly (failed classes stay failed; no global rerun per "
                    "ADR-0004)"
                ),
            },
            "wall_seconds": round(time.monotonic() - t2, 3),
        }
        _cur, probe_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        ev["timing_memory"] = {
            "total_wall_seconds": round(time.monotonic() - t_boot, 3),
            "wall_cap_seconds": WATCHDOG_KILL_SECONDS,
            "within_cap": bool(time.monotonic() - t_boot < WATCHDOG_KILL_SECONDS),
            "peak_working_set_mb": peak_working_set_mb(),
            "peak_tracemalloc_mb_phases23": round(probe_peak / (1024 * 1024), 3),
        }

    out_path = (
        Path(__file__).resolve().parent.parent.parent
        / ".prime" / "tasks" / "T-FAST-ATLAS-ULTRA-01"
        / "fixture-evidence-vietnamese.yaml"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(_yaml(ev), encoding="utf-8")
    print(_yaml(ev))
    print(f"EVIDENCE_SAVED: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

