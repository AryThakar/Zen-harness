"""run_code / fix_code / save: a helper's one scratch file, for the length of one task.

run_code writes the whole file and runs it; fix_code changes lines from_line-to_line and runs it
again, so fixing one line of a long script doesn't mean sending the whole script again (the small
model's copies drift). Every run goes through run_python (sandbox.py: the same limits, libraries and
off-limits rules). When a run fails, the result shows the numbered lines around the line the error
points at, so the fix lands on the right lines. The file is thrown away with the task: the helper
stays stateless, and what it made is kept only when it saves it.

save (the coder's finish) keeps the scratch file in the workspace under a name, with its report -
the knowledge main answers from instead of the code (docs/ASSISTANT_PLAN.md, section 5). The report
is the coder's sections plus facts the app writes itself (whether the last run passed, what it
printed, what it made), so it can't claim a run that failed. Python is always run before it is
saved; a file in another language is kept as written ("not run: only Python runs here"). Saving
over a file the user brought asks them first; over the assistant's own file, it just replaces it.
"""

from __future__ import annotations

import ast
import builtins
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePath

from .core import ToolResult, ok
from .reports import CODE_SECTIONS, assemble, section_problems
from .sandbox import DEFAULT_TIMEOUT, MAX_CODE, run_python
from .search_index import SearchIndex
from .workspace import Workspace

AROUND = 3  # lines shown on each side of the failing one
ERROR_LINE = re.compile(r'File "script\.py", line (\d+)')
SECTIONS = CODE_SECTIONS
PRINTED = 400  # characters of the last run's output kept in the report


@dataclass
class Scratch:
    """The helper's scratch file during one task; name/item when the task gave a file to work on."""
    code: str = ""
    name: str | None = None
    item: str | None = None
    last: dict | None = None  # the last run: code, ok, seconds, output, errors, files
    made: list[str] = field(default_factory=list)
    can_look: bool = False  # the helper has view_image: a run that draws a picture points to it
    images: list[str] = field(default_factory=list)  # images the runs made (the coder looks before it saves)

    @property
    def runs(self) -> bool:
        """Only Python runs here."""
        return self.name is None or self.name.lower().endswith(".py")


def run_code(ws: Workspace, scratch: Scratch, code: str, timeout: int = DEFAULT_TIMEOUT) -> ToolResult:
    """Write the whole scratch file and run it."""
    code = str(code)
    if not code.strip():
        return _invalid("code is empty: write the whole script.", "code")
    scratch.code = code  # exactly as written: the code that is saved is the code that ran
    return _run(ws, scratch, timeout)


def fix_code(ws: Workspace, scratch: Scratch, from_line: int, to_line: int, new_code: str,
             timeout: int = DEFAULT_TIMEOUT) -> ToolResult:
    """Replace lines from_line-to_line (inclusive) of the scratch file with new_code, and run it.
    to_line = from_line - 1 inserts before from_line without removing anything."""
    if not scratch.code:
        return _invalid("There is no code yet: write the whole script with run_code first.", "from_line")
    lines = scratch.code.splitlines()
    if not (1 <= from_line <= len(lines) + 1 and from_line - 1 <= to_line <= len(lines)):
        return _invalid(f"The script has {len(lines)} lines: from_line must be 1-{len(lines) + 1} and to_line "
                        f"from_line-1 (to insert) to {len(lines)}.", "from_line")
    new = lines[:from_line - 1] + str(new_code).splitlines() + lines[to_line:]
    if len("\n".join(new)) > MAX_CODE:
        return _invalid(f"The script would be longer than {MAX_CODE:,} characters.", "new_code")
    scratch.code = "\n".join(new) + ("\n" if scratch.code.endswith("\n") else "")
    return _run(ws, scratch, timeout)


def _run(ws: Workspace, scratch: Scratch, timeout: int = DEFAULT_TIMEOUT) -> ToolResult:
    if not scratch.runs:
        return ok({"lines": len(scratch.code.splitlines()),
                   "note": f"{scratch.name} isn't Python: kept in your scratch file, not run (only Python runs here)."})
    result = run_python(ws, scratch.code, timeout)
    res = result.result
    scratch.last = {"code": scratch.code, "ok": result.status == "ok", "seconds": res.get("seconds"),
                    "output": res.get("output", ""), "errors": res.get("errors", ""),
                    "files": [f["id"] for f in res.get("files", []) if f.get("id")]}
    scratch.made += scratch.last["files"]
    res["lines"] = len(scratch.code.splitlines())
    drawn = [i for i in scratch.last["files"] if ws.kind(i) == "image"]
    scratch.images += drawn
    if drawn and scratch.can_look:  # the model can't see the picture in the result: it looks before it describes
        res["next"] = (f"It made {', '.join(drawn)}: look with view_image(image='{drawn[0]}', question=...) that it "
                       "shows what the task asked before you describe or save it.")
    found = ERROR_LINE.findall(res.get("errors", "")) if result.status != "ok" else []
    if found:  # the script's own line the error ends in: the last of its frames
        at = int(found[-1])
        res["at_line"] = at
        res["around"] = around(scratch.code, at)
        res["error"]["message"] += (f" It points at line {at}: change those lines with fix_code(from_line, "
                                    "to_line, new_code), or rewrite the script with run_code.")
    return result


def around(code: str, line: int, width: int = AROUND) -> str:
    """The numbered lines around one line, that one marked."""
    lines = code.splitlines()
    first, last = max(1, line - width), min(len(lines), line + width)
    pad = len(str(last))
    return "\n".join(f"{'>>' if n == line else '  '} {n:>{pad}}| {lines[n - 1]}" for n in range(first, last + 1))


# ---------------------------------------------------------------- save

def report_problems(sections: dict, code: str, python: bool = True) -> list[str]:
    """What is wrong with a report before it is saved: missing sections, too long, functions named
    (in backticks, as `name()`) that a Python script doesn't define or import."""
    problems = section_problems(SECTIONS, sections)
    defined = _defined(code) if python else None
    if defined is not None:
        named = set(re.findall(r"`([A-Za-z_]\w*)\(\)`", str(sections.get("how_it_works") or "")))
        unknown = sorted(named - defined - set(dir(builtins)))
        if unknown:
            problems.append(f"'How it works' names {', '.join(unknown)}, which the code doesn't define or import")
    return problems


def _defined(code: str) -> set[str] | None:
    """Names a Python script defines or imports (None when it doesn't parse)."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
    return names


def save(ws: Workspace, index: SearchIndex | None, scratch: Scratch, name: str, sections: dict,
         code: str | None = None) -> ToolResult:
    """Keep the scratch file under ``name`` with its report; returns the file's card, or an approval
    question when the name is a file the user brought."""
    from . import approval, file_ops
    if code is not None and str(code).strip():
        scratch.code = str(code)
    if not scratch.code.strip():
        return _invalid("There is nothing to save: write the file with run_code first.", "name")
    name = " ".join(str(name or scratch.name or "").split()) or "script.py"
    if not PurePath(name).suffix:
        name += ".py"
    file_ops._file_name(name)  # a plain file name, no folders
    scratch.name = name
    if scratch.runs and (scratch.last is None or scratch.last["code"] != scratch.code):
        _run(ws, scratch)  # what is saved has run
    existing = file_ops._exact(ws, name)
    if existing and ws.kind(existing) != "file":
        return _invalid(f"{name} is a {ws.kind(existing)}: save the code under another name.", "name")
    outputs = list(scratch.last["files"]) if scratch.runs and scratch.last else []
    item = existing or "(new)"
    report = assemble(SECTIONS, sections, facts(ws, scratch, name, item, outputs))
    if existing and not file_ops.made_by_assistant(ws.entry(existing)):
        return approval.replace_request(ws, existing, scratch.code, report=report, outputs=outputs)
    if existing:
        file_ops.replace_file(ws, index, existing, scratch.code)
    else:
        existing = file_ops._store_new(ws, index, name, scratch.code).result["file"]
        report = report.replace(f"({item})", f"({existing})", 1)
    return keep(ws, index, existing, report, outputs)


def keep(ws: Workspace, index: SearchIndex | None, item: str, report: str, outputs: list[str]) -> ToolResult:
    """The saved file's report and outputs (also after the user approved replacing their file)."""
    from .files import index_report
    ws.set_report(item, report)
    index_report(ws, index, item)
    for out in outputs:
        if out in ws.ids():
            ws.set_parent(out, item)
    ws.set_meta(item, "outputs", [o for o in outputs if o in ws.ids()])
    e = ws.entry(item)
    return ok({"file": item, "name": e["name"], "report": report},
              {"type": "file", "id": item, "name": e["name"], "kind": e["kind"], "bytes": e.get("bytes"),
               "action": "saved", "report": report, "outputs": [o for o in outputs if o in ws.ids()]})


def facts(ws: Workspace, scratch: Scratch, name: str, item: str, outputs: list[str]) -> list[str]:
    """What the app knows about the saved file - written by code, so the report can't claim otherwise."""
    lines = [f"File: {name} ({item}), saved {datetime.now():%Y-%m-%d %H:%M}"]
    last = scratch.last if scratch.runs else None
    if not scratch.runs:
        lines.append("Last run: not run (only Python runs here)")
    elif last and last["ok"]:
        lines.append(f"Last run: passed in {last['seconds']} s")
    else:
        error = next((ln for ln in reversed((last or {}).get("errors", "").splitlines()) if ln.strip()), "an error")
        lines.append(f"Last run: failed - {error.strip()[:200]}")
    printed = " ".join((last or {}).get("output", "").split())
    if printed:
        lines.append("It printed: " + (printed if len(printed) <= PRINTED else printed[:PRINTED] + " ..."))
    if outputs:
        lines.append("It made: " + ", ".join(f"{o} ({ws.entry(o)['name']})" for o in outputs if o in ws.ids()))
    return lines


def _invalid(message: str, field: str) -> ToolResult:
    return ToolResult("invalid_arguments", {"error": {"message": message, "field": field}})
