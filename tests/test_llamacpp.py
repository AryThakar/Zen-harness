# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""The llama.cpp provider, against a small fake llama-server (no model needed)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.providers import LlamaCppProvider, ProviderError
from app.providers.llamacpp import REPLY_MARGIN, _read_stream, parse_turn, to_openai_messages
from app.assistant.registry import TOOLS

TOOL_SPECS = [TOOLS[n].spec() for n in ("calculate", "get_weather")]


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


def test_a_steps_thinking_goes_back_with_it_and_a_cloud_drops_it():
    """Qwen 3.6's template shows the turn's earlier steps with their thinking; given none, each showed an empty
    <think></think> - its plan lost between tool calls (and Qwen's guide: dropping it degrades accuracy)."""
    from app.providers.cloud import CloudProvider
    calls = [{"id": "a", "type": "function", "function": {"name": "calculate", "arguments": '{"expression": "2"}'}}]
    history = [{"role": "user", "content": "q"},
               {"role": "assistant", "text": "", "tool_calls": [], "raw": {"tool_calls": calls}, "provider": "llamacpp",
                "thinking": "The user wants 2; calculate it, then answer."},
               tool_result("a")]
    sent = to_openai_messages(history)[1]
    assert sent["reasoning_content"] == "The user wants 2; calculate it, then answer." and sent["tool_calls"] == calls
    cloud = CloudProvider("gemini-3.5-flash", "k", "http://127.0.0.1:9")._body("s", history, [], True)
    assert all("reasoning_content" not in m for m in cloud["messages"])


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
    assert parse_turn("", call, "length", True, "m").cut_call == {"name": "run_sql", "chars": 12}
    assert parse_turn("Thought, then", [], "length", True, "m").cut_call is None  # cut while thinking or saying


def test_a_call_being_written_is_reported_as_it_grows():
    """A long file written into a call takes minutes: the page shows it growing, not a line that seems stuck."""
    content = "x" * 1_000
    args = json.dumps({"mode": "write", "name": "game.html", "content": content})
    pieces = [args[i:i + 50] for i in range(0, len(args), 50)]
    lines = [f'data: {json.dumps({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "files" if not i else "", "arguments": piece}}]}}]})}'.encode()
             for i, piece in enumerate(pieces)] + [b"data: [DONE]"]
    events = []
    _read_stream(lines, lambda kind, text: events.append((kind, json.loads(text))))
    assert [kind for kind, _ in events] == ["writing"] * len(events) and len(events) == 3  # every 400 characters
    assert events[0][1] == {"name": "files", "chars": 50, "file": "game.html"} and events[-1][1]["chars"] == 850


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):  # a client that saw a cut connection: expected here
        pass


class FakeServer:
    """Answers /health and streams a scripted /v1/chat/completions reply."""

    def __init__(self, chunks=(), status=200, error=None, props=None, cut=False, garbled=False, fail_first=0,
                 page=None):  # page: an HTML error page, as a proxy or tunnel sends it
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1" if cut else "HTTP/1.0"

            def log_message(self, *args):
                pass

            def do_GET(self):
                if page is not None:
                    return self.page()
                body = json.dumps(props).encode() if props is not None and self.path == "/props" else b""
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def page(self):
                body = page.encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=UTF-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                fake.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                if page is not None:
                    return self.page()
                if error or len(fake.requests) <= fail_first:
                    body = json.dumps({"error": {"message": error or "The Kaggle server can't be reached"}}).encode()
                    self.send_response(status)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                if cut:  # chunked, then the connection drops part way through a chunk (a tunnel going down)
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    first = f"data: {json.dumps(chunks[0])}\n\n".encode()
                    self.wfile.write(f"{len(first):x}\r\n".encode() + first + b"\r\n" + b"64\r\ndata: {")
                    self.wfile.flush()
                    self.close_connection = True
                    return
                self.end_headers()
                if garbled:
                    self.wfile.write(b"data: {not json\n\n")
                for chunk in chunks:
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")

        self.server = QuietServer(("127.0.0.1", 0), Handler)
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
         "timings": {"prompt_n": 3500, "cache_n": 1200, "predicted_n": 42, "predicted_per_second": 13.4}},
    ])
    events = []
    provider = LlamaCppProvider("qwen3.5-9b", server.url)
    turn = provider.complete("system", [{"role": "user", "content": "q"}], TOOL_SPECS,
                             on_event=lambda kind, text: events.append((kind, text)))
    assert turn.text == "Let me check." and turn.stop == "tool_use"
    assert [(c["id"], c["name"], c["input"]) for c in turn.tool_calls][0] == ("call_1", "run_sql", {"sql": "SELECT 1"})
    assert turn.tool_calls[1]["input"] == {"expression": "2*3"} and turn.tool_calls[1]["id"].startswith("call_")
    assert [e for e in events if e[0] == "text"] == [("text", "Let me "), ("text", "check.")]
    assert [json.loads(t)["name"] for kind, t in events if kind == "writing"] == ["run_sql", "calculate"]
    assert turn.usage == {"output_tokens": 42, "prompt_tokens": 3500, "cached_tokens": 1200, "tokens_per_second": 13.4,
                          "thinking_chars": 0}

    body = server.requests[0]
    assert body["messages"][0] == {"role": "system", "content": "system"} and body["stream"] is True
    assert body["tools"][0]["function"]["name"] == TOOL_SPECS[0]["name"] and body["tool_choice"] == "auto"
    assert body["chat_template_kwargs"] == {"enable_thinking": False, "reasoning_effort": "low"} and body["max_tokens"] == 4096


def test_round_limit_and_thinking_settings(fake):
    server = fake(chunks=[delta(content="Answer."), {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}])
    turn = LlamaCppProvider("m", server.url, thinking=True).complete(
        "s", [{"role": "user", "content": "q"}], TOOL_SPECS, allow_tools=False)
    assert turn.text == "Answer." and turn.stop == "end" and turn.raw is None
    body = server.requests[0]
    assert body["tool_choice"] == "none" and body["chat_template_kwargs"] == {"enable_thinking": True, "reasoning_effort": "medium"}
    assert body["messages"][-1]["role"] == "user" and "tool limit" in body["messages"][-1]["content"]
    assert "reasoning_budget_tokens" not in body  # no thinking budget by default (the user's call)


def test_a_call_can_cap_its_own_thinking_or_switch_it_off(fake):
    done = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    server = fake(chunks=[delta(reasoning_content="Let me "), delta(reasoning_content="think."),
                          delta(content="Answer."), done])
    model = LlamaCppProvider("m", server.url, thinking=True)
    turn = model.complete("s", [{"role": "user", "content": "q"}], TOOL_SPECS, budget=2048)
    assert server.requests[0]["reasoning_budget_tokens"] == 2048
    assert turn.text == "Answer." and turn.thinking == "Let me think."  # kept apart, never in the answer
    model.complete("s", [{"role": "user", "content": "q"}], TOOL_SPECS, think=False)
    body = server.requests[1]
    assert body["chat_template_kwargs"] == {"enable_thinking": False, "reasoning_effort": "low"} and "reasoning_budget_tokens" not in body


def test_tool_calls_written_as_text_are_removed():
    text = "Here is the chart.\n<tool_call>\n<function=make_chart>\n<parameter=x>\nmonth\n</parameter>"
    assert parse_turn(text, [], "stop", False, "m").text == "Here is the chart."
    assert parse_turn("A <tool_call>x</tool_call> B", [], "stop", True, "m").text == "A  B"


def test_errors_become_provider_errors(fake):
    server = fake(status=400, error="the request exceeds the available context size")
    with pytest.raises(ProviderError, match="context size"):
        LlamaCppProvider("m", server.url).complete("s", [{"role": "user", "content": "q"}], [])
    with pytest.raises(ProviderError, match="isn't running"):
        LlamaCppProvider("m", "http://127.0.0.1:9").complete("s", [{"role": "user", "content": "q"}], [])


def test_a_connection_that_fails_before_anything_streams_is_tried_again(fake):
    """The Kaggle bridge's 502 when the tunnel drops a request: 2 of the first 5 acceptance jobs failed on one."""
    done = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    server = fake(chunks=[delta(content="Answer."), done], status=502, fail_first=1)
    turn = LlamaCppProvider("m", server.url).complete("s", [{"role": "user", "content": "q"}], [])
    assert turn.text == "Answer." and len(server.requests) == 2


def test_only_a_failed_connection_is_retried_and_never_for_ever(fake):
    down = fake(status=502, error="The Kaggle server can't be reached")
    with pytest.raises(ProviderError, match="can't be reached right now"):
        LlamaCppProvider("m", down.url).complete("s", [{"role": "user", "content": "q"}], [])
    assert len(down.requests) == 3  # the first try and two more
    refused = fake(status=400, error="the request exceeds the available context size")
    with pytest.raises(ProviderError, match="context size"):
        LlamaCppProvider("m", refused.url).complete("s", [{"role": "user", "content": "q"}], [])
    assert len(refused.requests) == 1  # the server answered: its error stands
    stopped = fake(status=502, error="down")
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ProviderError):
        LlamaCppProvider("m", stopped.url, cancel=cancel).complete("s", [{"role": "user", "content": "q"}], [])
    assert len(stopped.requests) == 1  # the user pressed Stop: no retry


def test_an_answer_cut_off_part_way_is_a_provider_error(fake):
    """The Kaggle bridge's kind of failure: the stream stops in the middle of a chunk
    (http.client.IncompleteRead) - before, it was no ProviderError, so main said 'Something went wrong'
    and a helper 'stopped with an internal error'. A garbled line is the same kind of failure."""
    server = fake(chunks=[delta(content="The answer")], cut=True)
    with pytest.raises(ProviderError, match="dropped in the middle of the answer"):
        LlamaCppProvider("m", server.url).complete("s", [{"role": "user", "content": "q"}], [])
    server = fake(chunks=[delta(content="x")], garbled=True)
    with pytest.raises(ProviderError, match="could not be read"):
        LlamaCppProvider("m", server.url).complete("s", [{"role": "user", "content": "q"}], [])


def test_the_slot_size_comes_from_the_server():
    """The prompt limit follows the server as it runs (16K on a laptop, 32K per slot on Kaggle)."""
    from app.assistant.context import prompt_limit
    server = FakeServer(props={"model_path": "/m/gemma-4-26B.gguf", "total_slots": 2,
                               "default_generation_settings": {"n_ctx": 32768}, "modalities": {"vision": True}})
    try:
        model = LlamaCppProvider("m", server.url, max_tokens=4096)
        assert model.ready() == (True, "gemma-4-26B (llama.cpp, on this computer)")
        assert model.slot_tokens == 32768 and prompt_limit(model) == 32768 - 4096 and model.sees_images()
    finally:
        server.close()
    offline = LlamaCppProvider("m", "http://127.0.0.1:9")
    assert offline.slot_tokens == 16_384 and offline._slot_tokens is None  # asked again once it is up


def test_ready_reports_loading_and_missing_server(fake):
    assert LlamaCppProvider("m", fake().url).ready() == (True, "m (llama.cpp, on this computer)")
    # the Kaggle notebook: said as it is (measured: the page said 'running on this computer' of a model on Kaggle)
    kaggle = LlamaCppProvider("m", fake().url, api_key="pw", place="Kaggle")
    assert kaggle.ready() == (True, "m (llama.cpp, on Kaggle)") and kaggle.location() == "on Kaggle"
    loading = LlamaCppProvider("m", fake(status=503).url).ready()
    assert not loading[0] and "loading" in loading[1]
    missing = LlamaCppProvider("m", "http://127.0.0.1:9").ready()
    assert not missing[0] and "isn't running" in missing[1]


def test_a_tunnels_error_page_is_said_plainly_never_shown(fake):
    """Cloudflare's 530 page from the Kaggle notebook's address, when the tunnel is down: before, the status said 'llama-server
    answered 530; check its window' and a reply ended with the page's first 300 characters of HTML."""
    page = "<!DOCTYPE html><html><head><title>Error 1033</title></head><body>Cloudflare Tunnel error</body></html>"
    tunnel = fake(status=530, page=page)
    model = LlamaCppProvider("m", tunnel.url)
    assert model.health() == ("down", "The model can't be reached right now: its connection is down. Try again in a "
                                      "minute.")
    with pytest.raises(ProviderError) as failed:
        model.complete("s", [{"role": "user", "content": "q"}], [])
    assert "<" not in str(failed.value) and "can't be reached" in str(failed.value)
    assert LlamaCppProvider("m", fake(status=503).url).health()[0] == "loading"


def test_a_call_gemma_wrote_into_its_text_becomes_a_real_call():
    # Seen in a real chat: an answer in prose, then the submit call in Gemma's syntax as text.
    text = ('The repository is for Zen.\n\nsubmit_result{answer: "Zen is a voice assistant (Q4\\_K).",'
            'sources:[<|"|>https://github.com/AryThakar/zen<|"|>]}')
    turn = parse_turn(text, [], "stop", True, "m", {"submit_result", "web_search"})
    assert turn.text == "The repository is for Zen." and turn.stop == "tool_use"
    assert turn.tool_calls[0]["name"] == "submit_result"
    assert turn.tool_calls[0]["input"] == {"answer": "Zen is a voice assistant (Q4_K).",
                                           "sources": ["https://github.com/AryThakar/zen"]}
    assert turn.raw["tool_calls"][0]["function"]["name"] == "submit_result"  # replayed like a parsed call
    json_call = ('I will use the image generation tool.\n\n{\n  "tool_calls": [\n    {\n      "tool_name": "generate_image",\n'
                 '      "tool_args": {"prompt": "A JK flip-flop", "size": "square"}\n    }\n  ]\n}')
    turn = parse_turn(json_call, [], "stop", True, "m", {"generate_image", "draw_diagram"})
    assert turn.text == "I will use the image generation tool." and turn.stop == "tool_use"  # seen in a real reply
    assert [(c["name"], c["input"]) for c in turn.tool_calls] == [("generate_image", {"prompt": "A JK flip-flop",
                                                                                      "size": "square"})]
    fenced = 'Saving it.\n```json\n{"name": "save_note", "arguments": "{\\"title\\": \\"T\\", \\"content\\": \\"C\\"}"}\n```'
    turn = parse_turn(fenced, [], "stop", True, "m", {"save_note"})
    assert turn.text == "Saving it." and turn.tool_calls[0]["input"] == {"title": "T", "content": "C"}
    data = 'The table:\n{"name": "Ada", "arguments": {"x": 1}}'  # a JSON answer, not a tool offered: left alone
    assert parse_turn(data, [], "stop", True, "m", {"save_note"}).text == data
    mention = "Use submit_result{answer} when done."
    assert parse_turn(mention, [], "stop", True, "m", {"submit_result"}).text == mention  # prose is left alone
    cut = parse_turn("Half\nsubmit_result{answer:<|\"|>unfinished", [], "length", True, "m", {"submit_result"})
    assert cut.text == "Half" and cut.tool_calls == []  # a cut-off call is dropped, never run
    off = parse_turn("Done.\ncall:web_search{query:<|\"|>x<|\"|>}", [], "stop", False, "m", {"web_search"})
    assert off.text == "Done." and off.tool_calls == []  # tools off: removed from the text only
    # Also seen: the same call written Python-style, read by ast (literals only, nothing evaluated).
    py = parse_turn('It ran.\nsubmit_result(answer="0.2722 (numeric)", code=\'print("a)")\', options=None)',
                    [], "stop", True, "m", {"submit_result"})
    assert py.text == "It ran." and py.tool_calls[0]["input"] == {"answer": "0.2722 (numeric)",
                                                                  "code": 'print("a)")', "options": None}


def test_quoted_keys_inside_arguments_are_unquoted():
    """Seen in a real run: the keys of the objects in a list arrived as '"op"', and clean_data refused them."""
    from app.providers.llamacpp import _arguments
    raw = json.dumps({"data": "data_1", "steps": [{'"op"': "drop_duplicates"}, {'<|"|>op<|"|>': "tidy_text",
                                                                               "'columns'": ["a"]}]})
    assert _arguments(raw) == {"data": "data_1", "steps": [{"op": "drop_duplicates"},
                                                           {"op": "tidy_text", "columns": ["a"]}]}


def test_a_helper_is_shown_pictures_as_image_parts():
    from app.providers.base import IMAGE_TOKENS, estimate_tokens
    shown = {"role": "user", "content": "[The picture your script made: image_1]",
             "images": [{"mime": "image/png", "data": "iVBORw0KGgo" * 2000}]}
    out = to_openai_messages([shown])[0]
    assert out["content"][0] == {"type": "text", "text": "[The picture your script made: image_1]"}
    assert out["content"][1]["image_url"]["url"].startswith("data:image/png;base64,iVBORw0KGgo")
    # an image counts as an image, not as 22,000 characters of base64
    assert estimate_tokens("s", [shown], []) < IMAGE_TOKENS + 100


def test_a_call_written_as_json_is_made_a_call():
    """Measured (chat dacb378f2781): main wrote '{"action": "generate_image", "action_input": "..."}' as its reply;
    the user saw the JSON and nothing ran. The one place calls are read takes every key such a call was seen with."""
    from app.providers.gemma_calls import heal
    written = ('{\n  "action": "generate_image",\n  "action_input": "{ \\"prompt\\": \\"A majestic lion\\", '
               '\\"size\\": \\"wide\\" }"\n}')
    text, calls = heal(written, {"generate_image", "calculate"})
    assert text == "" and calls == [("generate_image", {"prompt": "A majestic lion", "size": "wide"})]
    _, calls = heal('```json\n{"name": "calculate", "arguments": {"expression": "2+2"}}\n```', {"calculate"})
    assert calls == [("calculate", {"expression": "2+2"})]
    for other in ('{"total": 12}', 'Here is the call: {"action": "generate_image"}', '{"action": "fly_away"}'):
        assert heal(other, {"generate_image"})[1] == [], other


def test_a_helper_step_requires_a_tool_call(fake):
    server = fake()
    LlamaCppProvider("m", server.url).complete("s", [{"role": "user", "content": "q"}],
                                               [{"name": "finish", "description": "d", "input_schema": {"type": "object"}}],
                                               require_tool=True)
    assert server.requests[0]["tool_choice"] == "required"


def test_a_reply_gets_the_room_the_slot_has_left_and_never_less_than_max_tokens(fake):
    """Measured 2026-10-16: a game page was cut off twice at 4,096 tokens with ~10,000 left in a 24,576 slot."""
    done = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    server = fake(chunks=[delta(content="ok"), done], props={"default_generation_settings": {"n_ctx": 24576}})
    model = LlamaCppProvider("m", server.url)
    for size in (14_000, 23_000, None):
        model.complete("s", [{"role": "user", "content": "q"}], [], prompt_tokens=size)
    sent = [r["max_tokens"] for r in server.requests]
    assert sent == [24576 - 14_000 - REPLY_MARGIN, 4096, 4096]  # the room left; the floor; not counted: the floor
