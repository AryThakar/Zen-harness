"""The legacy document library (legacy/rag/library.py) and its API."""

from datetime import date

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.providers import ScriptedProvider
from app.rag.embed import EmbedderUnavailable, HashEmbedder
from legacy.datagen import planted_events
from legacy.db.connection import Database
from legacy.main import create_app
from legacy.rag.library import DuplicateDocument, Library
from legacy.rag.samples import sample_documents

END = date(2026, 9, 26)
SAMPLES = dict(sample_documents(END))


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
