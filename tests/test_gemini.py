# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""The Gemini provider, against a fake of its OpenAI-compatible API shaped like the real one's answers (read from
the API on 2026-10-15; no network here)."""

import json

import pytest

from app.assistant.context import prompt_limit
from app.providers import ProviderError
from app.providers.gemini import MAX_TOKENS, SLOT_TOKENS, GeminiProvider

from .test_groq import FakeGroq
from .test_llamacpp import TOOL_SPECS


@pytest.fixture
def gemini():
    servers = []

    def make(*replies):
        servers.append(FakeGroq(replies))
        return servers[-1]

    yield make
    for s in servers:
        s.server.shutdown()


SIGNED = {"google": {"thought_signature": "c2lnbmVk"}}


def whole_call(call_id: str, name: str, arguments: dict, signed: bool = False) -> dict:
    """One call as Gemini streams it: whole, in one piece, with an id and no index."""
    piece = {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)},
             **({"extra_content": SIGNED} if signed else {})}
    return {"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [piece]}}],
            "usage": {"completion_tokens": 40, "prompt_tokens": 73, "total_tokens": 254}}


STOP = {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": "stop"}],
        "usage": {"completion_tokens": 40, "prompt_tokens": 73, "total_tokens": 254}}


def test_two_calls_streamed_whole_stay_two_and_their_signature_goes_back(gemini):
    server = gemini((200, [whole_call("call_1", "calculate", {"expression": "0.175*2340"}, signed=True),
                           whole_call("call_2", "calculate", {"expression": "3**7"}), STOP]))
    model = GeminiProvider("gemini-3.5-flash", "k3y", server.url, thinking=True)
    turn = model.complete("system", [{"role": "user", "content": "both at once"}], TOOL_SPECS)
    assert [c["input"] for c in turn.tool_calls] == [{"expression": "0.175*2340"}, {"expression": "3**7"}]
    assert turn.stop == "tool_use" and turn.usage["prompt_tokens"] == 73
    assert server.paths[0] == "/chat/completions" and server.headers[0]["Authorization"] == "Bearer k3y"
    body = server.requests[0]
    assert not {"cache_prompt", "id_slot", "chat_template_kwargs", "reasoning_budget_tokens"} & body.keys()
    assert "reasoning_effort" not in body and body["max_tokens"] == MAX_TOKENS  # thinking on: the model's default
    history = [{"role": "user", "content": "both at once"},
               {"role": "assistant", "text": "", "tool_calls": turn.tool_calls, "raw": turn.raw, "provider": "gemini"},
               {"role": "tool", "tool_call_id": "call_1", "content": "409.5"},
               {"role": "tool", "tool_call_id": "call_2", "content": "2187"}]
    model.complete("system", history, TOOL_SPECS, think=False)
    sent = server.requests[1]["messages"][2]["tool_calls"]
    assert sent[0]["extra_content"] == SIGNED and "extra_content" not in sent[1]  # replayed as it came
    assert server.requests[1]["reasoning_effort"] == "low"


def test_a_per_minute_limit_waits_the_delay_gemini_gives_and_a_daily_one_is_said(gemini, monkeypatch):
    waits = []
    monkeypatch.setattr(GeminiProvider, "_wait", lambda self, s: waits.append(s))
    minute = (429, [{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "21s"}]}}])
    server = gemini(minute, (200, [{"choices": [{"index": 0, "delta": {"content": "ok"}}]}, STOP]))
    assert GeminiProvider("m", "k", server.url).complete("s", [{"role": "user", "content": "q"}], []).text == "ok"
    assert waits == [21.0]
    daily = gemini((429, [{"error": {"code": 429, "message": "Quota exceeded for metric: "
                                     "generate_content_free_tier_requests, quotaId: "
                                     "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}}]))
    with pytest.raises(ProviderError, match="daily limit for gemini-3.5-flash is used up"):
        GeminiProvider("gemini-3.5-flash", "k", daily.url).complete("s", [{"role": "user", "content": "q"}], [])


def test_the_key_is_checked_on_its_model_list_and_it_sees_images(gemini):
    server = gemini((200, [STOP]))
    model = GeminiProvider("gemini-3.5-flash", "k", server.url)
    assert model.health() == ("ready", "gemini-3.5-flash (Gemini API)") and server.paths == ["/models"]
    assert model.ready()[0] and model.sees_images() and prompt_limit(model) == SLOT_TOKENS - MAX_TOKENS
    assert GeminiProvider("m", "k", server.url, slot_tokens=65_536).slot_tokens == 65_536
    assert GeminiProvider("m", "").health() == ("down", "Add GEMINI_API_KEY to the .env file, then restart the app.")


def test_a_question_about_a_picture_sends_only_what_gemini_takes(gemini):
    server = gemini((200, {"choices": [{"message": {"content": "A bar chart."}}]}))
    server_reply = GeminiProvider("m", "k", server.url)
    with pytest.raises(Exception):  # the fake streams; the answer's shape is not the point here
        server_reply.look(b"\x89PNG", "image/png", "What is it?")
    body = server.requests[0]
    assert server.paths[0] == "/chat/completions"
    assert not {"cache_prompt", "id_slot", "chat_template_kwargs"} & body.keys()
    assert body["messages"][0]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")
