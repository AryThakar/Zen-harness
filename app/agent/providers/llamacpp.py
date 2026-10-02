"""A local model served by llama.cpp's ``llama-server`` (its OpenAI-compatible chat API).

    llama-server -m Qwen3.5-9B-MTP-IQ4_XS.gguf --alias qwen3.5-9b -c 16384 --spec-type draft-mtp
    BIZ_LLM_PROVIDER=llamacpp uvicorn app.main:app

- Streams every turn: answer text appears as it is written, and tool calls arrive
  as deltas that are put together here. llama-server reads the calls out of the
  model's own chat template (jinja, on by default).
- Thinking is off unless BIZ_LLM_THINKING=on: on a laptop GPU every thought token
  costs as much time as an answer token. Thoughts are never replayed.
- Assistant turns keep their tool calls' arguments exactly as the model wrote
  them (``raw``), so replaying history to the same model is byte-for-byte.
- The OpenAI format has no error flag on tool results, so failed tools are
  marked in their text.
- Only the standard library is used for HTTP; nothing to install.
"""

from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.request
import uuid

from .base import EventCallback, ModelTurn, ProviderError, answered_ids

MAX_TOKENS = 4096           # per turn, without thinking
MAX_TOKENS_THINKING = 8192  # thinking counts against it
READ_TIMEOUT = 600          # longest silence allowed while a turn streams (a first turn reads the whole prompt)
# Sampling for Qwen-style instruct models; llama-server's own defaults are tuned for creative text.
SAMPLING = {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0.0}
SAMPLING_THINKING = {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0}
NO_MORE_TOOLS = ("(The tool limit for this question is reached. Answer now in plain text from the results above, "
                 "without calling any tools; say what is still missing if something is.)")
# A tool call the model writes as text anyway (tools switched off, or a call the template parser missed).
TEXT_TOOL_CALL = re.compile(r"<tool_call>.*?(?:</tool_call>|$)", re.DOTALL)


class LlamaCppProvider:
    name = "llamacpp"

    def __init__(self, model: str, base_url: str = "http://127.0.0.1:8080", thinking: bool = False,
                 slot: int | None = None, max_tokens: int | None = None):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.thinking = thinking
        # With llama-server --parallel N, each role keeps its own slot (main = 0, worker = 1), so
        # each keeps its own prompt cache: the same instructions are not read again every call.
        self.slot = slot
        self.max_tokens = max_tokens or (MAX_TOKENS_THINKING if thinking else MAX_TOKENS)

    def ready(self) -> tuple[bool, str]:
        try:
            with urllib.request.urlopen(f"{self.base_url}/health", timeout=3):
                pass
        except urllib.error.HTTPError as exc:
            if exc.code == 503:
                return False, "The local model is still loading; try again in a moment."
            return False, f"llama-server at {self.base_url} answered {exc.code}; check its window for errors."
        except (urllib.error.URLError, OSError):
            return False, f"Start llama-server (address {self.base_url}), then reload this page."
        return True, f"{self.model} (local, llama.cpp)"

    def complete(self, system: str, messages: list[dict], tools: list[dict], *,
                 allow_tools: bool = True, on_event: EventCallback | None = None) -> ModelTurn:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *to_openai_messages(messages)],
            "stream": True,
            "max_tokens": self.max_tokens,
            "chat_template_kwargs": {"enable_thinking": self.thinking},
            "cache_prompt": True,
            **({"id_slot": self.slot} if self.slot is not None else {}),
            **(SAMPLING_THINKING if self.thinking else SAMPLING),
        }
        if tools:
            body["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
                for t in tools]
            body["tool_choice"] = "auto" if allow_tools else "none"
            if not allow_tools:
                # tool_choice only stops llama-server parsing calls; the model still sees the
                # tools, so it has to be told in the conversation.
                body["messages"].append({"role": "user", "content": NO_MORE_TOOLS})

        request = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        )
        try:
            with urllib.request.urlopen(request, timeout=READ_TIMEOUT) as response:
                text, calls, finish, timings = _read_stream(response, on_event)
        except urllib.error.HTTPError as exc:
            raise ProviderError(f"The local model returned an error ({exc.code}): {_error_message(exc)}") from exc
        except (socket.timeout, TimeoutError) as exc:
            raise ProviderError("The local model took too long to respond; try a simpler question.") from exc
        except (urllib.error.URLError, ConnectionError) as exc:
            raise ProviderError(f"Could not reach the local model at {self.base_url}; is llama-server running?") from exc
        turn = parse_turn(text, calls, finish, allow_tools, self.model)
        if timings:
            turn.usage = {"output_tokens": timings.get("predicted_n"), "prompt_tokens": timings.get("prompt_n"),
                          "tokens_per_second": timings.get("predicted_per_second")}
        return turn


def parse_turn(text: str, calls: list[dict], finish: str | None, allow_tools: bool, model: str) -> ModelTurn:
    """A finished stream as a ModelTurn. A turn cut off by max_tokens runs no tools:
    its last call may be incomplete."""
    stop = "max_tokens" if finish == "length" else "tool_use" if calls else "end"
    if stop == "max_tokens" or not allow_tools:
        calls = []
        stop = "max_tokens" if finish == "length" else "end"
    tool_calls = [{"id": c["id"], "name": c["function"]["name"], "input": _arguments(c["function"]["arguments"])}
                  for c in calls]
    raw = {"tool_calls": calls} if calls else None
    text = TEXT_TOOL_CALL.sub("", text).strip()
    return ModelTurn(text=text, tool_calls=tool_calls, raw=raw, stop=stop, model=model)


def to_openai_messages(messages: list[dict]) -> list[dict]:
    """Neutral history -> OpenAI chat messages (without the system prompt)."""
    out: list[dict] = []
    for i, msg in enumerate(messages):
        role = msg["role"]
        if role == "user":
            out.append({"role": "user", "content": msg["content"]})
        elif role == "assistant":
            turn = _assistant_message(msg, answered_ids(messages, i))
            if turn:  # an empty turn (nothing said, no call that ran) is skipped
                out.append(turn)
        elif role == "tool":
            content = f"Error: {msg['content']}" if msg.get("is_error") else msg["content"]
            out.append({"role": "tool", "tool_call_id": msg["tool_call_id"], "content": content})
    return out


def _assistant_message(msg: dict, answered: set[str]) -> dict | None:
    raw = msg.get("raw") if msg.get("provider") == LlamaCppProvider.name else None
    if raw is not None:
        calls = [c for c in raw.get("tool_calls", []) if c["id"] in answered]
    else:  # written by another provider: rebuild the calls
        calls = [{"id": c["id"], "type": "function",
                  "function": {"name": c["name"], "arguments": json.dumps(c["input"])}}
                 for c in msg.get("tool_calls", []) if c["id"] in answered]
    text = msg.get("text") or ""
    if not text and not calls:
        return None
    turn = {"role": "assistant", "content": text}
    if calls:
        turn["tool_calls"] = calls
    return turn


def _read_stream(response, on_event: EventCallback | None) -> tuple[str, list[dict], str | None, dict | None]:
    """Server-sent events -> (answer text, complete tool calls, finish reason, llama-server's timings)."""
    text, finish, timings = "", None, None
    partial: dict[int, dict] = {}  # tool call index -> {"id", "type", "function": {"name", "arguments"}}
    for line in response:
        line = line.decode("utf-8").strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            break
        chunk = json.loads(data)
        if "error" in chunk:
            raise ProviderError(f"The local model stopped with an error: {chunk['error'].get('message', chunk['error'])}")
        timings = chunk.get("timings") or timings  # sent with the last chunk
        for choice in chunk.get("choices", []):
            delta = choice.get("delta") or {}
            if delta.get("content"):
                text += delta["content"]
                if on_event:
                    on_event("text", delta["content"])
            for d in delta.get("tool_calls") or []:
                call = partial.setdefault(d.get("index", 0), {"id": None, "type": "function",
                                                             "function": {"name": "", "arguments": ""}})
                call["id"] = d.get("id") or call["id"]
                fn = d.get("function") or {}
                call["function"]["name"] += fn.get("name") or ""
                call["function"]["arguments"] += fn.get("arguments") or ""
            finish = choice.get("finish_reason") or finish
    calls = [partial[i] for i in sorted(partial)]
    for call in calls:
        call["id"] = call["id"] or f"call_{uuid.uuid4().hex[:24]}"
    return text, calls, finish, timings


def _arguments(arguments: str):
    """The call's input as a dict, or the raw text when it isn't valid JSON - the
    tool runner then tells the model its input was unreadable."""
    try:
        return json.loads(arguments) if arguments.strip() else {}
    except json.JSONDecodeError:
        return arguments


def _error_message(exc: urllib.error.HTTPError) -> str:
    body = exc.read().decode("utf-8", "replace")
    try:
        error = json.loads(body).get("error", body)
        return error.get("message", str(error)) if isinstance(error, dict) else str(error)
    except (json.JSONDecodeError, AttributeError):
        return body[:300] or exc.reason
