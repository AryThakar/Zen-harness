# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""The Cloud choice - any OpenAI-style API, three things: an address, a key, a model - against a small fake of such an
API (no network). The answers are shaped like the real ones: Groq's and Gemini's (read from their APIs on 2026-10-15)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.assistant.context import prompt_limit
from app.providers import ProviderError
from app.providers.cloud import MAX_TOKENS, SLOT_TOKENS, CloudProvider
from app.tools.net import USER_AGENT

from .test_llamacpp import TOOL_SPECS, delta


class FakeCloud:
    """Answers POSTs with the scripted (status, body) replies in turn; the last one repeats. GETs (the model list)
    answer 200, or ``models`` (status, body) when given."""

    def __init__(self, replies, models=(200, {"data": []})):
        self.requests, self.headers, self.paths = [], [], []
        fake, replies = self, list(replies)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                if status == 429:
                    self.send_header("retry-after", "0")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                fake.paths.append(self.path)
                fake.headers.append(dict(self.headers))
                self._json(*models)

            def do_POST(self):
                fake.paths.append(self.path)
                fake.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                fake.headers.append(dict(self.headers))
                status, body = replies.pop(0) if len(replies) > 1 else replies[0]
                if status != 200:
                    self._json(status, body)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for chunk in body:
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def cloud():
    servers = []

    def make(*replies, **kw):
        servers.append(FakeCloud(replies, **kw))
        return servers[-1]

    yield make
    for s in servers:
        s.server.shutdown()


DONE = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 2000, "completion_tokens": 120, "completion_time": 0.25}}
ASK = [{"role": "user", "content": "q"}]


def test_sends_only_what_a_cloud_takes_with_the_key_and_reads_its_thinking_and_usage(cloud):
    server = cloud((200, [delta(reasoning="Check the "), delta(reasoning="date."), delta(content="Argentina."), DONE]))
    model = CloudProvider("openai/gpt-oss-120b", "k3y", server.url, thinking=True)
    turn = model.complete("system", ASK, TOOL_SPECS)
    assert turn.text == "Argentina." and turn.thinking == "Check the date."
    assert turn.usage["prompt_tokens"] == 2000 and turn.usage["tokens_per_second"] == 480
    body, headers = server.requests[0], server.headers[0]
    assert server.paths[0] == "/v1/chat/completions"  # under the address, which carries the version
    assert not {"cache_prompt", "id_slot", "chat_template_kwargs", "reasoning_budget_tokens"} & body.keys()
    assert body["reasoning_effort"] == "medium" and body["max_tokens"] == MAX_TOKENS and body["stream"] is True
    assert headers["Authorization"] == "Bearer k3y" and headers["User-Agent"] == USER_AGENT  # Groq refuses urllib's
    model.complete("system", ASK, TOOL_SPECS, think=False)
    assert server.requests[1]["reasoning_effort"] == "low"


def test_thinking_is_switched_in_each_models_own_words():
    def fields(model, url="https://api.example.com/v1", thinking=True):
        return CloudProvider(model, "k", url).thinking_fields(thinking)

    assert fields("gpt-4.1") == {} and fields("llama3.2", "http://127.0.0.1:11434/v1") == {}  # most: none
    assert fields("gemini-3.5-flash") == {} and fields("gemini-3.5-flash", thinking=False) == {"reasoning_effort": "low"}
    assert fields("qwen/qwen3-32b", "https://api.groq.com/openai/v1")["reasoning_format"] == "parsed"
    assert fields("qwen/qwen3-32b") == {}  # Qwen elsewhere: as it comes


def test_a_per_minute_limit_is_waited_out_and_a_daily_one_is_said(cloud, monkeypatch):
    waits = []
    monkeypatch.setattr(CloudProvider, "_wait", lambda self, s: waits.append(s))
    groq_minute = (429, {"error": {"message": "Rate limit reached on tokens per minute (TPM): Limit 8000"}})
    gemini_minute = (429, [{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "21s"}]}}])
    server = cloud(groq_minute, gemini_minute, (200, [delta(content="ok"), DONE]))
    assert CloudProvider("m", "k", server.url).complete("s", ASK, []).text == "ok"
    assert len(server.requests) == 3 and waits == [0.0, 21.0]  # its header's wait, then Gemini's own delay
    for said in ("Rate limit reached on requests per day (RPD): Limit 1000",
                 "Quota exceeded for metric: generate_content_free_tier_requests, quotaId: "
                 "GenerateRequestsPerDayPerProjectPerModel-FreeTier"):
        daily = cloud((429, {"error": {"message": said}}))
        with pytest.raises(ProviderError, match="daily limit for gemini-3.5-flash is used up"):
            CloudProvider("gemini-3.5-flash", "k", daily.url).complete("s", ASK, [])
    too_big = cloud((413, {"error": {"message": "Request too large"}}))
    with pytest.raises(ProviderError, match="larger than 127.0.0.1 accepts"):
        CloudProvider("m", "k", too_big.url).complete("s", ASK, [])


def test_two_calls_streamed_whole_stay_two_and_their_signature_goes_back(cloud):
    signed = {"google": {"thought_signature": "c2lnbmVk"}}

    def whole_call(call_id, arguments, sign=False):  # as Gemini streams one: whole, with an id and no index
        piece = {"id": call_id, "type": "function", "function": {"name": "calculate", "arguments": json.dumps(arguments)},
                 **({"extra_content": signed} if sign else {})}
        return {"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [piece]}}]}

    stop = {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": "stop"}],
            "usage": {"completion_tokens": 40, "prompt_tokens": 73, "total_tokens": 254}}
    server = cloud((200, [whole_call("call_1", {"expression": "0.175*2340"}, sign=True),
                          whole_call("call_2", {"expression": "3**7"}), stop]))
    model = CloudProvider("gemini-3.5-flash", "k3y", server.url, thinking=True)
    turn = model.complete("system", [{"role": "user", "content": "both at once"}], TOOL_SPECS)
    assert [c["input"] for c in turn.tool_calls] == [{"expression": "0.175*2340"}, {"expression": "3**7"}]
    assert turn.stop == "tool_use" and turn.usage["prompt_tokens"] == 73
    assert "reasoning_effort" not in server.requests[0]  # thinking on: the model's default
    history = [{"role": "user", "content": "both at once"},
               {"role": "assistant", "text": "", "tool_calls": turn.tool_calls, "raw": turn.raw, "provider": "cloud"},
               {"role": "tool", "tool_call_id": "call_1", "content": "409.5"},
               {"role": "tool", "tool_call_id": "call_2", "content": "2187"}]
    model.complete("system", history, TOOL_SPECS, think=False)
    sent = server.requests[1]["messages"][2]["tool_calls"]
    assert sent[0]["extra_content"] == signed and "extra_content" not in sent[1]  # replayed as it came


def test_prompts_stay_a_turns_size_and_the_key_is_checked_on_the_model_list(cloud):
    server = cloud((200, [DONE]))
    model = CloudProvider("openai/gpt-oss-120b", "k", server.url)
    assert prompt_limit(model) == SLOT_TOKENS - MAX_TOKENS
    assert CloudProvider("m", "k", server.url, slot_tokens=65_536).slot_tokens == 65_536
    assert model.count_tokens("s", ASK, []) > 0  # an estimate: no /tokenize
    assert model.health() == ("ready", "gpt-oss-120b (127.0.0.1)") and server.paths == ["/v1/models"]
    assert CloudProvider("m", "k", "https://api.groq.com/openai/v1").location() == "on groq.com"
    refused = cloud((200, [DONE]), models=(401, {"error": {"message": "Invalid API Key"}}))
    assert CloudProvider("m", "bad", refused.url).health() == (
        "down", "127.0.0.1 refused the API key; check it in Zen's Model settings.")


def test_a_model_that_refuses_a_picture_is_not_sent_one_again(cloud):
    server = cloud((400, {"error": {"message": "This model does not support image input."}}))
    model = CloudProvider("llama-3.3-70b", "k", server.url)
    assert model.sees_images()
    with pytest.raises(ProviderError, match="can't see pictures"):
        model.look(b"\x89PNG", "image/png", "What is it?")
    body = server.requests[0]
    assert not {"cache_prompt", "id_slot", "chat_template_kwargs"} & body.keys()
    assert body["messages"][0]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert model.sees_images() is False
    with pytest.raises(ProviderError, match="can't see pictures"):
        model.look(b"\x89PNG", "image/png", "And now?")
    assert len(server.requests) == 1  # not asked again
