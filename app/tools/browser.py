# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
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


# A page Zen wrote, tried before it is called ready (measured 2026-10-17: a cup game parsed but its Start button died on
# 'sleep is not defined', and Zen said "Click Start Game"). It runs under the chat frame's own policy (web/js/files.js,
# SEALED: nothing loads from anywhere) and the head the chat gives it (zenHead there: the theme's colours and a page
# storage, PAGE_HEAD here, the dark theme), with dialogs answered so none can stop the try. Its picture is taken as it
# opens (the card in the chat shows it); then its controls are clicked and the keys a game is played with pressed, for
# TRY_SECONDS at most (measured: 2048's 16 tiles, a second each, three rounds - every edit waited the full minute), and
# its errors kept - thrown, or a promise that failed - and a canvas that still shows one flat colour: it draws nothing
# (measured: a racing game whose Start button stayed hidden, a white board each time).
PAGE_POLICY = ("default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval'; style-src 'unsafe-inline'; "
               "img-src data: blob:; media-src data: blob:; font-src data:; connect-src 'none'; form-action 'none'; "
               "frame-src 'none'")
CATCH = ("<script>window.__errs=[];addEventListener('error',e=>__errs.push(String(e.message)+(e.lineno?' (line '+e.lineno"
         "+')':'')));addEventListener('unhandledrejection',e=>__errs.push(String(e.reason&&e.reason.message||e.reason)));"
         "window.alert=()=>{};window.confirm=()=>true;window.prompt=()=>'';window.__said=[];"
         "for(const C of [window.CanvasRenderingContext2D,window.OffscreenCanvasRenderingContext2D])if(C)for(const m of "
         "['fillText','strokeText']){const f=C.prototype[m];C.prototype[m]=function(t,...a){"
         "if(__said.length<2000&&this.canvas&&this.canvas.isConnected)__said.push(String(t));"
         "return f.call(this,t,...a)}}</script>")
PAGE_HEAD = ("<style>:root{--zen-bg:#2a2a2a;--zen-ink:#ececec;--zen-muted:#b4b4b4;--zen-line:rgba(255,255,255,.09);"
             "--zen-accent:#ececec;--zen-on-accent:#0d0d0d;--zen-green:#30d158;--zen-blue:#409cff;--zen-red:#ff6961;"
             "--zen-orange:#ffb340;--zen-yellow:#ffd60a;--zen-purple:#da8fff;color-scheme:dark}"
             "*,*::before,*::after{box-sizing:border-box}html,body{margin:0}body{background:var(--zen-bg);"
             "color:var(--zen-ink);font:16px/1.5 'Segoe UI',system-ui,sans-serif}</style>"
             "<script>(()=>{const d={},s={getItem:k=>k in d?d[k]:null,setItem:(k,v)=>{d[k]=String(v)},"
             "removeItem:k=>{delete d[k]},clear:()=>{for(const k in d)delete d[k]},key:i=>Object.keys(d)[i]??null,"
             "get length(){return Object.keys(d).length}};try{localStorage.length}catch(e){"
             "Object.defineProperty(window,'localStorage',{value:s,configurable:true})}})()</script>")
TRY_SECONDS = 10
VIEW = {"width": 720, "height": 450}  # the chat lays a page out at least this wide (files.js DESIGN_W)
CLICK_THROUGH = """async () => {
  const wait = ms => new Promise(r => setTimeout(r, ms)), end = Date.now() + %d;
  const clicked = [], seen = {};
  await wait(500);
  // over before it began: 'Game Over' on show (in the page, or drawn on its canvas in the page) before any key or click
  // (measured 2026-10-17: a snake that didn't move yet ran into itself on its first step - Game Over, score 0, at once).
  // On show is what a person would see: no ancestor hidden or see-through, on screen, on top (measured the same night:
  // 2048's overlay, opacity 0 on its parent, was taken for shown - and Zen chased a bug that wasn't there for 8 minutes).
  // The words lead the text: an instruction that mentions them ("Any key after game over to restart") says nothing
  // (measured 2026-10-21: it was quoted as the game over, and Zen rewrote a working draw loop four times)
  const OVER = /^\\W*(game over|you (lose|lost|died)|crashed)\\b/i;
  const visible = e => {
    if (e.checkVisibility && !e.checkVisibility({opacityProperty: true, visibilityProperty: true})) return false;
    let opacity = 1;
    for (let n = e; n && n !== document.documentElement; n = n.parentElement) opacity *= Number(getComputedStyle(n).opacity);
    const r = e.getBoundingClientRect();
    if (opacity < 0.5 || r.width < 2 || r.height < 2 || r.bottom <= 0 || r.right <= 0 || r.top >= innerHeight || r.left >= innerWidth) return false;
    const top = document.elementFromPoint(Math.min(innerWidth - 1, Math.max(0, r.left + r.width / 2)), Math.min(innerHeight - 1, Math.max(0, r.top + r.height / 2)));
    return !!top && (e.contains(top) || top.contains(e));
  };
  const over = () => {
    const shown = [...document.querySelectorAll('body *')].filter(e => e.children.length === 0 && OVER.test((e.textContent || '').trim()) && visible(e));
    return (shown[0] && shown[0].textContent.trim().slice(0, 40)) || window.__said.find(t => OVER.test(t.trim())) || null;
  };
  // and again three seconds in, still untouched: a snake that sets off at once is in the wall by then, before a person
  // has found the keys (measured 2026-10-21: four snakes out of four did, and the check, a second in, saw one)
  let overAtStart = over();
  if (!overAtStart) { await wait(2500); overAtStart = over(); }
  const keys = ['Enter', ' ', 'ArrowUp', 'ArrowRight', 'ArrowLeft', 'ArrowDown', 'w', 'a', 'd'];
  for (let round = 0; round < 2 && !window.__errs.length && Date.now() < end; round++) {
    const targets = [...document.querySelectorAll('button, input[type=button], input[type=submit], [onclick], [role=button], '
      + '[tabindex], [class*=cup], [class*=card], [class*=cell], [class*=tile], [class*=key], canvas')]
      .filter(e => e.offsetParent !== null && !e.disabled);
    // a grid's sixteen tiles are one control many times over: three of each kind are clicked
    for (const el of targets) {
      const kind = el.tagName + '.' + el.className;
      if (Date.now() > end) break;
      if ((seen[kind] = (seen[kind] || 0) + 1) > 3 && el.tagName !== 'BUTTON') continue;
      el.click();
      clicked.push((el.textContent || el.getAttribute('aria-label') || el.className || el.tagName).trim().slice(0, 30));
      await wait(300);
      if (window.__errs.length) break;
    }
    for (const key of keys) {
      if (window.__errs.length || Date.now() > end) break;
      for (const type of ['keydown', 'keyup']) {
        const e = new KeyboardEvent(type, {key, code: key === ' ' ? 'Space' : key.length > 1 ? key : 'Key' + key.toUpperCase(), bubbles: true});
        (document.activeElement || document.body).dispatchEvent(e);
      }
      await wait(150);
    }
  }
  await wait(600);
  // a canvas still one colour all over: nothing is drawn on it (a WebGL or WebGPU canvas can't be read here: left out)
  const flat = [];
  for (const c of document.querySelectorAll('canvas')) {
    if (c.offsetParent === null || c.width * c.height < 10000) continue;
    let ctx = null;
    try { ctx = c.getContext('2d'); } catch {}
    if (!ctx) continue;
    const px = ctx.getImageData(0, 0, c.width, c.height).data;
    let same = true;
    for (let i = 4; i < px.length; i += 4 * 7) {
      if (Math.abs(px[i] - px[0]) + Math.abs(px[i + 1] - px[1]) + Math.abs(px[i + 2] - px[2]) + Math.abs(px[i + 3] - px[3]) > 12) { same = false; break; }
    }
    if (same) flat.push(c.width + 'x' + c.height + (px[3] ? ' rgb(' + px[0] + ',' + px[1] + ',' + px[2] + ')' : ' empty'));
  }
  return JSON.stringify({errors: window.__errs.slice(0, 5), clicked, flat, over_at_start: overAtStart});
}""" % (TRY_SECONDS * 1000)


def try_page(html: str, shot: Path | None = None) -> dict | None:
    """{errors, clicked, flat, over_at_start}: the page opened headless, its controls clicked and keys pressed; flat:
    its canvases that still draw nothing; over_at_start: 'Game Over' (its words) on show before anything was pressed.
    shot: where its picture as it opens is saved. None without a browser, or when the try itself
    failed (then nothing is claimed either way)."""
    if not available():
        return None
    import base64
    page = f'<meta http-equiv="Content-Security-Policy" content="{PAGE_POLICY}">{CATCH}{PAGE_HEAD}{html}'
    url = "data:text/html;base64," + base64.b64encode(page.encode("utf-8")).decode("ascii")
    browser = _server()
    try:
        browser.call("browser_resize", VIEW)
        browser.call("browser_navigate", {"url": url})
        if shot is not None:
            _picture(browser, shot)
        return json.loads(_result(browser.call("browser_evaluate", {"function": CLICK_THROUGH},
                                               timeout=TRY_SECONDS + 15)))
    except (McpError, ValueError, TypeError):
        return None
    finally:
        try:  # web pages are read at the browser's usual size
            browser.call("browser_resize", {"width": 1280, "height": 720})
        except McpError:
            pass
        _empty(_output)


def _picture(browser: McpServer, shot: Path) -> None:
    """The page as it opens, a moment after (its first frames drawn), saved to shot; nothing when it can't be taken."""
    taken = _output / "page.png"
    try:
        browser.call("browser_wait_for", {"time": 0.8})
        browser.call("browser_take_screenshot", {"type": "png", "filename": str(taken)})
        if taken.is_file():
            shot.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(taken, shot)
    except (McpError, OSError):
        pass


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
