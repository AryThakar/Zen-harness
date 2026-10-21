# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""agent.md: the project's standing memory - one short Markdown file at the top of the workspace, always in view of
main and every helper: the user's standing instructions, their preferences and facts about them and their work.

It is a file like any other (one path, versions, the file panel), with three rules of its own:
- the model changes it freely (files write or edit) when the user asks it to remember something or says how things
  should always be done - saying "I'll remember" keeps nothing; the user edits it in the file panel;
- it is capped at MAX_CHARS: it is sent with every request, so a memory that grew without bound would crowd out the
  conversation; longer material goes in notes/ as ordinary files, which find searches;
- it can't be deleted or renamed, and scripts can't change it (sandbox: protected, like the user's files).

How it reaches the model: main's system prompt carries it, after the fixed prompt (main.py) - every turn, as it is
now, never stored in the history and never cut with old turns; a helper's task opens with it (context_block). The
model server reprocesses the prompt only when it changes, which is rare - attached to each turn's message instead,
it would break the cached prompt every turn.
Replaces notes (a separate kind and tool, the 5 latest pinned by date: a standing preference fell out of view after 5
newer notes - docs/history/REBUILD_PLAN.md, 2.2).
"""

from __future__ import annotations

from .core import ToolError
from .workspace import AGENT_FILE, Workspace

MAX_CHARS = 2_000
HEADER = ("# agent.md\n\nWhat I keep in mind in every chat of this project: your standing instructions, your preferences "
          "and facts about you and your work. You and I both edit it; it holds up to 2,000 characters.\n")


def key(ws: Workspace) -> str | None:
    return ws.find_path(AGENT_FILE)


def ensure(ws: Workspace) -> str:
    """agent.md, made with its header when the workspace has none yet; its key."""
    found = key(ws)
    if found:
        return found
    return ws.put(AGENT_FILE, HEADER.encode("utf-8"), kind="file", by="assistant").key


def text(ws: Workspace) -> str:
    """Its whole text ('' when there is none)."""
    found = key(ws)
    return ws.path(found).read_text("utf-8", errors="replace") if found else ""


def body(ws: Workspace) -> str:
    """What is remembered: its text without the header it was made with."""
    kept = text(ws)
    return (kept[len(HEADER):] if kept.startswith(HEADER) else kept).strip()


def in_view(ws: Workspace, keeper: bool = True) -> str:
    """What the model reads: agent.md's whole text, as it is in the file - so an edit quotes it exactly (measured on
    the 26B: shown only what was remembered, its first edit guessed the header's words and failed) - and, for main
    (``keeper``), how to change it. A helper reads it as the user's standing notes: told to write it, the web helper
    was told to call a tool it doesn't have."""
    if key(ws) is None:
        return ""
    held = text(ws).strip() + ("\n(nothing remembered yet)" if not body(ws) else "")
    if not keeper:
        return f"[agent.md - the user's standing notes for this project (main keeps them); follow what they say:\n{held}]"
    return ("[agent.md - your standing memory for this project, in view in every chat; to remember something, or when "
            "the user says how things should always be done, call files(mode='write', name='agent.md', content=...) "
            "with content = the text below plus your line. Its whole text as it is now - no need to read it:"
            f"\n{held}]")


def is_agent(name: str) -> bool:
    return str(name).replace("\\", "/").strip("/").casefold() == AGENT_FILE


def check(name: str, content: str) -> None:
    """agent.md's new text must fit its cap; a ToolError says by how much it doesn't."""
    if is_agent(name) and len(content) > MAX_CHARS:
        raise ToolError(f"agent.md holds at most {MAX_CHARS:,} characters (this would be {len(content):,}): keep it to "
                        "standing instructions and short facts - merge or drop lines; longer material goes in a file "
                        "under notes/.", field="content")


def refuse_move(name: str) -> None:
    """agent.md stays: it can't be deleted or renamed."""
    if is_agent(name):
        raise ToolError("agent.md is this project's memory: it can't be deleted or renamed. Change its text with write "
                        "or edit - or write it back to just its header to empty it.", field="name")
