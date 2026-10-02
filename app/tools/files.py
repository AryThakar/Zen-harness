"""search_files: answer from the user's documents and saved notes, with the source of every passage.

Documents enter through add_document (called by the upload API, not by the model): the text is
extracted (PDF, Word, .txt, .md - claude-cloud's tested extractor), split into ~250-word passages
that never cross a page or heading, and indexed. Notes are indexed by save_note. Scanned PDFs
(images of pages) have no text and are refused with that reason.
"""

from __future__ import annotations

from pathlib import Path

from app.rag.chunking import chunk
from app.rag.extract import DocumentError, extract

from .core import ToolError, ToolResult, ok, tool
from .search_index import Passage, SearchIndex
from .workspace import Workspace

MAX_PASSAGE_CHARS = 1500
SOURCES = ("document", "note")


def add_document(ws: Workspace, index: SearchIndex, path: str | Path) -> dict:
    """Store and index an uploaded document; returns its id and size. Raises ToolError if unusable."""
    path = Path(path)
    data = path.read_bytes()
    try:
        doc = extract(path.name, data)
    except DocumentError as exc:
        raise ToolError(str(exc), invalid=False) from exc
    passages = [Passage(c.text, c.page, c.section) for c in chunk(doc.blocks)]
    doc_id = ws.add_bytes("file", data, path.suffix.lstrip(".") or "bin", name=path.name,
                          meta={"title": doc.title, "kind": doc.kind, "pages": doc.pages, "words": doc.words,
                                "passages": len(passages)})
    embedded = index.add("document", doc_id, path.name, passages)
    return {"file": doc_id, "name": path.name, "pages": doc.pages, "passages": len(passages),
            **({} if embedded else {"note": "Indexed for keyword search only (the embedding model is unavailable)."})}


@tool
def search_files(index: SearchIndex, query: str, k: int = 5) -> ToolResult:
    """Find the passages in the user's documents and notes that best match the query."""
    query = " ".join(str(query).split())
    if len(query) < 2:
        raise ToolError("query is empty; say what to look for.", field="query")
    k = max(1, min(int(k), 10))
    hits, used_meaning = index.search(query, SOURCES, k)
    result = {"query": query, "search": "keywords and meaning" if used_meaning else
              "keywords only (the embedding model is unavailable)"}
    if not hits:
        result["passages"] = []
        result["note"] = "Nothing matched. Say the documents don't cover this rather than answering from memory."
        return ok(result)
    result["passages"] = [{"source": _source(h), "text": _cut(h.text)} for h in hits]
    result["note"] = "Answer only from these passages and name the source of each fact."
    return ok(result, {"type": "sources", "query": query,
                       "items": [{"source": _source(h), "ref": h.ref, "page": h.page, "text": h.text} for h in hits]})


def _source(h) -> str:
    if h.source == "note":
        return f"note '{h.title}' ({h.ref})"
    where = f", page {h.page}" if h.page else ""
    section = f' - "{h.section}"' if h.section else ""
    return f"{h.title}{where}{section}"


def _cut(text: str) -> str:
    return text if len(text) <= MAX_PASSAGE_CHARS else text[:MAX_PASSAGE_CHARS].rsplit(" ", 1)[0] + " ..."
