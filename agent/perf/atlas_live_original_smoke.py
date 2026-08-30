"""R5 LIVE ORIGINAL acquisition smoke (T-FAST-ATLAS-PROD-READY-01, R1 chain).

Exactly ONE small ORIGINAL run of the ProductionAtlasPipeline against the
canonical MyFonts collection URL, through the REAL transport chain:
exact cache/binary -> Chrome native --dump-dom -> (lazy persistent browser)
-> Monotype CDN primary raster -> Algolia -> CDN.

Bounds: pipeline deadline 480 s (8 min) inside a 600 s hard-kill watchdog;
coverage capped to the first 24 OBSERVED code points (bounded smoke subset -
every served byte remains an observed response; the cap is recorded).
No retry: whatever the chain does (happy path OR fail-closed bot-challenge
/ 403 behavior) is recorded HONESTLY as evidence.
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

from atlas.policy import AtlasRuntimeDefaults  # noqa: E402
from atlas.transport import (  # noqa: E402
    AtlasTransportCounters,
    ProductionAtlasPipeline,
)

SOURCE_URL = "https://www.myfonts.com/collections/be-vietnam-pro"
FAMILY = "Be Vietnam Pro"
STYLE = "Regular"
STYLE_ID = "regular"
COVERAGE_CAP = 24
PIPELINE_DEADLINE_SECONDS = 480
WATCHDOG_KILL_SECONDS = 600
CACHE_ROOT = Path(os.environ.get(
    "ATLAS_ORIG_LIVE_SMOKE_CACHE",
    str(Path.home() / ".telefont-atlas-orig-live-smoke-cache"),
))


def _watchdog(start_monotonic: float) -> None:
    while True:
        if time.monotonic() - start_monotonic > WATCHDOG_KILL_SECONDS:
            print("ORIG_LIVE_SMOKE_WATCHDOG_KILL: 10-minute wall exceeded", flush=True)
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
    t_boot = time.monotonic()
    threading.Thread(target=_watchdog, args=(t_boot,), daemon=True).start()

    counters = AtlasTransportCounters()
    pipeline = ProductionAtlasPipeline(
        job_id="r5-original-live-smoke",
        mode="ORIGINAL",
        source_url=SOURCE_URL,
        family_name=FAMILY,
        style_id=STYLE_ID,
        style_name=STYLE,
        build_dir=CACHE_ROOT / "build",
        deadline=time.monotonic() + PIPELINE_DEADLINE_SECONDS,
        cache_root=CACHE_ROOT / "cache",
        checkpoint_root=CACHE_ROOT / "ckpt",
        binary_cache=None,  # fresh chain: dump-dom -> CDN -> browser
        runtime=AtlasRuntimeDefaults(),
        counters=counters,
        coverage_cap=COVERAGE_CAP,
    )

    ev: dict = {
        "task": "T-FAST-ATLAS-PROD-READY-01",
        "acceptance": "R5",
        "run_label": "original_live_smoke_single",
        "chain": (
            "exact cache/binary -> Chrome native --dump-dom metadata/MD5 -> "
            "(lazy persistent browser, only when needed) -> Monotype CDN "
            "PRIMARY raster (exact-MD5 verified) -> Algolia -> CDN"
        ),
        "bounds": {
            "source_url": SOURCE_URL,
            "family": FAMILY,
            "style": STYLE,
            "mode": "ORIGINAL",
            "coverage_cap_observed_cps": COVERAGE_CAP,
            "pipeline_deadline_seconds": PIPELINE_DEADLINE_SECONDS,
            "watchdog_kill_seconds": WATCHDOG_KILL_SECONDS,
            "retry": "none (exactly one run)",
        },
    }

    t0 = time.monotonic()
    run_section: dict = {}
    try:
        result = asyncio.run(pipeline.run())
        run_section["outcome"] = "COMPLETED"
        run_section["glyph_count"] = result.evidence.glyph_count
        run_section["easy_glyphs"] = result.evidence.easy_glyphs
        run_section["refined_glyphs"] = result.evidence.refined_glyphs
        run_section["failed_glyphs"] = result.evidence.failed_glyphs
        run_section["failed_glyph_ids"] = [
            f"U+{cp:04X}" for cp in result.evidence.failed_glyph_ids
        ]
        run_section["pages_by_source"] = result.evidence.pages_by_source
        run_section["validation_passed"] = bool(result.report.get("passed"))
        run_section["validation_reasons"] = list(result.report.get("reasons", []))
        run_section["ttf_bytes"] = (
            result.ttf_path.stat().st_size if result.ttf_path else 0
        )
        run_section["otf_bytes"] = (
            result.otf_path.stat().st_size if result.otf_path else 0
        )
    except Exception as exc:  # honest capture; fail-closed is a valid observation
        run_section["outcome"] = "FAIL_CLOSED_OR_ERROR"
        run_section["error_type"] = type(exc).__name__
        run_section["error"] = str(exc)[:300]
    run_section["wall_seconds"] = round(time.monotonic() - t0, 3)
    ev["run"] = run_section

    ev["counters_observed"] = {
        "http_requests": counters.http_requests,
        "cdp_calls": counters.cdp_calls,
        "browser_readbacks": counters.browser_readbacks,
        "dump_dom_calls": counters.dump_dom_calls,
        "cache_hits": counters.cache_hits,
    }

    # MD5 discovery outcome (public identity; not secret material). Re-uses
    # the cached dump when present (no extra subprocess).
    md5_section: dict = {"resolved": False}
    try:
        envelope = asyncio.run(pipeline.dump_dom.family_envelope(SOURCE_URL, FAMILY))
        if envelope is not None:
            rec = envelope.get_style_record(STYLE, STYLE) or envelope.get_style_record(STYLE_ID, STYLE)
            md5_section["envelope_family"] = envelope.family_name
            md5_section["envelope_styles"] = len(envelope.styles)
            if rec is not None and len(rec.md5) == 32:
                md5_section["resolved"] = True
                md5_section["style_md5"] = rec.md5
            else:
                md5_section["style_md5_absent"] = True
        else:
            md5_section["envelope"] = "none (dump-dom produced no parseable discovery)"
    except Exception as exc:
        md5_section["envelope_error"] = type(exc).__name__
    ev["md5_discovery"] = md5_section

    ev["timing"] = {
        "total_wall_seconds": round(time.monotonic() - t_boot, 3),
        "wall_cap_seconds": WATCHDOG_KILL_SECONDS,
        "within_cap": bool(time.monotonic() - t_boot < WATCHDOG_KILL_SECONDS),
    }

    out_path = (
        Path(__file__).resolve().parent.parent.parent
        / ".prime" / "tasks" / "T-FAST-ATLAS-PROD-READY-01"
        / "original-live-smoke-evidence.yaml"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(_yaml(ev), encoding="utf-8")
    print(_yaml(ev))
    print(f"EVIDENCE_SAVED: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
