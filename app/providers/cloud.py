# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""A model on a cloud's OpenAI-compatible chat API: for testing the app with a model this laptop can't run.
Two clouds use it - Google's Gemini API (gemini.py) and Groq (groq.py) - each a few settings on this class.

The same requests and stream as llama-server's (llamacpp.py), less what only llama-server has: slots and their
prompt cache, its exact token counter (an estimate instead, which runs high), its own fields (stripped). The key
goes in the .env file; ready() checks it once a minute against the cloud's model list. A request refused for a
per-minute limit waits for it and is sent again - the Stop button still stops the wait - and a used-up daily
limit is said plainly. The model is the user's choice: the prompt limit follows SLOT_TOKENS, not the model's own
(much larger) context, so a turn costs about what it costs on the local model.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

from .base import Cancelled, ProviderError, estimate_tokens
from .llamacpp import LlamaCppProvider

LLAMA_ONLY = ("cache_prompt", "id_slot", "chat_template_kwargs", "reasoning_budget_tokens")
MAX_WAIT = 75   # longest wait for a per-minute limit, seconds
WAITS = 4       # waits before giving up


class CloudProvider(LlamaCppProvider):
    """A cloud's chat API. A subclass sets: name and where (in messages), label (shown after the model), key_name
    (the .env variable), slot_tokens and max_tokens (per request), vision, and its thinking switch."""
    name = "cloud"
    where = "the cloud model"
    label = "cloud"
    key_name = "API_KEY"
    slot_tokens = 32_768
    max_tokens_default = 8_192
    vision = False
    STREAM_EXTRA = {"stream_options": {"include_usage": True}}  # else a cloud may send no usage at all
    FILLS_SLOT = False  # its API caps a reply at max_tokens, whatever the context has left

    def __init__(self, model: str, api_key: str, base_url: str, thinking: bool = False,
                 max_tokens: int | None = None, cancel=None, slot_tokens: int | None = None):
        super().__init__(model, base_url, thinking, max_tokens=max_tokens or self.max_tokens_default, cancel=cancel)
        self.api_key = api_key
        if slot_tokens:
            self.slot_tokens = slot_tokens
        self._vision = self.vision
        self._checked = 0.0  # when the key last worked

    def _headers(self) -> dict:
        return {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}

    def health(self) -> tuple[str, str]:
        """ready or down (a cloud doesn't load): the key and the connection, checked once a minute."""
        if not self.api_key:
            return "down", f"Add {self.key_name} to the .env file, then restart the app."
        if time.monotonic() - self._checked > 60:  # the page asks often
            try:
                request = urllib.request.Request(f"{self.base_url}{self.MODELS}", headers=self._headers())
                with urllib.request.urlopen(request, timeout=10):
                    pass
            except urllib.error.HTTPError as exc:
                return "down", f"{self.where} refused the key or the request ({exc.code})."
            except (urllib.error.URLError, OSError):
                return "down", f"Could not reach {self.where}; check the internet connection."
            self._checked = time.monotonic()
        return "ready", f"{self.model} ({self.label})"

    def location(self) -> str:
        return f"on {self.where}"

    def loaded_model(self) -> str:
        return self.model.split("/")[-1]

    def sees_images(self) -> bool:
        return self.vision

    def count_tokens(self, system: str, messages: list[dict], tools: list[dict], *, allow_tools: bool = True) -> int:
        return estimate_tokens(system, messages, tools)

    def _clean(self, body: dict) -> dict:
        """llama-server's own fields out - a step's reasoning_content among them (a cloud keeps its own thinking:
        Gemini's thought signature); the cloud's thinking switch in."""
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
        """The request's fields that switch thinking on or off, in this cloud's words."""
        return {}

    def _open(self, path: str, body: dict, timeout: float, stream: bool = False):
        """As llama-server's, but a request refused for the per-minute limit waits and is sent again."""
        for attempt in range(WAITS + 1):
            try:
                return super()._open(path, body, timeout, stream)
            except urllib.error.HTTPError as exc:
                if exc.code == 413:
                    raise ProviderError(self.too_big()) from exc
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
        return "per day" in said.lower() or "perday" in said.lower()

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
