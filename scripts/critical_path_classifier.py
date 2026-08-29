#!/usr/bin/env python3
"""Deterministic critical-path classifier for the FULL-MAX gate.

Issue #88: routes CI into the conditional `fullmax-final` lane. The
classifier is a pure, deterministic function of the changed path list plus
explicit override flags. It is FAIL-CLOSED by construction:

1. Every changed path that is not inside the explicit NON-CRITICAL allowlist
   (documentation / governance / agent-profile surfaces) triggers FULL-MAX.
   Unknown or unmapped paths therefore trigger, never skip.
2. An empty/missing change list triggers FULL-MAX (identity not proven
   non-critical -> assume critical).
3. Any internal exception triggers FULL-MAX (the error envelope itself is a
   trigger; the classifier never exits by silently reporting "no trigger").
4. Architect/reviewer override (PR label `fullmax:force`) and final-release
   override (`workflow_dispatch` force input) always trigger.

The critical-path reason map below is informational: it labels WHY a
critical surface triggers (reconstruction / optimizer / measurement
schedule / fidelity evaluator / release gate / font model / font builder /
CI+test gating layer). Triggering does not depend on the map being complete;
the allowlist is the only exemption surface and it is intentionally tiny.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterable

# ---------------------------------------------------------------------------
# NON-CRITICAL allowlist: documentation / governance / agent-profile ONLY.
# Everything else in the repository is critical-path for gating purposes.
# ---------------------------------------------------------------------------
NON_CRITICAL_PREFIXES: tuple[str, ...] = (
    "docs/",
    ".ai/",
)
NON_CRITICAL_FILES: tuple[str, ...] = (
    "ARCHITECT.md",
    "EXECUTOR.md",
    "README.md",
)
NON_CRITICAL_REASON = "docs/governance/agent-profile-only"

# ---------------------------------------------------------------------------
# Critical-path reason map (informational labels for trigger decisions).
# Prefix or exact-path match, longest match wins.
# ---------------------------------------------------------------------------
CRITICAL_REASON_MAP: tuple[tuple[str, str], ...] = (
    ("agent/src/reconstruction/", "reconstruction/font-model/font-builder"),
    ("agent/src/fidelity/", "optimizer/fidelity-evaluator/release-gate"),
    ("agent/src/measurement/", "measurement-schedule/collector/calibration"),
    ("agent/src/compute/font_builder.py", "font-builder"),
    ("agent/src/compute/", "compute-pipeline"),
    ("agent/src/acquisition/", "acquisition-boundary"),
    ("agent/src/typography/", "font-builder-typography"),
    ("agent/src/", "agent-production-core"),
    (".github/workflows/", "ci-gating-layer"),
    (".github/", "ci-gating-layer"),
    ("agent/tests/", "test-layer"),
    ("agent/pyproject.toml", "test-layer-config"),
    ("agent/requirements", "test-layer-dependencies"),
    ("scripts/critical_path_classifier.py", "gate-classifier-self"),
    ("scripts/", "production-ops-scripts"),
    ("edge/", "edge-production-worker"),
    ("ops/", "ops-manifests-reports"),
    ("agent/", "agent-surface"),
)

OVERRIDE_LABEL = "fullmax:force"


def _normalize(path: str) -> str:
    normalized = path.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _non_critical(path: str) -> bool:
    if path in NON_CRITICAL_FILES:
        return True
    return any(path.startswith(prefix) for prefix in NON_CRITICAL_PREFIXES)


def _critical_reason(path: str) -> str:
    best = ""
    best_label = "unclassified-path-fail-closed"
    for prefix, label in CRITICAL_REASON_MAP:
        if path.startswith(prefix) or path == prefix.rstrip("/"):
            if len(prefix) > len(best):
                best = prefix
                best_label = label
    return best_label


def classify(
    changed_paths: Iterable[str],
    force_label: bool = False,
    force_dispatch: bool = False,
) -> dict:
    """Return {"fullmax": bool, "reasons": [str, ...]} fail-closed.

    This function never raises: any internal fault produces a trigger with a
    FAIL_CLOSED_ERROR reason (silent omission is impossible).
    """
    try:
        reasons: list[str] = []
        fullmax = False

        if force_label:
            fullmax = True
            reasons.append(f"override: PR label '{OVERRIDE_LABEL}' (Architect/reviewer)")
        if force_dispatch:
            fullmax = True
            reasons.append("override: workflow_dispatch force (final-release/manual)")

        paths = sorted({_normalize(p) for p in changed_paths if p and p.strip()})
        if not paths:
            fullmax = True
            reasons.append("fail-closed: empty or undeterminable change list")
            return {"fullmax": fullmax, "reasons": reasons}

        for path in paths:
            if _non_critical(path):
                reasons.append(f"non-critical: {path} ({NON_CRITICAL_REASON})")
            else:
                fullmax = True
                reasons.append(f"critical: {path} ({_critical_reason(path)})")

        if not fullmax:
            reasons.append(
                "non-trigger: every changed path is docs/governance/agent-profile-only"
            )
        return {"fullmax": fullmax, "reasons": reasons}
    except Exception as exc:  # noqa: BLE001 - fail-closed envelope
        return {
            "fullmax": True,
            "reasons": [f"fail-closed: classifier internal error ({type(exc).__name__}: {exc})"],
        }


def _parse_bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"true", "1", "yes", "y"}


def _emit(decision: dict, github_output: str | None) -> int:
    lines = [f"FULLMAX={'true' if decision['fullmax'] else 'false'}"]
    lines += [f"REASON {reason}" for reason in decision["reasons"]]
    text = "\n".join(lines)
    print(text)
    if github_output:
        with open(github_output, "a", encoding="utf-8") as handle:
            handle.write(f"fullmax={'true' if decision['fullmax'] else 'false'}\n")
            handle.write("reason<<FULLMAX_REASON_EOF\n")
            handle.write(text + "\n")
            handle.write("FULLMAX_REASON_EOF\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic FULL-MAX critical-path classifier")
    parser.add_argument("--event", default="", help="GitHub event name (informational)")
    parser.add_argument("--force-label", default="false", help="PR carries the override label")
    parser.add_argument("--force-dispatch", default="false", help="workflow_dispatch force input")
    parser.add_argument("--files", default="", help="newline/space separated changed paths")
    parser.add_argument("--file", action="append", default=[], help="single changed path (repeatable)")
    args = parser.parse_args(argv)

    raw_paths: list[str] = []
    if args.files:
        raw_paths.extend(args.files.replace("\r", "\n").replace(";", "\n").split())
    raw_paths.extend(args.file)

    decision = classify(
        raw_paths,
        force_label=_parse_bool(args.force_label),
        force_dispatch=_parse_bool(args.force_dispatch),
    )
    if args.event:
        decision["reasons"].insert(0, f"event: {args.event}")
    return _emit(decision, os.environ.get("GITHUB_OUTPUT"))


if __name__ == "__main__":
    sys.exit(main())
