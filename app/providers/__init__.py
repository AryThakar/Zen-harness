"""The assistant's model providers: llama-server (both slots - main and the helpers), a cloud's model for testing
the app with a stronger one (cloud.py: NVIDIA Build in nvidia.py, Google's Gemini API in gemini.py, Groq in groq.py), and a scripted model for
tests."""

from __future__ import annotations

from .base import ModelTurn, Provider, ProviderError
from .llamacpp import LlamaCppProvider
from .scripted import ScriptedProvider

__all__ = ["LlamaCppProvider", "ModelTurn", "Provider", "ProviderError", "ScriptedProvider"]
