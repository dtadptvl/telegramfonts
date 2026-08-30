#!/usr/bin/env python3
"""Deterministic lint/fingerprint for Prime governance. Stdlib only."""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

FINGERPRINT_ROOTS = (
    Path("PRIME.md"),
    # POLICY-REV is bound separately as the human-readable revision truth.
    Path(".ai/policies"),
    Path(".ai/templates/prime-memory"),
    Path(".ai/tools"),
)

AGENT_DIR = Path(".kilo/agents")
RUNTIME_ONLY_AGENT_KEYS = {"description", "model", "variant", "temperature", "steps", "hidden"}

LEGACY_PATHS = (
    "ARCHITECT.md",
    "EXECUTOR.md",
    ".ai/ARCHITECT-REF.md",
    ".ai/EXECUTOR-REF.md",
    ".kilo/agents/architect-qwen.md",
    ".kilo/agents/executor-qwen.md",
    ".kilo/agents/executor-gemini.md",
    ".kilo/agents/researcher.md",
    ".kilo/agents/review-assistant.md",
)


def text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def normalize_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in value.split("\n")]
    return "\n".join(lines).rstrip() + "\n"


def normalize_agent_semantics(path: Path) -> str:
    """Remove runtime-only profile metadata while preserving permissions + logical guardrails."""
    body = normalize_text(path.read_text(encoding="utf-8"))
    front_kept = []
    rest = body
    if body.startswith("---\n") and "\n---\n" in body[4:]:
        front, rest = body[4:].split("\n---\n", 1)
        for line in front.splitlines():
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):", line)
            if m and not line.startswith((" ", "\t")) and m.group(1) in RUNTIME_ONLY_AGENT_KEYS:
                continue
            front_kept.append(line)

    group = agent_group(path)
    if group in {"worker-fast", "worker-deep"} and "\nRules:\n" in rest:
        preamble, rules = rest.split("\nRules:\n", 1)
        flags = [
            f"freshness={'yes' if 'policy_rev' in preamble and 'policy_fingerprint' in preamble else 'no'}",
            f"routing_reason={'yes' if 'routing.reason' in preamble else 'no'}",
        ]
        rest = f"logical-role: {group}; " + "; ".join(flags) + "\nRules:\n" + rules
    else:
        rest = re.sub(
            r"You are the (?:fallback )?(?:Gemini|Qwen) runtime of logical (`worker-(?:fast|deep)`)",
            r"You are the RUNTIME runtime of logical \1",
            rest,
        )

    prefix = "---\n" + "\n".join(front_kept).rstrip() + "\n---\n" if front_kept else ""
    return normalize_text(prefix + rest)


def agent_group(path: Path) -> str:
    name = path.name
    if name.startswith("worker-fast"):
        return "worker-fast"
    if name.startswith("worker-deep"):
        return "worker-deep"
    if name == "prime-agent.md":
        return "prime"
    if name == "inspector.md":
        return "inspector"
    return path.stem


def fingerprint_inputs() -> list[tuple[str, bytes]]:
    records: list[tuple[str, bytes]] = []
    files: set[Path] = set()
    for rel in FINGERPRINT_ROOTS:
        p = ROOT / rel
        if p.is_file():
            files.add(p)
        elif p.is_dir():
            for child in p.rglob("*"):
                if not child.is_file():
                    continue
                if "__pycache__" in child.parts or child.suffix in {".pyc", ".pyo"}:
                    continue
                if child.name == ".DS_Store":
                    continue
                files.add(child)

    for p in sorted(files, key=lambda x: x.relative_to(ROOT).as_posix()):
        rel = p.relative_to(ROOT).as_posix()
        raw = p.read_bytes()
        try:
            normalized = normalize_text(raw.decode("utf-8")).encode("utf-8")
        except UnicodeDecodeError:
            normalized = raw
        records.append((rel, normalized))

    # Runtime variants of one logical role contribute one deduplicated semantic record.
    # Model/variant/temperature/steps/description/hidden changes do not stale contracts.
    groups: dict[str, set[str]] = {}
    agent_root = ROOT / AGENT_DIR
    if agent_root.is_dir():
        for p in sorted(agent_root.glob("*.md")):
            groups.setdefault(agent_group(p), set()).add(normalize_agent_semantics(p))
    for group in sorted(groups):
        for i, semantic in enumerate(sorted(groups[group])):
            records.append((f"{AGENT_DIR.as_posix()}@{group}:{i}", semantic.encode("utf-8")))
    return records


def governance_fingerprint() -> str:
    h = hashlib.sha256()
    for label, payload in fingerprint_inputs():
        h.update(label.encode("utf-8"))
        h.update(b"\0")
        h.update(payload)
        h.update(b"\0")
    return h.hexdigest()


def require(errors: list[str], cond: bool, msg: str) -> None:
    if not cond:
        errors.append(msg)


def governance_text_files() -> list[Path]:
    out = [ROOT / "PRIME.md"]
    for base in (ROOT / ".ai/policies", ROOT / ".kilo/agents"):
        if base.is_dir():
            out.extend(sorted(base.glob("*.md")))
    migration = ROOT / "PRIME-MIGRATION.md"
    if migration.is_file():
        out.append(migration)
    return out


def lint_prime_section_refs(errors: list[str], prime: str) -> None:
    sections = set(re.findall(r"(?m)^##\s+(\d+)\.", prime))
    require(errors, bool(sections), "PRIME.md has no numbered sections")
    for p in governance_text_files():
        body = p.read_text(encoding="utf-8")
        for ref in re.findall(r"PRIME\s+§(\d+)", body):
            require(errors, ref in sections, f"stale PRIME section ref in {p.relative_to(ROOT)}: §{ref}")


def lint() -> list[str]:
    errors: list[str] = []

    rev_path = ROOT / ".ai/POLICY-REV"
    require(errors, rev_path.is_file(), "missing .ai/POLICY-REV")
    rev = rev_path.read_text(encoding="utf-8").strip() if rev_path.is_file() else ""
    require(errors, bool(rev) and "\n" not in rev, ".ai/POLICY-REV must contain exactly one non-empty revision line")

    prime = text("PRIME.md")
    require(errors, "CORE_REV" not in prime, "PRIME.md must not define duplicate CORE_REV")
    require(errors, "single revision truth" in prime, "PRIME.md must state .ai/POLICY-REV is the single revision truth")
    require(errors, "Single-Owner Matrix" in prime, "PRIME.md missing single-owner truth matrix")
    require(errors, "Deterministic Lazy Policy Routing" in prime, "PRIME.md missing deterministic policy routing")
    lint_prime_section_refs(errors, prime)

    bootstrap_template = ROOT / ".ai/templates/prime-memory/BOOTSTRAP.md"
    require(errors, not bootstrap_template.exists(), "default BOOTSTRAP template must stay absent; BOOTSTRAP is project-specific only")

    state = text(".ai/templates/prime-memory/state.yaml")
    contract = text(".ai/templates/prime-memory/tasks/T-EXAMPLE/contract.yaml")
    result = text(".ai/templates/prime-memory/tasks/T-EXAMPLE/result.yaml")
    progress = text(".ai/templates/prime-memory/tasks/T-EXAMPLE/progress.yaml")
    journal = text(".ai/templates/prime-memory/journal/EVENT-SCHEMA.jsonl")

    require(errors, "local_commit:" not in state and "remote_commit:" not in state, "state template must not cache Git commit SHAs")
    require(errors, "policy_fingerprint:" in contract, "contract template missing policy_fingerprint")
    require(errors, re.search(r"(?m)^contract_rev:\s*[1-9][0-9]*\s*$", contract) is not None, "contract template missing positive contract_rev")
    require(errors, "contract_rev:" in result and "contract_rev:" in progress, "result/progress templates must echo contract_rev")
    require(errors, "acceptance:\n  A1:" in contract, "contract template must use compact acceptance IDs")
    require(errors, "runtime:" not in contract and "failover_from" not in contract, "runtime/failover metadata must not be semantic contract truth")
    require(errors, rev not in state and rev not in contract and rev not in result, "templates must use revision placeholders, not duplicate current POLICY-REV")
    require(errors, "fingerprint:" in result and "proved:" in result, "result template must echo governance fingerprint and proved IDs")
    require(errors, "worker outcome claim" in result, "result template must state it is a worker outcome claim pending Prime promotion")
    require(errors, "rollback:" in contract, "contract template missing optional rollback control")
    require(errors, re.search(r"(?m)^#?\s*policies:\s*", contract) is None, "contract must not duplicate derived policies list")
    require(errors, "extra_policies:" in contract, "contract template missing additive extra_policies control")
    require(errors, "# failure:" in result, "result template missing compact failure evidence envelope")

    require(errors, '"actor":"human|prime"' in journal, "journal actor must be human|prime")
    for forbidden in ("worker", "task_created", "task_cancelled", "task_completed", "generation_changed", "|other", '"other"'):
        require(errors, forbidden not in journal, f"journal schema contains forbidden routine/garbage event token: {forbidden}")

    for rel in LEGACY_PATHS:
        require(errors, not (ROOT / rel).exists(), f"legacy governance must be retired: {rel}")

    live_state = ROOT / ".prime/state.yaml"
    migration = ROOT / "PRIME-MIGRATION.md"
    require(errors, not (live_state.is_file() and migration.is_file()), "PRIME-MIGRATION.md must be deleted after live .prime/state.yaml exists")

    prime_agent = text(".kilo/agents/prime-agent.md")
    require(errors, 'edit:\n    "*": deny\n    ".prime/**": allow' in prime_agent, "Prime edit permission must default-deny and allow only .prime/**")
    require(errors, 'bash:\n    "*": deny' in prime_agent, "Prime bash permission must default-deny")
    require(errors, "edit: allow" not in prime_agent and "bash: allow" not in prime_agent, "Prime must not have blanket edit/bash allow")
    require(errors, "PRIME §8" in prime_agent, "Prime agent must reference current lazy-policy routing section PRIME §8")

    fast = ROOT / ".kilo/agents/worker-fast.md"
    fast_qwen = ROOT / ".kilo/agents/worker-fast-qwen.md"
    deep = ROOT / ".kilo/agents/worker-deep.md"
    deep_gemini = ROOT / ".kilo/agents/worker-deep-gemini.md"
    require(errors, normalize_agent_semantics(fast) == normalize_agent_semantics(fast_qwen), "worker-fast runtime semantic permissions/rules drifted")
    require(errors, normalize_agent_semantics(deep) == normalize_agent_semantics(deep_gemini), "worker-deep runtime semantic permissions/rules drifted")

    for rel in (fast, fast_qwen, deep, deep_gemini, ROOT / ".kilo/agents/inspector.md"):
        body = rel.read_text(encoding="utf-8")
        require(errors, "policy_fingerprint" in body, f"{rel.relative_to(ROOT)} must check policy_fingerprint")
    require(errors, "failover_from" not in text(".kilo/agents/worker-deep-gemini.md"), "deep Gemini profile must not require failover metadata in semantic contract")
    require(errors, "worker-fast-qwen" in prime and "fail over once" in prime, "PRIME.md must state explicit bounded fast failover")
    require(errors, "one active delegated writer" in prime.lower() and "isolated worktrees" in prime.lower(), "PRIME.md must enforce one delegated writer per worktree")
    require(errors, "worker outcome claim" in prime, "PRIME.md must distinguish worker outcome claim from accepted project truth")
    require(errors, "contract_rev" in prime, "PRIME.md must bind worker handoffs to contract_rev")

    policy_names = (
        "evidence",
        "adversarial",
        "diagnosis",
        "review",
        "consequential",
        "production",
        "security",
        "budget",
        "reconciliation",
    )
    for name in policy_names:
        require(errors, (ROOT / f".ai/policies/{name}.md").is_file(), f"missing policy: {name}.md")
        require(errors, f".ai/policies/{name}.md" in prime, f"PRIME.md missing policy reference: {name}.md")

    security = text(".ai/policies/security.md")
    require(errors, "command arguments" in security and "secure injection/reference" in security, "security policy must forbid plaintext secret command arguments when safer injection/reference exists")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--fingerprint", action="store_true", help="print 16-hex semantic governance fingerprint only")
    group.add_argument("--full-fingerprint", action="store_true", help="print full semantic SHA-256 governance fingerprint only")
    group.add_argument("--list-files", action="store_true", help="list normalized inputs included in the semantic governance fingerprint")
    args = parser.parse_args()

    fp = governance_fingerprint()
    if args.fingerprint:
        print(fp[:16])
        return 0
    if args.full_fingerprint:
        print(fp)
        return 0
    if args.list_files:
        for label, _ in fingerprint_inputs():
            print(label)
        return 0

    errors = lint()
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        print(f"governance-lint: FAIL ({len(errors)} issue(s))", file=sys.stderr)
        return 1

    rev = text(".ai/POLICY-REV").strip()
    print("governance-lint: OK")
    print(f"policy_rev: {rev}")
    print(f"fingerprint: {fp[:16]}")
    print(f"fingerprint_inputs: {len(fingerprint_inputs())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
