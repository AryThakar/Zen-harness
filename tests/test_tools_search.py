"""search_files, recall and save_note indexing over one SQLite FTS5 + embeddings index."""

from __future__ import annotations

import pytest

from app.rag.embed import EmbedderUnavailable, FastEmbedder
from app.tools.files import add_document, search_files
from app.tools.memory import recall, remember_turn
from app.tools.notes import save_note
from app.tools.search_index import SearchIndex
from app.tools.workspace import Workspace

POLICY = """# Returns

Customers may return opened phones within 14 days if the box and accessories are complete.
A 10% restocking fee applies to opened items.

# Suppliers

Supplier invoices are paid within 45 days of receipt, by bank transfer, after the goods are checked.
"""


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path / "ws")


@pytest.fixture
def index(tmp_path):
    return SearchIndex(tmp_path / "ws" / "search.sqlite", embedder=None)  # keywords only: fast, deterministic


@pytest.fixture
def policy(ws, index, tmp_path):
    path = tmp_path / "store_policy.md"
    path.write_text(POLICY, encoding="utf-8")
    return add_document(ws, index, path)


# ---------------------------------------------------------------- search_files

def test_document_passages_come_with_their_source(ws, index, policy):
    assert policy["file"] == "file_1" and policy["passages"] == 2
    r = search_files(index, "restocking fee for opened phones")
    assert r.status == "ok" and r.result["search"].startswith("keywords only")
    top = r.result["passages"][0]
    assert top["source"] == 'store_policy.md - "Returns"' and "14 days" in top["text"]
    r = search_files(index, "when are supplier invoices paid")
    assert r.result["passages"][0]["source"].endswith('"Suppliers"')


def test_no_match_says_so_instead_of_returning_noise(index, policy):
    for query in ["best pizza topping", "how to be the one"]:  # the second is all stopwords
        r = search_files(index, query)
        assert r.result["passages"] == [] and "don't cover this" in r.result["note"]
    assert search_files(index, " ").status == "invalid_arguments"


def test_unusable_document_is_refused(ws, index, tmp_path):
    bad = tmp_path / "scan.xyz"
    bad.write_bytes(b"\x00\x01")
    with pytest.raises(Exception, match="aren't supported"):
        add_document(ws, index, bad)


def test_notes_are_searchable_and_updates_replace_old_text(ws, index):
    save_note(ws, "Revenue column", "Use net_sales as revenue.", index=index)
    save_note(ws, "Revenue column", "Use gross_sales as revenue from now on.", index=index)
    r = search_files(index, "which sales column is revenue")
    texts = [p["text"] for p in r.result["passages"]]
    assert texts == ["Use gross_sales as revenue from now on."]
    assert r.result["passages"][0]["source"] == "note 'Revenue column' (note_1)"


# ---------------------------------------------------------------- recall

def test_recall_returns_question_with_answer_and_skips_retrieval_calls(index):
    remember_turn(index, "c1", "What is total revenue in 2015?", "Total revenue in 2015 was 817,860.05.",
                  tool_calls=[{"name": "query_data", "arguments": {"sql": "SELECT sum(revenue) FROM data_2"}},
                              {"name": "search_files", "arguments": {"query": "secret passage text"}}],
                  when="2026-10-01T10:00:00")
    remember_turn(index, "c2", "Draw the sales process", "Here is the diagram of the sales process.")
    r = recall(index, "total revenue 2015")
    turn = r.result["turns"][0]
    assert turn["conversation"] == "c1" and turn["when"] == "2026-10-01 10:00"
    assert "user: What is total revenue" in turn["text"] and "817,860.05" in turn["text"]
    assert "assistant called query_data" in turn["text"] and "secret passage" not in turn["text"]
    assert recall(index, "total revenue 2015", exclude_conversation="c1").result["turns"] == []


def test_documents_and_chats_do_not_mix(index, policy):
    remember_turn(index, "c1", "Tell me about phone returns", "Opened phones can be returned within 14 days.")
    assert all("conversation" not in p["source"] for p in search_files(index, "phone returns").result["passages"])
    assert all(t["conversation"] == "c1" for t in recall(index, "phone returns").result["turns"])


# ---------------------------------------------------------------- with the real embedding model

@pytest.fixture(scope="module")
def embedder():
    e = FastEmbedder("BAAI/bge-small-en-v1.5", "data/models")
    try:
        e.embed_query("probe")
    except EmbedderUnavailable:
        pytest.skip("embedding model not available offline")
    return e


def test_meaning_finds_paraphrases_and_nothing_for_unrelated(ws, tmp_path, embedder):
    index = SearchIndex(tmp_path / "real.sqlite", embedder)
    path = tmp_path / "store_policy.md"
    path.write_text(POLICY, encoding="utf-8")
    add_document(ws, index, path)
    r = search_files(index, "refund rules for used mobiles")  # no shared word with the passage
    assert r.result["search"] == "keywords and meaning"
    assert r.result["passages"] and r.result["passages"][0]["source"].endswith('"Returns"')
    r = search_files(index, "who won the football world cup")
    assert r.result["passages"] == []
