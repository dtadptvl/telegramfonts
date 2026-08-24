#!/usr/bin/env python3
"""Bounded Executor-only GitHub/local event handoff.

This module is the active Issue #57 host boundary. It discovers and validates
the canonical GitHub contract before one Luna call, atomically claims the
derived event locally, checks a durable GitHub marker, validates the result,
publishes reported workspace changes, and records a concise GitHub report.
The host never asks Luna to query or mutate GitHub and never decides project
PASS, authorization, merge, deploy, or production state.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import quote


ROOT = Path(__file__).resolve().parent
EXECUTOR_SCHEMA = ROOT / "schema" / "executor.schema.json"
PROTOCOL = "orchestra/event/v1"
RESULT_PROTOCOL = "orchestra/executor/v1"
REPOSITORY = "dtadptvl/telegramfonts"
EXECUTE_LABEL = "orchestra:execute"
REVIEW_LABEL = "orchestra:review"
HUMAN_LABEL = "orchestra:human"
DONE_LABEL = "orchestra:done"
CANONICAL_LABELS = frozenset({EXECUTE_LABEL, REVIEW_LABEL, HUMAN_LABEL, DONE_LABEL})
EVENT_MARKER_PREFIX = "<!-- orchestra:executor:v1"
LUNA_MODEL = "gpt-5.6-luna"
LUNA_EFFORT = "max"
LUNA_SANDBOX = "workspace-write"
WINDOWS_SANDBOX_IMPLEMENTATION = "elevated"
MAX_CORRECTIONS = 1
MAX_TIMEOUT_SECONDS = 300
MAX_EVENT_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_TEXT_LENGTH = 512
EVENT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
CANONICAL_REF_RE = re.compile(r"^(?:SELF|(?:issue|review|comment|pr):[A-Za-z0-9._-]+)$")
ARCHITECT_STATE_RE = re.compile(
    r"(?im)^\s*ARCHITECT\s*\|\s*(READY|FIX_REQUIRED)\s*\r?\n"
    r"\s*REF\s*:\s*([^\s]+)\s*$"
)
ARCHITECT_ANY_STATE_RE = re.compile(
    r"(?im)^\s*ARCHITECT\s*\|\s*([A-Z][A-Z_]*)\s*\r?\n"
    r"\s*REF\s*:\s*([^\s]+)\s*$"
)
HEAD_RE = re.compile(r"(?im)^\s*(?:HEAD|PR_HEAD)\s*:\s*([^\s]+)\s*$")
PR_RE = re.compile(r"(?im)^\s*(?:ACTIVE_PR|PR)\s*:\s*#?([0-9]+)\s*$")
EXECUTOR_STATUSES = frozenset(
    {"DONE", "UPDATED", "NO_CHANGE", "BLOCKED", "READY_HUMAN_AUTH", "SECURITY_BLOCKED"}
)
STOP_STATUSES = frozenset({"BLOCKED", "READY_HUMAN_AUTH", "SECURITY_BLOCKED"})
GITHUB_CREDENTIAL_ENV_NAMES = frozenset(
    {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "GITHUB_APP_PEM",
        "GITHUB_APP_PRIVATE_KEY",
        "GITHUB_APP_INSTALLATION_TOKEN",
        "ACTIONS_RUNTIME_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_URL",
    }
)


def scrub_github_credentials(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build a Luna environment with GitHub/ACTIONS credentials absent."""

    source = dict(os.environ if environment is None else environment)
    scrubbed: dict[str, str] = {}
    for key, value in source.items():
        upper = key.upper()
        if (
            upper in GITHUB_CREDENTIAL_ENV_NAMES
            or upper.startswith("GH_")
            or upper.startswith("GITHUB_")
            or upper.startswith("ACTIONS_ID_TOKEN_")
            or upper == "ACTIONS_RUNTIME_TOKEN"
        ):
            continue
        scrubbed[key] = value
    return scrubbed


class HandoffError(Exception):
    """A deterministic fail-closed handoff stop."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _fail(code: str) -> None:
    raise HandoffError(code)


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _bounded_text(value: Any, code: str = "invalid_text") -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(code)
    normalized = " ".join(value.split())
    secret_pattern = re.compile(
        r"(?i)\b(?:authorization|bearer|token|secret|password|api[-_]?key)\b"
        r"(?:\s*[:=]\s*|\s+)[^\s,;]+"
    )
    normalized = secret_pattern.sub("[REDACTED]", normalized)
    normalized = re.sub(
        r"(?i)\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b",
        "[REDACTED]",
        normalized,
    )
    if len(normalized) > MAX_TEXT_LENGTH:
        suffix = "...[TRUNCATED]"
        normalized = normalized[: MAX_TEXT_LENGTH - len(suffix)] + suffix
    return normalized


def _valid_issue_number(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2_147_483_647:
        _fail("invalid_issue_number")
    return value


def _valid_event_id(value: Any) -> str:
    if not isinstance(value, str) or EVENT_ID_RE.fullmatch(value) is None:
        _fail("invalid_event_id")
    return value


def _validate_repository(value: Any, expected: str) -> str:
    if not isinstance(value, str) or value != expected:
        _fail("invalid_repository")
    return value


def _validate_correction(value: Any, correction_of: str | None) -> dict[str, str] | None:
    if correction_of is None:
        if value is not None:
            _fail("unexpected_correction")
        return None
    _valid_event_id(correction_of)
    if not isinstance(value, dict) or set(value) != {"summary"}:
        _fail("invalid_correction")
    return {"summary": _bounded_text(value["summary"], "invalid_correction_summary")}


def _event_digest(parts: Mapping[str, Any]) -> str:
    return hashlib.sha256(_compact(parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class HandoffEvent:
    event_id: str
    source: str
    repository: str
    issue_number: int
    action: str
    label: str
    correction_of: str | None = None
    correction: dict[str, str] | None = None
    canonical_ref: str | None = None
    main_head: str | None = None
    pr_head: str | None = None
    architect_state: str | None = None

    @property
    def ref(self) -> str:
        return self.canonical_ref or f"issue:{self.issue_number}"

    @property
    def correction_depth(self) -> int:
        return 1 if self.correction_of is not None else 0

    def as_prompt_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "protocol": PROTOCOL,
            "source": self.source,
            "event_id": self.event_id,
            "repository": self.repository,
            "issue_number": self.issue_number,
            "action": self.action,
            "label": self.label,
        }
        if self.correction_of is not None:
            data["correction_of"] = self.correction_of
            data["correction"] = self.correction
        if self.canonical_ref is not None:
            data["architect_ref"] = self.canonical_ref
        if self.architect_state is not None:
            data["architect_state"] = self.architect_state
        if self.main_head is not None:
            data["main_head"] = self.main_head
        if self.pr_head is not None:
            data["pr_head"] = self.pr_head
        return data


def parse_github_event(payload: Any, expected_repository: str = REPOSITORY) -> HandoffEvent:
    """Convert a native GitHub ``issues:labeled`` payload to a stable event."""

    if not isinstance(payload, dict):
        _fail("github_event_object")
    if payload.get("action") != "labeled":
        _fail("unsupported_github_action")
    issue = payload.get("issue")
    label = payload.get("label")
    repository = payload.get("repository")
    if not isinstance(issue, dict) or not isinstance(label, dict) or not isinstance(repository, dict):
        _fail("github_event_shape")
    full_name = _validate_repository(repository.get("full_name"), expected_repository)
    issue_number = _valid_issue_number(issue.get("number"))
    label_name = label.get("name")
    if label_name != EXECUTE_LABEL:
        _fail("ignored_label")
    label_id = label.get("id")
    issue_id = issue.get("id")
    event_id = _event_digest(
        {
            "action": "labeled",
            "issue_id": issue_id,
            "issue_number": issue_number,
            "issue_updated_at": issue.get("updated_at"),
            "label_id": label_id,
            "label": EXECUTE_LABEL,
            "repository": full_name,
        }
    )
    return HandoffEvent(
        event_id=event_id,
        source="github",
        repository=full_name,
        issue_number=issue_number,
        action="labeled",
        label=EXECUTE_LABEL,
    )


def parse_local_event(payload: Any, expected_repository: str = REPOSITORY) -> HandoffEvent:
    """Validate a compact Desktop/local event without accepting a contract."""

    if not isinstance(payload, dict):
        _fail("local_event_object")
    required = {"protocol", "source", "event_id", "repository", "issue_number", "action", "label"}
    allowed = required | {"correction_of", "correction"}
    if set(payload) - allowed or not required.issubset(payload):
        _fail("local_event_shape")
    if payload["protocol"] != PROTOCOL or payload["source"] != "local":
        _fail("local_event_identity")
    event_id = _valid_event_id(payload["event_id"])
    repository = _validate_repository(payload["repository"], expected_repository)
    issue_number = _valid_issue_number(payload["issue_number"])
    if payload["action"] != "labeled" or payload["label"] != EXECUTE_LABEL:
        _fail("invalid_local_action")
    correction_of = payload.get("correction_of")
    if correction_of is not None and not isinstance(correction_of, str):
        _fail("invalid_correction_reference")
    correction = _validate_correction(payload.get("correction"), correction_of)
    return HandoffEvent(
        event_id=event_id,
        source="local",
        repository=repository,
        issue_number=issue_number,
        action="labeled",
        label=EXECUTE_LABEL,
        correction_of=correction_of,
        correction=correction,
    )


def read_json_file(path: Path, max_bytes: int = MAX_EVENT_BYTES) -> Any:
    try:
        if path.stat().st_size > max_bytes:
            _fail("event_too_large")
        return json.loads(path.read_text(encoding="utf-8"))
    except HandoffError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        _fail("event_unreadable")


def _valid_sha(value: Any, code: str = "invalid_head") -> str:
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        _fail(code)
    return value.lower()


def _valid_canonical_ref(value: Any) -> str:
    if not isinstance(value, str) or CANONICAL_REF_RE.fullmatch(value) is None:
        _fail("invalid_canonical_ref")
    return value


def _issue_number_from_ref(ref: str) -> int | None:
    if not ref.startswith("issue:"):
        return None
    suffix = ref.split(":", 1)[1]
    return int(suffix) if suffix.isdigit() else None


def _number(value: Any, code: str = "invalid_github_number") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _fail(code)
    return value


@dataclass(frozen=True)
class CanonicalArtifact:
    state: str
    ref: str
    head: str | None
    head_present: bool
    pr_number: int | None
    source: str
    ordinal: int


def _artifact_metadata(text: str, start: int, end: int) -> tuple[str | None, bool, int | None]:
    section = text[start:end]
    head_match = HEAD_RE.search(section)
    head_present = head_match is not None
    head: str | None = None
    if head_match:
        token = head_match.group(1).strip()
        if token.lower() not in {"none", "null"}:
            head = _valid_sha(token, "invalid_architect_head")

    pr_values = [int(match.group(1)) for match in PR_RE.finditer(section)]
    if len(set(pr_values)) > 1:
        _fail("ambiguous_pr_ref")
    return head, head_present, (pr_values[0] if pr_values else None)


def parse_canonical_artifacts(text: Any, source: str, ordinal_base: int = 0) -> list[CanonicalArtifact]:
    """Parse only canonical Architect envelopes; prose is never a contract."""

    if not isinstance(text, str):
        return []
    matches = list(ARCHITECT_ANY_STATE_RE.finditer(text))
    artifacts: list[CanonicalArtifact] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        state = match.group(1).upper()
        ref = _valid_canonical_ref(match.group(2))
        head, head_present, pr_number = _artifact_metadata(text, match.start(), end)
        ref_pr = int(ref.split(":", 1)[1]) if ref.startswith("pr:") and ref.split(":", 1)[1].isdigit() else None
        if ref_pr is not None:
            if pr_number is not None and pr_number != ref_pr:
                _fail("ambiguous_pr_ref")
            pr_number = ref_pr
        artifacts.append(
            CanonicalArtifact(
                state=state,
                ref=ref,
                head=head,
                head_present=head_present,
                pr_number=pr_number,
                source=source,
                ordinal=ordinal_base + index,
            )
        )
    return artifacts


@dataclass(frozen=True)
class GitHubContract:
    repository: str
    issue_number: int
    architect_state: str
    architect_ref: str
    architect_head: str | None
    main_head: str
    pr_number: int | None
    pr_head: str | None
    pr_ref: str | None
    issue_body: str
    comments: tuple[Mapping[str, Any], ...]
    event_key: str
    execute_label_present: bool = True

    @property
    def expected_executor_head(self) -> str:
        return self.pr_head or self.main_head


def _comment_sort_key(comment: Mapping[str, Any], index: int) -> tuple[str, int, int]:
    timestamp = comment.get("created_at") or comment.get("createdAt") or comment.get("updated_at") or ""
    value = comment.get("id", index)
    numeric_id = value if isinstance(value, int) else index
    return (str(timestamp), numeric_id, index)


def _issue_body(issue: Mapping[str, Any]) -> str:
    body = issue.get("body")
    return body if isinstance(body, str) else ""


def _pull_number_from_issue(issue: Mapping[str, Any]) -> int | None:
    for key in ("pr_number", "pull_request_number", "active_pr"):
        value = issue.get(key)
        if value is None:
            continue
        if isinstance(value, Mapping):
            value = value.get("number")
        if isinstance(value, str) and value.startswith("#"):
            value = value[1:]
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, str) and value.isdigit() and int(value) > 0:
            return int(value)
        _fail("invalid_pr_ref")
    return None


def _pull_number_from_mapping(pull: Mapping[str, Any]) -> int:
    value = pull.get("number")
    return _number(value, "invalid_pr_ref")


def _pull_head(pull: Mapping[str, Any]) -> str:
    head = pull.get("head")
    if isinstance(head, Mapping):
        head = head.get("sha")
    return _valid_sha(head, "invalid_pr_head")


def _pull_ref(pull: Mapping[str, Any]) -> str:
    head = pull.get("head")
    ref = head.get("ref") if isinstance(head, Mapping) else pull.get("head_ref")
    if (
        not isinstance(ref, str)
        or not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", ref)
        or ref.startswith(("-", "/"))
        or ref.endswith(("/", "."))
        or ".." in ref
        or "//" in ref
        or ref == "main"
    ):
        _fail("invalid_pr_branch")
    return ref


def derive_event_key(
    repository: str,
    issue_number: int,
    architect_ref: str,
    main_head: str,
    pr_head: str | None,
) -> str:
    """Derive the durable key from canonical refs and live heads only."""

    _validate_repository(repository, REPOSITORY if repository == REPOSITORY else repository)
    _valid_issue_number(issue_number)
    _valid_canonical_ref(architect_ref)
    _valid_sha(main_head)
    if pr_head is not None:
        _valid_sha(pr_head)
    digest = _event_digest(
        {
            "repository": repository,
            "issue_number": issue_number,
            "architect_ref": architect_ref,
            "main_head": main_head.lower(),
            "pr_head": pr_head.lower() if pr_head else None,
        }
    )
    return f"github-{digest}"


class GitHubAdapter(Protocol):
    """Host-only GitHub operations; this interface is replaced by fakes in UNIT tests."""

    def list_open_execute_issues(self, repository: str) -> Sequence[Mapping[str, Any]]: ...

    def get_issue(self, repository: str, issue_number: int) -> Mapping[str, Any]: ...

    def list_issue_comments(self, repository: str, issue_number: int) -> Sequence[Mapping[str, Any]]: ...

    def get_repository(self, repository: str) -> Mapping[str, Any]: ...

    def get_branch_head(self, repository: str, branch: str) -> str: ...

    def get_pull_request(self, repository: str, pull_number: int) -> Mapping[str, Any]: ...

    def list_open_pull_requests(self, repository: str) -> Sequence[Mapping[str, Any]]: ...

    def post_issue_comment(self, repository: str, issue_number: int, body: str) -> Mapping[str, Any]: ...

    def remove_issue_label(self, repository: str, issue_number: int, label: str) -> None: ...

    def add_issue_label(self, repository: str, issue_number: int, label: str) -> None: ...

    def create_pull_request(self, repository: str, title: str, body: str, head: str, base: str) -> Mapping[str, Any]: ...


class GitHubClient:
    """Minimal ``gh api`` client. Credentials stay in this host process."""

    def __init__(self, command: str | None = None, timeout_seconds: int = 20):
        self.command = command or shutil.which("gh")
        self.timeout_seconds = timeout_seconds
        if not self.command:
            _fail("github_cli_not_found")

    def _api(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        payload: Mapping[str, Any] | None = None,
        paginate: bool = False,
    ) -> Any:
        command = [str(self.command), "api", endpoint, "--method", method]
        if paginate:
            command.extend(["--paginate", "--slurp"])
        input_text = None
        if payload is not None:
            command.extend(["--input", "-"])
            input_text = _compact(dict(payload))
        environment = os.environ.copy()
        environment["GH_PAGER"] = "cat"
        environment["NO_COLOR"] = "1"
        try:
            result = subprocess.run(
                command,
                input=input_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            _fail("github_api_failed")
        if result.returncode != 0:
            _fail("github_api_failed")
        try:
            return json.loads(result.stdout) if result.stdout.strip() else None
        except json.JSONDecodeError:
            _fail("github_api_invalid_json")

    @staticmethod
    def _pages(value: Any) -> list[Any]:
        if not isinstance(value, list):
            _fail("github_api_invalid_page")
        pages: list[Any] = []
        for page in value:
            if not isinstance(page, list):
                _fail("github_api_invalid_page")
            pages.extend(page)
        return pages

    def list_open_execute_issues(self, repository: str) -> Sequence[Mapping[str, Any]]:
        value = self._api(
            f"repos/{repository}/issues?state=open&labels={EXECUTE_LABEL}&per_page=100",
            paginate=True,
        )
        issues = self._pages(value)
        return [item for item in issues if isinstance(item, Mapping) and "pull_request" not in item]

    def get_issue(self, repository: str, issue_number: int) -> Mapping[str, Any]:
        value = self._api(f"repos/{repository}/issues/{issue_number}")
        if not isinstance(value, Mapping):
            _fail("github_issue_invalid")
        return value

    def list_issue_comments(self, repository: str, issue_number: int) -> Sequence[Mapping[str, Any]]:
        value = self._api(f"repos/{repository}/issues/{issue_number}/comments?per_page=100", paginate=True)
        comments = self._pages(value)
        return [item for item in comments if isinstance(item, Mapping)]

    def get_repository(self, repository: str) -> Mapping[str, Any]:
        value = self._api(f"repos/{repository}")
        if not isinstance(value, Mapping):
            _fail("github_repository_invalid")
        return value

    def get_branch_head(self, repository: str, branch: str) -> str:
        value = self._api(f"repos/{repository}/git/ref/heads/{branch}")
        if not isinstance(value, Mapping) or not isinstance(value.get("object"), Mapping):
            _fail("github_branch_invalid")
        return _valid_sha(value["object"].get("sha"), "github_branch_invalid")

    def get_pull_request(self, repository: str, pull_number: int) -> Mapping[str, Any]:
        value = self._api(f"repos/{repository}/pulls/{pull_number}")
        if not isinstance(value, Mapping):
            _fail("github_pr_invalid")
        return value

    def list_open_pull_requests(self, repository: str) -> Sequence[Mapping[str, Any]]:
        value = self._api(f"repos/{repository}/pulls?state=open&per_page=100", paginate=True)
        pulls = self._pages(value)
        return [item for item in pulls if isinstance(item, Mapping)]

    def post_issue_comment(self, repository: str, issue_number: int, body: str) -> Mapping[str, Any]:
        value = self._api(f"repos/{repository}/issues/{issue_number}/comments", method="POST", payload={"body": body})
        if not isinstance(value, Mapping):
            _fail("github_comment_failed")
        return value

    def remove_issue_label(self, repository: str, issue_number: int, label: str) -> None:
        encoded = quote(label, safe="")
        self._api(f"repos/{repository}/issues/{issue_number}/labels/{encoded}", method="DELETE")

    def add_issue_label(self, repository: str, issue_number: int, label: str) -> None:
        self._api(f"repos/{repository}/issues/{issue_number}/labels", method="POST", payload={"labels": [label]})

    def create_pull_request(self, repository: str, title: str, body: str, head: str, base: str) -> Mapping[str, Any]:
        value = self._api(
            f"repos/{repository}/pulls",
            method="POST",
            payload={"title": title, "body": body, "head": head, "base": base},
        )
        if not isinstance(value, Mapping):
            _fail("github_pr_create_failed")
        return value


def discover_github_contract(
    github: GitHubAdapter,
    repository: str,
    event: HandoffEvent | None = None,
    allow_marker_only: bool = False,
) -> GitHubContract:
    """Recover one current canonical contract before any model invocation."""

    issues = list(github.list_open_execute_issues(repository))
    execute_label_present = True
    if not issues and allow_marker_only and event is not None:
        listed_issue = github.get_issue(repository, event.issue_number)
        execute_label_present = False
    elif len(issues) != 1:
        _fail("no_execute_issue" if not issues else "multiple_execute_issues")
    else:
        listed_issue = issues[0]
    listed_number = _number(listed_issue.get("number"), "invalid_issue_number")
    if event is not None and event.issue_number != listed_number:
        _fail("event_issue_mismatch")

    issue = github.get_issue(repository, listed_number)
    issue_state = issue.get("state", "open")
    if issue_state != "open":
        _fail("execute_issue_not_open")
    comments = tuple(sorted(
        [item for item in github.list_issue_comments(repository, listed_number) if isinstance(item, Mapping)],
        key=lambda item: _comment_sort_key(item, 0),
    ))
    artifacts = parse_canonical_artifacts(_issue_body(issue), f"issue:{listed_number}", 0)
    ordinal = len(artifacts)
    for index, comment in enumerate(comments):
        artifacts.extend(parse_canonical_artifacts(comment.get("body"), f"comment:{comment.get('id', index)}", ordinal))
        ordinal = len(artifacts)
    if not artifacts:
        _fail("canonical_architect_ref_missing")
    latest = artifacts[-1]
    if latest.state not in {"READY", "FIX_REQUIRED"}:
        _fail("canonical_architect_state_stale")
    active_cycle: list[CanonicalArtifact] = []
    for artifact in artifacts:
        if artifact.state == "READY":
            active_cycle = [artifact]
        elif active_cycle and artifact.state == "FIX_REQUIRED":
            active_cycle.append(artifact)
    correction_count = sum(artifact.state == "FIX_REQUIRED" for artifact in active_cycle)
    if correction_count > MAX_CORRECTIONS:
        _fail("correction_budget_exhausted")
    latest_ref = f"issue:{listed_number}" if latest.ref == "SELF" else latest.ref
    issue_ref_number = _issue_number_from_ref(latest_ref)
    if issue_ref_number is not None and issue_ref_number != listed_number:
        _fail("canonical_issue_ref_mismatch")

    repository_info = github.get_repository(repository)
    default_branch = repository_info.get("default_branch", "main")
    if default_branch != "main":
        _fail("default_branch_not_main")
    main_head = _valid_sha(github.get_branch_head(repository, default_branch), "github_main_head_invalid")

    pr_number = latest.pr_number or _pull_number_from_issue(issue)
    if pr_number is None:
        candidates = []
        for pull in github.list_open_pull_requests(repository):
            if not isinstance(pull, Mapping):
                continue
            text = " ".join(str(pull.get(key, "")) for key in ("title", "body"))
            if re.search(rf"(?<!\d)#{listed_number}(?!\d)", text):
                candidates.append(_pull_number_from_mapping(pull))
        if len(set(candidates)) > 1:
            _fail("ambiguous_pr_ref")
        pr_number = candidates[0] if candidates else None

    pr_head: str | None = None
    pr_ref: str | None = None
    if pr_number is not None:
        pull = github.get_pull_request(repository, pr_number)
        if pull.get("state") != "open" or pull.get("base", {}).get("ref", default_branch) != default_branch:
            _fail("stale_pr_ref")
        pr_head = _pull_head(pull)
        pr_ref = _pull_ref(pull)

    if latest.head_present:
        expected_head = pr_head or main_head
        if latest.head != expected_head:
            _fail("stale_canonical_head")
    elif pr_head is not None:
        _fail("canonical_pr_head_missing")

    event_key = derive_event_key(repository, listed_number, latest_ref, main_head, pr_head)
    return GitHubContract(
        repository=repository,
        issue_number=listed_number,
        architect_state=latest.state,
        architect_ref=latest_ref,
        architect_head=latest.head,
        main_head=main_head,
        pr_number=pr_number,
        pr_head=pr_head,
        pr_ref=pr_ref,
        issue_body=_issue_body(issue),
        comments=comments,
        event_key=event_key,
        execute_label_present=execute_label_present,
    )


def bind_github_event(event: HandoffEvent, contract: GitHubContract) -> HandoffEvent:
    if event.repository != contract.repository or event.issue_number != contract.issue_number:
        _fail("event_contract_mismatch")
    return HandoffEvent(
        event_id=contract.event_key,
        source=event.source,
        repository=event.repository,
        issue_number=event.issue_number,
        action=event.action,
        label=event.label,
        correction_of=event.correction_of,
        correction=event.correction,
        canonical_ref=contract.architect_ref,
        main_head=contract.main_head,
        pr_head=contract.pr_head,
        architect_state=contract.architect_state,
    )


@dataclass(frozen=True)
class Claim:
    claimed: bool
    reason: str
    chain_id: str | None = None
    depth: int = 0
    existing_result: dict[str, Any] | None = None


class EventLedger:
    """SQLite-backed atomic event ledger for GitHub redelivery and local replay."""

    def __init__(self, path: Path):
        self.path = Path(path).expanduser().resolve()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as connection:
                with connection:
                    connection.executescript(
                        """
                        CREATE TABLE IF NOT EXISTS handoff_events (
                            event_id TEXT PRIMARY KEY,
                            chain_id TEXT NOT NULL,
                            depth INTEGER NOT NULL,
                            state TEXT NOT NULL,
                            result_json TEXT
                        );
                        """
                    )
        except (OSError, sqlite3.Error):
            _fail("ledger_unavailable")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=1.0)
        connection.execute("PRAGMA busy_timeout=1000")
        return connection

    @staticmethod
    def _decode_result(value: str | None) -> dict[str, Any] | None:
        if value is None:
            return None
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def claim(self, event: HandoffEvent) -> Claim:
        try:
            with closing(self._connect()) as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    existing = connection.execute(
                        "SELECT chain_id, depth, state, result_json FROM handoff_events WHERE event_id = ?",
                        (event.event_id,),
                    ).fetchone()
                    if existing is not None:
                        chain_id, depth, state, result_json = existing
                        if state == "RUNNING":
                            return Claim(False, "event_in_progress", chain_id, int(depth))
                        return Claim(
                            False,
                            "duplicate_event",
                            chain_id,
                            int(depth),
                            self._decode_result(result_json),
                        )

                    chain_id = event.event_id
                    depth = 0
                    if event.correction_of is not None:
                        parent = connection.execute(
                            "SELECT chain_id, depth, state FROM handoff_events WHERE event_id = ?",
                            (event.correction_of,),
                        ).fetchone()
                        if parent is None:
                            return Claim(False, "stale_correction_reference")
                        parent_chain, parent_depth, parent_state = parent
                        if parent_state == "RUNNING":
                            return Claim(False, "correction_parent_in_progress", parent_chain, int(parent_depth))
                        if int(parent_depth) >= MAX_CORRECTIONS:
                            return Claim(False, "correction_budget_exhausted", parent_chain, int(parent_depth))
                        chain_id = str(parent_chain)
                        depth = int(parent_depth) + 1

                    connection.execute(
                        "INSERT INTO handoff_events(event_id, chain_id, depth, state) VALUES (?, ?, ?, 'RUNNING')",
                        (event.event_id, chain_id, depth),
                    )
                    return Claim(True, "claimed", str(chain_id), depth)
        except sqlite3.Error:
            _fail("ledger_claim_failed")

    def finish(self, event_id: str, state: str, result: Mapping[str, Any]) -> None:
        try:
            with closing(self._connect()) as connection:
                with connection:
                    cursor = connection.execute(
                        "UPDATE handoff_events SET state = ?, result_json = ? "
                        "WHERE event_id = ? AND state = 'RUNNING'",
                        (state, _compact(dict(result)), event_id),
                    )
                    if cursor.rowcount != 1:
                        _fail("ledger_finish_failed")
        except HandoffError:
            raise
        except sqlite3.Error:
            _fail("ledger_finish_failed")


def _git_read(workspace: Path, arguments: Sequence[str]) -> str | None:
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_TERMINAL_PROMPT"] = "0"
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


def workspace_identity(workspace: Path) -> dict[str, str] | None:
    head = _git_read(workspace, ["rev-parse", "--verify", "HEAD"])
    if not head:
        return None
    status = _git_read(workspace, ["status", "--short", "--untracked-files=all"]) or ""
    cached = _git_read(workspace, ["diff", "--cached", "--no-ext-diff", "--binary"]) or ""
    return {
        "head": head,
        "status_sha256": hashlib.sha256(status.encode("utf-8")).hexdigest(),
        "cached_diff_sha256": hashlib.sha256(cached.encode("utf-8")).hexdigest(),
    }


def workspace_snapshot(workspace: Path) -> dict[str, str]:
    if not workspace.is_dir():
        _fail("workspace_missing")
    files: dict[str, str] = {}
    for current, directories, names in os.walk(workspace):
        directories[:] = [name for name in directories if name != ".git"]
        for name in names:
            path = Path(current) / name
            relative = path.relative_to(workspace).as_posix()
            try:
                files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                _fail("workspace_snapshot_failed")
    return files


def snapshot_diff(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    return sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))


def _valid_changed_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        _fail("invalid_changed_path")
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or ".." in path.parts or path.parts[0] == ".git":
        _fail("invalid_changed_path")
    return path.as_posix()


def validate_executor_result(
    payload: Any,
    expected_ref: str,
    expected_head: str | None = None,
    require_expected_head: bool = False,
) -> dict[str, Any]:
    """Validate the Executor schema plus the event identity and status gates."""

    required = {"status", "ref", "head", "summary", "changed_files", "evidence", "blocker"}
    if not isinstance(payload, dict) or set(payload) != required:
        _fail("executor_schema_invalid")
    if payload["status"] not in EXECUTOR_STATUSES:
        _fail("invalid_executor_status")
    if payload["ref"] != expected_ref:
        _fail("stale_ref")
    if not isinstance(payload["head"], (str, type(None))):
        _fail("invalid_executor_head")
    if expected_head is not None:
        if require_expected_head and payload["head"] != expected_head:
            _fail("stale_head")
        if not require_expected_head and payload["head"] not in {None, expected_head}:
            _fail("stale_head")
    _bounded_text(payload["summary"], "invalid_executor_summary")
    changed_files = payload["changed_files"]
    if not isinstance(changed_files, list) or any(not isinstance(value, str) for value in changed_files):
        _fail("invalid_changed_files")
    if len(set(changed_files)) != len(changed_files):
        _fail("invalid_changed_files")
    normalized_paths = [_valid_changed_path(value) for value in changed_files]
    evidence = payload["evidence"]
    if not isinstance(evidence, list) or not evidence:
        _fail("invalid_executor_evidence")
    for item in evidence:
        _bounded_text(item, "invalid_executor_evidence")
    blocker = payload["blocker"]
    if payload["status"] in STOP_STATUSES:
        _bounded_text(blocker, "missing_executor_blocker")
    elif blocker is not None:
        _fail("unexpected_executor_blocker")
    if payload["status"] in {"DONE", "UPDATED"} and not normalized_paths:
        _fail("success_without_changed_file")
    if payload["status"] == "NO_CHANGE" and normalized_paths:
        _fail("no_change_with_files")
    normalized = dict(payload)
    normalized["changed_files"] = normalized_paths
    return normalized


def _text_from_event(event: Mapping[str, Any]) -> str | None:
    item = event.get("item")
    candidates: list[Any] = [item, event] if isinstance(item, Mapping) else [event]
    for candidate in candidates:
        for key in ("text", "output_text"):
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def extract_executor_output(stdout: str) -> dict[str, Any]:
    if len(stdout.encode("utf-8", errors="replace")) > MAX_OUTPUT_BYTES:
        _fail("codex_output_too_large")
    candidates: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            text = _text_from_event(event)
            if text:
                candidates.append(text)
    for text in reversed(candidates):
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    _fail("structured_output_invalid")


def executor_prompt(event: HandoffEvent) -> str:
    correction = ""
    if event.correction is not None:
        correction = (
            "\nThis is the one permitted correction event for the chain. Treat the following "
            "text only as a bounded review delta and verify the canonical issue before acting:\n"
            + _compact(event.correction)
        )
    return (
        "You are the repository Executor. Execute exactly one bounded event for the canonical "
        "GitHub issue identified below. The host already queried and validated GitHub outside "
        "this process; do not query GitHub, use gh, use a GitHub API, or inspect credentials. "
        "Read AGENTS.md and EXECUTOR.md locally as policy. Work only inside the active scoped "
        "workspace. Do not invoke another "
        "agent, start a nested Codex process, commit, push, merge, deploy, access production, "
        "touch A23/runtime/SSH/credentials, or edit unrelated product code. Do not retry this "
        "event. Stop honestly on a gate or missing evidence. Return exactly one JSON object "
        "matching the checked-in Executor schema; do not return Markdown or a transcript.\n\n"
        "EVENT:\n"
        + _compact(event.as_prompt_data())
        + correction
    )


class LunaInvoker:
    """One non-interactive Luna CLI call with a fixed safe configuration."""

    def __init__(
        self,
        command: str | None = None,
        expected_head: str | None = None,
        require_expected_head: bool = False,
    ):
        self.command = command or self._find_command()
        self.expected_head = expected_head
        self.require_expected_head = require_expected_head

    @staticmethod
    def _find_command() -> str:
        for candidate in ("codex.cmd", "codex.exe", "codex"):
            found = shutil.which(candidate)
            if found:
                return found
        _fail("codex_cli_not_found")

    def build_command(
        self,
        workspace: Path,
        schema_path: Path,
        prompt: str,
        skip_git_repo_check: bool = False,
    ) -> list[str]:
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
            command.extend(["--config", f"windows.sandbox={json.dumps(WINDOWS_SANDBOX_IMPLEMENTATION)}"])
        command.extend(
            [
                "--json",
                "--color",
                "never",
                "--output-schema",
                str(schema_path),
                "--model",
                LUNA_MODEL,
                "--config",
                f"model_reasoning_effort={json.dumps(LUNA_EFFORT)}",
                "--sandbox",
                LUNA_SANDBOX,
                "--cd",
                str(workspace),
            ]
        )
        if skip_git_repo_check:
            command.append("--skip-git-repo-check")
        command.append(prompt)
        return command

    def invoke(self, event: HandoffEvent, workspace: Path, timeout_seconds: int) -> dict[str, Any]:
        if not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
            _fail("invalid_timeout")
        workspace_head = workspace_identity(workspace)
        expected_head_value = self.expected_head
        if expected_head_value is None and workspace_head is not None:
            expected_head_value = workspace_head["head"]
        try:
            schema = json.loads(EXECUTOR_SCHEMA.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            _fail("executor_schema_unreadable")
        with tempfile.TemporaryDirectory(prefix="orchestra-executor-") as directory:
            schema_path = Path(directory) / "executor.json"
            try:
                schema_path.write_text(_compact(schema), encoding="utf-8")
            except OSError:
                _fail("executor_schema_write_failed")
            command = self.build_command(
                workspace,
                schema_path,
                executor_prompt(event),
                skip_git_repo_check=expected_head is None,
            )
            environment = scrub_github_credentials()
            environment["GIT_OPTIONAL_LOCKS"] = "0"
            environment["GIT_TERMINAL_PROMPT"] = "0"
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
                _fail("codex_timeout")
            except OSError:
                _fail("codex_start_failed")
            if result.returncode != 0:
                _fail("codex_process_failed")
            payload = extract_executor_output(result.stdout)
            return validate_executor_result(
                payload,
                event.ref,
                expected_head_value,
                require_expected_head=self.require_expected_head,
            )


def _safe_result(
    event: HandoffEvent,
    *,
    terminal: str,
    reason: str,
    invoked: bool,
    depth: int = 0,
    changed_files: Sequence[str] = (),
    evidence: Sequence[str] = (),
    blocker: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "protocol": RESULT_PROTOCOL,
        "event_id": event.event_id,
        "ref": event.ref,
        "source": event.source,
        "issue_number": event.issue_number,
        "terminal": terminal,
        "reason": reason,
        "invoked": invoked,
        "correction_depth": depth,
        "correction_budget": MAX_CORRECTIONS,
        "changed_files": list(changed_files),
        "evidence": [_bounded_text(item) for item in evidence] if evidence else [],
    }
    if blocker is not None:
        result["blocker"] = _bounded_text(blocker)
    if metadata:
        for key, value in metadata.items():
            if isinstance(value, str):
                result[key] = _bounded_text(value)
            elif isinstance(value, (int, type(None), bool)):
                result[key] = value
    return result


class EventProcessor:
    """Claim and execute one event; every path terminates without retry."""

    def __init__(
        self,
        workspace: Path,
        ledger: EventLedger,
        invoker: Any,
        timeout_seconds: int = 180,
        expected_result_head: str | None = None,
        require_expected_head: bool = False,
        terminal_handler: Callable[[HandoffEvent, Mapping[str, Any], Sequence[str]], Mapping[str, Any] | None]
        | None = None,
    ):
        self.workspace = Path(workspace).resolve()
        self.ledger = ledger
        self.invoker = invoker
        self.timeout_seconds = timeout_seconds
        self.expected_result_head = expected_result_head
        self.require_expected_head = require_expected_head
        self.terminal_handler = terminal_handler
        try:
            self.ledger.path.relative_to(self.workspace)
        except ValueError:
            pass
        else:
            _fail("ledger_inside_workspace")

    def process(self, event: HandoffEvent) -> dict[str, Any]:
        claim = self.ledger.claim(event)
        if not claim.claimed:
            return _safe_result(
                event,
                terminal="NO_CHANGE" if claim.reason == "duplicate_event" else "STOP",
                reason=claim.reason,
                invoked=False,
                depth=claim.depth,
                evidence=("event already has a terminal ledger record",)
                if claim.reason == "duplicate_event"
                else (),
                blocker=None if claim.reason == "duplicate_event" else claim.reason,
            )

        before_files = workspace_snapshot(self.workspace)
        before_identity = workspace_identity(self.workspace)
        try:
            try:
                raw_payload = self.invoker.invoke(event, self.workspace, self.timeout_seconds)
            finally:
                # Even a malformed/failed call is checked before it can become a
                # terminal ledger record. A model may not alter repository
                # identity while producing an invalid result.
                after_files = workspace_snapshot(self.workspace)
                after_identity = workspace_identity(self.workspace)
                if (before_identity is None) != (after_identity is None):
                    _fail("executor_touched_git")
                if before_identity and after_identity:
                    if before_identity["head"] != after_identity["head"]:
                        _fail("executor_changed_head")
                    if before_identity["cached_diff_sha256"] != after_identity["cached_diff_sha256"]:
                        _fail("executor_touched_git")
            expected_head = self.expected_result_head
            if expected_head is None and before_identity is not None:
                expected_head = before_identity["head"]
            payload = validate_executor_result(
                raw_payload,
                event.ref,
                expected_head,
                require_expected_head=self.require_expected_head,
            )
            actual_changes = snapshot_diff(before_files, after_files)
            if actual_changes != sorted(payload["changed_files"]):
                _fail("executor_change_report_mismatch")
            metadata = self.terminal_handler(event, payload, actual_changes) if self.terminal_handler else None
            terminal = payload["status"]
            reason = "executor_stop_gate" if terminal in STOP_STATUSES else "executor_result"
            result = _safe_result(
                event,
                terminal=terminal,
                reason=reason,
                invoked=True,
                depth=claim.depth,
                changed_files=actual_changes,
                evidence=payload["evidence"],
                blocker=payload.get("blocker"),
                metadata=metadata,
            )
            self.ledger.finish(event.event_id, "TERMINAL", result)
            return result
        except HandoffError as error:
            result = _safe_result(
                event,
                terminal="STOP",
                reason=error.code,
                invoked=True,
                depth=claim.depth,
                blocker=error.code,
            )
            self.ledger.finish(event.event_id, "STOPPED", result)
            return result
        except Exception:
            result = _safe_result(
                event,
                terminal="STOP",
                reason="handoff_failed",
                invoked=True,
                depth=claim.depth,
                blocker="handoff_failed",
            )
            self.ledger.finish(event.event_id, "STOPPED", result)
            return result


class GitWorkspaceProtocol(Protocol):
    def require_clean_at(self, expected_head: str) -> None: ...

    def prepare_for_pr(self, branch: str, expected_head: str) -> None: ...

    def stage_commit_push(self, paths: Sequence[str], branch: str, expected_remote_head: str | None) -> str: ...


@dataclass(frozen=True)
class PublishedPR:
    number: int | None
    head_sha: str | None
    branch: str | None


def event_marker(event_key: str) -> str:
    _valid_event_id(event_key)
    return f"{EVENT_MARKER_PREFIX} event={event_key} -->"


def github_has_event_marker(contract: GitHubContract) -> bool:
    marker = event_marker(contract.event_key)
    if marker in contract.issue_body:
        return True
    return any(marker in str(comment.get("body", "")) for comment in contract.comments)


def build_executor_report(
    contract: GitHubContract,
    event: HandoffEvent,
    payload: Mapping[str, Any],
    pr: PublishedPR,
) -> str:
    status = _bounded_text(payload.get("status"), "invalid_report_status")
    summary = _bounded_text(payload.get("summary"), "invalid_report_summary")
    evidence = payload.get("evidence")
    if not isinstance(evidence, list):
        _fail("invalid_report_evidence")
    lines = [
        event_marker(event.event_id),
        "EXECUTOR | " + status,
        f"Issue: #{contract.issue_number}",
        f"PR: #{pr.number}" if pr.number is not None else "PR: none",
        f"PR HEAD: {pr.head_sha}" if pr.head_sha is not None else "PR HEAD: none",
        f"Event: {event.event_id}",
        f"Ref: {event.ref}",
        f"Status: {status}",
        f"Summary: {summary}",
        "Evidence:",
    ]
    for item in evidence[:8]:
        lines.append("- " + _bounded_text(item, "invalid_report_evidence"))
    return "\n".join(lines)


class GitWorkspace:
    """Host-side git publisher. It is never reachable from the Luna child."""

    def __init__(self, workspace: Path, timeout_seconds: int = 30):
        self.workspace = Path(workspace).resolve()
        self.timeout_seconds = timeout_seconds

    def _run(self, arguments: Sequence[str], github_write: bool = False) -> str:
        environment = os.environ.copy()
        environment["GIT_TERMINAL_PROMPT"] = "0"
        environment["GIT_OPTIONAL_LOCKS"] = "0"
        if github_write:
            token = environment.get("GH_TOKEN") or environment.get("GITHUB_TOKEN")
            if token:
                try:
                    config_count = int(environment.get("GIT_CONFIG_COUNT", "0"))
                except ValueError:
                    _fail("git_host_command_failed")
                encoded = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
                environment[f"GIT_CONFIG_KEY_{config_count}"] = "http.extraheader"
                environment[f"GIT_CONFIG_VALUE_{config_count}"] = f"AUTHORIZATION: basic {encoded}"
                environment["GIT_CONFIG_COUNT"] = str(config_count + 1)
        try:
            result = subprocess.run(
                ["git", "-C", str(self.workspace), *arguments],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            _fail("git_host_command_failed")
        if result.returncode != 0:
            _fail("git_host_command_failed")
        return result.stdout.strip()

    def require_clean_at(self, expected_head: str) -> None:
        if not self.workspace.is_dir():
            _fail("workspace_missing")
        status = self._run(["status", "--porcelain", "--untracked-files=all"])
        if status:
            _fail("workspace_not_clean")
        head = _valid_sha(self._run(["rev-parse", "--verify", "HEAD"]), "workspace_head_invalid")
        if head != expected_head.lower():
            _fail("workspace_main_head_stale")

    def prepare_for_pr(self, branch: str, expected_head: str) -> None:
        if (
            not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", branch)
            or branch.startswith(("-", "/"))
            or branch.endswith(("/", "."))
            or ".." in branch
            or "//" in branch
            or branch == "main"
        ):
            _fail("invalid_publish_branch")
        _valid_sha(expected_head, "stale_pr_head")
        self._run([
            "fetch",
            "--no-tags",
            "origin",
            f"refs/heads/{branch}:refs/remotes/origin/{branch}",
        ], github_write=True)
        self._run(["switch", "--detach", expected_head])
        head = _valid_sha(self._run(["rev-parse", "--verify", "HEAD"]), "workspace_pr_head_invalid")
        if head != expected_head.lower():
            _fail("workspace_pr_head_stale")

    def stage_commit_push(self, paths: Sequence[str], branch: str, expected_remote_head: str | None) -> str:
        normalized = sorted({_valid_changed_path(path) for path in paths})
        if not normalized:
            _fail("nothing_to_publish")
        if (
            not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", branch)
            or branch.startswith(("-", ".git/", "/"))
            or branch.endswith(("/", "."))
            or ".." in branch
            or "//" in branch
        ):
            _fail("invalid_publish_branch")
        self._run(["add", "--", *normalized])
        staged = self._run(["diff", "--cached", "--name-only"]).splitlines()
        if sorted(staged) != normalized:
            _fail("staged_path_mismatch")
        self._run(["commit", "-m", f"orchestra: Executor update for Issue {branch.split('/')[-1]}"])
        actual_head = _valid_sha(self._run(["rev-parse", "--verify", "HEAD"]), "commit_head_invalid")
        if expected_remote_head is not None:
            _valid_sha(expected_remote_head, "stale_pr_head")
            self._run([
                "push",
                "--force-with-lease=" + f"refs/heads/{branch}:{expected_remote_head}",
                "origin",
                f"HEAD:refs/heads/{branch}",
            ], github_write=True)
        else:
            self._run(["push", "origin", f"HEAD:refs/heads/{branch}"], github_write=True)
        return actual_head


def publish_executor_changes(
    github: GitHubAdapter,
    git: GitWorkspaceProtocol,
    contract: GitHubContract,
    event: HandoffEvent,
    payload: Mapping[str, Any],
    changed_files: Sequence[str],
) -> PublishedPR:
    """Stage, commit, push, and create/update exactly one PR on the host."""

    branch: str | None = None
    if contract.pr_number is not None:
        pull = github.get_pull_request(contract.repository, contract.pr_number)
        branch = _pull_ref(pull)
    elif changed_files:
        branch = f"orchestra/issue-{contract.issue_number}"

    if changed_files:
        if branch is None:
            _fail("publish_branch_missing")
        commit_head = git.stage_commit_push(changed_files, branch, contract.pr_head)
        if contract.pr_number is not None:
            pull = github.get_pull_request(contract.repository, contract.pr_number)
            actual = _pull_head(pull)
            if actual != commit_head:
                _fail("published_pr_head_mismatch")
            return PublishedPR(contract.pr_number, actual, branch)
        title = f"Executor update for Issue #{contract.issue_number}"
        body = (
            f"ARCHITECT | EXECUTING\nREF: {event.ref}\n\n"
            f"Host-published Executor change for Issue #{contract.issue_number}.\n"
            f"Event: {event.event_id}\n"
        )
        created = github.create_pull_request(contract.repository, title, body, branch, "main")
        number = _pull_number_from_mapping(created)
        pull = github.get_pull_request(contract.repository, number)
        actual = _pull_head(pull)
        if actual != commit_head:
            _fail("created_pr_head_mismatch")
        return PublishedPR(number, actual, branch)

    if contract.pr_number is not None:
        pull = github.get_pull_request(contract.repository, contract.pr_number)
        return PublishedPR(contract.pr_number, _pull_head(pull), _pull_ref(pull))
    return PublishedPR(None, None, None)


class GitHubTerminalHandler:
    """Publish one validated terminal result, then move execute to review."""

    def __init__(self, github: GitHubAdapter, git: GitWorkspaceProtocol, contract: GitHubContract):
        self.github = github
        self.git = git
        self.contract = contract

    def __call__(
        self,
        event: HandoffEvent,
        payload: Mapping[str, Any],
        changed_files: Sequence[str],
    ) -> Mapping[str, Any]:
        pr = publish_executor_changes(self.github, self.git, self.contract, event, payload, changed_files)
        report = build_executor_report(self.contract, event, payload, pr)
        self.github.post_issue_comment(self.contract.repository, self.contract.issue_number, report)
        self.github.remove_issue_label(self.contract.repository, self.contract.issue_number, EXECUTE_LABEL)
        self.github.add_issue_label(self.contract.repository, self.contract.issue_number, REVIEW_LABEL)
        return {
            "pr_number": pr.number,
            "pr_head": pr.head_sha,
            "event_marker": event_marker(event.event_id),
        }


def execute_github_handoff(
    workspace: Path,
    github: GitHubAdapter,
    ledger: EventLedger,
    invoker: Any | None = None,
    git: GitWorkspaceProtocol | None = None,
    event: HandoffEvent | None = None,
    repository: str = REPOSITORY,
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    """Run one complete host handoff; no GitHub call is delegated to Luna."""

    contract = discover_github_contract(
        github,
        repository,
        event,
        allow_marker_only=event is not None,
    )
    if event is None:
        event = HandoffEvent(
            event_id="manual-discovery",
            source="local",
            repository=repository,
            issue_number=contract.issue_number,
            action="labeled",
            label=EXECUTE_LABEL,
        )
    bound = bind_github_event(event, contract)
    if github_has_event_marker(contract):
        # Recover a runner restart that occurred after the durable report was
        # posted but before the execute -> review label transition completed.
        if contract.execute_label_present:
            github.remove_issue_label(contract.repository, contract.issue_number, EXECUTE_LABEL)
        github.add_issue_label(contract.repository, contract.issue_number, REVIEW_LABEL)
        return _safe_result(
            bound,
            terminal="NO_CHANGE",
            reason="github_event_marker",
            invoked=False,
            evidence=("durable GitHub Executor marker already exists",),
            metadata={"event_marker": event_marker(bound.event_id)},
        )
    if not contract.execute_label_present:
        _fail("no_execute_issue")

    git = git or GitWorkspace(workspace)
    if hasattr(git, "require_clean_at"):
        git.require_clean_at(contract.main_head)
    if contract.pr_number is not None and contract.pr_ref and contract.pr_head and hasattr(git, "prepare_for_pr"):
        git.prepare_for_pr(contract.pr_ref, contract.pr_head)
    if invoker is None:
        invoker = LunaInvoker(
            expected_head=contract.expected_executor_head,
            require_expected_head=True,
        )
    elif isinstance(invoker, LunaInvoker):
        invoker.expected_head = contract.expected_executor_head
        invoker.require_expected_head = True
    handler = GitHubTerminalHandler(github, git, contract)
    processor = EventProcessor(
        workspace,
        ledger,
        invoker,
        timeout_seconds=timeout_seconds,
        expected_result_head=contract.expected_executor_head,
        require_expected_head=True,
        terminal_handler=handler,
    )
    return processor.process(bound)


def default_state_path() -> Path:
    configured = os.environ.get("ORCHESTRA_STATE_FILE")
    if configured:
        return Path(configured)
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return root / "telegramfonts" / "orchestra" / "events.sqlite3"
