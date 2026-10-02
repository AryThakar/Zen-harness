"""What every model call gets without having to ask, and how long histories fit a slot.

context_block: today's date and time, the workspace listing (ids, names, sizes - never content)
and the pinned notes. Built by code every turn, so a small model never has to remember to pass
them on.

Rolling window (main slot): the oldest complete turns are dropped (a turn = a user message and
everything up to the next one; the current turn is never cut). When trimming is needed it trims
to 25% below the budget, and the cut stays where it is (``dropped``) until the history grows past
the budget again - so the prompt prefix stays the same for several turns and llama.cpp's prompt
cache keeps working. Dropped turns are still findable: every turn is indexed for recall.

Inside one worker task the history is short, but tool results can be big (a web page is ~3,000
tokens): when it runs over budget, older tool results are shortened, newest kept whole.
Ideas from Unsloth Studio's rolling context (studio/backend/core/inference/context_window.py),
written independently. Token counts are estimates (characters / 3.5), on the safe side.
"""

from __future__ import annotations

import json
from datetime import datetime

from app.tools.notes import pinned_text
from app.tools.workspace import Workspace

SLOT_TOKENS = 16_384  # context of each llama-server slot (main = 0, worker = 1)
OUTPUT_RESERVE = 4_096  # room kept for one reply: thinking plus answer (the provider's max_tokens)
CHARS_PER_TOKEN = 3.5
HEADROOM = 0.25
SHORT_RESULT = 1_200  # characters an older tool result keeps when a worker task runs long


def context_block(ws: Workspace, now: datetime | None = None, dropped_turns: int = 0) -> str:
    now = now or datetime.now().astimezone()
    parts = [f"Now: {now:%A, %d %B %Y, %H:%M} (the user's local time).", ws.listing()]
    pinned = pinned_text(ws)
    if pinned:
        parts.append(pinned)
    if dropped_turns:
        parts.append(f"{dropped_turns} earlier turn(s) of this chat are not shown here; they can be found "
                     "with ask_knowledge.")
    return "\n\n".join(parts)


def history_budget(system: str, tool_specs: list[dict]) -> int:
    """Tokens left for the conversation once the instructions, tools and reply room are counted."""
    return SLOT_TOKENS - OUTPUT_RESERVE - tokens(system) - tokens(tool_specs)


def tokens(item) -> int:
    return int(len(json.dumps(item, ensure_ascii=False, default=str)) / CHARS_PER_TOKEN) + 1


def turn_starts(messages: list[dict]) -> list[int]:
    return [i for i, m in enumerate(messages) if m["role"] == "user"]


def fit_turns(messages: list[dict], budget: int, dropped: int = 0) -> tuple[list[dict], int]:
    """(the messages to send, how many turns are dropped). ``dropped`` is the cut used last time."""
    starts = turn_starts(messages)
    if not starts:
        return messages, 0
    dropped = min(dropped, len(starts) - 1)
    visible = messages[starts[dropped]:]
    if tokens(visible) <= budget:
        return visible, dropped
    target = int(budget * (1 - HEADROOM))
    while dropped < len(starts) - 1 and tokens(messages[starts[dropped]:]) > target:
        dropped += 1
    return messages[starts[dropped]:], dropped


def shorten_tool_results(messages: list[dict], budget: int, keep_last: int = 2) -> list[dict]:
    """Within one task: shorten older tool results (newest ``keep_last`` stay whole) until it fits."""
    if tokens(messages) <= budget:
        return messages
    out = [dict(m) for m in messages]
    tool_positions = [i for i, m in enumerate(out) if m["role"] == "tool"][:-keep_last or None]
    for i in tool_positions:
        content = out[i]["content"]
        if len(content) > SHORT_RESULT:
            out[i]["content"] = content[:SHORT_RESULT] + " ... [shortened to save space]"
            if tokens(out) <= budget:
                break
    return out
