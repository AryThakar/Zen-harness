"""The one reader: the text of a stored file or note, from a line on, with its report - for main
(files(mode='read')) and the helpers (read_file) alike.

A read shows lines start.. up to a size budget (main 8,000 characters, a helper 12,000, so a helper
reads a long document in fewer steps) and says where to go on ('next'). PDF and Word text comes from
the same extractor as document search, with [page N] lines; code comes with line numbers, so changes
can point at lines (data and prose don't, since numbers there would be copied as if they were
content). A file's report - what a helper did with it - comes first: for main alone when it reads
without start (the report answers most questions), for a helper beside the first lines.

What isn't text says which tool reads it: an image -> view_image, a table -> its tools (a helper's
script: table('data_1')). search_files finds passages that match a question; this reads in order.
"""

from __future__ import annotations

from pathlib import Path

from app.rag.extract import DocumentError, extract, kind_of
from app.rag.extract import lines as document_lines

from .core import ToolError, ToolResult, ok, tool
from .filetypes import CODE
from .workspace import Workspace

MAIN_CHARS = 8_000
HELPER_CHARS = 12_000
# Line numbers help to talk about code (filetypes.CODE); in data or prose they would be copied as if they were
# content.
MAX_FILE = 10 * 1024 * 1024
KINDS = ("file", "note", "image", "table")


@tool
def read_file(ws: Workspace, file: str, start: int = 1) -> ToolResult:
    """A helper's read: the report (first read) and the text from line ``start``."""
    return read(ws, ws.resolve(file, KINDS, field="file"), start, HELPER_CHARS, tool="read_file")


def read(ws: Workspace, item: str, start: int | None, budget: int, tool: str = "files",
         report_alone: bool = False) -> ToolResult:
    """Read ``item`` from line ``start`` (None: from the top; with report_alone, the report only when there
    is one). ``tool`` is how the result says to go on."""
    e = ws.entry(item)
    kind, name = e["kind"], e["name"]
    report = ws.report(item) if not start or start <= 1 else None
    if report and report_alone and start is None:  # what a helper did with it: enough for most questions
        return ok({"file": item, "name": name, "report": report, "next": _call(tool, name, 1)})
    if kind != "file" and kind != "note":
        return ok({"file": item, "name": name, **_not_text(ws, item, e, tool)})
    start = max(1, start or 1)  # start=0: counting from zero means the first line
    path = ws.path(item)
    if path.stat().st_size > MAX_FILE:
        raise ToolError(f"{item} is larger than 10 MB.", invalid=False)
    numbered = kind == "file" and Path(name).suffix.lower() in CODE
    lines = units(name, path.read_bytes(), numbered=numbered)
    if not lines:
        return ok({"file": item, "name": name, "text": "", "note": "The file is empty."})
    if start > len(lines):
        raise ToolError(f"{name} has {len(lines)} lines; start must be 1 to {len(lines)}.", field="start")
    shown, size = [], 0
    for line in lines[start - 1:]:
        if shown and size + len(line) + 1 > budget:
            break
        shown.append(line if len(line) <= budget else line[:budget] + " ...")
        size += len(line) + 1
    end = start + len(shown) - 1
    result = {"file": item, "name": name, "lines": f"{start}-{end} of {len(lines)}", "text": "\n".join(shown),
              "next": _call(tool, name, end + 1) if end < len(lines) else "end of file"}
    if report:
        result = {"file": item, "name": name, "report": report, **{k: v for k, v in result.items()
                                                                     if k not in ("file", "name")}}
    return ok(result)


def _call(tool: str, name: str, start: int) -> str:
    return (f"files(mode='read', name='{name}', start={start})" if tool == "files"
            else f"read_file(file='{name}', start={start})")


def _not_text(ws: Workspace, item: str, e: dict, tool: str = "files") -> dict:
    """What an item without text is, and which tool reads it (main: view_image; a helper that sees images
    is shown it by its runner)."""
    meta = e.get("meta", {})
    if e["kind"] == "image":
        size = f" ({meta['width']}x{meta['height']})" if meta.get("width") else ""
        look = (f" view_image(image='{item}', question=...) looks at it and answers what it shows or says."
                if tool == "files" else "")
        return {"kind": "image", "note": f"An image{size}: it has no text to read.{look}"}
    if e["kind"] == "table":
        head = ws.table(item).head(10)
        return {"kind": "table", "rows": e["rows"], "columns": list(map(str, head.columns)),
                "first_rows": head.astype(object).where(head.notna(), None).values.tolist(),
                "note": f"A table: numbers, charts and models come from the data tools (main: analyze_or_code); a "
                        f"script reads it with table('{item}')."}
    return {"kind": e["kind"], **{k: v for k, v in meta.items() if isinstance(v, (str, int, float))}}


def units(name: str, data: bytes, numbered: bool) -> list[str]:
    """The lines of a file: pages and paragraphs of documents (with [page N] lines and their headings),
    lines of other text."""
    try:
        doc_kind = kind_of(name)
    except DocumentError:
        doc_kind = None
    if doc_kind in ("pdf", "docx", "pptx"):
        try:
            return document_lines(extract(name, data))
        except DocumentError as exc:
            raise ToolError(str(exc), invalid=False) from exc
    text = data.decode("utf-8", "replace") if not data.startswith(b"\xff\xfe") else data.decode("utf-16")
    if b"\x00" in data[:4000] and not data.startswith(b"\xff\xfe"):
        raise ToolError("This file is binary, not text.", invalid=False)
    lines = text.splitlines()
    if not numbered:
        return lines
    width = len(str(len(lines)))
    return [f"{i:>{width}}| {line}" for i, line in enumerate(lines, start=1)]
