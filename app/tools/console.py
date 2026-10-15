"""The console: a Python file of the workspace run with the user at the keyboard (REBUILD_PLAN 2.12). The program's
output streams to the page as it is written, and each line the user types goes to its input() - a game, a menu, a quiz.

It is the same program a run starts (sandbox.start: the set-up, the audit hook, the clean environment), started by
the user from the file - in its card in the chat or in the file panel. What happened is kept as the file's report
(run.keep_report): the facts of the run and the session as it went - what the program printed and what the user typed
- so the model reads it when the user asks about it ("what did I score?").

A session ends when the program does, when the user stops it, or at a limit: MAX_SECONDS in all, IDLE_SECONDS with
nothing typed. One session per file at a time (starting it again ends the one before).
"""

from __future__ import annotations

import codecs
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass, field

from . import sandbox
from .core import ToolError, ToolResult, jsonable
from .filetypes import suffix
from .run import keep_report
from .search_index import SearchIndex
from .workspace import Workspace

MAX_SECONDS = 15 * 60  # a session ends after this, typed or not
IDLE_SECONDS = 5 * 60  # ... or after this long with nothing typed
MAX_LINE = 2_000  # characters a typed line may have
KEPT = 4_000  # characters of the session kept in the file's report (its end, when longer)
PROMPT = "› "  # how a typed line shows in the kept session


@dataclass
class Session:
    id: str
    ws: Workspace
    index: SearchIndex | None
    key: str
    name: str
    code: str
    proc: subprocess.Popen | None = None
    before: dict = field(default_factory=dict)
    started: float = 0.0
    typed_at: float = 0.0
    events: list[dict] = field(default_factory=list)  # out, err, in, end - in order; a page that reconnects replays them
    typed: list[str] = field(default_factory=list)
    ended: dict | None = None
    stopped: str | None = None  # why it ended before the program did (a Stop, a limit)
    _said: threading.Condition = field(default_factory=threading.Condition)

    # ------------------------------------------------------------- what the page calls
    def type(self, line: str) -> None:
        """One line the user typed, to the program's input()."""
        if self.ended or self.proc is None or self.proc.stdin is None:
            raise ToolError("The program has ended; start it again to type more.", invalid=False)
        line = str(line).replace("\r", "").split("\n")[0][:MAX_LINE]
        self.typed.append(line)
        self.typed_at = time.monotonic()
        self._add({"type": "in", "text": line})
        try:
            self.proc.stdin.write((line + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except OSError:  # it ended between the check and the write
            pass

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.stopped = "stopped"
            sandbox.stop(self.proc)

    def follow(self, start: int = 0, wait: float = 15.0):
        """The session's events from index start, then each new one as it comes, until it has ended; None when
        nothing came for `wait` seconds (the page's stream sends a keep-alive)."""
        n = start
        while True:
            with self._said:
                if n >= len(self.events) and not self.ended:
                    self._said.wait(wait)
                fresh = self.events[n:]
                done = self.ended is not None
            if fresh:
                yield from fresh
                n += len(fresh)
            elif done:
                return
            else:
                yield None

    # ------------------------------------------------------------- inside
    def _add(self, event: dict) -> None:
        with self._said:
            self.events.append(event)
            self._said.notify_all()

    def _pump(self, stream, kind: str) -> None:
        """The program's output as it is written: each chunk an event (an input() prompt has no line end)."""
        decode = codecs.getincrementaldecoder("utf-8")("replace")
        held = ""  # a chunk's last "\r": its "\n" may come in the next one (Windows writes "\r\n")
        while True:
            chunk = stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096)
            if not chunk:
                break
            text = held + decode.decode(chunk)
            held = "\r" if text.endswith("\r") else ""
            text = (text[:-1] if held else text).replace("\r\n", "\n")
            if kind == "err":
                text = sandbox.own_errors(text, self.ws, self._run_dir)
            if text:
                self._add({"type": kind, "text": text})
        rest = decode.decode(b"", final=True)
        if rest:
            self._add({"type": kind, "text": rest})

    @property
    def _run_dir(self):
        return self.ws.zen / "console" / self.key

    def _watch(self, pumps: list[threading.Thread]) -> None:
        """Wait for the end - the program's, a Stop, or a limit - then keep what happened."""
        while self.proc.poll() is None:
            now = time.monotonic()
            if now - self.started > MAX_SECONDS:
                self.stopped = f"stopped after {MAX_SECONDS // 60} minutes (the console's limit)"
                sandbox.stop(self.proc)
            elif now - self.typed_at > IDLE_SECONDS:
                self.stopped = f"stopped after {IDLE_SECONDS // 60} minutes with nothing typed"
                sandbox.stop(self.proc)
            time.sleep(0.2)
        for p in pumps:
            p.join(timeout=5)
        seconds = round(time.monotonic() - self.started, 1)
        _, made = sandbox.register_since(self.ws, self.before)
        for f in made:  # what it made comes from the program, as a run's does (the file panel lists it under it)
            if not f.get("updated") and f["id"] != self.key:
                self.ws.set_parent(f["id"], self.key)
        output = "".join(e["text"] for e in self.events if e["type"] == "out")
        errors = "".join(e["text"] for e in self.events if e["type"] == "err")
        failed = self.proc.returncode != 0 and not self.stopped
        result = ToolResult("invalid_arguments" if failed else "ok",
                            jsonable({"exit_code": self.proc.returncode, "seconds": seconds, "output": output,
                                      **({"errors": errors} if errors.strip() else {})}),
                            jsonable({"type": "code", "files": made}))
        keep_report(self.ws, self.index, self.key, self.code, result, "\n".join(self.typed) or None,
                    session=self.transcript())
        end = {"type": "end", "exit_code": self.proc.returncode, "seconds": seconds, "files": made,
               **({"stopped": self.stopped} if self.stopped else {})}
        with self._said:
            self.ended = end
            self.events.append(end)
            self._said.notify_all()

    def transcript(self) -> str:
        """The session as it went - what the program printed, each typed line after its prompt - its end kept."""
        text = "".join(e["text"] if e["type"] != "in" else f"{PROMPT}{e['text']}\n"
                       for e in self.events if e["type"] in ("out", "err", "in"))
        return text if len(text) <= KEPT else "... " + text[-KEPT:]


_sessions: dict[str, Session] = {}
_guard = threading.Lock()


def start(ws: Workspace, index: SearchIndex | None, key: str) -> Session:
    """Run a Python file of the workspace in a console; the session (its id for typing, following and stopping)."""
    name = ws.label(key)
    if suffix(name) != ".py":
        raise ToolError(f"{name} isn't Python: only a Python file runs in the console.", field="file")
    code = ws.path(key).read_text("utf-8", errors="replace")
    if not code.strip():
        raise ToolError(f"{name} is empty.", field="file")
    with _guard:
        for old in [s for s in _sessions.values() if s.ws.root == ws.root and s.key == key and not s.ended]:
            old.stop()  # the same file again: the run before ends
        session = Session(secrets.token_hex(8), ws, index, key, name, code)
        _sessions[session.id] = session
        for done in [k for k, s in _sessions.items() if s.ended and time.monotonic() - s.started > MAX_SECONDS]:
            del _sessions[done]  # finished long ago: nobody follows them any more
    session.proc, session.before = sandbox.start(ws, code, name, session._run_dir)
    session.started = session.typed_at = time.monotonic()
    pumps = [threading.Thread(target=session._pump, args=(session.proc.stdout, "out"), daemon=True),
             threading.Thread(target=session._pump, args=(session.proc.stderr, "err"), daemon=True)]
    for p in pumps:
        p.start()
    threading.Thread(target=session._watch, args=(pumps,), daemon=True).start()
    return session


def get(session_id: str, ws: Workspace) -> Session:
    session = _sessions.get(session_id)
    if session is None or session.ws.root != ws.root:
        raise ToolError("No such console session; start the program again.", invalid=False)
    return session
