"""main's honesty checks: what the system guarantees about main's own replies and calls, by code.

Two kinds, as everywhere in the app: the guards before a call judge it, so they say why they stopped it and run
it when main confirms (the same call with confirm: true - registry.CONFIRMABLE); the checks after a reply only
advise, once per turn, with a hidden [App check] message.

The helpers' results are checked in workers/checks.py (sources, numbers, reports); these are the checks on
main itself, each from a measured failure in the user's own test chat (found in the system pass, 2026-10-04):

    repeated failure    the same call that already failed in this turn is stopped: the model is told what
                        failed and to try something else (it repeated a failed download) - or to confirm
                        it, for a failure that may pass
    invented data       a helper task carrying several numbers that no tool returned and the user never
                        wrote is stopped before the helper runs (invented sales figures went to the coder,
                        were charted, then explained as real); confirmed - sample data the user asked for,
                        a demo - it runs, and the helper is told the numbers are sample data to say so
    announced, not done a reply that ends by announcing an action ("One moment.", "I am writing a script
                        now.", "**Action:** ...") with no tool call gets one hidden nudge: act now, or say
                        plainly what can't be done (9 times in one chat)
    asked to remember   the user asked to remember or note something and the reply saved nothing (no
                        notes(save) or files call): one hidden nudge to save it with notes ("I will remember
                        that" with no note, after main's prompt already said to save it)
    claimed, not done   a reply claiming an action ("I saved a note", "has been downloaded") in a turn
                        where no tool did anything gets one hidden nudge to do it or correct the reply
                        ("remember X" answered "I saved a note" without saving it, 4 of 6)
    written, not called a reply that is a tool call written out as JSON text gets one hidden nudge to make
                        the call or answer in words (the user saw '{"action": "generate_image", ...}')

Each nudge is sent at most once per turn; after it the reply stands. Claims about what a tool that did
run really did (an image "with the date added") are checked where they happen: the coder's report
against its run, vision after generate_image.
"""

from __future__ import annotations

import json
import re

from .prompts import ANNOUNCE_NUDGE, CLAIM_NUDGE, REMEMBER_NUDGE, WRITTEN_NUDGE
from .registry import CONFIRMABLE, split_confirm
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
    r"fix|edit|add|load|import|clean|query|chart|forecast|delete|rename|research|summari[sz]e|go ahead|try|retry|"
    r"find|locate)\b"
    r"|^\W*action\W*:",
    re.I)
# Not a promise to act now: an offer ("Let me know if ... and I'll update it"), or waiting for the user
# ("Once you provide these numbers, I will run the model"). Commitments ("I will always use ...", "I will
# remember ...") don't use the action verbs above. Measured on the 188 replies stored so far: the 9
# real announcements caught, no other reply.
OFFER = re.compile(r"\b(?:if|let me know|would you|do you want|want me|shall I|should I|feel free|once|when(?:ever)? you|"
                   r"as soon as|after you|until|tell me|send me|give me|share (?:it|them|that|this|with me)|"
                   r"please (?:tell|share|send|give|upload|provide|let))\b", re.I)
# ("Tell me, and I'll make sure to remember it" and "Please tell me what you like ..., and I will add it to
# your notes" wait for the user - measured as false nudges, 2 in one chat.)
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
# about the past ("do you remember ...?", "do remember when I like coffee" typed without "you" or "?") is
# not one, and neither is a note named as a thing ("a note that you used to answer me") - both measured
# as false nudges in one chat.
REMEMBER = re.compile(r"\b(?:remember|memori[sz]e|take (?:a )?note|make a note|"
                      r"(?<!\ba )(?<!\bthe )(?<!\bthis )(?<!\bthat )(?<!\bany )(?<!\bmy )(?<!\byour )(?<!\bwhich )"
                      r"note (?:that|this|it|down)|"
                      r"keep (?:that |this |it )?in (?:mind|view)|don'?t forget|from now on|in (?:every|all|future) "
                      r"chats?)\b", re.I)
# ("i given you name dont you remember" - a reproach about the past, no question mark - was nudged; found
# replaying today's checks over the 301 stored tool-less replies, 2026-10-09: 17 right, this one wrong.)
RECALLING = re.compile(r"\b(?:do|did|can|could|would|don'?t|didn'?t|can'?t|won'?t) (?:you|u) (?:still )?remember\b"
                       r"|^\W*(?:do|did|can|could|would) (?:still )?remember\b|\bremember when\b", re.I)


def asked_to_remember(said: str) -> bool:
    said = said.strip()
    return bool(REMEMBER.search(said)) and not said.endswith("?") and not RECALLING.search(said)


def this_turn(messages: list[dict]) -> list[dict]:
    """The current turn: from the user's last own message (hidden nudges belong to the turn)."""
    start = max((i for i, m in enumerate(messages) if m["role"] == "user" and not m.get("hidden")), default=0)
    return messages[start:]


def repeated_failure(turn: list[dict], call: dict) -> str | None:
    """Why this exact call is stopped - it already failed in this turn - else None."""
    key = _key(call)
    if key is None:
        return None
    failed = {m["tool_call_id"] for m in turn if m["role"] == "tool" and m.get("is_error")}
    earlier = [c for m in turn if m["role"] == "assistant" for c in m.get("tool_calls") or []]
    if any(c["id"] in failed and _key(c) == key for c in earlier):
        return ("This exact call already failed in this turn (see its result above): try another way, or tell the "
                "user what didn't work." + CONFIRMABLE)
    return None


def _key(call: dict) -> str | None:
    """A call's identity, its confirm aside (the confirmed call is the same call)."""
    given, _ = split_confirm(call["input"])
    return _call_key(call["name"], given) if isinstance(given, dict) else None


def invented_numbers(call: dict, messages: list[dict]) -> str | None:
    """For a helper task: why it is stopped when it carries numbers that came from nowhere - no tool result and
    no user message in the chat holds them (years and small counts don't count)."""
    given, _ = split_confirm(call["input"])
    if call["name"] not in DELEGATES or not isinstance(given, dict):
        return None
    task = str(given.get("task") or given.get("question") or "")
    # Results that are data - not errors: a refusal quotes the very numbers it refused.
    evidence = [str(m.get("content") or "") for m in messages if m["role"] == "tool" and not m.get("is_error")]
    evidence += [str(m.get("text") or m.get("content") or "") for m in messages if m["role"] == "user"]
    missing = [n for n in ungrounded_numbers(task, evidence) if not _year(n)]
    if len(missing) < INVENTED_MIN:
        return None
    return (f"Not run: the task gives numbers that no tool returned and the user never gave ({', '.join(missing[:6])}). "
            "Real data is found first (research_web, the user's files) or asked of the user. If they are meant to be "
            "made up - sample data the user asked for, a demo - make the same call again with confirm: true: the "
            "helper is told they are sample data, and says so.")


# What a confirmed task with invented numbers carries to its helper.
SAMPLE_DATA = ("\n(From the app: the numbers in this task were written by main - no tool found them and the user "
               "didn't give them. Treat them as sample data, and say so in your answer.)")


def _year(raw: str) -> bool:
    values = [v for _, v, _, _ in numbers_in(raw)]
    return bool(values) and values[0] == int(values[0]) and 1900 <= values[0] <= 2100


def announced(text: str) -> bool:
    """Whether a reply ends by promising an action: a promise among its last three sentences that isn't
    an offer, or a last line that opens something it never gives ('Here is the next section:'). A
    reply ending with a question asks the user - fine, also in bold or before an emoji ('**What topic
    would you like?**' was nudged: 50 s of checking, measured)."""
    ending = text.strip()
    plain = re.sub(r"[^\w?.!:]+$", "", ending)
    if not plain or plain.endswith("?"):
        return False
    if re.sub(r"[\s*_]+$", "", ending).endswith(":"):  # (not plain: "Have fun :)" opens nothing)
        return True
    last = [s for s in SENTENCES.split(ending) if s.strip()][-3:]
    return any(ANNOUNCED.search(s) and not OFFER.search(s) for s in last)


CALL_KEYS = ("action", "tool", "tool_name", "name", "function")
FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def written_call(text: str, tools) -> str | None:
    """The tool's name when the whole reply is a call to it written out as JSON text, else None. The
    measured one wasn't valid JSON (a quoted object inside), so a name key is also read from the text."""
    body = FENCE.sub("", text.strip())
    if not (body.startswith("{") and body.endswith("}")):
        return None
    try:
        data = json.loads(body)
    except ValueError:
        named = re.search(rf'"(?:{"|".join(CALL_KEYS)})"\s*:\s*"(\w+)"', body)
        return named.group(1) if named and named.group(1) in tools else None
    for key in CALL_KEYS if isinstance(data, dict) else ():
        value = data.get(key)
        value = value.get("name") if isinstance(value, dict) else value
        if isinstance(value, str) and value in tools:
            return value
    return None


def nudge_for(text: str, turn: list[dict], tools=()) -> str | None:
    """The one nudge a tool-less reply gets, or None: a call written as text first, then asked to remember
    but nothing saved, then announced-not-done, then claimed-not-done. None once the turn already had a
    nudge of these kinds."""
    if any(m.get("check") for m in turn if m["role"] == "user"):
        return None
    name = written_call(text, tools)
    if name:
        return WRITTEN_NUDGE.format(name=name)
    said = next((str(m.get("text") or m.get("content") or "") for m in turn if m["role"] == "user"), "")
    kept = any(c["name"] == "files" or (c["name"] == "notes" and isinstance(c.get("input"), dict)
                                        and c["input"].get("mode") == "save")
               for m in turn if m["role"] == "assistant" for c in m.get("tool_calls") or [])
    if asked_to_remember(said) and not kept:
        return REMEMBER_NUDGE
    if announced(text):
        return ANNOUNCE_NUDGE
    claim = CLAIMED.search(text)
    worked = any(m["role"] == "tool" and not m.get("is_error") for m in turn)
    if claim and not worked:
        return CLAIM_NUDGE.format(verb=next(g for g in claim.groups() if g).lower())
    return None
