"""What every model provider implements.

Conversation history is kept in a small provider-neutral format (plain dicts, so
LangGraph can checkpoint it), and each provider converts it to its own API shape:

    {"role": "user", "content": "text"}
    {"role": "assistant", "text": "...", "tool_calls": [{"id", "name", "input"}],
     "raw": <provider-native content, replayed unchanged to the same provider>,
     "provider": "anthropic", "model": "...", "stop": "end|tool_use|refusal|max_tokens"}
    {"role": "tool", "tool_call_id": "...", "name": "...", "content": "text for the model",
     "is_error": bool, "artifact": <chart/table data for the UI> | None}
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

# on_event(kind, text): kind "text" streams answer text; "restart" means the
# provider is re-issuing the turn, so text streamed so far should be discarded.
EventCallback = Callable[[str, str], None]


class ProviderError(RuntimeError):
    """A model call failed; the message is safe to show to the user."""


@dataclass
class ModelTurn:
    text: str
    tool_calls: list[dict] = field(default_factory=list)  # [{"id", "name", "input"}]
    raw: list | None = None
    stop: str = "end"  # end | tool_use | refusal | max_tokens
    model: str | None = None


class Provider(Protocol):
    name: str
    model: str

    def ready(self) -> tuple[bool, str]:
        """(can it be called?, a short status or setup instruction)."""

    def complete(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        *,
        allow_tools: bool = True,
        on_event: EventCallback | None = None,
    ) -> ModelTurn:
        """One model turn. ``tools`` are {"name", "description", "input_schema"}.
        With ``allow_tools=False`` the model must answer in text (tools stay
        declared because the history may contain earlier tool calls)."""
