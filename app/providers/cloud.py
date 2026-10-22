# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""A model on a cloud's OpenAI-style chat API - OpenAI, Gemini, Groq, OpenRouter, a company's own, or an app on this
computer such as Ollama or LM Studio: the Model settings' Cloud choice (app/model_link.py), three things - its
address (it usually ends in /v1), a key, a model.

The same requests and stream as llama-server's (llamacpp.py), less what only llama-server has: slots and their
prompt cache, its exact token counter (an estimate instead, which runs high), its own fields (stripped). ready()
checks the key once a minute against the cloud's model list. A request refused for a per-minute limit waits for it
and is sent again - the Stop button still stops the wait - and a used-up daily limit is said plainly. The prompt
limit follows SLOT_TOKENS, not the model's own (much larger) context, so a turn costs about what it costs on the
local model.

What some models read differently - found by the model's name, not set by hand:
- gpt-oss (OpenAI's open model, on Groq and others) always reasons; reasoning_effort is its thinking switch;
- Gemini thinks unless told to keep it low (reasoning_effort), and its tool call carries a thought signature that
  must come back with it - kept in the call's raw form, replayed unchanged (llamacpp._assistant_message);
- Qwen on Groq gives its thinking apart only when asked (reasoning_format).
Groq's firewall refuses Python's default User-Agent: the app's own goes everywhere (llamacpp._headers).
A model that can't see pictures refuses one; it is not sent pictures again (sees_images).
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse

from .base import Cancelled, ProviderError, estimate_tokens
from .llamacpp import LlamaCppProvider

LLAMA_ONLY = ("cache_prompt", "id_slot", "chat_template_kwargs", "reasoning_budget_tokens")
SLOT_TOKENS = 32_768  # a request's prompt and reply: about a turn's size on the local model
MAX_TOKENS = 8_192    # reply room, thinking included
MAX_WAIT = 75         # longest wait for a per-minute limit, seconds
WAITS = 4             # waits before giving up


class CloudProvider(LlamaCppProvider):
    name = "cloud"
    CHAT = "/chat/completions"  # under its address, which carries the version (…/v1)
    MODELS = "/models"
    STREAM_EXTRA = {"stream_options": {"include_usage": True}}  # else a cloud may send no usage at all
    FILLS_SLOT = False  # its API caps a reply at max_tokens, whatever the context has left
    slot_tokens = SLOT_TOKENS  # a plain number here (llama-server's is a property, read from the server)

    def __init__(self, model: str, api_key: str, base_url: str, thinking: bool = False,
                 max_tokens: int | None = None, cancel=None, slot_tokens: int | None = None):
        super().__init__(model, base_url, thinking, max_tokens=max_tokens or MAX_TOKENS, cancel=cancel, api_key=api_key)
        self.slot_tokens = slot_tokens or SLOT_TOKENS
        host = urllib.parse.urlsplit(self.base_url).hostname or "the cloud"
        self.where = host.removeprefix("api.").removeprefix("www.")  # e.g. openai.com, groq.com
        self._vision = True  # until the model refuses a picture
        self._checked = 0.0  # when the key last worked

    def health(self) -> tuple[str, str]:
        """ready or down (a cloud doesn't load): the key and the connection, checked once a minute."""
        if time.monotonic() - self._checked > 60:  # the page asks often
            try:
                with self._get(self.MODELS, 10):
                    pass
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    return "down", f"{self.where} refused the API key; check it in Zen's Model settings."
                return "down", f"{self.where} refused the request ({exc.code}); check the address in Zen's Model settings."
            except (urllib.error.URLError, OSError):
                return "down", f"Could not reach {self.where}; check the internet connection."
            self._checked = time.monotonic()
        return "ready", f"{self.loaded_model()} ({self.where})"

    def location(self) -> str:
        return f"on {self.where}"

    def loaded_model(self) -> str:
        return self.model.split("/")[-1]

    def sees_images(self) -> bool:
        return self._vision

    def count_tokens(self, system: str, messages: list[dict], tools: list[dict], *, allow_tools: bool = True) -> int:
        return estimate_tokens(system, messages, tools)

    def _clean(self, body: dict) -> dict:
        """llama-server's own fields out - a step's reasoning_content among them (a cloud keeps its own thinking:
        Gemini's thought signature); the model's thinking switch in."""
        out = {k: v for k, v in body.items() if k not in LLAMA_ONLY}
        if "messages" in out:
            out["messages"] = [{k: v for k, v in m.items() if k != "reasoning_content"} for m in out["messages"]]
        return out

    def _body(self, system, messages, tools, allow_tools, think=True, budget=None, require_tool=False,
              prompt_tokens=None, sampling=None) -> dict:
        body = self._clean(super()._body(system, messages, tools, allow_tools, think, budget, require_tool,
                                         prompt_tokens, sampling))
        return {**body, **self.thinking_fields(self.thinking and think)}

    def thinking_fields(self, thinking: bool) -> dict:
        """The request's fields that switch thinking on or off, in this model's words (none for most)."""
        model = self.model.lower()
        if "gpt-oss" in model:
            return {"reasoning_effort": "medium" if thinking else "low"}
        if "qwen" in model and self.where == "groq.com":  # its thinking apart from the answer, or none
            return {"reasoning_format": "parsed", "reasoning_effort": "default" if thinking else "none"}
        if "gemini" in model:  # thinking on: the model's own default
            return {} if thinking else {"reasoning_effort": "low"}
        return {}

    def look(self, image: bytes, mime: str, question: str) -> str:
        if not self._vision:
            raise ProviderError(f"{self.loaded_model()} can't see pictures.")
        return super().look(image, mime, question)

    def _open(self, path: str, body: dict, timeout: float, stream: bool = False):
        """As llama-server's, but a request refused for the per-minute limit waits and is sent again, and a picture
        refused means the model can't see them."""
        for attempt in range(WAITS + 1):
            try:
                return super()._open(path, body, timeout, stream)
            except urllib.error.HTTPError as exc:
                if exc.code == 413:
                    raise ProviderError(self.too_big()) from exc
                if exc.code == 400 and '"image_url"' in json.dumps(body):
                    said = _peek(exc)
                    if "image" in said.lower() or "vision" in said.lower():
                        self._vision = False
                        raise ProviderError(f"{self.loaded_model()} can't see pictures; Zen won't send it any "
                                            "more.") from exc
                    raise ProviderError(f"{self.where} returned an error (400): {said[:300]}") from exc
                if exc.code != 429 or attempt == WAITS:
                    raise
                said = _peek(exc)
                if self.daily(said):
                    raise ProviderError(f"{self.where}'s daily limit for {self.loaded_model()} is used up; it resets "
                                        "within 24 hours.") from exc
                self._wait(min(self.retry_after(exc, said), MAX_WAIT))
        raise AssertionError("unreachable")

    def daily(self, said: str) -> bool:
        """Is this 429 a used-up daily limit (no point waiting)?"""
        said = said.lower()
        return "per day" in said or "perday" in said

    def retry_after(self, exc: urllib.error.HTTPError, said: str) -> float:
        """How long the cloud says to wait: a retryDelay in its error (Gemini: "21s"), else its retry-after header."""
        delay = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', said)
        return float(delay.group(1) if delay else exc.headers.get("retry-after") or 10)

    def too_big(self) -> str:
        return f"This step is larger than {self.where} accepts; start a new chat or ask something shorter."

    def _wait(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:  # the Stop button still stops it
            if self.cancel is not None and self.cancel.is_set():
                raise Cancelled
            time.sleep(0.25)

    def _unreachable(self) -> str:
        return f"Could not reach {self.where}; check the internet connection."


def _peek(exc: urllib.error.HTTPError) -> str:
    try:
        return json.dumps(json.loads(exc.read()))
    except (ValueError, OSError):
        return ""
