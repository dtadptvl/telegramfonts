"""Persistent Chromium measurement session driving direct browser metrics & lossless raster capture via CDP."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

import websockets

from measurement.models import DirectMetrics

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
        self.read_task: asyncio.Task[None] | None = None
        self.browser_version: str = "unknown"
        self._loaded_fonts: set[str] = set()

    async def start(self) -> None:
        """Launch headless Chromium subprocess and initialize CDP WebSocket session."""
        if self.process is not None and self.process.poll() is None and self.ws is not None:
            return

        self.user_data_dir = tempfile.TemporaryDirectory(prefix="telefont_chrome_")
        target_port = self.port if self.port > 0 else 9222

        cmd = [
            self.executable_path,
            "--headless=new",
            f"--remote-debugging-port={target_port}",
            f"--user-data-dir={self.user_data_dir.name}",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-software-rasterizer",
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
        start_time = asyncio.get_event_loop().time()

        for _ in range(30):
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

    async def load_font_data(self, font_family: str, font_bytes: bytes) -> None:
        """Inject an in-memory font file via FontFace API and wait for document.fonts to be ready."""
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
        await self.evaluate_script(js_inject)
        self._loaded_fonts.add(font_family)
        logger.info(f"Loaded font face into Chromium: {font_family}")

    async def measure_glyph_direct(
        self,
        font_family: str,
        code_point: int,
        font_size_px: float = 200.0,
        upem: int = 1000,
    ) -> DirectMetrics:
        """Directly measure glyph advance, bounding box, ascent, and descent via browser TextMetrics API."""
        js_measure = f"""
        (() => {{
            const char = String.fromCodePoint({code_point});
            const canvas = document.createElement('canvas');
            const ctx = canvas.getContext('2d', {{ willReadFrequently: true }});
            ctx.font = '{font_size_px}px "{font_family}"';
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
        font_family: str,
        code_point: int,
        resolution_px: int,
        subpixel_offset: tuple[float, float] = (0.0, 0.0),
        font_size_px: float | None = None,
    ) -> bytes:
        """Render glyph to an in-memory high-contrast Canvas and extract lossless PNG bytes."""
        sub_x, sub_y = subpixel_offset
        f_size_val = font_size_px if font_size_px is not None else "null"

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
            ctx.font = `${{fSize}}px "{font_family}"`;
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

    async def restart(self) -> None:
        """Gracefully restart Chromium process on unexpected termination or timeout."""
        logger.warning("Restarting persistent Chromium session...")
        self.close()
        await self.start()

    def close(self) -> None:
        """Clean up background tasks, WebSocket, and child Chromium process."""
        if self.read_task and not self.read_task.done():
            self.read_task.cancel()
            self.read_task = None

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
        logger.info("Chromium session closed")
