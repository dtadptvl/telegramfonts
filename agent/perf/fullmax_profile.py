"""Issue #82 bounded FULL MAX profiling harness (LOCAL_ONLY, NO_LOOP).

Runs the canonical FULL MAX E2E fixture chains (same fixture mechanism and
production code path as test_issue75_fullmax_e2e.py) with deterministic
stage-boundary instrumentation and emits a JSON profiling artifact:

- per-stage wall time and call counts (snapshot/partition/reconstruction/
  optimizer internals/calibration/model seal/candidate build/four consumers/
  held-out evaluation);
- per-gate truth identities (model hash, artifact SHA, trace/report/
  attestation hashes) so SAME-MAX-TRUTH is provable across HEADs;
- process peak working set + environment identity.

Usage:
    python perf/fullmax_profile.py --label baseline --out perf/reports/baseline.json
    python perf/fullmax_profile.py --label after --out perf/reports/after.json

The harness never mutates production code; instrumentation is applied by
wrapping module attributes for the duration of the run only.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import gc
import json
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR / "src"))
sys.path.insert(0, str(AGENT_DIR))

from fidelity import evaluator as fidelity_evaluator  # noqa: E402
from fidelity import optimizer as fidelity_optimizer  # noqa: E402
from fidelity import pipeline as fidelity_pipeline  # noqa: E402
from fidelity import producers as fidelity_producers  # noqa: E402
from fidelity.release_gate import PROVENANCE_STAGE9D_RASTER, Stage9DReleaseGate  # noqa: E402
from measurement import calibration as measurement_calibration  # noqa: E402
from measurement import collector as measurement_collector  # noqa: E402
from reconstruction import bezier_fitter as reconstruction_bezier  # noqa: E402
from reconstruction import candidate_builder as reconstruction_candidate_builder  # noqa: E402
from reconstruction import font_model as reconstruction_font_model  # noqa: E402
from reconstruction import sdf as reconstruction_sdf  # noqa: E402
from reconstruction import solver as reconstruction_solver  # noqa: E402
from reconstruction import topology as reconstruction_topology  # noqa: E402
from tests.test_issue75_fullmax_e2e import (  # noqa: E402
    _E2EFixtureSession,
    _E2E_ORIGINAL_COVERAGE,
    _E2E_VI_COVERAGE,
    _collect_family,
)

TIMERS: dict[str, dict[str, float]] = {}
VERBOSE = False
_RUN_START = 0.0


def _progress(msg: str) -> None:
    if VERBOSE:
        import sys as _sys
        print(f'[+{(time.perf_counter() - _RUN_START):8.1f}s] {msg}', flush=True)


def _record(label: str, dt: float, extra: dict[str, float] | None = None) -> None:
    entry = TIMERS.setdefault(label, {"calls": 0.0, "time_ms": 0.0})
    entry["calls"] += 1
    entry["time_ms"] += dt * 1000.0
    if extra:
        for k, v in extra.items():
            entry[k] = entry.get(k, 0.0) + v


class _Instrumentation:
    """Scoped instrumentation bundle; restores every wrapped attribute."""

    def __init__(self) -> None:
        self._restore: list[tuple[object, str, object]] = []
        self._async_restore: list[tuple[object, str, object]] = []

    def _resolve(self, obj, attr: str):
        raw = vars(obj).get(attr) if isinstance(obj, type) else getattr(obj, attr)
        if isinstance(raw, staticmethod):
            return raw.__func__, 'static'
        if isinstance(raw, classmethod):
            return raw.__func__, 'class'
        return raw, 'plain'

    def _install(self, obj, attr: str, wrapper, kind: str, raw) -> None:
        if kind == 'static':
            setattr(obj, attr, staticmethod(wrapper))
        elif kind == 'class':
            setattr(obj, attr, classmethod(wrapper))
        else:
            setattr(obj, attr, wrapper)

    def wrap(self, obj, attr: str, label: str | None = None, count_bytes: bool = False, progress: bool = False) -> None:
        raw = vars(obj).get(attr) if isinstance(obj, type) else getattr(obj, attr)
        fn, kind = self._resolve(obj, attr)
        self._restore.append((obj, attr, raw))
        target_label = label or attr

        def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            result = fn(*args, **kwargs)
            dt = time.perf_counter() - t0
            extra = {'bytes': float(len(result))} if (count_bytes and isinstance(result, bytes)) else None
            _record(target_label, dt, extra)
            if progress:
                detail = ''
                if result is not None and hasattr(result, 'code_point'):
                    detail = f' cp={result.code_point:04X}'
                elif isinstance(result, tuple) and result and hasattr(result[0], 'code_point'):
                    detail = f' cp={result[0].code_point:04X}'
                _progress(f'{target_label} done ({dt:.2f}s){detail}')
            return result

        self._install(obj, attr, wrapper, kind, raw)

    def wrap_async(self, obj, attr: str, label: str | None = None) -> None:
        raw = vars(obj).get(attr) if isinstance(obj, type) else getattr(obj, attr)
        fn, kind = self._resolve(obj, attr)
        self._async_restore.append((obj, attr, raw))
        target_label = label or attr

        async def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            result = await fn(*args, **kwargs)
            _record(target_label, time.perf_counter() - t0)
            return result

        self._install(obj, attr, wrapper, kind, raw)

    def restore(self) -> None:
        for obj, attr, original in reversed(self._restore):
            setattr(obj, attr, original)
        for obj, attr, original in reversed(self._async_restore):
            setattr(obj, attr, original)
        self._restore.clear()
        self._async_restore.clear()


def _peak_working_set_mb() -> float:
    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
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

    counters = PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
    handle = ctypes.windll.kernel32.GetCurrentProcess()
    ok = ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
    if not ok:
        return -1.0
    return counters.PeakWorkingSetSize / (1024.0 * 1024.0)


def _git_identity() -> dict[str, str]:
    def run(*args):
        try:
            return subprocess.run(
                ["git", *args], capture_output=True, text=True, cwd=AGENT_DIR, timeout=30
            ).stdout.strip()
        except Exception:
            return "unknown"

    return {
        "git_sha": run("rev-parse", "HEAD"),
        "git_branch": run("rev-parse", "--abbrev-ref", "HEAD"),
        "git_dirty": run("status", "--porcelain"),
    }


def _gate_truth(res) -> dict[str, str]:
    truth = {
        "status": res.status,
        "is_publishable": res.is_publishable,
        "model_hash": res.model_hash,
        "candidate_artifact_sha": res.candidate_artifact_sha,
        "candidate_size_bytes": res.candidate_size_bytes,
        "snapshot_fingerprint": res.snapshot_fingerprint,
        "fit_set_fingerprint": res.fit_set_fingerprint,
        "held_out_set_fingerprint": res.held_out_set_fingerprint,
        "report_hash": res.report_hash,
    }
    if res.trace is not None:
        truth["trace_hash"] = res.trace.compute_trace_hash()
        truth["total_iterations"] = res.trace.total_iterations
    if res.attestation is not None:
        truth["attestation_hash"] = res.attestation.compute_hash()
        truth["consumer_bundle_hash"] = res.attestation.consumer_bundle_hash
        truth["provenance"] = res.attestation.provenance
        truth["ai_binding"] = res.attestation.ai_binding
    return truth


def _install_instrumentation(inst: _Instrumentation) -> None:
    # Snapshot / partition.
    inst.wrap(fidelity_pipeline.ObservationStoreSnapshot, "load_from_store", "snapshot.load_from_store")
    inst.wrap(fidelity_pipeline, "partition_snapshot", "partition_snapshot")
    inst.wrap(fidelity_pipeline.ObservationStoreSnapshot, "get_raster_bytes", "snapshot.get_raster_bytes", count_bytes=True)
    # Reconstruction.
    inst.wrap(reconstruction_solver.MaxReconstructionSolver, "reconstruct_glyph", "solver.reconstruct_glyph")
    inst.wrap(reconstruction_sdf, "fuse_observation_sdfs", "solver.fuse_observation_sdfs")
    inst.wrap(reconstruction_topology, "extract_zero_crossing_contours", "solver.extract_zero_crossings")
    inst.wrap(reconstruction_topology, "build_topology_hierarchy", "solver.build_topology")
    inst.wrap(reconstruction_bezier.SchneiderFitter, "fit_contour", "solver.schneider_fit_contour")
    # Optimizer internals.
    inst.wrap(fidelity_optimizer.FitOnlyGlyphOptimizer, "optimize", "optimizer.optimize")
    inst.wrap(fidelity_optimizer.FitOnlyGlyphOptimizer, "optimize_glyph", "optimizer.optimize_glyph")
    inst.wrap(fidelity_optimizer.FitOnlyGlyphOptimizer, "_decode_mask", "optimizer.decode_mask")
    inst.wrap(fidelity_optimizer.FitOnlyGlyphOptimizer, "_prepare_reference_artifacts", "optimizer.prepare_reference_artifacts")
    inst.wrap(fidelity_optimizer.FitOnlyGlyphOptimizer, "_signed_distance", "optimizer.signed_distance")
    inst.wrap(fidelity_optimizer.FitOnlyGlyphOptimizer, "_rasterize_model_crop", "optimizer.rasterize_model_crop")
    inst.wrap(fidelity_optimizer.FitOnlyGlyphOptimizer, "_rasterize_contours", "optimizer.rasterize_contours")
    inst.wrap(fidelity_optimizer.FitOnlyGlyphOptimizer, "_boundary", "optimizer.boundary")
    inst.wrap(fidelity_optimizer.FitOnlyGlyphOptimizer, "_loss_components", "optimizer.loss_components")
    inst.wrap(fidelity_optimizer.FitOnlyGlyphOptimizer, "_curvature_loss", "optimizer.curvature_loss")
    inst.wrap(fidelity_optimizer.FitOnlyGlyphOptimizer, "_complexity_loss", "optimizer.complexity_loss")
    inst.wrap(fidelity_optimizer, "_transform_contours", "optimizer.transform_contours")
    inst.wrap(fidelity_optimizer, "_simplify_contours", "optimizer.simplify_contours")
    inst.wrap(fidelity_optimizer, "_subdivide_contours", "optimizer.subdivide_contours")
    # Calibration / model assembly.
    inst.wrap(measurement_calibration.ObservationCalibrator, "calibrate_all", "calibration.calibrate_all")
    inst.wrap(measurement_calibration.ObservationCalibrator, "compute_calibration_fingerprint", "calibration.fingerprint")
    inst.wrap(measurement_calibration, "derive_multisize_derived_metrics", "calibration.derive_multisize_metrics")
    inst.wrap(measurement_collector, "derive_multisize_kerning", "calibration.derive_multisize_kerning")
    inst.wrap(measurement_collector, "validate_pair_size_schedule", "calibration.validate_pair_schedule")
    inst.wrap(reconstruction_font_model.CanonicalFontModel, "seal", "model.seal")
    inst.wrap(reconstruction_font_model.CanonicalFontModel, "compute_canonical_hash", "model.compute_canonical_hash")
    # Candidate build.
    inst.wrap(reconstruction_candidate_builder.MaxCandidateFontBuilder, "build_candidate_family", "build.candidate_family")
    # Consumer evidence + evaluation.
    inst.wrap_async(fidelity_producers.ProductionConsumerEvidenceProducer, "produce_bundle", "consumers.produce_bundle")
    inst.wrap(fidelity_producers.FontToolsEvidenceProducer, "produce", "consumers.fonttools")
    inst.wrap(fidelity_producers.FreeTypeEvidenceProducer, "produce", "consumers.freetype")
    inst.wrap(fidelity_producers.HarfBuzzEvidenceProducer, "produce", "consumers.harfbuzz")
    inst.wrap_async(fidelity_producers.ChromiumEvidenceProducer, "produce", "consumers.chromium")
    inst.wrap(fidelity_evaluator.FidelityEvaluator, "evaluate", "evaluator.evaluate")


async def _run_original_chain(base_dir: Path, store, config, bv: str) -> list[dict]:
    gate_kwargs = dict(
        store=store,
        config=config,
        reference_id="e2e_fam",
        style_id="regular",
        family_name="E2EFam",
        style_name="Regular",
        browser_version=bv,
    )
    gates: list[dict] = []

    t0 = time.perf_counter()
    res_ttf = await Stage9DReleaseGate.execute(format_type="TTF", output_dir=base_dir / "out_ttf", **gate_kwargs)
    gates.append({"chain": "ORIGINAL", "format": "TTF", "wall_ms": (time.perf_counter() - t0) * 1000.0, **_gate_truth(res_ttf)})
    assert res_ttf.is_publishable, res_ttf.failure_reasons

    t0 = time.perf_counter()
    res_otf = await Stage9DReleaseGate.execute(format_type="OTF", output_dir=base_dir / "out_otf", **gate_kwargs)
    gates.append({"chain": "ORIGINAL", "format": "OTF", "wall_ms": (time.perf_counter() - t0) * 1000.0, **_gate_truth(res_otf)})
    assert res_otf.is_publishable, res_otf.failure_reasons

    t0 = time.perf_counter()
    res_l2 = await Stage9DReleaseGate.execute_with_model(
        format_type="TTF",
        output_dir=base_dir / "out_l2",
        model=res_ttf.model,
        cached_snapshot_fingerprint=res_ttf.snapshot_fingerprint,
        cached_trace_hash=res_ttf.attestation.optimizer_trace_hash,
        cached_provenance=PROVENANCE_STAGE9D_RASTER,
        **gate_kwargs,
    )
    gates.append({"chain": "ORIGINAL", "format": "L2_TTF", "wall_ms": (time.perf_counter() - t0) * 1000.0, **_gate_truth(res_l2)})
    assert res_l2.is_publishable, res_l2.failure_reasons

    t0 = time.perf_counter()
    res_repeat = await Stage9DReleaseGate.execute(format_type="TTF", output_dir=base_dir / "out_ttf_repeat", **gate_kwargs)
    gates.append({"chain": "ORIGINAL", "format": "TTF_REPEAT", "wall_ms": (time.perf_counter() - t0) * 1000.0, **_gate_truth(res_repeat)})
    assert res_repeat.is_publishable, res_repeat.failure_reasons

    # Truth invariants of the canonical chain (mirror E2E assertions).
    assert res_ttf.model_hash == res_otf.model_hash
    assert res_l2.candidate_artifact_sha == res_ttf.candidate_artifact_sha
    assert res_repeat.candidate_artifact_sha == res_ttf.candidate_artifact_sha
    assert res_repeat.trace.compute_trace_hash() == res_ttf.trace.compute_trace_hash()

    for res in (res_ttf, res_otf, res_l2, res_repeat):
        res.cleanup()
    return gates


async def _run_vi_chain(base_dir: Path, store, config, bv: str) -> list[dict]:
    import hashlib

    from compute.vietnamese import MARK_CODEPOINT_SET, VietnameseExtensionService
    from tests.test_issue75_fullmax_e2e import _E2E_VI_COVERAGE as _existing

    class _E2EAIProvider:
        model_id = "openrouter"
        model_version = "openrouter-route-v1"

        def __init__(self):
            self.calls = 0
            self.requested: list[int] = []

        def prompt_hash(self) -> str:
            return hashlib.sha256(b"e2e_prompt").hexdigest()

        async def generate_candidates(self, request):
            from compute.vietnamese import AICandidateSpec

            self.calls += 1
            self.requested = list(request["missing_codepoints"])
            specs = []
            for cp in self.requested:
                anchors = (("mark", 250.0, 320.0),) if cp in MARK_CODEPOINT_SET else ()
                specs.append(
                    AICandidateSpec(
                        code_point=cp,
                        contours=(((175.0, 100.0), (425.0, 100.0), (425.0, 340.0), (175.0, 340.0)),),
                        advance_width_upem=1.0 if cp in MARK_CODEPOINT_SET else 600.0,
                        lsb_upem=175.0,
                        rsb_upem=175.0,
                        ascent_upem=340.0,
                        descent_upem=-100.0,
                        anchors=anchors,
                    )
                )
            return specs

    provider = _E2EAIProvider()
    service = VietnameseExtensionService(provider, config_hash=config.compute_hash(), source_hash="e" * 64)

    t0 = time.perf_counter()
    res_vi = await Stage9DReleaseGate.execute(
        store=store,
        config=config,
        reference_id="e2e_vi_fam",
        style_id="regular",
        family_name="E2EViFam",
        style_name="Regular",
        browser_version=bv,
        format_type="TTF",
        output_dir=base_dir / "out_vi",
        mode="VIETNAMESE",
        vietnamese_service=service,
    )
    wall = (time.perf_counter() - t0) * 1000.0
    assert res_vi.is_publishable, res_vi.failure_reasons
    assert provider.calls == 1
    truth = _gate_truth(res_vi)
    truth["ai_calls"] = provider.calls
    truth["glyph_count"] = len(res_vi.model.glyphs)
    res_vi.cleanup()
    return [{"chain": "VIETNAMESE", "format": "TTF", "wall_ms": wall, **truth}]


async def _collect_with_timing(base_dir: Path, reference_id: str, family_name: str, session, coverage) -> tuple:
    t0 = time.perf_counter()
    store, config, bv = await _collect_family(base_dir, reference_id, family_name, session, coverage)
    _record("collection.total", time.perf_counter() - t0)
    return store, config, bv


def _flush_report(report: dict, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    report["stages"] = {
        label: {k: (round(v, 3) if isinstance(v, float) else v) for k, v in entry.items()}
        for label, entry in sorted(TIMERS.items())
    }
    out_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")


def main() -> int:
    global VERBOSE, _RUN_START
    parser = argparse.ArgumentParser(description="Issue #82 bounded FULL MAX profiling harness")
    parser.add_argument("--label", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--segment", choices=["all", "original", "vi"], default="all")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    VERBOSE = args.verbose

    TIMERS.clear()
    gc.collect()
    _RUN_START = time.perf_counter()

    inst = _Instrumentation()
    _install_instrumentation(inst)

    report: dict = {
        "label": args.label,
        "segment": args.segment,
        "issue": 82,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": __import__("os").cpu_count(),
        **_git_identity(),
    }
    out_path = Path(args.out)

    started = time.perf_counter()
    gates: list[dict] = []
    try:
        with tempfile.TemporaryDirectory(prefix="issue82_profile_") as tmp:
            base_dir = Path(tmp)

            async def drive():
                if args.segment in ("all", "original"):
                    _progress("collect ORIGINAL fixture")
                    session_o = _E2EFixtureSession(_E2E_ORIGINAL_COVERAGE, "chromium_fullmax_e2e_v1")
                    store_o, config_o, bv_o = await _collect_with_timing(
                        base_dir, "e2e_fam", "E2EFam", session_o, _E2E_ORIGINAL_COVERAGE
                    )
                    _progress("run ORIGINAL chain (TTF/OTF/L2/repeat)")
                    gates.extend(await _run_original_chain(base_dir / "orig", store_o, config_o, bv_o))
                    report["gates"] = gates
                    report["total_wall_ms"] = (time.perf_counter() - started) * 1000.0
                    _flush_report(report, out_path)
                if args.segment in ("all", "vi"):
                    _progress("collect VIETNAMESE fixture")
                    session_v = _E2EFixtureSession(_E2E_VI_COVERAGE, "chromium_fullmax_e2e_v2")
                    store_v, config_v, bv_v = await _collect_with_timing(
                        base_dir, "e2e_vi_fam", "E2EViFam", session_v, _E2E_VI_COVERAGE
                    )
                    _progress("run VIETNAMESE chain (TTF)")
                    gates.extend(await _run_vi_chain(base_dir / "vi", store_v, config_v, bv_v))
                    report["gates"] = gates
                    report["total_wall_ms"] = (time.perf_counter() - started) * 1000.0
                    _flush_report(report, out_path)

            asyncio.run(drive())
    finally:
        inst.restore()

    report["total_wall_ms"] = (time.perf_counter() - started) * 1000.0
    report["peak_working_set_mb"] = _peak_working_set_mb()
    report["gates"] = gates
    report["coverage_original"] = list(_E2E_ORIGINAL_COVERAGE)
    report["coverage_vietnamese"] = list(_E2E_VI_COVERAGE)
    _flush_report(report, out_path)

    ordered = sorted(TIMERS.items(), key=lambda kv: -kv[1]["time_ms"])
    print(f"== FULL MAX profile [{args.label}/{args.segment}] HEAD={report['git_sha'][:12]} total={report['total_wall_ms']:.0f}ms peak_rss={report['peak_working_set_mb']:.0f}MB ==")
    for label, entry in ordered[:25]:
        print(f"  {label:48s} {entry['time_ms']:12.1f} ms  calls={int(entry['calls'])}")
    for g in gates:
        print(f"  GATE {g['chain']:10s} {g['format']:11s} wall={g['wall_ms']:10.1f} ms publishable={g['is_publishable']} sha={g['candidate_artifact_sha'][:16]}")
    print(f"artifact: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


