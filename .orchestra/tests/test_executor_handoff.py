"""Deterministic tests for the active Executor-only event handoff."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ORCHESTRA_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORCHESTRA_DIR))

from executor_handoff import (  # noqa: E402
    DONE_LABEL,
    EXECUTE_LABEL,
    HUMAN_LABEL,
    REVIEW_LABEL,
    HandoffError,
    EventLedger,
    EventProcessor,
    HandoffEvent,
    GitHubContract,
    LunaInvoker,
    MAX_CORRECTIONS,
    PROTOCOL,
    bind_github_event,
    build_executor_report,
    discover_github_contract,
    event_marker,
    execute_github_handoff,
    parse_github_event,
    parse_local_event,
    scrub_github_credentials,
    validate_executor_result,
)


def local_event(event_id: str, correction_of: str | None = None) -> HandoffEvent:
    payload = {
        "protocol": PROTOCOL,
        "source": "local",
        "event_id": event_id,
        "repository": "dtadptvl/telegramfonts",
        "issue_number": 57,
        "action": "labeled",
        "label": EXECUTE_LABEL,
    }
    if correction_of is not None:
        payload["correction_of"] = correction_of
        payload["correction"] = {"summary": "Apply one bounded correction."}
    return parse_local_event(payload)


class FakeInvoker:
    def __init__(self):
        self.calls: list[str] = []

    def invoke(self, event, workspace, timeout_seconds):
        self.calls.append(event.event_id)
        if len(self.calls) == 1:
            (workspace / "result.txt").write_text("safe fixture", encoding="utf-8")
            return {
                "status": "UPDATED",
                "ref": event.ref,
                "head": None,
                "summary": "fixture initial result",
                "changed_files": ["result.txt"],
                "evidence": ["fixture observed result.txt"],
                "blocker": None,
            }
        return {
            "status": "NO_CHANGE",
            "ref": event.ref,
            "head": None,
            "summary": "fixture correction verified no additional change",
            "changed_files": [],
            "evidence": ["fixture correction completed"],
            "blocker": None,
        }


MAIN_HEAD = "1" * 40
PR_HEAD = "2" * 40
PUBLISHED_HEAD = "3" * 40


class FakeGitHub:
    def __init__(self, body: str | None = None, comments=None, issues=None):
        self.issue = {
            "number": 57,
            "state": "open",
            "body": body or "ARCHITECT | READY\nREF: issue:57\nHEAD: " + MAIN_HEAD,
            "labels": [{"name": EXECUTE_LABEL}],
        }
        self.issues = list(issues or [self.issue])
        self.comments = list(comments or [])
        self.pulls: dict[int, dict] = {}
        self.calls: list[tuple] = []
        self.default_branch = "main"
        self.labels: list[tuple] = []
        self.created_pr = 100

    def list_open_execute_issues(self, repository):
        self.calls.append(("list_issues", repository))
        return self.issues

    def get_issue(self, repository, issue_number):
        self.calls.append(("get_issue", issue_number))
        return self.issue

    def list_issue_comments(self, repository, issue_number):
        self.calls.append(("comments", issue_number))
        return self.comments

    def get_repository(self, repository):
        self.calls.append(("repository", repository))
        return {"default_branch": self.default_branch}

    def get_branch_head(self, repository, branch):
        self.calls.append(("branch", branch))
        return MAIN_HEAD

    def get_pull_request(self, repository, pull_number):
        self.calls.append(("pull", pull_number))
        return self.pulls[pull_number]

    def list_open_pull_requests(self, repository):
        self.calls.append(("list_pulls", repository))
        return list(self.pulls.values())

    def post_issue_comment(self, repository, issue_number, body):
        self.calls.append(("comment_post", issue_number, body))
        self.comments.append({"id": len(self.comments) + 1, "body": body, "created_at": "2026-08-24T07:00:00Z"})
        return {"id": len(self.comments)}

    def remove_issue_label(self, repository, issue_number, label):
        self.calls.append(("label_remove", issue_number, label))
        self.labels.append(("remove", label))

    def add_issue_label(self, repository, issue_number, label):
        self.calls.append(("label_add", issue_number, label))
        self.labels.append(("add", label))

    def create_pull_request(self, repository, title, body, head, base):
        self.calls.append(("pr_create", title, head, base))
        self.pulls[self.created_pr] = {
            "number": self.created_pr,
            "state": "open",
            "base": {"ref": base},
            "head": {"ref": head, "sha": PUBLISHED_HEAD},
        }
        return {"number": self.created_pr}


class FakeGit:
    def __init__(self):
        self.calls: list[tuple] = []

    def require_clean_at(self, expected_head):
        self.calls.append(("preflight", expected_head))

    def stage_commit_push(self, paths, branch, expected_remote_head):
        self.calls.append(("publish", tuple(paths), branch, expected_remote_head))
        return PUBLISHED_HEAD


class HostInvoker:
    def __init__(self, status="UPDATED", changed=True, head=MAIN_HEAD):
        self.calls = []
        self.status = status
        self.changed = changed
        self.head = head

    def invoke(self, event, workspace, timeout_seconds):
        self.calls.append((event.event_id, timeout_seconds))
        changed_files = ["result.txt"] if self.changed else []
        if self.changed:
            (workspace / "result.txt").write_text("host fixture", encoding="utf-8")
        return {
            "status": self.status,
            "ref": event.ref,
            "head": self.head,
            "summary": "host fixture result",
            "changed_files": changed_files,
            "evidence": ["host observed the bounded fixture"],
            "blocker": "bounded fixture blocker" if self.status in {"BLOCKED", "READY_HUMAN_AUTH", "SECURITY_BLOCKED"} else None,
        }


class ExecutorHandoffTests(unittest.TestCase):
    def test_canonical_labels_are_the_only_active_protocol_labels(self):
        self.assertEqual(
            {EXECUTE_LABEL, REVIEW_LABEL, HUMAN_LABEL, DONE_LABEL},
            {"orchestra:execute", "orchestra:review", "orchestra:human", "orchestra:done"},
        )

    def test_github_event_normalizes_to_stable_id_without_run_metadata(self):
        payload = {
            "action": "labeled",
            "repository": {"full_name": "dtadptvl/telegramfonts"},
            "issue": {"id": 5701, "number": 57, "updated_at": "2026-08-24T06:00:00Z"},
            "label": {"id": 9001, "name": EXECUTE_LABEL},
        }
        first = parse_github_event(payload)
        second = parse_github_event(dict(payload, workflow_run_id="different"))
        self.assertEqual(first, second)
        self.assertEqual(first.source, "github")
        self.assertEqual(first.ref, "issue:57")

        invalid = dict(payload, label={"id": 9001, "name": "not-the-execute-label"})
        with self.assertRaises(HandoffError) as context:
            parse_github_event(invalid)
        self.assertEqual(context.exception.code, "ignored_label")

    def test_discovery_recovers_latest_fix_required_ref_and_live_heads(self):
        github = FakeGitHub(
            comments=[
                {
                    "id": 99,
                    "created_at": "2026-08-24T07:00:00Z",
                    "body": "ARCHITECT | FIX_REQUIRED\nREF: review:499\nPR: #12\nHEAD: " + PR_HEAD,
                }
            ]
        )
        github.pulls[12] = {
            "number": 12,
            "state": "open",
            "base": {"ref": "main"},
            "head": {"ref": "orchestra/issue-57", "sha": PR_HEAD},
        }
        contract = discover_github_contract(github, "dtadptvl/telegramfonts")
        self.assertEqual(contract.architect_state, "FIX_REQUIRED")
        self.assertEqual(contract.architect_ref, "review:499")
        self.assertEqual(contract.main_head, MAIN_HEAD)
        self.assertEqual(contract.pr_head, PR_HEAD)
        self.assertEqual(contract.pr_number, 12)
        self.assertTrue(contract.event_key.startswith("github-"))

    def test_discovery_normalizes_self_to_the_active_issue_ref(self):
        contract = discover_github_contract(
            FakeGitHub(body="ARCHITECT | READY\nREF: SELF\nHEAD: " + MAIN_HEAD),
            "dtadptvl/telegramfonts",
        )
        self.assertEqual(contract.architect_ref, "issue:57")

    def test_discovery_rejects_multiple_execute_issues_and_stale_refs_or_heads(self):
        second = {"number": 58, "state": "open", "body": "ARCHITECT | READY\nREF: issue:58"}
        with self.assertRaises(HandoffError) as multiple:
            discover_github_contract(
                FakeGitHub(issues=[FakeGitHub().issue, second]),
                "dtadptvl/telegramfonts",
            )
        self.assertEqual(multiple.exception.code, "multiple_execute_issues")

        stale_head = FakeGitHub(body="ARCHITECT | READY\nREF: issue:57\nHEAD: " + "f" * 40)
        with self.assertRaises(HandoffError) as stale:
            discover_github_contract(stale_head, "dtadptvl/telegramfonts")
        self.assertEqual(stale.exception.code, "stale_canonical_head")

        stale_ref = FakeGitHub(body="ARCHITECT | READY\nREF: issue:56\nHEAD: " + MAIN_HEAD)
        with self.assertRaises(HandoffError) as ref_error:
            discover_github_contract(stale_ref, "dtadptvl/telegramfonts")
        self.assertEqual(ref_error.exception.code, "canonical_issue_ref_mismatch")

    def test_discovery_enforces_github_correction_budget(self):
        github = FakeGitHub(
            comments=[
                {
                    "id": 98,
                    "created_at": "2026-08-24T07:00:00Z",
                    "body": "ARCHITECT | FIX_REQUIRED\nREF: review:498\nHEAD: " + MAIN_HEAD,
                },
                {
                    "id": 99,
                    "created_at": "2026-08-24T08:00:00Z",
                    "body": "ARCHITECT | FIX_REQUIRED\nREF: review:499\nHEAD: " + MAIN_HEAD,
                },
            ]
        )
        with self.assertRaises(HandoffError) as exhausted:
            discover_github_contract(github, "dtadptvl/telegramfonts")
        self.assertEqual(exhausted.exception.code, "correction_budget_exhausted")

    def test_event_marker_deduplicates_a_restart_before_luna_and_local_ledger(self):
        github = FakeGitHub()
        with tempfile.TemporaryDirectory() as workspace_dir, tempfile.TemporaryDirectory() as state_dir:
            first_invoker = HostInvoker()
            first = execute_github_handoff(
                Path(workspace_dir),
                github,
                EventLedger(Path(state_dir) / "first.sqlite3"),
                invoker=first_invoker,
                git=FakeGit(),
                event=local_event("first-delivery"),
            )
            self.assertEqual(first["terminal"], "UPDATED")

            # A restarted delivery may observe the terminal review label rather
            # than the original execute label; the durable marker still wins.
            github.issues = []
            restart_invoker = HostInvoker()
            restart = execute_github_handoff(
                Path(workspace_dir),
                github,
                EventLedger(Path(state_dir) / "restart.sqlite3"),
                invoker=restart_invoker,
                git=FakeGit(),
                event=local_event("redelivered-after-restart"),
            )
        self.assertEqual(restart["terminal"], "NO_CHANGE")
        self.assertEqual(restart["reason"], "github_event_marker")
        self.assertEqual(restart_invoker.calls, [])
        self.assertEqual(github.labels[-1], ("add", REVIEW_LABEL))

    def test_host_publishes_report_and_transitions_execute_to_review(self):
        github = FakeGitHub()
        git = FakeGit()
        invoker = HostInvoker()
        with tempfile.TemporaryDirectory() as workspace_dir, tempfile.TemporaryDirectory() as state_dir:
            result = execute_github_handoff(
                Path(workspace_dir),
                github,
                EventLedger(Path(state_dir) / "events.sqlite3"),
                invoker=invoker,
                git=git,
                event=local_event("host-event"),
            )
        self.assertEqual(result["terminal"], "UPDATED")
        self.assertEqual(result["pr_number"], 100)
        self.assertEqual(result["pr_head"], PUBLISHED_HEAD)
        self.assertEqual(invoker.calls[0][1], 180)
        self.assertEqual(git.calls[0], ("preflight", MAIN_HEAD))
        self.assertIn(("remove", EXECUTE_LABEL), github.labels)
        self.assertIn(("add", REVIEW_LABEL), github.labels)
        self.assertNotIn(("add", HUMAN_LABEL), github.labels)
        report = next(call[2] for call in github.calls if call[0] == "comment_post")
        self.assertIn("Issue: #57", report)
        self.assertIn("PR: #100", report)
        self.assertIn(f"PR HEAD: {PUBLISHED_HEAD}", report)
        self.assertIn("Ref: issue:57", report)
        self.assertIn("Status: UPDATED", report)
        self.assertIn("Evidence:", report)
        self.assertIn(event_marker(result["event_id"]), report)

    def test_blocked_executor_is_reported_to_review_without_human_route(self):
        github = FakeGitHub()
        with tempfile.TemporaryDirectory() as workspace_dir, tempfile.TemporaryDirectory() as state_dir:
            result = execute_github_handoff(
                Path(workspace_dir),
                github,
                EventLedger(Path(state_dir) / "events.sqlite3"),
                invoker=HostInvoker(status="BLOCKED", changed=False),
                git=FakeGit(),
                event=local_event("blocked-event"),
            )
        self.assertEqual(result["terminal"], "BLOCKED")
        self.assertIn(("remove", EXECUTE_LABEL), github.labels)
        self.assertIn(("add", REVIEW_LABEL), github.labels)
        self.assertNotIn(("add", HUMAN_LABEL), github.labels)

    def test_github_credentials_are_scrubbed_from_luna_environment(self):
        scrubbed = scrub_github_credentials(
            {
                "GH_TOKEN": "write-secret",
                "GITHUB_TOKEN": "write-secret-2",
                "GITHUB_EVENT_PATH": "event.json",
                "ACTIONS_RUNTIME_TOKEN": "runtime-secret",
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "id-secret",
                "SAFE_VALUE": "kept",
            }
        )
        self.assertEqual(scrubbed, {"SAFE_VALUE": "kept"})

    def test_workflow_launcher_and_fallback_contract_are_bounded(self):
        workflow = (ORCHESTRA_DIR.parent / ".github" / "workflows" / "executor-issue-label.yml").read_text(encoding="utf-8")
        launcher = (ORCHESTRA_DIR / "executor_launcher.py").read_text(encoding="utf-8")
        fallback = (ORCHESTRA_DIR.parent / "orchestra.cmd").read_text(encoding="utf-8")
        self.assertIn("issues:", workflow)
        self.assertIn("types: [labeled]", workflow)
        self.assertIn("orchestra:execute", workflow)
        self.assertIn("orchestra-executor-global", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("issues: write", workflow)
        self.assertIn("pull-requests: write", workflow)
        self.assertIn("[self-hosted, windows, x64, orchestra]", workflow)
        self.assertIn("clean: true", workflow)
        self.assertIn("ref: ${{ github.event.repository.default_branch }}", workflow)
        self.assertIn("./orchestra.cmd execute --github-event $env:GITHUB_EVENT_PATH", workflow)
        self.assertNotIn("runner.py", workflow)
        self.assertNotIn("gpt-5.6-sol", workflow)
        self.assertIn("nargs=\"?\"", launcher)
        self.assertIn("manual discovery", launcher)
        self.assertIn("executor_launcher.py", fallback)
        self.assertIn("python_unavailable", fallback)

    def test_safe_correction_fixture_declares_architect_review_semantics(self):
        fixture_path = ORCHESTRA_DIR / "fixtures" / "issue57-safe-correction-cycle.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        cycle = fixture["correction_cycle"]
        self.assertEqual(
            [item["state"] for item in cycle],
            ["READY", "EXECUTOR", "ARCHITECT_REVIEW", "FIX_REQUIRED", "EXECUTOR", "ARCHITECT_REVIEW"],
        )
        self.assertEqual(cycle[3]["label_transition"], "orchestra:review -> orchestra:execute")
        self.assertEqual(cycle[5]["label"], REVIEW_LABEL)

    def test_local_event_rejects_contract_or_identity_fields(self):
        event = local_event("desktop-57")
        self.assertEqual(event.event_id, "desktop-57")
        invalid = {
            "protocol": PROTOCOL,
            "source": "local",
            "event_id": "desktop-57",
            "repository": "dtadptvl/telegramfonts",
            "issue_number": 57,
            "action": "labeled",
            "label": EXECUTE_LABEL,
            "contract": {"goal": "must not cross the event boundary"},
        }
        with self.assertRaises(HandoffError) as context:
            parse_local_event(invalid)
        self.assertEqual(context.exception.code, "local_event_shape")

    def test_luna_command_is_fixed_and_has_no_second_role(self):
        command = LunaInvoker(command="codex.cmd").build_command(
            Path("C:/workspace"), Path("C:/schema.json"), "bounded prompt"
        )
        self.assertIn("gpt-5.6-luna", command)
        self.assertIn("workspace-write", command)
        self.assertIn('model_reasoning_effort="max"', command)
        self.assertNotIn("gpt-5.6-sol", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertNotIn("--output-last-message", command)
        self.assertLess(command.index("--ask-for-approval"), command.index("exec"))

    def test_duplicate_and_inflight_events_are_idempotent(self):
        with tempfile.TemporaryDirectory() as workspace_dir, tempfile.TemporaryDirectory() as state_dir:
            ledger = EventLedger(Path(state_dir) / "events.sqlite3")
            invoker = FakeInvoker()
            processor = EventProcessor(Path(workspace_dir), ledger, invoker)
            event = local_event("idempotent-event")

            first = processor.process(event)
            duplicate = processor.process(event)
            self.assertEqual(first["terminal"], "UPDATED")
            self.assertEqual(duplicate["terminal"], "NO_CHANGE")
            self.assertEqual(duplicate["reason"], "duplicate_event")
            self.assertEqual(invoker.calls, ["idempotent-event"])

            running_event = local_event("inflight-event")
            claim = ledger.claim(running_event)
            self.assertTrue(claim.claimed)
            inflight = processor.process(running_event)
            self.assertEqual(inflight["terminal"], "STOP")
            self.assertEqual(inflight["reason"], "event_in_progress")
            self.assertEqual(invoker.calls, ["idempotent-event"])

    def test_safe_correction_fixture_has_one_correction_then_stops(self):
        fixture_path = ORCHESTRA_DIR / "fixtures" / "issue57-safe-correction-cycle.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        self.assertEqual(fixture["expected"]["max_corrections"], MAX_CORRECTIONS)
        events = [parse_local_event(item) for item in fixture["events"]]

        with tempfile.TemporaryDirectory() as workspace_dir, tempfile.TemporaryDirectory() as state_dir:
            ledger = EventLedger(Path(state_dir) / "events.sqlite3")
            invoker = FakeInvoker()
            processor = EventProcessor(Path(workspace_dir), ledger, invoker)
            initial = processor.process(events[0])
            correction = processor.process(events[1])
            exhausted = processor.process(events[2])

        self.assertEqual(initial["terminal"], "UPDATED")
        self.assertEqual(correction["terminal"], "NO_CHANGE")
        self.assertEqual(correction["correction_depth"], 1)
        self.assertEqual(exhausted["terminal"], fixture["expected"]["third_event_terminal"])
        self.assertEqual(exhausted["reason"], fixture["expected"]["third_event_reason"])
        self.assertEqual(invoker.calls, ["fixture-initial", "fixture-correction"])

    def test_stop_statuses_require_a_blocker_and_success_requires_real_path(self):
        base = {
            "status": "BLOCKED",
            "ref": "issue:57",
            "head": None,
            "summary": "bounded stop",
            "changed_files": [],
            "evidence": ["stop evidence"],
            "blocker": "required evidence is unavailable",
        }
        self.assertEqual(validate_executor_result(base, "issue:57")["status"], "BLOCKED")

        missing_blocker = dict(base, blocker=None)
        with self.assertRaises(HandoffError) as context:
            validate_executor_result(missing_blocker, "issue:57")
        self.assertEqual(context.exception.code, "missing_executor_blocker")

        success_without_path = dict(base, status="DONE", blocker=None)
        with self.assertRaises(HandoffError) as context:
            validate_executor_result(success_without_path, "issue:57")
        self.assertEqual(context.exception.code, "success_without_changed_file")

        with self.assertRaises(HandoffError) as context:
            validate_executor_result(dict(base, status="NO_CHANGE", ref="issue:56"), "issue:57")
        self.assertEqual(context.exception.code, "stale_ref")

        with self.assertRaises(HandoffError) as context:
            validate_executor_result(
                dict(base, status="NO_CHANGE", blocker=None, head="f" * 40),
                "issue:57",
                expected_head=MAIN_HEAD,
                require_expected_head=True,
            )
        self.assertEqual(context.exception.code, "stale_head")

    def test_stale_correction_parent_does_not_invoke(self):
        with tempfile.TemporaryDirectory() as workspace_dir, tempfile.TemporaryDirectory() as state_dir:
            ledger = EventLedger(Path(state_dir) / "events.sqlite3")
            invoker = FakeInvoker()
            processor = EventProcessor(Path(workspace_dir), ledger, invoker)
            result = processor.process(local_event("correction", correction_of="missing-parent"))
        self.assertEqual(result["terminal"], "STOP")
        self.assertEqual(result["reason"], "stale_correction_reference")
        self.assertEqual(invoker.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
