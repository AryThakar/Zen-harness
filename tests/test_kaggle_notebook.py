# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""The Kaggle notebook (kaggle/zen-qwen36-server.ipynb): every cell is Python that compiles, and its router - one
address for both models - sends /embed/... to the search model and the rest to Qwen, the password and Qwen's streamed
answers passing through. The router's own code is taken from the notebook and run here between two stand-in servers."""

from __future__ import annotations

import ast
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = json.loads((ROOT / "kaggle" / "zen-qwen36-server.ipynb").read_text(encoding="utf-8"))
CODE = ["".join(c["source"]) for c in NOTEBOOK["cells"] if c["cell_type"] == "code"]


def test_every_cell_compiles_and_the_tunnel_goes_to_the_router():
    for source in CODE:
        ast.parse(source)
    settings = CODE[0]
    assert 'EMBED_FILE = "embeddinggemma-2-Q8_0.gguf"' in settings  # the file Zen's index names its vectors after
    assert any('f"http://127.0.0.1:{ROUTER_PORT}"' in source and "cloudflared" in source.lower() for source in CODE)


class Stub(BaseHTTPRequestHandler):
    """A llama-server stand-in: answers with what it got (path, password); /v1/chat/completions streams."""
    seen: list = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.answer()

    def do_POST(self):
        self.answer()

    def answer(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.seen.append((self.server.name, self.command, self.path, self.headers.get("Authorization"), body))
        if self.headers.get("Authorization") != "Bearer secret" and self.path != "/health":
            data = b'{"error": {"message": "Invalid API Key"}}'
            self.send_response(401)
        elif self.path == "/v1/chat/completions":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for word in ("Hel", "lo"):
                self.wfile.write(f'data: {{"delta": "{word}"}}\n\n'.encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            return
        else:
            data = json.dumps({"server": self.server.name, "path": self.path}).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(handler, name=""):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.name = name
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def router():
    qwen, embed = serve(Stub, "qwen"), serve(Stub, "embed")
    source = next(s for s in CODE if "class Router(" in s).split("# --- start it ---")[0]
    space = {"json": json, "urllib": urllib, "PORT": qwen.server_address[1], "EMBED_PORT": embed.server_address[1]}
    exec(compile(source, "notebook router", "exec"), space)  # noqa: S102 - the notebook's own code, as Kaggle runs it
    front = serve(space["Router"])
    Stub.seen = []
    yield f"http://127.0.0.1:{front.server_address[1]}", qwen, embed
    for server in (front, qwen, embed):
        server.shutdown()
        server.server_close()


def call(url, body=None, password="secret"):
    headers = {"Content-Type": "application/json"} | ({"Authorization": f"Bearer {password}"} if password else {})
    request = urllib.request.Request(url, json.dumps(body).encode() if body is not None else None, headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, response.read()


def test_the_router_sends_embed_to_the_search_model_and_the_rest_to_qwen(router):
    url, *_ = router
    assert json.loads(call(url + "/embed/embedding", {"content": ["hi"]})[1]) == {"server": "embed", "path": "/embedding"}
    assert json.loads(call(url + "/embed/props")[1]) == {"server": "embed", "path": "/props"}
    assert json.loads(call(url + "/props")[1]) == {"server": "qwen", "path": "/props"}
    assert json.loads(call(url + "/embedding-like/x")[1])["server"] == "qwen"  # only /embed itself is the prefix
    # the password went through as it was, and the body
    name, method, path, password, body = Stub.seen[0]
    assert (name, method, path, password, json.loads(body)) == ("embed", "POST", "/embedding", "Bearer secret", {"content": ["hi"]})


def test_qwen_streams_through_and_refusals_keep_their_status(router):
    url, *_ = router
    status, body = call(url + "/v1/chat/completions", {"stream": True})
    assert status == 200 and body.decode().count("data: ") == 3 and body.decode().endswith("data: [DONE]\n\n")
    with pytest.raises(urllib.error.HTTPError) as refused:
        call(url + "/embed/embedding", {"content": ["x"]}, password="wrong")
    assert refused.value.code == 401 and b"Invalid API Key" in refused.value.read()


def test_a_server_that_is_down_gets_a_clear_502(router):
    url, qwen, embed = router
    embed.shutdown()
    embed.server_close()
    with pytest.raises(urllib.error.HTTPError) as down:
        call(url + "/embed/embedding", {"content": ["x"]})
    assert down.value.code == 502 and b"Nothing answers on the notebook" in down.value.read()


def test_the_last_cell_prints_a_zen_code_that_zen_reads():
    from app import model_link
    line = next(x for x in CODE[-2].splitlines() if x.startswith("ZEN_CODE = "))  # the cell before the one that waits
    names = {"base64": __import__("base64"), "URL": "https://a-b.trycloudflare.com", "API_KEY": "Pass_word-1"}
    exec(line, names)
    assert model_link.code(names["ZEN_CODE"]) == ("https://a-b.trycloudflare.com", "Pass_word-1")
    assert "bridge" not in json.dumps(NOTEBOOK).lower()
