# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""A model on Google's Gemini API (its OpenAI-compatible chat API): BIZ_LLM_PROVIDER=gemini, GEMINI_API_KEY in
.env (a key from aistudio.google.com), BIZ_GEMINI_MODEL (default gemini-3.5-flash). What every cloud shares is in
cloud.py. For testing the app with a stronger model than the local one - the same harness, another model.

    .claude/launch.json "api-gemini"    (the app on Gemini; "api" is the app on llama-server)

Read from the API itself (2026-10-15), not assumed:
- a tool call streams whole, in one piece, with an id and no index (llamacpp._call_index), and finishes "stop";
- a Gemini 3 call carries a thought signature (extra_content.google.thought_signature) that must come back with
  the call on the next request - kept in the call's raw form, replayed unchanged (llamacpp._read_stream);
- usage comes on every chunk; there is no generation time, so no tokens a second;
- gemini-3.5-flash answered a tool call in 1.8 s; gemini-flash-latest was refused for high demand (503, tried
  again by the shared retry).
It sees images (image_url parts, as llama-server's). Its context is far larger than the app uses: SLOT_TOKENS keeps
a turn about the size it is on the local model (BIZ_GEMINI_SLOT changes it).
"""

from __future__ import annotations

from .cloud import CloudProvider

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai"  # + /chat/completions, /models
SLOT_TOKENS = 32_768
MAX_TOKENS = 8_192   # reply room, thinking included


class GeminiProvider(CloudProvider):
    name = "gemini"
    where = "Google's Gemini API"
    label = "Gemini API"
    key_name = "GEMINI_API_KEY"
    CHAT = "/chat/completions"
    MODELS = "/models"
    slot_tokens = SLOT_TOKENS
    max_tokens_default = MAX_TOKENS
    vision = True

    def __init__(self, model: str, api_key: str, base_url: str = GEMINI_URL, thinking: bool = False,
                 max_tokens: int | None = None, cancel=None, slot_tokens: int | None = None):
        super().__init__(model, api_key, base_url, thinking, max_tokens=max_tokens, cancel=cancel,
                         slot_tokens=slot_tokens)

    def thinking_fields(self, thinking: bool) -> dict:
        """Thinking on: the model's own default (it decides how much); off: as little as it allows."""
        return {} if thinking else {"reasoning_effort": "low"}
