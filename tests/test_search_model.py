# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Search by meaning on this computer, set up from Settings: files already here used where they are, the rest
downloaded (from a small stand-in for Hugging Face and GitHub - no network), checked, carried on after a stop, then
named in the Model settings - and used before the notebook's."""

from __future__ import annotations

import dataclasses
import hashlib
import io
import os
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from app import model_link, search_model
from app.assistant.store import Store
from app.connection import Connection, LiveEmbedder
from app.main import create_app
from app.providers import ScriptedProvider
from app.rag.embed import HashEmbedder
from app.rag.embed_server import EmbedServer
from app.search_model import Part, Setup

from .test_model_link import _settings


def _zip() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("llama-server.exe", b"exe")
        z.writestr("ggml-vulkan.dll", b"dll")
        z.writestr("llama-cli.exe", b"not wanted")
        z.writestr("LICENSE-curl", b"not wanted")
    return out.getvalue()


BLOBS = {"/m.gguf": b"model " * 1000, "/mmproj.gguf": b"picture " * 2000, "/llama.zip": _zip()}


class Hub:
    """Serves BLOBS, a Range request as 206 - as Hugging Face and GitHub do."""

    def __init__(self):
        self.requests = []
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                hub.requests.append((self.path, self.headers.get("Range")))
                data = BLOBS[self.path]
                start = int(self.headers["Range"].split("=")[1].rstrip("-")) if self.headers.get("Range") else 0
                self.send_response(206 if start else 200)
                self.send_header("Content-Length", str(len(data) - start))
                self.end_headers()
                self.wfile.write(data[start:])

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def hub(monkeypatch):
    h = Hub()

    def part(name, path):
        data = BLOBS[path]
        return Part(name, h.url + path, len(data), hashlib.sha256(data).hexdigest())

    monkeypatch.setattr(search_model, "MODEL", part("embeddinggemma-2-Q8_0.gguf", "/m.gguf"))
    monkeypatch.setattr(search_model, "PROJECTOR", part("mmproj-embeddinggemma-2-Q8_0.gguf", "/mmproj.gguf"))
    monkeypatch.setattr(search_model, "LLAMA", part("llama-b11515-bin-win-vulkan-x64.zip", "/llama.zip"))
    yield h
    h.server.shutdown()


def _run(setup: Setup) -> dict:
    setup.start()
    setup.wait(20)
    return setup.state()


def test_files_already_here_are_used_where_they_are(tmp_path, hub):
    near = tmp_path / "zen-harness"
    models = near / "models" / "embeddinggemma-2"
    models.mkdir(parents=True)
    (models / "embeddinggemma-2-Q8_0.gguf").write_bytes(BLOBS["/m.gguf"])
    (models / "mmproj-embeddinggemma-2-Q8_0.gguf").write_bytes(BLOBS["/mmproj.gguf"])
    for build in ("llama-b11000", "llama-b11515", "llama-b11600"):  # too old, enough, the newest
        (near / build).mkdir()
        (near / build / "llama-server.exe").write_bytes(b"exe")
    settings = tmp_path / "settings.env"
    setup = Setup(settings, near / "app" / "data" / "models", near)
    assert setup.state() == {"ready": False, "running": False, "done_mb": 0, "total_mb": 0, "step": "", "error": "",
                             "download_mb": 0}  # nothing to download
    assert _run(setup)["ready"] and hub.requests == []
    values = model_link.read(settings)
    assert values["SEARCH_GGUF"] == str(models / "embeddinggemma-2-Q8_0.gguf")
    assert values["SEARCH_SERVER"] == str(near / "llama-b11600" / "llama-server.exe")
    assert search_model.here(values) is not None


def test_the_rest_is_downloaded_checked_and_a_stopped_one_goes_on(tmp_path, hub):
    folder = tmp_path / "data" / "models"
    started = folder / "embeddinggemma-2" / "mmproj-embeddinggemma-2-Q8_0.gguf.part"
    started.parent.mkdir(parents=True)
    started.write_bytes(BLOBS["/mmproj.gguf"][:5000])  # a download stopped part way
    settings = tmp_path / "settings.env"
    setup = Setup(settings, folder)
    assert _run(setup)["ready"]
    assert ("/mmproj.gguf", "bytes=5000-") in hub.requests  # carried on, not started again
    assert (folder / "embeddinggemma-2" / "mmproj-embeddinggemma-2-Q8_0.gguf").read_bytes() == BLOBS["/mmproj.gguf"]
    assert sorted(p.name for p in (folder / "llama.cpp").iterdir()) == ["ggml-vulkan.dll", "llama-server.exe"]
    assert model_link.read(settings)["SEARCH_SERVER"] == str(folder / "llama.cpp" / "llama-server.exe")


def test_a_download_that_doesnt_match_its_checksum_is_not_kept(tmp_path, hub, monkeypatch):
    monkeypatch.setattr(search_model, "MODEL", dataclasses.replace(search_model.MODEL, sha256="0" * 64))
    folder = tmp_path / "models"
    state = _run(Setup(tmp_path / "settings.env", folder))
    assert not state["ready"] and "checksum" in state["error"]
    assert not list((folder / "embeddinggemma-2").glob("embeddinggemma-2-Q8_0.gguf*"))
    assert not (tmp_path / "settings.env").exists()  # nothing named


def test_kaggle_searches_on_the_notebook_and_the_others_on_this_computer(tmp_path, monkeypatch):
    files = {}
    for key, name in (("SEARCH_GGUF", "embeddinggemma-2-Q8_0.gguf"), ("SEARCH_MMPROJ", "mm.gguf"),
                      ("SEARCH_SERVER", "llama-server.exe")):
        (tmp_path / name).write_bytes(b"x")
        files[key] = str(tmp_path / name)
    path = tmp_path / "model.env"
    model_link.write(path, {"ZEN_MODEL": "kaggle", "KAGGLE_URL": "https://a.trycloudflare.com", "KAGGLE_PASSWORD": "p",
                            **files})
    settings = _settings(embed_url="http://127.0.0.1:8070", assistant_dir=str(tmp_path))
    search = LiveEmbedder(Connection(path, settings))
    assert search.url == "https://a.trycloudflare.com/embed"  # Kaggle: the notebook's - nothing runs here
    for kind in ("local", "cloud"):
        model_link.write(path, {"ZEN_MODEL": kind, "CLOUD_URL": "http://127.0.0.1:11434/v1", "CLOUD_MODEL": "m"})
        os.utime(path, (time.time() + 5, time.time() + 5))
        assert search.url == "http://127.0.0.1:8070"  # this computer's
        assert search.name == "embeddinggemma-2-Q8_0"  # as the notebook's: the same vectors
    closed = []
    monkeypatch.setattr(EmbedServer, "close", lambda self: closed.append(self.port))
    model_link.write(path, {"ZEN_MODEL": "kaggle"})
    os.utime(path, (time.time() + 9, time.time() + 9))
    assert search.url == "https://a.trycloudflare.com/embed" and closed == [8070]  # back on Kaggle: this PC's stopped


def test_only_zens_page_starts_a_setup(tmp_path, hub, monkeypatch):
    store = Store(tmp_path / "assistant", HashEmbedder())
    with TestClient(create_app(assistant_parts=(store, ScriptedProvider([]), ScriptedProvider([])))) as c:
        c.app.state.search_setup = Setup(tmp_path / "settings.env", tmp_path / "models")
        assert c.get("/assistant/search-model").json()["download_mb"] == 0  # the stand-in's files: a few KB
        refused = c.post("/assistant/search-model", json={"action": "start"}, headers={"Origin": "https://evil.example"})
        assert refused.status_code == 403 and not (tmp_path / "models").exists()
        c.post("/assistant/search-model", json={"action": "start"})
        c.app.state.search_setup.wait(20)
        assert c.get("/assistant/search-model").json()["ready"]
    store.close()
