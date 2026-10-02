"""A provider that replays a script - for tests and for exercising the whole chat
pipeline (graph, tools, streaming, UI) without calling a real model."""

from __future__ import annotations

import itertools
from collections.abc import Callable

from .base import EventCallback, ModelTurn, ProviderError

_ids = itertools.count(1)


def call(name: str, **inputs) -> dict:
    """A tool call for a scripted turn."""
    return {"id": f"call_{next(_ids)}", "name": name, "input": inputs}


class ScriptedProvider:
    name = "scripted"
    model = "scripted"

    def __init__(self, turns: list[ModelTurn | Callable[[list[dict]], ModelTurn]]):
        self.turns = list(turns)
        self.requests: list[dict] = []  # what each call received, for assertions

    def ready(self) -> tuple[bool, str]:
        return True, "Scripted test model"

    def complete(self, system: str, messages: list[dict], tools: list[dict], *,
                 allow_tools: bool = True, on_event: EventCallback | None = None) -> ModelTurn:
        self.requests.append({"system": system, "messages": list(messages), "tools": tools,
                              "allow_tools": allow_tools})
        if not self.turns:
            raise ProviderError("The scripted model has no more turns.")
        turn = self.turns.pop(0)
        if callable(turn):
            turn = turn(messages)
        if on_event and turn.text:
            for word in turn.text.split(" "):
                on_event("text", word + " ")
        return ModelTurn(text=turn.text, tool_calls=list(turn.tool_calls) if allow_tools else [],
                         raw=None, stop=turn.stop if allow_tools else "end", model=self.model, usage=turn.usage)
