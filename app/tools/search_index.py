"""One search engine for documents, notes and past conversations: SQLite FTS5 + embeddings.

Every passage is stored once with its source ('document', 'note', 'chat'). A search runs two
rankings over the sources asked for - keywords (FTS5's BM25, Porter-stemmed) and meaning (cosine
similarity of bge-small embeddings, run locally by fastembed) - and fuses them by rank
(reciprocal rank fusion), so neither score scale has to be trusted. Keyword search is the part
that must work: if the embedding model can't load, passages are stored without vectors and search
says it used keywords only.

search_files (documents + notes) and recall (chat) are thin tools over this index.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from app.rag.embed import Embedder, EmbedderUnavailable
from app.rag.search import STOPWORDS

RRF_K = 60
CANDIDATES = 50
# Meaning-only matches below this cosine similarity are dropped, so an unrelated question finds
# nothing instead of the least-bad passage. Measured with bge-small-en-v1.5 on 11 queries: the right
# passage scored 0.64-0.75, the best passage for unrelated questions at most 0.47.
MIN_SIMILARITY = 0.55

SCHEMA = """
CREATE TABLE IF NOT EXISTS passages (
    id INTEGER PRIMARY KEY, source TEXT NOT NULL, ref TEXT NOT NULL, title TEXT, page INTEGER,
    section TEXT, text TEXT NOT NULL, created TEXT NOT NULL, meta TEXT, vector BLOB, model TEXT);
CREATE INDEX IF NOT EXISTS passages_ref ON passages (source, ref);
CREATE VIRTUAL TABLE IF NOT EXISTS passages_fts USING fts5(
    text, title, content='passages', content_rowid='id', tokenize='porter unicode61');
CREATE TRIGGER IF NOT EXISTS passages_ai AFTER INSERT ON passages BEGIN
    INSERT INTO passages_fts (rowid, text, title) VALUES (new.id, new.text, coalesce(new.title, ''));
END;
CREATE TRIGGER IF NOT EXISTS passages_ad AFTER DELETE ON passages BEGIN
    INSERT INTO passages_fts (passages_fts, rowid, text, title) VALUES ('delete', old.id, old.text, coalesce(old.title, ''));
END;
"""


@dataclass
class Passage:
    text: str
    page: int | None = None
    section: str | None = None


@dataclass
class Hit:
    source: str
    ref: str
    title: str | None
    page: int | None
    section: str | None
    text: str
    created: str
    meta: str | None
    score: float
    keyword: bool
    meaning: bool


class SearchIndex:
    def __init__(self, path: str | Path, embedder: Embedder | None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.embedder = embedder
        self._lock = threading.Lock()
        self._vectors: dict[tuple, tuple[int, np.ndarray, np.ndarray]] = {}
        self._version = 0
        with self._db() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _db(self):
        """A connection that commits on success and is always closed (Windows keeps files locked)."""
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            yield db
            db.commit()
        finally:
            db.close()

    # ------------------------------------------------------------------ writing

    def add(self, source: str, ref: str, title: str | None, passages: list[Passage], meta: str | None = None,
            created: str | None = None) -> bool:
        """Store passages; returns whether they got vectors (False = keyword search only)."""
        passages = [p for p in passages if p.text.strip()]
        if not passages:
            return True
        vectors, model = self._embed([f"{p.section}\n{p.text}" if p.section else p.text for p in passages])
        created = created or datetime.now().isoformat(timespec="seconds")
        rows = [(source, ref, title, p.page, p.section, p.text, created, meta,
                 vectors[i].tobytes() if vectors is not None else None, model) for i, p in enumerate(passages)]
        with self._lock, self._db() as db:
            db.executemany("INSERT INTO passages (source, ref, title, page, section, text, created, meta, vector, model) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
            self._version += 1
        return vectors is not None

    def remove(self, source: str, ref: str) -> int:
        with self._lock, self._db() as db:
            n = db.execute("DELETE FROM passages WHERE source = ? AND ref = ?", (source, ref)).rowcount
            self._version += 1
        return n

    def replace(self, source: str, ref: str, title: str | None, passages: list[Passage], meta: str | None = None) -> bool:
        self.remove(source, ref)
        return self.add(source, ref, title, passages, meta)

    def count(self, sources: tuple[str, ...]) -> int:
        with self._db() as db:
            return db.execute(f"SELECT count(*) FROM passages WHERE source IN ({_marks(sources)})", sources).fetchone()[0]

    # ------------------------------------------------------------------ searching

    def search(self, query: str, sources: tuple[str, ...], k: int = 5) -> tuple[list[Hit], bool]:
        """Top k passages from these sources; returns (hits, whether meaning was used)."""
        keyword = self._keyword(query, sources)
        meaning, used_meaning = self._meaning(query, sources)
        fused: dict[int, float] = defaultdict(float)
        for ranking in (keyword, meaning):
            for rank, rowid in enumerate(ranking):
                fused[rowid] += 1 / (RRF_K + rank + 1)
        best = sorted(fused, key=lambda i: (-fused[i], i))[:k]
        if not best:
            return [], used_meaning
        with self._db() as db:
            rows = {r[0]: r for r in db.execute(
                "SELECT id, source, ref, title, page, section, text, created, meta FROM passages "
                f"WHERE id IN ({_marks(best)})", best)}
        kw, mn = set(keyword), set(meaning)
        return [Hit(*rows[i][1:], score=fused[i], keyword=i in kw, meaning=i in mn) for i in best if i in rows], used_meaning

    def _keyword(self, query: str, sources: tuple[str, ...]) -> list[int]:
        words = [w for w in re.findall(r"\w+", query.lower()) if w not in STOPWORDS]
        if not words:
            return []
        match = " OR ".join(f'"{w}"' for w in dict.fromkeys(words))  # quoted: no FTS syntax from user text
        with self._db() as db:
            return [r[0] for r in db.execute(
                "SELECT p.id FROM passages_fts f JOIN passages p ON p.id = f.rowid "
                f"WHERE passages_fts MATCH ? AND p.source IN ({_marks(sources)}) ORDER BY bm25(passages_fts) LIMIT ?",
                (match, *sources, CANDIDATES))]

    def _meaning(self, query: str, sources: tuple[str, ...]) -> tuple[list[int], bool]:
        if self.embedder is None:
            return [], False
        try:
            q = self.embedder.embed_query(query)
        except EmbedderUnavailable:
            return [], False
        ids, matrix = self._matrix(sources)
        if not len(ids):
            return [], True
        sims = matrix @ q
        return [int(ids[j]) for j in np.argsort(-sims)[:CANDIDATES] if sims[j] >= MIN_SIMILARITY], True

    def _matrix(self, sources: tuple[str, ...]) -> tuple[np.ndarray, np.ndarray]:
        """Vectors of these sources, cached until the index changes."""
        cached = self._vectors.get(sources)
        if cached and cached[0] == self._version:
            return cached[1], cached[2]
        with self._db() as db:
            rows = db.execute(f"SELECT id, vector FROM passages WHERE vector IS NOT NULL AND model = ? "
                              f"AND source IN ({_marks(sources)})", (self.embedder.name, *sources)).fetchall()
        ids = np.array([r[0] for r in rows], dtype=np.int64)
        matrix = np.vstack([np.frombuffer(r[1], dtype=np.float32) for r in rows]) if rows else np.zeros((0, 1), np.float32)
        self._vectors[sources] = (self._version, ids, matrix)
        return ids, matrix

    def _embed(self, texts: list[str]) -> tuple[np.ndarray | None, str | None]:
        if self.embedder is None:
            return None, None
        try:
            return self.embedder.embed_documents(texts), self.embedder.name
        except EmbedderUnavailable:
            return None, None


def _marks(values) -> str:
    return ", ".join("?" * len(values))
