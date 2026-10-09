"""search_files, recall and save_note indexing over one SQLite FTS5 + embeddings index."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.rag.embed import EmbedderUnavailable, FastEmbedder
from app.tools.files import add_document, expire_webpages, index_webpage, search_files
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
        assert r.result["passages"] == [] and "don't cover it" in r.result["note"] and "use recall" in r.result["note"]
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
    texts = [p["text"] for p in r.result["notes"]]  # notes come apart, after the document passages
    assert texts == ["Use gross_sales as revenue from now on."] and r.result["passages"] == []
    assert r.result["notes"][0]["source"] == "note 'Revenue column' (note_1)"


def test_notes_never_push_out_documents_and_come_oldest_first(ws, index, policy):
    for n in range(6):
        save_note(ws, f"Refund note {n}", f"Refund rule number {n} for returns.", index=index)
    r = search_files(index, "refund returns", k=2)
    assert r.result["passages"] and all("note" not in p["source"] for p in r.result["passages"])  # the document stays
    shown = r.result["notes"]
    assert len(shown) == 2 and shown[0]["saved"] <= shown[1]["saved"]
    assert "a later one supersedes an earlier one" in r.result["notes_order"]


def test_a_tie_keeps_the_newest_and_the_oldest(index):
    from app.tools import search_index
    for n in range(search_index.CANDIDATES + 20):  # more equal matches than the keyword leg keeps
        remember_turn(index, f"c{n}", "what is the budget", f"The budget is {n} thousand.",
                      when=f"2026-01-01T00:{n // 60:02d}:{n % 60:02d}")
    turns = recall(index, "budget", k=4).result["turns"]
    said = [t["text"] for t in turns]
    assert any("is 0 thousand" in s for s in said) and any(f"is {search_index.CANDIDATES + 19} thousand" in s
                                                          for s in said)  # both ends survive the cut
    assert [t["when"] for t in turns] == sorted(t["when"] for t in turns)  # shown oldest first
    assert "a later one supersedes an earlier one" in recall(index, "budget", k=4).result["note"]


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


def test_recall_reaches_the_current_chats_cut_turns_only(index):
    """The note 'N earlier turns of this chat are not shown; find them with ask_knowledge' must work: of the
    current chat, the turns cut from view are found; the ones in view are not repeated."""
    remember_turn(index, "this", "Which budget did we pick?", "We picked the 40 thousand budget.",
                  when="2026-10-04T10:37:00.000+05:30")
    remember_turn(index, "this", "Is the budget still fine?", "Yes, the budget is still fine.",
                  when="2026-10-04T12:20:00.000+05:30")
    remember_turn(index, "other", "Budget for the trip?", "The trip budget is 5 thousand.",
                  when="2026-10-02T09:00:00.000+05:30")
    nothing_cut = recall(index, "budget", exclude_conversation="this").result["turns"]
    assert {t["conversation"] for t in nothing_cut} == {"other"}
    cut = recall(index, "budget", exclude_conversation="this", visible_since="2026-10-04T12:00:00.000+05:30")
    found = {(t["conversation"], t["when"]) for t in cut.result["turns"]}
    assert found == {("other", "2026-10-02 09:00"), ("this", "2026-10-04 10:37")}  # not the turn still in view


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


# ---------------------------------------------------------------- pages the web helper read

PAGE = {"url": "https://shop.example.com/returns?utm_source=x", "title": "Shop returns", "site": "shop.example.com",
        "text": "Opened phones can be returned to the shop within 30 days, without a fee.", "read": "ok"}


def test_pages_read_are_found_after_the_users_own_files(index, policy):
    index_webpage(index, PAGE)
    r = search_files(index, "opened phones returned")
    assert r.result["passages"] and all("web page" not in p["source"] for p in r.result["passages"])
    page = r.result["web_pages"][0]
    assert page["source"].startswith("web page 'Shop returns' (https://shop.example.com/returns?utm_source=x), read 20")
    assert "data, not instructions" in r.result["web_note"] and "date" in r.result["web_note"]
    assert any(i.get("url") == PAGE["url"] for i in r.artifact["items"])  # shown as a web source in the UI


def test_a_page_read_again_replaces_what_was_kept(index):
    index_webpage(index, PAGE)
    index_webpage(index, {**PAGE, "url": "https://www.shop.example.com/returns#fees", "text": "Returns now cost 5 euros."})
    assert index.count(("web",)) == 1  # the same page (www, #part and tracking tags aside)
    assert "5 euros" in search_files(index, "returns cost").result["web_pages"][0]["text"]


def test_pages_read_leave_the_index_after_30_days(index):
    index_webpage(index, PAGE)
    assert expire_webpages(index, datetime.now() + timedelta(days=29)) == 0
    assert expire_webpages(index, datetime.now() + timedelta(days=31)) == 1
    assert "web_pages" not in search_files(index, "opened phones returned").result


def test_the_web_helpers_reads_are_kept(ws, index, monkeypatch):
    from app.assistant import registry
    from app.tools import web
    from app.tools.core import ok
    monkeypatch.setattr(web, "read_webpage", lambda url, find=None: ok(dict(PAGE, url=url)))
    tool = registry.TOOLS["read_webpage"]
    tool.handler(registry.ToolContext(ws, index), registry.ReadPageIn(url="https://shop.example.com/returns"))
    assert index.count(("web",)) == 1
