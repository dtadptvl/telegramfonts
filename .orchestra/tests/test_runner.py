"""Focused deterministic transport tests; stdlib-only so CI needs no new dependency."""

from __future__ import annotations

import copy
import json
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
    classify_process_failure,
    identity_constrained_schema,
    route_architect,
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


def architect_event(state: str) -> dict:
    return {
        "state": state,
        "ref": BASE_CONTRACT["ref"],
        "head": BASE_CONTRACT["head"],
        "contract": copy.deepcopy({key: BASE_CONTRACT[key] for key in BASE_CONTRACT if key not in {"ref", "head"}}),
        "review": {"decision": state, "summary": f"fixture {state}"},
    }


def executor_event(status: str, changed_files: list[str] | None = None, blocker=None) -> dict:
    return {
        "status": status,
        "ref": BASE_CONTRACT["ref"],
        "head": BASE_CONTRACT["head"],
        "summary": f"fixture {status}",
        "changed_files": changed_files or [],
        "evidence": ["fixture evidence"],
        "blocker": blocker,
    }


class FakeTransport:
    def __init__(self, events, write_first=False, mutate_architect=False):
        self.events = list(events)
        self.calls = []
        self.write_first = write_first
        self.mutate_architect = mutate_architect

    def invoke(self, role, workspace, prompt, timeout_seconds, expected_ref=None, expected_head=None):
        self.calls.append(role)
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
        validate_architect(architect_event("READY"), BASE_CONTRACT["ref"], HEAD, BASE_CONTRACT)
        validate_executor(executor_event("DONE"), BASE_CONTRACT["ref"], HEAD, BASE_CONTRACT)

        bad_architect = architect_event("READY")
        bad_architect["unexpected"] = True
        with self.assertRaises(ProtocolError):
            validate_architect(bad_architect, BASE_CONTRACT["ref"], HEAD, BASE_CONTRACT)

        bad_executor = executor_event("DONE", blocker="must be null")
        with self.assertRaises(ProtocolError):
            validate_executor(bad_executor, BASE_CONTRACT["ref"], HEAD, BASE_CONTRACT)

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
            [architect_event("READY"), executor_event("DONE", ["result.txt"]), architect_event("MERGE_READY")],
            write_first=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            result = DeterministicRunner(BASE_CONTRACT, Path(directory), transport).run()

        self.assertEqual(result["terminal"], "MERGE_READY")
        self.assertEqual(result["calls"], 3)
        self.assertEqual(result["handoffs"], 2)
        self.assertEqual(transport.calls, ["architect", "executor", "architect"])
        self.assertTrue(all(item["unchanged"] for item in result["isolation"] if item["role"] == "architect"))
        self.assertEqual(result["isolation"][1]["changed_files"], ["result.txt"])

    def test_correction_route_is_one_bounded_rereview(self):
        transport = FakeTransport(
            [
                architect_event("READY"),
                executor_event("DONE", ["result.txt"]),
                architect_event("FIX_REQUIRED"),
                executor_event("NO_CHANGE"),
                architect_event("MERGE_READY"),
            ],
            write_first=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            result = DeterministicRunner(BASE_CONTRACT, Path(directory), transport).run()

        self.assertEqual(result["terminal"], "MERGE_READY")
        self.assertTrue(result["correction_used"])
        self.assertEqual(result["calls"], 5)
        self.assertEqual(result["handoffs"], 4)
        self.assertEqual(transport.calls, ["architect", "executor", "architect", "executor", "architect"])

    def test_correction_budget_stops_without_a_sixth_call(self):
        transport = FakeTransport(
            [
                architect_event("READY"),
                executor_event("DONE", ["result.txt"]),
                architect_event("FIX_REQUIRED"),
                executor_event("NO_CHANGE"),
                architect_event("FIX_REQUIRED"),
            ],
            write_first=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ProtocolError) as context:
                DeterministicRunner(BASE_CONTRACT, Path(directory), transport).run()

        self.assertEqual(context.exception.code, "duplicate_or_no_progress")
        self.assertEqual(len(transport.calls), 5)

    def test_human_and_security_gates_stop_before_executor(self):
        for state in ("HUMAN_AUTH", "SECURITY_BLOCKED", "BLOCKED"):
            transport = FakeTransport([architect_event(state)])
            with tempfile.TemporaryDirectory() as directory:
                result = DeterministicRunner(BASE_CONTRACT, Path(directory), transport).run()
            self.assertEqual(result["terminal"], state)
            self.assertEqual(result["calls"], 1)
            self.assertEqual(transport.calls, ["architect"])

    def test_executor_human_gate_routes_to_architect_then_stops(self):
        transport = FakeTransport(
            [
                architect_event("READY"),
                executor_event("READY_HUMAN_AUTH", blocker="human action required"),
                architect_event("HUMAN_AUTH"),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            result = DeterministicRunner(BASE_CONTRACT, Path(directory), transport).run()
        self.assertEqual(result["terminal"], "HUMAN_AUTH")
        self.assertEqual(transport.calls, ["architect", "executor", "architect"])

    def test_stale_reference_and_scope_escape_fail_closed(self):
        stale = architect_event("READY")
        stale["head"] = "deadbeef"
        with self.assertRaises(ProtocolError):
            validate_architect(stale, BASE_CONTRACT["ref"], HEAD, BASE_CONTRACT)

        transport = FakeTransport([architect_event("READY"), executor_event("DONE", ["other.txt"])], write_first=False)
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "other.txt").write_text("escape", encoding="utf-8")
            with self.assertRaises(ProtocolError):
                DeterministicRunner(BASE_CONTRACT, Path(directory), transport).run()

    def test_architect_mutation_is_rejected(self):
        transport = FakeTransport([architect_event("READY")], mutate_architect=True)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ProtocolError):
                DeterministicRunner(BASE_CONTRACT, Path(directory), transport).run()

    def test_route_rejects_invalid_transition_and_builds_exact_role_configs(self):
        with self.assertRaises(ProtocolError):
            route_architect("EXECUTING", "initial")
        self.assertEqual(route_architect("FIX_REQUIRED", "review", correction_available=True), "executor")

        transport = CodexTransport(command="codex.cmd")
        with tempfile.TemporaryDirectory() as directory:
            command = transport.build_command(
                "architect",
                Path(directory),
                ORCHESTRA_DIR / "schema" / "architect.schema.json",
                Path(directory) / "final.json",
                "fixture",
            )
        self.assertIn("gpt-5.6-sol", command)
        self.assertIn("read-only", command)
        self.assertIn('model_reasoning_effort="high"', command)
        self.assertIn("--strict-config", command)
        self.assertIn("--ignore-user-config", command)
        approval_index = command.index("--ask-for-approval")
        self.assertLess(approval_index, command.index("exec"))
        self.assertEqual(command[approval_index + 1], "never")
        self.assertIn("--json", command)
        self.assertNotIn("--output-last-message", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertNotIn("--add-dir", command)

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
