"""read_file: the text of a stored file or note, from start to end, in parts.

search_files finds passages that match a question; read_file is for reading a whole document
("summarise this PDF") or a script ("fix convert.py"). Text comes in parts of about 12,000
characters, cut at page or line boundaries; the result says which part this is and how many
there are. PDF and Word text comes from the same extractor as document search, with page
numbers; code files come with line numbers, so changes can point at lines (data and prose don't,
since numbers there would be copied as if they were content).
"""

from __future__ import annotations

from pathlib import Path

from app.rag.extract import DocumentError, extract, kind_of

from .core import ToolError, ToolResult, ok, tool
from .workspace import Workspace

PART_CHARS = 12_000
# Line numbers help to talk about code; in data or prose they would be copied as if they were content.
CODE = {".py", ".js", ".ts", ".java", ".c", ".h", ".cpp", ".cs", ".go", ".rs", ".rb", ".php", ".sh", ".ps1",
        ".sql", ".r", ".html", ".css", ".kt", ".swift", ".scala", ".m", ".pl", ".lua"}
MAX_FILE = 10 * 1024 * 1024


@tool
def read_file(ws: Workspace, file: str, part: int = 1) -> ToolResult:
    """Read part ``part`` (from 1) of a stored file or note."""
    kind = ws.kind(file)
    if kind == "table":
        raise ToolError(f"{file} is a table: use inspect_data or query_data to read it.", field="file")
    if kind not in ("file", "note"):
        raise ToolError(f"{file} is a {kind}, which has no text to read.", field="file")
    path = ws.path(file)
    if path.stat().st_size > MAX_FILE:
        raise ToolError(f"{file} is larger than 10 MB.", invalid=False)
    name = ws.entry(file)["name"]
    units = _units(name, path.read_bytes(), numbered=kind == "file" and Path(name).suffix.lower() in CODE)
    parts = _pack(units)
    if not 1 <= part <= len(parts):
        raise ToolError(f"{file} has {len(parts)} part(s); part must be 1 to {len(parts)}.", field="part")
    result = {"file": file, "name": ws.entry(file)["name"], "part": part, "parts": len(parts), "text": parts[part - 1]}
    if part < len(parts):
        result["note"] = f"There is more: read part {part + 1} of {len(parts)} if the task needs it."
    return ok(result)


def _units(name: str, data: bytes, numbered: bool) -> list[str]:
    """Pieces that are never split: pages / paragraphs of documents, lines of other text."""
    try:
        doc_kind = kind_of(name)
    except DocumentError:
        doc_kind = None
    if doc_kind in ("pdf", "docx"):
        try:
            doc = extract(name, data)
        except DocumentError as exc:
            raise ToolError(str(exc), invalid=False) from exc
        units, page = [], None
        for b in doc.blocks:
            if b.page != page and b.page is not None:
                page = b.page
                units.append(f"[page {page}]")
            units.append(b.text)
        return units
    text = data.decode("utf-8", "replace") if not data.startswith(b"\xff\xfe") else data.decode("utf-16")
    if b"\x00" in data[:4000] and not data.startswith(b"\xff\xfe"):
        raise ToolError("This file is binary, not text.", invalid=False)
    lines = text.splitlines()
    if not numbered:
        return lines
    width = len(str(len(lines)))
    return [f"{i:>{width}}| {line}" for i, line in enumerate(lines, start=1)]


def _pack(units: list[str]) -> list[str]:
    parts, current, size = [], [], 0
    for u in units:
        while len(u) > PART_CHARS:  # a single huge line or paragraph
            if current:
                parts.append("\n".join(current))
                current, size = [], 0
            parts.append(u[:PART_CHARS])
            u = u[PART_CHARS:]
        if size + len(u) + 1 > PART_CHARS and current:
            parts.append("\n".join(current))
            current, size = [], 0
        current.append(u)
        size += len(u) + 1
    if current:
        parts.append("\n".join(current))
    return parts or [""]
