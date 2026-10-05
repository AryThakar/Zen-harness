"""The assistant's model providers: llama-server (both slots - main and the helpers) and a scripted
model for tests. The legacy product's Claude provider lives with it (legacy/agent/claude.py)."""

from __future__ import annotations

from .base import ModelTurn, Provider, ProviderError
from .llamacpp import LlamaCppProvider
from .scripted import ScriptedProvider

__all__ = ["LlamaCppProvider", "ModelTurn", "Provider", "ProviderError", "ScriptedProvider"]
