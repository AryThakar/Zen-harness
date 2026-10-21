# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""A model on NVIDIA Build (build.nvidia.com, its OpenAI-compatible API): BIZ_LLM_PROVIDER=nvidia, NVIDIA_API_KEY in
.env (a key from build.nvidia.com, nvapi-...), BIZ_NVIDIA_MODEL. What every cloud shares is in cloud.py. For testing
the app's harness with a strong model: its free use is for development, about 40 requests a minute (2026-10-15:
Gemini's free tier allowed 20 requests a day per Flash model - too few to test with).

Measured on the free plan (2026-10-16): moonshotai/kimi-k2.6 is listed but refused this account (404), and
deepseek-ai/deepseek-v4.1-flash (the default) sent nothing back for four minutes on its first request - the free
endpoints queue; not usable for the full tests that day.

    .claude/launch.json "api-nvidia"    (the app on NVIDIA Build; "api" is the app on llama-server)

No images are sent (vision is off until a model here is checked for it). Its context is far larger than the app uses:
SLOT_TOKENS keeps a turn about the size it is on the local model.
"""

from __future__ import annotations

from .cloud import CloudProvider

NVIDIA_URL = "https://integrate.api.nvidia.com/v1"  # + /chat/completions, /models
SLOT_TOKENS = 32_768
MAX_TOKENS = 8_192   # reply room, thinking included


class NvidiaProvider(CloudProvider):
    name = "nvidia"
    where = "NVIDIA Build"
    label = "NVIDIA Build"
    key_name = "NVIDIA_API_KEY"
    CHAT = "/chat/completions"
    MODELS = "/models"
    slot_tokens = SLOT_TOKENS
    max_tokens_default = MAX_TOKENS
    look = None  # no vision here: view_image says so

    def __init__(self, model: str, api_key: str, base_url: str = NVIDIA_URL, thinking: bool = False,
                 max_tokens: int | None = None, cancel=None):
        super().__init__(model, api_key, base_url, thinking, max_tokens=max_tokens, cancel=cancel)
