"""Hybrid passage search: BM25 keyword ranking + embedding similarity, merged
with reciprocal rank fusion.

Keywords find exact terms (a product code, "7 days", a supplier's name);
embeddings find the same meaning in other words ("give back" ~ "return").
Reciprocal rank fusion adds 1 / (60 + rank) from each list, so a passage both
methods rank highly wins, without having to put BM25 scores and cosine
similarities on one scale.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass

import numpy as np

K1, B = 1.5, 0.75          # BM25 term-frequency saturation and length normalisation
RRF_K = 60
CANDIDATES = 50            # taken from each ranking before fusing

STOPWORDS = frozenset("""
a an and are as at be but by can could did do does for from had has have how i if in into is it its
me my no not of on or our shall should so than that the their them then there these they this to
us was we were what when where which who why will with would you your about any all also been
""".split())


def tokenize(text: str) -> list[str]:
    return [_stem(t) for t in re.findall(r"\w+", text.lower()) if t not in STOPWORDS]


def _stem(word: str) -> str:
    """Just enough to match plurals: returns/return, policies/policy, days/day."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


class BM25:
    def __init__(self, documents: list[str]):
        self.n = len(documents)
        self.lengths = []
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for i, doc in enumerate(documents):
            counts = Counter(tokenize(doc))
            self.lengths.append(sum(counts.values()))
            for term, tf in counts.items():
                self.postings[term].append((i, tf))
        self.avg_length = (sum(self.lengths) / self.n) if self.n else 0.0

    def scores(self, query: str) -> dict[int, float]:
        scores: dict[int, float] = defaultdict(float)
        for term in set(tokenize(query)):
            postings = self.postings.get(term)
            if not postings:
                continue
            idf = math.log(1 + (self.n - len(postings) + 0.5) / (len(postings) + 0.5))
            for i, tf in postings:
                norm = K1 * (1 - B + B * self.lengths[i] / self.avg_length) if self.avg_length else K1
                scores[i] += idf * tf * (K1 + 1) / (tf + norm)
        return scores


@dataclass
class Ranked:
    index: int              # position in the corpus
    score: float            # fused score
    keyword: float | None   # BM25 score, if the passage shares words with the query
    similarity: float | None  # cosine similarity, if the passage is embedded


def hybrid(bm25: BM25, query: str, vectors: np.ndarray | None, vector_rows: np.ndarray | None,
           query_vector: np.ndarray | None, k: int) -> list[Ranked]:
    """Top ``k`` passages. ``vectors[j]`` is the embedding of passage ``vector_rows[j]``
    (not every passage has to be embedded)."""
    keyword = bm25.scores(query)
    by_keyword = sorted(keyword, key=keyword.get, reverse=True)[:CANDIDATES]

    similarity: dict[int, float] = {}
    by_meaning: list[int] = []
    if vectors is not None and query_vector is not None and len(vectors):
        sims = vectors @ query_vector
        top = np.argsort(-sims)[:CANDIDATES]
        by_meaning = [int(vector_rows[j]) for j in top]
        similarity = dict(zip(vector_rows.tolist(), sims.tolist()))

    fused: dict[int, float] = defaultdict(float)
    for ranking in (by_keyword, by_meaning):
        for rank, i in enumerate(ranking):
            fused[i] += 1 / (RRF_K + rank + 1)
    best = sorted(fused, key=lambda i: (-fused[i], i))[:k]
    return [Ranked(i, fused[i], keyword.get(i), similarity.get(i)) for i in best]
