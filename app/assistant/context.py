# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""What every model call gets without having to ask, and how long histories fit a slot.

What the workspace holds (each file's path and size - never content) is built by code, so a small model
never has to remember to pass it on. It is not in main's system prompt: llama.cpp reuses a prompt only up
to its first changed token, and a minute that changed early in the prompt made it reprocess 5,673 of
5,878 tokens (measured; the same prompt again: 1). So it goes on the user's messages instead, append-only
(message_context): the chat's first message carries a snapshot, later ones only what changed since
the message before (most carry nothing), and a message sent when old turns had to be dropped
carries a fresh snapshot with the user's standing instructions from the dropped turns. Older
messages never change, so the cached prompt always matches up to the newest message. A worker's
task is one short conversation: its opening message carries context_block.

agent.md (agent_md.py) is the one thing in main's system prompt besides the fixed prompt: it is in view every
turn as it is now, never stored in the history and never cut with old turns; it changes rarely, and only then is
the prompt processed again.

Fitting a slot is measured, not guessed: ``count`` gives the exact size of the whole prompt -
system, tool declarations and messages as the model's chat template renders them - from the
model server (LlamaCppProvider.count_tokens). The prompt must leave room for the reply:
limit = slot size - the provider's max_tokens. That is the least a reply gets: the counted size goes with the
call, and llama-server's reply may take all the room the slot has left (LlamaCppProvider.reply_tokens).

Rolling window (main slot): the oldest complete turns are dropped (a turn = a user message and
everything up to the next one; the current turn is never cut). When trimming is needed it trims
to 25% below the limit, and the cut stays where it is (``dropped``) until the prompt outgrows the
limit again - so the prompt's beginning stays the same for several turns and llama.cpp's prompt
cache keeps working. Dropped turns are still findable: every turn is indexed for recall.

Inside one worker task the history is short, but tool results can be big (a web page is ~3,000
tokens): when it runs over, older tool results are shortened, newest kept whole - again to 25%
below the limit, and the number shortened stays put until it overflows again. main does the same
when its current turn alone outgrows the slot (several helpers' results in one turn): cut_turns
says so, since the current turn itself is never dropped.

How much to drop is found by binary search over exact counts (a handful of calls, ~10 ms each).
Approach (exact counts accept a fit, a sticky boundary, a bite well under the limit) from Unsloth
Studio's rolling context (studio/backend/core/inference/context_window.py), written independently.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime

from app.providers.base import estimate_tokens
from app.tools import agent_md
from app.tools.workspace import ITEM_REF, Workspace

SLOT_TOKENS = 16_384  # context of each llama-server slot (main = 0, worker = 1)
OUTPUT_RESERVE = 4_096  # reply room when a provider does not say (its max_tokens)
HEADROOM = 0.25
SHORT_RESULT = 1_200  # characters an older tool result keeps when a worker task runs long

Count = Callable[[list[dict]], int]  # messages -> exact tokens of the whole prompt they make


def now_line(now: datetime | None = None) -> str:
    now = now or datetime.now().astimezone()
    return f"Now: {now:%A, %d %B %Y, %H:%M} (the user's local time)."


def context_block(ws: Workspace) -> str:
    """A worker task's view: the workspace and agent.md (the user's notes, to follow - main keeps them)."""
    return "\n\n".join(p for p in (ws.listing(), agent_md.in_view(ws, keeper=False)) if p)


SNAPSHOT_ITEMS = 5  # newest items in a snapshot; files(mode='list') shows them all
STANDING = re.compile(r"\b(always|never(?! mind)|from now on|every time|whenever|each time|don'?t ever|do not ever|"
                      r"in (?:all|every) (?:future )?(?:answers?|repl(?:y|ies)|responses?))\b", re.I)
MAX_STANDING = 3


def seen_state(ws: Workspace) -> dict:
    """What a message told main about the workspace - kept on the message, never sent to the model."""
    return {"items": ws.lines()}


def message_context(ws: Workspace, before: dict | None, *, dropped: int = 0,
                    standing: list[str] | None = None, started: str | None = None) -> tuple[str, dict]:
    """(the note for the user's newest message, '' when nothing changed; the state it reflects).
    before: the state the previous message reflected, None for a snapshot (first message, or a cut)."""
    now = seen_state(ws)
    if before is not None and any(ITEM_REF.match(line) for line in before.get("items", {}).values()):
        before = None  # told in the first layout's words ('data_1  sales.csv - ...'): a fresh snapshot instead
    if before is None:
        lines = list(now["items"].values())
        shown = "\n".join(lines[-SNAPSHOT_ITEMS:]) or "empty (no files attached yet)"
        more = f"\n({len(lines) - SNAPSHOT_ITEMS} more: files(mode='list'))" if len(lines) > SNAPSHOT_ITEMS else ""
        parts = [f"[Workspace now:\n{shown}{more}]"]
        if dropped:  # after a cut the chat's first stamp is out of view: when it began is said here
            began = f" This chat started {started}." if started else ""
            parts.append(f"[{dropped} earlier turn(s) of this chat are not shown; find them with ask_knowledge.{began}]")
        if standing:
            parts.append("[The user's standing instructions from those turns: "
                         + "; ".join(f'"{s}"' for s in standing) + "]")
        return "\n".join(parts), now
    old = before.get("items", {})
    changes = [f"+ {line}" for i, line in now["items"].items() if i not in old]
    changes += [f"~ {line}" for i, line in now["items"].items() if i in old and old[i] != line]
    changes += [f"- {old[i].split(': ', 1)[0]} (removed)" for i in old if i not in now["items"]]
    parts = ["[Workspace changes since the last message:\n" + "\n".join(changes) + "]"] if changes else []
    return "\n".join(parts), now


# The app's notes, as message_context writes them. A small model copies them into its reply (measured:
# "[Workspace now: data_1 sales.csv ..." at the end of an answer), as it copied the time stamp.
NOTE = re.compile(r"\[(?:Workspace now:|Workspace changes since|agent\.md - your standing memory|Files with this message|"
                  r"Pinned and pointed-at files|"
                  r"The user's standing instructions|"
                  r"\d+ earlier turn\(s\) of this chat).*?\](?=[ \t]*(?:\n|$))\s*", re.S)


def without_notes(text: str) -> str:
    """A reply without copies of the app's bracketed notes."""
    return NOTE.sub("", text).rstrip() if "[" in text else text


def standing_instructions(messages: list[dict]) -> list[str]:
    """The user's standing instructions ("always ...", "from now on ...") among these messages, newest
    kept when there are more than MAX_STANDING (Unsloth's instruction-pin idea, written independently)."""
    said = [" ".join(str(m.get("text") or "").split()) for m in messages if m["role"] == "user" and not m.get("hidden")]
    found = [s if len(s) <= 200 else s[:200] + " ..." for s in said if s and STANDING.search(s)]
    return found[-MAX_STANDING:]


def prompt_limit(provider) -> int:
    """Tokens the prompt may use: the slot (a provider's own, e.g. a cloud plan's limit) less the room the
    reply needs."""
    return getattr(provider, "slot_tokens", SLOT_TOKENS) - (getattr(provider, "max_tokens", None) or OUTPUT_RESERVE)


def counter(provider, system: str, tools: list[dict]) -> Count:
    """Exact prompt size from the provider when it can count (llama-server), else the estimate."""
    count = getattr(provider, "count_tokens", None)
    if count is None:
        return lambda messages: estimate_tokens(system, messages, tools)
    return lambda messages: count(system, messages, tools)


def turn_starts(messages: list[dict]) -> list[int]:
    """Where each turn starts: the user's own messages. A hidden nudge (the app's "your reply was cut
    off") belongs to the turn it is in - counted as a start, a window once began on a nudge and the
    model saw its own half answer without the question."""
    return [i for i, m in enumerate(messages) if m["role"] == "user" and not m.get("hidden")]


def fit_turns(messages: list[dict], limit: int, count: Count, dropped: int = 0) -> tuple[list[dict], int]:
    """(the messages to send, how many turns are dropped). ``dropped`` is the cut used last time."""
    visible, dropped, _ = cut_turns(messages, limit, count, dropped)
    return visible, dropped


PAST_RESULT = 1_500  # characters an earlier turn's tool result keeps in main's view
PAST_ARG = 400  # characters an earlier turn's long call text keeps (a file's whole text: the file keeps it)


def lean_past(messages: list[dict]) -> list[dict]:
    """main's earlier turns without their bulk: tool results and long call texts (a written file's whole text)
    shortened; the current turn - from the user's latest message - stays whole. Its answer says what mattered, a file
    can be read again. Each turn is shortened once, when the next one starts, so the prompt before it stays cached.
    Measured 2026-10-17: a 15-turn chat carried two whole games, a 747-line program and its reads - 57k of the 64k
    tokens - and every cut of the window then re-read 41k tokens (58 s for one step)."""
    start = max((i for i, m in enumerate(messages) if m["role"] == "user" and not m.get("hidden")), default=0)
    out = []
    for i, m in enumerate(messages):
        if i < start and m["role"] == "tool" and len(str(m.get("content") or "")) > PAST_RESULT:
            m = {**m, "content": str(m["content"])[:PAST_RESULT] + " ... [an earlier turn's result, shortened: its "
                                                                    "answer says what mattered]"}
        elif i < start and m["role"] == "assistant" and m.get("tool_calls"):
            calls = [{**c, "input": {k: (v[:PAST_ARG] + " ... [shortened: the file keeps it]"
                                         if isinstance(v, str) and len(v) > PAST_ARG * 2 else v)
                                     for k, v in c["input"].items()}} if isinstance(c.get("input"), dict) else c
                     for c in m["tool_calls"]]
            m = {**m, "tool_calls": calls}
        out.append(m)
    return out


def cut_turns(messages: list[dict], limit: int, count: Count, dropped: int = 0) -> tuple[list[dict], int, bool]:
    """fit_turns, and whether what is left fits: the current turn is never cut, so when it alone is too
    big (several helper results in one turn) it doesn't - main then shortens its older tool results
    (shorten_tool_results), as a helper does."""
    starts = turn_starts(messages)
    if not starts:
        return messages, 0, True
    last = len(starts) - 1
    dropped = min(dropped, last)
    if count(messages[starts[dropped]:]) <= limit:
        return messages[starts[dropped]:], dropped, True
    if dropped == last:  # only the current turn is left, and it is too big
        return messages[starts[last]:], last, False
    n = _fewest(dropped + 1, last, lambda k: count(messages[starts[k]:]) <= int(limit * (1 - HEADROOM)))
    window = messages[starts[n]:]
    return window, n, n < last or count(window) <= limit


def shorten_tool_results(messages: list[dict], limit: int, count: Count, shortened: int = 0,
                         keep_last: int = 2) -> tuple[list[dict], int]:
    """Within one task: (the messages to send, how many of the oldest tool results are shortened).
    ``shortened`` is the count used last time; the newest ``keep_last`` results stay whole."""
    positions = [i for i, m in enumerate(messages) if m["role"] == "tool"][:-keep_last or None]

    def cut(n: int) -> list[dict]:
        out = list(messages)
        for i in positions[:n]:
            if len(out[i]["content"]) > SHORT_RESULT:
                out[i] = {**out[i], "content": out[i]["content"][:SHORT_RESULT] + " ... [shortened to save space]"}
        return out

    shortened = min(shortened, len(positions))
    if count(cut(shortened)) <= limit or shortened == len(positions):
        return cut(shortened), shortened
    n = _fewest(shortened + 1, len(positions), lambda k: count(cut(k)) <= int(limit * (1 - HEADROOM)))
    return cut(n), n


SHORT_ARGS = 300  # characters an older call's long text keeps (a file's whole text: the file keeps it)


def shorten_call_args(messages: list[dict], limit: int, count: Count, cut: int = 0,
                      keep_last: int = 2) -> tuple[list[dict], int]:
    """Within one task, when shortened results are not enough: the long texts of the helper's own older calls (a
    file's whole text it wrote) cut to their start - the file holds them, so nothing is lost. (the messages to send,
    how many calls are cut). Measured before files: a scratch script sent whole five times filled a 16K slot."""
    positions = [i for i, m in enumerate(messages)
                 if m["role"] == "assistant" and m.get("tool_calls")][:-keep_last or None]

    def short(value):
        if isinstance(value, str) and len(value) > SHORT_ARGS:
            return value[:SHORT_ARGS] + " ... [shortened here: the rest is in the file]"
        return value

    def cut_to(n: int) -> list[dict]:
        out = list(messages)
        for i in positions[:n]:
            calls = [{**c, "input": {k: short(v) for k, v in c["input"].items()} if isinstance(c.get("input"), dict)
                      else c.get("input")} for c in out[i]["tool_calls"]]
            out[i] = {**out[i], "tool_calls": calls, "raw": None}  # rebuilt from the calls (llamacpp)
        return out

    cut = min(cut, len(positions))
    if count(cut_to(cut)) <= limit or cut == len(positions):
        return cut_to(cut), cut
    n = _fewest(cut + 1, len(positions), lambda k: count(cut_to(k)) <= int(limit * (1 - HEADROOM)))
    return cut_to(n), n


IMAGES_SHOWN = 2  # pictures a helper keeps in view: the newest; an older one is a line saying what it was


def fewer_images(messages: list[dict], keep: int = IMAGES_SHOWN) -> list[dict]:
    """The messages with only the newest ``keep`` images still attached (each costs up to 560 tokens of
    a 16K slot); an older picture's message keeps its text, which names it."""
    seen, out = 0, list(messages)
    for i in range(len(out) - 1, -1, -1):
        images = out[i].get("images")
        if not images:
            continue
        if seen + len(images) <= keep:
            seen += len(images)
            continue
        left = max(0, keep - seen)
        seen = keep
        out[i] = {**out[i], "images": images[len(images) - left:],
                  "content": out[i]["content"] + f" [{len(images) - left} of its pictures no longer shown]"}
    return out


def _fewest(low: int, high: int, fits: Callable[[int], bool]) -> int:
    """The smallest n in [low, high] for which fits(n) holds (fits only gets truer as n grows);
    high when none does - the most that can go."""
    if low > high or not fits(high):
        return max(low, high)
    while low < high:
        mid = (low + high) // 2
        if fits(mid):
            high = mid
        else:
            low = mid + 1
    return low
