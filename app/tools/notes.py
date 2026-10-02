"""save_note: keep a decision, constraint, conclusion or summary as a markdown note.

Saving again under the same title updates that note (no 'Summary', 'Summary (2)' ...). Notes are
found later with search_files. pin=true asks for the user's approval first (the UI shows it); a
pinned note is put in front of the model at the start of every turn, so decisions made early in
a long task stay in view. Pinned notes share a size limit, because they cost context every turn.

The model can request a pin but never grant one: set_pin is called by the server when the user
approves, and is not a tool.
"""

from __future__ import annotations

import re
from datetime import datetime

from app.rag.chunking import _split_long

from .core import ToolError, ToolResult, ok, tool
from .search_index import Passage, SearchIndex
from .workspace import Workspace

MAX_NOTE = 20_000  # characters
MAX_PINNED = 6_000  # characters across all pinned notes (about 1,500 words)


@tool
def save_note(ws: Workspace, title: str, content: str, pin: bool = False, *,
              index: SearchIndex | None = None) -> ToolResult:
    """Save a markdown note (same title = update). pin=true asks the user to pin it."""
    title = " ".join(str(title).split())
    content = str(content).strip()
    if not title or len(title) > 120:
        raise ToolError("title must be 1 to 120 characters.", field="title")
    if not content:
        raise ToolError("content is empty.", field="content")
    if len(content) > MAX_NOTE:
        raise ToolError(f"content is longer than {MAX_NOTE:,} characters; save a shorter summary.", field="content")
    existing = _by_title(ws, title)
    text = f"# {title}\n\n{content}\n".encode("utf-8")
    if existing:
        meta = ws.entry(existing)["meta"]
        ws.replace_bytes(existing, text, {**meta, "title": title})
        note_id, action = existing, "updated"
    else:
        note_id = ws.add_bytes("note", text, "md", name=title, meta={"title": title, "pinned": False})
        action = "saved"
    if index is not None:
        parts = _split_long(content) if len(content.split()) > 300 else [content]
        index.replace("note", note_id, title, [Passage(p) for p in parts])
    result = {"note": note_id, "title": title, "action": action}
    artifact = {"type": "note", "id": note_id, "title": title, "action": action}
    if pin and not ws.entry(note_id)["meta"].get("pinned"):
        _fits(ws, note_id, len(content))
        result["pin"] = "waiting for the user's approval"
        artifact["pin_request"] = True
    elif ws.entry(note_id)["meta"].get("pinned"):
        result["pin"] = "pinned"
    return ok(result, artifact)


def set_pin(ws: Workspace, note_id: str, pinned: bool) -> None:
    """Called by the server when the user approves (or removes) a pin - not a model tool."""
    if ws.kind(note_id) != "note":
        raise ToolError(f"'{note_id}' is not a note.")
    if pinned:
        _fits(ws, note_id, len(_body(ws, note_id)))
    e = ws.entry(note_id)
    ws.replace_bytes(note_id, ws.path(note_id).read_bytes(),
                     {**e["meta"], "pinned": pinned, **({"pinned_at": datetime.now().isoformat(timespec="seconds")}
                                                         if pinned else {})})


def pinned_text(ws: Workspace) -> str:
    """The pinned notes, for the start of every turn ('' when there are none)."""
    notes = [i for i in ws.ids("note") if ws.entry(i)["meta"].get("pinned")]
    if not notes:
        return ""
    parts = [f"## {ws.entry(i)['meta']['title']} ({i})\n{_body(ws, i)}" for i in notes]
    return "Pinned notes (decisions the user approved; follow them):\n\n" + "\n\n".join(parts)


def _by_title(ws: Workspace, title: str) -> str | None:
    key = _key(title)
    return next((i for i in ws.ids("note") if _key(ws.entry(i)["meta"]["title"]) == key), None)


def _key(title: str) -> str:
    return re.sub(r"\W+", " ", title.casefold()).strip()


def _body(ws: Workspace, note_id: str) -> str:
    text = ws.path(note_id).read_text("utf-8")
    return text.split("\n\n", 1)[1].strip() if "\n\n" in text else text.strip()


def _fits(ws: Workspace, note_id: str, size: int) -> None:
    used = sum(len(_body(ws, i)) for i in ws.ids("note") if i != note_id and ws.entry(i)["meta"].get("pinned"))
    if used + size > MAX_PINNED:
        raise ToolError(f"Pinned notes are limited to {MAX_PINNED:,} characters together ({used:,} used); shorten "
                        "this note or ask the user to unpin another.", field="pin")
