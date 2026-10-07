"""main's honesty checks: what the system guarantees about main's own replies and calls, by code.

The helpers' results are checked in workers.py (sources, numbers, reports); these are the checks on
main itself, each from a measured failure in the user's own test chat (docs/SYSTEM_MAP.md, section 10):

    repeated failure    the same call that already failed in this turn is not run again: the model
                        is told what failed and to try something else (it repeated a failed download)
    invented data       a helper task (write_code, analyze_data) carrying several numbers that no tool
                        returned and the user never wrote is refused before the helper runs: invented
                        sales figures went to the coder, were charted, then explained
    announced, not done a reply that ends by announcing an action ("One moment.", "I am writing a script
                        now.", "**Action:** ...") with no tool call gets one hidden nudge: act now, or say
                        plainly what can't be done (9 times in one chat)
    asked to remember   the user asked to remember or note something and the reply saved nothing (no
                        save_note or files call): one hidden nudge to call save_note ("I will remember
                        that" with no note, after main's prompt already said to save it)
    claimed, not done   a reply claiming an action ("I saved a note", "has been downloaded") in a turn
                        where no tool did anything gets one hidden nudge to do it or correct the reply
                        ("remember X" answered "I saved a note" without save_note, 4 of 6)

Each nudge is sent at most once per turn; after it the reply stands. Claims about what a tool that did
run really did (an image "with the date added") are checked where they happen: the coder's report
against its run, vision after generate_image.
"""

from __future__ import annotations

import re

from .prompts import ANNOUNCE_NUDGE, CLAIM_NUDGE, REMEMBER_NUDGE
from .workers import DELEGATES, _call_key, numbers_in, ungrounded_numbers

INVENTED_MIN = 3  # numbers no one gave, in one helper task, before it is refused

# A sentence promising an action (the reply's last three sentences are read): measured endings were
# "One moment.", "Please wait while I execute the update", "I am writing a script now.", "Starting the
# ML model creation now.", "Then, I will assign it an ID, perform the analysis ...", "**Action:** ...".
ANNOUNCED = re.compile(
    r"\b(?:one moment|please wait|stand by|hold on|bear with me)\b"
    r"|\bI(?:'m| am) (?:now |currently )?\w+ing\b.*\bnow\b"
    r"|\b(?:starting|beginning|proceeding|moving on)\b.*\bnow\b"
    r"|\bI(?:'ll| will) (?:now |next |then |first |also |continue to )?(?:retrieve|download|fetch|run|execute|write|"
    r"create|save|update|analy[sz]e|perform|generate|read|start|continue|proceed|call|use|get|gather|check|assign|"
    r"present|compute|calculate|search|look|open|draw|plot|build|make|convert|extract|send|prepare|apply|test|train|"
    r"fix|edit|add|load|import|clean|query|chart|forecast|delete|rename|research|summari[sz]e|go ahead)\b"
    r"|^\W*action\W*:",
    re.I)
# Not a promise to act now: an offer ("Let me know if ... and I'll update it"), or waiting for the user
# ("Once you provide these numbers, I will run the model"). Commitments ("I will always use ...", "I will
# remember ...") don't use the action verbs above. Measured on the 188 replies stored so far: the 9
# real announcements caught, no other reply.
OFFER = re.compile(r"\b(?:if|let me know|would you|do you want|want me|shall I|should I|feel free|once|when you|"
                   r"as soon as|after you|until)\b", re.I)
SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")
# A claim, in the first person or passive, that something was done. "run", "written" and "drawn" are past
# only after have/been: "I run on a local clock" (measured, answering "what year is it?") is not a claim.
DONE = ("saved|stored|noted|remembered|created|wrote|downloaded|deleted|removed|renamed|updated|changed|"
        "added|generated|drew|plotted|ran|executed|printed")
PARTICIPLES = "written|drawn|run"
CLAIMED = re.compile(rf"\bI (?:now |just |also |successfully |already )?({DONE})\b"
                     rf"|\bI(?:'ve| have) (?:now |just |also |successfully |already )?({DONE}|{PARTICIPLES})\b"
                     rf"|\b(?:has|have) (?:now )?been (?:successfully )?({DONE}|{PARTICIPLES})\b", re.I)


# The user asking to keep something ("remember that ...", "note this down", "keep in mind ..."); a question
# about the past ("do you remember ...?") is not one.
REMEMBER = re.compile(r"\b(?:remember|memori[sz]e|take (?:a )?note|make a note|note (?:that|this|it|down)|"
                      r"keep (?:that |this |it )?in (?:mind|view)|don'?t forget|from now on|in (?:every|all|future) "
                      r"chats?)\b", re.I)
RECALLING = re.compile(r"\b(?:do|did|can|could|would) you (?:still )?remember\b", re.I)


def asked_to_remember(said: str) -> bool:
    said = said.strip()
    return bool(REMEMBER.search(said)) and not said.endswith("?") and not RECALLING.search(said)


def this_turn(messages: list[dict]) -> list[dict]:
    """The current turn: from the user's last own message (hidden nudges belong to the turn)."""
    start = max((i for i, m in enumerate(messages) if m["role"] == "user" and not m.get("hidden")), default=0)
    return messages[start:]


def repeated_failure(turn: list[dict], call: dict) -> str | None:
    """The error message when this exact call already failed in this turn, else None."""
    key = _call_key(call["name"], call["input"]) if isinstance(call["input"], dict) else None
    if key is None:
        return None
    failed = {m["tool_call_id"] for m in turn if m["role"] == "tool" and m.get("is_error")}
    earlier = [c for m in turn if m["role"] == "assistant" for c in m.get("tool_calls") or []]
    if any(c["id"] in failed and isinstance(c["input"], dict) and _call_key(c["name"], c["input"]) == key
           for c in earlier):
        return ("This exact call already failed in this turn (see its result above). Don't repeat it: try another "
                "way, or tell the user what didn't work.")
    return None


def invented_numbers(call: dict, messages: list[dict]) -> str | None:
    """For a helper task: the refusal when it carries numbers that came from nowhere - no tool result and
    no user message in the chat holds them (years and small counts don't count)."""
    if call["name"] not in DELEGATES or not isinstance(call["input"], dict):
        return None
    task = str(call["input"].get("task") or call["input"].get("question") or "")
    evidence = [str(m.get("content") or "") for m in messages if m["role"] == "tool"]
    evidence += [str(m.get("text") or m.get("content") or "") for m in messages if m["role"] == "user"]
    missing = [n for n in ungrounded_numbers(task, evidence) if not _year(n)]
    if len(missing) < INVENTED_MIN:
        return None
    return (f"Not run: the task gives numbers that no tool returned and the user never gave ({', '.join(missing[:6])}). "
            "Never make up data: find it first (research_web, or the user's files), or ask the user for it.")


def _year(raw: str) -> bool:
    values = [v for _, v, _, _ in numbers_in(raw)]
    return bool(values) and values[0] == int(values[0]) and 1900 <= values[0] <= 2100


def announced(text: str) -> bool:
    """Whether a reply ends by promising an action: a promise among its last three sentences that isn't
    an offer, or a last line that opens something it never gives ('Here is the next section:'). A
    reply ending with a question asks the user - fine."""
    ending = text.strip()
    if not ending or ending.endswith("?"):
        return False
    if ending.endswith(":"):
        return True
    last = [s for s in SENTENCES.split(ending) if s.strip()][-3:]
    return any(ANNOUNCED.search(s) and not OFFER.search(s) for s in last)


def nudge_for(text: str, turn: list[dict]) -> str | None:
    """The one nudge a tool-less reply gets, or None: asked to remember but nothing saved first, then
    announced-not-done, then claimed-not-done. None once the turn already had a nudge of these kinds."""
    if any(m.get("check") for m in turn if m["role"] == "user"):
        return None
    said = next((str(m.get("text") or m.get("content") or "") for m in turn if m["role"] == "user"), "")
    kept = any(c["name"] in ("save_note", "files") for m in turn if m["role"] == "assistant"
               for c in m.get("tool_calls") or [])
    if asked_to_remember(said) and not kept:
        return REMEMBER_NUDGE
    if announced(text):
        return ANNOUNCE_NUDGE
    claim = CLAIMED.search(text)
    worked = any(m["role"] == "tool" and not m.get("is_error") for m in turn)
    if claim and not worked:
        return CLAIM_NUDGE.format(verb=next(g for g in claim.groups() if g).lower())
    return None
