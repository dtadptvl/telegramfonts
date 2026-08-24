#!/usr/bin/env python3
"""CLI for one host-owned Executor handoff."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from executor_handoff import (
    EventLedger,
    GitHubClient,
    HandoffError,
    default_state_path,
    execute_github_handoff,
    parse_github_event,
    parse_local_event,
    read_json_file,
)


def _stop_result(reason: str) -> dict[str, str]:
    return {"protocol": "orchestra/executor/v1", "terminal": "STOP", "reason": reason}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one bounded Executor event")
    parser.add_argument("command", nargs="?", choices=("execute",), default="execute")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--github-event", type=Path, help="Actions event payload; omit for manual discovery")
    source.add_argument("--local-event", type=Path, help="Desktop pointer event; contract is still recovered from GitHub")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--state-file", type=Path, default=None)
    parser.add_argument("--repository", default="dtadptvl/telegramfonts")
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--github-cli", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--codex", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    try:
        event = None
        if args.github_event is not None:
            event = parse_github_event(
                read_json_file(args.github_event),
                expected_repository=args.repository,
            )
        elif args.local_event is not None:
            event = parse_local_event(
                read_json_file(args.local_event),
                expected_repository=args.repository,
            )
        github = GitHubClient(command=args.github_cli)
        ledger = EventLedger(args.state_file or default_state_path())
        # --codex is retained only as a deterministic test seam; the active
        # command configuration remains Luna/max/workspace-write in code.
        invoker = None
        if args.codex is not None:
            from executor_handoff import LunaInvoker

            invoker = LunaInvoker(command=args.codex)
        result = execute_github_handoff(
            workspace=args.workspace,
            github=github,
            ledger=ledger,
            invoker=invoker,
            event=event,
            repository=args.repository,
            timeout_seconds=args.timeout_seconds,
        )
    except HandoffError as error:
        result = _stop_result(error.code)
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 2 if result.get("terminal") in {"STOP", "BLOCKED", "READY_HUMAN_AUTH", "SECURITY_BLOCKED"} else 0


if __name__ == "__main__":
    sys.exit(main())
