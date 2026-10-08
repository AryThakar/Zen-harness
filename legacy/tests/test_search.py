"""The legacy library's ranking: BM25 and reciprocal rank fusion (legacy/rag/search.py)."""

import numpy as np
import pytest

from legacy.rag.search import BM25, hybrid, tokenize


def test_bm25_matches_plurals_and_prefers_rare_terms():
    assert tokenize("Returns and policies") == ["return", "policy"]
    bm25 = BM25(["refund policy for returned goods", "delivery policy", "return a product within 7 days"])
    scores = bm25.scores("how do I return products")
    assert max(scores, key=scores.get) == 2


def test_fusion_rewards_passages_both_methods_like():
    bm25 = BM25(["apple pie recipe", "apple orchard", "banana bread"])
    vectors = np.array([[1, 0], [0, 1], [0.6, 0.8]], dtype=np.float32)
    ranked = hybrid(bm25, "apple", vectors, np.array([0, 1, 2]), np.array([1, 0], dtype=np.float32), k=3)
    assert ranked[0].index == 0  # first by keywords and by meaning
    assert ranked[0].keyword and ranked[0].similarity == pytest.approx(1.0)
    only_meaning = hybrid(bm25, "cake", vectors, np.array([0, 1, 2]), np.array([1, 0], dtype=np.float32), k=1)
    assert only_meaning[0].index == 0 and only_meaning[0].keyword is None  # no shared words, found by meaning
