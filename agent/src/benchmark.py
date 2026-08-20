"""A23 Compute Pipeline Capacity Benchmark Harness.

Measures local compute performance across font reconstruction (TTF, OTF, WOFF2),
ZIP packaging, and calculates conservative consumer dimensioning for 500 & 1000 jobs/day.

Usage:
    python agent/src/benchmark.py [--samples 10] [--styles 2] [--json-out report.json]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from PIL import Image, ImageDraw

# Add agent/src to path if run standalone
sys.path.insert(0, str(Path(__file__).parent))

from compute.font_builder import FontBuilderService
from compute.models import ClaimStyle, GeneratedFontFile, StagedManifest
from compute.packager import PackagerService
from compute.source import SourceAcquirer


def get_git_sha() -> str:
    """Resolve current git commit SHA if available."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return os.environ.get("GIT_SHA", "unknown")


def get_peak_rss_mb() -> float:
    """Get peak resident set size (RSS) memory in megabytes."""
    try:
        if sys.platform != "win32":
            import resource
            # On Linux/Darwin ru_maxrss is in kilobytes
            kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            if sys.platform == "darwin":
                return kb / (1024 * 1024)
            return kb / 1024
        else:
            import ctypes
            from ctypes import wintypes

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
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
            if ctypes.windll.psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb
            ):
                return counters.PeakWorkingSetSize / (1024 * 1024)
    except Exception:
        pass
    return 0.0


def make_representative_preview_bytes() -> bytes:
    """Generate a representative raster preview PNG for benchmarking glyph contour extraction."""
    import io
    img = Image.new("L", (800, 200), color=255)
    draw = ImageDraw.Draw(img)
    # Draw dark glyph shapes to benchmark polygon contour extraction
    draw.rectangle([50, 40, 150, 160], fill=0)
    draw.rectangle([200, 40, 300, 160], fill=0)
    draw.ellipse([350, 40, 450, 160], fill=0)
    draw.polygon([(500, 160), (550, 40), (600, 160)], fill=0)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@dataclass
class IterationResult:
    acquire_time_ms: float
    build_time_ms: float
    package_time_ms: float
    total_time_ms: float
    artifact_size_bytes: int
    success: bool
    error: str | None = None


@dataclass
class BenchmarkReport:
    git_sha: str
    timestamp: str
    is_production_proof: bool
    platform_info: dict[str, Any]
    config: dict[str, Any]
    samples_count: int
    success_count: int
    failure_count: int
    p50_total_ms: float
    p95_total_ms: float
    min_total_ms: float
    max_total_ms: float
    mean_total_ms: float
    p95_stage_ms: dict[str, float]
    avg_artifact_size_bytes: int
    peak_rss_mb: float
    capacity_model: dict[str, Any]
    disclaimer: str


def calculate_percentile(data: list[float], percentile: float) -> float:
    if not data:
        return 0.0
    sorted_data = sorted(data)
    idx = (percentile / 100.0) * (len(sorted_data) - 1)
    low = int(math.floor(idx))
    high = int(math.ceil(idx))
    weight = idx - low
    return sorted_data[low] * (1.0 - weight) + sorted_data[high] * weight


async def run_single_iteration(
    source_acquirer: SourceAcquirer,
    font_builder: FontBuilderService,
    packager: PackagerService,
    preview_bytes: bytes,
    claim_styles: list[ClaimStyle],
    formats: list[str],
    scratch_dir: Path,
) -> IterationResult:
    job_id = f"bench_{int(time.time() * 1000)}_{os.urandom(4).hex()}"
    job_scratch = scratch_dir / job_id
    build_dir = job_scratch / "build"
    dist_dir = job_scratch / "dist"
    build_dir.mkdir(parents=True, exist_ok=True)
    dist_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()

    try:
        # 1. Acquire & extract vector outlines
        t_acq_start = time.perf_counter()
        source_payload = await source_acquirer.acquire_source(
            source_url="https://www.myfonts.com/fonts/foundry/benchmark-sans/",
            styles=claim_styles,
            preview_input=preview_bytes,
        )
        t_acq_ms = (time.perf_counter() - t_acq_start) * 1000

        # 2. Build font formats (TTF, OTF, WOFF2)
        t_bld_start = time.perf_counter()
        built_files: list[GeneratedFontFile] = []
        for style_id, style_data in source_payload.styles.items():
            for fmt in formats:
                font_file = font_builder.build_font(
                    style_source=style_data,
                    family_name=source_payload.family_name,
                    format_type=fmt,
                    output_dir=build_dir,
                )
                built_files.append(font_file)
        t_bld_ms = (time.perf_counter() - t_bld_start) * 1000

        # 3. Package
        t_pkg_start = time.perf_counter()
        staged_manifest = packager.package_job_output(
            job_id=job_id,
            order_id="ord_benchmark",
            family_name=source_payload.family_name,
            files=built_files,
            output_dir=dist_dir,
        )
        t_pkg_ms = (time.perf_counter() - t_pkg_start) * 1000
        t_tot_ms = (time.perf_counter() - t0) * 1000

        artifact_size = staged_manifest.zip_size_bytes
        return IterationResult(
            acquire_time_ms=t_acq_ms,
            build_time_ms=t_bld_ms,
            package_time_ms=t_pkg_ms,
            total_time_ms=t_tot_ms,
            artifact_size_bytes=artifact_size,
            success=True,
        )
    except Exception as exc:
        t_tot_ms = (time.perf_counter() - t0) * 1000
        return IterationResult(
            acquire_time_ms=0.0,
            build_time_ms=0.0,
            package_time_ms=0.0,
            total_time_ms=t_tot_ms,
            artifact_size_bytes=0,
            success=False,
            error=str(exc),
        )
    finally:
        # Cleanup
        try:
            for p in list(job_scratch.rglob("*")):
                if p.is_file():
                    p.unlink(missing_ok=True)
            for p in sorted(job_scratch.rglob("*"), reverse=True):
                if p.is_dir():
                    p.rmdir()
            job_scratch.rmdir()
        except Exception:
            pass


async def run_benchmark(
    sample_count: int = 10,
    style_count: int = 2,
    formats: list[str] | None = None,
) -> BenchmarkReport:
    if formats is None:
        formats = ["TTF", "OTF", "WOFF2"]

    preview_bytes = make_representative_preview_bytes()
    style_names = ["Regular", "Bold", "Italic", "Light"]
    claim_styles = [
        ClaimStyle(id=f"style_{i+1}", display_name=style_names[i])
        for i in range(min(style_count, len(style_names)))
    ]

    source_acquirer = SourceAcquirer()
    font_builder = FontBuilderService()
    packager = PackagerService()

    with tempfile.TemporaryDirectory(prefix="telefont_bench_") as temp_dir:
        scratch_dir = Path(temp_dir)
        results: list[IterationResult] = []

        # Warmup iteration
        await run_single_iteration(
            source_acquirer, font_builder, packager, preview_bytes, claim_styles, formats, scratch_dir
        )

        for _ in range(sample_count):
            res = await run_single_iteration(
                source_acquirer, font_builder, packager, preview_bytes, claim_styles, formats, scratch_dir
            )
            results.append(res)

    successful = [r for r in results if r.success]
    success_count = len(successful)
    failure_count = len(results) - success_count

    total_times = [r.total_time_ms for r in successful]
    acq_times = [r.acquire_time_ms for r in successful]
    bld_times = [r.build_time_ms for r in successful]
    pkg_times = [r.package_time_ms for r in successful]
    sizes = [r.artifact_size_bytes for r in successful]

    p50_total = calculate_percentile(total_times, 50)
    p95_total = calculate_percentile(total_times, 95)
    min_total = min(total_times) if total_times else 0.0
    max_total = max(total_times) if total_times else 0.0
    mean_total = sum(total_times) / len(total_times) if total_times else 0.0

    p95_acq = calculate_percentile(acq_times, 95)
    p95_bld = calculate_percentile(bld_times, 95)
    p95_pkg = calculate_percentile(pkg_times, 95)
    avg_size = int(sum(sizes) / len(sizes)) if sizes else 0

    # Capacity Model Calculation:
    # Target 1: 1000 jobs/day = 1000/86400 jobs/sec (1 job every 86.4s)
    # Target 2: 500 jobs/day = 500/86400 jobs/sec (1 job every 172.8s)
    # Max steady-state utilization: 60% (0.60)
    # N = ceil((arrival_rate) * p95_seconds / max_utilization), min 1
    p95_seconds = p95_total / 1000.0
    arrival_1000 = 1000.0 / 86400.0
    arrival_500 = 500.0 / 86400.0
    max_utilization = 0.60

    req_consumers_1000 = max(1, math.ceil((arrival_1000 * p95_seconds) / max_utilization))
    req_consumers_500 = max(1, math.ceil((arrival_500 * p95_seconds) / max_utilization))
    daily_cap_per_consumer = math.floor((86400.0 * max_utilization) / max(0.001, p95_seconds))

    peak_rss = get_peak_rss_mb()

    return BenchmarkReport(
        git_sha=get_git_sha(),
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        is_production_proof=False,
        platform_info={
            "os": platform.system(),
            "os_release": platform.release(),
            "architecture": platform.machine(),
            "python_version": platform.python_version(),
            "cpu_count": os.cpu_count() or 1,
        },
        config={
            "styles_per_job": style_count,
            "formats_per_style": formats,
            "total_fonts_per_job": style_count * len(formats),
            "target_utilization_max": max_utilization,
        },
        samples_count=sample_count,
        success_count=success_count,
        failure_count=failure_count,
        p50_total_ms=round(p50_total, 2),
        p95_total_ms=round(p95_total, 2),
        min_total_ms=round(min_total, 2),
        max_total_ms=round(max_total, 2),
        mean_total_ms=round(mean_total, 2),
        p95_stage_ms={
            "acquire_ms": round(p95_acq, 2),
            "build_ms": round(p95_bld, 2),
            "package_ms": round(p95_pkg, 2),
        },
        avg_artifact_size_bytes=avg_size,
        peak_rss_mb=round(peak_rss, 2),
        capacity_model={
            "target_1000_jobs_day": {
                "arrival_rate_sec": round(86400.0 / 1000.0, 1),
                "required_consumers": req_consumers_1000,
                "steady_state_utilization_at_1_worker": round(
                    ((arrival_1000 * p95_seconds) / 1.0) * 100, 2
                ),
            },
            "target_500_jobs_day": {
                "arrival_rate_sec": round(86400.0 / 500.0, 1),
                "required_consumers": req_consumers_500,
                "steady_state_utilization_at_1_worker": round(
                    ((arrival_500 * p95_seconds) / 1.0) * 100, 2
                ),
            },
            "daily_capacity_per_consumer_at_60pct_utilization": daily_cap_per_consumer,
        },
        disclaimer=(
            "NOTE: This benchmark is a development/CI environment execution. Per Issue #16 policy, "
            "production capacity proof requires execution on a physical A23 Android/ARM64 device."
        ),
    )


def print_human_summary(report: BenchmarkReport) -> None:
    print("\n============================================================")
    print("  TelegramFonts A23 Compute Pipeline Capacity Benchmark")
    print("============================================================")
    print(f"Git SHA:         {report.git_sha}")
    print(f"Platform:        {report.platform_info['os']} {report.platform_info['architecture']} (Python {report.platform_info['python_version']})")
    print(f"CPUs:            {report.platform_info['cpu_count']}")
    print(f"Workload:        {report.config['styles_per_job']} styles x {len(report.config['formats_per_style'])} formats = {report.config['total_fonts_per_job']} font binaries/job")
    print(f"Samples:         {report.samples_count} ({report.success_count} success, {report.failure_count} failed)")
    print(f"Peak RSS:        {report.peak_rss_mb:.2f} MB")
    print("------------------------------------------------------------")
    print("  Latency Timings (End-to-End Compute & Packaging)")
    print("------------------------------------------------------------")
    print(f"  p50 (Median):  {report.p50_total_ms:.2f} ms ({report.p50_total_ms / 1000:.3f} s)")
    print(f"  p95:           {report.p95_total_ms:.2f} ms ({report.p95_total_ms / 1000:.3f} s)")
    print(f"  Min / Max:     {report.min_total_ms:.2f} ms / {report.max_total_ms:.2f} ms")
    print(f"  Mean:          {report.mean_total_ms:.2f} ms")
    print("------------------------------------------------------------")
    print("  Stage Breakdown (p95)")
    print("------------------------------------------------------------")
    print(f"  Acquisition:   {report.p95_stage_ms['acquire_ms']:.2f} ms")
    print(f"  Font Build:    {report.p95_stage_ms['build_ms']:.2f} ms")
    print(f"  ZIP Package:   {report.p95_stage_ms['package_ms']:.2f} ms")
    print(f"  Avg ZIP Size:  {report.avg_artifact_size_bytes:,} bytes")
    print("------------------------------------------------------------")
    print("  Conservative Capacity Model (Target Utilization <= 60%)")
    print("------------------------------------------------------------")
    cm = report.capacity_model
    t1000 = cm["target_1000_jobs_day"]
    t500 = cm["target_500_jobs_day"]
    print(f"  1000 jobs/day (1 job / {t1000['arrival_rate_sec']}s):  Requires {t1000['required_consumers']} consumer(s) (util: {t1000['steady_state_utilization_at_1_worker']:.2f}%)")
    print(f"  500 jobs/day  (1 job / {t500['arrival_rate_sec']}s):  Requires {t500['required_consumers']} consumer(s) (util: {t500['steady_state_utilization_at_1_worker']:.2f}%)")
    print(f"  Single-consumer capacity (@ 60% util): ~{cm['daily_capacity_per_consumer_at_60pct_utilization']:,} jobs/day")
    print("------------------------------------------------------------")
    print(f"[DISCLAIMER] {report.disclaimer}")
    print("============================================================\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="A23 Pipeline Capacity Benchmark")
    parser.add_argument("--samples", type=int, default=10, help="Number of benchmark iterations")
    parser.add_argument("--styles", type=int, default=2, help="Styles per font family")
    parser.add_argument("--json-out", type=str, default=None, help="Output file for machine-readable JSON report")
    parser.add_argument("--quiet", action="store_true", help="Suppress human stdout summary")

    args = parser.parse_args()

    report = asyncio.run(run_benchmark(sample_count=args.samples, style_count=args.styles))

    if not args.quiet:
        print_human_summary(report)

    if args.json_out:
        out_p = Path(args.json_out)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(asdict(report), indent=2), encoding="utf-8")
        print(f"Machine-readable benchmark report written to {out_p}")


if __name__ == "__main__":
    main()
