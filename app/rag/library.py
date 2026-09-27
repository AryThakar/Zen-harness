"""The document library: add, list, delete and search business documents.

Adding a document: extract text -> split into passages -> embed (outside the
write lock) -> store document and passages in one transaction. If the model
can't be loaded the passages are stored without vectors and search uses
keywords only until ``reindex`` fills them in.

Search keeps an in-memory index (BM25 postings + a matrix of vectors) that is
rebuilt when the passages change.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.db.connection import Database

from .chunking import Chunk, chunk
from .embed import Embedder, EmbedderUnavailable
from .extract import DocumentError, extract
from .search import BM25, hybrid

log = logging.getLogger(__name__)

DOC_COLUMNS = ("doc_id", "filename", "kind", "title", "pages", "words", "chunks", "uploaded_at")
EMBED_BATCH = 256


class DuplicateDocument(DocumentError):
    """The same file is already in the library."""


@dataclass
class Hit:
    chunk_id: int
    doc_id: int
    filename: str
    title: str
    page: int | None
    section: str | None
    text: str
    score: float
    keyword: float | None
    similarity: float | None

    @property
    def source(self) -> str:
        where = f", page {self.page}" if self.page else ""
        section = f' - "{self.section}"' if self.section else ""
        return f"{self.filename}{where}{section}"


@dataclass
class SearchResult:
    hits: list[Hit]
    mode: str                # "hybrid" or "keyword"
    note: str | None = None  # why search is keyword-only, if it is


@dataclass
class _Index:
    fingerprint: tuple
    rows: list[tuple]        # (chunk_id, doc_id, filename, title, page, section, text)
    bm25: BM25
    vectors: np.ndarray | None
    vector_rows: np.ndarray | None
    missing_vectors: int = 0
    extra: dict = field(default_factory=dict)


class Library:
    def __init__(self, db: Database, embedder: Embedder):
        self.db = db
        self.embedder = embedder
        self._index: _Index | None = None
        self._index_lock = threading.Lock()
        self._semantic_error: str | None = None

    # --- documents ---------------------------------------------------------------

    def add(self, filename: str, data: bytes) -> dict:
        """Store a document; returns it with ``replaced`` (an older version of the
        same filename that it replaced, if any) and ``semantic`` (embedded or not)."""
        content_hash = hashlib.sha256(data).hexdigest()
        cur = self.db.cursor()
        try:
            same = cur.execute("SELECT filename FROM documents WHERE content_hash = ?", [content_hash]).fetchone()
        finally:
            cur.close()
        if same:
            raise DuplicateDocument(f"This file is already in the library as '{same[0]}'.")

        extracted = extract(filename, data)
        chunks = chunk(extracted.blocks)
        vectors, semantic = self._embed(chunks)

        with self.db.transaction() as cur:
            # Checked again under the write lock: the same file may have arrived meanwhile.
            same = cur.execute("SELECT filename FROM documents WHERE content_hash = ?", [content_hash]).fetchone()
            if same:
                raise DuplicateDocument(f"This file is already in the library as '{same[0]}'.")
            old = cur.execute(
                f"SELECT {', '.join(DOC_COLUMNS)} FROM documents WHERE filename = ?", [filename]
            ).fetchone()
            if old:
                self._delete(cur, old[0])
            doc_id = cur.execute(
                "INSERT INTO documents (doc_id, filename, kind, title, pages, words, chunks, content_hash) "
                "VALUES (nextval('doc_id_seq'), ?, ?, ?, ?, ?, ?, ?) RETURNING doc_id",
                [filename, extracted.kind, extracted.title, extracted.pages, extracted.words, len(chunks),
                 content_hash],
            ).fetchone()[0]
            staged = pd.DataFrame({
                "doc_id": doc_id,
                "ord": range(len(chunks)),
                "page": pd.array([c.page for c in chunks], dtype="Int32"),
                "section": [c.section for c in chunks],
                "text": [c.text for c in chunks],
                "embed_model": [self.embedder.name if semantic else None] * len(chunks),
                "embedding": [v.tobytes() for v in vectors] if semantic else [None] * len(chunks),
            })
            cur.register("staged_chunks", staged)
            try:
                cur.execute(
                    "INSERT INTO doc_chunks (chunk_id, doc_id, ord, page, section, text, embed_model, embedding) "
                    "SELECT nextval('chunk_id_seq'), doc_id, ord, page, section, text, embed_model, "
                    "embedding::BLOB FROM staged_chunks ORDER BY ord"
                )
            finally:
                cur.unregister("staged_chunks")
            doc = self._doc(cur, doc_id)
        doc["replaced"] = dict(zip(DOC_COLUMNS, old)) if old else None
        doc["semantic"] = semantic
        return doc

    def documents(self) -> list[dict]:
        cur = self.db.cursor()
        try:
            rows = cur.execute(f"SELECT {', '.join(DOC_COLUMNS)} FROM documents ORDER BY uploaded_at DESC, doc_id DESC")
            return [dict(zip(DOC_COLUMNS, r)) for r in rows.fetchall()]
        finally:
            cur.close()

    def passages(self, doc_id: int, limit: int = 20, offset: int = 0) -> list[dict]:
        cur = self.db.cursor()
        try:
            rows = cur.execute(
                "SELECT chunk_id, ord, page, section, text FROM doc_chunks WHERE doc_id = ? ORDER BY ord "
                "LIMIT ? OFFSET ?", [doc_id, limit, offset]
            ).fetchall()
        finally:
            cur.close()
        return [dict(zip(("chunk_id", "ord", "page", "section", "text"), r)) for r in rows]

    def delete(self, doc_id: int) -> dict | None:
        with self.db.transaction() as cur:
            doc = self._doc(cur, doc_id)
            if doc:
                self._delete(cur, doc_id)
            return doc

    def reindex(self) -> int:
        """Embed passages that have no vector for the current model; returns how many."""
        cur = self.db.cursor()
        try:
            stale = cur.execute(
                "SELECT chunk_id, section, text FROM doc_chunks WHERE embed_model IS DISTINCT FROM ? ORDER BY chunk_id",
                [self.embedder.name],
            ).fetchall()
        finally:
            cur.close()
        done = 0
        for start in range(0, len(stale), EMBED_BATCH):
            batch = stale[start:start + EMBED_BATCH]
            vectors = self.embedder.embed_documents([Chunk(t, None, s).search_text() for _, s, t in batch])
            with self.db.transaction() as cur:
                cur.executemany(
                    "UPDATE doc_chunks SET embed_model = ?, embedding = ? WHERE chunk_id = ?",
                    [[self.embedder.name, v.tobytes(), cid] for (cid, _, _), v in zip(batch, vectors)],
                )
            done += len(batch)
        self._semantic_error = None
        return done

    def status(self) -> dict:
        cur = self.db.cursor()
        try:
            docs, chunks, embedded = cur.execute(
                "SELECT (SELECT count(*) FROM documents), count(*), count(*) FILTER (WHERE embed_model = ?) "
                "FROM doc_chunks", [self.embedder.name]
            ).fetchone()
        finally:
            cur.close()
        return {"documents": docs, "passages": chunks, "embedded": embedded, "embed_model": self.embedder.name,
                "semantic_error": self._semantic_error}

    # --- search ------------------------------------------------------------------

    def search(self, query: str, k: int = 5) -> SearchResult:
        index = self._current_index()
        if not index.rows:
            return SearchResult([], "keyword", "No documents in the library yet.")
        query_vector, note = None, None
        if index.vectors is not None:
            try:
                query_vector = self.embedder.embed_query(query)
            except EmbedderUnavailable as exc:
                self._semantic_error = note = str(exc)
        else:
            note = self._semantic_error or "Passages aren't embedded yet; run a reindex for meaning-based search."
        if index.missing_vectors and query_vector is not None:
            note = f"{index.missing_vectors} passages aren't embedded yet and are matched by keywords only."
        ranked = hybrid(index.bm25, query, index.vectors, index.vector_rows, query_vector, k)
        hits = [Hit(*index.rows[r.index], score=r.score, keyword=r.keyword, similarity=r.similarity) for r in ranked]
        return SearchResult(hits, "hybrid" if query_vector is not None else "keyword", note)

    def _current_index(self) -> _Index:
        cur = self.db.cursor()
        try:
            fingerprint = cur.execute(
                "SELECT count(*), coalesce(max(chunk_id), 0), count(*) FILTER (WHERE embed_model = ?) FROM doc_chunks",
                [self.embedder.name],
            ).fetchone()
            with self._index_lock:
                if self._index is None or self._index.fingerprint != fingerprint:
                    self._index = self._build(cur, fingerprint)
                return self._index
        finally:
            cur.close()

    def _build(self, cur, fingerprint: tuple) -> _Index:
        data = cur.execute(
            """
            SELECT c.chunk_id, c.doc_id, d.filename, coalesce(d.title, d.filename), c.page, c.section, c.text,
                   c.embed_model = ? AS current, c.embedding
            FROM doc_chunks c JOIN documents d USING (doc_id)
            ORDER BY c.chunk_id
            """,
            [self.embedder.name],
        ).fetchall()
        rows = [r[:7] for r in data]
        bm25 = BM25([Chunk(text, None, section).search_text() for (_, _, _, _, _, section, text) in rows])
        embedded = [(i, r[8]) for i, r in enumerate(data) if r[7] and r[8] is not None]
        vectors = vector_rows = None
        if embedded:
            vector_rows = np.array([i for i, _ in embedded])
            vectors = np.vstack([np.frombuffer(blob, dtype=np.float32) for _, blob in embedded])
        return _Index(fingerprint, rows, bm25, vectors, vector_rows, missing_vectors=len(rows) - len(embedded))

    # --- helpers -----------------------------------------------------------------

    def _embed(self, chunks: list[Chunk]) -> tuple[np.ndarray | None, bool]:
        try:
            vectors = self.embedder.embed_documents([c.search_text() for c in chunks])
        except EmbedderUnavailable as exc:
            self._semantic_error = str(exc)
            return None, False
        self._semantic_error = None
        return vectors, True

    @staticmethod
    def _doc(cur, doc_id: int) -> dict | None:
        row = cur.execute(f"SELECT {', '.join(DOC_COLUMNS)} FROM documents WHERE doc_id = ?", [doc_id]).fetchone()
        return dict(zip(DOC_COLUMNS, row)) if row else None

    @staticmethod
    def _delete(cur, doc_id: int) -> None:
        cur.execute("DELETE FROM doc_chunks WHERE doc_id = ?", [doc_id])
        cur.execute("DELETE FROM documents WHERE doc_id = ?", [doc_id])
