# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""A model served by llama.cpp's ``llama-server`` (its OpenAI-compatible chat API): on this computer, or on the
Kaggle notebook - its address and password (api_key) - as Zen's Model settings say (app/model_link.py).

    llama-server -m Qwen3.6-35B-A3B-UD-Q4_K_S.gguf --temp 1.0 --top-p 0.95 --top-k 20 --presence-penalty 1.5 ...
    (the full command: the Kaggle notebook zen-qwen36-server)

- Streams every turn: answer text appears as it is written, and tool calls arrive
  as deltas that are put together here. llama-server reads the calls out of the
  model's own chat template (jinja, on by default).
- Thinking is off unless asked for (the assistant turns it on): on a laptop GPU every thought
  token costs as much time as an answer token. A step's thinking goes back with it (reasoning_content), and
  the model's chat template decides what it shows: Qwen 3.6's shows the steps of the current turn with their
  thinking and drops it from older turns; given none, it showed each earlier step of the turn as an empty
  <think></think> - the plan lost between tool calls, and the prompt cache missing where the generated text
  was replayed without it. Qwen's guide: dropping it between tool calls degrades accuracy. No thinking budget by default (the user's
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
import urllib.parse
import urllib.request
import uuid

from app.tools.net import USER_AGENT

from .base import Cancelled, EventCallback, ModelTurn, ProviderError, answered_ids, estimate_tokens
from .gemma_calls import QUOTE, heal

log = logging.getLogger(__name__)

MAX_TOKENS = 4096           # per turn, without thinking
MAX_TOKENS_THINKING = 8192  # thinking counts against it
THINKING_BUDGET = None      # no thinking budget (a number caps thinking tokens per reply, for experiments)
READ_TIMEOUT = 600          # longest silence allowed while a turn streams (a first turn reads the whole prompt)
DEFAULT_SLOT_TOKENS = 16_384  # a slot's context until the server says (it is still starting)
REPLY_MARGIN = 256          # tokens kept back from the reply's room: a text-only step adds a line the count didn't see
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
# model on another computer - the 26B on Kaggle, through a tunnel - drops a connection now and then (2026-10-09:
# 2 of the first 5 acceptance jobs failed on one dropped request, a 502 from the bridge Zen had then). A server
# that answered with its own error (4xx, 500), a timeout and a stopped turn are not retried.
RETRY_PAUSES = (2.0, 6.0)
RETRY_STATUS = {502, 503, 504}
# What a proxy or tunnel in front of the model answers when it can't reach it (the notebook's router's 502, Cloudflare's
# 52x and 530 - an HTML page): the model is down, said in plain words, never the page itself.
GATEWAY = {502, 504, *range(520, 531)}
DOWN = "The model can't be reached right now: its connection is down. Try again in a minute."


def template_switches(thinking: bool) -> dict:
    """Thinking on or off, in each chat template's own words; a template reads its own and ignores the
    other. Gemma and Qwen: enable_thinking. gpt-oss always reasons, at a reasoning_effort (its default,
    medium, on every step makes a laptop slow): low when thinking is off."""
    return {"enable_thinking": thinking, "reasoning_effort": "medium" if thinking else "low"}


class LlamaCppProvider:
    name = "llamacpp"
    where = "the local model"  # in error messages
    CHAT = "/v1/chat/completions"  # under base_url; a cloud's own paths in cloud.py's subclasses
    MODELS = "/v1/models"
    STREAM_EXTRA: dict = {}  # a stream's own fields (llama-server sends its timings unasked)

    def __init__(self, model: str, base_url: str = "http://127.0.0.1:8080", thinking: bool = False,
                 slot: int | None = None, max_tokens: int | None = None, sampling: dict | None = None,
                 cancel: threading.Event | None = None, thinking_budget: int | None = THINKING_BUDGET,
                 api_key: str = "", place: str | None = None):
        self.model = model
        # A llama-server elsewhere is reached with its password (--api-key: the Kaggle notebook's), and named by
        # where it runs ('Kaggle'), for the page
        self.api_key = api_key
        self.place = place
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

    def health(self) -> tuple[str, str]:
        """The model server's state - 'ready', 'loading' or 'down' - and what to tell the user, in plain words."""
        try:
            with self._get("/health", 3 if self.place is None else 10):  # a tunnel takes longer
                pass
        except urllib.error.HTTPError as exc:
            if exc.code == 503 and not _is_page(exc):  # llama-server's own "Loading model"
                return "loading", "The model is still loading; it will be ready in a moment."
            if exc.code in GATEWAY or _is_page(exc):
                return "down", DOWN
            return "down", f"The model server answered with an error ({exc.code}); its window says why."
        except NET_ERRORS:
            if self.place:  # a new notebook session has a new address
                return "down", (f"The notebook on {self.place} isn't answering. Start it, then paste its new Zen code: "
                                "click the dot by the chat's name.")
            return "down", (f"llama-server isn't running at {self.base_url}. Start it, or choose where the model runs: "
                            "click the dot by the chat's name.")
        return "ready", f"{self.loaded_model()} (llama.cpp, {self.location()})"

    def location(self) -> str:
        """Where the model runs, in words: 'on Kaggle' (the place it was given), 'on this computer' for
        llama-server here, else its address."""
        if self.place:
            return f"on {self.place}"
        where = urllib.parse.urlsplit(self.base_url).hostname or ""
        return "on this computer" if where in ("127.0.0.1", "localhost", "::1") else f"at {where}"

    def ready(self) -> tuple[bool, str]:
        state, message = self.health()
        return state == "ready", message

    def sees_images(self) -> bool:
        """Whether llama-server has the model's vision part loaded (--mmproj; /props says so), asked once.
        A helper is shown images only then: sent to a server without it, an image fails the call."""
        if self._vision is None:
            props = self._props()
            if not props:
                return False  # not remembered: the server may still be starting
            self._vision = bool((props.get("modalities") or {}).get("vision"))
        return self._vision

    FILLS_SLOT = True  # a reply may take the slot's whole room left; a cloud API caps replies (CloudProvider)

    def reply_tokens(self, prompt_tokens: int | None = None) -> int:
        """The reply's room: what the slot has left after the prompt, so a long file fits in one call (measured: a
        game page cut off twice at 4,096 tokens with 10,000 left in the slot, 2026-10-16) - never less than
        max_tokens, the room context.py always keeps free."""
        if not self.FILLS_SLOT or not prompt_tokens:
            return self.max_tokens
        return max(self.max_tokens, self.slot_tokens - prompt_tokens - REPLY_MARGIN)

    @property
    def slot_tokens(self) -> int:
        """One slot's context, as the server runs it; DEFAULT_SLOT_TOKENS until it answers."""
        if self._slot_tokens is None:
            self._props()
        return self._slot_tokens or DEFAULT_SLOT_TOKENS

    def _props(self) -> dict:
        """llama-server's /props ({} when it doesn't answer), remembering the slot size it gives."""
        try:
            with self._get("/props", 3 if self.place is None else 10) as response:
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
              think: bool = True, budget: int | None = None, require_tool: bool = False,
              prompt_tokens: int | None = None, sampling: dict | None = None) -> dict:
        """The request, the same for generating and for counting. ``sampling``: a call's own settings over the
        server's (a helper whose work is code asks for the model's precise-coding ones)."""
        thinking = self.thinking and think
        budget = budget or self.thinking_budget
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *to_openai_messages(messages)],
            "max_tokens": self.reply_tokens(prompt_tokens),
            "chat_template_kwargs": template_switches(thinking),
            **({"reasoning_budget_tokens": budget} if thinking and budget else {}),
            "cache_prompt": True,
            **({"id_slot": self.slot} if self.slot is not None else {}),
            **self.sampling, **(sampling or {}),
        }
        if tools:
            body["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
                for t in tools]
            body["tool_choice"] = "required" if require_tool and allow_tools else "auto" if allow_tools else "none"
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
        return {"Content-Type": "application/json", "User-Agent": USER_AGENT,
                **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})}

    def _get(self, path: str, timeout: float):
        """A GET under the server's address, with its password."""
        return urllib.request.urlopen(urllib.request.Request(f"{self.base_url}{path}", headers=self._headers()),
                                      timeout=timeout)

    def _clean(self, body: dict) -> dict:
        """A request as this server takes it (llama-server takes its own fields; a cloud strips them)."""
        return body

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

    def _unreachable(self) -> str:
        if self.place:
            return f"The notebook on {self.place} isn't answering. Start it (a new session: paste its new Zen code), then try again."
        return f"The model server isn't running (expected at {self.base_url}). Start it, then try again."

    def _http_error(self, exc: urllib.error.HTTPError) -> str:
        """What the user is told when the server answered with an error: its own message, or - for a proxy's or
        tunnel's error page - that the model can't be reached."""
        if exc.code in GATEWAY or _is_page(exc):
            return DOWN
        return f"{self.where[:1].upper() + self.where[1:]} returned an error ({exc.code}): {_error_message(exc)}"

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
            out = self._post(self.CHAT, self._clean(body), timeout=LOOK_TIMEOUT)
        except urllib.error.HTTPError as exc:
            raise ProviderError(self._http_error(exc)) from exc
        except NET_ERRORS as exc:
            raise ProviderError(self._unreachable()) from exc
        return out["choices"][0]["message"].get("content") or ""

    def complete(self, system: str, messages: list[dict], tools: list[dict], *,
                 allow_tools: bool = True, require_tool: bool = False, on_event: EventCallback | None = None,
                 think: bool = True, budget: int | None = None, prompt_tokens: int | None = None,
                 sampling: dict | None = None) -> ModelTurn:
        body = {**self._body(system, messages, tools, allow_tools, think, budget, require_tool, prompt_tokens,
                             sampling),
                "stream": True,
                **self.STREAM_EXTRA}
        try:
            with self._open(self.CHAT, body, READ_TIMEOUT, stream=True) as response:
                text, calls, finish, timings, thinking = _read_stream(response, on_event, self.cancel)
        except urllib.error.HTTPError as exc:
            raise ProviderError(self._http_error(exc)) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise ProviderError(f"{self.where[:1].upper() + self.where[1:]} took too long to respond; try a simpler question.") from exc
        except (urllib.error.URLError, ConnectionRefusedError) as exc:
            raise ProviderError(self._unreachable()) from exc
        except (http.client.HTTPException, OSError) as exc:  # the answer was cut off part way
            raise ProviderError(f"The connection to {self.where} dropped in the middle of the answer; try again.") from exc
        except json.JSONDecodeError as exc:
            raise ProviderError(f"{self.where[:1].upper() + self.where[1:]} sent an answer that could not be read; try again.") from exc
        turn = parse_turn(text, calls, finish, allow_tools, self.model, {t["name"] for t in tools or []})
        turn.thinking = thinking
        if timings:
            turn.usage = {"output_tokens": timings.get("predicted_n"), "prompt_tokens": timings.get("prompt_n"),
                          "cached_tokens": timings.get("cache_n"),  # reused from llama.cpp's prompt cache
                          "tokens_per_second": timings.get("predicted_per_second"),
                          "thinking_chars": len(thinking),
                          # a cloud that keeps its thinking to itself says how long it was (Gemini: total less the rest)
                          **({"thinking_tokens": timings["thought_n"]} if timings.get("thought_n") else {})}
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
    cut_call = None
    if stop == "max_tokens" and calls:  # the limit came while it wrote a call: too long, said so (base.ModelTurn)
        cut_call = {k: v for k, v in writing(calls[-1]["function"]).items() if k in ("name", "chars")}
    if stop == "max_tokens" or not allow_tools:
        calls = []
        stop = "max_tokens" if finish == "length" else "end"
    tool_calls = [{"id": c["id"], "name": c["function"]["name"], "input": _arguments(c["function"]["arguments"])}
                  for c in calls]
    raw = {"tool_calls": calls} if calls else None
    text = TEXT_TOOL_CALL.sub("", text).strip()
    return ModelTurn(text=text, tool_calls=tool_calls, raw=raw, stop=stop, model=model, cut_call=cut_call)


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


# providers whose raw tool calls are already in this format (the last three: chats from before one cloud provider)
OPENAI_STYLE = {"llamacpp", "cloud", "groq", "gemini", "nvidia"}


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
    if msg.get("thinking"):  # the step's own reasoning, back with it (module docstring: thinking)
        turn["reasoning_content"] = msg["thinking"]
    return turn


def _read_stream(response, on_event: EventCallback | None,
                 cancel: threading.Event | None = None) -> tuple[str, list[dict], str | None, dict | None, str]:
    """Server-sent events -> (answer text, complete tool calls, finish reason, llama-server's timings,
    the thinking - llama-server sends it apart as reasoning_content, a cloud server as reasoning). A
    server without timings reports usage instead; it is read into the same shape."""
    text, finish, timings, thinking = "", None, None, ""
    partial: dict[int, dict] = {}  # tool call index -> {"id", "type", "function": {"name", "arguments"}}
    told: dict[int, int] = {}  # a call being written -> its length when the page was last told
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
                index = _call_index(d, partial)
                call = partial.setdefault(index, {"id": None, "type": "function",
                                                  "function": {"name": "", "arguments": ""}})
                call["id"] = d.get("id") or call["id"]
                if d.get("extra_content"):  # Gemini's thought signature: sent back with the call, or refused
                    call["extra_content"] = d["extra_content"]
                fn = d.get("function") or {}
                call["function"]["name"] += fn.get("name") or ""
                call["function"]["arguments"] += fn.get("arguments") or ""
                length = len(call["function"]["arguments"])
                if on_event and length - told.get(index, -WRITING_STEP) >= WRITING_STEP:
                    told[index] = length
                    on_event("writing", json.dumps(writing(call["function"]), ensure_ascii=False))
            finish = choice.get("finish_reason") or finish
    calls = [partial[i] for i in sorted(partial)]
    for call in calls:
        call["id"] = call["id"] or f"call_{uuid.uuid4().hex[:24]}"
    return text, calls, finish, timings, thinking


def _call_index(d: dict, partial: dict[int, dict]) -> int:
    """Which call a streamed piece belongs to: its index (llama-server, Groq), or - from a cloud that sends each
    call whole and without one (Gemini) - a new call for a new id, else the last one."""
    if "index" in d:
        return d["index"]
    if d.get("id") and all(call["id"] != d["id"] for call in partial.values()):
        return len(partial)
    return next((i for i, call in partial.items() if call["id"] == d.get("id")), max(partial, default=0))


WRITING_STEP = 400  # characters between "writing" events: a few a second at the model's speed
WRITTEN_NAME = re.compile(r'"(?:name|file)"\s*:\s*"((?:[^"\\]|\\.)+)"')


def writing(function: dict) -> dict:
    """A call as far as it is written: its tool, the file it names once that is written, its length."""
    args = function.get("arguments") or ""
    named = WRITTEN_NAME.search(args)
    return {"name": function.get("name") or "", "chars": len(args), **({"file": named.group(1)} if named else {})}


def _usage_timings(usage: dict | None) -> dict | None:
    """An OpenAI-style usage report in llama-server's timings shape."""
    if not usage:
        return None
    out, seconds = usage.get("completion_tokens"), usage.get("completion_time")
    total, prompt = usage.get("total_tokens"), usage.get("prompt_tokens")
    thought = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or (
        total - prompt - out if total and prompt is not None and out is not None and total > prompt + out else None)
    return {"prompt_n": prompt, "predicted_n": out,
            "cache_n": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
            "predicted_per_second": out / seconds if out and seconds else None,
            **({"thought_n": thought} if thought else {})}


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


def _is_page(exc: urllib.error.HTTPError) -> bool:
    """The error came as a web page (HTML from a proxy or tunnel), not the model server's own message."""
    return "html" in (exc.headers.get("Content-Type") or "").lower() if exc.headers else False


def _error_message(exc: urllib.error.HTTPError) -> str:
    body = exc.read().decode("utf-8", "replace")
    if body.lstrip()[:1] == "<":  # a page, whatever its header says: never shown as it is
        return exc.reason or f"HTTP {exc.code}"
    try:
        error = json.loads(body).get("error", body)
        return error.get("message", str(error)) if isinstance(error, dict) else str(error)
    except (json.JSONDecodeError, AttributeError):
        return body[:300] or exc.reason
