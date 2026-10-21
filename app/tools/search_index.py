# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""One search engine for the files (documents, code, charts, pictures, tables), past conversations and pages read:
SQLite FTS5 + embeddings.

Every passage is stored once with its source ('document', 'code', 'chart', 'report', 'chat', 'image', 'table',
'web' - a page the web helper read). A search
runs two rankings over the sources asked for - keywords (FTS5's BM25, Porter-stemmed) and meaning
(cosine similarity of the embedding model's vectors: EmbeddingGemma 2 on llama-server, or bge-small
on the CPU - app/rag/embed.py) - and fuses them by rank (reciprocal rank fusion), so neither score
scale has to be trusted. Keyword search is the part that must work: if the embedding model can't
load, passages are stored without vectors and search says it used keywords only.

A picture is one passage: its name and what made it (a prompt) for keywords, and the picture itself
for meaning - a vector in the same space as text, so a typed question finds it. A table is one passage
too: its name, size, columns and a few example values (files.index_items keeps both current).

Each vector records its model; vectors of two models can't be compared, so search uses only the
current model's, and reindex() gives the others new ones (a new model = a full re-index).

files(mode='find') (documents, code, charts, pictures, tables, reports), the per-turn snapshot of pinned files
(search within given files: refs) and recall (chat) are thin tools over this index.
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

RRF_K = 60
STOPWORDS = frozenset("""
a an and are as at be but by can could did do does for from had has have how i if in into is it its
me my no not of on or our shall should so than that the their them then there these they this to
us was we were what when where which who why will with would you your about any all also been
""".split())  # left out of keyword queries: they match almost every passage
CANDIDATES = 50
# Meaning-only matches below this cosine similarity are dropped, so an unrelated question finds
# nothing instead of the least-bad passage. Each model has its own scale: an embedder's own
# min_similarity (and min_similarity_pictures) wins. This default was measured with bge-small-en-v1.5
# on 11 queries: the right passage scored 0.64-0.75, the best passage for unrelated questions at most
# 0.47. (On 2026-10-09's held-out questions bge-small's ranges overlapped - right 0.575-0.88, unrelated
# up to 0.688 - one reason EmbeddingGemma 2 replaced it.)
MIN_SIMILARITY = 0.55
PICTURE = "image"
TABLE = "table"
WEB = "web"

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
        self._vectors: dict[tuple, tuple] = {}
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
            created: str | None = None, embed: bool = True) -> bool:
        """Store passages; returns whether they got vectors (False = keyword search only). ``embed=False``: keyword
        search now, vectors later (stale() then says so, and the store's background refresh adds them)."""
        passages = [p for p in passages if p.text.strip()]
        if not passages:
            return True
        vectors, model = (self._embed([_text(p.section, p.text) for p in passages], [title] * len(passages))
                          if embed else (None, None))
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

    def replace(self, source: str, ref: str, title: str | None, passages: list[Passage], meta: str | None = None,
                embed: bool = True) -> bool:
        self.remove(source, ref)
        return self.add(source, ref, title, passages, meta, embed=embed)

    def add_picture(self, ref: str, title: str, text: str, data: bytes, stamp: str) -> bool:
        """A picture as one passage: ``text`` (its name, what made it) for keywords, the picture itself
        for meaning when the model takes pictures; ``stamp`` marks the file version it was made from.
        Replaces the picture's earlier passage. Returns whether it got a vector."""
        vector, model = None, None
        if getattr(self.embedder, "images", False):
            try:
                vector, model = self.embedder.embed_images([data])[0], self.embedder.name
            except EmbedderUnavailable:
                pass
        with self._lock, self._db() as db:
            db.execute("DELETE FROM passages WHERE source = ? AND ref = ?", (PICTURE, ref))
            db.execute("INSERT INTO passages (source, ref, title, text, created, meta, vector, model) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (PICTURE, ref, title, text, datetime.now().isoformat(timespec="seconds"), stamp,
                        vector.tobytes() if vector is not None else None, model))
            self._version += 1
        return vector is not None

    def stamps(self, source: str) -> dict[str, tuple[str | None, str | None, str | None]]:
        """The indexed items of a source kept in step with the workspace (pictures, tables):
        ref -> (stamp of the file version, model of its vector, title)."""
        with self._db() as db:
            return {r[0]: (r[1], r[2], r[3]) for r in db.execute(
                "SELECT ref, meta, model, title FROM passages WHERE source = ?", (source,))}

    def expire(self, source: str, before: str) -> int:
        """Remove a source's passages stored before this time (ISO); returns how many."""
        with self._lock, self._db() as db:
            n = db.execute("DELETE FROM passages WHERE source = ? AND created < ?", (source, before)).rowcount
            self._version += 1
        return n

    def first(self, source: str, ref: str, n: int = 1) -> list[Hit]:
        """An item's first n passages (its start)."""
        with self._db() as db:
            rows = db.execute("SELECT source, ref, title, page, section, text, created, meta FROM passages "
                              "WHERE source = ? AND ref = ? ORDER BY id LIMIT ?", (source, ref, n)).fetchall()
        return [Hit(*r, score=0.0, keyword=False, meaning=False) for r in rows]

    def count(self, sources: tuple[str, ...]) -> int:
        with self._db() as db:
            return db.execute(f"SELECT count(*) FROM passages WHERE source IN ({_marks(sources)})", sources).fetchone()[0]

    # ------------------------------------------------------------------ searching

    def search(self, query: str, sources: tuple[str, ...], k: int = 5,
               refs: tuple[str, ...] | None = None) -> tuple[list[Hit], bool]:
        """Top k passages from these sources (and only these items' passages, with refs); returns (hits, whether
        meaning was used). Relevance alone picks them; where it ties, the newest and the oldest come first
        (_ends_first)."""
        keyword = self._keyword(query, sources, refs)
        meaning, used_meaning = self._meaning(query, sources, refs)
        fused: dict[int, float] = defaultdict(float)
        for ranking in (keyword, meaning):
            for rank, rowid in enumerate(ranking):
                fused[rowid] += 1 / (RRF_K + rank + 1)
        best = _ends_first([(i, -fused[i]) for i in fused])[:k]
        if not best:
            return [], used_meaning
        with self._db() as db:
            rows = {r[0]: r for r in db.execute(
                "SELECT id, source, ref, title, page, section, text, created, meta FROM passages "
                f"WHERE id IN ({_marks(best)})", best)}
        kw, mn = set(keyword), set(meaning)
        return [Hit(*rows[i][1:], score=fused[i], keyword=i in kw, meaning=i in mn) for i in best if i in rows], used_meaning

    def _keyword(self, query: str, sources: tuple[str, ...], refs: tuple[str, ...] | None = None) -> list[int]:
        words = [w for w in re.findall(r"\w+", query.lower()) if w not in STOPWORDS]
        if not words or refs == ():
            return []
        match = " OR ".join(f'"{w}"' for w in dict.fromkeys(words))  # quoted: no FTS syntax from user text
        only = f" AND p.ref IN ({_marks(refs)})" if refs else ""
        sql = ("SELECT p.id, bm25(passages_fts) FROM passages_fts f JOIN passages p ON p.id = f.rowid "
               f"WHERE passages_fts MATCH ? AND p.source IN ({_marks(sources)}){only} ORDER BY bm25(passages_fts), p.id {{}} "
               "LIMIT ?")
        # Fetched from both ends of a tie: a word every passage shares scores the same in all of them,
        # and SQLite returns a tied run oldest first - past the limit the newest would be unreachable
        # (Unsloth's measured failure, solved the same way: half the candidates from each end).
        with self._db() as db:
            oldest = db.execute(sql.format("ASC"), (match, *sources, *(refs or ()), CANDIDATES - CANDIDATES // 2)).fetchall()
            newest = db.execute(sql.format("DESC"), (match, *sources, *(refs or ()), CANDIDATES // 2)).fetchall()
        return _ends_first(list(dict(oldest + newest).items()))

    def _meaning(self, query: str, sources: tuple[str, ...], refs: tuple[str, ...] | None = None) -> tuple[list[int], bool]:
        if self.embedder is None:
            return [], False
        try:
            q = self.embedder.embed_query(query)
        except EmbedderUnavailable:
            return [], False
        ids, matrix, pictures, owners = self._matrix(sources)
        if refs is not None:
            keep = np.isin(owners, list(refs))
            ids, matrix, pictures = ids[keep], matrix[keep], pictures[keep]
        if not len(ids):
            return [], True
        sims = matrix @ q
        # Each model has its own scale, and a question scores lower against a picture than against text.
        text_floor = getattr(self.embedder, "min_similarity", MIN_SIMILARITY)
        floors = np.where(pictures, getattr(self.embedder, "min_similarity_pictures", text_floor), text_floor)
        return [int(ids[j]) for j in np.argsort(-sims)[:CANDIDATES] if sims[j] >= floors[j]], True

    def _matrix(self, sources: tuple[str, ...]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Vectors of these sources, which rows are pictures and whose each row is (its ref), cached until the index
        changes."""
        cached = self._vectors.get(sources)
        if cached and cached[0] == self._version:
            return cached[1:]
        with self._db() as db:
            rows = db.execute(f"SELECT id, vector, source, ref FROM passages WHERE vector IS NOT NULL AND model = ? "
                              f"AND source IN ({_marks(sources)})", (self.embedder.name, *sources)).fetchall()
        ids = np.array([r[0] for r in rows], dtype=np.int64)
        matrix = np.vstack([np.frombuffer(r[1], dtype=np.float32) for r in rows]) if rows else np.zeros((0, 1), np.float32)
        pictures = np.array([r[2] == PICTURE for r in rows], dtype=bool)
        owners = np.array([r[3] for r in rows], dtype=object)
        self._vectors[sources] = (self._version, ids, matrix, pictures, owners)
        return ids, matrix, pictures, owners

    def _embed(self, texts: list[str], titles: list[str | None]) -> tuple[np.ndarray | None, str | None]:
        if self.embedder is None:
            return None, None
        try:
            return self.embedder.embed_documents(texts, titles), self.embedder.name
        except EmbedderUnavailable:
            return None, None

    # ------------------------------------------------------------------ the model's vectors

    def stale(self) -> int:
        """Text passages without a vector of the current model: made by another model, or added while
        the model was unavailable. Search by meaning doesn't see them until reindex() gives them one.
        (Pictures are kept current by their own sync, files.index_items: their vectors need the file.)"""
        if self.embedder is None:
            return 0
        with self._db() as db:
            return db.execute("SELECT count(*) FROM passages WHERE (vector IS NULL OR model IS NOT ?) AND source != ?",
                              (self.embedder.name, PICTURE)).fetchone()[0]

    def reindex(self, batch: int = 64) -> int:
        """Give every stale text passage a vector of the current model (vectors of two models can't be
        compared, so a new model means a full re-index); returns how many got one. Stops at the first
        failure - the model unavailable - and the rest wait for the next call."""
        if self.embedder is None:
            return 0
        done, last = 0, 0
        while True:
            with self._db() as db:
                rows = db.execute("SELECT id, title, section, text FROM passages WHERE (vector IS NULL OR model IS NOT ?) "
                                  "AND source != ? AND id > ? ORDER BY id LIMIT ?",
                                  (self.embedder.name, PICTURE, last, batch)).fetchall()
            if not rows:
                return done
            vectors, model = self._embed([_text(r[2], r[3]) for r in rows], [r[1] for r in rows])
            if vectors is None:
                return done
            with self._lock, self._db() as db:
                db.executemany("UPDATE passages SET vector = ?, model = ? WHERE id = ?",
                               [(vectors[i].tobytes(), model, r[0]) for i, r in enumerate(rows)])
                self._version += 1
            done, last = done + len(rows), rows[-1][0]


def _ends_first(scored: list[tuple[int, float]]) -> list[int]:
    """Row ids by score (lower is better); inside a run of equal scores the newest, the oldest, the
    next newest ... (row ids grow with time), so a cut keeps both "what is it now" and "what was it
    first"."""
    out, run, run_score = [], [], None

    def flush():
        run.sort()
        while run:
            out.append(run.pop())
            if run:
                out.append(run.pop(0))

    for rowid, score in sorted(scored, key=lambda x: (round(x[1], 9), x[0])):
        if run and round(score, 9) != run_score:
            flush()
        run.append(rowid)
        run_score = round(score, 9)
    flush()
    return out


def _text(section: str | None, text: str) -> str:
    """What is embedded of a passage: its section heading, then its text (the title goes apart)."""
    return f"{section}\n{text}" if section else text


def _marks(values) -> str:
    return ", ".join("?" * len(values))
