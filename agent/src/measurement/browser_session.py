"""Persistent Chromium measurement session driving direct browser metrics & lossless raster capture via CDP."""
from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import inspect
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit

import websockets

from measurement.models import BrowserFontSelection, DirectMetrics

logger = logging.getLogger("telegramfonts.agent.measurement.browser")

_DIAGNOSTIC_TAIL_BYTES = 2048
_DIAGNOSTIC_MESSAGE_CHARS = 240
_CAUSE_CHAIN_LIMIT = 6
_EXPECTED_PAGE_PATH = re.compile(r"^/devtools/page/[^/]+$")
_PROXY_ENV_NAMES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
_SAFE_BROWSER_VERSION = re.compile(r"^(?:Chromium|Chrome)/[A-Za-z0-9._-]{1,80}$")
_HANDSHAKE_PROFILE = "minimal-direct"
_WEBSOCKETS_VERSION_CLASSES = frozenset({"13-16", "17-plus", "unknown"})


@dataclass(frozen=True)
class ChromiumExceptionInfo:
    """Sanitized, bounded exception information safe for diagnostics."""

    type_name: str
    message: str
    code: str | None = None


@dataclass(frozen=True)
class ChromiumStreamEvidence:
    """Bounded evidence for a child process stream."""

    size_bytes: int
    sha256: str
    safe_tail: str


@dataclass(frozen=True)
class ChromiumEndpoint:
    """Validated, non-secret CDP endpoint identity without the target id."""

    scheme: str
    host: str
    port: int
    path_prefix: str


@dataclass(frozen=True)
class ChromiumCleanup:
    """Explicit finalization state for a Chromium session."""

    websocket_closed: bool
    process_closed: bool
    profile_removed: bool
    output_drained: bool
    error: ChromiumExceptionInfo | None = None

    @property
    def ok(self) -> bool:
        return (
            self.websocket_closed
            and self.process_closed
            and self.profile_removed
            and self.output_drained
            and self.error is None
        )


@dataclass(frozen=True)
class ChromiumSessionDiagnostics:
    """Fail-closed startup/cleanup evidence with no opaque target id or secrets."""

    stage: str
    error: ChromiumExceptionInfo
    cause_chain: tuple[ChromiumExceptionInfo, ...]
    process_state: str
    process_created: bool
    endpoint: ChromiumEndpoint | None
    stdout: ChromiumStreamEvidence | None
    stderr: ChromiumStreamEvidence | None
    cleanup: ChromiumCleanup
    browser_version: str | None = None
    handshake_profile: str | None = None
    websockets_version_class: str | None = None


class ChromiumSessionError(RuntimeError):
    """Typed RuntimeError preserving bounded Chromium session diagnostics."""

    def __init__(self, diagnostics: ChromiumSessionDiagnostics) -> None:
        self.diagnostics = diagnostics
        super().__init__(
            f"CHROMIUM_SESSION_{diagnostics.stage.upper()}_FAILED: "
            f"{diagnostics.error.type_name}: {diagnostics.error.message}"
        )


class _BoundedPipeCapture:
    """Drain a child pipe continuously while retaining only bounded tail memory."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._size_bytes = 0
        self._digest = hashlib.sha256()
        self._tail = bytearray()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._error: ChromiumExceptionInfo | None = None

    def start(self) -> None:
        if self._stream is None:
            return
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        try:
            while True:
                chunk = self._stream.read(4096)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", "replace")
                with self._lock:
                    self._size_bytes += len(chunk)
                    self._digest.update(chunk)
                    self._tail.extend(chunk)
                    if len(self._tail) > _DIAGNOSTIC_TAIL_BYTES:
                        del self._tail[:-_DIAGNOSTIC_TAIL_BYTES]
        except Exception as exc:  # pragma: no cover - OS pipe failures are platform-specific
            self._error = _exception_info(exc)

    def finish(self, timeout_seconds: float = 1.0) -> bool:
        if self._thread is None:
            return True
        self._thread.join(timeout_seconds)
        if self._thread.is_alive():
            try:
                self._stream.close()
            except Exception as exc:  # pragma: no cover - defensive OS cleanup
                self._error = _exception_info(exc)
            self._thread.join(timeout_seconds)
        try:
            self._stream.close()
        except Exception as exc:  # pragma: no cover - defensive OS cleanup
            if self._error is None:
                self._error = _exception_info(exc)
        return not self._thread.is_alive() and self._error is None

    @property
    def error(self) -> ChromiumExceptionInfo | None:
        return self._error

    def evidence(self) -> ChromiumStreamEvidence:
        with self._lock:
            tail = bytes(self._tail)
            size_bytes = self._size_bytes
            digest = self._digest.hexdigest()
        return ChromiumStreamEvidence(
            size_bytes=size_bytes,
            sha256=digest,
            safe_tail=_sanitize_text(tail.decode("utf-8", "replace")),
        )


def _sanitize_text(value: str) -> str:
    """Redact URLs, paths, target ids, and secret-like values from bounded text."""

    text = str(value).replace("\x00", " ")
    text = re.sub(r"(?i)wss?://[^\s\"']+", "<ws-endpoint>", text)
    text = re.sub(r"(?i)(?:https?|file)://[^\s\"']+", "<url>", text)
    text = re.sub(r"(?i)(target[ _-]?id\s*[:=]\s*)[^\s,;]+", r"\1<redacted>", text)
    text = re.sub(r"(?i)(/devtools/page/)[^/\s\"']+", r"\1<target>", text)
    text = re.sub(
        r"(?i)\b(authorization\s*[:=]\s*(?:(?:bearer|basic)\s+)?)\S+",
        r"\1<redacted>",
        text,
    )
    text = re.sub(
        r"(?i)\b(?:[a-z0-9]+[_-])*(?:token|secret|password|api[_-]?key)"
        r"\s*[:=]\s*[^\s,;]+",
        "<credential>=<redacted>",
        text,
    )
    text = re.sub(
        r"(?i)\b(token|secret|password)(\s*[:=]\s*)[^\s,;]+",
        r"\1\2<redacted>",
        text,
    )
    text = re.sub(r"(?:[A-Za-z]:[\\/]|/)[^\r\n\"'<>|,;]+", "<path>", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_DIAGNOSTIC_MESSAGE_CHARS]


def _safe_browser_version(value: object) -> str | None:
    text = str(value).strip()
    return text if _SAFE_BROWSER_VERSION.fullmatch(text) else None


def _websockets_version_class() -> str:
    version = str(getattr(websockets, "__version__", ""))
    match = re.fullmatch(r"(\d+)(?:\.\d+){0,2}", version)
    if match is None:
        return "unknown"
    major = int(match.group(1))
    if 13 <= major <= 16:
        return "13-16"
    if major >= 17:
        return "17-plus"
    return "unknown"


def _exception_info(exc: BaseException) -> ChromiumExceptionInfo:
    message = _sanitize_text(str(exc)) or "<no message>"
    code_candidate = message.split(":", 1)[0].strip()
    code = (
        code_candidate
        if re.fullmatch(r"[A-Z][A-Z0-9_]{2,79}", code_candidate)
        else None
    )
    return ChromiumExceptionInfo(
        type_name=type(exc).__name__,
        message=message,
        code=code,
    )


def _cause_chain(exc: BaseException) -> tuple[ChromiumExceptionInfo, ...]:
    chain: list[ChromiumExceptionInfo] = []
    seen: set[int] = set()
    current = exc.__cause__ or exc.__context__
    while current is not None and id(current) not in seen and len(chain) < _CAUSE_CHAIN_LIMIT:
        seen.add(id(current))
        chain.append(_exception_info(current))
        current = current.__cause__ or current.__context__
    return tuple(chain)


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@contextmanager
def _without_proxy_environment():
    saved = {name: os.environ.get(name) for name in _PROXY_ENV_NAMES}
    saved_no_proxy = {name: os.environ.get(name) for name in ("NO_PROXY", "no_proxy")}
    try:
        for name in _PROXY_ENV_NAMES:
            os.environ.pop(name, None)
        os.environ["NO_PROXY"] = "*"
        os.environ["no_proxy"] = "*"
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        for name, value in saved_no_proxy.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def find_chromium_executable() -> str:
    """Locate Chromium / Chrome executable on the current host system."""
    env_browser = os.environ.get("CHROMIUM_PATH") or os.environ.get("CHROME_PATH")
    if env_browser and os.path.exists(env_browser):
        return env_browser

    candidates = [
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        shutil.which("chrome"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/local/bin/chromium",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]

    for cand in candidates:
        if cand and os.path.exists(cand):
            return cand

    raise RuntimeError("CHROMIUM_EXECUTABLE_NOT_FOUND: no Chromium / Chrome binary found on host")


def _ps_single_quoted(value: str) -> str:
    """PowerShell single-quoted literal (only embedded quotes are doubled)."""
    return "'" + str(value).replace("'", "''") + "'"


def _windows_arg_quote(arg: str) -> str:
    """Quote one argument for a Windows CommandLine string (CommandLineToArgvW rules)."""
    if arg and not any(c in arg for c in ' \t"'):
        return arg
    quoted = ['"']
    backslashes = 0
    for ch in arg:
        if ch == "\\":
            backslashes += 1
            quoted.append(ch)
        elif ch == '"':
            quoted.append("\\" * (2 * backslashes + 1))
            quoted.append(ch)
            backslashes = 0
        else:
            backslashes = 0
            quoted.append(ch)
    quoted.append("\\" * (2 * backslashes))
    quoted.append('"')
    return "".join(quoted)


def _windows_shell_launch_command(cmd: Sequence[str]) -> list[str]:
    """Build the Windows detached-launch command for a Chromium browser.

    Direct launches (and shell-mediated child launches) are unreliable on
    Windows hosts: Chrome launcher delegation can swallow the launch when an
    interactive browser session exists, and a browser spawned inside the
    launching process tree dies when that tree exits. Creating the process
    through WMI parents it under the WMI service, outside the session
    process tree, so the headless browser survives independently and CDP is
    the sole authoritative startup/shutdown channel. The wrapper's exit code
    carries the WMI ``Create`` ReturnValue (0 = success).
    """
    cmdline = " ".join(_windows_arg_quote(str(a)) for a in cmd)
    script = (
        "$cl = " + _ps_single_quoted(cmdline) + "; "
        "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        "-Arguments @{CommandLine=$cl}; exit [int]$r.ReturnValue"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        encoded,
    ]


def launch_is_cdp_owned() -> bool:
    """True when the platform launch path yields a CDP-owned (detached) browser.

    The Windows WMI-detached launch parents the browser under the WMI
    service; the session's launched wrapper is transient and never owns the
    browser process, so startup/shutdown must go through CDP.
    """
    return sys.platform == "win32" and shutil.which("powershell") is not None


def launch_chromium_process(
    cmd: Sequence[str],
    stdout: Any,
    stderr: Any,
) -> subprocess.Popen[bytes]:
    """Launch the Chromium browser process for a session.

    Windows hosts use the WMI-detached launch (see
    ``_windows_shell_launch_command``): the wrapper exits as soon as the WMI
    creation returns, so session startup must rely on CDP discovery rather
    than launcher-process liveness, and cleanup must close the browser
    through CDP. Other platforms launch the browser directly.
    """
    if sys.platform == "win32":
        powershell = shutil.which("powershell")
        if powershell:
            return subprocess.Popen(
                _windows_shell_launch_command(list(cmd)),
                stdout=stdout,
                stderr=stderr,
                text=False,
                bufsize=0,
            )
    return subprocess.Popen(cmd, stdout=stdout, stderr=stderr, text=False, bufsize=0)


async def close_browser_session(session: Any) -> Any:
    """Await owned browser cleanup while retaining compatibility with test doubles.

    Incident E-00035 F1(d): bounded timeout (5.0s) so session cleanup hangs never
    block post-fence abort or prevent terminal error handling.
    """
    closer = getattr(session, "aclose", None)
    if closer is not None:
        result = closer()
    else:
        result = session.close()
    if inspect.isawaitable(result):
        try:
            return await asyncio.wait_for(result, timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("session close bounded-timeout: cleanup exceeded 5.0s deadline")
            return None
        except Exception as exc:
            logger.warning("session close exception: %s", type(exc).__name__)
            return None
    return result


class ChromiumSession:
    """Persistent, long-lived headless Chromium session with fail-closed CDP transport."""

    def __init__(
        self,
        executable_path: str | None = None,
        timeout_seconds: float = 10.0,
        port: int = 0,
    ) -> None:
        self.executable_path = executable_path or find_chromium_executable()
        self.timeout_seconds = timeout_seconds
        self.port = port
        self.process: subprocess.Popen[bytes] | None = None
        self.user_data_dir: tempfile.TemporaryDirectory[str] | None = None
        self.ws_url: str | None = None
        self.cdp_port: int | None = None
        self.endpoint: ChromiumEndpoint | None = None
        self.ws: Any = None
        self.msg_id: int = 0
        self.pending_responses: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.event_waiters: dict[str, list[asyncio.Future[dict[str, Any]]]] = {}
        self.read_task: asyncio.Task[None] | None = None
        self._pending_close_task: asyncio.Task[ChromiumCleanup] | None = None
        self._stdout_capture: _BoundedPipeCapture | None = None
        self._stderr_capture: _BoundedPipeCapture | None = None
        self.last_cleanup: ChromiumCleanup | None = None
        self.last_diagnostic: ChromiumSessionDiagnostics | None = None
        self._process_created = False
        # True when the launched browser outlived its launcher process (host
        # launcher delegation): the session then owns the browser via CDP only.
        self._launcher_detached = False
        self.browser_version: str = "unknown"
        self.handshake_profile = _HANDSHAKE_PROFILE
        self.websockets_version_class = _websockets_version_class()
        self._loaded_fonts: set[str] = set()
        self._loaded_font_blobs: dict[str, bytes] = {}

    async def start(self) -> None:
        """Launch headless Chromium subprocess and initialize CDP WebSocket session."""
        if self._pending_close_task is not None:
            pending_close = self._pending_close_task
            await pending_close

        if (
            self._is_connected()
            and (
                self._launcher_detached
                or (self.process is not None and self.process.poll() is None)
            )
        ):
            return

        if self.process is not None or self.ws is not None or self.user_data_dir is not None:
            await self.aclose(clear_fonts=False)

        stage = "prepare"
        target_port: int | None = None
        last_discovery_error: Exception | None = None
        try:
            self._stdout_capture = None
            self._stderr_capture = None
            self.last_cleanup = None
            self.last_diagnostic = None
            self._process_created = False
            self._launcher_detached = False
            self.ws_url = None
            self.endpoint = None
            self.cdp_port = None

            self.user_data_dir = tempfile.TemporaryDirectory(prefix="telefont_chrome_")
            if sys.platform == "win32":
                # WMI-detached Chrome startup silently fails against a
                # pre-existing profile directory; the browser must create
                # the profile itself. The tracked path stays registered
                # for cleanup.
                try:
                    os.rmdir(self.user_data_dir.name)
                except OSError:
                    pass
            if self.port > 0:
                target_port = self.port
            else:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.bind(("", 0))
                    target_port = int(sock.getsockname()[1])
            self.cdp_port = target_port

            cmd = [
                self.executable_path,
                "--headless=new",
                f"--remote-debugging-port={target_port}",
                f"--user-data-dir={self.user_data_dir.name}",
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-background-networking",
                "--disable-sync",
                "--disable-extensions",
                "--window-size=1280,800",
                "about:blank",
            ]

            stage = "launch"
            logger.info("Launching persistent Chromium session on selected CDP port")
            cdp_owned_launch = launch_is_cdp_owned()
            self.process = launch_chromium_process(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self._process_created = self.process is not None
            self._stdout_capture = _BoundedPipeCapture(self.process.stdout)
            self._stderr_capture = _BoundedPipeCapture(self.process.stderr)
            self._stdout_capture.start()
            self._stderr_capture.start()

            stage = "http_discovery"
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            http_url = f"http://127.0.0.1:{target_port}"
            loop = asyncio.get_running_loop()
            startup_deadline = loop.time() + max(0.0, float(self.timeout_seconds))
            launcher_exit_code: int | None = None

            async def fetch_discovery_json(url: str, method: str = "GET") -> Any:
                remaining = startup_deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError("CHROMIUM_CDP_DISCOVERY_DEADLINE_EXPIRED")
                async with asyncio.timeout_at(startup_deadline):
                    return await asyncio.to_thread(
                        self._fetch_json,
                        opener,
                        url,
                        timeout_seconds=remaining,
                        method=method,
                    )

            version_discovered = False
            while loop.time() < startup_deadline:
                exit_code = self.process.poll()
                if exit_code is not None and launcher_exit_code is None:
                    # Host launcher delegation: some Chromium distributions
                    # (e.g. Windows Chrome launcher) spawn the real browser as
                    # a separate process and exit the launched process
                    # immediately. Completion is proven only by an observed
                    # CDP endpoint, never by launcher process liveness.
                    launcher_exit_code = exit_code
                    logger.info(
                        "Chromium launcher exited early (code %s); continuing CDP discovery for a detached browser",
                        exit_code,
                    )
                try:
                    vdata = await fetch_discovery_json(f"{http_url}/json/version")
                    if not isinstance(vdata, dict):
                        raise RuntimeError("CHROMIUM_VERSION_RESPONSE_REJECTED")
                    self.browser_version = str(vdata.get("Browser", "Chromium/unknown"))
                    version_discovered = True
                    break
                except Exception as exc:
                    last_discovery_error = exc
                remaining = startup_deadline - loop.time()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(0.1, remaining))

            if not version_discovered:
                if launcher_exit_code is not None:
                    # The launcher exited AND no CDP endpoint ever answered:
                    # fail closed with the observable process evidence.
                    if last_discovery_error is not None:
                        raise RuntimeError(
                            f"CHROMIUM_PROCESS_EXITED_DURING_CDP_DISCOVERY:launcher_exit={launcher_exit_code}"
                        ) from last_discovery_error
                    raise RuntimeError(
                        f"CHROMIUM_PROCESS_EXITED_DURING_CDP_DISCOVERY:launcher_exit={launcher_exit_code}"
                    )
                if last_discovery_error is not None:
                    raise RuntimeError("CHROMIUM_CDP_DISCOVERY_TIMEOUT") from last_discovery_error
                raise RuntimeError("CHROMIUM_CDP_DISCOVERY_TIMEOUT")

            page_target = await fetch_discovery_json(
                f"{http_url}/json/new?about:blank", method="PUT"
            )
            stage = "endpoint_validation"
            page_ws_url = self._page_target_endpoint(page_target)
            self.endpoint = self._validate_endpoint(page_ws_url, target_port)
            self.ws_url = page_ws_url

            stage = "websocket_handshake"
            self.ws = await self._connect_websocket(page_ws_url, startup_deadline)
            self.read_task = asyncio.create_task(self._reader_loop())

            stage = "cdp_initialization"
            await self.send_command("Page.enable")
            await self.send_command("Runtime.enable")
            await self.evaluate_script("void 0")
            if not self._is_connected():
                raise RuntimeError("CDP_CONNECTION_NOT_READY")

            if launcher_exit_code is not None or cdp_owned_launch:
                # The browser demonstrably outlives (or is never owned by)
                # the launched process: drop the launcher/wrapper handle;
                # ownership is CDP-only and cleanup must close the browser
                # through CDP.
                self._launcher_detached = True
                self.process = None

            stage = "font_restore"
            if self._loaded_font_blobs:
                for family_name, blob in list(self._loaded_font_blobs.items()):
                    await self._inject_font_face(family_name, blob)
                logger.info(
                    "Restored %d font faces after session start/recovery",
                    len(self._loaded_font_blobs),
                )
            logger.info("Persistent Chromium session ready")
        except asyncio.CancelledError:
            process_state = self._process_state()
            cleanup_task = asyncio.create_task(
                self.aclose(clear_fonts=False, raise_on_failure=False)
            )
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                await cleanup_task
            raise
        except Exception as exc:
            process_state = self._process_state()
            cleanup = await self.aclose(clear_fonts=False, raise_on_failure=False)
            diagnostics = self._make_diagnostics(stage, exc, process_state, cleanup)
            self.last_diagnostic = diagnostics
            raise ChromiumSessionError(diagnostics) from exc

    @staticmethod
    def _fetch_json(
        opener: urllib.request.OpenerDirector,
        url: str,
        timeout_seconds: float = 1.0,
        method: str = "GET",
    ) -> Any:
        request = urllib.request.Request(url, method=method)
        with opener.open(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _page_target_endpoint(target: Any) -> str:
        if not isinstance(target, dict):
            raise RuntimeError("CDP_PAGE_TARGET_RESPONSE_REJECTED")
        if target.get("type") != "page":
            raise RuntimeError("CDP_PAGE_TARGET_TYPE_REJECTED")
        if target.get("url") != "about:blank":
            raise RuntimeError("CDP_PAGE_TARGET_URL_REJECTED")
        websocket_url = target.get("webSocketDebuggerUrl")
        if not isinstance(websocket_url, str) or not websocket_url:
            raise RuntimeError("CDP_PAGE_TARGET_WEBSOCKET_REJECTED")
        return websocket_url

    @staticmethod
    def _validate_endpoint(url: str, selected_port: int) -> ChromiumEndpoint:
        try:
            parts = urlsplit(url)
            host = parts.hostname
            port = parts.port
        except (TypeError, ValueError) as exc:
            raise RuntimeError("CDP_ENDPOINT_MALFORMED") from exc

        if parts.scheme != "ws":
            raise RuntimeError("CDP_ENDPOINT_SCHEME_REJECTED")
        if not _is_loopback_host(host):
            raise RuntimeError("CDP_ENDPOINT_HOST_REJECTED")
        if port != selected_port:
            raise RuntimeError("CDP_ENDPOINT_PORT_REJECTED")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise RuntimeError("CDP_ENDPOINT_METADATA_REJECTED")
        if not _EXPECTED_PAGE_PATH.fullmatch(parts.path):
            raise RuntimeError("CDP_ENDPOINT_PATH_REJECTED")
        return ChromiumEndpoint(
            scheme="ws",
            host=str(host).lower(),
            port=int(port),
            path_prefix="/devtools/page/",
        )

    async def _connect_websocket(self, url: str, deadline: float | None = None) -> Any:
        kwargs: dict[str, Any] = {"max_size": 20 * 1024 * 1024}
        try:
            parameters = inspect.signature(websockets.connect).parameters
        except (TypeError, ValueError):
            parameters = {}
        for name, value in (
            ("proxy", None),
            ("compression", None),
            ("origin", None),
            ("user_agent_header", None),
        ):
            if name in parameters:
                kwargs[name] = value

        async def connect() -> Any:
            with _without_proxy_environment():
                return await websockets.connect(url, **kwargs)

        if deadline is None:
            return await connect()
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("CHROMIUM_WEBSOCKET_DEADLINE_EXPIRED")
        try:
            async with asyncio.timeout_at(deadline):
                return await connect()
        except asyncio.TimeoutError as exc:
            raise TimeoutError("CHROMIUM_WEBSOCKET_DEADLINE_EXPIRED") from exc

    def _process_state(self) -> str:
        if self.process is None:
            return "detached" if self._launcher_detached else "not_started"
        try:
            code = self.process.poll()
        except Exception:
            return "unknown"
        return "running" if code is None else f"exited:{code}"

    def _make_diagnostics(
        self,
        stage: str,
        exc: BaseException,
        process_state: str,
        cleanup: ChromiumCleanup,
    ) -> ChromiumSessionDiagnostics:
        return ChromiumSessionDiagnostics(
            stage=stage,
            error=_exception_info(exc),
            cause_chain=_cause_chain(exc),
            process_state=process_state,
            process_created=self._process_created,
            endpoint=self.endpoint,
            stdout=self._stdout_capture.evidence() if self._stdout_capture else None,
            stderr=self._stderr_capture.evidence() if self._stderr_capture else None,
            cleanup=cleanup,
            browser_version=_safe_browser_version(self.browser_version),
            handshake_profile=self.handshake_profile,
            websockets_version_class=self.websockets_version_class
            if self.websockets_version_class in _WEBSOCKETS_VERSION_CLASSES
            else "unknown",
        )

    @staticmethod
    def _terminate_process(process: subprocess.Popen[bytes]) -> tuple[bool, Exception | None]:
        cleanup_error: Exception | None = None
        try:
            if process.poll() is None:
                process.terminate()
        except Exception as exc:
            cleanup_error = exc

        try:
            if process.poll() is None:
                process.wait(timeout=2.0)
        except subprocess.TimeoutExpired as exc:
            cleanup_error = cleanup_error or exc
            try:
                process.kill()
                process.wait(timeout=2.0)
            except Exception as kill_exc:
                cleanup_error = cleanup_error or kill_exc
        except Exception as exc:
            cleanup_error = cleanup_error or exc

        try:
            closed = process.poll() is not None
        except Exception as exc:
            cleanup_error = cleanup_error or exc
            closed = False
        return closed, cleanup_error

    async def _await_websocket_close(self, websocket: Any) -> None:
        close_result = websocket.close()
        if inspect.isawaitable(close_result):
            await asyncio.wait_for(close_result, timeout=2.0)

    async def _close_detached_browser(self) -> bool:
        """Close a launcher-detached browser through CDP and verify the exit.

        The launched process is already gone (host launcher delegation), so
        the only authoritative shutdown path is CDP ``Browser.close`` plus
        observed CDP-endpoint refusal. A loopback probe that cannot reach
        any listener (refused/aborted/reset/timeout) proves the endpoint is
        gone. The endpoint still accepting connections after the bounded
        deadline reports False so cleanup fails closed instead of silently
        orphaning a host browser process.
        """
        port = self.cdp_port
        ack = False
        try:
            await asyncio.wait_for(self.send_command("Browser.close"), timeout=3.0)
            ack = True
        except Exception:
            ack = False
        if port is None:
            return ack
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        serving = False
        while loop.time() < deadline:
            try:
                _reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", port), timeout=0.5
                )
                serving = True
                try:
                    writer.close()
                    await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
                except Exception:
                    pass
                await asyncio.sleep(0.2)
            except (ConnectionRefusedError, ConnectionAbortedError, ConnectionResetError):
                return True
            except (OSError, asyncio.TimeoutError):
                # No listener answered on loopback: the endpoint is gone.
                return True
        return ack and not serving

    async def aclose(
        self,
        clear_fonts: bool = True,
        raise_on_failure: bool = True,
    ) -> ChromiumCleanup:
        """Await complete cleanup and expose failures instead of swallowing them."""
        current_task = asyncio.current_task()
        if self._pending_close_task is not None and self._pending_close_task is not current_task:
            await self._pending_close_task

        process_state = self._process_state()
        cleanup_error: Exception | None = None

        # A launcher-detached browser has no process handle: it must be
        # closed through CDP BEFORE the transport is torn down, otherwise
        # the browser survives the session as an orphaned host process.
        detached_browser_closed = True
        if self._launcher_detached and self.ws is not None and self.read_task is not None:
            detached_browser_closed = await self._close_detached_browser()
            if not detached_browser_closed:
                cleanup_error = cleanup_error or RuntimeError(
                    "CHROMIUM_DETACHED_BROWSER_NOT_CLOSED"
                )
        self._launcher_detached = False

        if self.read_task is not None:
            self.read_task.cancel()
            try:
                await self.read_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                cleanup_error = cleanup_error or exc
            self.read_task = None

        for waiter in self.pending_responses.values():
            if not waiter.done():
                waiter.cancel()
        self.pending_responses.clear()
        for waiters in self.event_waiters.values():
            for waiter in waiters:
                if not waiter.done():
                    waiter.cancel()
        self.event_waiters.clear()

        websocket_closed = True
        websocket = self.ws
        if websocket is not None:
            try:
                await self._await_websocket_close(websocket)
                state = getattr(websocket, "state", None)
                state_name = getattr(state, "name", None)
                if state_name is not None and state_name != "CLOSED":
                    websocket_closed = False
                elif state_name is None and getattr(websocket, "closed", None) is False:
                    websocket_closed = False
            except Exception as exc:
                websocket_closed = False
                cleanup_error = cleanup_error or exc
            if websocket_closed:
                self.ws = None
                self.ws_url = None

        process_closed = True
        process = self.process
        if process is not None:
            process_closed, process_error = await asyncio.to_thread(
                self._terminate_process, process
            )
            cleanup_error = cleanup_error or process_error
            if process_closed:
                self.process = None
        if not detached_browser_closed:
            # A session-owned browser is still running: cleanup is incomplete.
            process_closed = False

        captures = [capture for capture in (self._stdout_capture, self._stderr_capture) if capture]
        output_drained = True
        if captures:
            results = await asyncio.gather(
                *(asyncio.to_thread(capture.finish) for capture in captures),
                return_exceptions=True,
            )
            output_drained = all(result is True for result in results)
            for result, capture in zip(results, captures):
                if isinstance(result, Exception):
                    cleanup_error = cleanup_error or result
                elif result is not True:
                    cleanup_error = cleanup_error or capture.error or RuntimeError(
                        "CHROMIUM_OUTPUT_DRAIN_FAILED"
                    )
                elif capture.error is not None:
                    cleanup_error = cleanup_error or RuntimeError(
                        "CHROMIUM_OUTPUT_DRAIN_FAILED"
                    )

        profile_removed = True
        profile = self.user_data_dir
        if profile is not None:
            profile_name = profile.name
            # Bounded retry: after a CDP browser close, Windows releases
            # profile file handles asynchronously (process tree exit plus
            # filesystem filter drivers); under load this can take tens of
            # seconds before the directory is removable.
            profile_error: Exception | None = None
            for _attempt in range(40):
                try:
                    await asyncio.to_thread(profile.cleanup)
                    profile_error = None
                    break
                except Exception as exc:
                    profile_error = exc
                    await asyncio.sleep(1.5)
            if profile_error is not None:
                profile_removed = False
                cleanup_error = cleanup_error or profile_error
            try:
                profile_removed = profile_removed and not os.path.exists(profile_name)
            except Exception as exc:
                profile_removed = False
                cleanup_error = cleanup_error or exc
            if profile_removed:
                self.user_data_dir = None

        if not process_closed:
            cleanup_error = cleanup_error or RuntimeError("CHROMIUM_PROCESS_NOT_CLOSED")
        if not websocket_closed:
            cleanup_error = cleanup_error or RuntimeError("CHROMIUM_WEBSOCKET_NOT_CLOSED")
        if not profile_removed:
            cleanup_error = cleanup_error or RuntimeError("CHROMIUM_PROFILE_NOT_REMOVED")
        if not output_drained:
            cleanup_error = cleanup_error or RuntimeError("CHROMIUM_OUTPUT_NOT_DRAINED")

        cleanup = ChromiumCleanup(
            websocket_closed=websocket_closed,
            process_closed=process_closed,
            profile_removed=profile_removed,
            output_drained=output_drained,
            error=_exception_info(cleanup_error) if cleanup_error else None,
        )
        self.last_cleanup = cleanup
        self.cdp_port = None
        if clear_fonts:
            self._loaded_fonts.clear()
            self._loaded_font_blobs.clear()
        logger.info("Chromium session closed")

        if raise_on_failure and not cleanup.ok:
            diagnostic_error = cleanup_error or RuntimeError("CHROMIUM_CLEANUP_INCOMPLETE")
            diagnostics = self._make_diagnostics(
                "cleanup", diagnostic_error, process_state, cleanup
            )
            self.last_diagnostic = diagnostics
            raise ChromiumSessionError(diagnostics) from diagnostic_error
        return cleanup

    async def _reader_loop(self) -> None:
        """Background reader routing incoming CDP message payloads to waiting futures."""
        try:
            async for raw in self.ws:
                data = json.loads(raw)
                msg_id = data.get("id")
                if msg_id in self.pending_responses:
                    fut = self.pending_responses.pop(msg_id)
                    if not fut.done():
                        fut.set_result(data)
                method = data.get("method")
                if method and method in self.event_waiters:
                    waiters = self.event_waiters.pop(method)
                    for waiter in waiters:
                        if not waiter.done():
                            waiter.set_result(data.get("params", {}))
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("CDP reader loop disconnected: %s", _sanitize_text(str(exc)))

    def _is_connected(self) -> bool:
        """Check if WebSocket connection is open."""
        if self.ws is None:
            return False
        try:
            if hasattr(self.ws, "state"):
                return self.ws.state.name == "OPEN"
            return not getattr(self.ws, "closed", False)
        except Exception:
            return False

    async def send_command(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a CDP command and await its correlated response with bounded timeout."""
        if not self._is_connected():
            raise RuntimeError("CDP_NOT_CONNECTED")

        self.msg_id += 1
        msg_id = self.msg_id
        fut = asyncio.get_running_loop().create_future()
        self.pending_responses[msg_id] = fut

        payload: dict[str, Any] = {"id": msg_id, "method": method}
        if params:
            payload["params"] = params

        await self.ws.send(json.dumps(payload))

        try:
            response = await asyncio.wait_for(fut, timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            self.pending_responses.pop(msg_id, None)
            raise TimeoutError(f"CDP_COMMAND_TIMEOUT: {method}")

        if "error" in response:
            raise RuntimeError(f"CDP_ERROR_{method}: {response['error']}")

        return response.get("result", {})

    async def evaluate_script(self, expression: str) -> Any:
        """Evaluate a JavaScript expression in the page context and return by value."""
        res = await self.send_command(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        result_obj = res.get("result", {})
        if "exceptionDetails" in res:
            raise RuntimeError(f"JS_EVALUATION_EXCEPTION: {res['exceptionDetails']}")
        return result_obj.get("value")

    async def _inject_font_face(self, font_family: str, font_bytes: bytes) -> None:
        """Internal helper to inject a font face and await document.fonts.ready."""
        b64_font = base64.b64encode(font_bytes).decode("ascii")
        js_inject = f"""
        (async () => {{
            const fontData = 'data:font/ttf;base64,{b64_font}';
            const font = new FontFace('{font_family}', `url(${{fontData}})`);
            await font.load();
            document.fonts.add(font);
            await document.fonts.ready;
            return true;
        }})()
        """
        res = await self.evaluate_script(js_inject)
        if not res:
            raise RuntimeError(f"FONT_FACE_INJECTION_FAILED: {font_family}")
        self._loaded_fonts.add(font_family)

    async def load_font_data(self, font_family: str, font_bytes: bytes) -> None:
        """Inject an in-memory font file via FontFace API and record blob for persistent recovery."""
        self._loaded_font_blobs[font_family] = font_bytes
        await self._inject_font_face(font_family, font_bytes)
        logger.info(f"Loaded font face into Chromium: {font_family}")

    @staticmethod
    def _font_shorthand(font: str | BrowserFontSelection, size_px: float) -> str:
        if isinstance(font, BrowserFontSelection):
            return f'{font.style} {font.weight} {size_px}px {json.dumps(font.family)}'
        return f'{size_px}px {json.dumps(font)}'

    async def observe_source_font(
        self,
        source_url: str,
        style_name: str,
        family_name: str | None = None,
    ) -> BrowserFontSelection:
        """Navigate to an observable source page and select its loaded face descriptors."""
        await self.start()
        load_future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.event_waiters.setdefault("Page.loadEventFired", []).append(load_future)
        navigation = await self.send_command("Page.navigate", {"url": source_url})
        if navigation.get("errorText"):
            raise ValueError(f"SOURCE_NAVIGATION_FAILED: {navigation['errorText']}")
        try:
            await asyncio.wait_for(load_future, timeout=self.timeout_seconds)
        except asyncio.TimeoutError as exc:
            waiters = self.event_waiters.get("Page.loadEventFired", [])
            if load_future in waiters:
                waiters.remove(load_future)
            raise TimeoutError("SOURCE_PAGE_LOAD_TIMEOUT") from exc
        faces = await self.evaluate_script(
            f"""
            (async () => {{
                const deadline = Date.now() + {int(self.timeout_seconds * 1000)};
                while (document.readyState !== 'complete' && Date.now() < deadline) {{
                    await new Promise(resolve => setTimeout(resolve, 50));
                }}
                const host = location.hostname.toLowerCase();
                if (host !== 'myfonts.com' && host !== 'www.myfonts.com') {{
                    throw new Error('SOURCE_NAVIGATION_LEFT_MYFONTS');
                }}
                await Promise.race([
                    document.fonts.ready,
                    new Promise((_, reject) => setTimeout(
                        () => reject(new Error('SOURCE_FONTS_TIMEOUT')),
                        {int(self.timeout_seconds * 1000)}
                    )),
                ]);
                const declared = Array.from(document.fonts);
                await Promise.allSettled(declared.map(face => face.load()));
                return declared
                    .filter(face => face.status === 'loaded' && face.family)
                    .map(face => ({{
                        family: String(face.family).replace(/^['\"]|['\"]$/g, ''),
                        style: String(face.style || 'normal'),
                        weight: String(face.weight || '400'),
                        stretch: String(face.stretch || 'normal'),
                    }}));
            }})()
            """
        )
        if not isinstance(faces, list) or not faces:
            raise ValueError("NO_OBSERVABLE_BROWSER_FONT_FACES")

        requested = style_name.lower()
        requested_italic = "italic" in requested or "oblique" in requested
        requested_weight = 400
        for label, weight in (
            ("thin", 100), ("extra light", 200), ("extralight", 200),
            ("light", 300), ("medium", 500), ("semi bold", 600),
            ("semibold", 600), ("bold", 700), ("extra bold", 800),
            ("extrabold", 800), ("black", 900),
        ):
            if label in requested:
                requested_weight = weight
                break

        expected_tokens = {
            token for token in re.split(r"[^a-z0-9]+", (family_name or "").lower())
            if len(token) > 2 and token != "font"
        }

        def score(face: dict[str, Any]) -> tuple[int, int, int]:
            face_style = str(face.get("style", "normal")).lower()
            style_penalty = 0 if requested_italic == (face_style in {"italic", "oblique"}) else 10_000
            raw_weight = str(face.get("weight", "400"))
            weights = [int(v) for v in raw_weight.split() if v.isdigit()]
            weight_penalty = min((abs(v - requested_weight) for v in weights), default=1_000)
            family_tokens = set(re.split(r"[^a-z0-9]+", str(face.get("family", "")).lower()))
            family_penalty = -100 * len(expected_tokens & family_tokens)
            return family_penalty, style_penalty, weight_penalty

        selected = min((face for face in faces if isinstance(face, dict)), key=score, default=None)
        if not selected or not str(selected.get("family", "")).strip():
            raise ValueError(f"NO_OBSERVABLE_BROWSER_STYLE_FOR_{style_name}")
        return BrowserFontSelection(
            family=str(selected["family"]).strip(),
            style=str(selected.get("style", "normal")),
            weight=str(selected.get("weight", "400")),
            stretch=str(selected.get("stretch", "normal")),
        )

    async def is_glyph_supported_in_font(
        self, font_family: str | BrowserFontSelection, code_point: int
    ) -> bool:
        """Verify whether a character is natively supported in the target font vs falling back to system fonts."""
        target_font = self._font_shorthand(font_family, 100.0)
        js_check = f"""
        (() => {{
            const char = String.fromCodePoint({code_point});
            const canvas = document.createElement('canvas');
            const ctx = canvas.getContext('2d');
            
            ctx.font = {json.dumps(target_font + ', monospace')};
            const m_target = ctx.measureText(char);
            
            ctx.font = '100px monospace';
            const m_mono = ctx.measureText(char);
            
            ctx.font = '100px serif';
            const m_serif = ctx.measureText(char);
            
            const isMonoMatch = Math.abs(m_target.width - m_mono.width) < 0.001 &&
                                Math.abs(m_target.actualBoundingBoxRight - m_mono.actualBoundingBoxRight) < 0.001 &&
                                Math.abs(m_target.actualBoundingBoxAscent - m_mono.actualBoundingBoxAscent) < 0.001;
            
            const isSerifMatch = Math.abs(m_target.width - m_serif.width) < 0.001 &&
                                 Math.abs(m_target.actualBoundingBoxRight - m_serif.actualBoundingBoxRight) < 0.001 &&
                                 Math.abs(m_target.actualBoundingBoxAscent - m_serif.actualBoundingBoxAscent) < 0.001;

            const hasInk = (m_target.actualBoundingBoxRight - m_target.actualBoundingBoxLeft) > 0.01 ||
                           (m_target.actualBoundingBoxAscent + m_target.actualBoundingBoxDescent) > 0.01 ||
                           char === ' ' || char === '\\u00A0';

            return !isMonoMatch && !isSerifMatch && hasInk;
        }})()
        """
        try:
            res = await self.evaluate_script(js_check)
            return bool(res)
        except Exception:
            return False

    async def measure_glyph_direct(
        self,
        font_family: str | BrowserFontSelection,
        code_point: int,
        font_size_px: float = 200.0,
        upem: int = 1000,
    ) -> DirectMetrics:
        """Directly measure glyph advance, bounding box, ascent, and descent via browser TextMetrics API."""
        target_font = self._font_shorthand(font_family, font_size_px)
        js_measure = f"""
        (() => {{
            const char = String.fromCodePoint({code_point});
            const canvas = document.createElement('canvas');
            const ctx = canvas.getContext('2d', {{ willReadFrequently: true }});
            ctx.font = {json.dumps(target_font)};
            const m = ctx.measureText(char);
            return {{
                width: m.width,
                actualBoundingBoxLeft: m.actualBoundingBoxLeft,
                actualBoundingBoxRight: m.actualBoundingBoxRight,
                actualBoundingBoxAscent: m.actualBoundingBoxAscent,
                actualBoundingBoxDescent: m.actualBoundingBoxDescent,
                fontBoundingBoxAscent: m.fontBoundingBoxAscent,
                fontBoundingBoxDescent: m.fontBoundingBoxDescent
            }};
        }})()
        """
        raw_m = await self.evaluate_script(js_measure)
        char = chr(code_point)
        return DirectMetrics.from_browser_measurements(
            code_point=code_point,
            char=char,
            font_size_px=font_size_px,
            m=raw_m,
            upem=upem,
        )

    async def capture_lossless_raster(
        self,
        font_family: str | BrowserFontSelection,
        code_point: int,
        resolution_px: int,
        subpixel_offset: tuple[float, float] = (0.0, 0.0),
        font_size_px: float | None = None,
    ) -> bytes:
        """Render glyph to an in-memory high-contrast Canvas and extract lossless PNG bytes."""
        sub_x, sub_y = subpixel_offset
        f_size_val = font_size_px if font_size_px is not None else "null"

        default_size = float(font_size_px) if font_size_px is not None else float(resolution_px) * 0.72
        target_font = self._font_shorthand(font_family, default_size)
        js_render = f"""
        (() => {{
            const char = String.fromCodePoint({code_point});
            const size = {resolution_px};
            const canvas = document.createElement('canvas');
            canvas.width = size;
            canvas.height = size;
            const ctx = canvas.getContext('2d', {{ willReadFrequently: true }});
            
            // Clean white background
            ctx.fillStyle = '#ffffff';
            ctx.fillRect(0, 0, size, size);
            
            // Subpixel phase translation
            ctx.save();
            ctx.translate({sub_x}, {sub_y});
            
            const fSize = {f_size_val} || Math.floor(size * 0.72);
            ctx.font = {json.dumps(target_font)};
            ctx.fillStyle = '#000000';
            ctx.textBaseline = 'alphabetic';
            
            // Center glyph within resolution canvas
            const m = ctx.measureText(char);
            const adv = m.width;
            const ascent = m.actualBoundingBoxAscent || (fSize * 0.72);
            const descent = m.actualBoundingBoxDescent || (fSize * 0.2);
            const totalH = ascent + descent;
            
            const x = Math.round((size - adv) / 2);
            const y = Math.round((size - totalH) / 2 + ascent);
            
            ctx.fillText(char, x, y);
            ctx.restore();
            
            return canvas.toDataURL('image/png');
        }})()
        """
        data_url = await self.evaluate_script(js_render)
        if not data_url or not isinstance(data_url, str) or not data_url.startswith("data:image/png;base64,"):
            raise ValueError("MALFORMED_CANVAS_PNG_DATA_URL")

        header, b64_data = data_url.split(",", 1)
        png_bytes = base64.b64decode(b64_data)
        if not png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("INVALID_PNG_HEADER_MAGIC")

        return png_bytes

    async def probe_opentype_feature(
        self,
        font_family: str | BrowserFontSelection,
        feature_tag: str,
        sample_text: str,
        font_size_px: float = 200.0,
        upem: int = 1000,
    ) -> dict[str, Any]:
        """Measure one OpenType feature with shaping enabled and disabled."""
        target_font = self._font_shorthand(font_family, font_size_px)
        raw = await self.evaluate_script(
            f"""
            (() => {{
                const sample = {json.dumps(sample_text)};
                const tag = {json.dumps(feature_tag)};
                const render = enabled => {{
                    const canvas = document.createElement('canvas');
                    canvas.width = 1024;
                    canvas.height = 320;
                    const ctx = canvas.getContext('2d', {{ willReadFrequently: true }});
                    ctx.fillStyle = '#fff';
                    ctx.fillRect(0, 0, canvas.width, canvas.height);
                    ctx.font = {json.dumps(target_font)};
                    ctx.fontKerning = enabled ? 'normal' : 'none';
                    ctx.fontFeatureSettings = `"${{tag}}" ${{enabled ? 1 : 0}}`;
                    ctx.fillStyle = '#000';
                    ctx.fillText(sample, 20, 240);
                    const pixels = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
                    let signature = 2166136261;
                    for (let i = 0; i < pixels.length; i += 4) {{
                        signature ^= pixels[i];
                        signature = Math.imul(signature, 16777619) >>> 0;
                    }}
                    return {{ width: ctx.measureText(sample).width, signature: signature.toString(16) }};
                }};
                return {{ enabled: render(true), disabled: render(false) }};
            }})()
            """
        )
        scale = float(upem) / max(font_size_px, 1.0)
        return {
            "enabled_advance_upem": round(float(raw["enabled"]["width"]) * scale, 2),
            "disabled_advance_upem": round(float(raw["disabled"]["width"]) * scale, 2),
            "enabled_raster_signature": str(raw["enabled"]["signature"]),
            "disabled_raster_signature": str(raw["disabled"]["signature"]),
        }

    async def measure_text_advance(
        self,
        font_family: str | BrowserFontSelection,
        text: str,
        font_size_px: float = 200.0,
        upem: int = 1000,
    ) -> float:
        """Measure observable browser shaping advance for an arbitrary text sample."""
        target_font = self._font_shorthand(font_family, font_size_px)
        width = await self.evaluate_script(
            f"""
            (() => {{
                const canvas = document.createElement('canvas');
                const ctx = canvas.getContext('2d');
                ctx.font = {json.dumps(target_font)};
                return ctx.measureText({json.dumps(text)}).width;
            }})()
            """
        )
        return (float(width) / max(font_size_px, 1.0)) * upem

    async def restart(self) -> None:
        """Explicitly restart Chromium after a caller has observed a failed session."""
        logger.warning("Restarting persistent Chromium session...")
        await self.aclose(clear_fonts=False)
        await self.start()

    def _close_task_done(self, task: asyncio.Task[ChromiumCleanup]) -> None:
        if self._pending_close_task is task:
            self._pending_close_task = None
        try:
            task.result()
        except asyncio.CancelledError:
            self.last_diagnostic = self._make_diagnostics(
                "cleanup",
                RuntimeError("CHROMIUM_CLEANUP_CANCELLED"),
                self._process_state(),
                self.last_cleanup
                or ChromiumCleanup(False, False, False, False, None),
            )
        except ChromiumSessionError as exc:
            self.last_diagnostic = exc.diagnostics
            logger.error("Chromium session cleanup failed: %s", exc)
        except Exception as exc:  # pragma: no cover - defensive task boundary
            cleanup = self.last_cleanup or ChromiumCleanup(False, False, False, False, None)
            self.last_diagnostic = self._make_diagnostics(
                "cleanup", exc, self._process_state(), cleanup
            )
            logger.error("Chromium session cleanup failed: %s", _sanitize_text(str(exc)))

    def close(self, clear_fonts: bool = True) -> ChromiumCleanup | None:
        """Compatibility close wrapper; async callers can await the scheduled cleanup task."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.aclose(clear_fonts=clear_fonts))

        if self._pending_close_task is not None:
            if not self._pending_close_task.done():
                return None
            self._pending_close_task = None
        task = loop.create_task(self.aclose(clear_fonts=clear_fonts))
        self._pending_close_task = task
        task.add_done_callback(self._close_task_done)
        return None
