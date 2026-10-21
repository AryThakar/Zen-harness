# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""The snapshot: what the files the user pinned to the chat, or pointed at with @, hold for the message being answered -
retrieved fresh each turn and attached to that turn only (docs/history/REBUILD_PLAN.md 2.2).

For each file: what it is, its coordinates - a document's outline (its headings with their pages), a program's
functions with their lines, a table's columns - and the parts that match the user's message: a document's passages
(page, section), a program's lines, a table's matching rows, a picture's caption. So the model talks about the file
with it in view, and knows where to read more (files read), instead of searching blind.

It is never stored: main.py adds it to the current turn's user message while the prompt is built, and the next turn
builds its own - the history keeps only what the user said and which files they meant (the user's design: like
thinking, gone after the turn). It costs the model server the cached prompt from that message on, once a turn - the
price of a view that follows the conversation. Within BUDGET characters (about 1,500 tokens), shared by the files.
"""

from __future__ import annotations

import re

from app.tools import filetypes
from app.tools.chart_image import is_chart
from app.tools.files import index_items
from app.tools.search_index import STOPWORDS, SearchIndex
from app.tools.workspace import Workspace

BUDGET = 6_000  # characters, about 1,500 tokens
PASSAGES = 3  # matching passages per document or program
ROWS = 5  # matching rows per table
OUTLINE = 12  # headings or functions listed
DEF = re.compile(r"^[ \t]*(?:async[ \t]+)?(?:def|class|function)[ \t]+(\w+)", re.M)
HEAD = ("[Pinned and pointed-at files - matched to this message by the app, for this turn only; read more of a file "
        "with files(mode='read'):")


def build(ws: Workspace, index: SearchIndex | None, pinned: list[str], pointed: list[str], message: str) -> str:
    """The snapshot for this message ('' when no file is pinned or pointed at)."""
    lines = ws.lines()
    keys = [k for k in dict.fromkeys([*pinned, *pointed]) if k in lines]
    if not keys:
        return ""
    if index is not None:
        index_items(ws, index)  # in step with the files as they are now (cheap when nothing changed)
    share = max(600, BUDGET // len(keys))
    parts = [HEAD]
    for key in keys:
        why = "pinned" if key in pinned else "with this message"
        path, about = ws.label(key), lines[key].split(": ", 1)[-1]
        body = _view(ws, index, key, message)
        block = f"- {path} ({why}) - {about}" + ("\n" + body if body else "")
        parts.append(block if len(block) <= share else block[:share].rsplit(" ", 1)[0] + " ...")
    return "\n".join(parts) + "]"


def _view(ws: Workspace, index: SearchIndex | None, key: str, message: str) -> str:
    kind, name = ws.kind(key), ws.label(key)
    if kind == "image":
        meta = ws.entry(key).get("meta") or {}
        said = meta.get("caption") or meta.get("prompt")
        return f"  Shows: {said}" if said else "  A picture: view_image looks at it."
    if kind == "table":
        return _table(ws, key, message)
    if is_chart(name):
        return ""  # its line says what it is; the page draws it
    if filetypes.is_document(name):
        return _passages(index, "document", key, message, _outline(ws, key))
    if filetypes.is_code(name):
        return _passages(index, "code", key, message, _functions(ws, key))
    return ""


def _outline(ws: Workspace, key: str) -> str:
    """A document's headings with the pages they start on (kept when it was indexed: files.outline)."""
    out = [f"{h} (p.{page})" if page else str(h) for h, page in (ws.entry(key).get("meta") or {}).get("outline") or []]
    shown = " · ".join(out[:OUTLINE]) + (f" · ... {len(out) - OUTLINE} more" if len(out) > OUTLINE else "")
    return f"  Outline: {shown}" if out else ""


def _functions(ws: Workspace, key: str) -> str:
    """A program's functions and classes with their lines."""
    try:
        text = ws.path(key).read_text("utf-8", errors="replace")
    except OSError:
        return ""
    found = [(m.group(1), text.count("\n", 0, m.start()) + 1) for m in DEF.finditer(text)]
    total = text.count("\n") + 1
    shown = ", ".join(f"{n} (line {at})" for n, at in found[:OUTLINE])
    return f"  {total} lines" + (f"; defines {shown}" if found else "")


def _passages(index: SearchIndex | None, source: str, key: str, message: str, head: str) -> str:
    """The passages of this file that match the message - or its start, when none does (a 'summarise this')."""
    if index is None:
        return head
    hits, _ = index.search(message, (source,), PASSAGES, refs=(key,))
    label = "Matching this message"
    if not hits:
        hits, label = index.first(source, key, 1), "Its start (nothing in it matched the message)"
    quoted = []
    for h in hits:
        where = ", ".join(x for x in (f"p.{h.page}" if h.page else "", f'"{h.section}"' if h.section else "") if x)
        text = " ".join(h.text.split())
        quoted.append(f"  - {where + ': ' if where else ''}{text[:900]}{' ...' if len(text) > 900 else ''}")
    return "\n".join(x for x in (head, f"  {label}:" if quoted else "", *quoted) if x)


def _table(ws: Workspace, key: str, message: str) -> str:
    """A table's rows that hold the message's words - or its first rows, when none does."""
    try:
        df = ws.table(key)
    except Exception:  # noqa: BLE001 - an unreadable table still gets its line
        return ""
    words = [w for w in re.findall(r"\w+", message.casefold()) if w not in STOPWORDS and len(w) > 2]
    text = df.select_dtypes(include="object")
    hit = None
    if words and not text.empty:
        joined = text.fillna("").astype(str).agg(" ".join, axis=1).str.casefold()
        hit = joined.apply(lambda row: any(w in row for w in words))
    if hit is not None and hit.any():
        rows, label = df[hit.values].head(ROWS), f"Rows matching this message ({min(int(hit.sum()), ROWS)} shown of {int(hit.sum()):,})"
    else:
        rows, label = df.head(3), f"Its first rows (3 of {len(df):,})"
    shown = [" | ".join(f"{c}={_cell(v)}" for c, v in r.items()) for _, r in rows.iterrows()]
    # Measured: shown 3 of 400 rows, main answered "132 passengers survived" (145) with no tool.
    return (f"  {label} - a sample to see what the table holds, not its numbers: any count, total or average of it "
            f"comes from analyze_or_code, which reads every row:\n" + "\n".join(f"  - {s}" for s in shown))


def _cell(value) -> str:
    text = str(value)
    return text if len(text) <= 40 else text[:40] + "..."
