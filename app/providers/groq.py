"""A model on Groq's cloud (its OpenAI-compatible chat API): for testing the app with a large model this
laptop can't run - BIZ_LLM_PROVIDER=groq, GROQ_API_KEY in .env, BIZ_GROQ_MODEL (default gpt-oss-120b).

    .claude/launch.json "api-groq"      (the app on Groq; "api" is the app on llama-server)

The same requests and stream as llama-server's, less what only llama-server has: slots and their
prompt cache, its exact token counter (an estimate instead), a vision part (none: images are not sent).
gpt-oss always reasons; reasoning_effort is its thinking switch (as on llama-server).

Measured on the free plan (2026-10-07): 1,000 requests a day and 8,000 tokens a minute for both
gpt-oss-120b and qwen3.8-27b, prompt and reply together - a request larger than that is refused. So
the prompt is kept to SLOT_TOKENS less the reply room (the app's usual trimming of older turns), and a
request refused for the per-minute limit waits for it (retry-after) instead of failing the turn.
Groq's firewall refuses Python's default User-Agent (error 1010); the app's own is sent.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from app.tools.net import USER_AGENT

from .base import Cancelled, ProviderError, estimate_tokens
from .llamacpp import LlamaCppProvider

GROQ_URL = "https://api.groq.com/openai"  # + /v1/chat/completions, /v1/models
SLOT_TOKENS = 7_500   # per request, prompt and reply (free plan: 8,000 tokens a minute; the estimate runs high)
MAX_TOKENS = 2_048    # reply room, reasoning included
MAX_WAIT = 75         # longest wait for the per-minute limit, seconds
WAITS = 4             # waits before giving up
LLAMA_ONLY = ("cache_prompt", "id_slot", "chat_template_kwargs", "reasoning_budget_tokens")


class GroqProvider(LlamaCppProvider):
    name = "groq"
    where = "Groq"
    slot_tokens = SLOT_TOKENS
    look = None  # no vision: view_image says so, generate_image skips its self-check

    def __init__(self, model: str, api_key: str, base_url: str = GROQ_URL, thinking: bool = False,
                 max_tokens: int | None = None, cancel=None):
        super().__init__(model, base_url, thinking, max_tokens=max_tokens or MAX_TOKENS, cancel=cancel)
        self.api_key = api_key
        self._vision = False
        self._checked = 0.0  # when the key last worked (ready)

    def _headers(self) -> dict:
        return {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}", "User-Agent": USER_AGENT}

    def ready(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "Add GROQ_API_KEY to the .env file, then restart the app."
        if time.monotonic() - self._checked > 60:  # the page asks often; the key is checked once a minute
            try:
                request = urllib.request.Request(f"{self.base_url}/v1/models", headers=self._headers())
                with urllib.request.urlopen(request, timeout=10):
                    pass
            except urllib.error.HTTPError as exc:
                return False, f"Groq refused the key or the request ({exc.code})."
            except (urllib.error.URLError, OSError):
                return False, "Could not reach Groq; check the internet connection."
            self._checked = time.monotonic()
        return True, f"{self.model} (Groq cloud)"

    def loaded_model(self) -> str:
        return self.model.split("/")[-1]

    def sees_images(self) -> bool:
        return False

    def count_tokens(self, system: str, messages: list[dict], tools: list[dict], *, allow_tools: bool = True) -> int:
        return estimate_tokens(system, messages, tools)

    def _body(self, system, messages, tools, allow_tools, think=True, budget=None) -> dict:
        body = {k: v for k, v in super()._body(system, messages, tools, allow_tools, think, budget).items()
                if k not in LLAMA_ONLY}
        thinking = self.thinking and think
        if "gpt-oss" in self.model:
            body["reasoning_effort"] = "medium" if thinking else "low"
        elif "qwen" in self.model:  # thinking apart from the answer (else it comes inside it), or none
            body["reasoning_format"] = "parsed"
            body["reasoning_effort"] = "default" if thinking else "none"
        return body

    def _open(self, path: str, body: dict, timeout: float, stream: bool = False):
        """As llama-server's, but a request refused for the per-minute limit waits and is sent again."""
        for attempt in range(WAITS + 1):
            try:
                return super()._open(path, body, timeout, stream)
            except urllib.error.HTTPError as exc:
                if exc.code == 413:
                    raise ProviderError("This step is larger than Groq's free limit of 8,000 tokens a minute; "
                                        "start a new chat or ask something shorter.") from exc
                if exc.code != 429 or attempt == WAITS:
                    raise
                if "per day" in _peek(exc):
                    raise ProviderError("Groq's free daily limit is used up; it resets within 24 hours.") from exc
                self._wait(min(float(exc.headers.get("retry-after") or 10), MAX_WAIT))
        raise AssertionError("unreachable")

    def _wait(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:  # the Stop button still stops it
            if self.cancel is not None and self.cancel.is_set():
                raise Cancelled
            time.sleep(0.25)

    def _unreachable(self) -> str:
        return "Could not reach Groq; check the internet connection."


def _peek(exc: urllib.error.HTTPError) -> str:
    try:
        return json.dumps(json.loads(exc.read()))
    except (ValueError, OSError):
        return ""
