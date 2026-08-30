#!/usr/bin/env python3
"""Bounded synthetic regression probes for governance-lint. Governance-only, stdlib-only.

Each probe builds a throwaway synthetic tree, points the linter at it, and asserts one
fail/pass behaviour required by the R15 governance contract. No product/test invocation."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
REV = "P-PROBE.1"
FP = "fp0000000000000"

STATE = """schema_version: {schema}
generation: 2

governance:
  policy_rev: "{rev}"
  fingerprint: "{fp}"

objective: "probe"

phase:
  id: probe
  status: active

now: []
next: []
blockers: []
exit: []
human_requirements:
  active: []
active_decisions: []
recent_invalidations: []
reconciliation:
  status: clean
  event: null
updated_at: "2026-08-30T00:00:00Z"
"""

OPEN_CONTRACT = """schema_version: {schema}
policy_rev: "{rev}"
policy_fingerprint: "{fp}"
id: {tid}
contract_rev: 1
mode: NORMAL
validated_at_generation: 2
scope_tags: [probe]

objective: "open probe"

acceptance:
  A1: "x"
{extra}"""

DONE_CONTRACT = """schema_version: 6
policy_rev: "OLD-REV"
policy_fingerprint: "oldfp0000000000"
id: {tid}
contract_rev: 1
mode: NORMAL
created_at_generation: 1
scope_tags: []

objective: "historical probe"

acceptance:
  A1: "x"

routing:
  role: worker-fast
  reason: "legacy routing retained as immutable history"
"""

DONE_RESULT = """schema_version: 3
task: {tid}
contract_rev: 1
status: completed
governance:
  policy_rev: "OLD-REV"
  fingerprint: "oldfp0000000000"
proved: [A1]
finished_at: "2026-08-30T00:00:00Z"
"""


def load_linter(root: Path):
    spec = importlib.util.spec_from_file_location("gl_probe", HERE / "governance-lint.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.ROOT = root
    return mod


def tree(tmp: Path) -> Path:
    prime = tmp / ".prime"
    (prime / "tasks").mkdir(parents=True, exist_ok=True)
    (tmp / ".ai").mkdir(exist_ok=True)
    (tmp / ".ai" / "POLICY-REV").write_text(REV + "\n", encoding="utf-8")
    return prime


def write_state(prime: Path, schema: str = "7", extra: str = "") -> None:
    (prime / "state.yaml").write_text(STATE.format(schema=schema, rev=REV, fp=FP) + extra, encoding="utf-8")


def make_task(prime: Path, tid: str, contract_text: str, result_text: str | None) -> None:
    d = prime / "tasks" / tid
    d.mkdir(parents=True, exist_ok=True)
    (d / "contract.yaml").write_text(contract_text, encoding="utf-8")
    if result_text is not None:
        (d / "result.yaml").write_text(result_text, encoding="utf-8")


def has(errs: list[str], needle: str) -> bool:
    return any(needle in e for e in errs)


def p1_old_state_schema_fails(mod, tmp: Path) -> bool:
    prime = tree(tmp)
    write_state(prime, schema="6", extra="\npersistence:\n  remote_sync: current\n\nlast_event: E-00001\n")
    errs: list[str] = []
    mod.lint_runtime_memory(errs, REV, FP)
    return has(errs, "schema_version must be 7") and has(errs, "deprecated field: persistence") and has(errs, "deprecated field: last_event")


def p2_open_contract_old_schema_policy_fails(mod, tmp: Path) -> bool:
    prime = tree(tmp)
    write_state(prime)
    make_task(prime, "T-OPEN", OPEN_CONTRACT.format(schema="7", rev="OLD-REV", fp="oldfp0000000000", tid="T-OPEN", extra=""), None)
    errs: list[str] = []
    mod.lint_runtime_memory(errs, REV, FP)
    return has(errs, "schema_version must be 8") and has(errs, "policy_rev stale") and has(errs, "policy_fingerprint stale")


def p3_open_contract_deprecated_fields_fail(mod, tmp: Path) -> bool:
    prime = tree(tmp)
    write_state(prime)
    extra = "created_at_generation: 1\nrouting:\n  role: worker-fast\n  reason: \"deprecated role truth\"\n"
    make_task(prime, "T-OPEN", OPEN_CONTRACT.format(schema="8", rev=REV, fp=FP, tid="T-OPEN", extra=extra), None)
    errs: list[str] = []
    mod.lint_runtime_memory(errs, REV, FP)
    return has(errs, "deprecated created_at_generation") and has(errs, "deprecated routing.role")


GIT_ENV_NOISE = "cannot verify Git-tracked"


def _real(errs: list[str]) -> list[str]:
    return [e for e in errs if GIT_ENV_NOISE not in e]


def p4_historical_completed_immutable(mod, tmp: Path) -> bool:
    prime = tree(tmp)
    write_state(prime)
    make_task(prime, "T-DONE", DONE_CONTRACT.format(tid="T-DONE"), DONE_RESULT.format(tid="T-DONE"))
    errs: list[str] = []
    mod.lint_runtime_memory(errs, REV, FP)
    return not _real(errs)


def p5_completed_dependency_resolves(mod, tmp: Path) -> bool:
    prime = tree(tmp)
    write_state(prime)
    make_task(prime, "T-DONE", DONE_CONTRACT.format(tid="T-DONE"), DONE_RESULT.format(tid="T-DONE"))
    extra = "depends_on:\n  tasks: [T-DONE]\n"
    make_task(prime, "T-OPEN", OPEN_CONTRACT.format(schema="8", rev=REV, fp=FP, tid="T-OPEN", extra=extra), None)
    errs: list[str] = []
    mod.lint_runtime_memory(errs, REV, FP)
    return not any("depends_on" in e for e in errs) and not _real(errs)


def p6_dangling_refs_fail(mod, tmp: Path) -> bool:
    prime = tree(tmp)
    write_state(prime, extra='\nroadmap_ref: "decisions/ROADMAP.md"\n')
    state_text = (prime / "state.yaml").read_text(encoding="utf-8").replace("active_decisions: []", 'active_decisions:\n  - "ADR-9999"')
    (prime / "state.yaml").write_text(state_text, encoding="utf-8")
    extra = "depends_on:\n  tasks: [T-MISSING]\n"
    make_task(prime, "T-OPEN", OPEN_CONTRACT.format(schema="8", rev=REV, fp=FP, tid="T-OPEN", extra=extra), None)
    errs: list[str] = []
    mod.lint_runtime_memory(errs, REV, FP)
    return has(errs, "dangling roadmap_ref") and has(errs, "dangling active_decisions ref") and has(errs, "dangling depends_on.tasks ref")


def p7_legacy_journal_lines_valid(mod, tmp: Path) -> bool:
    prime = tree(tmp)
    write_state(prime)
    jdir = prime / "journal"
    jdir.mkdir(exist_ok=True)
    legacy = '{"id":"E-00001","ts":"2026-08-30T00:00:00Z","actor":"prime","type":"decision_created","generation":1,"source_ref":null,"supersedes":[],"summary":"legacy entry without scopes"}'
    (jdir / "events.jsonl").write_text(legacy + "\n", encoding="utf-8")
    errs: list[str] = []
    mod.lint_live_journal(errs)
    return not errs


def p8_new_journal_template_has_scopes(mod, tmp: Path) -> bool:
    tpl = (REPO_ROOT / ".ai/templates/prime-memory/journal/EVENT-SCHEMA.jsonl").read_text(encoding="utf-8")
    return '"scopes":[]' in tpl


def p9_untracked_prime_memory_fails(mod, tmp: Path) -> bool:
    prime = tree(tmp)
    write_state(prime)
    run = lambda *a: subprocess.run(list(a), cwd=tmp, capture_output=True, check=False)
    if run("git", "init", "-q").returncode != 0:
        return False
    if run("git", "add", ".ai").returncode != 0:
        return False
    errs: list[str] = []
    mod._lint_prime_git_tracking(errs)
    if not has(errs, "not Git-tracked"):
        return False
    if run("git", "add", ".prime").returncode != 0:
        return False
    errs2: list[str] = []
    mod._lint_prime_git_tracking(errs2)
    return not errs2


PROBES = (
    ("P1 old live state schema or deprecated state fields fail", p1_old_state_schema_fails),
    ("P2 open contract with old policy/schema fails", p2_open_contract_old_schema_policy_fails),
    ("P3 open contract containing created_at_generation or routing.role fails", p3_open_contract_deprecated_fields_fail),
    ("P4 completed historical task with older valid policy/result remains accepted and immutable", p4_historical_completed_immutable),
    ("P5 completed dependency resolution still works", p5_completed_dependency_resolves),
    ("P6 dangling roadmap/ADR/task dependency fails", p6_dangling_refs_fail),
    ("P7 old journal lines remain valid history", p7_legacy_journal_lines_valid),
    ("P8 new journal template includes scopes", p8_new_journal_template_has_scopes),
    ("P9 untracked canonical .prime memory fails", p9_untracked_prime_memory_fails),
)


def main() -> int:
    failures = 0
    for name, fn in PROBES:
        tmp = Path(tempfile.mkdtemp(prefix="glprobe-"))
        try:
            mod = load_linter(tmp)
            ok = fn(mod, tmp)
        except Exception as exc:  # probe bugs must surface loudly
            ok = False
            name = f"{name} (exception: {exc!r})"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            failures += 1
    if failures:
        print(f"governance probes: FAIL ({failures}/{len(PROBES)})")
        return 1
    print(f"governance probes: OK ({len(PROBES)}/{len(PROBES)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())