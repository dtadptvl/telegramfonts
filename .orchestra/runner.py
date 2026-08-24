#!/usr/bin/env python3
"""Deterministic Codex Architect/Executor transport for Issue #55.

This module owns transport mechanics only.  It does not edit contracts or
source files, decide PASS, grant authorization, merge, deploy, or retry a
model call.  The host supplies the bounded contract and workspace.
"""

from __future__ import annotations

import argparse
import copy
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parent
SCHEMA_DIR = ROOT / "schema"
ARCHITECT_SCHEMA = SCHEMA_DIR / "architect.schema.json"
EXECUTOR_SCHEMA = SCHEMA_DIR / "executor.schema.json"
REF_RE = re.compile(r"^(?:SELF|(?:issue|review|pr):[A-Za-z0-9._-]+)$")

ARCHITECT_STATES = frozenset(
    {
        "READY",
        "EXECUTING",
        "ARCHITECT_REVIEW",
        "FIX_REQUIRED",
        "MERGE_READY",
        "MERGED",
        "COMPLETE",
        "HUMAN_AUTH",
        "EXECUTING_AUTHORIZED",
        "BLOCKED",
        "SECURITY_BLOCKED",
    }
)
EXECUTOR_STATUSES = frozenset(
    {
        "DONE",
        "UPDATED",
        "NO_CHANGE",
        "BLOCKED",
        "READY_HUMAN_AUTH",
        "SECURITY_BLOCKED",
    }
)
ARCHITECT_TERMINALS = frozenset(
    {"MERGE_READY", "MERGED", "COMPLETE", "HUMAN_AUTH", "BLOCKED", "SECURITY_BLOCKED"}
)


@dataclass(frozen=True)
class RoleConfig:
    model: str
    effort: str
    sandbox: str


ROLE_CONFIG = {
    "architect": RoleConfig("gpt-5.6-sol", "high", "read-only"),
    "executor": RoleConfig("gpt-5.6-luna", "max", "workspace-write"),
}
WINDOWS_SANDBOX_IMPLEMENTATION = "elevated"


class ProtocolError(Exception):
    """A deterministic fail-closed protocol stop."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class TransportError(ProtocolError):
    """A bounded subprocess or structured-output failure."""


@dataclass(frozen=True)
class InvocationResult:
    payload: dict[str, Any]
    evidence: dict[str, Any]


def _fail(code: str) -> None:
    raise ProtocolError(code)


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return _is_integer(value)
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "number":
        return (isinstance(value, int) and not isinstance(value, bool)) or isinstance(value, float)
    return False


def _resolve_local_ref(root: Mapping[str, Any], ref: str) -> Mapping[str, Any]:
    if not ref.startswith("#/"):
        _fail("schema_external_ref")
    value: Any = root
    for part in ref[2:].split("/"):
        if not isinstance(value, Mapping) or part not in value:
            _fail("schema_ref_missing")
        value = value[part]
    if not isinstance(value, Mapping):
        _fail("schema_ref_not_object")
    return value


def validate_json_schema(value: Any, schema: Mapping[str, Any], path: str = "$", root: Mapping[str, Any] | None = None) -> None:
    """Validate the small JSON-Schema subset used by the checked-in schemas."""

    root = schema if root is None else root
    if "$ref" in schema:
        validate_json_schema(value, _resolve_local_ref(root, str(schema["$ref"])), path, root)
        return

    if "type" in schema:
        expected = schema["type"]
        types = expected if isinstance(expected, list) else [expected]
        if not any(_matches_type(value, str(item)) for item in types):
            _fail(f"schema_type:{path}")

    if "enum" in schema and value not in schema["enum"]:
        _fail(f"schema_enum:{path}")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            _fail(f"schema_min_length:{path}")
        if "pattern" in schema and re.fullmatch(str(schema["pattern"]), value) is None:
            _fail(f"schema_pattern:{path}")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            _fail(f"schema_min_items:{path}")
        if "items" in schema:
            for index, item in enumerate(value):
                validate_json_schema(item, schema["items"], f"{path}[{index}]", root)

    if _is_integer(value):
        if "minimum" in schema and value < int(schema["minimum"]):
            _fail(f"schema_minimum:{path}")
        if "maximum" in schema and value > int(schema["maximum"]):
            _fail(f"schema_maximum:{path}")

    if isinstance(value, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                _fail(f"schema_required:{path}.{key}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            unknown = set(value) - set(properties)
            if unknown:
                _fail(f"schema_additional:{path}.{sorted(unknown)[0]}")
        for key, child_schema in properties.items():
            if key in value:
                validate_json_schema(value[key], child_schema, f"{path}.{key}", root)


def _read_schema(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _fail("schema_unreadable")
    if not isinstance(value, dict):
        _fail("schema_not_object")
    return value


def identity_constrained_schema(role: str, expected_ref: str, expected_head: str | None) -> dict[str, Any]:
    """Clone a role schema and bind its routing identity for one invocation."""

    if role not in {"architect", "executor"}:
        _fail("unknown_role")
    if not isinstance(expected_ref, str) or REF_RE.fullmatch(expected_ref) is None:
        _fail("identity_schema_ref")
    schema_path = ARCHITECT_SCHEMA if role == "architect" else EXECUTOR_SCHEMA
    schema = copy.deepcopy(_read_schema(schema_path))
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        _fail("identity_schema_properties")

    ref_schema = properties.get("ref")
    if not isinstance(ref_schema, dict):
        _fail("identity_schema_ref_property")
    ref_schema["pattern"] = f"^{re.escape(expected_ref)}$"

    if expected_head is None:
        properties["head"] = {"type": "null"}
    else:
        properties["head"] = {
            "type": "string",
            "pattern": f"^{re.escape(expected_head)}$",
        }
    return schema


def _require_keys(value: Mapping[str, Any], required: set[str], allowed: set[str], code: str) -> None:
    if set(value) != allowed:
        missing = required - set(value)
        if missing:
            _fail(f"{code}_missing:{sorted(missing)[0]}")
        _fail(f"{code}_additional:{sorted(set(value) - allowed)[0]}")


def validate_contract_spec(contract: Any, code_prefix: str = "contract") -> dict[str, Any]:
    if not isinstance(contract, dict):
        _fail(f"{code_prefix}_object")
    contract_keys = {"goal", "scope", "accept", "evidence", "budget", "gate", "stop"}
    _require_keys(contract, contract_keys, contract_keys, code_prefix)
    if not isinstance(contract["goal"], str) or not contract["goal"].strip():
        _fail(f"{code_prefix}_goal")
    scope = contract["scope"]
    if not isinstance(scope, dict):
        _fail(f"{code_prefix}_scope")
    scope_keys = {"allowed_paths", "forbidden_paths"}
    _require_keys(scope, scope_keys, scope_keys, f"{code_prefix}_scope")
    for key in scope_keys:
        if not isinstance(scope[key], list) or not scope[key] or not all(
            isinstance(item, str) and item.strip() for item in scope[key]
        ):
            _fail(f"{code_prefix}_{key}")
    for key in ("accept", "evidence", "gate", "stop"):
        if not isinstance(contract[key], list) or not contract[key] or not all(
            isinstance(item, str) and item.strip() for item in contract[key]
        ):
            _fail(f"{code_prefix}_{key}")
    budget = contract["budget"]
    if not isinstance(budget, dict):
        _fail(f"{code_prefix}_budget")
    budget_keys = {"max_calls", "max_handoffs", "timeout_seconds"}
    _require_keys(budget, budget_keys, budget_keys, f"{code_prefix}_budget")
    for key, lower, upper in (
        ("max_calls", 1, 5),
        ("max_handoffs", 1, 4),
        ("timeout_seconds", 1, 300),
    ):
        if not _is_integer(budget[key]) or not lower <= budget[key] <= upper:
            _fail(f"{code_prefix}_{key}")
    return contract


def validate_transport_contract(contract: Any) -> dict[str, Any]:
    """Validate the host envelope around the nested Architect contract."""

    if not isinstance(contract, dict):
        _fail("transport_contract_object")
    spec_keys = {"goal", "scope", "accept", "evidence", "budget", "gate", "stop"}
    allowed = {"ref", "head", *spec_keys}
    _require_keys(contract, allowed, allowed, "transport_contract")
    if not isinstance(contract["ref"], str) or REF_RE.fullmatch(contract["ref"]) is None:
        _fail("contract_ref")
    if not isinstance(contract["head"], (str, type(None))):
        _fail("contract_head")
    validate_contract_spec({key: contract[key] for key in spec_keys})
    return contract


_UNSET = object()


def _validate_ref_head(
    payload: Mapping[str, Any],
    expected_ref: str | None,
    expected_head: str | None | object = _UNSET,
) -> None:
    if not isinstance(payload.get("ref"), str) or REF_RE.fullmatch(payload["ref"]) is None:
        _fail("invalid_ref")
    if expected_ref is not None and payload["ref"] != expected_ref:
        _fail("stale_ref")
    if expected_head is not _UNSET and payload.get("head") != expected_head:
        _fail("stale_head")


def validate_architect(
    payload: Any,
    expected_ref: str | None = None,
    expected_head: str | None | object = _UNSET,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        _fail("architect_object")
    schema = _read_schema(ARCHITECT_SCHEMA)
    validate_json_schema(payload, schema)
    _validate_ref_head(payload, expected_ref, expected_head)
    if payload["review"]["decision"] != payload["state"]:
        _fail("architect_decision_mismatch")
    return payload


def _valid_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        _fail("invalid_changed_path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.parts[0] == ".git":
        _fail("invalid_changed_path")
    return path.as_posix()


def _path_matches(path: str, patterns: Sequence[str]) -> bool:
    return any(path == pattern or fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def validate_executor(
    payload: Any,
    expected_ref: str | None = None,
    expected_head: str | None | object = _UNSET,
    contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        _fail("executor_object")
    schema = _read_schema(EXECUTOR_SCHEMA)
    validate_json_schema(payload, schema)
    _validate_ref_head(payload, expected_ref, expected_head)
    status = payload["status"]
    if status not in EXECUTOR_STATUSES:
        _fail("invalid_executor_status")
    if len(set(payload["changed_files"])) != len(payload["changed_files"]):
        _fail("duplicate_changed_path")
    changed_files = [_valid_relative_path(item) for item in payload["changed_files"]]
    if status in {"BLOCKED", "READY_HUMAN_AUTH", "SECURITY_BLOCKED"}:
        if not isinstance(payload["blocker"], str) or not payload["blocker"].strip():
            _fail("missing_executor_blocker")
    elif payload["blocker"] is not None:
        _fail("unexpected_executor_blocker")
    if status in {"DONE", "UPDATED"} and not changed_files:
        _fail("success_without_changed_file")
    if status == "NO_CHANGE" and changed_files:
        _fail("no_change_with_files")
    if contract is not None:
        allowed = contract["scope"]["allowed_paths"]
        forbidden = contract["scope"]["forbidden_paths"]
        for path in changed_files:
            if not _path_matches(path, allowed) or _path_matches(path, forbidden):
                _fail("executor_scope_escape")
    payload["changed_files"] = changed_files
    return payload


def route_architect(state: str, phase: str, correction_available: bool = False) -> str:
    if phase == "initial":
        if state == "READY":
            return "executor"
        if state in ARCHITECT_TERMINALS:
            return "stop"
        _fail("invalid_initial_transition")
    if phase == "review":
        if state == "FIX_REQUIRED":
            if correction_available:
                return "executor"
            return "stop"
        if state in ARCHITECT_TERMINALS:
            return "stop"
        _fail("invalid_review_transition")
    _fail("invalid_phase")


def route_executor(status: str) -> str:
    if status not in EXECUTOR_STATUSES:
        _fail("invalid_executor_route")
    return "architect_review"


def route_executor_review(executor_status: str, architect_state: str, correction_available: bool) -> str:
    """Apply the compatibility gate between an Executor result and its review."""

    route_executor(executor_status)
    if executor_status == "BLOCKED" and architect_state not in {"BLOCKED", "FIX_REQUIRED"}:
        _fail("executor_review_incompatible")
    if executor_status == "READY_HUMAN_AUTH" and architect_state != "HUMAN_AUTH":
        _fail("executor_human_review_incompatible")
    if executor_status == "SECURITY_BLOCKED" and architect_state != "SECURITY_BLOCKED":
        _fail("executor_security_review_incompatible")
    return route_architect(architect_state, "review", correction_available)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def workspace_snapshot(workspace: Path) -> dict[str, str]:
    if not workspace.is_dir():
        _fail("workspace_missing")
    files: dict[str, str] = {}
    for current, directories, names in os.walk(workspace):
        directories[:] = [name for name in directories if name != ".git"]
        current_path = Path(current)
        for name in names:
            path = current_path / name
            relative = path.relative_to(workspace).as_posix()
            try:
                files[relative] = _sha256_bytes(path.read_bytes())
            except OSError:
                _fail("workspace_snapshot_failed")
    return files


def _git_read(workspace: Path, arguments: Sequence[str]) -> str | None:
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def tracked_workspace_identity(workspace: Path) -> dict[str, Any] | None:
    head = _git_read(workspace, ["rev-parse", "--verify", "HEAD"])
    if not head:
        return None
    status = _git_read(workspace, ["status", "--short", "--untracked-files=all"]) or ""
    tracked_diff = _git_read(workspace, ["diff", "--no-ext-diff", "--binary"]) or ""
    cached_diff = _git_read(workspace, ["diff", "--cached", "--no-ext-diff", "--binary"]) or ""
    return {
        "repository": True,
        "head": head,
        "status_sha256": _sha256_text(status),
        "tracked_diff_sha256": _sha256_text(tracked_diff),
        "cached_diff_sha256": _sha256_text(cached_diff),
    }


def snapshot_diff(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    return sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))


def _extract_event_lines(stdout: str) -> tuple[list[dict[str, Any]], list[str]]:
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    event_types = [str(event["type"]) for event in events if isinstance(event.get("type"), str)]
    return events, event_types


def _text_from_event(event: Mapping[str, Any]) -> str | None:
    item = event.get("item")
    candidates: list[Any] = [event]
    if isinstance(item, Mapping):
        candidates.insert(0, item)
    for candidate in candidates:
        for key in ("text", "output_text"):
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return None


def extract_structured_output(output_file: Path, stdout: str) -> tuple[dict[str, Any], list[str], str]:
    texts: list[tuple[str, str]] = []
    try:
        if output_file.exists():
            value = output_file.read_text(encoding="utf-8").strip()
            if value:
                texts.append((value, "output-last-message"))
    except (OSError, UnicodeError):
        _fail("structured_output_unreadable")
    events, event_types = _extract_event_lines(stdout)
    for event in reversed(events):
        text = _text_from_event(event)
        if text:
            texts.append((text.strip(), "jsonl-event"))
    for text, source in texts:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload, event_types, source
    _fail("structured_output_invalid")


def _event_metadata(stdout: str) -> dict[str, Any]:
    events, event_types = _extract_event_lines(stdout)
    metadata: dict[str, Any] = {"event_types": event_types}
    for event in events:
        for key in ("model", "reasoning_effort", "sandbox", "sandbox_policy", "approval_policy"):
            value = event.get(key)
            if isinstance(value, (str, int, float, bool)):
                metadata[key] = value
        item = event.get("item")
        if isinstance(item, Mapping):
            for key in ("model", "reasoning_effort", "sandbox", "sandbox_policy", "approval_policy"):
                value = item.get(key)
                if isinstance(value, (str, int, float, bool)):
                    metadata[key] = value
    return metadata


def classify_process_failure(stderr: str, stdout: str) -> str:
    """Classify a nonzero CLI result without retaining or exposing raw output."""

    combined = f"{stderr}\n{stdout}".lower()
    parse_markers = (
        "unexpected argument",
        "unexpected option",
        "unknown argument",
        "unknown option",
        "unrecognized option",
        "invalid value",
        "failed to parse",
        "could not parse",
        "config error",
    )
    if any(marker in combined for marker in parse_markers):
        return "cli_parse_or_config_failure"
    return "model_or_runtime_failure"


class CodexTransport:
    """One bounded, non-interactive Codex subprocess per invocation."""

    def __init__(self, command: str | None = None):
        self.command = command or self._find_command()

    @staticmethod
    def _find_command() -> str:
        for candidate in ("codex.cmd", "codex.exe", "codex"):
            found = shutil.which(candidate)
            if found:
                return found
        _fail("codex_cli_not_found")

    def build_command(
        self,
        role: str,
        workspace: Path,
        schema_path: Path,
        output_path: Path,
        prompt: str,
    ) -> list[str]:
        if role not in ROLE_CONFIG:
            _fail("unknown_role")
        config = ROLE_CONFIG[role]
        command = [
            self.command,
            "--ask-for-approval",
            "never",
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
        ]
        if os.name == "nt":
            command.extend(
                ["--config", f"windows.sandbox={json.dumps(WINDOWS_SANDBOX_IMPLEMENTATION)}"]
            )
        command.extend(
            [
                "--json",
                "--color",
                "never",
                "--output-schema",
                str(schema_path),
                "--model",
                config.model,
                "--config",
                f"model_reasoning_effort={json.dumps(config.effort)}",
                "--sandbox",
                config.sandbox,
                "--cd",
                str(workspace),
            ]
        )
        if tracked_workspace_identity(workspace) is None:
            command.append("--skip-git-repo-check")
        command.append(prompt)
        return command

    def invoke(
        self,
        role: str,
        workspace: Path,
        prompt: str,
        timeout_seconds: int,
        expected_ref: str | None = None,
        expected_head: str | None = None,
    ) -> InvocationResult:
        config = ROLE_CONFIG.get(role)
        if config is None:
            _fail("unknown_role")
        if expected_ref is None:
            _fail("identity_required")
        schema = identity_constrained_schema(role, expected_ref, expected_head)
        with tempfile.TemporaryDirectory(prefix="orchestra-transport-") as temporary:
            schema_path = Path(temporary) / f"{role}.json"
            try:
                schema_path.write_text(json.dumps(schema, ensure_ascii=True, sort_keys=True), encoding="utf-8")
            except OSError:
                raise TransportError("identity_schema_write_failure") from None
            output_path = Path(temporary) / "final.json"
            command = self.build_command(role, workspace, schema_path, output_path, prompt)
            environment = os.environ.copy()
            environment["GIT_OPTIONAL_LOCKS"] = "0"
            environment["NO_COLOR"] = "1"
            environment["TERM"] = "dumb"
            try:
                result = subprocess.run(
                    command,
                    cwd=str(workspace),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=environment,
                    timeout=timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                raise TransportError("subprocess_timeout") from None
            except OSError:
                raise TransportError("subprocess_start_failed") from None
            if result.returncode != 0:
                raise TransportError(classify_process_failure(result.stderr, result.stdout))
            payload, event_types, output_source = extract_structured_output(output_path, result.stdout)
            try:
                validate_json_schema(payload, schema)
            except ProtocolError:
                raise TransportError("output_schema_invalid") from None
            metadata = _event_metadata(result.stdout)
            return InvocationResult(
                payload=payload,
                evidence={
                    "model": config.model,
                    "reasoning_effort": config.effort,
                    "sandbox": config.sandbox,
                    "approval_policy": "never",
                    "strict_config": True,
                    "ignore_user_config": True,
                    "windows_sandbox_implementation": (
                        WINDOWS_SANDBOX_IMPLEMENTATION if os.name == "nt" else None
                    ),
                    "exit_code": result.returncode,
                    "schema_valid": True,
                    "output_source": output_source,
                    "event_types": event_types,
                    "observed": metadata,
                },
            )


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _bounded_text(value: Any, code: str) -> str:
    if not isinstance(value, str):
        _fail(code)
    normalized = " ".join(value.split())
    secret_pattern = re.compile(
        r"(?i)\b(?:authorization|bearer|token|secret|password|api[-_]?key)\b"
        r"(?:\s*[:=]\s*|\s+)[^\s,;]+"
    )
    normalized = secret_pattern.sub("[REDACTED]", normalized)
    if len(normalized) > 512:
        suffix = "...[TRUNCATED]"
        normalized = normalized[: 512 - len(suffix)] + suffix
    return normalized


def _bounded_review_delta(review: Mapping[str, Any]) -> dict[str, str]:
    return {
        "decision": str(review["decision"]),
        "summary": _bounded_text(review.get("summary"), "architect_review_summary"),
    }


def _bounded_executor_delta(payload: Mapping[str, Any]) -> dict[str, Any]:
    blocker = payload.get("blocker")
    return {
        "status": str(payload["status"]),
        "summary": _bounded_text(payload.get("summary"), "executor_summary"),
        "blocker": None if blocker is None else _bounded_text(blocker, "executor_blocker"),
        "changed_files": list(payload["changed_files"]),
    }


def architect_prompt(
    contract: Mapping[str, Any],
    stage: str,
    executor_event: Mapping[str, Any] | None = None,
) -> str:
    contract_json = _compact(contract)
    if stage == "initial":
        return (
            "You are the Architect in a bounded local Codex orchestration smoke. "
            "Read the current workspace and assess the host contract honestly, but do not edit "
            "anything. The host has already validated the following authoritative contract; use "
            "it as input, do not modify it, reconstruct it, or echo it in your output. "
            + " Return exactly one JSON object matching the Architect schema: no Markdown, prose, "
            "footer, or extra keys. Emit only state, ref, head, and review. Preserve the supplied "
            "identity exactly. The review decision must equal state and its summary must be only "
            "the bounded decision/action delta. Emit READY only if the bounded local action is "
            "executable; otherwise emit the semantically correct terminal state. Do not invent "
            "evidence or authorize, merge, deploy, or repair.\n\nHOST CONTRACT JSON:\n"
            + contract_json
        )
    return (
        "You are the Architect reviewing one bounded local Executor event. Read the current "
        "workspace and compare the observed change with the following authoritative host contract. "
        "Use it as input, do not modify it, reconstruct it, or echo it in your output. "
        + " Return exactly one JSON object matching the Architect schema: no Markdown, prose, "
        "footer, or extra keys. Emit only state, ref, head, and review. Preserve the supplied "
        "identity exactly. The review decision must equal state and its summary must be only the "
        "bounded decision/action delta. Emit MERGE_READY only when the host contract and evidence "
        "are actually satisfied; otherwise emit FIX_REQUIRED, BLOCKED, HUMAN_AUTH, or "
        "SECURITY_BLOCKED as applicable. Do not edit, authorize, merge, deploy, repair, or invent "
        "evidence.\n\nHOST CONTRACT JSON:\n"
        + contract_json
        + "\n\nEXECUTOR JSON:\n"
        + _compact(executor_event or {})
    )


def executor_prompt(contract: Mapping[str, Any], correction: Mapping[str, Any] | None = None) -> str:
    prompt = (
        "You are the Executor in a bounded local Codex orchestration smoke. Read the current "
        "workspace and execute only the supplied contract. Use workspace-write only for the "
        "allowed paths. Do not edit the contract, policies, schemas, runner, or .git; do not "
        "commit, push, merge, deploy, call production, or create credentials. If the exact "
        "contract cannot be completed, return BLOCKED with truthful evidence instead of claiming "
        "success. This is a tiny bounded action: do not inspect unrelated files or run repo-wide "
        "tests. Use the minimum commands needed, verify only the stated acceptance criteria, then "
        "immediately return exactly one JSON object matching the Executor schema: no Markdown, "
        "prose, footer, or extra keys.\n\nCONTRACT JSON:\n"
        + _compact(contract)
    )
    if correction is not None:
        prompt += "\n\nARCHITECT REVIEW DELTA JSON:\n" + _compact(correction)
    return prompt


class DeterministicRunner:
    """A finite state transport with explicit correction and gate stops."""

    def __init__(
        self,
        contract: Mapping[str, Any],
        workspace: Path,
        transport: Any | None = None,
    ):
        self.contract = validate_transport_contract(copy.deepcopy(dict(contract)))
        self.workspace = workspace.resolve()
        self.transport = transport or CodexTransport()
        self.calls = 0
        self.handoffs = 0
        self.correction_used = False
        self.seen_transitions: set[tuple[str, str, str, str | None]] = set()
        self.trace: list[dict[str, Any]] = []
        self.invocations: list[dict[str, Any]] = []
        self.isolations: list[dict[str, Any]] = []
        self.preflight: dict[str, Any] = {}

    @property
    def ref(self) -> str:
        return str(self.contract.get("ref", "issue:55"))

    def _budget(self, key: str) -> int:
        return int(self.contract["budget"][key])

    def _handoff(self) -> None:
        self.handoffs += 1
        if self.handoffs > self._budget("max_handoffs"):
            _fail("handoff_budget_exhausted")

    def _preflight_workspace_identity(self) -> None:
        expected_head = self.contract.get("head")
        identity = tracked_workspace_identity(self.workspace)
        actual_head = identity.get("head") if identity is not None else None
        self.preflight = {
            "contract_head": expected_head,
            "workspace_head": actual_head,
            "tracked_git": identity is not None,
            "required": expected_head is not None,
            "matched": expected_head is None or actual_head == expected_head,
        }
        if expected_head is None:
            return
        if identity is None:
            _fail("workspace_identity_missing")
        if actual_head != expected_head:
            _fail("workspace_head_mismatch")

    def _invoke(self, role: str, prompt: str) -> InvocationResult:
        if self.calls >= self._budget("max_calls"):
            _fail("call_budget_exhausted")
        self.calls += 1
        try:
            result = self.transport.invoke(
                role,
                self.workspace,
                prompt,
                self._budget("timeout_seconds"),
                expected_ref=self.ref,
                expected_head=self.contract.get("head"),
            )
        except TransportError as error:
            config = ROLE_CONFIG[role]
            self.invocations.append(
                {
                    "call": self.calls,
                    "role": role,
                    "model": config.model,
                    "reasoning_effort": config.effort,
                    "sandbox": config.sandbox,
                    "approval_policy": "never",
                    "strict_config": True,
                    "ignore_user_config": True,
                    "exit_code": "nonzero_or_bounded_failure",
                    "schema_valid": False,
                    "transport_error": error.code,
                }
            )
            raise
        except ProtocolError:
            raise
        except Exception:
            raise TransportError("transport_failed") from None
        evidence = result.evidence if isinstance(result.evidence, dict) else {}
        self.invocations.append(
            {
                "call": self.calls,
                "role": role,
                "model": evidence.get("model", ROLE_CONFIG[role].model),
                "reasoning_effort": evidence.get("reasoning_effort", ROLE_CONFIG[role].effort),
                "sandbox": evidence.get("sandbox", ROLE_CONFIG[role].sandbox),
                "approval_policy": evidence.get("approval_policy", "never"),
                "strict_config": bool(evidence.get("strict_config", True)),
                "ignore_user_config": bool(evidence.get("ignore_user_config", True)),
                "exit_code": evidence.get("exit_code", 0),
                "schema_valid": bool(evidence.get("schema_valid", True)),
                "output_source": evidence.get("output_source", "transport"),
                "event_types": list(evidence.get("event_types", [])),
                "observed": dict(evidence.get("observed", {})),
            }
        )
        return result

    def _record(self, role: str, payload: Mapping[str, Any], changed_files: Sequence[str] = ()) -> None:
        field = "state" if role == "architect" else "status"
        value = str(payload[field])
        key = (role, value, str(payload["ref"]), payload.get("head"))
        if key in self.seen_transitions:
            _fail("duplicate_or_no_progress")
        self.seen_transitions.add(key)
        self.trace.append(
            {
                "actor": role,
                "state_or_status": value,
                "ref": payload["ref"],
                "head": payload.get("head"),
                "changed_files": list(changed_files),
            }
        )
        if role == "architect":
            self.trace[-1]["review"] = _bounded_review_delta(payload["review"])
        else:
            self.trace[-1]["executor_delta"] = _bounded_executor_delta(payload)

    def _architect_call(self, stage: str, executor_event: Mapping[str, Any] | None = None) -> dict[str, Any]:
        before_files = workspace_snapshot(self.workspace)
        before_identity = tracked_workspace_identity(self.workspace)
        try:
            result = self._invoke("architect", architect_prompt(self.contract, stage, executor_event))
        except ProtocolError:
            after_files = workspace_snapshot(self.workspace)
            after_identity = tracked_workspace_identity(self.workspace)
            if before_files != after_files or before_identity != after_identity:
                _fail("architect_mutated_workspace")
            raise
        after_files = workspace_snapshot(self.workspace)
        after_identity = tracked_workspace_identity(self.workspace)
        if before_files != after_files or before_identity != after_identity:
            _fail("architect_mutated_workspace")
        payload = validate_architect(
            result.payload,
            expected_ref=self.ref,
            expected_head=self.contract.get("head"),
        )
        self.isolations.append(
            {
                "stage": stage,
                "role": "architect",
                "unchanged": True,
                "before": before_identity,
                "after": after_identity,
                "file_count_before": len(before_files),
                "file_count_after": len(after_files),
            }
        )
        self._record("architect", payload)
        return payload

    def _executor_call(self, correction: Mapping[str, Any] | None = None) -> dict[str, Any]:
        before_files = workspace_snapshot(self.workspace)
        before_identity = tracked_workspace_identity(self.workspace)
        try:
            result = self._invoke("executor", executor_prompt(self.contract, correction))
        except ProtocolError:
            after_files = workspace_snapshot(self.workspace)
            after_identity = tracked_workspace_identity(self.workspace)
            if before_identity and after_identity and (
                before_identity["head"] != after_identity["head"]
                or before_identity["cached_diff_sha256"] != after_identity["cached_diff_sha256"]
            ):
                _fail("executor_touched_git")
            if before_files != after_files:
                _fail("executor_changed_on_failed_call")
            raise
        after_files = workspace_snapshot(self.workspace)
        after_identity = tracked_workspace_identity(self.workspace)
        if before_identity and after_identity:
            if before_identity["head"] != after_identity["head"]:
                _fail("executor_changed_head")
            if before_identity["cached_diff_sha256"] != after_identity["cached_diff_sha256"]:
                _fail("executor_touched_git")
        payload = validate_executor(
            result.payload,
            expected_ref=self.ref,
            expected_head=self.contract.get("head"),
            contract=self.contract,
        )
        actual_changes = snapshot_diff(before_files, after_files)
        reported_changes = sorted(payload["changed_files"])
        if actual_changes != reported_changes:
            _fail("executor_change_report_mismatch")
        isolation = {
                "stage": "executor_correction" if correction is not None else "executor",
                "role": "executor",
                "before": before_identity,
                "after": after_identity,
                "changed_files": actual_changes,
                "allowed": True,
                "executor_delta": _bounded_executor_delta(payload),
            }
        if correction is not None:
            isolation["architect_review"] = _bounded_review_delta(correction)
        self.isolations.append(isolation)
        self._record("executor", payload, actual_changes)
        return payload

    def report(self, terminal: str, reason: str) -> dict[str, Any]:
        return {
            "protocol": "orchestra/v1",
            "terminal": terminal,
            "reason": reason,
            "calls": self.calls,
            "handoffs": self.handoffs,
            "correction_used": self.correction_used,
            "preflight": self.preflight,
            "trace": self.trace,
            "invocations": self.invocations,
            "isolation": self.isolations,
        }

    def run(self) -> dict[str, Any]:
        self._preflight_workspace_identity()
        initial = self._architect_call("initial")
        initial_route = route_architect(initial["state"], "initial")
        if initial_route == "stop":
            return self.report(initial["state"], "architect_initial_terminal")

        self._handoff()
        executor_event = self._executor_call()
        route_executor(executor_event["status"])
        self._handoff()
        review = self._architect_call("review", executor_event)
        review_route = route_executor_review(executor_event["status"], review["state"], correction_available=True)
        if review_route == "executor":
            self.correction_used = True
            self._handoff()
            corrected_event = self._executor_call(review["review"])
            route_executor(corrected_event["status"])
            self._handoff()
            review = self._architect_call("rereview", corrected_event)
            if review["state"] == "FIX_REQUIRED":
                return self.report("STOP", "correction_budget_exhausted")
            route_executor_review(corrected_event["status"], review["state"], correction_available=False)
        return self.report(review["state"], "architect_review_terminal")


def _load_contract(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _fail("contract_unreadable")
    if not isinstance(value, dict):
        _fail("contract_object")
    if not isinstance(value.get("ref"), str) or REF_RE.fullmatch(value["ref"]) is None:
        _fail("contract_ref")
    if not isinstance(value.get("head"), (str, type(None))):
        _fail("contract_head")
    return validate_transport_contract(value)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one bounded Codex Architect/Executor transport")
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--codex", default=None, help="Optional codex executable path")
    args = parser.parse_args(argv)
    runner: DeterministicRunner | None = None
    try:
        contract = _load_contract(args.contract.resolve())
        transport = CodexTransport(args.codex)
        runner = DeterministicRunner(contract, args.workspace, transport)
        result = runner.run()
    except ProtocolError as error:
        if runner is not None:
            result = runner.report("STOP", error.code)
        else:
            result = {
                "protocol": "orchestra/v1",
                "terminal": "STOP",
                "reason": error.code,
            }
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
