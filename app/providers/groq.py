# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""A model on Groq's cloud (its OpenAI-compatible chat API): BIZ_LLM_PROVIDER=groq, GROQ_API_KEY in .env,
BIZ_GROQ_MODEL (default gpt-oss-120b). What every cloud shares is in cloud.py.

    .claude/launch.json "api-groq"      (the app on Groq; "api" is the app on llama-server)

No vision part here (images are not sent). gpt-oss always reasons; reasoning_effort is its thinking switch.

Measured on the free plan (2026-10-07): 1,000 requests a day and 8,000 tokens a minute for both
gpt-oss-120b and qwen3.8-27b, prompt and reply together - a request larger than that is refused. So
the prompt is kept to SLOT_TOKENS less the reply room (the app's usual trimming of older turns). Too small for
the app's full tests (one call a minute); Gemini (gemini.py) is the cloud for those.
Groq's firewall refuses Python's default User-Agent (error 1010); the app's own is sent.
"""

from __future__ import annotations

from app.tools.net import USER_AGENT

from .cloud import CloudProvider

GROQ_URL = "https://api.groq.com/openai"  # + /v1/chat/completions, /v1/models
SLOT_TOKENS = 7_500   # per request, prompt and reply (free plan: 8,000 tokens a minute; the estimate runs high)
MAX_TOKENS = 2_048    # reply room, reasoning included


class GroqProvider(CloudProvider):
    name = "groq"
    where = "Groq"
    label = "Groq cloud"
    key_name = "GROQ_API_KEY"
    slot_tokens = SLOT_TOKENS
    max_tokens_default = MAX_TOKENS
    look = None  # no vision: view_image says so, generate_image skips its self-check

    def __init__(self, model: str, api_key: str, base_url: str = GROQ_URL, thinking: bool = False,
                 max_tokens: int | None = None, cancel=None):
        super().__init__(model, api_key, base_url, thinking, max_tokens=max_tokens, cancel=cancel)

    def _headers(self) -> dict:
        return {**super()._headers(), "User-Agent": USER_AGENT}

    def thinking_fields(self, thinking: bool) -> dict:
        if "gpt-oss" in self.model:
            return {"reasoning_effort": "medium" if thinking else "low"}
        if "qwen" in self.model:  # thinking apart from the answer (else it comes inside it), or none
            return {"reasoning_format": "parsed", "reasoning_effort": "default" if thinking else "none"}
        return {}

    def too_big(self) -> str:
        return ("This step is larger than Groq's free limit of 8,000 tokens a minute; start a new chat or ask "
                "something shorter.")
