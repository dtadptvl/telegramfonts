from __future__ import annotations

import asyncio
import io
import json
import threading
from types import SimpleNamespace

import pytest
from websockets.exceptions import InvalidMessage

import measurement.browser_session as browser_session
from measurement.browser_session import ChromiumSession, ChromiumSessionError, _sanitize_text


class FakeProcess:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode: int | None = None
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.returncode = 0

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class FakeWebSocket:
    def __init__(self) -> None:
        self.state = SimpleNamespace(name="OPEN")
        self.closed = False
        self.close_calls = 0
        self.sent: list[dict[str, object]] = []

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(3600)
        raise StopAsyncIteration

    async def close(self) -> None:
        self.close_calls += 1
        self.state = SimpleNamespace(name="CLOSED")
        self.closed = True


@pytest.mark.parametrize(
    "url,reason",
    [
        ("http://127.0.0.1:9222/devtools/page/opaque", "SCHEME"),
        ("ws://192.0.2.10:9222/devtools/page/opaque", "HOST"),
        ("ws://127.0.0.1:9223/devtools/page/opaque", "PORT"),
        ("ws://127.0.0.1:9222/json/version", "PATH"),
    ],
)
def test_endpoint_drift_is_rejected_before_websocket_connect(url: str, reason: str):
    session = ChromiumSession(executable_path="unused")
    with pytest.raises(RuntimeError, match=f"CDP_ENDPOINT_{reason}"):
        session._validate_endpoint(url, 9222)


def test_diagnostic_redaction_covers_bearer_values_and_spaced_paths():
    safe = _sanitize_text(
        "Authorization: Bearer super-secret-value at C:\\Users\\runner temp\\profile data"
    )
    assert "super-secret-value" not in safe
    assert "runner temp" not in safe
    assert "profile data" not in safe
    assert "<redacted>" in safe
    assert "<path>" in safe


@pytest.mark.asyncio
async def test_discovered_endpoint_is_rejected_before_connect(monkeypatch):
    session = ChromiumSession(executable_path="unused", port=9222)
    process = FakeProcess()
    connect_calls = 0

    monkeypatch.setattr(browser_session.subprocess, "Popen", lambda *args, **kwargs: process)

    def fake_fetch(opener, url, timeout_seconds=1.0):
        if url.endswith("/json/version"):
            return {"Browser": "Chromium/test"}
        return [{"webSocketDebuggerUrl": "ws://192.0.2.10:9222/devtools/page/opaque"}]

    monkeypatch.setattr(session, "_fetch_json", fake_fetch)

    async def unexpected_connect(url: str):
        nonlocal connect_calls
        connect_calls += 1
        raise AssertionError("websocket connect must not run for rejected endpoint")

    monkeypatch.setattr(session, "_connect_websocket", unexpected_connect)

    with pytest.raises(ChromiumSessionError) as caught:
        await session.start()

    assert caught.value.diagnostics.stage == "endpoint_validation"
    assert connect_calls == 0
    assert caught.value.diagnostics.cleanup.ok


@pytest.mark.asyncio
async def test_loopback_websocket_ignores_ambient_proxy(monkeypatch):
    session = ChromiumSession(executable_path="unused")
    direct_attempts = 0
    proxy_attempts = 0

    async def direct_handler(reader, writer):
        nonlocal direct_attempts
        direct_attempts += 1
        writer.close()
        await writer.wait_closed()

    async def proxy_handler(reader, writer):
        nonlocal proxy_attempts
        proxy_attempts += 1
        writer.close()
        await writer.wait_closed()

    direct_server = await asyncio.start_server(direct_handler, "127.0.0.1", 0)
    proxy_server = await asyncio.start_server(proxy_handler, "127.0.0.1", 0)
    direct_port = direct_server.sockets[0].getsockname()[1]
    proxy_port = proxy_server.sockets[0].getsockname()[1]
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{proxy_port}")
    monkeypatch.setenv("ALL_PROXY", f"http://127.0.0.1:{proxy_port}")
    monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy_port}")
    monkeypatch.setenv("https_proxy", f"http://127.0.0.1:{proxy_port}")
    monkeypatch.setenv("all_proxy", f"http://127.0.0.1:{proxy_port}")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")

    try:
        with pytest.raises(Exception):
            await session._connect_websocket(
                f"ws://127.0.0.1:{direct_port}/devtools/page/opaque"
            )
        await asyncio.sleep(0)
    finally:
        direct_server.close()
        proxy_server.close()
        await direct_server.wait_closed()
        await proxy_server.wait_closed()

    assert direct_attempts == 1
    assert proxy_attempts == 0


@pytest.mark.asyncio
async def test_handshake_failure_keeps_bounded_cause_and_cleanup(monkeypatch):
    session = ChromiumSession(executable_path="unused")
    process = FakeProcess(
        stdout=b"stdout /devtools/page/opaque-target\n",
        stderr=b"stderr target_id=opaque-target\n",
    )

    monkeypatch.setattr(browser_session.subprocess, "Popen", lambda *args, **kwargs: process)

    def fake_fetch(opener, url, timeout_seconds=1.0):
        if url.endswith("/json/version"):
            return {"Browser": "Chromium/test"}
        return [
            {
                "webSocketDebuggerUrl":
                "ws://127.0.0.1:9222/devtools/page/opaque-target"
            }
        ]

    monkeypatch.setattr(session, "_fetch_json", fake_fetch)

    async def fake_connect(url: str):
        try:
            raise ConnectionError("target_id=opaque-target upstream refused")
        except ConnectionError as cause:
            raise InvalidMessage("did not receive a valid HTTP response") from cause

    monkeypatch.setattr(session, "_connect_websocket", fake_connect)
    session.port = 9222

    with pytest.raises(ChromiumSessionError) as caught:
        await session.start()

    diagnostics = caught.value.diagnostics
    assert diagnostics.stage == "websocket_handshake"
    assert diagnostics.error.message == "did not receive a valid HTTP response"
    assert diagnostics.cause_chain
    assert diagnostics.process_state == "running"
    assert diagnostics.endpoint is not None
    assert diagnostics.endpoint.path_prefix == "/devtools/page/"
    assert diagnostics.stderr is not None
    assert diagnostics.stderr.size_bytes > 0
    assert "opaque-target" not in str(diagnostics)
    assert "target_id=opaque-target" not in diagnostics.stderr.safe_tail
    assert diagnostics.cleanup.ok
    assert session.process is None
    assert session.user_data_dir is None


@pytest.mark.asyncio
async def test_stalled_cdp_fetch_stops_at_monotonic_deadline(monkeypatch):
    session = ChromiumSession(executable_path="unused", timeout_seconds=0.05, port=9222)
    process = FakeProcess()
    launches = 0
    fetch_attempts = 0
    release_fetch = threading.Event()

    def fake_popen(*args, **kwargs):
        nonlocal launches
        launches += 1
        return process

    monkeypatch.setattr(browser_session.subprocess, "Popen", fake_popen)

    def stalled_fetch(*args, **kwargs):
        nonlocal fetch_attempts
        fetch_attempts += 1
        release_fetch.wait(60)
        return {}

    monkeypatch.setattr(session, "_fetch_json", stalled_fetch)

    started = browser_session.asyncio.get_running_loop().time()
    try:
        with pytest.raises(ChromiumSessionError) as caught:
            await session.start()
    finally:
        release_fetch.set()
    elapsed = browser_session.asyncio.get_running_loop().time() - started

    assert caught.value.diagnostics.stage == "http_discovery"
    assert launches == 1
    assert fetch_attempts == 1
    assert elapsed < 0.5
    assert caught.value.diagnostics.cleanup.ok


@pytest.mark.asyncio
async def test_cleanup_failure_is_explicit(monkeypatch, tmp_path):
    session = ChromiumSession(executable_path="unused")
    profile_path = tmp_path / "profile"
    profile_path.mkdir()

    class BrokenProfile:
        name = str(profile_path)

        def cleanup(self) -> None:
            raise OSError("injected profile cleanup failure")

    monkeypatch.setattr(browser_session.tempfile, "TemporaryDirectory", lambda **kwargs: BrokenProfile())

    def fail_popen(*args, **kwargs):
        raise OSError("injected launch failure")

    monkeypatch.setattr(browser_session.subprocess, "Popen", fail_popen)

    with pytest.raises(ChromiumSessionError) as caught:
        await session.start()

    diagnostics = caught.value.diagnostics
    assert diagnostics.stage == "launch"
    assert not diagnostics.cleanup.ok
    assert diagnostics.cleanup.profile_removed is False
    assert diagnostics.cleanup.error is not None
    assert "injected profile cleanup failure" in diagnostics.cleanup.error.message
    assert session.user_data_dir is not None


@pytest.mark.asyncio
async def test_controlled_start_and_close_are_single_pass(monkeypatch):
    session = ChromiumSession(executable_path="unused", port=9222)
    process = FakeProcess()
    websocket = FakeWebSocket()
    calls: list[str] = []

    monkeypatch.setattr(browser_session.subprocess, "Popen", lambda *args, **kwargs: process)

    def fake_fetch(opener, url, timeout_seconds=1.0):
        if url.endswith("/json/version"):
            return {"Browser": "Chromium/controlled"}
        return [{"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/test"}]

    monkeypatch.setattr(session, "_fetch_json", fake_fetch)

    async def fake_connect(url: str):
        return websocket

    async def fake_send(method: str, params=None):
        calls.append(method)
        return {}

    async def fake_evaluate(expression: str):
        calls.append(f"eval:{expression}")
        return None

    monkeypatch.setattr(session, "_connect_websocket", fake_connect)
    monkeypatch.setattr(session, "send_command", fake_send)
    monkeypatch.setattr(session, "evaluate_script", fake_evaluate)

    await session.start()
    assert session.browser_version == "Chromium/controlled"
    assert session.endpoint is not None
    assert session.endpoint.port == 9222
    assert calls == ["Page.enable", "Runtime.enable", "eval:void 0"]

    cleanup = await session.aclose()
    assert cleanup.ok
    assert websocket.close_calls == 1
    assert process.terminate_calls == 1
    assert session.process is None
    assert session.user_data_dir is None


@pytest.mark.asyncio
async def test_send_command_does_not_launch_implicit_recovery(monkeypatch):
    session = ChromiumSession(executable_path="unused")
    restart = False

    async def unexpected_restart():
        nonlocal restart
        restart = True

    monkeypatch.setattr(session, "restart", unexpected_restart)

    with pytest.raises(RuntimeError, match="CDP_NOT_CONNECTED"):
        await session.send_command("Runtime.enable")
    assert restart is False
