"""Persistent Chromium measurement session driving direct browser metrics & lossless raster capture via CDP."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

import websockets

from measurement.models import BrowserFontSelection, DirectMetrics

logger = logging.getLogger("telegramfonts.agent.measurement.browser")


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


class ChromiumSession:
    """Persistent, long-lived headless Chromium session with bounded timeouts and automatic recovery."""

    def __init__(
        self,
        executable_path: str | None = None,
        timeout_seconds: float = 10.0,
        port: int = 0,
    ) -> None:
        self.executable_path = executable_path or find_chromium_executable()
        self.timeout_seconds = timeout_seconds
        self.port = port
        self.process: subprocess.Popen[str] | None = None
        self.user_data_dir: tempfile.TemporaryDirectory[str] | None = None
        self.ws_url: str | None = None
        self.ws: Any = None
        self.msg_id: int = 0
        self.pending_responses: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.event_waiters: dict[str, list[asyncio.Future[dict[str, Any]]]] = {}
        self.read_task: asyncio.Task[None] | None = None
        self.browser_version: str = "unknown"
        self._loaded_fonts: set[str] = set()
        self._loaded_font_blobs: dict[str, bytes] = {}

    async def start(self) -> None:
        """Launch headless Chromium subprocess and initialize CDP WebSocket session."""
        if self.process is not None and self.process.poll() is None and self.ws is not None:
            return

        self.user_data_dir = tempfile.TemporaryDirectory(prefix="telefont_chrome_")
        if self.port > 0:
            target_port = self.port
        else:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("", 0))
                target_port = int(s.getsockname()[1])

        cmd = [
            self.executable_path,
            "--headless=new",
            f"--remote-debugging-port={target_port}",
            f"--user-data-dir={self.user_data_dir.name}",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-default-apps",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-extensions",
            "--window-size=1280,800",
            "about:blank",
        ]

        logger.info(f"Launching persistent Chromium session on port {target_port}")
        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )

        # Wait for CDP HTTP endpoint readiness
        http_url = f"http://127.0.0.1:{target_port}"
        page_ws_url: str | None = None

        for _ in range(50):
            try:
                req = urllib.request.Request(f"{http_url}/json/version")
                with urllib.request.urlopen(req, timeout=1.0) as resp:
                    vdata = json.loads(resp.read().decode("utf-8"))
                    self.browser_version = vdata.get("Browser", "Chromium/unknown")
                
                list_req = urllib.request.Request(f"{http_url}/json/list")
                with urllib.request.urlopen(list_req, timeout=1.0) as list_resp:
                    pages = json.loads(list_resp.read().decode("utf-8"))
                    if pages and len(pages) > 0:
                        page_ws_url = pages[0].get("webSocketDebuggerUrl")
                        break
            except Exception:
                await asyncio.sleep(0.1)

        if not page_ws_url:
            self.close()
            raise RuntimeError(f"FAILED_TO_CONNECT_CHROMIUM_CDP on {http_url}")

        self.ws_url = page_ws_url
        self.ws = await websockets.connect(self.ws_url, max_size=20 * 1024 * 1024)
        self.read_task = asyncio.create_task(self._reader_loop())

        # Enable Page and Runtime domains
        await self.send_command("Page.enable")
        await self.send_command("Runtime.enable")
        logger.info(f"Persistent Chromium session ready: {self.browser_version}")

        # Restore any previously registered font faces into the fresh document context
        if self._loaded_font_blobs:
            for family_name, blob in list(self._loaded_font_blobs.items()):
                await self._inject_font_face(family_name, blob)
            logger.info(f"Restored {len(self._loaded_font_blobs)} font faces after session start/recovery")

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
            logger.debug(f"CDP reader loop disconnected: {exc}")

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
            await self.restart()

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
        """Gracefully restart Chromium process on unexpected termination or timeout."""
        logger.warning("Restarting persistent Chromium session...")
        self.close(clear_fonts=False)
        await self.start()

    def close(self, clear_fonts: bool = True) -> None:
        """Clean up background tasks, WebSocket, and child Chromium process."""
        if self.read_task and not self.read_task.done():
            self.read_task.cancel()
            self.read_task = None

        for waiters in self.event_waiters.values():
            for waiter in waiters:
                if not waiter.done():
                    waiter.cancel()
        self.event_waiters.clear()

        if self.ws:
            try:
                asyncio.create_task(self.ws.close())
            except Exception:
                pass
            self.ws = None

        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=2.0)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None

        if self.user_data_dir:
            try:
                self.user_data_dir.cleanup()
            except Exception:
                pass
            self.user_data_dir = None

        self._loaded_fonts.clear()
        if clear_fonts:
            self._loaded_font_blobs.clear()
        logger.info("Chromium session closed")
