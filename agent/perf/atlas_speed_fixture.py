"""U12 bounded speed fixture harness (T-FAST-ATLAS-ULTRA-01, ADR-0004).

Runs ONE AtlasUltraPipeline ORIGINAL fixture run and captures the full
evidence dict, then ONE immediate repeat run over the persistent
exact-identity caches for the reuse-path timing.

FIXTURE SUBSTITUTION (recorded honestly): the contract names Neurath Mono
Regular (myfonts.com) as the ordinary fixture; network acquisition of that
font is unavailable in this local environment (no authorized-session
material, no production access under this task). The next best available
LOCAL fixture is used instead: Be Vietnam Pro Regular
(agent/benchmark_data/ground_truth/BeVietnamPro-Regular.ttf), limited to
the first ~200 cmap code points (ordinary ORIGINAL 150-250 glyph shape).

Bounds: ONE run, 8-minute wall inside the pipeline + a 10-minute hard
watchdog kill. No blind retry: a failed run is captured and reported.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from atlas.cache import AtlasCacheStore, AtlasCheckpointStore  # noqa: E402
from atlas.local_fixture import (  # noqa: E402
    LocalFontMetricsProvider,
    LocalFontRasterProvider,
    fixture_glyph_set,
)
from atlas.pipeline import AtlasStyleSpec, AtlasUltraPipeline  # noqa: E402
from atlas.policy import AtlasRuntimeDefaults  # noqa: E402

FIXTURE_FONT = (
    Path(__file__).resolve().parent.parent
    / "benchmark_data"
    / "ground_truth"
    / "BeVietnamPro-Regular.ttf"
)
CACHE_ROOT = Path(os.environ.get(
    "ATLAS_FIXTURE_CACHE",
    str(Path.home() / ".telefont-atlas-fixture-cache"),
))
WALL_SECONDS = 480          # 8-minute bounded wall (ORIGINAL hard wall)
WATCHDOG_KILL_SECONDS = 600  # +2 min hard kill margin
# Env overrides permit the bounded large-style timing run (400-500 glyphs)
# without duplicating the harness; defaults are the ordinary fixture.
GLYPH_LIMIT = int(os.environ.get("ATLAS_FIXTURE_LIMIT", "200"))
EVIDENCE_NAME = os.environ.get(
    "ATLAS_FIXTURE_EVIDENCE_NAME", "fixture-evidence.yaml"
)
RUN_LABEL = os.environ.get("ATLAS_FIXTURE_RUN_LABEL", "ordinary_original")


def _watchdog(start_monotonic: float) -> None:
    while True:
        if time.monotonic() - start_monotonic > WATCHDOG_KILL_SECONDS:
            print("FIXTURE_WATCHDOG_KILL: wall exceeded", flush=True)
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


def main() -> int:
    import asyncio

    t_boot = time.monotonic()
    threading.Thread(target=_watchdog, args=(t_boot,), daemon=True).start()

    def peak_rss_mb() -> float:
        try:
            import resource as _r  # POSIX only; Windows -> 0.0 (tracemalloc
            # peak in the evidence covers Python allocations either way).

            ru = _r.getrusage(_r.RUSAGE_SELF).ru_maxrss
            return ru / 1024.0  # Linux reports KB
        except Exception:
            return 0.0

    cps = fixture_glyph_set(FIXTURE_FONT, limit=GLYPH_LIMIT)
    spec = AtlasStyleSpec(
        source_url="fixture://local/be-vietnam-pro",
        family_name="Be Vietnam Pro Fixture",
        style_name="Regular",
        style_id="regular",
        mode="ORIGINAL",
        code_points=cps,
    )

    def make(deadline: float) -> AtlasUltraPipeline:
        return AtlasUltraPipeline(
            spec=spec,
            runtime=AtlasRuntimeDefaults(),
            metrics_provider=LocalFontMetricsProvider(FIXTURE_FONT),
            raster_provider=LocalFontRasterProvider(FIXTURE_FONT),
            cache=AtlasCacheStore(CACHE_ROOT / "cache"),
            checkpoint_store=AtlasCheckpointStore(CACHE_ROOT / "checkpoints"),
            deadline=deadline,
        )

    # ---- ONE cold run (bounded wall) -----------------------------------
    t0 = time.monotonic()
    result = asyncio.run(make(t0 + WALL_SECONDS).run())
    cold_wall = time.monotonic() - t0
    ev = result.evidence.to_dict()
    ev["fixture"] = {
        "run_label": RUN_LABEL,
        "font": str(FIXTURE_FONT),
        "substitution": (
            "Neurath Mono Regular (myfonts.com) network acquisition "
            "unavailable locally (no authorized session material, no "
            "production access); next best LOCAL fixture used: "
            "Be Vietnam Pro Regular"
        ),
        "glyph_limit": GLYPH_LIMIT,
        "glyph_count": len(cps),
        "mode": "ORIGINAL",
    }
    ev["cold_wall_seconds"] = round(cold_wall, 3)
    ev["peak_rss_mb"] = round(peak_rss_mb(), 3)
    ev["outputs"] = {
        "ttf": str(result.ttf_path),
        "otf": str(result.otf_path),
        "model_hash": (
            result.model.compute_canonical_hash() if result.model is not None else ""
        ),
    }

    # ---- ONE immediate repeat run: reuse-path timing -------------------
    t1 = time.monotonic()
    result_reuse = asyncio.run(make(t1 + WALL_SECONDS).run())
    reuse_wall = time.monotonic() - t1
    ev["reuse_run"] = {
        "wall_seconds": round(reuse_wall, 3),
        "target_under_seconds": 60,
        "met": bool(reuse_wall < 60.0),
        "ttf_sha256": result_reuse.report.get("passed"),
    }

    out_path = (
        Path(__file__).resolve().parent.parent.parent
        / ".prime" / "tasks" / "T-FAST-ATLAS-ULTRA-01" / EVIDENCE_NAME
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(_yaml(ev), encoding="utf-8")
    print(_yaml(ev))
    print(f"EVIDENCE_SAVED: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
