"""Bounded, fail-closed Chromium readiness CLI for local validation."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

import measurement.browser_session as browser_session
from measurement.browser_session import (
    ChromiumCleanup,
    ChromiumEndpoint,
    ChromiumExceptionInfo,
    ChromiumSession,
    ChromiumSessionDiagnostics,
    ChromiumSessionError,
)

_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")
_SAFE_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,80}$")
_SAFE_STAGE = re.compile(r"^[a-z][a-z0-9_]{0,64}$")
_SAFE_PROCESS_STATE = re.compile(r"^(?:not_started|running|unknown|exited:-?[0-9]+)$")
_SAFE_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _safe_text(value: object) -> str:
    return browser_session._sanitize_text(str(value)) or "<no message>"


def _safe_type(value: object) -> str:
    text = str(value)
    return text if _SAFE_TYPE.fullmatch(text) else "<redacted>"


def _safe_code(value: object, message: str) -> str | None:
    candidate = str(value).strip() if value is not None else ""
    if _SAFE_CODE.fullmatch(candidate):
        return candidate
    candidate = message.split(":", 1)[0].strip()
    return candidate if _SAFE_CODE.fullmatch(candidate) else None


def _error_payload(info: ChromiumExceptionInfo | None) -> dict[str, Any] | None:
    if info is None:
        return None
    message = _safe_text(info.message)
    return {
        "type": _safe_type(info.type_name),
        "code": _safe_code(getattr(info, "code", None), message),
        "message": message,
    }


def _exception_payload(exc: BaseException) -> dict[str, Any]:
    info = browser_session._exception_info(exc)
    return _error_payload(info) or {
        "type": "Exception",
        "code": None,
        "message": "<no message>",
    }


def _cause_payload(cause_chain: tuple[ChromiumExceptionInfo, ...]) -> list[dict[str, Any]]:
    return [payload for payload in (_error_payload(item) for item in cause_chain) if payload]


def _safe_stage(value: object) -> str:
    text = str(value)
    return text if _SAFE_STAGE.fullmatch(text) else "<redacted>"


def _safe_process_state(value: object) -> str:
    text = str(value)
    return text if _SAFE_PROCESS_STATE.fullmatch(text) else "<redacted>"


def _endpoint_payload(endpoint: ChromiumEndpoint | None) -> dict[str, Any] | None:
    if endpoint is None:
        return None
    host = str(endpoint.host).lower()
    if host not in _LOOPBACK_HOSTS:
        return None
    port = int(endpoint.port) if isinstance(endpoint.port, int) else None
    if port is None or not 1 <= port <= 65535:
        return None
    return {
        "scheme": "ws" if endpoint.scheme == "ws" else None,
        "host": host,
        "port": port,
        "path_prefix": "/devtools/page/" if endpoint.path_prefix == "/devtools/page/" else None,
    }


def _stream_payload(stream: Any) -> dict[str, Any] | None:
    if stream is None:
        return None
    try:
        size_bytes = int(stream.size_bytes)
    except (AttributeError, TypeError, ValueError):
        size_bytes = None
    digest = str(getattr(stream, "sha256", ""))
    return {
        "size_bytes": size_bytes if size_bytes is not None and size_bytes >= 0 else None,
        "sha256": digest if _SAFE_SHA256.fullmatch(digest) else None,
        "safe_tail": _safe_text(getattr(stream, "safe_tail", "")),
    }


def _empty_cleanup() -> dict[str, Any]:
    return {
        "available": False,
        "websocket_closed": None,
        "process_closed": None,
        "profile_removed": None,
        "output_drained": None,
        "error": None,
        "ok": False,
    }


def _cleanup_payload(cleanup: ChromiumCleanup | None) -> dict[str, Any]:
    if cleanup is None:
        return _empty_cleanup()
    return {
        "available": True,
        "websocket_closed": bool(cleanup.websocket_closed),
        "process_closed": bool(cleanup.process_closed),
        "profile_removed": bool(cleanup.profile_removed),
        "output_drained": bool(cleanup.output_drained),
        "error": _error_payload(cleanup.error),
        "ok": bool(cleanup.ok),
    }


def _diagnostic_payload(
    diagnostics: ChromiumSessionDiagnostics,
    cleanup: ChromiumCleanup | None = None,
) -> dict[str, Any]:
    effective_cleanup = cleanup if cleanup is not None else diagnostics.cleanup
    return {
        "available": True,
        "stage": _safe_stage(diagnostics.stage),
        "error": _error_payload(diagnostics.error),
        "cause_chain": _cause_payload(diagnostics.cause_chain),
        "process_state": _safe_process_state(diagnostics.process_state),
        "process_created": bool(getattr(diagnostics, "process_created", False)),
        "endpoint": _endpoint_payload(diagnostics.endpoint),
        "stdout": _stream_payload(diagnostics.stdout),
        "stderr": _stream_payload(diagnostics.stderr),
        "cleanup": _cleanup_payload(effective_cleanup),
    }


def _runner_diagnostic(
    stage: str,
    exc: BaseException,
    process_state: str,
    process_created: bool,
    endpoint: ChromiumEndpoint | None,
    cleanup: ChromiumCleanup | None,
) -> dict[str, Any]:
    return {
        "available": True,
        "stage": _safe_stage(stage),
        "error": _exception_payload(exc),
        "cause_chain": _cause_payload(browser_session._cause_chain(exc)),
        "process_state": _safe_process_state(process_state),
        "process_created": bool(process_created),
        "endpoint": _endpoint_payload(endpoint),
        "stdout": None,
        "stderr": None,
        "cleanup": _cleanup_payload(cleanup),
    }


def _executable_identity(executable_path: str) -> dict[str, Any]:
    identity: dict[str, Any] = {
        "input_provided": bool(executable_path),
        "path": "<redacted>",
        "exists": False,
        "file_type": "unavailable",
        "executable": False,
        "architecture": None,
        "verified": False,
    }
    if not executable_path:
        return identity
    try:
        path = Path(executable_path)
        identity["exists"] = path.exists()
        identity["file_type"] = "regular" if path.is_file() else "non_regular"
        if identity["file_type"] != "regular":
            return identity
        if os.name == "nt":
            identity["executable"] = path.suffix.lower() in {".exe", ".com", ".bat", ".cmd"}
        else:
            mode = path.stat().st_mode
            identity["executable"] = bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
        identity["verified"] = bool(identity["executable"])
    except (OSError, ValueError):
        identity["file_type"] = "unavailable"
    return identity


def _session_process_state(session: Any) -> str:
    process = getattr(session, "process", None)
    if process is None:
        return "not_started"
    try:
        code = process.poll()
    except Exception:
        return "unknown"
    return "running" if code is None else f"exited:{int(code)}"


def _session_process_created(session: Any, start_returned: bool) -> bool:
    if bool(getattr(session, "_process_created", False)):
        return True
    return bool(start_returned and getattr(session, "process", None) is not None)


def _owned_residue_clear(session: Any) -> bool:
    required = ("process", "ws", "user_data_dir", "read_task")
    return all(hasattr(session, name) and getattr(session, name) is None for name in required)


def _base_report(identity: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "chromium-readiness.v1",
        "executable": identity,
        "start_attempt_count": 0,
        "start_returned": False,
        "stage": "preflight",
        "process_state": "not_started",
        "process_creation_proven": False,
        "endpoint": None,
        "browser_version": None,
        "evaluation_count": 0,
        "evaluation_value": None,
        "close_attempted": False,
        "close_awaited": False,
        "cleanup": _empty_cleanup(),
        "owned_residue_clear": False,
        "diagnostics": None,
        "ready": False,
    }


async def run_readiness(
    executable_path: str,
    timeout_seconds: float = 10.0,
) -> tuple[dict[str, Any], int]:
    """Run exactly one inert Chromium lifecycle and return report plus exit code."""
    identity = _executable_identity(executable_path)
    report = _base_report(identity)
    if not identity["verified"] or not (0.0 < float(timeout_seconds) <= 120.0):
        report["diagnostics"] = _runner_diagnostic(
            "executable_validation",
            ValueError("CHROMIUM_EXECUTABLE_OR_TIMEOUT_INVALID"),
            "not_started",
            False,
            None,
            None,
        )
        return report, 1

    session: Any = None
    cleanup: ChromiumCleanup | None = None
    typed_diagnostics: ChromiumSessionDiagnostics | None = None
    runner_failure: tuple[str, BaseException] | None = None
    endpoint: ChromiumEndpoint | None = None
    start_returned = False
    try:
        session = ChromiumSession(
            executable_path=executable_path,
            timeout_seconds=float(timeout_seconds),
        )
        report["start_attempt_count"] = 1
        await session.start()
        start_returned = True
        report["start_returned"] = True
        endpoint = getattr(session, "endpoint", None)
        report["endpoint"] = _endpoint_payload(endpoint)
        browser_version = _safe_text(getattr(session, "browser_version", ""))
        report["browser_version"] = browser_version

        if _endpoint_payload(endpoint) is None:
            raise RuntimeError("CHROMIUM_LOOPBACK_CDP_NOT_VALIDATED")
        if not browser_version or browser_version == "unknown":
            raise RuntimeError("CHROMIUM_BROWSER_VERSION_NOT_AVAILABLE")

        report["evaluation_count"] = 1
        evaluation = await session.evaluate_script("1 + 1")
        if isinstance(evaluation, bool) or evaluation != 2:
            raise RuntimeError("CHROMIUM_INERT_EVALUATION_NOT_TWO")
        report["evaluation_value"] = 2
        report["stage"] = "ready_check"
        report["process_state"] = _session_process_state(session)
        report["process_creation_proven"] = _session_process_created(session, True)
    except ChromiumSessionError as exc:
        typed_diagnostics = exc.diagnostics
        endpoint = typed_diagnostics.endpoint
        report["stage"] = _safe_stage(typed_diagnostics.stage)
        report["process_state"] = _safe_process_state(typed_diagnostics.process_state)
        report["process_creation_proven"] = bool(
            getattr(typed_diagnostics, "process_created", False)
        )
    except Exception as exc:
        runner_failure = ("evaluation" if start_returned else "startup", exc)
    finally:
        if session is not None:
            if start_returned:
                report["close_attempted"] = True
                try:
                    cleanup = await session.aclose()
                    report["close_awaited"] = True
                except ChromiumSessionError as exc:
                    report["close_awaited"] = True
                    cleanup = exc.diagnostics.cleanup
                    if typed_diagnostics is None and runner_failure is None:
                        typed_diagnostics = exc.diagnostics
                    elif runner_failure is None:
                        typed_diagnostics = exc.diagnostics
                except Exception as exc:
                    report["close_awaited"] = True
                    cleanup = getattr(session, "last_cleanup", None)
                    if runner_failure is None and typed_diagnostics is None:
                        runner_failure = ("cleanup", exc)
            else:
                cleanup = getattr(session, "last_cleanup", None)
                report["close_awaited"] = cleanup is not None
            report["cleanup"] = _cleanup_payload(cleanup)
            report["owned_residue_clear"] = _owned_residue_clear(session)

    report["process_state"] = report["process_state"] or _session_process_state(session)
    report["process_creation_proven"] = bool(
        report["process_creation_proven"]
        or (session is not None and _session_process_created(session, start_returned))
    )
    if typed_diagnostics is not None:
        report["diagnostics"] = _diagnostic_payload(typed_diagnostics, cleanup)
        report["stage"] = _safe_stage(typed_diagnostics.stage)
        report["process_state"] = _safe_process_state(typed_diagnostics.process_state)
        report["process_creation_proven"] = bool(
            getattr(typed_diagnostics, "process_created", False)
        )
        report["endpoint"] = _endpoint_payload(typed_diagnostics.endpoint)
    elif runner_failure is not None:
        failure_stage, failure = runner_failure
        report["diagnostics"] = _runner_diagnostic(
            failure_stage,
            failure,
            str(report["process_state"]),
            bool(report["process_creation_proven"]),
            endpoint,
            cleanup,
        )
        report["stage"] = _safe_stage(failure_stage)

    cleanup_ok = bool(cleanup is not None and cleanup.ok)
    endpoint_ok = _endpoint_payload(endpoint) is not None
    browser_version_ok = bool(report["browser_version"]) and report["browser_version"] != "unknown"
    report["ready"] = bool(
        identity["verified"]
        and start_returned
        and endpoint_ok
        and browser_version_ok
        and report["evaluation_count"] == 1
        and report["evaluation_value"] == 2
        and report["close_awaited"]
        and cleanup_ok
        and report["owned_residue_clear"]
        and report["diagnostics"] is None
    )
    return report, 0 if report["ready"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one local Chromium readiness lifecycle")
    parser.add_argument("--executable", required=True, help="Exact Chromium executable path")
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    args = parser.parse_args(argv)
    report, exit_code = asyncio.run(
        run_readiness(args.executable, timeout_seconds=args.timeout_seconds)
    )
    print(json.dumps(report, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
