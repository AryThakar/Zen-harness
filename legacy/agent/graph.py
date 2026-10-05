"""The assistant as a LangGraph graph.

    START -> agent -> (tool calls?) -> tools -> agent -> ... -> END

- agent: one model turn with the system prompt (rules + live schema card) and the
  conversation so far; answer text streams out as it is written.
- tools: runs every tool call of that turn (inputs validated first) and appends
  the results; charts and tables travel as artifacts for the UI.
- A question gets at most MAX_TOOL_ROUNDS tool rounds; after that the model must
  answer with what it has (tools stay declared but can't be called).
- A checkpointer keeps each conversation (thread) between requests; the model
  sees the last MAX_EXCHANGES questions of it, cut at question boundaries so tool
  calls stay with their results.

Progress is streamed with LangGraph's custom stream: events
{"type": "text"|"restart"|"tool_start"|"tool_end"|"stats", ...}.
"""

from __future__ import annotations

import operator
import time
from typing import Annotated, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from app.config import settings
from legacy.db.connection import Database
from legacy.rag.library import Library

from .context import schema_card
from .prompts import system_prompt
from app.providers.base import Provider
from .tools import TOOL_SPECS, ToolContext, execute

MAX_TOOL_ROUNDS = 8
MAX_EXCHANGES = 12  # questions (with their tool steps and answers) sent to the model
SUMMARY_CHARS = 500


class ChatState(TypedDict):
    messages: Annotated[list[dict], operator.add]  # append-only, provider-neutral (see providers.base)
    rounds: int  # tool rounds used for the current question


def build_graph(db: Database, provider: Provider, checkpointer=None, currency: str | None = None,
                library: Library | None = None):
    ctx = ToolContext(db=db, currency=currency or settings.currency, library=library)

    def agent(state: ChatState) -> dict:
        writer = get_stream_writer()
        cur = db.cursor()
        try:
            system = system_prompt(schema_card(cur), ctx.currency)
        finally:
            cur.close()
        started = time.perf_counter()
        turn = provider.complete(
            system, recent(state["messages"]), TOOL_SPECS,
            allow_tools=state["rounds"] < MAX_TOOL_ROUNDS,
            on_event=lambda kind, text: writer({"type": kind, "text": text}),
        )
        if turn.usage:  # speed of this turn, for the numbers under the answer
            writer({"type": "stats", **turn.usage, "seconds": round(time.perf_counter() - started, 3)})
        message = {"role": "assistant", "text": turn.text, "tool_calls": turn.tool_calls, "raw": turn.raw,
                   "provider": provider.name, "model": turn.model, "stop": turn.stop}
        return {"messages": [message]}

    def tools(state: ChatState) -> dict:
        writer = get_stream_writer()
        results = []
        for call in state["messages"][-1]["tool_calls"]:
            writer({"type": "tool_start", "id": call["id"], "name": call["name"], "input": call["input"]})
            output = execute(ctx, call["name"], call["input"])
            writer({"type": "tool_end", "id": call["id"], "name": call["name"], "is_error": output.is_error,
                    "summary": summary(output.content), "artifact": output.artifact})
            results.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"],
                            "content": output.content, "is_error": output.is_error, "artifact": output.artifact})
        return {"messages": results, "rounds": state["rounds"] + 1}

    def next_step(state: ChatState) -> str:
        return "tools" if state["messages"][-1].get("tool_calls") else END

    graph = StateGraph(ChatState)
    graph.add_node("agent", agent)
    graph.add_node("tools", tools)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", next_step, ["tools", END])
    graph.add_edge("tools", "agent")
    return graph.compile(checkpointer=checkpointer)


def new_question(text: str) -> dict:
    """Graph input for a user message: appends it and resets the tool-round budget."""
    return {"messages": [{"role": "user", "content": text}], "rounds": 0}


def summary(content: str) -> str:
    """A tool result shortened for display: whole lines, up to SUMMARY_CHARS."""
    if len(content) <= SUMMARY_CHARS:
        return content
    cut = content.rfind("\n", 0, SUMMARY_CHARS)
    return content[: cut if cut > 0 else SUMMARY_CHARS].rstrip() + "\n…"


def recent(messages: list[dict], exchanges: int = MAX_EXCHANGES) -> list[dict]:
    """The messages from the start of the last ``exchanges`` user questions."""
    starts = [i for i, m in enumerate(messages) if m["role"] == "user"]
    return messages[starts[-exchanges]:] if len(starts) > exchanges else messages
