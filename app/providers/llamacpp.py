"""A local model served by llama.cpp's ``llama-server`` (its OpenAI-compatible chat API).

    llama-server -m gemma-4-E4B-it-qat-UD-Q4_K_XL.gguf --alias local --temp 1.0 --top-p 0.95 --top-k 64 ...
    BIZ_LLM_PROVIDER=llamacpp uvicorn app.main:app      (the full command: .claude/launch.json, "assistant-llm")

- Streams every turn: answer text appears as it is written, and tool calls arrive
  as deltas that are put together here. llama-server reads the calls out of the
  model's own chat template (jinja, on by default).
- Thinking is off unless asked for (the assistant turns it on): on a laptop GPU every thought
  token costs as much time as an answer token. Thoughts are never replayed; they are kept on the
  turn (``thinking``) and their length in its usage. No thinking budget by default (the user's
  call: a cut in the middle of thinking can break the model's work, and in 323 messages thinking
  ran out of room only twice); ``thinking_budget`` sets llama-server's reasoning_budget_tokens for
  experiments; a call's own ``budget`` caps just that call - main's "answer quickly" retry after
  a reply that thought until the limit. Measured on such a question (a hard integral, 4,096-token
  replies): no budget, 4,093 tokens of thinking and no answer; "think briefly" in the message, cut
  off mid-answer; thinking off, an answer in 50 s; a 2,048-token budget, thinking cut mid-sentence
  and then a complete answer in 45 s (the model redid the work in the answer). ``think=False``
  switches thinking off for a call.
- Sampling (temperature, top_p, top_k, min_p) is set where the model is launched - it belongs
  to the model, not to this client - and nothing is sent unless a caller passes ``sampling``
  (a benchmark comparing settings). Values left here from an earlier model once overrode the
  loaded model's own on every request.
- ready() names the model llama-server has loaded (its /props), not a configured label, and learns the
  context of one slot from the same answer (slot_tokens: default_generation_settings.n_ctx, per slot with
  --parallel) - the prompt limit follows the server as it runs, not a constant (16K on a laptop, 32K on Kaggle).
- count_tokens() is exact: the same request generation would send, counted by llama-server
  (/v1/chat/completions/input_tokens; else /apply-template then /tokenize), so the chat template's
  markers and tool declarations are counted too. Approach from Unsloth Studio's prompt counter.
- Assistant turns keep their tool calls' arguments exactly as the model wrote
  them (``raw``), so replaying history to the same model is byte-for-byte.
- The OpenAI format has no error flag on tool results, so failed tools are
  marked in their text.
- Only the standard library is used for HTTP; nothing to install.
"""

from __future__ import annotations

import base64
import http.client
import json
import logging
import re
import socket
import threading
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager

from .base import Cancelled, EventCallback, ModelTurn, ProviderError, answered_ids, estimate_tokens
from .gemma_calls import QUOTE, heal

log = logging.getLogger(__name__)

MAX_TOKENS = 4096           # per turn, without thinking
MAX_TOKENS_THINKING = 8192  # thinking counts against it
THINKING_BUDGET = None      # no thinking budget (a number caps thinking tokens per reply, for experiments)
READ_TIMEOUT = 600          # longest silence allowed while a turn streams (a first turn reads the whole prompt)
DEFAULT_SLOT_TOKENS = 16_384  # a slot's context until the server says (it is still starting)
NO_MORE_TOOLS = ("(The tool limit for this question is reached. Answer now in plain text from the results above, "
                 "without calling any tools; say what is still missing if something is.)")
# A tool call the model writes as text anyway (tools switched off, or a call the template parser missed).
LOOK_TOKENS = 600  # an answer about an image: a few sentences
LOOK_TIMEOUT = 120
TEXT_TOOL_CALL = re.compile(r"<tool_call>.*?(?:</tool_call>|$)", re.DOTALL)
# What a connection to the server can fail with: no answer at all (URLError, OSError), or an answer cut off
# part way - the Kaggle tunnel dropping mid-stream raises http.client.IncompleteRead, which is neither.
NET_ERRORS = (urllib.error.URLError, OSError, http.client.HTTPException)
# A request whose connection fails before anything comes back is tried again after these pauses (seconds): a
# model on another computer - the 26B on Kaggle, through its bridge and a tunnel - drops a connection now and
# then (2026-10-09: 2 of the first 5 acceptance jobs failed on one dropped request, the bridge's 502). A server
# that answered with its own error (4xx, 500), a timeout and a stopped turn are not retried.
RETRY_PAUSES = (2.0, 6.0)
RETRY_STATUS = {502, 503, 504}
# One request at a time per slot (server address, slot): two requests on one slot at once came back mixed - a
# helper answering another conversation's task (2026-10-09, the 26B on Kaggle, llama-server b11474 with two
# slots: while two clients shared slot 1, the coder asked for "invoices.csv" and wrote a churn script, both from
# another chat). The app runs one turn per chat, but two chats - or a turn and a picture check - can overlap.
_SLOT_LOCKS: dict[tuple[str, int], threading.Lock] = {}
_SLOT_LOCKS_GUARD = threading.Lock()


def template_switches(thinking: bool) -> dict:
    """Thinking on or off, in each chat template's own words; a template reads its own and ignores the
    other. Gemma and Qwen: enable_thinking. gpt-oss always reasons, at a reasoning_effort (its default,
    medium, on every step makes a laptop slow): low when thinking is off."""
    return {"enable_thinking": thinking, "reasoning_effort": "medium" if thinking else "low"}


class LlamaCppProvider:
    name = "llamacpp"
    where = "the local model"  # in error messages

    def __init__(self, model: str, base_url: str = "http://127.0.0.1:8080", thinking: bool = False,
                 slot: int | None = None, max_tokens: int | None = None, sampling: dict | None = None,
                 cancel: threading.Event | None = None, thinking_budget: int | None = THINKING_BUDGET):
        self.model = model
        # Set when the user stops: the call ends at the next streamed line and its connection
        # closes, which makes llama-server stop generating on that slot.
        self.cancel = cancel
        self.sampling = sampling or {}  # empty: the server's settings for the model it has loaded
        self.base_url = base_url.rstrip("/")
        self.thinking = thinking
        # With llama-server --parallel N, each role keeps its own slot (main = 0, worker = 1), so
        # each keeps its own prompt cache: the same instructions are not read again every call.
        self.slot = slot
        self.max_tokens = max_tokens or (MAX_TOKENS_THINKING if thinking else MAX_TOKENS)
        self.thinking_budget = thinking_budget  # None: unlimited (only for comparisons)
        self._vision: bool | None = None  # whether the server has the vision part loaded (sees_images)
        self._slot_tokens: int | None = None  # one slot's context, from /props

    def ready(self) -> tuple[bool, str]:
        try:
            with urllib.request.urlopen(f"{self.base_url}/health", timeout=3):
                pass
        except urllib.error.HTTPError as exc:
            if exc.code == 503:
                return False, "The local model is still loading; try again in a moment."
            return False, f"llama-server at {self.base_url} answered {exc.code}; check its window for errors."
        except NET_ERRORS:
            return False, f"Start llama-server (address {self.base_url}), then reload this page."
        return True, f"{self.loaded_model()} (local, llama.cpp)"

    def sees_images(self) -> bool:
        """Whether llama-server has the model's vision part loaded (--mmproj; /props says so), asked once.
        A helper is shown images only then: sent to a server without it, an image fails the call."""
        if self._vision is None:
            props = self._props()
            if not props:
                return False  # not remembered: the server may still be starting
            self._vision = bool((props.get("modalities") or {}).get("vision"))
        return self._vision

    @property
    def slot_tokens(self) -> int:
        """One slot's context, as the server runs it; DEFAULT_SLOT_TOKENS until it answers."""
        if self._slot_tokens is None:
            self._props()
        return self._slot_tokens or DEFAULT_SLOT_TOKENS

    def _props(self) -> dict:
        """llama-server's /props ({} when it doesn't answer), remembering the slot size it gives."""
        try:
            with urllib.request.urlopen(f"{self.base_url}/props", timeout=3) as response:
                props = json.loads(response.read())
        except (*NET_ERRORS, ValueError):
            return {}
        if not isinstance(props, dict):
            return {}
        n_ctx = (props.get("default_generation_settings") or {}).get("n_ctx")
        if isinstance(n_ctx, int) and n_ctx > 0:
            self._slot_tokens = n_ctx
        return props

    def loaded_model(self) -> str:
        """The model file llama-server has loaded, e.g. 'gemma-4-26B-A4B-it-qat-UD-Q4_K_XL'."""
        path = str(self._props().get("model_path") or "")
        return re.split(r"[\\/]", path)[-1].removesuffix(".gguf") or self.model

    def _body(self, system: str, messages: list[dict], tools: list[dict], allow_tools: bool,
              think: bool = True, budget: int | None = None) -> dict:
        """The request, the same for generating and for counting."""
        thinking = self.thinking and think
        budget = budget or self.thinking_budget
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *to_openai_messages(messages)],
            "max_tokens": self.max_tokens,
            "chat_template_kwargs": template_switches(thinking),
            **({"reasoning_budget_tokens": budget} if thinking and budget else {}),
            "cache_prompt": True,
            **({"id_slot": self.slot} if self.slot is not None else {}),
            **self.sampling,
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
        return body

    def count_tokens(self, system: str, messages: list[dict], tools: list[dict], *, allow_tools: bool = True) -> int:
        """The exact prompt size of this request, as llama-server will see it."""
        body = self._body(system, messages, tools, allow_tools)
        try:
            return int(self._post("/v1/chat/completions/input_tokens", body)["input_tokens"])
        except (*NET_ERRORS, ValueError, KeyError, TypeError):
            pass
        try:  # an older llama-server: render with the model's template, then tokenize
            prompt = self._post("/apply-template", {k: body[k] for k in ("messages", "tools", "chat_template_kwargs")
                                                    if k in body})["prompt"]
            return len(self._post("/tokenize", {"content": prompt, "add_special": True})["tokens"])
        except (*NET_ERRORS, ValueError, KeyError, TypeError):
            return estimate_tokens(system, messages, tools)

    def _headers(self) -> dict:
        return {"Content-Type": "application/json"}

    def _open(self, path: str, body: dict, timeout: float, stream: bool = False):
        """POST a request to the server; the open response. A connection that fails before anything comes back
        is tried again (RETRY_PAUSES); nothing has streamed yet, so a retry can't repeat an answer."""
        headers = {**self._headers(), **({"Accept": "text/event-stream"} if stream else {})}
        data = json.dumps(body).encode()
        for pause in (*RETRY_PAUSES, None):
            request = urllib.request.Request(f"{self.base_url}{path}", data=data, headers=headers)
            try:
                return urllib.request.urlopen(request, timeout=timeout)
            except urllib.error.HTTPError as exc:
                if pause is None or exc.code not in RETRY_STATUS or self._stopped():
                    raise
                why = f"HTTP {exc.code}"
                exc.close()
            except urllib.error.URLError as exc:
                if pause is None or self._stopped() or isinstance(exc.reason, (socket.timeout, TimeoutError)):
                    raise
                why = str(exc.reason)
            except ConnectionError as exc:  # reset or closed while opening (http.client's RemoteDisconnected)
                if pause is None or self._stopped():
                    raise
                why = type(exc).__name__
            log.warning("%s: %s failed (%s); trying again in %.0f s", self.where, path, why, pause)
            (self.cancel or threading.Event()).wait(pause)

    def _stopped(self) -> bool:
        return self.cancel is not None and self.cancel.is_set()

    @contextmanager
    def _slot(self):
        """This provider's slot to itself for one request: waits while another request of this app has it
        (Stop still stops the wait). No slot named: llama-server picks a free one, nothing to hold."""
        if self.slot is None:
            yield
            return
        with _SLOT_LOCKS_GUARD:
            lock = _SLOT_LOCKS.setdefault((self.base_url, self.slot), threading.Lock())
        while not lock.acquire(timeout=0.5):
            if self._stopped():
                raise Cancelled
        try:
            yield
        finally:
            lock.release()

    def _unreachable(self) -> str:
        return f"Could not reach the local model at {self.base_url}; is llama-server running?"

    def _post(self, path: str, body: dict, timeout: int = 30) -> dict:
        with self._open(path, body, timeout) as response:
            return json.loads(response.read())

    def look(self, image: bytes, mime: str, question: str) -> str:
        """One question about one image, answered by the model's vision part (llama-server --mmproj), on
        this provider's slot, thinking off: a description, not a reasoning task (view_image)."""
        url = f"data:{mime};base64," + base64.b64encode(image).decode()
        body = {"model": self.model, "max_tokens": LOOK_TOKENS, "cache_prompt": True,
                "chat_template_kwargs": template_switches(False),
                "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}},
                                                          {"type": "text", "text": question}]}],
                **({"id_slot": self.slot} if self.slot is not None else {}), **self.sampling}
        try:
            with self._slot():
                out = self._post("/v1/chat/completions", body, timeout=LOOK_TIMEOUT)
        except urllib.error.HTTPError as exc:
            raise ProviderError(_error_message(exc)) from exc
        except NET_ERRORS as exc:
            raise ProviderError(f"Could not reach llama-server at {self.base_url}.") from exc
        return out["choices"][0]["message"].get("content") or ""

    def complete(self, system: str, messages: list[dict], tools: list[dict], *,
                 allow_tools: bool = True, on_event: EventCallback | None = None, think: bool = True,
                 budget: int | None = None) -> ModelTurn:
        body = {**self._body(system, messages, tools, allow_tools, think, budget), "stream": True}
        try:
            with self._slot(), self._open("/v1/chat/completions", body, READ_TIMEOUT, stream=True) as response:
                text, calls, finish, timings, thinking = _read_stream(response, on_event, self.cancel)
        except urllib.error.HTTPError as exc:
            raise ProviderError(f"{self.where.capitalize()} returned an error ({exc.code}): {_error_message(exc)}") from exc
        except (socket.timeout, TimeoutError) as exc:
            raise ProviderError(f"{self.where.capitalize()} took too long to respond; try a simpler question.") from exc
        except (urllib.error.URLError, ConnectionRefusedError) as exc:
            raise ProviderError(self._unreachable()) from exc
        except (http.client.HTTPException, OSError) as exc:  # the answer was cut off part way
            raise ProviderError(f"The connection to {self.where} dropped in the middle of the answer; try again.") from exc
        except json.JSONDecodeError as exc:
            raise ProviderError(f"{self.where.capitalize()} sent an answer that could not be read; try again.") from exc
        turn = parse_turn(text, calls, finish, allow_tools, self.model, {t["name"] for t in tools or []})
        turn.thinking = thinking
        if timings:
            turn.usage = {"output_tokens": timings.get("predicted_n"), "prompt_tokens": timings.get("prompt_n"),
                          "cached_tokens": timings.get("cache_n"),  # reused from llama.cpp's prompt cache
                          "tokens_per_second": timings.get("predicted_per_second"),
                          "thinking_chars": len(thinking)}
        return turn


def parse_turn(text: str, calls: list[dict], finish: str | None, allow_tools: bool, model: str,
               names: set[str] = frozenset()) -> ModelTurn:
    """A finished stream as a ModelTurn. A turn cut off by max_tokens runs no tools:
    its last call may be incomplete. A call Gemma wrote into its text (``names``: the tools
    offered) is taken out of the text, and run when tools are allowed."""
    if not calls and names:
        text, written = heal(text, names)
        calls = [{"id": f"call_{uuid.uuid4().hex[:24]}", "type": "function",
                  "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}
                 for name, arguments in written]
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
        if role == "user" and msg.get("images"):  # pictures a helper is shown: its note, then each image
            out.append({"role": "user", "content": [{"type": "text", "text": msg["content"]}] + [
                {"type": "image_url", "image_url": {"url": f"data:{image['mime']};base64,{image['data']}"}}
                for image in msg["images"]]})
        elif role == "user":
            out.append({"role": "user", "content": msg["content"]})
        elif role == "assistant":
            turn = _assistant_message(msg, answered_ids(messages, i))
            if turn:  # an empty turn (nothing said, no call that ran) is skipped
                out.append(turn)
        elif role == "tool":
            content = f"Error: {msg['content']}" if msg.get("is_error") else msg["content"]
            out.append({"role": "tool", "tool_call_id": msg["tool_call_id"], "content": content})
    return out


OPENAI_STYLE = {"llamacpp", "groq"}  # providers whose raw tool calls are already in this format


def _assistant_message(msg: dict, answered: set[str]) -> dict | None:
    raw = msg.get("raw") if msg.get("provider") in OPENAI_STYLE else None
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


def _read_stream(response, on_event: EventCallback | None,
                 cancel: threading.Event | None = None) -> tuple[str, list[dict], str | None, dict | None, str]:
    """Server-sent events -> (answer text, complete tool calls, finish reason, llama-server's timings,
    the thinking - llama-server sends it apart as reasoning_content, a cloud server as reasoning). A
    server without timings reports usage instead; it is read into the same shape."""
    text, finish, timings, thinking = "", None, None, ""
    partial: dict[int, dict] = {}  # tool call index -> {"id", "type", "function": {"name", "arguments"}}
    for line in response:
        if cancel is not None and cancel.is_set():  # checked on thinking tokens too, not only on text
            raise Cancelled
        line = line.decode("utf-8").strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            break
        chunk = json.loads(data)
        if "error" in chunk:
            raise ProviderError(f"The local model stopped with an error: {chunk['error'].get('message', chunk['error'])}")
        timings = chunk.get("timings") or timings or _usage_timings(chunk.get("usage"))  # with the last chunk
        for choice in chunk.get("choices", []):
            delta = choice.get("delta") or {}
            thinking += delta.get("reasoning_content") or delta.get("reasoning") or ""
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
    return text, calls, finish, timings, thinking


def _usage_timings(usage: dict | None) -> dict | None:
    """An OpenAI-style usage report in llama-server's timings shape."""
    if not usage:
        return None
    out, seconds = usage.get("completion_tokens"), usage.get("completion_time")
    return {"prompt_n": usage.get("prompt_tokens"), "predicted_n": out,
            "cache_n": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
            "predicted_per_second": out / seconds if out and seconds else None}


def _arguments(arguments: str):
    """The call's input as a dict, or the raw text when it isn't valid JSON - the
    tool runner then tells the model its input was unreadable."""
    try:
        return _bare_keys(json.loads(arguments)) if arguments.strip() else {}
    except json.JSONDecodeError:
        return arguments


def _bare_keys(value):
    """Keys without the quotes they sometimes arrive in. Seen in a real run: Gemma quoted the keys of the
    objects inside a list (clean_data's steps), and they reached the tool as '"op"' - five calls in a row
    were refused as unknown fields."""
    if isinstance(value, dict):
        return {_unquote(k): _bare_keys(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_bare_keys(v) for v in value]
    return value


def _unquote(key: str) -> str:
    key = key.strip()
    if key.startswith(QUOTE) and key.endswith(QUOTE) and len(key) > 2 * len(QUOTE):
        return key[len(QUOTE):-len(QUOTE)]
    if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
        return key[1:-1]
    return key


def _error_message(exc: urllib.error.HTTPError) -> str:
    body = exc.read().decode("utf-8", "replace")
    try:
        error = json.loads(body).get("error", body)
        return error.get("message", str(error)) if isinstance(error, dict) else str(error)
    except (json.JSONDecodeError, AttributeError):
        return body[:300] or exc.reason
