"""What every model call gets without having to ask, and how long histories fit a slot.

What the workspace holds (ids, names, sizes - never content) and the pinned notes are built by
code, so a small model never has to remember to pass them on. None of it is in main's system
prompt: llama.cpp reuses a prompt only up to its first changed token, and a minute that changed
early in the prompt made it reprocess 5,673 of 5,878 tokens (measured; the same prompt again: 1).
So main's system prompt never changes, and this goes on the user's messages instead, append-only
(message_context): the chat's first message carries a snapshot, later ones only what changed since
the message before (most carry nothing), and a message sent when old turns had to be dropped
carries a fresh snapshot with the user's standing instructions from the dropped turns. Older
messages never change, so the cached prompt always matches up to the newest message. A worker's
task is one short conversation: its opening message carries context_block.

Fitting a slot is measured, not guessed: ``count`` gives the exact size of the whole prompt -
system, tool declarations and messages as the model's chat template renders them - from the
model server (LlamaCppProvider.count_tokens). The prompt must leave room for the reply:
limit = slot size - the provider's max_tokens.

Rolling window (main slot): the oldest complete turns are dropped (a turn = a user message and
everything up to the next one; the current turn is never cut). When trimming is needed it trims
to 25% below the limit, and the cut stays where it is (``dropped``) until the prompt outgrows the
limit again - so the prompt's beginning stays the same for several turns and llama.cpp's prompt
cache keeps working. Dropped turns are still findable: every turn is indexed for recall.

Inside one worker task the history is short, but tool results can be big (a web page is ~3,000
tokens): when it runs over, older tool results are shortened, newest kept whole - again to 25%
below the limit, and the number shortened stays put until it overflows again.

How much to drop is found by binary search over exact counts (a handful of calls, ~10 ms each).
Approach (exact counts accept a fit, a sticky boundary, a bite well under the limit) from Unsloth
Studio's rolling context (studio/backend/core/inference/context_window.py), written independently.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime

from app.providers.base import estimate_tokens
from app.tools.notes import pinned, pinned_text
from app.tools.workspace import Workspace

SLOT_TOKENS = 16_384  # context of each llama-server slot (main = 0, worker = 1)
OUTPUT_RESERVE = 4_096  # reply room when a provider does not say (its max_tokens)
HEADROOM = 0.25
SHORT_RESULT = 1_200  # characters an older tool result keeps when a worker task runs long

Count = Callable[[list[dict]], int]  # messages -> exact tokens of the whole prompt they make


def now_line(now: datetime | None = None) -> str:
    now = now or datetime.now().astimezone()
    return f"Now: {now:%A, %d %B %Y, %H:%M} (the user's local time)."


def context_block(ws: Workspace) -> str:
    """A worker task's view: the workspace and the pinned notes."""
    return "\n\n".join(p for p in (ws.listing(), pinned_text(ws)) if p)


SNAPSHOT_ITEMS = 5  # newest items in a snapshot; files(mode='list') shows them all
STANDING = re.compile(r"\b(always|never(?! mind)|from now on|every time|whenever|each time|don'?t ever|do not ever|"
                      r"in (?:all|every) (?:future )?(?:answers?|repl(?:y|ies)|responses?))\b", re.I)
MAX_STANDING = 3


def seen_state(ws: Workspace) -> dict:
    """What a message told main about the workspace - kept on the message, never sent to the model."""
    return {"items": ws.lines(), "pinned": [[n["id"], n["text"]] for n in pinned(ws)]}


def message_context(ws: Workspace, before: dict | None, *, dropped: int = 0,
                    standing: list[str] | None = None, started: str | None = None) -> tuple[str, dict]:
    """(the note for the user's newest message, '' when nothing changed; the state it reflects).
    before: the state the previous message reflected, None for a snapshot (first message, or a cut)."""
    now = seen_state(ws)
    if before is None:
        lines = list(now["items"].values())
        shown = "\n".join(lines[-SNAPSHOT_ITEMS:]) or "empty (no files attached yet)"
        more = f"\n({len(lines) - SNAPSHOT_ITEMS} more: files(mode='list'))" if len(lines) > SNAPSHOT_ITEMS else ""
        parts = [f"[Workspace now:\n{shown}{more}]"]
        if now["pinned"]:
            parts.append(f"[{pinned_text(ws)}]")
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
    changes += [f"- {i} (removed)" for i in old if i not in now["items"]]
    parts = ["[Workspace changes since the last message:\n" + "\n".join(changes) + "]"] if changes else []
    if now["pinned"] != before.get("pinned"):
        parts.append(f"[{pinned_text(ws)}]" if now["pinned"] else "[Pinned notes: none now.]")
    return "\n".join(parts), now


# The app's notes, as message_context writes them. A small model copies them into its reply (measured:
# "[Workspace now: data_1 sales.csv ..." at the end of an answer), as it copied the time stamp.
NOTE = re.compile(r"\[(?:Workspace now:|Workspace changes since|Pinned notes|The user's standing instructions|"
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
    """Tokens the prompt may use: the slot less the room the reply needs."""
    return SLOT_TOKENS - (getattr(provider, "max_tokens", None) or OUTPUT_RESERVE)


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
    starts = turn_starts(messages)
    if not starts:
        return messages, 0
    dropped = min(dropped, len(starts) - 1)
    if count(messages[starts[dropped]:]) <= limit:
        return messages[starts[dropped]:], dropped
    fits = _fewest(dropped + 1, len(starts) - 1,
                   lambda n: count(messages[starts[n]:]) <= int(limit * (1 - HEADROOM)))
    return messages[starts[fits]:], fits


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
