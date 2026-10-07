"""The Groq provider, against a small fake of its OpenAI-compatible API (no network)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.assistant.context import prompt_limit
from app.providers import ProviderError
from app.providers.groq import MAX_TOKENS, SLOT_TOKENS, GroqProvider
from app.tools.net import USER_AGENT

from .test_llamacpp import TOOL_SPECS, delta


class FakeGroq:
    """Answers POSTs with the scripted (status, body) replies in turn; the last one repeats."""

    def __init__(self, replies):
        self.requests, self.headers, self.paths = [], [], []
        fake, replies = self, list(replies)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                fake.paths.append(self.path)
                fake.headers.append(dict(self.headers))
                self.send_response(200)
                self.end_headers()

            def do_POST(self):
                fake.paths.append(self.path)
                fake.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                fake.headers.append(dict(self.headers))
                status, body = replies.pop(0) if len(replies) > 1 else replies[0]
                self.send_response(status)
                if status == 429:
                    self.send_header("retry-after", "0")
                if status != 200:
                    data = json.dumps(body).encode()
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for chunk in body:
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def groq():
    servers = []

    def make(*replies):
        servers.append(FakeGroq(replies))
        return servers[-1]

    yield make
    for s in servers:
        s.server.shutdown()


DONE = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 2000, "completion_tokens": 120, "completion_time": 0.25}}


def test_sends_only_what_groq_takes_with_the_key_and_reads_its_thinking_and_usage(groq):
    server = groq((200, [delta(reasoning="Check the "), delta(reasoning="date."), delta(content="Argentina."), DONE]))
    model = GroqProvider("openai/gpt-oss-120b", "k3y", server.url, thinking=True)
    turn = model.complete("system", [{"role": "user", "content": "q"}], TOOL_SPECS)
    assert turn.text == "Argentina." and turn.thinking == "Check the date."
    assert turn.usage["prompt_tokens"] == 2000 and turn.usage["tokens_per_second"] == 480
    body, headers = server.requests[0], server.headers[0]
    assert server.paths[0] == "/v1/chat/completions"  # under the base address, as on llama-server
    assert not {"cache_prompt", "id_slot", "chat_template_kwargs", "reasoning_budget_tokens"} & body.keys()
    assert body["reasoning_effort"] == "medium" and body["max_tokens"] == MAX_TOKENS and body["stream"] is True
    assert headers["Authorization"] == "Bearer k3y" and headers["User-Agent"] == USER_AGENT  # Groq refuses urllib's
    model.complete("system", [{"role": "user", "content": "q"}], TOOL_SPECS, think=False)
    assert server.requests[1]["reasoning_effort"] == "low"


def test_the_per_minute_limit_is_waited_out_and_the_rest_explained(groq):
    limited = (429, {"error": {"message": "Rate limit reached on tokens per minute (TPM): Limit 8000"}})
    server = groq(limited, (200, [delta(content="ok"), DONE]))
    assert GroqProvider("m", "k", server.url).complete("s", [{"role": "user", "content": "q"}], []).text == "ok"
    assert len(server.requests) == 2  # sent again after the wait
    daily = groq((429, {"error": {"message": "Rate limit reached on requests per day (RPD): Limit 1000"}}))
    with pytest.raises(ProviderError, match="daily limit"):
        GroqProvider("m", "k", daily.url).complete("s", [{"role": "user", "content": "q"}], [])
    too_big = groq((413, {"error": {"message": "Request too large"}}))
    with pytest.raises(ProviderError, match="8,000 tokens a minute"):
        GroqProvider("m", "k", too_big.url).complete("s", [{"role": "user", "content": "q"}], [])


def test_prompts_are_kept_within_the_free_plan_and_nothing_local_is_assumed(groq):
    server = groq((200, [DONE]))
    model = GroqProvider("openai/gpt-oss-120b", "k", server.url)
    assert prompt_limit(model) == SLOT_TOKENS - MAX_TOKENS < 8000
    assert model.count_tokens("s", [{"role": "user", "content": "q"}], []) > 0  # an estimate: no /tokenize
    assert model.sees_images() is False and model.look is None  # no vision: images are never sent
    assert model.ready() == (True, "openai/gpt-oss-120b (Groq cloud)") and model.loaded_model() == "gpt-oss-120b"
    assert server.paths == ["/v1/models"]  # the key checked under the base address
    assert GroqProvider("m", "").ready()[0] is False  # no key yet
