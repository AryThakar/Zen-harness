"""The legacy chat's model: Claude (the Anthropic API) or a local llama-server, from the settings."""

from __future__ import annotations

from app.config import Settings
from app.providers import LlamaCppProvider, Provider

from .claude import ClaudeProvider


def make_provider(settings: Settings) -> Provider:
    if settings.llm_provider == "anthropic":
        return ClaudeProvider(settings.llm_model, settings.llm_effort, settings.anthropic_base_url)
    if settings.llm_provider == "llamacpp":
        return LlamaCppProvider(settings.llamacpp_model, settings.llamacpp_url, settings.llm_thinking)
    raise ValueError(f"Unknown BIZ_LLM_PROVIDER '{settings.llm_provider}'. Supported: anthropic, llamacpp.")
