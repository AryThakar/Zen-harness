"""The main slot: talks with the user, does quick things itself and hands focused work to the workers.

A turn is the user's message, then model steps and tool calls until the model answers. It runs as a
LangGraph graph - agent -> tools -> agent ... -> END - streamed through LangGraph's custom stream,
and every message is saved to the store as it happens (the store is the history; no checkpointer).

What the main model gets at each step:
    system   main's fixed prompt (prompts.MAIN: its persona, then how it works), then agent.md as it is now
             (agent_md.py: in view every turn, never in the history). Nothing else goes there: the
             workspace and the time travel on the user's messages (context.py) - a minute that changed in
             the system prompt made llama.cpp process the whole prompt again (measured: 5,673 of 5,878
             tokens); agent.md changes rarely.
    history  the chat through the rolling window (cut_turns; the cut is kept in chats.dropped so the
             prompt stays the same turn after turn; a turn that alone outgrows the slot has its older
             tool results shortened). Each user message starts with its local time,
             "[Fri 03 Oct 2026, 14:05]", written once when it arrives.
    tools    9 of its own (workers.MAIN_DIRECT) + 3 delegates to the helpers (research_web, ask_knowledge,
             analyze_or_code). Its instructions and nudges are in prompts.py.

ask_user pauses the turn: the question goes to the UI and into chats.pending; the user's answer comes
back as that call's result and the turn goes on. A helper's own ask_user (analyze_or_code) pauses in place
instead: the turn keeps running and the helper waits (Turn.ask); the request that is streaming ends at
the question, the user's answer (POST /answer) is handed to the waiting helper and its request streams
the rest of the turn - the helper keeps everything it did (before, it ended, main asked, and the task
started again from nothing). A helper's question_for_user pauses it the same way, by code: the model
never has to copy the question and its options into ask_user (a small model fumbles that), and the
answer comes back as the helper call's result, to hand the task back with. A reply cut off by the
output limit gets another step. When the model answers, the turn is indexed for recall (remember_turn: the user's words, the
tools called, the answer) and a new chat is named after its first message.

Every turn leaves a trace (Live.trace, stored as a 'trace' row when it ends - done, waiting for the user,
stopped or failed): main's steps (seconds, stop, calls, tokens), each tool call (status, seconds), each
helper's steps, results and checks, the hidden checks that fired, and the error if it failed - measured
facts for the evaluation and a developer's view, instead of reconstructing a turn from its messages.

Events streamed to the UI (dicts with "type"):
    text {text}  ·  tool_start {id, name, input}  ·  tool_end {id, name, status, summary}
    artifact {artifact: {tool, type, ...}}  ·  worker {worker, event, ...}  ·  stats {output_tokens, ...}
    question {question, options, ...}  ·  done {answer}  ·  stopped {}  ·  error {message}
"""

from __future__ import annotations

import json
import logging
import operator
import re
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from app.providers.base import Cancelled, Provider, ProviderError
from app.tools import agent_md
from app.tools.approval import carry_out
from app.tools.ask import answer_result
from app.tools.clean_data import clean_data
from app.tools.core import ToolResult
from app.tools.diagram import mend_blocks
from app.tools.memory import remember_turn
from app.tools.search_index import SearchIndex
from app.tools.workspace import Workspace, present

from .checks import SAMPLE_DATA, invented_numbers, nudge_for, repeated_failure, this_turn
from . import snapshot
from .context import (counter, cut_turns, message_context, prompt_limit, shorten_call_args, shorten_tool_results,
                      standing_instructions,
                      without_notes)
from .prompts import MAIN, MAIN_CUT_OFF, QUICK, SHOWN
from .registry import ToolContext, execute, split_confirm
from .store import NEW_CHAT, Store
from .workers import DELEGATES, main_tools

log = logging.getLogger(__name__)
MAX_ROUNDS = 8  # tool rounds in one turn; then the model must answer with what it has
SUMMARY_CHARS = 300
ASK_TIMEOUT = 600  # seconds a helper waits for the user's answer; then it goes on with its default

# A reply that thought until the length limit and said nothing: the user decides (the user's design -
# 2 such replies in 323 messages; no thinking budget otherwise, since a cut mid-thought can break the
# model's work). The card "Try again, answer quickly"; clicked, this note goes on a hidden message and
# that one call thinks with a budget, so it always answers (measured on such a question: "think briefly"
# alone was still cut off mid-answer; with a 2,048-token budget it answered in full in 45 s). The prompts and
# nudges themselves are in prompts.py.
TRY_AGAIN = "Try again, answer quickly"
QUICK_BUDGET = 2048  # thinking tokens for that retry
TOO_LONG = "The model thought for too long and ran out of room before answering."
# One turn at a time: the model server has one main and one worker slot. STOP is the slots' stop
# signal - the providers check it on every streamed line (app/main.py hands it to them).
_busy = threading.Lock()
_state = threading.Lock()  # guards _live and the setting of STOP
_live: Live | None = None  # the running turn, or the last one
STOP = threading.Event()
STOP_WAIT = 20  # seconds a new turn waits for a stopped one to wind down
STAMP = re.compile(r"^\s*\[(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{2} \w{3} \d{4}, \d{2}:\d{2}\]\s*")


class _Unstamped:
    """Streams a reply's text without a copy of the time stamp at its start: a small model mimics the
    stamp it sees on every user message. Only the first characters are held back, until it is clear
    whether a stamp is there."""

    def __init__(self, write):
        self.write, self.head, self.open = write, "", True

    def __call__(self, kind: str, text: str) -> None:
        if kind != "text" or not self.open:
            self.write({"type": kind, "text": text})
            return
        self.head += text
        stripped = self.head.lstrip()
        if stripped.startswith("[") and "]" not in stripped and len(stripped) < 40:
            return  # could still be a stamp
        self.flush()

    def flush(self) -> None:
        if self.open and self.head:
            rest = STAMP.sub("", self.head, count=1)
            if rest:
                self.write({"type": "text", "text": rest})
        self.open, self.head = False, ""


class TurnState(TypedDict):
    messages: Annotated[list[dict], operator.add]
    rounds: int
    paused: bool


@dataclass
class Turn:
    """What the graph's nodes need for one turn (passed in the run's config)."""
    store: Store
    chat_id: str
    ws: Workspace
    index: SearchIndex
    main: Provider
    worker: Provider
    user_text: str
    dropped: int
    write: object = None
    tools: dict = field(default_factory=dict)
    visible_since: str | None = None  # the first turn main still sees, when older ones were cut
    live: Live | None = None  # the turn's events, for a helper's question asked in place
    shortened: int = 0  # this turn's older tool results shown shortened (context.shorten_tool_results)
    cut: int = 0  # this turn's older calls whose long texts are shown cut (context.shorten_call_args)
    snapshot: str | None = None  # the pinned and pointed-at files for this turn's message (snapshot.py), made once

    def setup(self, stream) -> None:
        """The tools for this turn, wired to the stream and the store. Every event written checks for a
        stop, so a stopped turn also ends between a worker's steps, not only during model calls."""
        def write(event: dict) -> None:
            if STOP.is_set():
                raise Cancelled
            stream(event)

        def emit(artifact: dict) -> None:
            # Nested: an artifact has its own "type" (chart, table, question ...).
            write({"type": "artifact", "artifact": artifact})
            self.store.append(self.chat_id, {"role": "artifact", "artifact": artifact})

        self.write = write
        self.ctx = ToolContext(ws=self.ws, index=self.index, conversation=self.chat_id, emit=emit,
                               visible_since=self.visible_since, ask=self.ask if self.live else None,
                               look=getattr(self.worker, "look", None),  # vision on the helpers' slot
                               pin=self.pin)
        def progress(event: dict) -> None:
            if self.live is not None:
                self.live.note_helper(event)
            write(_worker_event(event))

        self.tools = main_tools(self.worker, self.ctx, progress=progress)


    def pin(self, key: str, on: bool) -> list[str]:
        """Pin a file to this chat, or unpin it; the chat's pins now."""
        pins = [k for k in self.store.chat(self.chat_id)["pins"] if k != key] + ([key] if on else [])
        self.store.update_chat(self.chat_id, pins=pins)
        return pins

    def with_snapshot(self, visible: list[dict]) -> list[dict]:
        """The messages with this turn's snapshot on its user message - made once a turn, so every step sends the same
        prompt; never stored (snapshot.py)."""
        at = next((i for i in range(len(visible) - 1, -1, -1) if visible[i]["role"] == "user"
                   and not visible[i].get("hidden")), None)
        if at is None:
            return visible
        message = visible[at]
        if self.snapshot is None:
            pointed = [f["id"] for f in message.get("files") or [] if isinstance(f, dict) and f.get("id")]
            self.snapshot = snapshot.build(self.ws, self.index, self.store.chat(self.chat_id)["pins"], pointed,
                                           message.get("text") or self.user_text)
        if not self.snapshot:
            return visible
        return [*visible[:at], {**message, "content": f"{message['content']}\n\n{self.snapshot}"}, *visible[at + 1:]]

    def ask(self, card: dict) -> dict:
        """A helper's question, asked in place: the card goes to the UI, the helper waits here until the
        user answers (POST /answer -> reply), skips, stops the turn or doesn't answer in ASK_TIMEOUT.
        Returns {'answer': text} or {'skipped': True} or {'timeout': True}."""
        live = self.live
        waiting = live.wait_for_answer()
        card = {**card, "call_id": self.ctx.call_id}
        self.ctx.emit({"type": "question", **card})
        self.write({"type": "question", "inplace": True, **card})
        deadline = time.monotonic() + ASK_TIMEOUT
        while not waiting.wait(0.5):
            if STOP.is_set():
                raise Cancelled
            if time.monotonic() > deadline:
                live.answer(None, timed_out=True)
                return {"timeout": True}
        said = live.answer_value
        return {"skipped": True} if said is None else {"answer": said}


def _agent(state: TurnState, config) -> dict:
    t: Turn = config["configurable"]["turn"]
    t.setup(get_stream_writer())
    specs = [tool.spec() for tool in t.tools.values()]
    history = [m for m in state["messages"] if m["role"] in ("user", "assistant", "tool")]
    # The fixed prompt, then agent.md as it is now - never in the history; the workspace travels on the user's
    # messages (context.py)
    system = f"{MAIN}\n\n{agent_md.in_view(t.ws)}" if agent_md.key(t.ws) else MAIN
    limit, exact = prompt_limit(t.main), counter(t.main, system, specs)
    count = lambda messages: exact(t.with_snapshot(messages))  # the snapshot rides on this turn's message: it counts
    visible, dropped, fits = cut_turns(history, limit, count, t.dropped)
    if dropped != t.dropped:
        if dropped > t.dropped:  # turns were cut: the prompt's start changed anyway, so a fresh snapshot
            visible = _snapshot_on_newest(t, history, visible, dropped)
        t.dropped = dropped
        t.store.update_chat(t.chat_id, dropped=dropped)
    t.visible_since = visible[0].get("at") if dropped and visible else None  # recall reaches what was cut
    if t.shortened or not fits:  # this turn alone outgrew the slot: its older tool results are shortened
        visible, t.shortened = shorten_tool_results(visible, limit, count, t.shortened)
    if t.cut or count(visible) > limit:  # then its older calls' long texts (a file's whole text: the file keeps it)
        visible, t.cut = shorten_call_args(visible, limit, count, t.cut)
    if count(visible) > limit:  # then the newest results too
        visible, _ = shorten_tool_results(visible, limit, count, 0, keep_last=0)
    if count(visible) > limit:  # nothing left to shorten: never sent over the limit (the server would refuse it)
        raise ProviderError("This turn grew too long for the model's context, even with its older results "
                            "shortened. Ask for less at once, or start a new chat.")
    visible = t.with_snapshot(visible)  # the pinned and pointed-at files, for this turn only
    last = state["rounds"] >= MAX_ROUNDS
    started = time.perf_counter()
    stream = _Unstamped(t.write)
    quick = bool(history) and bool(history[-1].get("quick"))  # the user asked for a quick answer
    turn = t.main.complete(system, visible, specs, allow_tools=not last, on_event=stream,
                           budget=QUICK_BUDGET if quick else None)
    stream.flush()
    if t.live is not None:
        t.live.note("steps", {"seconds": round(time.perf_counter() - started, 2), "stop": turn.stop,
                              "calls": [c["name"] for c in turn.tool_calls], **({"usage": turn.usage} if turn.usage else {})})
    turn.text = mend_blocks(without_notes(STAMP.sub("", turn.text, count=1)))
    if turn.usage:
        t.write({"type": "stats", **turn.usage, "seconds": round(time.perf_counter() - started, 3)})
    message = {"role": "assistant", "text": turn.text, "tool_calls": turn.tool_calls, "raw": turn.raw,
               "provider": t.main.name, "model": turn.model, "stop": turn.stop,
               # kept (thinking length included) so real chats show how long the model thinks; the
               # thinking itself is not kept
               **({"usage": turn.usage} if turn.usage else {})}
    t.store.append(t.chat_id, message)
    new = [message]
    if turn.stop == "max_tokens" and not turn.tool_calls and not turn.text.strip() and not quick:
        # Thought until the limit, said nothing: the user decides whether to try again quickly.
        pending = {"retry": True, "question": TOO_LONG, "options": [TRY_AGAIN], "allow_text": False,
                   "allow_skip": True}
        t.ctx.emit({"type": "question", **pending})
        t.store.update_chat(t.chat_id, pending=pending)
        t.write({"type": "question", **pending})
        return {"messages": new, "paused": True}
    if turn.stop == "max_tokens" and not turn.tool_calls and not last:
        nudge = {"role": "user", "content": MAIN_CUT_OFF, "hidden": True}  # the UI does not show it
        t.store.append(t.chat_id, nudge)
        new.append(nudge)
        return {"messages": new, "rounds": state["rounds"] + 1}
    check = (nudge_for(turn.text, this_turn(history + [message]), exists=lambda name: t.ws.find(name) is not None)
             if not turn.tool_calls and not last else None)
    if check:  # announced or claimed an action no tool did: one hidden nudge per turn (checks.py)
        if t.live is not None:
            t.live.note("checks", check[:120])
        nudge = {"role": "user", "content": check, "hidden": True, "check": True}
        t.store.append(t.chat_id, nudge)
        t.write({"type": "checking"})  # the page says it is checking, instead of a finished reply that won't end
        new.append(nudge)
        return {"messages": new, "rounds": state["rounds"] + 1}
    return {"messages": new}


def _stopped(t: Turn, name: str) -> dict:
    """A call the user stopped, as its result: what its helper ran, made and last printed - so the next turn goes
    on from it (measured: main resent a stopped task three times from the start, though the helper had found the
    answer 95 s into the first)."""
    trace = t.ctx.trace
    out = {"status": "stopped", "note": "The user stopped this call. What it did before the stop is below and is "
                                       "kept; go on from it if the user asks to continue."}
    if trace is None:
        return out
    from .workers.runner import _ran
    made = [k for k in t.ws.ids() if k not in trace.existing]
    changed = [k for k, sha in trace.stamps.items() if k in t.ws.ids() and t.ws.entry(k).get("sha", "") != sha]
    out["ran"] = _ran(trace)
    if made or changed:
        out["files"] = [t.ws.label(k) for k in made] + [f"{t.ws.label(k)} (changed)" for k in changed]
    if trace.last_output:
        out["last_run_printed"] = trace.last_output[-600:]
    t.ctx.trace = None
    return out


def _tools(state: TurnState, config) -> dict:
    t: Turn = config["configurable"]["turn"]
    t.setup(get_stream_writer())
    results = []
    for call in state["messages"][-1]["tool_calls"]:
        t.write({"type": "tool_start", "id": call["id"], "name": call["name"], "input": call["input"]})
        t.ctx.call_id = call["id"]  # a helper's question in place shows on this call's step
        given, confirmed = split_confirm(call["input"])
        refused = None if confirmed else (repeated_failure(this_turn(state["messages"] + results), call)
                                          or invented_numbers(call, state["messages"]))
        if confirmed and isinstance(given, dict) and invented_numbers(call, state["messages"]):
            given = {**given, "task": str(given.get("task", "")) + SAMPLE_DATA}  # it runs, labelled
        began = time.perf_counter()
        try:
            result = (ToolResult("invalid_arguments", {"error": {"message": refused}}) if refused
                      else execute(t.ctx, call["name"], given, t.tools))
        except Cancelled:  # the user pressed Stop: what was done is kept as this call's result, then the turn ends
            t.store.append(t.chat_id, {"role": "tool", "tool_call_id": call["id"], "name": call["name"],
                                       "content": json.dumps(present(t.ws, _stopped(t, call["name"])), ensure_ascii=False),
                                       "is_error": False})
            raise
        if t.live is not None:
            t.live.note("tools", {"name": call["name"], "status": result.status,
                                  "seconds": round(time.perf_counter() - began, 2), **({"refused": refused} if refused else {})})
        if result.status == "ok" and (result.artifact or {}).get("type") == "question":
            # ask_user's question, or a request for the user's permission (approval.py): it is shown (its
            # artifact went out through emit); the answer becomes this call's result when the user gives
            # it. Calls after it in the same step never run.
            pending = {"call_id": call["id"], "name": call["name"],
                       **{k: v for k, v in result.artifact.items() if k != "type"}}
        elif call["name"] in DELEGATES and result.result.get("question_for_user"):
            # A helper needs the user: ask them now, with the helper's own question and options.
            asked = {"question": result.result["question_for_user"], "options": result.result.get("options") or [],
                     "allow_text": True, "allow_skip": True}
            t.ctx.emit({"tool": call["name"], "type": "question", **asked})
            helper = {k: v for k, v in result.result.items() if k not in ("question_for_user", "options", "choices")}
            pending = {"call_id": call["id"], "via": call["name"], "helper": helper,
                       "choices": result.result.get("choices"), **asked}
        else:
            pending = None
        if pending:
            t.store.update_chat(t.chat_id, pending=pending)
            t.write({"type": "question", **pending})
            return {"messages": results, "rounds": state["rounds"] + 1, "paused": True}
        if result.status in ("ok", "partial") and (result.artifact or {}).get("type") in SHOWN:
            result.result["shown"] = SHOWN[result.artifact["type"]]
        content = result.for_model(lambda body: present(t.ws, body))
        message = {"role": "tool", "tool_call_id": call["id"], "name": call["name"], "content": content,
                   "is_error": result.status not in ("ok", "partial")}
        t.store.append(t.chat_id, message)
        results.append(message)
        t.write({"type": "tool_end", "id": call["id"], "name": call["name"], "status": result.status,
                 "summary": content if len(content) <= SUMMARY_CHARS else content[:SUMMARY_CHARS] + " ..."})
    return {"messages": results, "rounds": state["rounds"] + 1}


def _snapshot_on_newest(t: Turn, history: list[dict], visible: list[dict], dropped: int) -> list[dict]:
    """Old turns were cut: the newest user message gets a fresh snapshot of the workspace,
    with the user's standing instructions from the cut turns. Stored, so later turns send the same
    prompt (the cache works again from here)."""
    newest = next((i for i in range(len(visible) - 1, -1, -1) if visible[i]["role"] == "user"
                   and not visible[i].get("hidden") and visible[i].get("id")), None)
    if newest is None:
        return visible
    message = visible[newest]
    cut = history[:len(history) - len(visible)]
    first = next((m for m in history if m["role"] == "user" and not m.get("hidden")), None)
    began = STAMP.match(first.get("said") or first.get("content") or "") if first else None
    note, seen = message_context(t.ws, None, dropped=dropped, standing=standing_instructions(cut),
                                 started=began.group(0).strip() if began else None)
    content = f"{message.get('said') or message['content']}\n\n{note}"
    t.store.update_message(message["id"], content=content, seen=seen)
    return [*visible[:newest], {**message, "content": content, "seen": seen}, *visible[newest + 1:]]


def _after_agent(state: TurnState) -> str:
    last = state["messages"][-1]
    if last["role"] == "user":  # the cut-off nudge
        return "agent"
    return "tools" if last.get("tool_calls") else END


def _after_tools(state: TurnState) -> str:
    return END if state.get("paused") else "agent"


def _build():
    graph = StateGraph(TurnState)
    graph.add_node("agent", _agent)
    graph.add_node("tools", _tools)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", _after_agent, ["agent", "tools", END])
    graph.add_conditional_edges("tools", _after_tools, ["agent", END])
    return graph.compile()


GRAPH = _build()


# ---------------------------------------------------------------- running a turn

def stamp(now: datetime | None = None) -> str:
    now = now or datetime.now().astimezone()
    return f"[{now:%a %d %b %Y, %H:%M}]"


def _attached(ws: Workspace, item: str) -> dict:
    """An attached file as its message shows it, as it was when sent (a later rename leaves it)."""
    e = ws.entry(item)
    size = {"rows": e["rows"]} if "rows" in e else {"bytes": e.get("bytes")}
    return {"id": item, "kind": e["kind"], "name": e.get("name"), **size}


def send(store: Store, chat_id: str, text: str, main: Provider, worker: Provider,
         files: Sequence[str] = ()) -> Iterator[dict]:
    """The user's message (or, when a question is waiting, their typed answer to it): the turn's events.
    files: the ids of the files sent with it - uploaded with it, or pointed at with @ - kept on the message so the
    page shows them on it again, and named on it for the model ([Files with this message: ...]); this turn's snapshot
    holds what in them matches the message (snapshot.py)."""
    chat = store.chat(chat_id)
    if chat is None:
        yield {"type": "error", "message": "No such chat."}
        return
    if (chat["pending"] or {}).get("retry"):  # "Try again, answer quickly" takes no text: words typed are a new message
        store.update_chat(chat_id, pending=None)
        store.append(chat_id, {"role": "artifact", "artifact": {"type": "answer", "answer": None, "skipped": True}})
        chat = store.chat(chat_id)
    waiting = live(chat_id)
    if chat["pending"] or (waiting is not None and waiting.waiting()):  # a helper asking in place waits too
        yield from reply(store, chat_id, text, main, worker)
        return

    def record(put: Callable[[dict], None]) -> str:
        # What the workspace holds, as changed since the last message (context.py).
        ws, _ = store.open(chat["workspace_id"])
        before = next((m["seen"] for m in reversed(store.messages(chat_id, for_model=True))
                       if m["role"] == "user" and m.get("seen") is not None), None)
        note, seen = message_context(ws, before)
        said = f"{stamp()} {text}"
        attached = [_attached(ws, i) for i in dict.fromkeys(files) if i in ws.ids()]
        sent = f"[Files with this message: {', '.join(a['name'] for a in attached)}]" if attached else ""
        content = "\n\n".join(x for x in (said, sent, note) if x)  # which files the user meant: part of what they said
        store.append(chat_id, {"role": "user", "content": content, "said": said,
                               "text": text, "seen": seen, **({"files": attached} if attached else {})})
        return text

    yield from _run(store, chat_id, record, main, worker)


def reply(store: Store, chat_id: str, answer: str | None, main: Provider, worker: Provider) -> Iterator[dict]:
    """The user's answer to the waiting question - ask_user's, a helper's, or a request for permission
    (None = skipped); the turn goes on. A helper waiting in place gets it directly and the running turn
    goes on; this request streams the rest of it."""
    turn = live(chat_id)
    if turn is not None and turn.waiting():
        card = {"type": "answer", "answer": answer, **({"skipped": True} if answer is None else {})}
        store.append(chat_id, {"role": "artifact", "artifact": card})
        after = turn.answer(answer)
        turn.put({"type": "artifact", "artifact": card})
        yield from turn.follow(start=after)
        return
    chat = store.chat(chat_id)
    if chat is None or not chat["pending"]:
        yield {"type": "error", "message": "No question is waiting for an answer."}
        return
    if chat["pending"].get("retry") and answer is None:  # the user skipped "try again": the turn ends here
        store.update_chat(chat_id, pending=None)
        store.append(chat_id, {"role": "artifact", "artifact": {"type": "answer", "answer": None, "skipped": True}})
        yield {"type": "done", "answer": ""}
        return

    def record(put: Callable[[dict], None]) -> str:
        pending = chat["pending"]
        if pending.get("retry"):  # "try again, answer quickly": a hidden note, and a thinking budget for that call
            store.append(chat_id, {"role": "artifact", "artifact": {"type": "answer", "answer": TRY_AGAIN}})
            store.append(chat_id, {"role": "user", "content": QUICK, "hidden": True, "quick": True})
            store.update_chat(chat_id, pending=None)
            asked = [m for m in store.messages(chat_id, for_model=True) if m["role"] == "user" and not m.get("hidden")]
            return asked[-1].get("text", "") if asked else ""
        answered = answer_result(pending.get("question", ""), answer)
        if pending.get("approval"):  # the user allowed or refused an action: done (or not) here, by code
            ws, index = store.open(chat["workspace_id"])
            answered, card = carry_out(ws, index, pending["approval"], pending["options"], answer)
            if card:
                shown = {"tool": pending["name"], **card}
                store.append(chat_id, {"role": "artifact", "artifact": shown})
                put({"type": "artifact", "artifact": shown})
        elif pending.get("via"):  # a helper asked: its result, plus the user's answer to hand the task back with
            applied = _apply_choice(store, chat, pending, answer)
            note = (f"The user's choice is applied: use table {applied['use_table']}. Give the helper the task "
                    f"again on {applied['use_table']}." if applied
                    else "Give the helper the task again, with the user's answer in it.")
            answered = {**pending.get("helper", {}), "helper_asked": answered["question"],
                        **({"user_skipped": True} if answered.get("skipped") else {"user_answered": answered["answer"]}),
                        **applied, "note": note if not answered.get("skipped") else answered["note"]}
        content = json.dumps(present(store.open(chat["workspace_id"])[0], {"status": "ok", **answered}), ensure_ascii=False)
        store.append(chat_id, {"role": "tool", "tool_call_id": pending["call_id"],
                               "name": pending.get("via") or pending.get("name", "ask_user"), "content": content,
                               "is_error": answered.get("status") == "failed"})
        store.update_chat(chat_id, pending=None)
        asked = [m for m in store.messages(chat_id, for_model=True) if m["role"] == "user" and not m.get("hidden")]
        return asked[-1].get("text", "") if asked else ""

    yield from _run(store, chat_id, record, main, worker)


def _apply_choice(store: Store, chat: dict, pending: dict, answer: str | None) -> dict:
    """When the question's options carry a cleaning step (inspect_data's do), the user's choice is
    applied here, by code: the helper gets the cleaned table instead of having to act on an answer
    it might misread."""
    choices = pending.get("choices") or {}
    said = (answer or "").strip().lower()
    option = next((o for o in choices.get("options", []) if said and (
        said == o["label"].lower() or said.startswith(o["label"].split("(")[0].strip().lower()))), None)
    if option is None:
        return {}
    ws, _ = store.open(chat["workspace_id"])
    done = clean_data(ws, choices["table"], [option["step"]])
    if done.status != "ok":
        return {}
    return {"applied": f"{option['label']}: {choices['table']} was cleaned into {done.result['data']}",
            "use_table": done.result["data"]}


@dataclass
class Live:
    """The events of the running turn (or the last one), kept so a page that reloads or opens the
    chat again follows the turn from its start. ``after_id``: the chat's last stored row before the
    turn - rows after it are this turn's, and the page shows them through these events instead."""
    chat_id: str
    after_id: int
    events: list[dict] = field(default_factory=list)
    done: bool = False
    cond: threading.Condition = field(default_factory=threading.Condition)
    asking: int | None = None  # the event index of a helper's question waiting for its answer
    answer_value: str | None = None
    _answered: threading.Event | None = None
    began: float = field(default_factory=time.perf_counter)
    trace: dict = field(default_factory=lambda: {"steps": [], "tools": [], "helpers": []})

    def note(self, part: str, entry) -> None:
        """One fact for the turn's trace, with when it happened (seconds since the turn began)."""
        at = round(time.perf_counter() - self.began, 2)
        self.trace.setdefault(part, []).append({"at": at, **entry} if isinstance(entry, dict) else entry)

    def note_helper(self, event: dict) -> None:
        """A helper's progress, kept per helper task: its steps (stop, calls, tokens), results and checks."""
        kind, at = event.get("type", ""), round(time.perf_counter() - self.began, 2)
        if kind == "worker_start" or not self.trace["helpers"]:
            self.trace["helpers"].append({"worker": event.get("worker"), "at": at, "steps": [], "tools": [], "checks": []})
        helper = self.trace["helpers"][-1]
        if kind == "worker_turn":
            helper["steps"].append({"at": at, "stop": event.get("stop"), "calls": event.get("calls", []),
                                    **({"usage": event["usage"]} if event.get("usage") else {})})
        elif kind == "worker_result":
            helper["tools"].append({"at": at, "name": event.get("name"), "status": event.get("status"),
                                    "chars": event.get("chars")})
        elif kind == "worker_check":
            helper["checks"].append({"at": at, "problems": event.get("problems", []), "retry": event.get("retry")})

    def put(self, event: dict) -> None:
        with self.cond:
            if event.get("type") == "question" and event.get("inplace"):
                self.asking = len(self.events)
            self.events.append(event)
            self.cond.notify_all()

    def wait_for_answer(self) -> threading.Event:
        self._answered, self.answer_value = threading.Event(), None
        return self._answered

    def waiting(self) -> bool:
        return self._answered is not None and not self._answered.is_set() and not self.done

    def answer(self, said: str | None, timed_out: bool = False) -> int:
        """Hand the user's answer (None = skipped) to the waiting helper; returns where the turn's events
        go on, for the answering request to stream from."""
        with self.cond:
            after = (self.asking if self.asking is not None else len(self.events) - 1) + 1
            self.asking, self.answer_value = None, None if timed_out else said
            if self._answered is not None:
                self._answered.set()
            self.cond.notify_all()
            return after

    def end(self) -> None:
        with self.cond:
            self.done = True
            self.cond.notify_all()

    def follow(self, start: int = 0) -> Iterator[dict]:
        """The events from ``start`` (the turn's start), then the new ones as they come, until the turn
        ends - or until a helper's question that is waiting for its answer: the request following it ends
        there, so the page can answer it (the answer's request streams the rest)."""
        seen = start
        while True:
            with self.cond:
                while seen == len(self.events) and not self.done:
                    self.cond.wait()
                batch, ended, asking = self.events[seen:], self.done, self.asking
            for i, event in enumerate(batch, start=seen):
                yield event
                if asking is not None and i == asking:
                    return
            seen += len(batch)
            if ended and seen == len(self.events):
                return


def stop(chat_id: str) -> bool:
    """The Stop button: the running turn of this chat ends at its next streamed token or event.
    Nothing else stops a turn - a page that reloads or leaves the chat comes back to it."""
    with _state:
        if _live is None or _live.done or _live.chat_id != chat_id:
            return False
        STOP.set()
        return True


def end_turn(chat_ids: set[str]) -> None:
    """Before a chat or workspace is deleted: its running turn is stopped and waited for (bounded),
    so it does not write into rows that are going away."""
    turn = _live
    if turn is None or turn.done or turn.chat_id not in chat_ids:
        return
    with _state:
        STOP.set()
    with turn.cond:
        turn.cond.wait_for(lambda: turn.done, timeout=STOP_WAIT)


def live(chat_id: str) -> Live | None:
    """The chat's turn while it runs."""
    turn = _live
    return turn if turn is not None and turn.chat_id == chat_id and not turn.done else None


def follow(chat_id: str) -> Iterator[dict]:
    """The events of the chat's running turn, from its start; nothing when none runs."""
    turn = _live
    if turn is not None and turn.chat_id == chat_id:
        yield from turn.follow()


def _run(store: Store, chat_id: str, record: Callable[[Callable[[dict], None]], str], main: Provider,
         worker: Provider) -> Iterator[dict]:
    """One turn, run in its own thread; the request that started it follows its events like any
    other page would. The model slots serve one turn at a time, and the turn holds them until its
    thread has really ended (LangGraph runs a node in a worker thread that closing a stream does not
    stop - measured). record(put) stores the user's message or answer; events it puts (a card for
    an action the user just allowed) open the turn's stream."""
    turn = _claim(store, chat_id)
    if turn is None:
        other = _live.chat_id if _live is not None and not _live.done else None
        where = (store.chat(other) or {}).get("title") if other and other != chat_id else None
        yield {"type": "error", "message": f"An answer is still being written in “{where}”; wait for it, or stop "
                                           "it there." if where else "An answer is still being written; wait for it "
                                           "to finish."}
        return
    try:
        user_text = record(turn.put)  # only now: a refused turn must leave no unanswered message behind
    except BaseException:
        _release(turn)
        raise
    threading.Thread(target=_work, args=(turn, store, chat_id, user_text, main, worker),
                     name="assistant-turn", daemon=True).start()
    yield from turn.follow()


def _work(turn: Live, store: Store, chat_id: str, user_text: str, main: Provider, worker: Provider) -> None:
    ended = "failed"
    try:
        chat = store.chat(chat_id)
        ws, index = store.open(chat["workspace_id"])
        state = {"messages": store.messages(chat_id, for_model=True), "rounds": 0, "paused": False}
        config = {"configurable": {"turn": Turn(store, chat_id, ws, index, main, worker, user_text, chat["dropped"],
                                                live=turn)},
                  "recursion_limit": 4 * MAX_ROUNDS + 8}
        for event in GRAPH.stream(state, config, stream_mode="custom"):
            turn.put(event)
        if not store.chat(chat_id)["pending"]:  # else: waiting for the user's answer; the turn goes on after it
            turn.put({"type": "done", "answer": _finish(store, chat, index, user_text)})
            ended = "done"
        else:
            ended = "waiting for the user"
    except Cancelled:
        turn.put({"type": "stopped"})
        ended = "stopped"
    except ProviderError as exc:
        turn.put({"type": "error", "message": str(exc)})
        turn.trace["error"] = str(exc)
    except Exception as exc:  # a bug, not the user's fault; details go to the log
        log.exception("Assistant turn failed")
        turn.put({"type": "error", "message": "Something went wrong while answering. Please try again."})
        turn.trace["error"] = f"{type(exc).__name__}: {exc}"[:300]
    finally:
        _keep_trace(store, chat_id, turn, ended)
        _release(turn)


def _keep_trace(store: Store, chat_id: str, turn: Live, ended: str) -> None:
    """The turn's trace as its chat's last row; a chat deleted meanwhile has nowhere to keep it."""
    trace = {"ended": ended, "seconds": round(time.perf_counter() - turn.began, 2), **turn.trace}
    try:
        if store.chat(chat_id) is not None:
            store.append(chat_id, {"role": "trace", "trace": trace})
    except Exception:  # noqa: BLE001 - a trace never costs the user their turn
        log.exception("Could not keep the turn's trace")


def _claim(store: Store, chat_id: str) -> Live | None:
    """The slots for a new turn. A turn being stopped is waited for: it ends at its next token, or
    after the tool it is running."""
    with _state:
        stopping = STOP.is_set()
    if not (_busy.acquire(timeout=STOP_WAIT) if stopping else _busy.acquire(blocking=False)):
        return None
    global _live
    rows = store.messages(chat_id)
    turn = Live(chat_id, rows[-1]["id"] if rows else 0)
    with _state:
        _live = turn
        STOP.clear()
    return turn


def _release(turn: Live) -> None:
    _busy.release()  # first: a page that sends its next message the moment it sees the end gets the slots
    turn.end()  # kept as _live until the next turn: a page opening the chat now still gets its end


def _finish(store: Store, chat: dict, index: SearchIndex, user_text: str) -> str:
    """Remember the turn for recall, index the pictures and tables it made, and name a new chat after its first
    message."""
    messages = store.messages(chat["id"], for_model=True)
    starts = [i for i, m in enumerate(messages) if m["role"] == "user" and not m.get("hidden")]
    current = messages[starts[-1]:] if starts else messages
    answer = next((m["text"] for m in reversed(current) if m["role"] == "assistant" and m.get("text")), "")
    calls = [{"name": c["name"], "arguments": c["input"]} for m in current if m["role"] == "assistant"
             for c in m.get("tool_calls", [])]
    remember_turn(index, chat["id"], user_text, answer, calls, when=current[0].get("at") if current else None)
    store.sync_items(chat["workspace_id"])  # pictures and tables the turn made, changed or deleted: searchable now
    if chat["title"] == NEW_CHAT and user_text.strip():
        words = user_text.split()
        store.update_chat(chat["id"], title=" ".join(words[:8])[:60] + (" ..." if len(words) > 8 else ""))
    store.touch_workspace(chat["workspace_id"])
    return answer


def _worker_event(event: dict) -> dict:
    """A worker's progress for the UI: what it called and how it went, without whole pages."""
    out = {k: v for k, v in event.items() if k not in ("type", "content")}
    if "content" in event:
        out["summary"] = event["content"][:SUMMARY_CHARS]
    return {"type": "worker", "event": event["type"].removeprefix("worker_"), **out}
