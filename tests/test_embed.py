"""Search by meaning with an embedding server: ServerEmbedder against a stand-in llama-server (its prompts,
batches, pictures, failures), re-indexing when the model changes, each model's own cut-off, pictures in the
index and the store keeping it current - and, when llama-server and EmbeddingGemma 2 are on this computer,
the real server started by the app."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.assistant.store import Store
from app.config import settings
from app.rag import embed
from app.rag.embed import EmbedderUnavailable, HashEmbedder, ServerEmbedder
from app.rag.embed_server import EmbedServer
from app.tools import approval
from app.tools.files import index_items, search_files
from app.tools.search_index import Passage, SearchIndex
from app.tools.workspace import Workspace

MARKER = "<__media_test__>"


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass


class FakeLlamaServer:
    """/health, /props and /embedding the way llama-server answers them. A text's vector comes from its
    words (HashEmbedder), a picture's from its bytes - so tests can check what was sent."""

    def __init__(self, vision=True, port=0, model_path="C:/models/embeddinggemma-2-Q8_0.gguf", fail=None):
        self.requests: list[dict] = []
        self.words = HashEmbedder(64)
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/health":
                    self._send(200, {"status": "ok"})
                elif self.path == "/props":
                    self._send(200, {"media_marker": MARKER, "model_path": model_path,
                                     "modalities": {"vision": vision}})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(body)
                if fail:
                    self._send(400, {"error": {"message": fail}})
                    return
                out = []
                for i, item in enumerate(body["content"]):
                    if isinstance(item, dict):
                        seed = int(hashlib.md5(base64.b64decode(item["multimodal_data"][0])).hexdigest(), 16) % 2**32
                        v = np.random.default_rng(seed).normal(size=64)
                    else:
                        v = fake.words.embed_query(item) * 3  # not normalised: the client normalises
                    out.append({"index": i, "embedding": [v.tolist()]})
                self._send(200, out[::-1])  # out of order: the client sorts by index

        self.server = QuietServer(("127.0.0.1", port), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def server():
    s = FakeLlamaServer()
    yield s
    s.close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def png(color, size=(40, 30), fmt="PNG") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, fmt)
    return out.getvalue()


# ---------------------------------------------------------------- the embedder

def test_prompts_are_the_models_own_and_vectors_normalised(server):
    e = ServerEmbedder(server.url, "embeddinggemma-2-Q8_0")
    q = e.embed_query("refund rules")
    docs = e.embed_documents(["Opened phones can be returned.", "Invoices are paid in 45 days."], ["policy.md", None])
    assert server.requests[0]["content"] == ["task: search result | query: refund rules"]
    assert server.requests[1]["content"] == ["title: policy.md | text: Opened phones can be returned.",
                                             "title: none | text: Invoices are paid in 45 days."]
    assert np.allclose(np.linalg.norm(docs, axis=1), 1) and np.isclose(np.linalg.norm(q), 1)
    # the answer came back reversed: each vector still belongs to its own text
    assert np.allclose(docs[1], HashEmbedder(64).embed_query("title: none | text: Invoices are paid in 45 days."))
    assert (e.min_similarity, e.min_similarity_pictures) == (0.68, 0.64)


def test_another_model_gets_plain_text_and_the_default_cut_off(server):
    e = ServerEmbedder(server.url, "nomic-embed")
    e.embed_query("refund rules")
    assert server.requests[0]["content"] == ["refund rules"]
    assert not hasattr(e, "min_similarity")


def test_texts_go_in_batches(server):
    e = ServerEmbedder(server.url, "embeddinggemma-2")
    vectors = e.embed_documents([f"passage {i}" for i in range(20)])
    assert [len(r["content"]) for r in server.requests] == [16, 4]
    assert vectors.shape == (20, 64)


def test_pictures_are_sent_with_the_servers_marker_and_no_prompt(server):
    e = ServerEmbedder(server.url, "embeddinggemma-2")
    assert e.images
    a, b = png("red"), png("blue")
    vectors = e.embed_images([a, b])
    assert vectors.shape == (2, 64) and not np.allclose(vectors[0], vectors[1])
    sent = server.requests[0]["content"][0]
    assert sent == {"prompt_string": MARKER, "multimodal_data": [base64.b64encode(a).decode()]}


def test_no_projector_no_pictures():
    s = FakeLlamaServer(vision=False)
    try:
        e = ServerEmbedder(s.url, "embeddinggemma-2")
        assert not e.images
        with pytest.raises(EmbedderUnavailable, match="no picture projector"):
            e.embed_images([png("red")])
    finally:
        s.close()


def test_unreachable_server_waits_before_trying_again(monkeypatch):
    calls = []
    e = ServerEmbedder(f"http://127.0.0.1:{free_port()}", "embeddinggemma-2")
    real = e._send
    monkeypatch.setattr(e, "_send", lambda req: calls.append(1) or real(req))
    with pytest.raises(EmbedderUnavailable, match="not available"):
        e.embed_query("x")
    with pytest.raises(EmbedderUnavailable, match="not reachable"):
        e.embed_query("x")
    assert len(calls) == 1  # the second call didn't knock on a closed door
    monkeypatch.setattr(embed, "RETRY_AFTER", 0)
    with pytest.raises(EmbedderUnavailable):
        e.embed_query("x")
    assert len(calls) == 2


def test_a_refused_input_is_not_a_missing_server():
    s = FakeLlamaServer(fail="input is too large")
    try:
        e = ServerEmbedder(s.url, "embeddinggemma-2")
        with pytest.raises(EmbedderUnavailable, match="refused the request: .*too large"):
            e.embed_query("x")
        assert e._failed_at == 0.0  # the server is there: the next text is tried at once
        with pytest.raises(EmbedderUnavailable, match="refused"):
            e.embed_documents(["y"])
        assert len(s.requests) == 2
    finally:
        s.close()


def test_nothing_listening_starts_the_server_then_tries_again():
    port = free_port()
    started = []

    def start():
        started.append(FakeLlamaServer(port=port))

    e = ServerEmbedder(f"http://127.0.0.1:{port}", "embeddinggemma-2", start=start)
    try:
        assert e.embed_query("hello").shape == (64,)
        e.embed_query("again")
        assert len(started) == 1  # running now: not started twice
    finally:
        for s in started:
            s.close()


def test_a_start_that_fails_means_keywords_only():
    def start():
        raise RuntimeError("llama-server not found")

    e = ServerEmbedder(f"http://127.0.0.1:{free_port()}", "embeddinggemma-2", start=start)
    with pytest.raises(EmbedderUnavailable, match="llama-server not found"):
        e.embed_query("x")


def test_settings_without_an_embedding_server_get_the_cpu_model():
    """The legacy product passes its own settings, which know nothing of the embedding server."""
    from types import SimpleNamespace

    from app.rag.embed import FastEmbedder, make_embedder
    e = make_embedder(SimpleNamespace(embed_model="BAAI/bge-small-en-v1.5", embed_cache_dir="data/models"))
    assert isinstance(e, FastEmbedder) and e.name == "BAAI/bge-small-en-v1.5"


# ---------------------------------------------------------------- the server the app runs

def test_server_command_listens_on_this_computer_with_the_projector(tmp_path):
    s = EmbedServer("llama-server.exe", "m.gguf", "mm.gguf", "http://127.0.0.1:8070", tmp_path / "log")
    cmd = s.command()
    assert cmd[:4] == ["llama-server.exe", "-m", "m.gguf", "--embeddings"]
    assert cmd[cmd.index("--host") + 1] == "127.0.0.1" and cmd[cmd.index("--port") + 1] == "8070"
    assert cmd[cmd.index("--mmproj") + 1] == "mm.gguf" and "--fit" in cmd
    assert "-ngl" in s.command(gpu=False) and "--fit" not in s.command(gpu=False)


def test_a_server_already_answering_with_this_model_is_used(tmp_path):
    fake = FakeLlamaServer()
    try:
        s = EmbedServer("missing.exe", "C:/elsewhere/embeddinggemma-2-Q8_0.gguf", "", fake.url, tmp_path / "log")
        s.start()  # no launch: missing.exe would have failed
        assert s._proc is None
    finally:
        fake.close()


def test_another_model_at_the_address_is_refused(tmp_path):
    fake = FakeLlamaServer(model_path="C:/models/gemma-4-12b.gguf")
    try:
        s = EmbedServer("missing.exe", "embeddinggemma-2-Q8_0.gguf", "", fake.url, tmp_path / "log")
        with pytest.raises(RuntimeError, match="serves another model"):
            s.start()
    finally:
        fake.close()


def test_missing_files_are_named(tmp_path):
    s = EmbedServer(str(tmp_path / "llama-server.exe"), "m.gguf", "", f"http://127.0.0.1:{free_port()}", tmp_path / "log")
    with pytest.raises(RuntimeError, match="llama-server not found"):
        s.start()


# ---------------------------------------------------------------- the index

class Words(HashEmbedder):
    def __init__(self, dim, name=None, fail_after=None, **floors):
        super().__init__(dim)
        self.name = name or self.name
        self.fail_after, self.calls = fail_after, 0
        self.__dict__.update(floors)

    def embed_documents(self, texts, titles=None):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise EmbedderUnavailable("down")
        return super().embed_documents(texts, titles)


def test_a_new_model_reindexes_every_text_passage(tmp_path):
    path = tmp_path / "search.sqlite"
    old = SearchIndex(path, Words(64, "old", min_similarity=0.1))
    old.add("document", "file_1", "policy.md", [Passage("Opened phones are returned within 14 days.")])
    old.add("note", "note_1", "Fiscal year", [Passage("Our fiscal year starts on 1 April.")])
    new = SearchIndex(path, Words(128, "new", min_similarity=0.1))
    assert new.stale() == 2
    hits, used = new.search("fiscal year start", ("note",))
    assert used and hits and not hits[0].meaning  # the old vectors aren't compared: keywords found it
    assert new.reindex() == 2 and new.stale() == 0
    hits, _ = new.search("fiscal year start", ("note",))
    assert hits[0].meaning


def test_reindex_stops_when_the_model_goes_and_resumes_later(tmp_path):
    path = tmp_path / "search.sqlite"
    SearchIndex(path, None).add("chat", "c1", None, [Passage(f"turn {i} about budgets") for i in range(5)])
    flaky = Words(64, "new", fail_after=1)
    index = SearchIndex(path, flaky)
    assert index.reindex(batch=2) == 2 and index.stale() == 3
    flaky.fail_after = None
    assert index.reindex(batch=2) == 3 and index.stale() == 0


def test_each_model_has_its_own_cut_off(tmp_path):
    index = SearchIndex(tmp_path / "search.sqlite", Words(64, "strict", min_similarity=0.99))
    index.add("document", "file_1", "a.md", [Passage("Opened phones are returned within 14 days.")])
    assert index._meaning("returned phones opened", ("document",))[0] == []
    index.embedder.min_similarity = 0.1
    assert index._meaning("returned phones opened", ("document",))[0]


class Pictures(Words):
    """Words for text; a picture's vector is the vector of the word drawn into its file name - set by the
    test through self.shows - so a question about that word finds it."""
    images = True

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.shows: dict[bytes, str] = {}
        self.sent: list[bytes] = []

    def embed_images(self, images):
        self.sent += images
        return np.vstack([self.embed_query(self.shows.get(_pixels(b), "nothing")) for b in images])


def _pixels(data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as im:
        return im.convert("RGB").tobytes()


@pytest.fixture
def pictured(tmp_path):
    ws = Workspace(tmp_path / "ws")
    e = Pictures(64, "pictures", min_similarity=0.5, min_similarity_pictures=0.3)
    index = SearchIndex(tmp_path / "ws" / "search.sqlite", e)
    return ws, index, e


def test_pictures_are_found_by_what_they_show(pictured):
    ws, index, e = pictured
    red = png("red")
    e.shows[_pixels(red)] = "lion standing savanna"
    ws.add_image(red, "image.png")
    assert index_items(ws, index) == 1
    r = search_files(index, "a lion in the savanna")
    found = r.result["passages"][0]
    assert found["source"] == "picture 'image.png' (image_1)" and "view_image" in found["text"]
    assert index_items(ws, index) == 0  # nothing changed: nothing embedded again


def test_pictures_follow_changes_renames_and_deletes(pictured):
    ws, index, e = pictured
    item = ws.add_image(png("red"), "first.png", meta={"prompt": "a red square"})
    index_items(ws, index)
    assert index.stamps("image")[item][2] == "first.png"
    ws.rename(item, "second.png")
    assert index_items(ws, index) == 1 and index.stamps("image")[item][2] == "second.png"
    time.sleep(0.01)
    hits, _ = index.search("red square", ("image",))
    assert hits and "a red square" in hits[0].text  # its prompt, for keywords
    ws.replace_image(item, png("blue"))
    assert index_items(ws, index) == 1
    assert _pixels(e.sent[-1]) == _pixels(png("blue"))
    assert index.search("red square", ("image",))[0] == []  # a new picture: the old prompt went with the old one
    approval.delete(ws, index, [item])
    assert index.stamps("image") == {}


def test_a_webp_picture_is_sent_as_png(pictured):
    ws, index, e = pictured
    ws.add_image(png("green", fmt="WEBP"), "photo.webp")
    index_items(ws, index)
    assert e.sent and e.sent[0][:8] == b"\x89PNG\r\n\x1a\n"


def test_without_a_picture_model_pictures_are_found_by_name(tmp_path):
    ws = Workspace(tmp_path / "ws")
    index = SearchIndex(tmp_path / "ws" / "search.sqlite", HashEmbedder())
    ws.add_image(png("red"), "panda on a branch.png")
    index_items(ws, index)
    hits, _ = index.search("panda", ("image",))
    assert hits and hits[0].keyword


def test_tables_are_found_by_their_columns_and_values(tmp_path):
    import pandas as pd
    ws = Workspace(tmp_path / "ws")
    index = SearchIndex(tmp_path / "ws" / "search.sqlite", HashEmbedder())
    item = ws.add_table(pd.DataFrame({"region": ["North", "South"], "sales": [10, 20]}), "shop_sales.csv")
    ws.add_table(pd.DataFrame({"city": ["Pune"], "temperature": [31]}), "weather.csv")
    assert index_items(ws, index) == 2 and index_items(ws, index) == 0
    r = search_files(index, "sales by region")
    first = r.result["passages"][0]
    assert first["source"] == f"table 'shop_sales.csv' ({item})"
    assert "Columns: region, sales" in first["text"] and "North, South" in first["text"]
    assert "analyze_or_code" in first["text"]
    ws.rename(item, "q1_sales.csv")
    assert index_items(ws, index) == 1 and index.stamps("table")[item][2] == "q1_sales.csv"
    approval.delete(ws, index, [item])
    assert item not in index.stamps("table")


def test_store_reindexes_in_the_background_after_a_change_of_model(tmp_path):
    root = tmp_path / "assistant"
    old = Store(root, Words(64, "old"))
    ws_id = old.create_workspace("w")["id"]
    _, index = old.open(ws_id)
    index.add("note", "note_1", "Tea", [Passage("Ary drinks tea at 8 A.M.")])
    old.close()
    new = Store(root, Words(128, "new"))
    _, index = new.open(ws_id)  # opening starts the re-index
    deadline = time.monotonic() + 10
    while index.stale() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert index.stale() == 0
    new.close()


# ---------------------------------------------------------------- the real server, when it is here

REAL_EXE, REAL_GGUF, REAL_MMPROJ = settings.embed_server, settings.embed_gguf, settings.embed_mmproj  # from .env


@pytest.mark.skipif(not (Path(REAL_EXE).is_file() and Path(REAL_GGUF).is_file()),
                    reason="llama-server and EmbeddingGemma 2 are not set up on this computer")
def test_the_real_server_starts_embeds_text_and_pictures_and_stops(tmp_path):
    url = f"http://127.0.0.1:{free_port()}"
    server = EmbedServer(REAL_EXE, REAL_GGUF, REAL_MMPROJ, url, tmp_path / "embed.log")
    e = ServerEmbedder(url, Path(REAL_GGUF).stem, start=server.start)
    try:
        q = e.embed_query("what is the refund period for opened phones?")
        right, wrong = e.embed_documents(["Opened phones can be returned within 14 days.",
                                          "The fiscal year starts on 1 April."], ["policy", "finance"])
        assert q.shape == (768,) and q @ right > q @ wrong
        assert server._proc is not None and server._proc.poll() is None
        if REAL_MMPROJ:
            red, green = e.embed_images([png("red", (64, 64)), png("green", (64, 64))])
            assert e.embed_query("a plain red square") @ red > e.embed_query("a plain red square") @ green
    finally:
        server.close()
    assert server._proc is None
