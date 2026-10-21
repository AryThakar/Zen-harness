# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""main's honesty checks: what the system guarantees about main's own replies and calls, by code.

Two kinds, as everywhere in the app: the guards before a call judge it, so they say why they stopped it and run
it when main confirms (the same call with confirm: true - registry.CONFIRMABLE); the checks after a reply only
advise, once per turn, with a hidden [App check] message.

The helpers' results are checked in workers/checks.py (sources, numbers, reports); these are the checks on
main itself, each from a measured failure in the user's own test chat (found in the system pass, 2026-10-04):

    repeated failure    the same call that already failed in this turn is stopped: the model is told what
                        failed and to try something else (it repeated a failed download) - or to confirm
                        it, for a failure that may pass
    how, not what       a task for analyze_or_code that spells out the files to save, the libraries or the code steps
                        (none of them the user's words) is stopped: say what is wanted, the helper picks its tools
                        (its tested chart, forecast and model tools went unused behind main's recipes) - or confirm
    a typed-out table   a task for analyze_or_code carrying a found table's numbers by hand, when research_web named
                        the page to bring the table in from, is stopped: download it first (a month of share prices
                        was typed into the task and charted with matplotlib) - or confirm
    pictures again      a third generate_image in one turn is stopped: the model is told to show the best one and
                        let the user ask for another try (it judged its own pictures and made 4 in a row, ~2 min,
                        2026-10-16) - or to confirm it, when the user asked for several
    invented data       a helper task carrying several numbers that no tool returned and the user never
                        wrote is stopped before the helper runs (invented sales figures went to the coder,
                        were charted, then explained as real); confirmed - sample data the user asked for,
                        a demo - it runs, and the helper is told the numbers are sample data to say so
    announced, not done a reply that ends by announcing an action ("One moment.", "I am writing a script
                        now.", "**Action:** ...") with no tool call gets one hidden nudge: act now, or say
                        plainly what can't be done (9 times in one chat)
    asked to remember   the user asked to remember or note something and the reply saved nothing (no
                        files write or edit): one hidden nudge to add it to agent.md ("I will remember
                        that" with no note, after main's prompt already said to save it)
    claimed, not done   a reply claiming an action ("I saved a note", "has been downloaded") in a turn
                        where no tool did anything gets one hidden nudge to do it or correct the reply
                        ("remember X" answered "I saved a note" without saving it, 4 of 6)
    a file that isn't   a reply naming a file as saved, written or created ("I have saved the changes to
                        changes.md") when no file of that name is in the workspace, in a turn where other tools
                        did work (two writes refused, a third worked - the held-out run of 2026-10-15). Only a
                        file that doesn't exist counts: a file the reply only mentions (one it read) is there

Each nudge is sent at most once per turn; after it the reply stands. Claims about what a tool that did
run really did (an image "with the date added") are checked where they happen: analyze_or_code's report
against its run, vision after generate_image.
"""

from __future__ import annotations

import json
import re

from collections.abc import Callable

from .prompts import (ANNOUNCE_NUDGE, BROKEN_CODE_NUDGE, BROKEN_NUDGE, CLAIM_NUDGE, CURRENCY_NUDGE, FILE_NUDGE,
                      NUMBERS_NUDGE, REMEMBER_NUDGE)
from .registry import CONFIRMABLE, split_confirm
from .workers import DELEGATES, _call_key, numbers_in, ungrounded_numbers
from .workers.checks import JSON_BREAKS, MONEY, NO_CURRENCY

INVENTED_MIN = 3  # numbers no one gave, in one helper task, before it is refused
PICTURES_PER_TURN = 1  # generate_image calls in one turn before another is refused (unless main confirms)

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


def pictures_again(turn: list[dict], call: dict) -> str | None:
    """For generate_image: why it is stopped when this turn already made PICTURES_PER_TURN pictures, else None."""
    if call["name"] != "generate_image":
        return None
    made = sum(1 for m in turn if m["role"] == "tool" and m.get("name") == "generate_image" and not m.get("is_error"))
    if made < PICTURES_PER_TURN:
        return None
    # Measured 2026-10-17: asked once for "a Dr. Stone image", main judged each picture by its description and made
    # two a turn, three turns running - the user asked it to stop.
    return (f"Not made: {'a picture was' if made == 1 else f'{made} pictures were'} made in this turn already. Show "
            "the user what was made and say in a sentence what is off, if anything; they ask for another try if they "
            "want one. If the user asked for more than one picture, make the same call again with confirm: true.")


# How, not what, in a task for the analyst: files to save, libraries, code steps. Measured in the user's chats
# (2026-10-16): main's tasks read "save as work/reliance_line_chart.png ... feature engineering ... train a Random
# Forest regressor", the helper followed them in its own scripts, and across all traced turns it made 39 script calls
# to make_chart 12, predict 1, forecast 0 - its tested tools went unused.
SAVE_AS = re.compile(r"\b[\w./-]+\.(?:png|jpe?g|svg|gif|webp)\b", re.I)
LIBRARY = re.compile(r"\b(?:matplotlib|seaborn|plotly|mplfinance|sklearn|scikit-learn|statsmodels|xgboost|lightgbm"
                     r"|shap|prophet|pandas|numpy)\b|\b(?:plt|sns|pd|np)\.\w+", re.I)
CODE_STEP = re.compile(r"\bdatetime index\b|\bfeature engineering\b|\b(?:train[/ -]test|split the data|split data)\b"
                       r"|\btrain an? [\w -]{0,30}(?:regressor|classifier)\b|\bload the (?:csv|file|data)\b"
                       r"|\bdropna\b|\bgroupby\b|\bgroup by\b|\brolling\b|\bhigh dpi\b|\bdpi\b"
                       r"|\bfeature importances?\b|\bone-hot\b|\bcross[- ]validat\w*|\b\d[\d,]* iterations\b", re.I)
# A method named for the helper is the how itself: its compare, forecast, predict and find_patterns choose and test
# theirs (measured 2026-10-17: "run a permutation test ... 10,000 iterations ... also do a bootstrap" for "is the
# difference between categories real?" - the analyst wrote a script instead of compare).
METHOD = re.compile(r"\b(?:permutation tests?|bootstrap\w*|t-tests?|t tests?|chi-?squared?|anova|mann-whitney|kruskal\w*"
                    r"|wilcoxon|arima|sarima\w*|holt-winters|k-means|kmeans|dbscan|random forests?|gradient boost\w*"
                    r"|(?:linear|logistic) regression|monte carlo)\b", re.I)
# A numbered list of steps is one sign more (measured 2026-10-17: "Steps: 1. Clean ... 2. group by bill_no ... 4. random
# forest regression + SHAP ..." for "what drives a big basket? use machine learning") - a list of what is wanted, alone,
# passes: a refusal needs two signs.
NUMBERED = re.compile(r"^\s*(?:step\s*)?\d+[.):]\s", re.I | re.M)


def dictated_how(call: dict, messages: list[dict], exists: Callable[[str], bool] = lambda name: False) -> str | None:
    """For a task to analyze_or_code: why it is stopped when it spells out how to do the work - the files to save, the
    libraries or the code steps - that the user never asked for; the helper picks its own tested tools. A picture
    already in the workspace (``exists``) is what the task works on, not a file to make."""
    given, _ = split_confirm(call["input"])
    if call["name"] != "analyze_or_code" or not isinstance(given, dict):
        return None
    task = str(given.get("task") or "")
    said = " ".join(str(m.get("text") or m.get("content") or "") for m in messages
                    if m["role"] == "user" and not m.get("hidden")).lower()
    found = [f for f in dict.fromkeys(m.group(0) for p in (SAVE_AS, LIBRARY, METHOD) for m in p.finditer(task))
             if f.lower() not in said and not (SAVE_AS.fullmatch(f) and exists(f))]
    steps = [f for f in dict.fromkeys(m.group(0) for m in CODE_STEP.finditer(task)) if f.lower() not in said]
    if len(NUMBERED.findall(task)) >= 3 and len(NUMBERED.findall(said)) < 3:
        steps.append("numbered steps")
    if not found and len(steps) < 2:
        return None
    return (f"Not run: the task says how to do the work ({', '.join((found + steps)[:5])}). Give the helper what the "
            "user wants and why, in plain words: it picks its tested tools itself - make_chart for a chart, forecast for "
            "coming periods, predict for a model - and names its own files. If the user asked for exactly this, make the "
            "same call again with confirm: true.")


TYPED_MIN = 10  # numbers of a found table typed into one analyst task before it is stopped


def typed_table(call: dict, turn: list[dict]) -> str | None:
    """For a task to analyze_or_code: why it is stopped when it carries a table's numbers typed out of research_web's
    answer while research_web named where that table is (its downloads) and nothing was brought in this turn. Measured
    (2026-10-17): a month of MRF closes, some marked '~' as estimates, went into the task by hand - no table, so the
    helper drew with matplotlib and main guessed next week itself; the page's table came in exact with one download."""
    given, _ = split_confirm(call["input"])
    if call["name"] != "analyze_or_code" or not isinstance(given, dict):
        return None
    found, links = [], []
    for m in turn:
        if m["role"] == "tool" and m.get("name") == "research_web" and not m.get("is_error"):
            try:
                links += json.loads(m.get("content") or "{}").get("downloads") or []
            except (ValueError, AttributeError):
                continue
            found.append(str(m.get("content")))
    calls = [c for m in turn if m["role"] == "assistant" for c in m.get("tool_calls") or []]
    if not links or any(c["name"] == "files" and isinstance(g := split_confirm(c["input"])[0], dict)
                        and g.get("mode") == "download" for c in calls):
        return None
    pool = {abs(v) for text in found for _, v, _, _ in numbers_in(JSON_BREAKS.sub(" ", text))}
    typed = {abs(v) for raw, v, _, _ in numbers_in(str(given.get("task") or ""))
             if abs(v) > 10 and not _year(raw) and abs(v) in pool}
    if len(typed) < TYPED_MIN:
        return None
    return (f"Not run: the task carries {len(typed)} numbers typed out of research_web's answer, and research_web "
            f"named where their table is. Bring it in first - files(mode='download', link='{links[0]}'), the user "
            "approves it - then give the helper the table by its name: its charts, tests and forecasts run on the exact "
            "data." + CONFIRMABLE)


def _key(call: dict) -> str | None:
    """A call's identity, its confirm aside (the confirmed call is the same call)."""
    given, _ = split_confirm(call["input"])
    return _call_key(call["name"], given) if isinstance(given, dict) else None


# Dates are not data: 'Oct 19-23', '16 September', '2026-10-16' (measured 2026-10-17: a task for "next week, Oct 19-23"
# was refused as inventing the numbers 19 and 23)
MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
DATES = re.compile(rf"\b{MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:\s*[-–]\s*\d{{1,2}}(?:st|nd|rd|th)?)?\b"
                   rf"|\b\d{{1,2}}(?:st|nd|rd|th)?(?:\s*[-–]\s*\d{{1,2}})?\s+{MONTH}"
                   r"|\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b", re.I)


def invented_numbers(call: dict, messages: list[dict]) -> str | None:
    """For a helper task: why it is stopped when it carries numbers that came from nowhere - no tool result and
    no user message in the chat holds them (years and small counts don't count)."""
    given, _ = split_confirm(call["input"])
    if call["name"] not in DELEGATES or not isinstance(given, dict):
        return None
    task = DATES.sub(" ", str(given.get("task") or given.get("question") or ""))
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


# A file's name as a reply writes it: in backticks or bold, or a bare name with an extension ('memo.docx').
FILE_NAMED = re.compile(r"`([^`\n]{1,120}?\.[A-Za-z0-9]{1,5})`|\*\*([^*\n]{1,120}?\.[A-Za-z0-9]{1,5})\*\*"
                        r"|(?<![\w/.:-])([\w-]{1,80}(?:/[\w-]{1,80})*\.(?:md|txt|csv|xlsx|docx|pptx|pdf|py|html|json|png|"
                        r"jpe?g|svg))\b", re.I)


# Zen saying it made a file - in the first person: a summary's "has been added" quotes a document, and a file
# deleted or renamed away is meant to be gone (both measured as false nudges, replaying 333 stored replies).
MADE = re.compile(r"\bI(?:'ve| have)? (?:now |just |also |successfully |already )?"
                  r"(?:saved|stored|created|wrote|written|generated|downloaded|exported|made|put)\b", re.I)


def missing_files(text: str, exists: Callable[[str], bool]) -> list[str]:
    """The files the reply says it saved, wrote or made that the workspace doesn't have: in each sentence where Zen
    says it made something, the names of files - those that exist (one it read, one it made) are fine."""
    named = []
    for sentence in SENTENCES.split(text):
        if MADE.search(sentence):
            named += [next(g for g in m.groups() if g) for m in FILE_NAMED.finditer(sentence)]
    return [n for n in dict.fromkeys(named) if "://" not in n and not exists(n)]


UNFOUNDED_MIN = 3  # numbers in main's reply that no result holds, in a turn a helper worked in, before it is checked
DATA_HELPERS = ("analyze_or_code", "research_web", "ask_knowledge")


def unfounded_numbers(text: str, turn: list[dict], history: list[dict]) -> list[str]:
    """Numbers in main's reply that no tool result, user message or earlier reply in the chat holds - checked in a turn
    a helper worked in (a chat's own examples and explanations are main's to give). Measured 2026-10-17: the analyst
    charted and forecast MRF from the downloaded table and main answered with a 22-row price table of its own, four
    of its dates Saturdays, and a 'support level' below the month's real low."""
    if not any(m["role"] == "tool" and m.get("name") in DATA_HELPERS and not m.get("is_error") for m in turn):
        return []
    now = {id(m) for m in turn}  # this turn's own replies are not evidence; the chat's earlier ones are
    evidence = [str(m.get("content") or m.get("text") or "") for m in history + turn
                if m["role"] in ("tool", "user") or (m["role"] == "assistant" and id(m) not in now)]
    missing = [n for n in ungrounded_numbers(text, evidence) if not _year(n)]
    return missing if len(missing) >= UNFOUNDED_MIN else []


def added_currency(text: str, turn: list[dict], history: list[dict]) -> str | None:
    """A currency sign on the reply's numbers that nothing the tools returned or the user wrote uses, in a turn a
    helper worked on data in - or None. Measured 2026-10-22: a cafe's sales, a table with no currency, answered as
    "$16,839" and "$36,422" (the prompt's rule against it was not enough)."""
    if not any(m["role"] == "tool" and m.get("name") in DATA_HELPERS and not m.get("is_error") for m in turn):
        return None
    # a helper's answer its own check warned about (its sign is on no data) is no evidence: measured 2026-10-21, the
    # analyst kept "$40,060" after being sent back once, and main copied it - the answer itself "proved" the sign
    evidence = "".join(str(m.get("content") or m.get("text") or "") for m in history + turn
                       if m["role"] == "user" or m["role"] == "tool" and NO_CURRENCY not in str(m.get("content") or ""))
    return next((sign for sign in dict.fromkeys(MONEY.findall(text)) if sign not in evidence), None)


def still_broken(turn: list[dict], now: Callable[[str], str | None] | None = None) -> list[tuple[str, str]]:
    """(file, why) for each file this turn wrote or edited that still can't run, by its latest write - a Python file
    or a page whose script doesn't parse or fails when used (file_ops.written_code). Measured 2026-10-17: a cup game
    whose whole script failed on line 478 was answered with "Here's your game! Click Start Game"."""
    latest: dict[str, str | None] = {}
    for m in turn:
        if m["role"] != "tool" or m.get("name") != "files" or m.get("is_error"):
            continue
        try:
            res = json.loads(m.get("content") or "{}")
        except ValueError:
            continue
        if isinstance(res, dict) and "not_run" in res and (name := res.get("name") or res.get("file")):
            latest[name] = res.get("broken")
    # ``now``: a program as it is at the reply - a helper may have fixed (or broken) it after main's own write (a page
    # keeps its browser try's verdict: it parsing says nothing of its runtime error)
    out = []
    for name, why in latest.items():
        if why and now is not None and name.lower().endswith(".py"):
            why = now(name)
        if why:
            out.append((name, why))
    return out


def nudge_for(text: str, turn: list[dict], exists: Callable[[str], bool] | None = None,
              history: list[dict] | None = None, problem: Callable[[str], str | None] | None = None) -> str | None:
    """The one nudge a tool-less reply gets, or None: asked to remember but nothing saved, then
    announced-not-done, then claimed-not-done, then a file named as made that isn't there (exists: whether the
    workspace has a file of that name), then numbers no result holds (history: the chat before this turn). None once
    the turn already had a nudge of these kinds. (A call written out as text is made a real call where calls are read:
    providers/gemma_calls.py.)"""
    if any(m.get("check") for m in turn if m["role"] == "user"):
        return None
    said = next((str(m.get("text") or m.get("content") or "") for m in turn if m["role"] == "user"), "")
    kept = any(c["name"] == "files" and isinstance(c.get("input"), dict) and c["input"].get("mode") in ("write", "edit")
               for m in turn if m["role"] == "assistant" for c in m.get("tool_calls") or [])  # agent.md, or a file
    if asked_to_remember(said) and not kept:
        return REMEMBER_NUDGE
    if announced(text):
        return ANNOUNCE_NUDGE
    claim = CLAIMED.search(text)
    worked = any(m["role"] == "tool" and not m.get("is_error") for m in turn)
    if claim and not worked:
        return CLAIM_NUDGE.format(verb=next(g for g in claim.groups() if g).lower())
    missing = missing_files(text, exists) if exists else []
    if missing:
        return FILE_NUDGE.format(name=", ".join(missing))
    broken = still_broken(turn, problem)
    if broken:  # a program goes to analyze_or_code, which runs what it fixes (measured: main's own indentation edits
        # went round for 15 minutes, then handed the user the fix); a page main fixes itself, tried in a browser
        name, why = broken[0]
        return (BROKEN_CODE_NUDGE if name.lower().endswith(".py") else BROKEN_NUDGE).format(why=why)
    sign = added_currency(text, turn, history or [])
    if sign:
        return CURRENCY_NUDGE.format(sign=sign)
    unfounded = unfounded_numbers(text, turn, history or [])
    if unfounded:
        return NUMBERS_NUDGE.format(numbers=", ".join(unfounded[:6]))
    return None
