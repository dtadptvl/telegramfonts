"""Focused deterministic transport tests; stdlib-only so CI needs no new dependency."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ORCHESTRA_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORCHESTRA_DIR))

from runner import (  # noqa: E402
    DeterministicRunner,
    InvocationResult,
    ProtocolError,
    RoleConfig,
    ROLE_CONFIG,
    CodexTransport,
    TransportError,
    architect_prompt,
    classify_process_failure,
    identity_constrained_schema,
    route_architect,
    route_executor_review,
    tracked_workspace_identity,
    validate_json_schema,
    validate_architect,
    validate_executor,
)


HEAD = "3236bae6671728e94929c5fba33fb6a8df44f649"
BASE_CONTRACT = {
    "ref": "issue:55",
    "head": HEAD,
    "goal": "Create result.txt with exact smoke content.",
    "scope": {
        "allowed_paths": ["result.txt"],
        "forbidden_paths": [".git", ".git/**", "contract.json"],
    },
    "accept": ["result.txt contains the exact smoke content"],
    "evidence": ["the actual changed path is result.txt"],
    "budget": {"max_calls": 5, "max_handoffs": 4, "timeout_seconds": 30},
    "gate": ["LOCAL_ONLY", "NO_LOOP"],
    "stop": ["schema failure or scope escape"],
}
RUNNER_CONTRACT = copy.deepcopy(BASE_CONTRACT)
RUNNER_CONTRACT["head"] = None


def architect_event(state: str, contract: dict = BASE_CONTRACT) -> dict:
    return {
        "state": state,
        "ref": contract["ref"],
        "head": contract["head"],
        "review": {"decision": state, "summary": f"fixture {state}"},
    }


def executor_event(
    status: str,
    changed_files: list[str] | None = None,
    blocker=None,
    contract: dict = BASE_CONTRACT,
) -> dict:
    return {
        "status": status,
        "ref": contract["ref"],
        "head": contract["head"],
        "summary": f"fixture {status}",
        "changed_files": changed_files or [],
        "evidence": ["fixture evidence"],
        "blocker": blocker,
    }


class FakeTransport:
    def __init__(self, events, write_first=False, mutate_architect=False):
        self.events = list(events)
        self.calls = []
        self.prompts = []
        self.write_first = write_first
        self.mutate_architect = mutate_architect

    def invoke(self, role, workspace, prompt, timeout_seconds, expected_ref=None, expected_head=None):
        self.calls.append(role)
        self.prompts.append((role, prompt))
        event = copy.deepcopy(self.events.pop(0))
        if role == "architect" and self.mutate_architect:
            (workspace / "unexpected.txt").write_text("mutation", encoding="utf-8")
        if role == "executor" and self.write_first:
            (workspace / "result.txt").write_text("orchestra-smoke", encoding="utf-8")
            self.write_first = False
        config = ROLE_CONFIG[role]
        return InvocationResult(
            event,
            {
                "model": config.model,
                "reasoning_effort": config.effort,
                "sandbox": config.sandbox,
                "approval_policy": "never",
                "strict_config": True,
                "ignore_user_config": True,
                "exit_code": 0,
                "schema_valid": True,
                "output_source": "fake",
                "event_types": ["item.completed"],
            },
        )


class RunnerTests(unittest.TestCase):
    def test_schemas_and_semantic_required_fields(self):
        validate_architect(architect_event("READY"), BASE_CONTRACT["ref"], HEAD)
        validate_executor(executor_event("DONE", ["result.txt"]), BASE_CONTRACT["ref"], HEAD, BASE_CONTRACT)

        bad_architect = architect_event("READY")
        bad_architect["unexpected"] = True
        with self.assertRaises(ProtocolError):
            validate_architect(bad_architect, BASE_CONTRACT["ref"], HEAD)

        bad_contract = architect_event("READY")
        bad_contract["contract"] = copy.deepcopy(
            {key: BASE_CONTRACT[key] for key in BASE_CONTRACT if key not in {"ref", "head"}}
        )
        with self.assertRaises(ProtocolError):
            validate_architect(bad_contract, BASE_CONTRACT["ref"], HEAD)

        bad_executor = executor_event("DONE", blocker="must be null")
        with self.assertRaises(ProtocolError):
            validate_executor(bad_executor, BASE_CONTRACT["ref"], HEAD, BASE_CONTRACT)

        for status in ("DONE", "UPDATED"):
            with self.assertRaises(ProtocolError):
                validate_executor(executor_event(status), BASE_CONTRACT["ref"], HEAD, BASE_CONTRACT)

        bad_gate = executor_event("READY_HUMAN_AUTH", blocker=None)
        with self.assertRaises(ProtocolError):
            validate_executor(bad_gate, BASE_CONTRACT["ref"], HEAD, BASE_CONTRACT)

    def test_identity_constrained_schemas_accept_only_supplied_routing_identity(self):
        for role, payload in (
            ("architect", architect_event("READY")),
            ("executor", executor_event("DONE")),
        ):
            schema = identity_constrained_schema(role, BASE_CONTRACT["ref"], HEAD)
            validate_json_schema(payload, schema)
            for key, value in (
                ("ref", "SELF"),
                ("ref", "issue:56"),
                ("head", "deadbeef"),
                ("head", None),
            ):
                invalid = copy.deepcopy(payload)
                invalid[key] = value
                with self.assertRaises(ProtocolError, msg=f"{role} accepted {key}={value!r}"):
                    validate_json_schema(invalid, schema)

    def test_architect_prompt_contains_host_contract_once_without_output_copy(self):
        contract_json = json.dumps(RUNNER_CONTRACT, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        prompt = architect_prompt(RUNNER_CONTRACT, "initial")
        self.assertEqual(prompt.count(contract_json), 1)
        review_prompt = architect_prompt(
            RUNNER_CONTRACT,
            "review",
            executor_event("DONE", ["result.txt"], contract=RUNNER_CONTRACT),
        )
        self.assertEqual(review_prompt.count(contract_json), 1)
        self.assertNotIn("read-only path", prompt)
        self.assertIn("Emit only state, ref, head, and review", prompt)

    def test_host_contract_is_unchanged_through_review_and_correction(self):
        original = copy.deepcopy(RUNNER_CONTRACT)
        transport = FakeTransport(
            [
                architect_event("READY", original),
                executor_event("DONE", ["result.txt"], contract=original),
                architect_event("FIX_REQUIRED", original),
                executor_event("NO_CHANGE", contract=original),
                architect_event("MERGE_READY", original),
            ],
            write_first=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            runner = DeterministicRunner(original, Path(directory), transport)
            result = runner.run()

        self.assertEqual(result["terminal"], "MERGE_READY")
        self.assertEqual(runner.contract, original)
        executor_prompts = [prompt for role, prompt in transport.prompts if role == "executor"]
        expected_contract = json.dumps(original, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        self.assertEqual(len(executor_prompts), 2)
        self.assertTrue(all(expected_contract in prompt for prompt in executor_prompts))
        self.assertTrue(all("do not inspect unrelated files" in prompt for prompt in executor_prompts))
        self.assertTrue(all("immediately return exactly one JSON object" in prompt for prompt in executor_prompts))
        self.assertNotIn(expected_contract, json.dumps(result, ensure_ascii=True, sort_keys=True))
        self.assertIn("ARCHITECT REVIEW DELTA JSON", executor_prompts[1])
        self.assertIn('"decision":"FIX_REQUIRED"', executor_prompts[1])
        self.assertNotIn('"allowed_paths":["other.txt"]', executor_prompts[1])
        self.assertEqual(result["trace"][2]["review"], {"decision": "FIX_REQUIRED", "summary": "fixture FIX_REQUIRED"})
        self.assertEqual(
            result["isolation"][3]["architect_review"],
            {"decision": "FIX_REQUIRED", "summary": "fixture FIX_REQUIRED"},
        )

    def test_generated_identity_schema_is_used_for_both_role_commands(self):
        captured_commands = []

        def fake_run(command, **kwargs):
            if command[0] == "git":
                return subprocess.CompletedProcess(command, 0, "", "")
            captured_commands.append(command)
            role = "architect" if "gpt-5.6-sol" in command else "executor"
            payload = architect_event("READY") if role == "architect" else executor_event("DONE")
            event = {"type": "item.completed", "item": {"text": json.dumps(payload)}}
            return subprocess.CompletedProcess(command, 0, json.dumps(event) + "\n", "")

        transport = CodexTransport(command="codex.cmd")
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            with patch("runner.subprocess.run", side_effect=fake_run):
                for role in ("architect", "executor"):
                    transport.invoke(role, workspace, "fixture", 1, BASE_CONTRACT["ref"], HEAD)

        self.assertEqual(len(captured_commands), 2)
        for role, command in zip(("architect", "executor"), captured_commands):
            schema_path = Path(command[command.index("--output-schema") + 1])
            self.assertEqual(schema_path.name, f"{role}.json")
            self.assertFalse(schema_path.exists())
            self.assertNotIn(".orchestra", schema_path.parts)

    def test_happy_path_is_finite_and_checks_scoped_change(self):
        transport = FakeTransport(
            [
                architect_event("READY", RUNNER_CONTRACT),
                executor_event("DONE", ["result.txt"], contract=RUNNER_CONTRACT),
                architect_event("MERGE_READY", RUNNER_CONTRACT),
            ],
            write_first=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            result = DeterministicRunner(RUNNER_CONTRACT, Path(directory), transport).run()

        self.assertEqual(result["terminal"], "MERGE_READY")
        self.assertEqual(result["calls"], 3)
        self.assertEqual(result["handoffs"], 2)
        self.assertEqual(transport.calls, ["architect", "executor", "architect"])
        self.assertTrue(all(item["unchanged"] for item in result["isolation"] if item["role"] == "architect"))
        self.assertEqual(result["isolation"][1]["changed_files"], ["result.txt"])
        self.assertTrue(result["preflight"]["matched"])
        self.assertFalse(result["preflight"]["tracked_git"])

    def test_correction_route_is_one_bounded_rereview(self):
        transport = FakeTransport(
            [
                architect_event("READY", RUNNER_CONTRACT),
                executor_event("DONE", ["result.txt"], contract=RUNNER_CONTRACT),
                architect_event("FIX_REQUIRED", RUNNER_CONTRACT),
                executor_event("NO_CHANGE", contract=RUNNER_CONTRACT),
                architect_event("MERGE_READY", RUNNER_CONTRACT),
            ],
            write_first=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            result = DeterministicRunner(RUNNER_CONTRACT, Path(directory), transport).run()

        self.assertEqual(result["terminal"], "MERGE_READY")
        self.assertTrue(result["correction_used"])
        self.assertEqual(result["calls"], 5)
        self.assertEqual(result["handoffs"], 4)
        self.assertEqual(transport.calls, ["architect", "executor", "architect", "executor", "architect"])

    def test_correction_budget_stops_without_a_sixth_call(self):
        transport = FakeTransport(
            [
                architect_event("READY", RUNNER_CONTRACT),
                executor_event("DONE", ["result.txt"], contract=RUNNER_CONTRACT),
                architect_event("FIX_REQUIRED", RUNNER_CONTRACT),
                executor_event("NO_CHANGE", contract=RUNNER_CONTRACT),
                architect_event("FIX_REQUIRED", RUNNER_CONTRACT),
            ],
            write_first=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ProtocolError) as context:
                DeterministicRunner(RUNNER_CONTRACT, Path(directory), transport).run()

        self.assertEqual(context.exception.code, "duplicate_or_no_progress")
        self.assertEqual(len(transport.calls), 5)

    def test_human_and_security_gates_stop_before_executor(self):
        for state in ("HUMAN_AUTH", "SECURITY_BLOCKED", "BLOCKED"):
            transport = FakeTransport([architect_event(state, RUNNER_CONTRACT)])
            with tempfile.TemporaryDirectory() as directory:
                result = DeterministicRunner(RUNNER_CONTRACT, Path(directory), transport).run()
            self.assertEqual(result["terminal"], state)
            self.assertEqual(result["calls"], 1)
            self.assertEqual(transport.calls, ["architect"])

    def test_executor_human_gate_routes_to_architect_then_stops(self):
        transport = FakeTransport(
            [
                architect_event("READY", RUNNER_CONTRACT),
                executor_event("READY_HUMAN_AUTH", blocker="human action required", contract=RUNNER_CONTRACT),
                architect_event("HUMAN_AUTH", RUNNER_CONTRACT),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            result = DeterministicRunner(RUNNER_CONTRACT, Path(directory), transport).run()
        self.assertEqual(result["terminal"], "HUMAN_AUTH")
        self.assertEqual(transport.calls, ["architect", "executor", "architect"])

    def test_executor_review_matrix_rejects_incompatible_success_states(self):
        for status in ("BLOCKED", "READY_HUMAN_AUTH", "SECURITY_BLOCKED"):
            with self.assertRaises(ProtocolError):
                route_executor_review(status, "MERGE_READY", correction_available=True)
        with self.assertRaises(ProtocolError):
            route_executor_review("READY_HUMAN_AUTH", "BLOCKED", correction_available=True)
        with self.assertRaises(ProtocolError):
            route_executor_review("SECURITY_BLOCKED", "HUMAN_AUTH", correction_available=True)

        self.assertEqual(route_executor_review("BLOCKED", "FIX_REQUIRED", correction_available=True), "executor")
        self.assertEqual(route_executor_review("BLOCKED", "BLOCKED", correction_available=True), "stop")
        for status in ("DONE", "UPDATED", "NO_CHANGE"):
            self.assertEqual(route_executor_review(status, "MERGE_READY", correction_available=True), "stop")

    def test_incompatible_review_preserves_bounded_executor_delta_in_stop_report(self):
        blocked = executor_event("BLOCKED", blocker="no safe change", contract=RUNNER_CONTRACT)
        transport = FakeTransport(
            [architect_event("READY", RUNNER_CONTRACT), blocked, architect_event("MERGE_READY", RUNNER_CONTRACT)]
        )
        with tempfile.TemporaryDirectory() as directory:
            runner = DeterministicRunner(RUNNER_CONTRACT, Path(directory), transport)
            with self.assertRaises(ProtocolError) as context:
                runner.run()
            report = runner.report("STOP", context.exception.code)

        self.assertEqual(context.exception.code, "executor_review_incompatible")
        self.assertEqual(report["terminal"], "STOP")
        self.assertEqual(report["trace"][1]["executor_delta"]["status"], "BLOCKED")
        self.assertEqual(report["trace"][1]["executor_delta"]["summary"], "fixture BLOCKED")
        self.assertEqual(report["trace"][1]["executor_delta"]["blocker"], "no safe change")
        self.assertEqual(report["trace"][1]["executor_delta"]["changed_files"], [])
        self.assertEqual(report["trace"][2]["review"]["decision"], "MERGE_READY")

    def test_stale_reference_and_scope_escape_fail_closed(self):
        stale = architect_event("READY")
        stale["head"] = "deadbeef"
        with self.assertRaises(ProtocolError):
            validate_architect(stale, BASE_CONTRACT["ref"], HEAD)

        transport = FakeTransport(
            [
                architect_event("READY", RUNNER_CONTRACT),
                executor_event("DONE", ["other.txt"], contract=RUNNER_CONTRACT),
            ],
            write_first=False,
        )
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "other.txt").write_text("escape", encoding="utf-8")
            with self.assertRaises(ProtocolError):
                DeterministicRunner(RUNNER_CONTRACT, Path(directory), transport).run()

    def test_architect_mutation_is_rejected(self):
        transport = FakeTransport([architect_event("READY", RUNNER_CONTRACT)], mutate_architect=True)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ProtocolError):
                DeterministicRunner(RUNNER_CONTRACT, Path(directory), transport).run()

    def test_workspace_head_mismatch_stops_before_transport(self):
        mismatch = copy.deepcopy(BASE_CONTRACT)
        mismatch["head"] = "0" * 40
        transport = FakeTransport([architect_event("READY", mismatch)])
        runner = DeterministicRunner(mismatch, ORCHESTRA_DIR, transport)
        with self.assertRaises(ProtocolError) as context:
            runner.run()

        self.assertEqual(context.exception.code, "workspace_head_mismatch")
        self.assertEqual(runner.calls, 0)
        self.assertEqual(runner.handoffs, 0)
        self.assertFalse(runner.preflight["matched"])
        self.assertEqual(transport.calls, [])

    def test_matching_workspace_head_proceeds_and_null_head_allows_non_git_fixture(self):
        identity = tracked_workspace_identity(ORCHESTRA_DIR)
        self.assertIsNotNone(identity)
        matched = copy.deepcopy(BASE_CONTRACT)
        matched["head"] = identity["head"]
        matched_transport = FakeTransport([architect_event("BLOCKED", matched)])
        matched_result = DeterministicRunner(matched, ORCHESTRA_DIR, matched_transport).run()
        self.assertEqual(matched_result["terminal"], "BLOCKED")
        self.assertEqual(matched_result["calls"], 1)
        self.assertTrue(matched_result["preflight"]["matched"])
        self.assertTrue(matched_result["preflight"]["tracked_git"])

        with tempfile.TemporaryDirectory() as directory:
            null_transport = FakeTransport([architect_event("BLOCKED", RUNNER_CONTRACT)])
            null_result = DeterministicRunner(RUNNER_CONTRACT, Path(directory), null_transport).run()
        self.assertEqual(null_result["terminal"], "BLOCKED")
        self.assertFalse(null_result["preflight"]["required"])
        self.assertFalse(null_result["preflight"]["tracked_git"])
        self.assertTrue(null_result["preflight"]["matched"])

    def test_non_git_workspace_with_required_head_stops_before_transport(self):
        transport = FakeTransport([architect_event("READY")])
        with tempfile.TemporaryDirectory() as directory:
            runner = DeterministicRunner(BASE_CONTRACT, Path(directory), transport)
            with self.assertRaises(ProtocolError) as context:
                runner.run()
        self.assertEqual(context.exception.code, "workspace_identity_missing")
        self.assertEqual(runner.calls, 0)
        self.assertEqual(runner.handoffs, 0)
        self.assertFalse(runner.preflight["tracked_git"])

    def test_architect_review_delta_is_bounded_and_sanitized(self):
        event = architect_event("BLOCKED", RUNNER_CONTRACT)
        event["review"]["summary"] = "token: do-not-retain " + ("x" * 600)
        transport = FakeTransport([event])
        with tempfile.TemporaryDirectory() as directory:
            result = DeterministicRunner(RUNNER_CONTRACT, Path(directory), transport).run()
        summary = result["trace"][0]["review"]["summary"]
        self.assertNotIn("do-not-retain", summary)
        self.assertIn("[REDACTED]", summary)
        self.assertLessEqual(len(summary), 512)

    def test_route_rejects_invalid_transition_and_builds_exact_role_configs(self):
        with self.assertRaises(ProtocolError):
            route_architect("EXECUTING", "initial")
        self.assertEqual(route_architect("FIX_REQUIRED", "review", correction_available=True), "executor")

        transport = CodexTransport(command="codex.cmd")
        with tempfile.TemporaryDirectory() as directory:
            for role, model, sandbox, effort in (
                ("architect", "gpt-5.6-sol", "read-only", "high"),
                ("executor", "gpt-5.6-luna", "workspace-write", "max"),
            ):
                with self.subTest(role=role):
                    command = transport.build_command(
                        role,
                        Path(directory),
                        ORCHESTRA_DIR / "schema" / f"{role}.schema.json",
                        Path(directory) / "final.json",
                        "fixture",
                    )
                    self.assertIn(model, command)
                    self.assertIn(sandbox, command)
                    self.assertIn(f'model_reasoning_effort="{effort}"', command)
                    self.assertIn("--strict-config", command)
                    self.assertIn("--ignore-user-config", command)
                    self.assertIn("--ignore-rules", command)
                    if os.name == "nt":
                        self.assertIn('windows.sandbox="elevated"', command)
                    else:
                        self.assertNotIn('windows.sandbox="elevated"', command)
                    approval_index = command.index("--ask-for-approval")
                    self.assertLess(approval_index, command.index("exec"))
                    self.assertEqual(command[approval_index + 1], "never")
                    self.assertIn("--json", command)
                    self.assertNotIn("--output-last-message", command)
                    self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
                    self.assertNotIn("--add-dir", command)

        with patch("runner.os.name", "nt"):
            windows_command = transport.build_command(
                "executor",
                Path(tempfile.gettempdir()),
                ORCHESTRA_DIR / "schema" / "executor.schema.json",
                Path(tempfile.gettempdir()) / "final.json",
                "fixture",
            )
        self.assertIn('windows.sandbox="elevated"', windows_command)

        self.assertEqual(
            classify_process_failure("error: unexpected argument '--ask-for-approval'", ""),
            "cli_parse_or_config_failure",
        )
        self.assertEqual(classify_process_failure("service unavailable", ""), "model_or_runtime_failure")

    def test_subprocess_timeout_is_bounded_and_not_retried(self):
        transport = CodexTransport(command="codex.cmd")
        with tempfile.TemporaryDirectory() as directory:
            with patch("runner.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="codex", timeout=1)):
                with self.assertRaises(TransportError) as context:
                    transport.invoke("architect", Path(directory), "fixture", 1, BASE_CONTRACT["ref"], HEAD)
        self.assertEqual(context.exception.code, "subprocess_timeout")


if __name__ == "__main__":
    unittest.main(verbosity=2)
