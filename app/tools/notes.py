"""save_note: the AI's short memory - a decision, constraint, conclusion or fact worth keeping.

Saving again under the same title updates that note (no 'Summary', 'Summary (2)' ...), and the model
mostly reuses titles, so notes update in place. Notes are not workspace files (files() doesn't show
them); older ones are found with search_files.

The 5 latest notes (by last save) are pinned automatically: always in main's view, and shown live in
the UI, so the user sees what the AI keeps in mind (the user's design: no approval step).
Pruning is by code, when a note is saved (the user's rule: notes grow, never pile up): notes not
saved for a year go, and when the notes pass a limit on count or total length the oldest go until
20% of the limit is free.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from .core import ToolError, ToolResult, ok, tool
from .search_index import Passage, SearchIndex
from .workspace import Workspace

MAX_NOTE = 2_000  # characters: notes are a few lines
PINNED = 5  # latest notes always in view
PINNED_CHARS = 300  # of each pinned note shown every turn
MAX_NOTES = 100
MAX_NOTES_CHARS = 50_000
MAX_AGE = timedelta(days=365)
FREE = 0.2  # share of the limit freed when it is passed


@tool
def save_note(ws: Workspace, title: str, content: str, *, index: SearchIndex | None = None,
              now: datetime | None = None) -> ToolResult:
    """Save a short markdown note (same title = update); the 5 latest are kept in view."""
    title = " ".join(str(title).split())
    content = str(content).strip()
    if not title or len(title) > 120:
        raise ToolError("title must be 1 to 120 characters.", field="title")
    if not content:
        raise ToolError("content is empty.", field="content")
    if len(content) > MAX_NOTE:
        raise ToolError(f"A note is a few lines (at most {MAX_NOTE:,} characters); keep only the gist.",
                        field="content")
    now = now or datetime.now()
    existing = _by_title(ws, title)
    text = f"# {title}\n\n{content}\n".encode("utf-8")
    saved = now.isoformat(timespec="microseconds")  # orders notes saved within one second
    if existing:
        ws.replace_bytes(existing, text, {**ws.entry(existing)["meta"], "title": title, "saved": saved})
        note_id, action = existing, "updated"
    else:
        note_id = ws.add_bytes("note", text, "md", name=title, meta={"title": title, "saved": saved})
        action = "saved"
    if index is not None:
        index.replace("note", note_id, title, [Passage(content)])
    pruned = prune(ws, index, now=now)
    result = {"note": note_id, "title": title, "action": action,
              "kept_in_view": "yes: one of the 5 latest notes, shown with every message"}
    if pruned:
        result["pruned"] = f"{len(pruned)} oldest note(s) removed to make room"
    return ok(result, {"type": "note", "id": note_id, "title": title, "action": action})


def prune(ws: Workspace, index: SearchIndex | None = None, *, now: datetime | None = None) -> list[str]:
    """Remove notes past the limits, oldest first; returns their ids."""
    now = now or datetime.now()
    notes = sorted(ws.ids("note"), key=lambda i: saved_at(ws, i))  # oldest first
    gone = [i for i in notes if now - saved_at(ws, i) > MAX_AGE]
    kept = [i for i in notes if i not in gone]
    sizes = {i: len(body(ws, i)) for i in kept}
    if len(kept) > MAX_NOTES or sum(sizes.values()) > MAX_NOTES_CHARS:
        while kept and (len(kept) > MAX_NOTES * (1 - FREE) or sum(sizes[i] for i in kept) > MAX_NOTES_CHARS * (1 - FREE)):
            gone.append(kept.pop(0))
    for i in gone:
        ws.delete(i)
        if index is not None:
            index.remove("note", i)
    return gone


def pinned(ws: Workspace) -> list[dict]:
    """The 5 latest notes, oldest first: {id, title, saved, text (cut to PINNED_CHARS)}."""
    latest = sorted(ws.ids("note"), key=lambda i: saved_at(ws, i))[-PINNED:]
    out = []
    for i in latest:
        text = " ".join(body(ws, i).split())
        out.append({"id": i, "title": ws.entry(i)["meta"]["title"], "saved": saved_at(ws, i).strftime("%d %b %Y"),
                    "text": text if len(text) <= PINNED_CHARS else text[:PINNED_CHARS].rstrip() + " ..."})
    return out


def pinned_text(ws: Workspace) -> str:
    """The pinned notes as main and the workers see them ('' when there are none)."""
    notes = pinned(ws)
    if not notes:
        return ""
    # Whose words: a note saying "My name is Zen. The user's name is Ary." was read as the user's own words.
    lines = [f"- {source(n)}, saved {n['saved']}: {n['text']}" for n in notes]
    return ("Pinned notes (your 5 latest notes, oldest first - a later one supersedes an earlier one; 'I' in a "
            "note means you, the assistant):\n" + "\n".join(lines))


def source(note: dict) -> str:
    """How a note is cited - the same as search_files names it."""
    return f"note '{note['title']}' ({note['id']})"


def saved_at(ws: Workspace, note_id: str) -> datetime:
    e = ws.entry(note_id)
    return datetime.fromisoformat(e["meta"].get("saved") or e.get("updated") or e["created"])


def body(ws: Workspace, note_id: str) -> str:
    text = ws.path(note_id).read_text("utf-8")
    return text.split("\n\n", 1)[1].strip() if "\n\n" in text else text.strip()


def _by_title(ws: Workspace, title: str) -> str | None:
    key = _key(title)
    return next((i for i in ws.ids("note") if _key(ws.entry(i)["meta"]["title"]) == key), None)


def _key(title: str) -> str:
    return re.sub(r"\W+", " ", title.casefold()).strip()
