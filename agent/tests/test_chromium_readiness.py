from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import measurement.browser_session as browser_session
import measurement.chromium_readiness as readiness
from measurement.browser_session import (
    ChromiumCleanup,
    ChromiumEndpoint,
    ChromiumExceptionInfo,
    ChromiumSessionDiagnostics,
    ChromiumStreamEvidence,
)


def _cleanup(*, ok: bool = True) -> ChromiumCleanup:
    if ok:
        return ChromiumCleanup(True, True, True, True, None)
    return ChromiumCleanup(
        True,
        True,
        False,
        True,
        ChromiumExceptionInfo("OSError", "secret=do-not-print /tmp/private-profile"),
    )


def _diagnostics(*, process_created: bool, cleanup: ChromiumCleanup) -> ChromiumSessionDiagnostics:
    return ChromiumSessionDiagnostics(
        stage="websocket_handshake",
        error=ChromiumExceptionInfo(
            "InvalidMessage",
            "Authorization: Bearer secret-token at /tmp/private-profile",
        ),
        cause_chain=(
            ChromiumExceptionInfo(
                "ConnectionError",
                "target_id=opaque-target https://source.invalid/private",
            ),
        ),
        process_state="running" if process_created else "not_started",
        process_created=process_created,
        endpoint=ChromiumEndpoint("ws", "127.0.0.1", 9222, "/devtools/page/"),
        stdout=ChromiumStreamEvidence(
            4,
            "0" * 64,
            "stdout target_id=opaque-target /tmp/private-profile",
        ),
        stderr=ChromiumStreamEvidence(
            5,
            "1" * 64,
            "stderr Authorization: Bearer secret-token",
        ),
        cleanup=cleanup,
    )


def _executable(tmp_path: Path) -> str:
    path = tmp_path / ("chromium.exe" if os.name == "nt" else "chromium")
    path.write_bytes(b"fake")
    if os.name != "nt":
        path.chmod(0o700)
    return str(path)


@pytest.mark.asyncio
async def test_prelaunch_popen_failure_is_typed_and_fail_closed(monkeypatch, tmp_path: Path):
    executable = _executable(tmp_path)

    def fail_popen(*args, **kwargs):
        raise OSError("Authorization: Bearer secret-token /tmp/private-profile")

    monkeypatch.setattr(browser_session.subprocess, "Popen", fail_popen)
    report, exit_code = await readiness.run_readiness(executable)

    assert exit_code == 1
    assert report["start_attempt_count"] == 1
    assert report["start_returned"] is False
    assert report["diagnostics"]["stage"] == "launch"
    assert report["diagnostics"]["process_created"] is False
    assert report["process_creation_proven"] is False
    assert report["cleanup"]["available"] is True
    encoded = json.dumps(report)
    assert "secret-token" not in encoded
    assert "private-profile" not in encoded
    assert "/tmp/" not in encoded


class _TypedFailureSession:
    diagnostics = _diagnostics(process_created=True, cleanup=_cleanup())

    def __init__(self, executable_path: str, timeout_seconds: float) -> None:
        self.executable_path = executable_path
        self.timeout_seconds = timeout_seconds
        self.last_cleanup = self.diagnostics.cleanup
        self.process = None
        self.ws = None
        self.user_data_dir = None
        self.read_task = None

    async def start(self) -> None:
        raise readiness.ChromiumSessionError(self.diagnostics)


@pytest.mark.asyncio
async def test_post_spawn_diagnostics_are_serialized_without_raw_values(monkeypatch, tmp_path: Path):
    executable = _executable(tmp_path)
    monkeypatch.setattr(readiness, "ChromiumSession", _TypedFailureSession)

    report, exit_code = await readiness.run_readiness(executable)

    assert exit_code == 1
    assert report["diagnostics"]["stage"] == "websocket_handshake"
    assert report["diagnostics"]["process_created"] is True
    assert report["diagnostics"]["endpoint"] == {
        "scheme": "ws",
        "host": "127.0.0.1",
        "port": 9222,
        "path_prefix": "/devtools/page/",
    }
    encoded = json.dumps(report)
    for forbidden in ("secret-token", "opaque-target", "source.invalid", "/tmp/private-profile"):
        assert forbidden not in encoded
    assert report["diagnostics"]["stdout"]["size_bytes"] == 4
    assert report["diagnostics"]["stderr"]["sha256"] == "1" * 64


class _CleanupFailureSession:
    instances: list["_CleanupFailureSession"] = []

    def __init__(self, executable_path: str, timeout_seconds: float) -> None:
        self.executable_path = executable_path
        self.timeout_seconds = timeout_seconds
        self.endpoint = ChromiumEndpoint("ws", "127.0.0.1", 9222, "/devtools/page/")
        self.browser_version = "Chromium/test"
        self.process = SimpleNamespace(poll=lambda: None)
        self.ws = None
        self.user_data_dir = object()
        self.read_task = None
        self.close_calls = 0
        self.eval_calls: list[str] = []
        self.cleanup = _cleanup(ok=False)
        self.instances.append(self)

    async def start(self) -> None:
        return None

    async def evaluate_script(self, expression: str) -> int:
        self.eval_calls.append(expression)
        return 2

    async def aclose(self) -> ChromiumCleanup:
        self.close_calls += 1
        return self.cleanup


@pytest.mark.asyncio
async def test_cleanup_failure_cannot_emit_readiness(monkeypatch, tmp_path: Path):
    _CleanupFailureSession.instances.clear()
    executable = _executable(tmp_path)
    monkeypatch.setattr(readiness, "ChromiumSession", _CleanupFailureSession)

    report, exit_code = await readiness.run_readiness(executable)
    instance = _CleanupFailureSession.instances[0]

    assert exit_code == 1
    assert report["ready"] is False
    assert report["cleanup"]["profile_removed"] is False
    assert report["owned_residue_clear"] is False
    assert instance.close_calls == 1
    assert instance.eval_calls == ["1 + 1"]
    assert "do-not-print" not in json.dumps(report)


class _SuccessSession:
    instances: list["_SuccessSession"] = []

    def __init__(self, executable_path: str, timeout_seconds: float) -> None:
        self.executable_path = executable_path
        self.timeout_seconds = timeout_seconds
        self.endpoint = ChromiumEndpoint("ws", "127.0.0.1", 9222, "/devtools/page/")
        self.browser_version = "Chromium/123.0.0.0"
        self.process = SimpleNamespace(poll=lambda: None)
        self.ws = None
        self.user_data_dir = object()
        self.read_task = None
        self.close_calls = 0
        self.eval_calls: list[str] = []
        self.instances.append(self)

    async def start(self) -> None:
        return None

    async def evaluate_script(self, expression: str) -> int:
        self.eval_calls.append(expression)
        return 2

    async def aclose(self) -> ChromiumCleanup:
        self.close_calls += 1
        self.process = None
        self.user_data_dir = None
        return _cleanup()


@pytest.mark.asyncio
async def test_success_requires_exact_input_single_inert_eval_and_clean_close(monkeypatch, tmp_path: Path):
    _SuccessSession.instances.clear()
    executable = _executable(tmp_path)
    monkeypatch.setattr(readiness, "ChromiumSession", _SuccessSession)

    report, exit_code = await readiness.run_readiness(executable)
    instance = _SuccessSession.instances[0]

    assert exit_code == 0
    assert report["ready"] is True
    assert instance.executable_path == executable
    assert instance.eval_calls == ["1 + 1"]
    assert instance.close_calls == 1
    assert report["start_attempt_count"] == 1
    assert report["evaluation_count"] == 1
    assert report["evaluation_value"] == 2
    assert report["cleanup"]["ok"] is True
    assert report["owned_residue_clear"] is True
