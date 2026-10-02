"""recall: find what was said in earlier conversations.

After every finished turn the agent calls remember_turn (not a model tool). A turn is indexed as
one unit - the user's message, the tools the assistant called (with their arguments, cut short)
and the assistant's answer - so recall returns the question together with its answer, not a
floating fragment. Tool results are left out (large, and the numbers can be computed again), and
so are search_files / recall calls, so retrieved text never feeds back into memory. Nothing is
summarised by a model: what comes back is what was said. Idea from Unsloth Studio's conversation
archive (studio/backend/core/rag/conversation_archive.py), written independently.
"""

from __future__ import annotations

import json

from app.rag.chunking import _split_long

from .core import ToolError, ToolResult, ok, tool
from .search_index import Passage, SearchIndex

SOURCE = ("chat",)
MAX_ARGS = 300
MAX_TEXT = 1500
NOT_REMEMBERED = {"search_files", "recall"}


def remember_turn(index: SearchIndex, conversation: str, user: str, answer: str,
                  tool_calls: list[dict] | None = None, when: str | None = None) -> None:
    """Index one finished turn. tool_calls: [{'name': ..., 'arguments': {...}}]."""
    lines = [f"user: {user.strip()}"] if user.strip() else []
    for call in tool_calls or []:
        if call.get("name") in NOT_REMEMBERED:
            continue
        args = json.dumps(call.get("arguments") or {}, ensure_ascii=False)
        lines.append(f"assistant called {call.get('name')}: " + (args if len(args) <= MAX_ARGS else args[:MAX_ARGS] + " ..."))
    if answer.strip():
        lines.append(f"assistant: {answer.strip()}")
    text = "\n".join(lines)
    if not text:
        return
    # A long turn is split into ~250-word passages at sentence ends, each keeping the question on top.
    head = lines[0] if lines and lines[0].startswith("user:") and len(text.split()) > 300 else None
    parts = _split_long(text) if len(text.split()) > 300 else [text]
    passages = [Passage(p if head is None or p.startswith("user:") else f"{head[:300]}\n...\n{p}") for p in parts]
    index.add("chat", conversation, None, passages, meta=conversation, created=when)


@tool
def recall(index: SearchIndex, query: str, k: int = 4, exclude_conversation: str | None = None) -> ToolResult:
    """Find earlier conversation turns about the query."""
    query = " ".join(str(query).split())
    if len(query) < 2:
        raise ToolError("query is empty; say what to look for.", field="query")
    k = max(1, min(int(k), 8))
    hits, used_meaning = index.search(query, SOURCE, k + 4)
    hits = [h for h in hits if h.ref != exclude_conversation][:k]
    result = {"query": query, "search": "keywords and meaning" if used_meaning else
              "keywords only (the embedding model is unavailable)"}
    if not hits:
        result["turns"] = []
        result["note"] = "No earlier conversation matched. Say so; don't guess what was said."
        return ok(result)
    result["turns"] = [{"when": h.created[:16].replace("T", " "), "conversation": h.ref,
                        "text": h.text if len(h.text) <= MAX_TEXT else h.text[:MAX_TEXT] + " ..."} for h in hits]
    result["note"] = "These are earlier turns as they were said; mention when, and that things may have changed since."
    return ok(result, {"type": "sources", "query": query,
                       "items": [{"source": f"conversation {h.ref}, {h.created[:10]}", "text": h.text} for h in hits]})
