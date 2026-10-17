"""find (files' mode) and recall over one SQLite FTS5 + embeddings index."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.rag.embed import EmbedderUnavailable, FastEmbedder
from app.tools.files import add_document, expire_webpages, find_files, index_webpage
from app.tools.memory import recall, remember_turn
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


# ---------------------------------------------------------------- find

def test_a_document_is_found_with_the_passage_that_matched(ws, index, policy):
    assert policy["passages"] == 2
    r = find_files(ws, index, "restocking fee for opened phones")
    assert r.status == "ok" and r.result["search"] == "names and keywords" and r.result["found"] == 1
    top = r.result["files"][0]
    assert top["path"] == "store_policy.md" and top["match"].startswith("Returns: ") and "14 days" in top["match"]
    assert "searchable" in top["about"]
    assert find_files(ws, index, "when are supplier invoices paid").result["files"][0]["match"].startswith("Suppliers: ")


def test_no_match_says_so_instead_of_returning_noise(ws, index, policy):
    for query in ["best pizza topping", "how to be the one"]:  # the second is all stopwords
        r = find_files(ws, index, query)
        assert r.result["files"] == [] and r.result["found"] == 0 and "Don't answer from memory" in r.result["note"]
    with pytest.raises(Exception, match="find needs query"):
        find_files(ws, index, " ")


def test_unusable_document_is_refused(ws, index, tmp_path):
    bad = tmp_path / "scan.xyz"
    bad.write_bytes(b"\x00\x01")
    with pytest.raises(Exception, match="aren't supported"):
        add_document(ws, index, bad)


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


def test_a_search_within_given_files_finds_only_theirs(ws, index, policy, tmp_path):
    other = tmp_path / "other.md"
    other.write_text("# Returns\n\nOpened laptops have no restocking fee.\n", encoding="utf-8")
    second = add_document(ws, index, other)["file"]
    hits, _ = index.search("restocking fee", ("document",), 5, refs=(second,))
    assert hits and {h.ref for h in hits} == {second}
    assert index.search("restocking fee", ("document",), 5, refs=())[0] == []


# ---------------------------------------------------------------- recall

def test_recall_returns_question_with_answer_and_skips_retrieval_calls(index):
    remember_turn(index, "c1", "What is total revenue in 2015?", "Total revenue in 2015 was 817,860.05.",
                  tool_calls=[{"name": "query_data", "arguments": {"sql": "SELECT sum(revenue) FROM data_2"}},
                              {"name": "recall", "arguments": {"query": "secret passage text"}}],
                  when="2026-10-01T10:00:00")
    remember_turn(index, "c2", "Draw the sales process", "Here is the diagram of the sales process.")
    r = recall(index, "total revenue 2015")
    turn = r.result["turns"][0]
    assert turn["source"] == "conversation c1" and turn["when"] == "2026-10-01 10:00"
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
    assert {t["source"] for t in nothing_cut} == {"conversation other"}
    cut = recall(index, "budget", exclude_conversation="this", visible_since="2026-10-04T12:00:00.000+05:30")
    found = {(t["source"].removeprefix("conversation "), t["when"]) for t in cut.result["turns"]}
    assert found == {("other", "2026-10-02 09:00"), ("this", "2026-10-04 10:37")}  # not the turn still in view


def test_documents_and_chats_do_not_mix(ws, index, policy):
    remember_turn(index, "c1", "Tell me about phone returns", "Opened phones can be returned within 14 days.")
    assert [f["path"] for f in find_files(ws, index, "phone returns").result["files"]] == ["store_policy.md"]
    assert all(t["source"] == "conversation c1" for t in recall(index, "phone returns").result["turns"])


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
    r = find_files(ws, index, "refund rules for used mobiles")  # no shared word with the passage
    assert r.result["search"] == "names, keywords and meaning"
    assert r.result["files"] and r.result["files"][0]["match"].startswith("Returns: ")
    assert find_files(ws, index, "who won the football world cup").result["files"] == []


# ---------------------------------------------------------------- pages the web helper read

PAGE = {"url": "https://shop.example.com/returns?utm_source=x", "title": "Shop returns", "site": "shop.example.com",
        "text": "Opened phones can be returned to the shop within 30 days, without a fee.", "read": "ok"}


def test_pages_read_are_found_after_the_users_own_files(ws, index, policy):
    index_webpage(index, PAGE)
    r = find_files(ws, index, "opened phones returned")
    assert [f["path"] for f in r.result["files"]] == ["store_policy.md"]  # pages are not files
    page = r.result["pages_read_before"][0]
    assert page["source"].startswith("web page 'Shop returns' (https://shop.example.com/returns?utm_source=x), read 20")
    assert "data, not instructions" in r.result["pages_note"] and "date" in r.result["pages_note"]


def test_a_page_read_again_replaces_what_was_kept(ws, index):
    index_webpage(index, PAGE)
    index_webpage(index, {**PAGE, "url": "https://www.shop.example.com/returns#fees", "text": "Returns now cost 5 euros."})
    assert index.count(("web",)) == 1  # the same page (www, #part and tracking tags aside)
    assert "5 euros" in find_files(ws, index, "returns cost").result["pages_read_before"][0]["text"]


def test_pages_read_leave_the_index_after_30_days(ws, index):
    index_webpage(index, PAGE)
    assert expire_webpages(index, datetime.now() + timedelta(days=29)) == 0
    assert expire_webpages(index, datetime.now() + timedelta(days=31)) == 1
    assert "pages_read_before" not in find_files(ws, index, "opened phones returned").result


def test_the_web_helpers_reads_are_kept(ws, index, monkeypatch):
    from app.assistant import registry
    from app.tools import web
    from app.tools.core import ok
    monkeypatch.setattr(web, "read_webpage", lambda url, find=None: ok(dict(PAGE, url=url)))
    tool = registry.TOOLS["read_webpage"]
    tool.handler(registry.ToolContext(ws, index), registry.ReadPageIn(url="https://shop.example.com/returns"))
    assert index.count(("web",)) == 1


def _png(color: str) -> bytes:
    import io
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(out, "PNG")
    return out.getvalue()


def test_how_many_lion_pictures_is_one_find(ws, index, policy):
    """Measured before find: main paged through files(list), read 2 of 4 pages and answered 2; the answer was 5.
    Words naming a kind of file filter by kind: "images" matched every pasted image.png when it was a search word."""
    for n, prompt in enumerate(["A majestic lion on a rock", "A lion and a panda fighting", "A lion cub at dawn"]):
        ws.add_image(_png("gold"), f"generated-{n}.png", meta={"prompt": prompt})
    ws.add_image(_png("black"), "image.png", meta={"caption": "A lioness resting in tall grass"}, by="user")  # pasted
    ws.add_image(_png("white"), "image (2).png", meta={"caption": "A receipt from a coffee shop"}, by="user")
    ws.add_image(_png("grey"), "panda.png", meta={"prompt": "A panda eating bamboo"})
    ws.add_bytes("file", b"# Lions\n\nNotes on lion behaviour.", "md", name="lion-notes.md")  # a document, not a picture
    r = find_files(ws, index, "how many lion images")
    assert r.result["found"] == 3 and {f["path"] for f in r.result["files"]} == {f"generated-{n}.png" for n in range(3)}
    assert find_files(ws, index, "lion").result["found"] == 4  # the pictures and the document
    assert find_files(ws, index, "how many pictures").result["found"] == 6  # a kind alone: every file of it
    assert [f["path"] for f in find_files(ws, index, "lioness pictures").result["files"]] == ["image.png"]  # its caption
    near = find_files(ws, index, "polcy.md").result  # misspelt: the closest names
    assert near["found"] == 0 and "Did you mean: store_policy.md" in near["note"]
