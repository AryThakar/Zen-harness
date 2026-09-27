"""Business documents for the assistant to search: upload, list, read, delete, search."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, timedelta

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status

from app.ml.base import latest_sale_date
from app.rag.embed import EmbedderUnavailable
from app.rag.extract import MAX_BYTES, DocumentError
from app.rag.library import DuplicateDocument, Library
from app.rag.samples import sample_documents

from .deps import get_library
from .schemas import DocumentAddedOut, DocumentOut, LibraryOut, PassageOut, SamplesOut, SearchOut

router = APIRouter(prefix="/documents", tags=["documents"])


@router.get("", response_model=LibraryOut)
def list_documents(library: Library = Depends(get_library)) -> LibraryOut:
    """Documents in the library, newest first, and whether meaning-based search is available."""
    return LibraryOut(status=library.status(), documents=library.documents())


@router.post("", response_model=DocumentAddedOut, status_code=status.HTTP_201_CREATED)
def add_document(file: UploadFile = File(...), library: Library = Depends(get_library)) -> DocumentAddedOut:
    """Add a PDF, Word (.docx), .txt or .md file. A file with the same name replaces the earlier upload."""
    data = file.file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, f"Documents can be at most {MAX_BYTES // 2**20} MB.")
    try:
        return DocumentAddedOut(**library.add(file.filename or "document.txt", data))
    except DuplicateDocument as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except DocumentError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.post("/samples", response_model=SamplesOut, status_code=status.HTTP_201_CREATED)
def add_samples(library: Library = Depends(get_library)) -> SamplesOut:
    """Add the demo shop's sample documents (policies, supplier terms, an operations
    log dated to match the demo sales)."""
    cur = library.db.cursor()
    try:
        end = latest_sale_date(cur) or date.today() - timedelta(days=1)
    finally:
        cur.close()
    added, skipped = [], []
    for name, content in sample_documents(end):
        try:
            added.append(DocumentAddedOut(**library.add(name, content)))
        except DuplicateDocument:
            skipped.append(name)
    return SamplesOut(added=added, skipped=skipped)


@router.get("/search", response_model=SearchOut)
def search_documents(
    q: str = Query(..., min_length=1, max_length=500),
    k: int = Query(5, ge=1, le=20),
    library: Library = Depends(get_library),
) -> SearchOut:
    """Passages that best match a question: keywords and meaning combined."""
    result = library.search(q, k)
    hits = [{**asdict(h), "source": h.source} for h in result.hits]
    return SearchOut(query=q, mode=result.mode, note=result.note, hits=hits)


@router.post("/reindex")
def reindex(library: Library = Depends(get_library)) -> dict:
    """Embed passages stored without a vector (model was unavailable, or changed)."""
    try:
        return {"embedded": library.reindex()}
    except EmbedderUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc


@router.get("/{doc_id}/passages", response_model=list[PassageOut])
def passages(
    doc_id: int,
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    library: Library = Depends(get_library),
) -> list[PassageOut]:
    """A document's text as stored for search, in order."""
    rows = library.passages(doc_id, limit, offset)
    if not rows and offset == 0 and not any(d["doc_id"] == doc_id for d in library.documents()):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Document {doc_id} not found.")
    return [PassageOut(**r) for r in rows]


@router.delete("/{doc_id}", response_model=DocumentOut)
def delete_document(doc_id: int, library: Library = Depends(get_library)) -> DocumentOut:
    doc = library.delete(doc_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Document {doc_id} not found.")
    return DocumentOut(**doc)
