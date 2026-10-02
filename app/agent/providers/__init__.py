"""Model providers for the AI assistant."""

from __future__ import annotations

from app.config import Settings

from .base import ModelTurn, Provider, ProviderError
from .claude import ClaudeProvider
from .llamacpp import LlamaCppProvider
from .scripted import ScriptedProvider


def make_provider(settings: Settings) -> Provider:
    if settings.llm_provider == "anthropic":
        return ClaudeProvider(settings.llm_model, settings.llm_effort, settings.anthropic_base_url)
    if settings.llm_provider == "llamacpp":
        return LlamaCppProvider(settings.llamacpp_model, settings.llamacpp_url, settings.llm_thinking)
    raise ValueError(f"Unknown BIZ_LLM_PROVIDER '{settings.llm_provider}'. Supported: anthropic, llamacpp.")


__all__ = ["ClaudeProvider", "LlamaCppProvider", "ModelTurn", "Provider", "ProviderError", "ScriptedProvider",
           "make_provider"]
