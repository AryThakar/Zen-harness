"""The llama.cpp provider, against a small fake llama-server (no model needed)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.agent.providers import LlamaCppProvider, ProviderError
from app.agent.providers.llamacpp import parse_turn, to_openai_messages
from app.agent.tools import TOOL_SPECS


def tool_result(call_id, content="ok", is_error=False):
    return {"role": "tool", "tool_call_id": call_id, "name": "run_sql", "content": content, "is_error": is_error}


def test_history_replays_calls_exactly_and_marks_errors():
    calls = [{"id": "a", "type": "function", "function": {"name": "run_sql", "arguments": '{"sql": "SELECT 1"}'}},
             {"id": "b", "type": "function", "function": {"name": "calculate", "arguments": '{"expression":"1+1"}'}}]
    history = [{"role": "user", "content": "q"},
               {"role": "assistant", "text": "Checking.", "tool_calls": [], "raw": {"tool_calls": calls},
                "provider": "llamacpp"},
               tool_result("a"), tool_result("b", "bad", True)]
    out = to_openai_messages(history)
    assert out[1] == {"role": "assistant", "content": "Checking.", "tool_calls": calls}  # arguments unchanged
    assert out[2] == {"role": "tool", "tool_call_id": "a", "content": "ok"}
    assert out[3]["content"] == "Error: bad"


def test_history_drops_unrun_calls_and_rebuilds_other_providers():
    history = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "text": "", "tool_calls": [{"id": "x", "name": "run_sql", "input": {}}],
         "raw": [{"type": "tool_use"}], "provider": "anthropic"},  # never ran: nothing left to replay
        {"role": "user", "content": "q2"},
        {"role": "assistant", "text": "", "tool_calls": [{"id": "t", "name": "calculate", "input": {"expression": "2"}}],
         "raw": [], "provider": "anthropic"},
        tool_result("t"),
    ]
    out = to_openai_messages(history)
    assert [m["role"] for m in out] == ["user", "user", "assistant", "tool"]
    assert out[2]["tool_calls"][0]["function"] == {"name": "calculate", "arguments": '{"expression": "2"}'}


def test_truncated_or_tool_less_turns_run_no_tools():
    call = [{"id": "c", "type": "function", "function": {"name": "run_sql", "arguments": '{"sql": "SEL'}}]
    assert parse_turn("", call, "length", True, "m").tool_calls == []
    assert parse_turn("", call, "length", True, "m").stop == "max_tokens"
    assert parse_turn("Hi", call, "tool_calls", False, "m").stop == "end"
    unreadable = parse_turn("", call, "tool_calls", True, "m")
    assert unreadable.stop == "tool_use" and unreadable.tool_calls[0]["input"] == '{"sql": "SEL'


class FakeServer:
    """Answers /health and streams a scripted /v1/chat/completions reply."""

    def __init__(self, chunks=(), status=200, error=None):
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(status)
                self.end_headers()

            def do_POST(self):
                fake.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                if error:
                    body = json.dumps({"error": {"message": error}}).encode()
                    self.send_response(status)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for chunk in chunks:
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def fake():
    servers = []

    def make(**kwargs):
        servers.append(FakeServer(**kwargs))
        return servers[-1]

    yield make
    for s in servers:
        s.close()


def delta(**fields):
    return {"choices": [{"index": 0, "delta": fields, "finish_reason": None}]}


def test_streams_text_and_assembles_tool_calls(fake):
    server = fake(chunks=[
        delta(content="Let me "), delta(content="check."),
        delta(tool_calls=[{"index": 0, "id": "call_1", "type": "function",
                           "function": {"name": "run_sql", "arguments": '{"sql": '}}]),
        delta(tool_calls=[{"index": 0, "function": {"arguments": '"SELECT 1"}'}}]),
        delta(tool_calls=[{"index": 1, "function": {"name": "calculate", "arguments": '{"expression": "2*3"}'}}]),
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
         "timings": {"prompt_n": 3500, "predicted_n": 42, "predicted_per_second": 13.4}},
    ])
    events = []
    provider = LlamaCppProvider("qwen3.5-9b", server.url)
    turn = provider.complete("system", [{"role": "user", "content": "q"}], TOOL_SPECS,
                             on_event=lambda kind, text: events.append((kind, text)))
    assert turn.text == "Let me check." and turn.stop == "tool_use"
    assert [(c["id"], c["name"], c["input"]) for c in turn.tool_calls][0] == ("call_1", "run_sql", {"sql": "SELECT 1"})
    assert turn.tool_calls[1]["input"] == {"expression": "2*3"} and turn.tool_calls[1]["id"].startswith("call_")
    assert events == [("text", "Let me "), ("text", "check.")]
    assert turn.usage == {"output_tokens": 42, "prompt_tokens": 3500, "tokens_per_second": 13.4}

    body = server.requests[0]
    assert body["messages"][0] == {"role": "system", "content": "system"} and body["stream"] is True
    assert body["tools"][0]["function"]["name"] == TOOL_SPECS[0]["name"] and body["tool_choice"] == "auto"
    assert body["chat_template_kwargs"] == {"enable_thinking": False} and body["max_tokens"] == 4096


def test_round_limit_and_thinking_settings(fake):
    server = fake(chunks=[delta(content="Answer."), {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}])
    turn = LlamaCppProvider("m", server.url, thinking=True).complete(
        "s", [{"role": "user", "content": "q"}], TOOL_SPECS, allow_tools=False)
    assert turn.text == "Answer." and turn.stop == "end" and turn.raw is None
    body = server.requests[0]
    assert body["tool_choice"] == "none" and body["chat_template_kwargs"] == {"enable_thinking": True}
    assert body["messages"][-1]["role"] == "user" and "tool limit" in body["messages"][-1]["content"]


def test_tool_calls_written_as_text_are_removed():
    text = "Here is the chart.\n<tool_call>\n<function=make_chart>\n<parameter=x>\nmonth\n</parameter>"
    assert parse_turn(text, [], "stop", False, "m").text == "Here is the chart."
    assert parse_turn("A <tool_call>x</tool_call> B", [], "stop", True, "m").text == "A  B"


def test_errors_become_provider_errors(fake):
    server = fake(status=400, error="the request exceeds the available context size")
    with pytest.raises(ProviderError, match="context size"):
        LlamaCppProvider("m", server.url).complete("s", [{"role": "user", "content": "q"}], [])
    with pytest.raises(ProviderError, match="is llama-server running"):
        LlamaCppProvider("m", "http://127.0.0.1:9").complete("s", [{"role": "user", "content": "q"}], [])


def test_ready_reports_loading_and_missing_server(fake):
    assert LlamaCppProvider("m", fake().url).ready() == (True, "m (local, llama.cpp)")
    loading = LlamaCppProvider("m", fake(status=503).url).ready()
    assert not loading[0] and "loading" in loading[1]
    missing = LlamaCppProvider("m", "http://127.0.0.1:9").ready()
    assert not missing[0] and "Start llama-server" in missing[1]
