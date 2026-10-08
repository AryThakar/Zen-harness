"""search_files: answer from the user's documents and saved notes, with the source of every passage.

Documents enter through add_document (from store_file: an upload, or a file downloaded from GitHub
with the user's approval - never by the model directly): the text is
extracted (PDF, Word, .txt, .md - claude-cloud's tested extractor), split into ~250-word passages
that never cross a page or heading, and indexed. Notes are indexed when they are saved (notes.py). Scanned PDFs
(images of pages) have no text and are refused with that reason.
"""

from __future__ import annotations

from pathlib import Path

from app.rag.chunking import _split_long, chunk
from app.rag.extract import KINDS as DOCUMENT_KINDS
from app.rag.extract import DocumentError, extract

from .core import ToolError, ToolResult, ok, tool
from .search_index import Passage, SearchIndex
from .workspace import IMAGE_SUFFIXES, Workspace

MAX_PASSAGE_CHARS = 1500
SOURCES = ("document", "report")  # ranked together; notes are searched apart (matching_notes)
NOTES_SHOWN = 2
LATER_SUPERSEDES = "oldest first: where they differ, a later one supersedes an earlier one"
TABLES = {".csv", ".tsv", ".xlsx", ".xlsm", ".xls"}


def reindex(ws: Workspace, index: SearchIndex, item_id: str) -> None:
    """A stored document changed in place (by a script): its passages again - none if it no longer reads."""
    name = ws.entry(item_id)["name"]
    try:
        doc = extract(name, ws.path(item_id).read_bytes())
    except DocumentError:
        index.remove("document", item_id)
        ws.set_meta(item_id, "passages", 0)
        return
    passages = [Passage(c.text, c.page, c.section) for c in chunk(doc.blocks)]
    index.replace("document", item_id, name, passages)
    for key, value in (("title", doc.title), ("pages", doc.pages), ("words", doc.words), ("passages", len(passages))):
        ws.set_meta(item_id, key, value)


def index_report(ws: Workspace, index: SearchIndex | None, item_id: str) -> None:
    """An item's latest report, searchable (ask_knowledge finds what a worker did, and why)."""
    if index is None:
        return
    text = ws.report(item_id) if item_id in ws.ids() else None
    if not text:
        index.remove("report", item_id)
        return
    parts = _split_long(text) if len(text.split()) > 300 else [text]
    index.replace("report", item_id, ws.entry(item_id)["name"], [Passage(p) for p in parts])


def store_file(ws: Workspace, index: SearchIndex, path: str | Path) -> dict:
    """An uploaded or downloaded file, kept by what it is: a table (CSV, Excel) for the data analyst,
    a document (PDF, Word, text, Markdown) indexed for search, an image (PNG, JPEG, WebP, GIF), anything
    else as a file for the coder.
    Raises ToolError if a table or document can't be read."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in TABLES:
        item = ws.add_file(path)
        return {"id": item, "kind": "table", "name": path.name, "rows": len(ws.table(item))}
    if suffix in DOCUMENT_KINDS:
        return {"kind": "document", **add_document(ws, index, path)}
    if suffix in IMAGE_SUFFIXES:  # an image item, like generated and script images: vision can look at it
        item = ws.add_image(path.read_bytes(), path.name)
        return {"id": item, "kind": "image", "name": path.name}
    item = ws.add_bytes("file", path.read_bytes(), suffix.lstrip(".") or "bin", name=path.name)
    return {"id": item, "kind": "file", "name": path.name}


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
    notes = matching_notes(index, query)
    result = {"query": query, "search": "keywords and meaning" if used_meaning else
              "keywords only (the embedding model is unavailable)"}
    if not hits and not notes:
        result["passages"] = []
        result["note"] = ("Nothing matched in the documents and notes. If the question is about something said in "
                          "an earlier chat, use recall; otherwise say the documents don't cover it - don't answer "
                          "from memory.")
        return ok(result)
    result["passages"] = [{"source": _source(h), "text": _cut(h.text)} for h in hits]
    if notes:
        result["notes"] = [{"source": _source(h), "saved": h.created[:10], "text": _cut(h.text)} for h in notes]
        if len(notes) > 1:
            result["notes_order"] = LATER_SUPERSEDES
    return ok(result, {"type": "sources", "query": query,
                       "items": [{"source": _source(h), "ref": h.ref, "page": h.page, "text": h.text}
                                 for h in hits + notes]})


def matching_notes(index: SearchIndex, query: str) -> list:
    """The best 1-2 notes, searched apart: a note is a few lines and would lose to long passages in a
    shared ranking, so notes are added after them, never in their place. Only real matches - by
    meaning when the embedding model runs - shown oldest first (a later note supersedes)."""
    hits, used_meaning = index.search(query, ("note",), NOTES_SHOWN)
    return sorted((h for h in hits if h.meaning or not used_meaning), key=lambda h: h.created)


def _source(h) -> str:
    if h.source == "note":
        return f"note '{h.title}' ({h.ref})"
    if h.source == "report":
        return f"the report on {h.title} ({h.ref})"
    where = f", page {h.page}" if h.page else ""
    section = f' - "{h.section}"' if h.section else ""
    return f"{h.title}{where}{section}"


def _cut(text: str) -> str:
    return text if len(text) <= MAX_PASSAGE_CHARS else text[:MAX_PASSAGE_CHARS].rsplit(" ", 1)[0] + " ..."
