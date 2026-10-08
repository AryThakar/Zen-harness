"""files(): main's one tool for the workspace's files - list, find, read, write, update, rename,
delete, download. One job per mode: write only makes new files, update only changes existing ones
(the whole text, or lines start-end), rename only renames.

Notes are not files (they are the AI's memory, notes.py) and never show here. An item is named by
its id ('file_3'), the name it was saved under ('parabola.py') or a copied listing line - the
workspace's one resolver (Workspace.resolve); a name that isn't there gets the closest items back as
'id (name)', so an invented name is corrected in one step. read gives an item's report first (what a
worker did with it) when it has one; with start, its content (start=0 reads from the first line) -
the one reader, read_file.read, which the helpers' read_file uses too.

The user decides (approval.py): delete and download always ask with a button, and so does
updating a file the user brought (an upload or a download) - there are no versions, so the
original would be lost. Files the assistant wrote are updated freely; renaming loses nothing and
never asks. A .docx is written from Markdown by code (headings, paragraphs, lists, tables, bold
and italic).
"""

from __future__ import annotations

import io
import re
import tempfile
from pathlib import Path, PurePath

from . import approval, read_file
from .core import ToolError, ToolResult, ok, tool
from .search_index import SearchIndex
from .workspace import Workspace

PAGE = 15  # items per list page
KINDS = ("table", "image", "file")  # everything but notes
MAX_WRITE = 1_000_000  # characters
MODES = ("list", "find", "read", "write", "update", "rename", "delete", "download")
# Formats that are not text: text written under such a name would be a broken file (a .docx is the
# exception - it is built from Markdown). A script makes them (write_code).
BINARY = {".pdf", ".xlsx", ".xlsm", ".xls", ".doc", ".pptx", ".ppt", ".odt", ".ods", ".png", ".jpg", ".jpeg", ".gif",
          ".webp", ".bmp", ".ico", ".zip", ".gz", ".7z", ".rar", ".parquet", ".joblib", ".pkl", ".exe", ".dll", ".bin",
          ".mp3", ".mp4", ".wav"}


@tool
def files(ws: Workspace, index: SearchIndex | None, mode: str, *, name: str | None = None, page: int = 1,
          start: int | None = None, end: int | None = None, content: str | None = None,
          new_name: str | None = None, link: str | None = None) -> ToolResult:
    mode = str(mode).strip().lower()
    if mode == "list":
        return _list(ws, page)
    if mode == "download":
        return _download(_need(link, "link", "download needs link: a GitHub file's link, or a data file's link"))
    if mode not in MODES:
        raise ToolError(f"mode must be one of {', '.join(MODES)}.", field="mode")
    name = _need(name, "name", f"{mode} needs name: a file's name or id")
    if mode == "find":
        return _find(ws, name)
    if mode == "read":
        return _read(ws, _resolve(ws, name), start)
    if mode == "write":
        return _write(ws, index, name, _need(content, "content", "write needs content: the file's text"))
    if mode == "update":
        return _update(ws, index, _resolve(ws, name),
                       _need(content, "content", "update needs content: the new text (of the whole file, or of "
                                                 "lines start-end)"), start, end)
    if mode == "rename":
        return _rename(ws, index, _resolve(ws, name), _need(new_name, "new_name", "rename needs new_name"))
    return approval.delete_request(ws, [_resolve(ws, name)])


# ---------------------------------------------------------------- list, find, read

def _list(ws: Workspace, page: int) -> ToolResult:
    lines = list(reversed(ws.lines().values()))  # newest first
    if not lines:
        return ok({"items": [], "note": "The workspace is empty: nothing uploaded, downloaded or made yet."})
    pages = (len(lines) + PAGE - 1) // PAGE
    if not 1 <= page <= pages:
        raise ToolError(f"There are {pages} page(s); page must be 1 to {pages}.", field="page")
    shown = lines[(page - 1) * PAGE:page * PAGE]
    more = f"more: files(mode='list', page={page + 1})" if page < pages else "end of list"
    return ok({"total": len(lines), "page": f"{page} of {pages}", "items": shown, "next": more})


def _find(ws: Workspace, name: str) -> ToolResult:
    item = _exact(ws, name)
    if item:
        return ok({"found": True, "item": ws.lines()[item]})
    try:
        _resolve(ws, name)
    except ToolError as missing:  # the closest items, as 'id (name)'
        return ok({"found": False, "note": missing.message})


def _read(ws: Workspace, item: str, start: int | None) -> ToolResult:
    """The one reader (read_file.read): the report alone first, then the text from start."""
    return read_file.read(ws, item, start, read_file.MAIN_CHARS, tool="files", report_alone=True)


# ---------------------------------------------------------------- write

def _write(ws: Workspace, index: SearchIndex | None, name: str, content: str) -> ToolResult:
    """A new file; an existing name is update's job."""
    _fits(content)
    existing = _exact(ws, name)  # an id or a stored name, as given - before a default suffix is added
    if existing is None:
        name = _file_name(name)
        existing = _exact(ws, name)
    if existing is not None:
        raise ToolError(f"A file called {ws.entry(existing)['name']} exists ({existing}): change it with "
                        "mode='update', or write under another name.", field="name")
    _writable(name)
    return _store_new(ws, index, name, content)


def _update(ws: Workspace, index: SearchIndex | None, item: str, content: str, start: int | None,
            end: int | None) -> ToolResult:
    """The whole text, or lines start-end (inclusive) replaced by content. The user's own files ask first."""
    e = ws.entry(item)
    if e["kind"] != "file":
        raise ToolError(f"{e['name']} is a {e['kind']}, which can't be changed as text: write a new file instead.",
                        field="name")
    _writable(e["name"])  # before the user is asked to approve it: a PDF written as text would be broken
    _fits(content)
    if start is not None or end is not None:
        if e["name"].lower().endswith(".docx"):
            raise ToolError("A .docx is updated whole: give its full text in Markdown, without start and end.",
                            field="start")
        lines = read_file.units(e["name"], ws.path(item).read_bytes(), numbered=False)
        start, end = start or 1, end or start or 1
        if not 1 <= start <= end <= len(lines):
            raise ToolError(f"{e['name']} has {len(lines)} lines; start and end must be within 1-{len(lines)}, "
                            "start first.", field="start")
        content = "\n".join(lines[:start - 1] + content.splitlines() + lines[end:]) + "\n"
    if made_by_assistant(e):
        return replace_file(ws, index, item, content)
    return approval.replace_request(ws, item, content)


def _rename(ws: Workspace, index: SearchIndex | None, item: str, new_name: str) -> ToolResult:
    from .files import index_report, reindex
    e = ws.entry(item)
    old = e["name"]
    new_name = " ".join(str(new_name).split())
    if not PurePath(new_name).suffix and PurePath(old).suffix:
        new_name += PurePath(old).suffix  # 'report' for 'notes.md' keeps .md
    _file_name(new_name)
    if new_name == old:  # measured: 'tts.rs -> tts.rs' was reported as a rename
        raise ToolError(f"{item} is already called {old}: nothing to rename.", field="new_name")
    if PurePath(new_name).suffix.lower() != PurePath(old).suffix.lower() and e["kind"] == "file":
        _writable(new_name)  # notes.md renamed notes.pdf would still be text: a broken PDF
    taken = _exact(ws, new_name)
    if taken and taken != item:
        raise ToolError(f"{new_name} is already taken ({taken}): pick another name.", field="new_name")
    ws.rename(item, new_name)
    if index is not None and "passages" in e.get("meta", {}):
        reindex(ws, index, item)  # its passages carry its name
    index_report(ws, index, item)
    return ok({"file": item, "renamed": f"{old} -> {new_name}"})


def _fits(content: str) -> None:
    if len(content) > MAX_WRITE:
        raise ToolError(f"content is longer than {MAX_WRITE:,} characters.", field="content")


def _store_new(ws: Workspace, index: SearchIndex | None, name: str, content: str) -> ToolResult:
    from .files import store_file
    data = _bytes(name, content)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / name
        path.write_bytes(data)
        kept = store_file(ws, index, path)
    item = kept.get("id") or kept.get("file")
    ws.set_meta(item, "by", "assistant")
    return ok({"file": item, "name": name, "kind": kept["kind"], "bytes": len(data),
               "note": "Saved; the user sees it in the chat."}, _card(ws, item, "written"))


def replace_file(ws: Workspace, index: SearchIndex | None, item: str, content: str) -> ToolResult:
    """Replace a stored file's content (and its search passages, for a document)."""
    from .files import reindex
    name = ws.entry(item)["name"]
    data = _bytes(name, content)
    ws.replace_bytes(item, data)
    if index is not None and "passages" in ws.entry(item).get("meta", {}):
        reindex(ws, index, item)
    return ok({"file": item, "name": name, "bytes": len(data), "note": "Updated; the user sees it in the chat."},
              _card(ws, item, "updated"))


def made_by_assistant(entry: dict) -> bool:
    meta = entry.get("meta", {})
    return meta.get("by") == "assistant" or meta.get("from") == "run_python"


def _card(ws: Workspace, item: str, action: str) -> dict:
    e = ws.entry(item)
    return {"type": "file", "id": item, "name": e["name"], "kind": e["kind"], "bytes": e.get("bytes"),
            "action": action}


def _file_name(name: str) -> str:
    name = " ".join(str(name).split())
    if not name or "/" in name or "\\" in name or name.startswith(".") or len(name) > 120:
        raise ToolError("name must be a plain file name (no folders), e.g. 'report.md'.", field="name")
    return name if PurePath(name).suffix else name + ".md"


def _writable(name: str) -> None:
    """Text can be written under this name: a text format, or .docx (built from Markdown)."""
    suffix = PurePath(name).suffix.lower()
    if suffix in BINARY:
        raise ToolError(f"A {suffix} file isn't text, so it can't be written or changed here - the text would be a "
                        f"broken {suffix} file. Write .md, .txt, .csv, code or .docx (from Markdown); a {suffix} file "
                        "is made by a script (write_code).", field="name")


def _bytes(name: str, content: str) -> bytes:
    _writable(name)
    return markdown_docx(content) if name.lower().endswith(".docx") else content.encode("utf-8")


# ---------------------------------------------------------------- download

def _download(link: str) -> ToolResult:
    """One GitHub file of any kind, or a data table from any public site - both ask the user first."""
    from .web import _on_github, _raw_link
    if _on_github(_raw_link(link.strip())):
        return approval.download_request(link)
    return approval.import_request(link)


# ---------------------------------------------------------------- names

def _need(value, field: str, message: str) -> str:
    if value is None or not str(value).strip():
        raise ToolError(message + ".", field=field)
    return str(value) if field == "content" else str(value).strip()


def _exact(ws: Workspace, name: str) -> str | None:
    """The item this names (id, name or a copied listing line), or None."""
    return ws.find(name, KINDS)


def _resolve(ws: Workspace, name: str) -> str:
    return ws.resolve(name, KINDS, field="name")


# ---------------------------------------------------------------- .docx from Markdown

INLINE = re.compile(r"(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`)")


def markdown_docx(text: str) -> bytes:
    """A Word document from Markdown: # headings, paragraphs, - and 1. lists, | tables |, **bold**,
    *italic* and `code`."""
    from docx import Document
    doc = Document()
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if line.lstrip().startswith("|"):
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    rows.append(cells)
                i += 1
            if rows:
                table = doc.add_table(rows=len(rows), cols=max(len(r) for r in rows))
                table.style = "Table Grid"
                for r, cells in enumerate(rows):
                    for c, cell in enumerate(cells):
                        _runs(table.cell(r, c).paragraphs[0], cell, bold=r == 0)
            continue
        heading = re.match(r"(#{1,4})\s+(.*)", line)
        bullet = re.match(r"\s*[-*+]\s+(.*)", line)
        number = re.match(r"\s*\d+[.)]\s+(.*)", line)
        if heading:
            doc.add_heading(heading[2].strip(), level=len(heading[1]))
        elif bullet:
            _runs(doc.add_paragraph(style="List Bullet"), bullet[1])
        elif number:
            _runs(doc.add_paragraph(style="List Number"), number[1])
        elif line.strip():
            _runs(doc.add_paragraph(), line.strip())
        i += 1
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def _runs(paragraph, text: str, bold: bool = False) -> None:
    for piece in INLINE.split(text):
        if not piece:
            continue
        if piece.startswith("**") and piece.endswith("**"):
            paragraph.add_run(piece[2:-2]).bold = True
        elif piece.startswith("*") and piece.endswith("*") and len(piece) > 2:
            paragraph.add_run(piece[1:-1]).italic = True
        elif piece.startswith("`") and piece.endswith("`"):
            paragraph.add_run(piece[1:-1]).font.name = "Consolas"
        else:
            paragraph.add_run(piece).bold = bold or None
