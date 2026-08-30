#!/usr/bin/env python3
"""Deterministic lint/fingerprint for Prime governance. Stdlib only."""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

FINGERPRINT_ROOTS = (
    Path("PRIME.md"),
    # POLICY-REV is bound separately as the human-readable revision truth.
    Path(".ai/policies"),
    Path(".ai/protocols"),
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


def _agent_parts(path: Path) -> tuple[list[str], str]:
    """Return semantic frontmatter lines plus body, excluding runtime-only profile metadata."""
    body = normalize_text(path.read_text(encoding="utf-8"))
    front_kept: list[str] = []
    rest = body
    if body.startswith("---\n") and "\n---\n" in body[4:]:
        front, rest = body[4:].split("\n---\n", 1)
        for line in front.splitlines():
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):", line)
            if m and not line.startswith((" ", "\t")) and m.group(1) in RUNTIME_ONLY_AGENT_KEYS:
                continue
            front_kept.append(line)
    return front_kept, rest


def normalize_agent_semantics(path: Path) -> str:
    """Ignore runtime-only metadata/names but retain all semantic permissions and guardrails."""
    front_kept, rest = _agent_parts(path)

    # Model/runtime names are replaceable implementation detail. Fallback safety, trigger,
    # unchanged-contract and authority wording remain semantic and therefore fingerprinted.
    rest = re.sub(r"\b(?:Gemini|Qwen) runtime\b", "RUNTIME runtime", rest)
    rest = re.sub(r"after (?:Gemini|Qwen) capacity/unavailability", "after PRIMARY capacity/unavailability", rest)

    prefix = "---\n" + "\n".join(front_kept).rstrip() + "\n---\n" if front_kept else ""
    return normalize_text(prefix + rest)


def normalize_agent_common_semantics(path: Path) -> str:
    """Normalize role-common permissions/rules while allowing stricter runtime-variant preambles."""
    front_kept, rest = _agent_parts(path)
    group = agent_group(path)
    if group in {"worker-fast", "worker-deep"} and "\nRules:\n" in rest:
        preamble, rules = rest.split("\nRules:\n", 1)
        flags = [
            f"freshness={'yes' if 'policy_rev' in preamble and 'policy_fingerprint' in preamble else 'no'}",
            f"routing_reason={'yes' if 'routing.reason' in preamble else 'no'}",
        ]
        rest = f"logical-role: {group}; " + "; ".join(flags) + "\nRules:\n" + rules
    else:
        rest = re.sub(r"\b(?:Gemini|Qwen) runtime\b", "RUNTIME runtime", rest)

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

    # Runtime variants contribute deduplicated semantic records per logical role.
    # Stricter variant guardrails remain fingerprinted; model/variant/temperature/steps/description/hidden do not.
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
    for base in (ROOT / ".ai/policies", ROOT / ".ai/protocols", ROOT / ".kilo/agents"):
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


POLICY_ROUTE_NAMES = {
    "evidence", "adversarial", "diagnosis", "review", "consequential",
    "production", "security", "budget", "reconciliation",
}


def _route_words(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.lower()))


def _route_targets(value: str) -> frozenset[str]:
    return frozenset(name for name in POLICY_ROUTE_NAMES if re.search(rf"\b{re.escape(name)}\b", value))


def _prime_policy_routes(prime: str) -> list[tuple[set[str], frozenset[str], str]]:
    m = re.search(r"(?ms)^## 8\. .*?^## 9\.", prime)
    if not m:
        return []
    routes: list[tuple[set[str], frozenset[str], str]] = []
    for raw in m.group(0).splitlines():
        if "->" not in raw:
            continue
        left, right = raw.split("->", 1)
        targets = _route_targets(right)
        words = _route_words(left)
        if targets and words:
            routes.append((words, targets, raw.strip()))
    return routes


def _worker_policy_routes(body: str) -> list[tuple[set[str], frozenset[str], str]]:
    marker = "Derive lazy policies from task surfaces + additive `extra_policies`:"
    line = next((ln for ln in body.splitlines() if marker in ln), "")
    if not line:
        return []
    tail = line.split(marker, 1)[1]
    tail = tail.split(". Missing policy", 1)[0]
    routes: list[tuple[set[str], frozenset[str], str]] = []
    for clause in tail.split(";"):
        if "->" not in clause:
            continue
        left, right = clause.split("->", 1)
        targets = _route_targets(right)
        words = _route_words(left)
        if targets and words:
            routes.append((words, targets, clause.strip()))
    return routes


def lint_worker_policy_routes(errors: list[str], prime: str, worker_paths: list[Path]) -> None:
    canonical = _prime_policy_routes(prime)
    require(errors, bool(canonical), "cannot parse canonical lazy-policy routing from PRIME §8")
    if not canonical:
        return

    role_signatures: list[list[tuple[frozenset[str], frozenset[str]]]] = []
    for path in worker_paths:
        body = path.read_text(encoding="utf-8")
        routes = _worker_policy_routes(body)
        require(errors, bool(routes), f"{path.relative_to(ROOT)} missing parseable lazy-policy routing")
        matched_prime: set[int] = set()
        signature: list[tuple[frozenset[str], frozenset[str]]] = []
        for words, targets, raw in routes:
            scored = [(len(words & pwords), i, ptargets, praw) for i, (pwords, ptargets, praw) in enumerate(canonical)]
            score, idx, expected_targets, prime_raw = max(scored, default=(0, -1, frozenset(), ""))
            require(errors, score >= 2, f"{path.relative_to(ROOT)} has routing trigger with no canonical PRIME §8 match: {raw}")
            if score < 2:
                continue
            matched_prime.add(idx)
            require(
                errors,
                targets == expected_targets,
                f"{path.relative_to(ROOT)} routing contradiction: '{raw}' vs canonical '{prime_raw}'",
            )
            signature.append((frozenset(words), targets))

        # Review/reconciliation are Prime/inspector concerns and intentionally not duplicated here.
        for i, (_, targets, raw) in enumerate(canonical):
            if targets & {"review", "reconciliation"}:
                continue
            require(errors, i in matched_prime, f"{path.relative_to(ROOT)} missing canonical worker route: {raw}")
        role_signatures.append(signature)

    if role_signatures:
        first = role_signatures[0]
        for path, sig in zip(worker_paths[1:], role_signatures[1:]):
            require(errors, sig == first, f"worker lazy-policy routing drifted in {path.relative_to(ROOT)}")


def _yaml_commentless(value: str) -> str:
    value = value.strip()
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def _yaml_unquote(value: str) -> str:
    value = _yaml_commentless(value)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _yaml_scalar(path: Path, key: str, parent: str | None = None) -> str | None:
    lines = path.read_text(encoding="utf-8").splitlines()
    start = 0
    end = len(lines)
    min_indent = -1
    if parent is not None:
        for i, line in enumerate(lines):
            m = re.match(rf"^(\s*){re.escape(parent)}:\s*(?:#.*)?$", line)
            if m:
                start = i + 1
                min_indent = len(m.group(1))
                break
        else:
            return None
        for i in range(start, len(lines)):
            if not lines[i].strip() or lines[i].lstrip().startswith("#"):
                continue
            indent = len(lines[i]) - len(lines[i].lstrip())
            if indent <= min_indent:
                end = i
                break
    for line in lines[start:end]:
        m = re.match(rf"^(\s*){re.escape(key)}:\s*(.*?)\s*$", line)
        if not m:
            continue
        indent = len(m.group(1))
        if parent is None and indent != 0:
            continue
        if parent is not None and indent <= min_indent:
            continue
        return _yaml_unquote(m.group(2))
    return None


def _yaml_list(path: Path, key: str, parent: str | None = None) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    start = 0
    end = len(lines)
    parent_indent = -1
    if parent is not None:
        for i, line in enumerate(lines):
            m = re.match(rf"^(\s*){re.escape(parent)}:\s*(?:#.*)?$", line)
            if m:
                start = i + 1
                parent_indent = len(m.group(1))
                break
        else:
            return []
        for i in range(start, len(lines)):
            if not lines[i].strip() or lines[i].lstrip().startswith("#"):
                continue
            indent = len(lines[i]) - len(lines[i].lstrip())
            if indent <= parent_indent:
                end = i
                break

    key_index = None
    key_indent = None
    tail = ""
    for i in range(start, end):
        m = re.match(rf"^(\s*){re.escape(key)}:\s*(.*?)\s*$", lines[i])
        if not m:
            continue
        indent = len(m.group(1))
        if parent is None and indent != 0:
            continue
        if parent is not None and indent <= parent_indent:
            continue
        key_index = i
        key_indent = indent
        tail = _yaml_commentless(m.group(2))
        break
    if key_index is None or key_indent is None:
        return []

    if tail.startswith("[") and tail.endswith("]"):
        inner = tail[1:-1].strip()
        if not inner:
            return []
        return [_yaml_unquote(x.strip()) for x in inner.split(",") if _yaml_unquote(x.strip())]
    if tail and tail not in {"null", "~"}:
        return [_yaml_unquote(tail)]

    out: list[str] = []
    for i in range(key_index + 1, end):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= key_indent:
            break
        m = re.match(r"^\s*-\s*(.*?)\s*$", line)
        if m:
            value = _yaml_unquote(m.group(1))
            if value:
                out.append(value)
    return out


def _yaml_map_keys(path: Path, parent: str) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    start = None
    base_indent = None
    for i, line in enumerate(lines):
        m = re.match(rf"^(\s*){re.escape(parent)}:\s*(?:#.*)?$", line)
        if m:
            start = i + 1
            base_indent = len(m.group(1))
            break
    if start is None or base_indent is None:
        return []
    out: list[str] = []
    child_indent = None
    for line in lines[start:]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= base_indent:
            break
        m = re.match(r"^\s*([A-Za-z0-9_.-]+):", line)
        if not m:
            continue
        if child_indent is None:
            child_indent = indent
        if indent == child_indent:
            out.append(m.group(1))
    return out


def _safe_memory_path(ref: str) -> Path | None:
    ref = _yaml_unquote(ref)
    if not ref or ref.lower() in {"null", "none", "~"}:
        return None
    rel = Path(ref)
    if rel.is_absolute() or ".." in rel.parts:
        return None
    if rel.parts and rel.parts[0] == ".prime":
        path = ROOT / rel
    else:
        path = ROOT / ".prime" / rel
    try:
        path.resolve().relative_to((ROOT / ".prime").resolve())
    except ValueError:
        return None
    return path


def _decision_ref_path(ref: str) -> Path | None:
    path = _safe_memory_path(ref)
    if path is None:
        return None
    if path.parent == ROOT / ".prime":
        path = ROOT / ".prime/decisions" / path.name
    if path.suffix == "":
        path = path.with_suffix(".md")
    return path


def _task_contract_path(ref: str) -> Path | None:
    value = _yaml_unquote(ref).rstrip("/")
    if not value or value.lower() in {"null", "none", "~"}:
        return None
    if value.startswith(".prime/tasks/"):
        value = value[len(".prime/tasks/"):]
    elif value.startswith("tasks/"):
        value = value[len("tasks/"):]
    task_id = value.split("/", 1)[0]
    if not re.fullmatch(r"T-[A-Za-z0-9_.-]+", task_id):
        return None
    return ROOT / ".prime/tasks" / task_id / "contract.yaml"


def _markdown_frontmatter_scalar(path: Path, key: str) -> str | None:
    body = path.read_text(encoding="utf-8")
    if not body.startswith("---\n") or "\n---\n" not in body[4:]:
        return None
    front = body[4:].split("\n---\n", 1)[0]
    for line in front.splitlines():
        m = re.match(rf"^{re.escape(key)}:\s*(.*?)\s*$", line)
        if m:
            return _yaml_unquote(m.group(1))
    return None


def _positive_int(value: str | None) -> int:
    try:
        parsed = int(value or "")
    except ValueError:
        return 0
    return parsed if parsed > 0 else 0


def _completed_task_contract(contract_path: Path) -> bool:
    result_path = contract_path.parent / "result.yaml"
    if not result_path.is_file() or _yaml_scalar(result_path, "status") != "completed":
        return False
    acceptance = set(_yaml_map_keys(contract_path, "acceptance"))
    proved = set(_yaml_list(result_path, "proved"))
    unproved = set(_yaml_list(result_path, "unproved"))
    return bool(acceptance) and acceptance <= proved and not unproved


def _lint_prime_git_tracking(errors: list[str]) -> None:
    prime_root = ROOT / ".prime"
    if not prime_root.is_dir():
        return
    try:
        proc = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z", "--", ".prime"],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        errors.append(f"cannot verify Git-tracked .prime durability: {exc}")
        return
    require(errors, proc.returncode == 0, "cannot verify Git-tracked .prime durability outside a usable Git worktree")
    if proc.returncode != 0:
        return
    tracked = {raw.decode("utf-8", errors="replace") for raw in proc.stdout.split(b"\0") if raw}
    for path in sorted(x for x in prime_root.rglob("*") if x.is_file()):
        rel = path.relative_to(ROOT).as_posix()
        require(errors, rel in tracked, f"canonical runtime memory is not Git-tracked: {rel}")


def lint_runtime_memory(errors: list[str], rev: str, fingerprint: str) -> None:
    state_path = ROOT / ".prime/state.yaml"
    if not state_path.is_file():
        return

    require(errors, state_path.stat().st_size <= 12 * 1024, ".prime/state.yaml exceeds 12 KiB hard cap")
    generation = _positive_int(_yaml_scalar(state_path, "generation"))
    require(errors, generation > 0, ".prime/state.yaml generation must be a positive integer")

    state_rev = _yaml_scalar(state_path, "policy_rev", "governance")
    state_fp = _yaml_scalar(state_path, "fingerprint", "governance")
    require(errors, state_rev == rev, f"live state governance policy_rev stale: {state_rev!r} != {rev!r}")
    require(errors, state_fp == fingerprint, f"live state governance fingerprint stale: {state_fp!r} != {fingerprint!r}")

    roadmap_ref = _yaml_scalar(state_path, "roadmap_ref")
    roadmap_path = None
    if roadmap_ref and roadmap_ref.lower() not in {"null", "none", "~"}:
        roadmap_path = _safe_memory_path(roadmap_ref)
        require(errors, roadmap_path is not None, f"unsafe roadmap_ref: {roadmap_ref}")
        if roadmap_path is not None:
            require(errors, roadmap_path.is_file(), f"dangling roadmap_ref: {roadmap_ref}")
            canonical_roadmap = ROOT / ".prime/decisions/ROADMAP.md"
            require(errors, roadmap_path.resolve() == canonical_roadmap.resolve(), f"roadmap_ref must point to canonical decisions/ROADMAP.md: {roadmap_ref}")
    canonical_roadmap = ROOT / ".prime/decisions/ROADMAP.md"
    if canonical_roadmap.is_file():
        require(errors, roadmap_path is not None and roadmap_path.resolve() == canonical_roadmap.resolve(), "canonical ROADMAP.md exists but state.roadmap_ref does not point to it")

    active_decision_paths: set[Path] = set()
    for ref in _yaml_list(state_path, "active_decisions"):
        decision = _decision_ref_path(ref)
        require(errors, decision is not None and decision.is_file(), f"dangling active_decisions ref: {ref}")
        if decision is not None and decision.is_file():
            status = _markdown_frontmatter_scalar(decision, "status")
            require(errors, status != "superseded", f"active_decisions points to superseded ADR: {ref}")
            active_decision_paths.add(decision.resolve())

    decisions_root = ROOT / ".prime/decisions"
    if decisions_root.is_dir():
        for decision in sorted(decisions_root.rglob("*.md")):
            if decision.resolve() == canonical_roadmap.resolve():
                continue
            if _markdown_frontmatter_scalar(decision, "status") == "active":
                require(errors, decision.resolve() in active_decision_paths, f"orphan active ADR is not discoverable from state.active_decisions: {decision.relative_to(ROOT)}")

    now_ids = _yaml_list(state_path, "now")
    now_set = set(now_ids)
    for task_id in now_ids:
        contract_path = _task_contract_path(task_id)
        require(errors, contract_path is not None and contract_path.is_file(), f"state.now points to missing task contract: {task_id}")

    tasks_root = ROOT / ".prime/tasks"
    if not tasks_root.is_dir():
        require(errors, not now_ids, "state.now is non-empty but .prime/tasks is missing")
        _lint_prime_git_tracking(errors)
        return

    allowed_result_statuses = {"completed", "needs_recontract", "blocked", "failed", "cancelled"}

    for task_dir in sorted(p for p in tasks_root.iterdir() if p.is_dir()):
        contract_path = task_dir / "contract.yaml"
        if not contract_path.is_file():
            continue
        require(errors, contract_path.stat().st_size <= 16 * 1024, f"{contract_path.relative_to(ROOT)} exceeds 16 KiB hard cap")

        task_id = _yaml_scalar(contract_path, "id")
        contract_rev = _positive_int(_yaml_scalar(contract_path, "contract_rev"))
        validated_generation = _positive_int(_yaml_scalar(contract_path, "validated_at_generation"))
        acceptance = set(_yaml_map_keys(contract_path, "acceptance"))
        scope_tags_present = _yaml_scalar(contract_path, "scope_tags") is not None

        result_path = task_dir / "result.yaml"
        result_status = _yaml_scalar(result_path, "status") if result_path.is_file() else None
        # Completed/cancelled contracts remain immutable historical evidence (PRIME §2/§4 live memory preservation)
        historical = result_status in {"completed", "cancelled"}

        require(errors, task_id == task_dir.name, f"task directory/id mismatch: {task_dir.name} vs {task_id!r}")
        require(errors, scope_tags_present, f"{task_dir.relative_to(ROOT)} missing required scope_tags")
        require(errors, contract_rev > 0, f"{task_dir.relative_to(ROOT)} contract_rev must be positive")
        require(errors, validated_generation > 0, f"{task_dir.relative_to(ROOT)} validated_at_generation must be positive")
        if not historical:
            if task_dir.name in now_set:
                require(errors, validated_generation == generation, f"active {task_dir.name} generation stale: {validated_generation} != state generation {generation}")
            require(errors, _yaml_scalar(contract_path, "policy_rev") == rev, f"{task_dir.relative_to(ROOT)} policy_rev stale")
            require(errors, _yaml_scalar(contract_path, "policy_fingerprint") == fingerprint, f"{task_dir.relative_to(ROOT)} policy_fingerprint stale")

        for ref in _yaml_list(contract_path, "decisions", "depends_on"):
            decision = _decision_ref_path(ref)
            require(errors, decision is not None and decision.is_file(), f"{task_dir.name} dangling depends_on.decisions ref: {ref}")
            if decision is not None and decision.is_file():
                status = _markdown_frontmatter_scalar(decision, "status")
                require(errors, status != "superseded", f"{task_dir.name} depends_on superseded decision: {ref}")

        for ref in _yaml_list(contract_path, "tasks", "depends_on"):
            dep = _task_contract_path(ref)
            require(errors, dep is not None and dep.is_file(), f"{task_dir.name} dangling depends_on.tasks ref: {ref}")
            if dep is not None and dep.is_file():
                require(errors, _completed_task_contract(dep), f"{task_dir.name} depends_on task is not completed with all acceptance proved: {ref}")

        progress_path = task_dir / "progress.yaml"
        if progress_path.is_file():
            require(errors, progress_path.stat().st_size <= 8 * 1024, f"{progress_path.relative_to(ROOT)} exceeds 8 KiB hard cap")

        for filename in ("progress.yaml", "result.yaml"):
            handoff = task_dir / filename
            if not handoff.is_file():
                continue
            handoff_task = _yaml_scalar(handoff, "task")
            handoff_rev = _positive_int(_yaml_scalar(handoff, "contract_rev"))
            require(errors, handoff_task == task_id, f"{handoff.relative_to(ROOT)} task does not match current contract")
            require(errors, handoff_rev == contract_rev, f"{handoff.relative_to(ROOT)} contract_rev stale/mismatched")

        if result_path.is_file():
            if not historical:
                result_rev = _yaml_scalar(result_path, "policy_rev", "governance")
                result_fp = _yaml_scalar(result_path, "fingerprint", "governance")
                require(errors, result_rev == rev, f"{result_path.relative_to(ROOT)} governance policy_rev stale")
                require(errors, result_fp == fingerprint, f"{result_path.relative_to(ROOT)} governance fingerprint stale")

            status = _yaml_scalar(result_path, "status")
            require(errors, status in allowed_result_statuses, f"{result_path.relative_to(ROOT)} invalid status: {status!r}")
            proved = set(_yaml_list(result_path, "proved"))
            unproved = set(_yaml_list(result_path, "unproved"))
            require(errors, proved <= acceptance, f"{result_path.relative_to(ROOT)} proved contains unknown acceptance IDs: {sorted(proved - acceptance)}")
            require(errors, unproved <= acceptance, f"{result_path.relative_to(ROOT)} unproved contains unknown acceptance IDs: {sorted(unproved - acceptance)}")
            require(errors, not (proved & unproved), f"{result_path.relative_to(ROOT)} acceptance IDs cannot be both proved and unproved")

            if status == "completed":
                require(errors, bool(acceptance), f"{contract_path.relative_to(ROOT)} has no acceptance IDs")
                require(errors, acceptance <= proved, f"{result_path.relative_to(ROOT)} completed without proving all acceptance IDs")
                require(errors, not unproved, f"{result_path.relative_to(ROOT)} completed but still lists unproved IDs")
                require(errors, task_dir.name not in now_set, f"state.now still contains completed task: {task_dir.name}")
            if status == "cancelled":
                require(errors, task_dir.name not in now_set, f"state.now still contains cancelled task: {task_dir.name}")

    _lint_prime_git_tracking(errors)


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
    require(errors, (ROOT / ".ai/protocols/AIxAI_AGENT_PROTOCOL.md").is_file(), "missing canonical AIxAI protocol")
    require(errors, ".ai/protocols/AIxAI_AGENT_PROTOCOL.md" in prime, "PRIME.md missing canonical AIxAI protocol reference")
    lint_prime_section_refs(errors, prime)

    bootstrap_template = ROOT / ".ai/templates/prime-memory/BOOTSTRAP.md"
    require(errors, not bootstrap_template.exists(), "default BOOTSTRAP template must stay absent; BOOTSTRAP is project-specific only")

    state = text(".ai/templates/prime-memory/state.yaml")
    contract = text(".ai/templates/prime-memory/tasks/T-EXAMPLE/contract.yaml")
    result = text(".ai/templates/prime-memory/tasks/T-EXAMPLE/result.yaml")
    progress = text(".ai/templates/prime-memory/tasks/T-EXAMPLE/progress.yaml")
    journal = text(".ai/templates/prime-memory/journal/EVENT-SCHEMA.jsonl")
    adr = text(".ai/templates/prime-memory/decisions/ADR-TEMPLATE.md")
    roadmap = text(".ai/templates/prime-memory/decisions/ROADMAP-TEMPLATE.md")

    require(errors, "local_commit:" not in state and "remote_commit:" not in state, "state template must not cache Git commit SHAs")
    require(errors, "remote_sync:" not in state and "last_event:" not in state and "persistence:" not in state, "state template must not cache duplicate remote/event truth")
    require(errors, re.search(r"(?m)^generation:\s*[1-9][0-9]*\s*$", state) is not None, "state template missing positive generation")
    require(errors, "roadmap_ref:" in state, "state template missing optional roadmap_ref pointer")
    require(errors, "state.yaml remains the sole owner of current phase/NOW/NEXT" in state, "state template must keep current plan ownership in state.yaml")
    require(errors, "policy_fingerprint:" in contract, "contract template missing policy_fingerprint")
    require(errors, re.search(r"(?m)^contract_rev:\s*[1-9][0-9]*\s*$", contract) is not None, "contract template missing positive contract_rev")
    require(errors, re.search(r"(?m)^scope_tags:\s*", contract) is not None, "contract template missing required scope_tags")
    require(errors, re.search(r"(?m)^validated_at_generation:\s*[1-9][0-9]*\s*$", contract) is not None, "contract template missing positive validated_at_generation")
    require(errors, "created_at_generation:" not in contract, "contract template must use validated_at_generation, not created_at_generation")
    require(errors, "routing: {role:" not in contract and "routing.role" not in contract, "contract template must derive worker from mode, not duplicate routing.role")
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
    require(errors, "# depends_on:" in contract and "decisions: []" in contract and "tasks: []" in contract and "evidence: []" in contract, "contract template missing structured dependency refs")
    require(errors, "affects:" in adr and "superseded_by:" in adr, "ADR template missing impact/supersession metadata")
    require(errors, "state.yaml` owns current phase/NOW/NEXT" in roadmap and "task contracts own worker WHAT" in roadmap, "ROADMAP template must not duplicate hot state/task truth")

    require(errors, '"actor":"human|prime"' in journal, "journal actor must be human|prime")
    require(errors, '"scopes":[]' in journal, "journal schema must carry bounded scopes for Human-change impact")
    for forbidden in ("worker", "task_created", "task_cancelled", "task_completed", "generation_changed", "|other", '"other"'):
        require(errors, forbidden not in journal, f"journal schema contains forbidden routine/garbage event token: {forbidden}")

    for rel in LEGACY_PATHS:
        require(errors, not (ROOT / rel).exists(), f"legacy governance must be retired: {rel}")

    live_state = ROOT / ".prime/state.yaml"
    live_plan = ROOT / ".prime/plan.md"
    migration = ROOT / "PRIME-MIGRATION.md"
    require(errors, not live_plan.exists(), ".prime/plan.md is forbidden duplicate hot-plan truth; use state.yaml + optional decisions/ROADMAP.md")
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
    require(errors, normalize_agent_common_semantics(fast) == normalize_agent_common_semantics(fast_qwen), "worker-fast runtime semantic permissions/rules drifted")
    require(errors, normalize_agent_common_semantics(deep) == normalize_agent_common_semantics(deep_gemini), "worker-deep runtime semantic permissions/rules drifted")

    for rel in (fast, fast_qwen, deep, deep_gemini, ROOT / ".kilo/agents/inspector.md"):
        body = rel.read_text(encoding="utf-8")
        require(errors, "policy_fingerprint" in body, f"{rel.relative_to(ROOT)} must check policy_fingerprint")
    require(errors, "failover_from" not in text(".kilo/agents/worker-deep-gemini.md"), "deep Gemini profile must not require failover metadata in semantic contract")
    deep_fallback = text(".kilo/agents/worker-deep-gemini.md")
    require(errors, "capacity/unavailability" in deep_fallback, "deep fallback must be capacity/unavailability-only")
    require(errors, "fallback_safe: true" in deep_fallback, "deep fallback must require fallback_safe: true")
    require(errors, "unchanged `mode: DEEP`" in deep_fallback, "deep fallback must preserve DEEP as the single logical routing truth")
    require(errors, "routing.fallback_safe: true" in deep_fallback, "deep fallback must require routing.fallback_safe: true")
    require(errors, "runtime/failover metadata does not belong in the contract" in deep_fallback, "deep fallback must keep runtime/failover metadata non-semantic")
    lint_worker_policy_routes(errors, prime, [fast, fast_qwen, deep, deep_gemini])
    require(errors, "worker-fast-qwen" in prime and "fail over once" in prime, "PRIME.md must state explicit bounded fast failover")
    require(errors, "one active delegated writer" in prime.lower() and "isolated worktrees" in prime.lower(), "PRIME.md must enforce one delegated writer per worktree")
    require(errors, "worker outcome claim" in prime, "PRIME.md must distinguish worker outcome claim from accepted project truth")
    require(errors, "contract_rev" in prime, "PRIME.md must bind worker handoffs to contract_rev")
    require(errors, "Human change -> journal source event + scopes -> generation++ -> ADR create/supersede" in prime, "PRIME.md missing deterministic decision-change propagation")
    require(errors, "validated_at_generation == state.generation" in prime, "PRIME.md missing active-task generation barrier")
    require(errors, "scope_tags: []` means unknown/global" in prime, "PRIME.md missing fail-safe empty scope semantics")
    require(errors, "Every contract MUST contain `scope_tags`" in prime, "PRIME.md missing mandatory scope_tags invariant")
    require(errors, "workspace loss is plausible" in prime and "off-machine recovery is REQUIRED" in prime and "never push per task" in prime, "PRIME.md missing conditional off-machine recovery guardrail")
    require(errors, "worker returned without a valid current result matching" in prime and "`INTERRUPTED`, never success" in prime, "PRIME.md missing worker-liveness fail-closed invariant")
    require(errors, "compact technical English" in prime and "no bilingual duplicate" in prime, "PRIME.md missing compact AI-to-AI language invariant")
    require(errors, "Do not store a second editable `routing.role` truth" in prime, "PRIME.md missing mode-only worker routing rule")
    require(errors, "ROADMAP.md" in prime and "state.yaml` owns current phase/NOW/NEXT" in prime, "PRIME.md missing roadmap/state ownership rule")
    require(errors, "Lazy roadmap recovery" in prime and "do **not** preload roadmap content" in prime, "PRIME.md missing lazy roadmap recovery rule")
    require(errors, "roadmap_ref" in prime_agent and "empty/insufficient horizon" in prime_agent, "Prime agent missing hot roadmap dereference trigger")
    require(errors, "workspace loss is plausible" in prime_agent and "off-machine recovery is required" in prime_agent and "never per task" in prime_agent, "Prime agent missing conditional off-machine recovery invariant")
    require(errors, "`INTERRUPTED`, never success" in prime_agent, "Prime agent missing worker-liveness invariant")
    require(errors, "compact technical English" in prime_agent, "Prime agent missing compact coordination language invariant")

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

    production = text(".ai/policies/production.md")
    require(errors, "explicit/minimal target environment" in production and "silently inheriting host variables" in production, "production policy missing explicit target-environment identity guardrail")
    require(errors, "reset, clean, rebase, reclone" in prime and "temporary/reverted artifacts" in prime, "PRIME.md missing restored Git hygiene guardrails")

    diagnosis = text(".ai/policies/diagnosis.md")
    require(errors, "<=3 causally related read-only observations" in diagnosis and "OBSERVABILITY_LIMIT" in diagnosis, "diagnosis policy missing bounded-probe/observability guardrails")
    require(errors, "mechanical non-mutating correction" in diagnosis and "does **not** consume the alternative-method allowance" in diagnosis, "diagnosis policy missing free mechanical read-only correction guardrail")
    require(errors, "MUST NOT be converted into `STATE_MISMATCH` or PASS" in diagnosis, "diagnosis policy must not confuse observation failure with state mismatch")

    evidence = text(".ai/policies/evidence.md")
    require(errors, "`diff --stat` is change metadata, never semantic proof" in evidence, "evidence policy missing diff-stat-not-proof guardrail")

    adversarial = text(".ai/policies/adversarial.md")
    require(errors, "cross-artifact / cross-job substitution" in adversarial and "NaN / Inf" in adversarial and "partial success incorrectly promoted" in adversarial, "adversarial policy missing restored high-value edge cases")

    budget = text(".ai/policies/budget.md")
    require(errors, "Recurring CI efficiency" in budget and ">15-minute gates" in budget and "Do not duplicate equivalent already-accepted expensive evidence" in budget, "budget policy missing recurring expensive-CI efficiency guardrails")
    require(errors, "Do not enable parallel test execution until process, port, cache, global-state, filesystem, and fixture isolation are proven" in budget and "Unknown isolation => run sequentially" in budget, "budget policy missing parallel-test isolation guardrail")
    require(errors, (ROOT / ".ai/templates/prime-memory/BOOTSTRAP-TEMPLATE.md").is_file(), "missing optional BOOTSTRAP recovery template")

    lint_runtime_memory(errors, rev, governance_fingerprint()[:16])
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--fingerprint", action="store_true", help="print 16-hex semantic governance fingerprint only")
    group.add_argument("--full-fingerprint", action="store_true", help="print full semantic SHA-256 governance fingerprint only")
    group.add_argument("--list-files", action="store_true", help="list normalized inputs included in the semantic governance fingerprint")
    group.add_argument("--runtime-only", action="store_true", help="lint live .prime runtime-memory refs/bindings only")
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
    if args.runtime_only:
        rev = text(".ai/POLICY-REV").strip()
        errors: list[str] = []
        lint_runtime_memory(errors, rev, fp[:16])
        if errors:
            for error in errors:
                print(f"ERROR: {error}", file=sys.stderr)
            print(f"runtime-lint: FAIL ({len(errors)} issue(s))", file=sys.stderr)
            return 1
        print("runtime-lint: OK")
        print(f"policy_rev: {rev}")
        print(f"fingerprint: {fp[:16]}")
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
