"""Document search: extraction, chunking, ranking, the library and its API."""

from datetime import date

import numpy as np
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.providers import ScriptedProvider
from app.datagen import planted_events
from app.db.connection import Database
from app.main import create_app
from app.rag.chunking import MAX_WORDS, OVERLAP_WORDS, chunk
from app.rag.embed import EmbedderUnavailable, HashEmbedder
from app.rag.extract import Block, DocumentError, extract
from app.rag.library import DuplicateDocument, Library
from app.rag.samples import _pdf, sample_documents
from app.rag.search import BM25, hybrid, tokenize

END = date(2026, 9, 26)
SAMPLES = dict(sample_documents(END))


# --- extraction --------------------------------------------------------------------

def test_extracts_every_format_with_pages_and_sections():
    md = extract("returns_and_warranty_policy.md", SAMPLES["returns_and_warranty_policy.md"])
    assert md.kind == "md" and md.title == "Returns, refunds and warranty policy"
    assert {b.section for b in md.blocks} >= {"Returns", "Refunds", "Warranty"}
    assert not any(b.text.startswith("#") for b in md.blocks)

    docx = extract("discount_and_pricing_policy.docx", SAMPLES["discount_and_pricing_policy.docx"])
    assert docx.kind == "docx" and docx.title == "Discount and pricing policy"
    assert "Smartphone X1 | 18,999 | Electronics" in [b.text for b in docx.blocks]  # table rows kept
    assert next(b for b in docx.blocks if b.text.startswith("Smartphone X1")).section == "Price list"

    pdf = extract("supplier_terms_smarttech.pdf", SAMPLES["supplier_terms_smarttech.pdf"])
    assert pdf.kind == "pdf" and pdf.pages == 2
    assert any("Priya Nair" in b.text and b.page == 2 for b in pdf.blocks)

    txt = extract("operations_log.txt", SAMPLES["operations_log.txt"])
    assert txt.kind == "txt" and txt.words > 100


def test_text_files_in_windows_encoding_are_read():
    doc = extract("notes.txt", "Café opening hours: 10–8".encode("cp1252"))
    assert doc.blocks[0].text == "Café opening hours: 10–8"


@pytest.mark.parametrize("name, data, message", [
    ("sheet.xlsx", b"PK...", "aren't supported"),
    ("empty.txt", b"", "empty"),
    ("blank.txt", b"   \n\n  ", "No text found"),
    ("scan.pdf", _pdf([""]), "scanned PDF"),
    ("broken.pdf", b"%PDF-1.4 garbage", "couldn't be read"),
    ("broken.docx", b"not a zip", "save it as .docx"),
])
def test_unusable_files_are_explained(name, data, message):
    with pytest.raises(DocumentError, match=message):
        extract(name, data)


# --- chunking ----------------------------------------------------------------------

def test_long_sections_split_with_overlap_and_pages_never_mix():
    sentence = "The store opens at ten and closes at eight on weekdays. "
    blocks = [Block(sentence * 120, page=1), Block("Short page two text. " * 10, page=2)]
    chunks = chunk(blocks)
    page_one = [c for c in chunks if c.page == 1]
    assert len(page_one) >= 3 and all(len(c.text.split()) <= MAX_WORDS + OVERLAP_WORDS for c in page_one)
    first_tail = page_one[0].text.split()[-OVERLAP_WORDS:]
    assert page_one[1].text.split()[:OVERLAP_WORDS] == first_tail
    assert [c.page for c in chunks][-1] == 2 and "store opens" not in chunks[-1].text


def test_a_lone_heading_joins_the_next_passage():
    blocks = [Block("Delivery", section="Delivery"),
              Block("We deliver in Mumbai within two days. " * 5, section="Delivery details")]
    chunks = chunk(blocks)
    assert len(chunks) == 1 and chunks[0].text.startswith("Delivery We deliver")


# --- ranking -----------------------------------------------------------------------

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


# --- library -----------------------------------------------------------------------

@pytest.fixture
def library():
    db = Database()
    yield Library(db, HashEmbedder())
    db.close()


def test_library_adds_replaces_rejects_duplicates_and_deletes(library):
    doc = library.add("policy.md", b"# Returns\n\nReturns are accepted within 7 days with the bill.")
    assert doc["semantic"] and doc["replaced"] is None and doc["chunks"] == 1

    with pytest.raises(DuplicateDocument, match="already in the library as 'policy.md'"):
        library.add("copy-of-policy.md", b"# Returns\n\nReturns are accepted within 7 days with the bill.")

    newer = library.add("policy.md", b"# Returns\n\nReturns are accepted within 10 days with the bill.")
    assert newer["replaced"]["doc_id"] == doc["doc_id"]
    assert [d["doc_id"] for d in library.documents()] == [newer["doc_id"]]
    assert "10 days" in library.search("return days").hits[0].text

    assert library.delete(newer["doc_id"])["filename"] == "policy.md"
    assert library.delete(newer["doc_id"]) is None
    assert library.search("return").hits == [] and library.status()["passages"] == 0


def test_sample_documents_answer_questions(library):
    for name, data in SAMPLES.items():
        library.add(name, data)
    top = library.search("how long does a UPI refund take").hits[0]
    assert top.filename == "returns_and_warranty_policy.md" and top.section == "Refunds"
    assert top.source == 'returns_and_warranty_policy.md - "Refunds"'
    outage = library.search("billing outage").hits[0]
    day = planted_events(18, END)["outage"]
    assert outage.filename == "operations_log.txt" and f"{day.day} {day:%B %Y}: Billing system outage" in outage.text
    assert library.search("Priya Nair").hits[0].source == "supplier_terms_smarttech.pdf, page 2"


class Offline:
    name = "offline-model"

    def embed_documents(self, texts):
        raise EmbedderUnavailable("The embedding model offline-model couldn't be loaded (no network).")

    embed_query = embed_documents


def test_without_the_model_search_uses_keywords_until_reindexed():
    db = Database()
    offline = Library(db, Offline())
    doc = offline.add("policy.md", b"Returns are accepted within 7 days.")
    assert not doc["semantic"]
    result = offline.search("returns")
    assert result.mode == "keyword" and "couldn't be loaded" in result.note and result.hits

    online = Library(db, HashEmbedder())
    assert online.status()["embedded"] == 0
    assert online.reindex() == 1 and online.status()["embedded"] == 1
    assert online.search("returns").mode == "hybrid"


# --- API ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    app = create_app(Database(), tmp_path, provider=ScriptedProvider([]), checkpointer=InMemorySaver(),
                     embedder=HashEmbedder())
    with TestClient(app) as c:
        yield c


def test_documents_api(client):
    assert client.get("/documents").json()["status"]["documents"] == 0

    r = client.post("/documents", files={"file": ("policy.md", b"# Returns\n\nWithin 7 days with the bill.")})
    assert r.status_code == 201 and r.json()["kind"] == "md" and r.json()["semantic"] is True
    doc_id = r.json()["doc_id"]
    assert client.post("/documents", files={"file": ("again.md", b"# Returns\n\nWithin 7 days with the bill.")}
                       ).status_code == 409
    bad = client.post("/documents", files={"file": ("data.xlsx", b"PK")})
    assert bad.status_code == 422 and "aren't supported" in bad.json()["detail"]

    hits = client.get("/documents/search", params={"q": "return within days"}).json()
    assert hits["mode"] == "hybrid" and hits["hits"][0]["source"] == 'policy.md - "Returns"'
    assert client.get(f"/documents/{doc_id}/passages").json()[0]["section"] == "Returns"
    assert client.get("/documents/999/passages").status_code == 404

    samples = client.post("/documents/samples").json()
    assert len(samples["added"]) == 4 and samples["skipped"] == []
    assert client.post("/documents/samples").json()["skipped"] == sorted(SAMPLES, key=list(SAMPLES).index)
    assert client.get("/documents").json()["status"]["documents"] == 5
    assert client.post("/documents/reindex").json() == {"embedded": 0}

    assert client.delete(f"/documents/{doc_id}").status_code == 200
    assert client.delete(f"/documents/{doc_id}").status_code == 404
