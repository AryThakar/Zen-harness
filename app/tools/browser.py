"""A real browser for the pages a plain read can't get - a page drawn by JavaScript, or one a site shows only
to browsers - through Playwright MCP (Microsoft's MCP server for Playwright, Apache 2.0) and the official
MCP Python SDK.

    McpServer   one MCP server run over stdio: started on first use, closed when idle, one call at a time.
                Generic - another MCP server later is a setting, not code.
    page(url)   the browser's reading of one page: (final address, title, HTML). read_webpage puts the HTML
                through the same extraction as a plain read (web.py), so the browser changes how a page is
                fetched, never what the helper sees - no accessibility snapshot, no screenshot.

The browser: the installed Edge (settings.browser_channel), headless, an in-memory profile (no cookies or
logins kept), its own user agent (it says it is headless: no stealth, no proxy - a site that refuses it is
reported as blocked). Only public addresses: the address is checked before the browser opens it and the page
it ends on after (a redirect to this computer is refused, and its HTML never read); this computer's own
services are also blocked inside the browser, though Playwright says that list is no security boundary.
Nothing it downloads is kept: its output folder is emptied after every page.
Settings: ASSISTANT_PLAYWRIGHT_MCP, the folder Playwright MCP is installed in (npm install @playwright/mcp; run
by node), or its cli.js; empty, there is no browser, and such pages are reported as before.
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import json
import logging
import shutil
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit

from app.config import settings

log = logging.getLogger(__name__)

IDLE = 300  # seconds without a call before the server (and its browser) is closed
CALL_TIMEOUT = 60  # seconds for one tool call
START_TIMEOUT = 60
SETTLE_MS = 1200  # a page drawn by JavaScript gets this long after it loads, before its HTML is taken
READ_HTML = (f"async () => {{ await new Promise(r => setTimeout(r, {SETTLE_MS})); "
             "return document.documentElement.outerHTML; }")


class McpError(RuntimeError):
    """The MCP server could not do it; the message says why."""


class McpServer:
    """One MCP server over stdio, owned by a thread of its own: an asyncio task enters the SDK's client and
    session and serves calls from a queue until it has been idle for ``idle`` seconds (the SDK's task groups
    must be left by the task that entered them, so one task does it all)."""

    def __init__(self, name: str, command: str, args: list[str], idle: float = IDLE):
        self.name, self.command, self.args, self.idle = name, command, args, idle
        self._lock = threading.Lock()  # one call at a time: one browser, one page
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._thread: threading.Thread | None = None
        self._error: Exception | None = None

    def call(self, tool: str, arguments: dict, timeout: float = CALL_TIMEOUT) -> str:
        """The tool's text result; McpError when it fails or the server can't run."""
        with self._lock:
            for attempt in (1, 2):  # the server may have closed itself (idle) just now: start it once more
                self._start()
                future: concurrent.futures.Future = concurrent.futures.Future()
                self._loop.call_soon_threadsafe(self._queue.put_nowait, (tool, arguments, timeout, future))
                try:
                    return future.result(timeout + 5)
                except _Closed:
                    if attempt == 2:
                        raise McpError(f"{self.name} closed before it answered.") from None
                except concurrent.futures.TimeoutError:
                    self._stop()
                    raise McpError(f"{self.name} did not answer within {timeout:g} s.") from None
        raise AssertionError("unreachable")

    def close(self) -> None:
        with self._lock:
            self._stop()

    def _start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        ready = threading.Event()
        self._error = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_until_complete, args=(self._own(ready),),
                                        name=f"mcp-{self.name}", daemon=True)
        self._thread.start()
        if not ready.wait(START_TIMEOUT) or self._error is not None:
            self._stop()
            raise McpError(f"{self.name} could not start: {self._error or 'no answer'}")

    def _stop(self) -> None:
        if self._thread is not None and self._thread.is_alive() and self._loop is not None:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
            self._thread.join(15)
        self._thread = None

    async def _own(self, ready: threading.Event) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        self._queue = asyncio.Queue()
        try:
            async with stdio_client(StdioServerParameters(command=self.command, args=self.args)) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    ready.set()
                    while True:
                        try:
                            item = await asyncio.wait_for(self._queue.get(), self.idle)
                        except asyncio.TimeoutError:
                            break  # idle: close the server and its browser; the next call starts them again
                        if item is None:
                            break
                        tool, arguments, timeout, future = item
                        try:
                            result = await asyncio.wait_for(session.call_tool(tool, arguments), timeout)
                            text = "\n".join(c.text for c in result.content if getattr(c, "text", None))
                            if getattr(result, "is_error", False):
                                future.set_exception(McpError(text.strip() or f"{tool} failed"))
                            else:
                                future.set_result(text)
                        except Exception as exc:  # noqa: BLE001 - the caller gets it
                            future.set_exception(McpError(f"{tool}: {exc}") if not isinstance(exc, McpError) else exc)
        except Exception as exc:  # noqa: BLE001 - starting failed, or the server went away
            self._error = exc
            log.warning("MCP server %s stopped: %s", self.name, exc)
        finally:
            ready.set()
            while not self._queue.empty():  # calls that came in as it closed: their callers start it again
                item = self._queue.get_nowait()
                if item is not None:
                    item[3].set_exception(_Closed())


class _Closed(Exception):
    pass


# ---------------------------------------------------------------- the browser

_browser: McpServer | None = None
_output: Path | None = None
_made = threading.Lock()


def _cli() -> Path | None:
    """Playwright MCP's cli.js: the setting names it, or the folder it was installed in (npm install @playwright/mcp)."""
    path = Path(settings.playwright_mcp) if settings.playwright_mcp else None
    if path is not None and path.is_dir():
        path = path / "node_modules" / "@playwright" / "mcp" / "cli.js"
    return path if path is not None and path.is_file() else None


def available() -> bool:
    return _cli() is not None and shutil.which("node") is not None


def _server() -> McpServer:
    global _browser, _output
    with _made:
        if _browser is None:
            _output = Path(tempfile.mkdtemp(prefix="zen-browser-"))
            local = sorted({_origin(u) for u in (settings.llamacpp_url, "http://127.0.0.1:8000", "http://localhost:8000")})
            _browser = McpServer("the browser", "node", [
                str(_cli()), "--headless", "--browser", settings.browser_channel, "--isolated",
                "--snapshot-mode", "none", "--image-responses", "omit", "--block-service-workers",
                "--timeout-navigation", "30000", "--output-dir", str(_output), "--blocked-origins", ";".join(local)])
            atexit.register(_browser.close)
        return _browser


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def page(url: str, check) -> tuple[str, str | None, str]:
    """(final address, title, HTML) of one page as the browser shows it. ``check(url)`` refuses an address
    that isn't public (web._check_public): before the browser opens it, and the page it ends on."""
    check(url)
    browser = _server()
    try:
        opened = browser.call("browser_navigate", {"url": url})
        final = _field(opened, "Page URL") or url
        check(final)
        html = _result(browser.call("browser_evaluate", {"function": READ_HTML}))
        return final, _field(opened, "Page Title"), html
    finally:
        _empty(_output)  # nothing it downloaded or saved is kept


def _field(text: str, name: str) -> str | None:
    line = next((ln for ln in text.splitlines() if ln.strip().startswith(f"- {name}:")), None)
    value = line.split(":", 1)[1].strip() if line else ""
    return value or None


def _result(text: str) -> str:
    """The value browser_evaluate returned: the JSON after its '### Result' heading."""
    at = text.find("### Result")
    if at < 0:
        raise McpError("The browser gave no page.")
    start = text.find('"', at)
    try:
        value, _ = json.JSONDecoder().raw_decode(text, start)
    except (ValueError, IndexError) as exc:
        raise McpError("The browser's page could not be read.") from exc
    return str(value)


def _empty(folder: Path | None) -> None:
    if folder is None or not folder.is_dir():
        return
    for item in folder.iterdir():
        shutil.rmtree(item, ignore_errors=True) if item.is_dir() else item.unlink(missing_ok=True)
