"""Evidence fields small models get wrong, set right from the trace before the checks run, and a plain-text
answer read into the shape of the helper's result."""

from __future__ import annotations

import json
import re
from pathlib import PurePath

from .checks import NOTHING_FOUND, given_link, numbers_in, ungrounded_numbers
from .contracts import Profile, SourcedResult
from .evidence import Trace


JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```|^\s*(\{.*\})\s*$", re.S)


def _json_in_text(text: str) -> dict | None:
    """A result written out as JSON text instead of a submit_result call (small models do this)."""
    m = JSON_BLOCK.search(text.strip())
    if not m:
        return None
    try:
        value = json.loads(m.group(1) or m.group(2))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) and "answer" in value else None


def code_name(name: str, given_code: bool) -> str:
    """The name a kept script goes under: save_code keeps the script, and a picture (or file) its run wrote is kept
    already - so a script named after its picture is the .py of that name (seen: 'passengers_modified.png' named
    for the image the run made, and the save refused)."""
    from app.tools.file_ops import BINARY
    path = PurePath(str(name).strip())
    return f"{path.stem}.py" if not given_code and path.suffix.lower() in BINARY else str(name).strip()


def _repair(profile: Profile, result: dict, trace: Trace) -> dict:
    """Evidence fields small models get wrong, set right from the trace before the checks run."""
    if trace.questions and not result.get("options") and (
            result.get("question_for_user") or "?" in result.get("answer", "")):
        # The worker asked what a tool asked (in prose, or without the choices): hand on the tool's own
        # question and options, so the user gets buttons instead of the question being taken for an answer.
        q = trace.questions[-1]
        result = {**result, "question_for_user": q["question"],
                  "options": [o["label"] if isinstance(o, dict) else str(o) for o in q.get("options", [])] or None}
    if profile.name == "knowledge":
        return _attribute(result, trace)
    if profile.name == "web" and result.get("sources") and trace.read_urls:
        # Pages only seen as a search snippet (or never seen) come off the sources when a page it read is left:
        # the answer stands on what was read, without a whole resubmission to drop them (measured: 2 snippet-only
        # citations beside the Wikipedia page it read cost the helper a step).
        read = [s for s in result["sources"] if s in trace.read_urls]
        if read:
            result = {**result, "sources": read}
    if (profile.name == "web" and not result.get("sources") and trace.read_urls
            and not NOTHING_FOUND.search(result.get("answer", ""))):
        # The pages it read, credited when it listed none (3 of 5 web warnings in the user's chats: an answer
        # from pages read, with no sources given).
        result = {**result, "sources": [u for u in trace.cards if u in trace.read_urls][:3]}
    if profile.name == "web" and not result.get("downloads"):
        # A file link written into the answer (seen: the Iris CSV's link) is handed on as a download too -
        # only one a tool returned, so main can fetch it with the user's approval.
        from app.tools.web import is_file_link
        links = [u.rstrip(".,;:`'\")") for u in URL.findall(result.get("answer", ""))]
        found = [u for u in dict.fromkeys(links) if is_file_link(u) and given_link(u, trace)]
        if found:
            result = {**result, "downloads": found}
    return result


def _attribute(result: dict, trace: Trace) -> dict:
    """Knowledge answers: drop 'sources' no tool returned (e.g. a tool's name); if none are left,
    credit the passages that contain the answer - most of its words appear in them."""
    given = result.get("sources", [])
    real = [s for s in given if any(s in t or t in s for t in trace.sources)]
    answer = result.get("answer", "")
    if not real and numbers_in(answer):
        # The passages that hold the answer's numbers, when together they hold all of them (one fact
        # can come from one earlier turn, the next from another).
        holding = [(source, text) for source, text in trace.passages
                   if len(ungrounded_numbers(answer, [text])) < len(numbers_in(answer))]
        if holding and not ungrounded_numbers(answer, [text for _, text in holding]):
            real = [source for source, _ in holding][:3]
    if not real and not NOTHING_FOUND.search(result.get("answer", "")):
        words = {w for w in re.findall(r"\w+", result.get("answer", "").lower()) if len(w) > 2}
        scored = sorted(((len(words & set(re.findall(r"\w+", text.lower()))) / len(words), source)
                         for source, text in trace.passages) if words else [], reverse=True)
        real = [source for score, source in scored[:2] if score >= 0.5]
        if not real:
            return result  # nothing supports the answer: keep what was given, so the check can flag it
    return {**result, "sources": list(dict.fromkeys(real))}


URL = re.compile(r"https?://[^\s)\]>\"']+")


def _source_keys(source: str) -> list[str]:
    """Distinctive parts of a source label to recognise it in a plain-text answer: the file name,
    the note title, the conversation id."""
    keys = [source, source.split(",")[0].split(" - ")[0].strip()]
    keys += re.findall(r"'([^']+)'", source) + re.findall(r"conversation (\S+)", source)
    return [k for k in keys if len(k) >= 4]


FINISH_TAG = re.compile(r"<(?:submit_result|finish|save_code)\b(.*)", re.S)


TAG_VALUE = re.compile(r'\b(?:answer|results)\s*=\s*"(.*?)"\s*(?:/?>|$|\w+\s*=)', re.S)


# The finish tool's bare name as the answer's last line (measured: a web answer ended "...\n\nsubmit_result")
BARE_FINISH = re.compile(r"\n\s*`?(?:submit_result|finish)(?:\(\))?`?\s*$")


def _without_finish_markup(text: str) -> str:
    """A finish call written into the text as markup - '<submit_result answer="...">' after the same
    answer in prose (measured: it reached the user as it was) - comes off; with no prose before it, the
    answer inside it is the answer."""
    text = BARE_FINISH.sub("", text)
    m = FINISH_TAG.search(text)
    if not m:
        return text
    before = text[:m.start()].strip()
    if before:
        return before
    inside = TAG_VALUE.search(m.group(1))
    return inside.group(1).strip() if inside else text


def _from_text(profile: Profile, text: str, trace: Trace) -> dict:
    """A plain-text answer in the shape of the profile's result, so it can be checked the same way."""
    text = _without_finish_markup(text.strip())
    if issubclass(profile.result_model, SourcedResult):
        seen = trace.read_urls | trace.found_urls  # an address quoted from a page is not a source
        urls = [u for u in (u.rstrip(".,;:`'\"") for u in URL.findall(text)) if u in seen]
        named = [s for s in sorted(trace.sources) if any(k and k in text for k in _source_keys(s))]
        return {"answer": text, "sources": list(dict.fromkeys(urls + named))}
    return {"answer": text}
