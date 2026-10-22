# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""The assistant's model providers: llama-server (both slots - main and the helpers; on this computer or the Kaggle
notebook), any cloud's OpenAI-style API (cloud.py), and a scripted model for tests."""

from __future__ import annotations

from .base import ModelTurn, Provider, ProviderError
from .llamacpp import LlamaCppProvider
from .scripted import ScriptedProvider

__all__ = ["LlamaCppProvider", "ModelTurn", "Provider", "ProviderError", "ScriptedProvider"]
