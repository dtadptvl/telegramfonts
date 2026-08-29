from __future__ import annotations

import asyncio
import io
import json
import threading
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.browser
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

    requests: list[tuple[str, str]] = []

    def fake_fetch(opener, url, timeout_seconds=1.0, method="GET"):
        requests.append((url, method))
        if url.endswith("/json/version"):
            return {"Browser": "Chromium/test"}
        assert not url.endswith("/json/list")
        return {
            "type": "page",
            "url": "about:blank",
            "webSocketDebuggerUrl": "ws://192.0.2.10:9222/devtools/page/opaque",
        }

    monkeypatch.setattr(session, "_fetch_json", fake_fetch)

    async def unexpected_connect(url: str, deadline=None):
        nonlocal connect_calls
        connect_calls += 1
        raise AssertionError("websocket connect must not run for rejected endpoint")

    monkeypatch.setattr(session, "_connect_websocket", unexpected_connect)

    with pytest.raises(ChromiumSessionError) as caught:
        await session.start()

    assert caught.value.diagnostics.stage == "endpoint_validation"
    assert connect_calls == 0
    assert requests == [
        ("http://127.0.0.1:9222/json/version", "GET"),
        ("http://127.0.0.1:9222/json/new?about:blank", "PUT"),
    ]
    assert caught.value.diagnostics.cleanup.ok


@pytest.mark.parametrize(
    "target,reason",
    [
        ([], "CDP_PAGE_TARGET_RESPONSE_REJECTED"),
        (
            {
                "type": "worker",
                "url": "about:blank",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/target",
            },
            "CDP_PAGE_TARGET_TYPE_REJECTED",
        ),
        (
            {
                "type": "page",
                "url": "https://example.invalid/",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/target",
            },
            "CDP_PAGE_TARGET_URL_REJECTED",
        ),
        (
            {
                "type": "page",
                "url": "about:blank",
                "webSocketDebuggerUrl": "ws://192.0.2.10:9222/devtools/page/target",
            },
            "CDP_ENDPOINT_HOST_REJECTED",
        ),
        (
            {
                "type": "page",
                "url": "about:blank",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9223/devtools/page/target",
            },
            "CDP_ENDPOINT_PORT_REJECTED",
        ),
    ],
)
@pytest.mark.asyncio
async def test_explicit_page_target_is_validated_before_websocket_connect(
    monkeypatch, target, reason
):
    session = ChromiumSession(executable_path="unused", port=9222)
    process = FakeProcess()
    connect_calls = 0
    requests: list[tuple[str, str]] = []

    monkeypatch.setattr(browser_session.subprocess, "Popen", lambda *args, **kwargs: process)

    def fake_fetch(opener, url, timeout_seconds=1.0, method="GET"):
        requests.append((url, method))
        if url.endswith("/json/version"):
            return {"Browser": "Chromium/test"}
        if url.endswith("/json/list"):
            raise AssertionError("stale /json/list target must not be consumed")
        return target

    monkeypatch.setattr(session, "_fetch_json", fake_fetch)

    async def unexpected_connect(url: str, deadline=None):
        nonlocal connect_calls
        connect_calls += 1
        raise AssertionError("websocket connect must not run for invalid target")

    monkeypatch.setattr(session, "_connect_websocket", unexpected_connect)

    with pytest.raises(ChromiumSessionError, match=reason):
        await session.start()

    assert connect_calls == 0
    assert requests == [
        ("http://127.0.0.1:9222/json/version", "GET"),
        ("http://127.0.0.1:9222/json/new?about:blank", "PUT"),
    ]
    assert session.last_diagnostic is not None
    assert session.last_diagnostic.cleanup.ok


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

    def fake_fetch(opener, url, timeout_seconds=1.0, method="GET"):
        if url.endswith("/json/version"):
            return {"Browser": "Chromium/test"}
        return {
            "type": "page",
            "url": "about:blank",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/opaque-target",
        }

    monkeypatch.setattr(session, "_fetch_json", fake_fetch)

    async def fake_connect(url: str, deadline=None):
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
    assert diagnostics.browser_version == "Chromium/test"
    assert diagnostics.handshake_profile == "minimal-direct"
    assert diagnostics.websockets_version_class in {"13-16", "17-plus", "unknown"}
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

    def fake_fetch(opener, url, timeout_seconds=1.0, method="GET"):
        if url.endswith("/json/version"):
            return {"Browser": "Chromium/controlled"}
        return {
            "type": "page",
            "url": "about:blank",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/test",
        }

    monkeypatch.setattr(session, "_fetch_json", fake_fetch)

    async def fake_connect(url: str, deadline=None):
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
    if browser_session.launch_is_cdp_owned():
        # CDP-owned (detached) launch: the transient wrapper handle is
        # dropped without termination; the browser is closed through CDP.
        assert process.terminate_calls == 0
        assert "Browser.close" in calls
    else:
        assert process.terminate_calls == 1
    assert session.process is None
    assert session.user_data_dir is None


@pytest.mark.asyncio
async def test_websocket_attempt_uses_one_minimal_direct_profile(monkeypatch):
    session = ChromiumSession(executable_path="unused")
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_connect(
        url: str,
        *,
        max_size=None,
        proxy=None,
        compression=None,
        origin=None,
        user_agent_header=None,
    ):
        calls.append(
            (
                url,
                {
                    "max_size": max_size,
                    "proxy": proxy,
                    "compression": compression,
                    "origin": origin,
                    "user_agent_header": user_agent_header,
                },
            )
        )
        return object()

    monkeypatch.setattr(browser_session.websockets, "connect", fake_connect)

    deadline = asyncio.get_running_loop().time() + 1.0
    result = await session._connect_websocket(
        "ws://127.0.0.1:9222/devtools/page/target", deadline
    )

    assert result is not None
    assert len(calls) == 1
    assert calls[0][1] == {
        "max_size": 20 * 1024 * 1024,
        "proxy": None,
        "compression": None,
        "origin": None,
        "user_agent_header": None,
    }


@pytest.mark.asyncio
async def test_websockets_13_direct_boundary_omits_unsupported_proxy(monkeypatch):
    session = ChromiumSession(executable_path="unused")
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_websockets_13_connect(
        url: str,
        *,
        max_size=None,
        compression="deflate",
        origin=None,
        extra_headers=None,
    ):
        calls.append(
            (
                url,
                {
                    "max_size": max_size,
                    "compression": compression,
                    "origin": origin,
                    "extra_headers": extra_headers,
                },
            )
        )
        return object()

    monkeypatch.setattr(browser_session.websockets, "connect", fake_websockets_13_connect)
    monkeypatch.setattr(browser_session.websockets, "__version__", "13.0.1", raising=False)

    result = await session._connect_websocket(
        "ws://127.0.0.1:9222/devtools/page/target",
        asyncio.get_running_loop().time() + 1.0,
    )

    assert result is not None
    assert len(calls) == 1
    assert calls[0][1] == {
        "max_size": 20 * 1024 * 1024,
        "compression": None,
        "origin": None,
        "extra_headers": None,
    }


@pytest.mark.asyncio
async def test_websocket_attempt_respects_remaining_startup_deadline(monkeypatch):
    session = ChromiumSession(executable_path="unused")
    calls = 0

    async def hanging_connect(url: str, **kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(60)

    monkeypatch.setattr(browser_session.websockets, "connect", hanging_connect)

    with pytest.raises(TimeoutError, match="CHROMIUM_WEBSOCKET_DEADLINE_EXPIRED"):
        await session._connect_websocket(
            "ws://127.0.0.1:9222/devtools/page/target",
            asyncio.get_running_loop().time() + 0.01,
        )
    assert calls == 1


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
