# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""run: a helper runs a Python file of the workspace - in the workspace's folder, through the one engine
(sandbox.ENGINE) - and the app keeps what it knows of the file's last run as its report: facts written by code
(passed or failed, what it printed, what it made), so main and the next task know the file without its code.

The file is the state: a program is written with files(write), run, changed with files(edit) and run again; it
stays in the workspace, so the next task - or the same task after a Stop - goes on from it instead of writing it
again (measured before 2026-10-10: a scratch script thrown away with each task was rewritten whole 8 times in one
task, and its old copies filled the helper's context until it overflowed).
"""

from __future__ import annotations

import ast
import re
from datetime import datetime

from . import sandbox
from .core import ToolError, ToolResult, tool
from .filetypes import suffix
from .search_index import SearchIndex
from .workspace import Workspace

AROUND = 3  # lines shown on each side of the failing one
PRINTED = 400  # characters of the last run's output kept in the report


@tool
def run(ws: Workspace, index: SearchIndex | None, file: str, input: str | None = None,
        timeout: int = sandbox.DEFAULT_TIMEOUT) -> ToolResult:
    """Run a Python file of the workspace (input: what a person would type, one line per input())."""
    key = ws.resolve(file, ("file",), field="file")
    name = ws.label(key)
    if suffix(name) != ".py":
        raise ToolError(f"{name} isn't Python: only Python runs here. An .html file is opened by the user; a file in "
                        "another language is kept as it is.", field="file")
    code = ws.path(key).read_text("utf-8", errors="replace")
    result = sandbox.ENGINE.run(ws, code, timeout, stdin=input, name=name)
    res = result.result
    made = (result.artifact or {}).get("files", [])
    for f in made:
        if not f.get("updated") and f["id"] != key:
            ws.set_parent(f["id"], key)
    if result.status != "ok":
        found = re.findall(rf'File "{re.escape(name)}", line (\d+)', res.get("errors", ""))
        if found:  # the file's own line the error ends in: the last of its frames
            at = int(found[-1])
            res["at_line"] = at
            res["around"] = around(code, at)
            res["error"]["message"] += (f" It points at line {at} (shown in around): change it with files(mode='edit', "
                                        f"name='{name}', old_text=..., new_text=...) and run it again.")
    keep_report(ws, index, key, code, result, input)
    if result.artifact is not None:
        result.artifact["name"] = name
    return result


def around(code: str, line: int, width: int = AROUND) -> str:
    """The numbered lines around one line, that one marked."""
    lines = code.splitlines()
    first, last = max(1, line - width), min(len(lines), line + width)
    pad = len(str(last))
    return "\n".join(f"{'>>' if n == line else '  '} {n:>{pad}}| {lines[n - 1]}" for n in range(first, last + 1))


def keep_report(ws: Workspace, index: SearchIndex | None, key: str, code: str, result: ToolResult,
                typed: str | None, session: str | None = None) -> None:
    """The file's report after a run: what it says it does (its docstring's first line), then the facts - and, for a
    run in the console, the session as it went (what it printed, what the user typed), so the model can read it."""
    from .files import index_report
    res = result.result
    purpose = _docstring(code) or f"{ws.label(key)}"
    lines = []
    if result.status == "ok":
        lines.append(f"Last run: passed in {res.get('seconds')} s ({datetime.now():%Y-%m-%d %H:%M})")
    else:
        error = next((ln for ln in reversed(res.get("errors", "").splitlines()) if ln.strip()), "an error")
        lines.append(f"Last run: failed - {error.strip()[:200]} ({datetime.now():%Y-%m-%d %H:%M})")
    if typed:
        given = typed.splitlines()
        lines.append(f"Run with typed input: {len(given)} line(s) ({', '.join(given[:5])}{' ...' if len(given) > 5 else ''})")
    printed = " ".join(res.get("output", "").split())
    if printed and session is None:  # a session keeps all it printed, below
        lines.append("It printed: " + (printed if len(printed) <= PRINTED else printed[:PRINTED] + " ..."))
    made = [f["path"] for f in (result.artifact or {}).get("files", []) if f.get("id") and f["id"] != key]
    if made:
        lines.append("It made: " + ", ".join(made))
    report = f"# {purpose}\n\n## Facts (written by the app)\n" + "\n".join(f"- {ln}" for ln in lines) + "\n"
    if session is not None:
        report += f"\n## The user's session in the console (what it printed; their lines after ›)\n```\n{session}\n```\n"
    ws.set_report(key, report)
    index_report(ws, index, key)


def _docstring(code: str) -> str | None:
    """The first line of the module's docstring - the program's own word on what it does."""
    try:
        doc = ast.get_docstring(ast.parse(code))
    except SyntaxError:
        return None
    first = (doc or "").strip().splitlines()
    return first[0].strip().rstrip(".")[:150] if first else None
