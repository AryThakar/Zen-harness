"""What a helper's tools returned (Trace) - the evidence its result is checked against - and the guards
that stop a call before it runs: a repeat, a table nobody inspected, a choice only the user can make."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from app.tools.core import ToolResult
from app.tools.scratch import Scratch

from ..registry import ToolContext


SCRIPT_TOOLS = ("run_code", "fix_code")


@dataclass
class Trace:
    """What a worker's tools returned - the evidence its result is checked against."""
    outputs: list[str] = field(default_factory=list)
    read_urls: set[str] = field(default_factory=set)
    found_urls: set[str] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)
    python_runs: list[tuple[str, bool]] = field(default_factory=list)  # (code, ran cleanly)
    python_outputs: list[str] = field(default_factory=list)
    cards: dict[str, dict] = field(default_factory=dict)  # url -> what the UI shows for a web source
    passages: list[tuple[str, str]] = field(default_factory=list)  # (source, text) the knowledge tools returned
    calls: dict[str, ToolResult] = field(default_factory=dict)  # each distinct call (name + input) -> its result
    tried_urls: set[str] = field(default_factory=set)  # every page read_webpage was asked for, read or not
    failed_urls: dict[str, str] = field(default_factory=dict)  # page -> why it could not be read
    counts: dict[str, int] = field(default_factory=dict)  # tool -> calls that ran
    charts: int = 0  # charts the user saw (a failed forecast shows none)
    failures: list[str] = field(default_factory=list)  # "tool target: why", for the briefing
    not_run: int = 0  # calls refused: repeats, tools outside the profile
    existing: set[str] = field(default_factory=set)  # workspace ids before the task, to tell what it made
    last_query: str | None = None  # the latest web search, to focus long pages on
    inspected: set[str] = field(default_factory=set)  # tables inspect_data looked at in this task
    questions: list[dict] = field(default_factory=list)  # questions tools raised for the user, with options
    scratch: Scratch | None = None  # the helper's scratch script (the coder's save checks its last run)
    table: str | None = None  # the table the task is about (the analyst's report is kept on it)

    def add(self, name: str, args: dict, result: ToolResult) -> None:
        self.calls[_call_key(name, args)] = result
        self.counts[name] = self.counts.get(name, 0) + 1
        if name in ("make_chart", "forecast") and result.status in ("ok", "partial") and result.artifact:
            self.charts += 1
        self.outputs.append(json.dumps(result.result, ensure_ascii=False, default=str))
        if result.status not in ("ok", "partial"):
            target = next((str(args[k]) for k in ("url", "query", "data", "file", "table") if args.get(k)), "")
            why = " ".join(str((result.result.get("error") or {}).get("message", result.status)).split())
            self.failures.append(f"{name} {target[:80]}: {why[:140]}".replace(" :", ":"))
        res = result.result
        if name == "inspect_data" and result.status == "ok":
            self.inspected.add(args.get("data", ""))
        self.questions += [{**q, "table": args.get("data")} for q in res.get("questions", [])
                           if isinstance(q, dict) and q.get("question")]
        if name == "read_webpage":
            self.tried_urls.add(args.get("url", ""))
            if result.status not in ("ok", "partial"):
                self.failed_urls[args.get("url", "").strip()] = str((res.get("error") or {}).get("message", ""))
        if name == "read_webpage" and result.status == "ok":
            self.read_urls.add(res["url"])
            self.read_urls.add(args.get("url", ""))
            card = {k: v for k, v in (result.artifact or {}).items() if k != "type"}
            self.cards[res["url"]] = self.cards[args.get("url", "")] = card
        if name == "web_search":
            self.found_urls.update(r["url"] for r in res.get("results", []))
            self.last_query = args.get("query") or self.last_query
        notes = res.get("notes", []) if name == "search_files" else []  # forecast's "notes" are plain lines
        for p in res.get("passages", []) + notes + res.get("turns", []):
            source = p.get("source") or f"conversation {p.get('conversation')}"
            self.sources.add(source)
            self.passages.append((source, p.get("text", "")))
        if name == "read_file" and result.status == "ok":
            self.sources.update({res["file"], res["name"]})
            self.passages.append((res["name"], res.get("text", "")))
        if name in SCRIPT_TOOLS:  # the whole script that ran (fix_code's input is only the changed lines)
            self.python_runs.append(((result.artifact or {}).get("code", args.get("code", "")), result.status == "ok"))
            self.python_outputs.append(res.get("output", ""))


TABLE_ID = re.compile(r"\bdata_\d+(?:_\w+)?\b")


def _uninspected(ctx: ToolContext, trace: Trace, name: str, args: dict) -> str | None:
    """A table the call works on that nobody has inspected yet: a data worker that skips inspect_data
    computes on hidden summary rows, duplicates and text numbers. Tables made during this task come
    from inspected ones; an inspection from an earlier task is remembered on the table."""
    if name == "inspect_data":
        return None
    named = TABLE_ID.findall(args.get("sql", "")) if name == "query_data" else [args.get("data", "")]
    for t in named:
        if (t in trace.existing and t not in trace.inspected and t in ctx.ws.ids("table")
                and ctx.ws.entry(t).get("meta", {}).get("issues") is None):
            return t
    return None


def _guessed(trace: Trace, name: str, args: dict, context: str) -> str | None:
    """A cleaning step that settles a question only the user can answer (e.g. day-first dates),
    when the task does not say the user chose it: the question, so the worker asks instead."""
    if name != "clean_data":
        return None
    told = re.sub(r"[^a-z]", "", context.lower())
    for q in trace.questions:
        for option in q.get("options", []):
            step = option.get("step") if isinstance(option, dict) else None
            choice = re.sub(r"[^a-z]", "", str(option.get("label", "")).split("(")[0].lower()) if step else ""
            if step and choice not in told and any(_same_step(s, step) for s in args.get("steps", [])):
                return q["question"]
    return None


def _same_step(given, wanted: dict) -> bool:
    return isinstance(given, dict) and all(given.get(k) == v for k, v in wanted.items())


def _call_key(name: str, args: dict) -> str:
    """Two calls are the same when their inputs only differ in case or spacing of words (an address
    keeps its case: paths and video ids are case-sensitive)."""
    tidy = {k: (v.strip() if k == "url" else " ".join(v.split()).lower()) if k in ("query", "url", "find")
            and isinstance(v, str) else v for k, v in args.items()}
    return name + json.dumps(tidy, sort_keys=True, ensure_ascii=False, default=str)


def _repeat(trace: Trace, name: str, args: dict) -> ToolResult | None:
    """A call the worker already made with the same input is not run again - a small model re-runs
    a search after a failed read instead of reading another result - and it is told what to do
    instead. Running the script is exempt: the same code can give a new result once files have changed."""
    if name == "read_webpage" and str(args.get("url", "")).strip() in trace.failed_urls:
        # The same page with other find words is the same failure (find only picks lines to show).
        why = trace.failed_urls[str(args["url"]).strip()]
        return ToolResult("invalid_arguments", {"error": {"message": (
            f"This page already failed ({why.rstrip('.')}). Read another source instead.")}})
    earlier = trace.calls.get(_call_key(name, args))
    if earlier is None or name in SCRIPT_TOOLS:
        return None
    if earlier.status not in ("ok", "partial"):
        why = (earlier.result.get("error") or {}).get("message", "")
        message = f"This exact call already failed ({why.rstrip('.')}). Don't repeat it; try something else."
    elif name == "web_search":
        unread = [r["url"] for r in earlier.result.get("results", []) if r["url"] not in trace.tried_urls][:3]
        message = "You already ran this exact search; its results are above. " + (
            f"Read one you haven't tried: {', '.join(unread)}." if unread else "Search with different words.")
    else:
        message = "You already made this exact call; its result is above. Use it, or change the input."
    return ToolResult("invalid_arguments", {"error": {"message": message}})
