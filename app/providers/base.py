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

import json

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

# on_event(kind, text): kind "text" streams answer text; "restart" means the
# provider is re-issuing the turn, so text streamed so far should be discarded; "writing" is a call being
# written, as JSON {"name": the tool, "file": the name it writes when it has one, "chars": its length so far} -
# a long file takes minutes, and the page shows it growing instead of a line that seems stuck.
EventCallback = Callable[[str, str], None]


class ProviderError(RuntimeError):
    """A model call failed; the message is safe to show to the user."""


class Cancelled(BaseException):
    """The user stopped the turn. A BaseException, like asyncio's CancelledError: the crash
    boundaries around tools and workers catch Exception and must let a stop through."""


@dataclass
class ModelTurn:
    text: str
    tool_calls: list[dict] = field(default_factory=list)  # [{"id", "name", "input"}]
    raw: list | None = None
    stop: str = "end"  # end | tool_use | refusal | max_tokens
    model: str | None = None
    # How the turn went, for the speed shown under each answer: output_tokens, and when
    # the provider knows them prompt_tokens and tokens_per_second (generation only).
    usage: dict | None = None
    # The model's thinking, when it thought: kept on the step and sent back with it (llamacpp.py), measured too.
    thinking: str = ""
    # A call the length limit cut off part way (stop "max_tokens"): {"name", "chars"} - it never runs, and the
    # model is told it was too long rather than to think less (measured: a web helper's reply was cut off three
    # times in a row, 3.5 minutes each, told each time only that it was cut off).
    cut_call: dict | None = None


IMAGE_TOKENS = 560  # an image's most (llama-server --image-max-tokens 560; a chart measured about 290)


def estimate_tokens(system: str, messages: list[dict], tools: list[dict]) -> int:
    """A guess (3.5 characters a token) for providers that cannot count: a scripted model, or an
    API that bills by its own count. llama-server counts exactly (LlamaCppProvider.count_tokens).
    An image counts as IMAGE_TOKENS, not as the length of its base64 text."""
    images = sum(len(m.get("images") or []) for m in messages)
    sent = [{k: m[k] for k in (SENT[m["role"]] if m.get("role") in SENT else m) if k in m} for m in messages]
    return int(len(json.dumps([system, sent, tools], ensure_ascii=False, default=str)) / 3.5) + 1 + images * IMAGE_TOKENS


# What a message sends the model, by its role - a stored message also keeps what isn't sent (a user message's own
# words beside its stamped content, the time, the files' cards): counted, the user's words counted twice.
SENT = {"user": ("role", "content"), "tool": ("role", "content", "tool_call_id"),
        "assistant": ("role", "text", "tool_calls")}


def answered_ids(messages: list[dict], index: int) -> set[str]:
    """Ids of the tool calls of the assistant turn at ``index`` that got a result.
    Calls that never ran (refused, truncated, round limit) must not be replayed."""
    ids = set()
    for msg in messages[index + 1:]:
        if msg["role"] != "tool":
            break
        ids.add(msg["tool_call_id"])
    return ids


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
        require_tool: bool = False,
        on_event: EventCallback | None = None,
        think: bool = True,
        budget: int | None = None,
        prompt_tokens: int | None = None,
        sampling: dict | None = None,
    ) -> ModelTurn:
        """One model turn. ``tools`` are {"name", "description", "input_schema"}.
        With ``allow_tools=False`` the model must answer in text (tools stay
        declared because the history may contain earlier tool calls); with ``require_tool`` it must call one
        of them (a helper: it acts or finishes, never answers in prose). ``think=False``
        switches thinking off for this call; ``budget`` caps its thinking tokens (main's
        "answer quickly" retry after a reply that thought until the length limit). ``prompt_tokens``, the prompt's
        exact size when the caller counted it, lets a server with a fixed slot give the reply all the room left;
        ``sampling`` sets this call's own sampling over the server's."""
