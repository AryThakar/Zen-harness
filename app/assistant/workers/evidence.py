"""What a helper's tools returned (Trace) - the evidence its result is checked against - and the guards
before a call runs. A guard either serves the model or protects the user: a repeat of a call that worked gets its
earlier result back (nothing runs, nothing is refused); a call that already failed, or a page that couldn't be
read, is stopped with why - and runs anyway when the helper confirms it (registry.CONFIRMABLE); a choice only the
user can make, and a seventh page of one site, are not the helper's to confirm."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.tools.core import ToolResult

from ..registry import CONFIRMABLE, ToolContext


SCRIPT_TOOLS = ("run",)  # a run of the same file can give a new result once the file or its inputs changed
SEARCHES_BEFORE_READING = 3  # web searches in a row before the next one is refused: read a page they found
# A page table this long is a series - a month of trading days is ~21 rows - and ends the hunt for its rows; a
# shorter one (measured: a quote page's 7-row order book) may not be the data the task needs.
SERIES_ROWS = 15


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
    table: str | None = None  # the table the task is about (the report is kept on it)
    stamps: dict[str, str] = field(default_factory=dict)  # file -> its content hash before the task (what changed)
    site_reads: dict[str, int] = field(default_factory=dict)  # site -> pages asked of it in this task
    pages: list[str] = field(default_factory=list)  # pages read, in order: page n is pages[n - 1] (cited by number)
    searches_in_a_row: int = 0  # web searches since the last page read
    listed: dict[str, int] = field(default_factory=dict)  # page -> how many searches listed it
    last_output: str = ""  # what the latest run printed
    table_pages: list[str] = field(default_factory=list)  # pages read holding a table of SERIES_ROWS+ rows (data_table)
    table_said: bool = False  # the helper was told once to finish with that page (runner._table_found)
    run_files: dict[str, str | None] = field(default_factory=dict)  # program -> its latest run's error (None: it ran)

    def add(self, name: str, args: dict, result: ToolResult) -> None:
        self.calls[_call_key(name, args)] = result
        self.counts[name] = self.counts.get(name, 0) + 1
        held = re.search(r"\((\d+) rows", str(result.result.get("data_table") or ""))
        if name == "read_webpage" and held and int(held.group(1)) >= SERIES_ROWS and result.result.get("url"):
            self.table_pages.append(result.result["url"])
        if name in ("make_chart", "forecast") and result.status in ("ok", "partial") and result.artifact:
            self.charts += 1
        self.outputs.append(json.dumps(result.result, ensure_ascii=False, default=str))
        if result.status not in ("ok", "partial"):
            target = next((str(args[k]) for k in ("url", "query", "data", "file", "table") if args.get(k)), "")
            why = " ".join(str((result.result.get("error") or {}).get("message", result.status)).split())
            self.failures.append(f"{name} {target[:80]}: {why[:140]}".replace(" :", ":"))
        res = result.result
        if name == "inspect_data" and result.status == "ok":
            self.inspected.add(res.get("data") or args.get("data", ""))  # the table's key: it was named by path
        self.questions += [{**q, "table": res.get("data") or args.get("data")} for q in res.get("questions", [])
                           if isinstance(q, dict) and q.get("question")]
        if name == "read_webpage":
            self.tried_urls.add(args.get("url", ""))
            site = _site_of(args.get("url", ""))
            self.site_reads[site] = self.site_reads.get(site, 0) + 1
            if result.status not in ("ok", "partial"):
                self.failed_urls[args.get("url", "").strip()] = str((res.get("error") or {}).get("message", ""))
        if name == "read_webpage" and result.status == "ok":
            self.read_urls.add(res["url"])
            self.read_urls.add(args.get("url", ""))
            card = {k: v for k, v in (result.artifact or {}).items() if k != "type"}
            self.cards[res["url"]] = self.cards[args.get("url", "")] = card
            if res["url"] not in self.pages:
                self.pages.append(res["url"])
            res["source"] = self.pages.index(res["url"]) + 1  # cited by this number in the finish
        if name == "read_webpage":
            self.searches_in_a_row = 0
        if name == "web_search":
            self.found_urls.update(r["url"] for r in res.get("results", []))
            for r in res.get("results", []):
                self.listed[r["url"]] = self.listed.get(r["url"], 0) + 1
            self.last_query = args.get("query") or self.last_query
            self.searches_in_a_row += 1
        # find's files and the pages read before are sources (forecast's "notes" are plain lines)
        if name == "files" and args.get("mode") == "find" and result.status == "ok":
            for f in res.get("files", []):
                self.sources.add(f["path"])
                self.passages.append((f["path"], f.get("match", "")))
        found = res.get("pages_read_before", []) if name == "files" else []
        for p in found + res.get("turns", []):
            source = p.get("source") or f"conversation {p.get('conversation')}"
            self.sources.add(source)
            self.passages.append((source, p.get("text", "")))
        if name == "files" and args.get("mode") == "read" and result.status == "ok" and res.get("name"):
            self.sources.update({res["file"], res["name"]})
            self.passages.append((res["name"], res.get("text", "")))
        if name in SCRIPT_TOOLS:  # the whole program that ran: the file as it was
            self.python_runs.append(((result.artifact or {}).get("code", ""), result.status == "ok"))
            self.python_outputs.append(res.get("output", ""))
            self.last_output = res.get("output", "") or self.last_output
            if args.get("file"):  # each program's latest run, and why it failed
                self.run_files[str(args["file"])] = None if result.status == "ok" else _last_error(res)

    def cited(self, source) -> str | None:
        """The page a finish cites: by the number its read gave it, or by its link as read; None otherwise."""
        if isinstance(source, int) or str(source).strip().isdigit():
            n = int(source)
            return self.pages[n - 1] if 1 <= n <= len(self.pages) else None
        url = str(source).strip()
        return url if url in self.read_urls else None


def _last_error(res: dict) -> str:
    """A failed run's error, by its last line (the exception), short."""
    said = str(res.get("errors") or (res.get("error") or {}).get("message") or "it failed")
    lines = [ln.strip() for ln in said.strip().splitlines() if ln.strip()]
    return (lines[-1] if lines else "it failed")[:160]


def _unread(trace: Trace) -> list[str]:
    """The pages the searches listed that nobody read, the most often listed first."""
    return [u for u, _ in sorted(trace.listed.items(), key=lambda kv: -kv[1]) if u not in trace.tried_urls]


SITE_CAP = 6  # pages of one site in one task (the plan: a cap per task - a shop checked us after 2-3 quick visits)


def _site_of(url: str) -> str:
    return (urlsplit(str(url).strip()).hostname or "").lower().removeprefix("www.")


TABLE_ID = re.compile(r"\bdata_\d+(?:_\w+)?\b")


def _uninspected(ctx: ToolContext, trace: Trace, name: str, args: dict) -> str | None:
    """A table the call works on that nobody has inspected yet: a data worker that skips inspect_data
    computes on hidden summary rows, duplicates and text numbers. Tables made during this task come
    from inspected ones; an inspection from an earlier task is remembered on the table."""
    if name == "inspect_data":
        return None
    if name == "query_data":
        from app.tools.query_data import by_key
        named = TABLE_ID.findall(by_key(ctx.ws, str(args.get("sql", ""))))
    else:
        named = [ctx.ws.find(str(args.get("data", "")), ("table",)) or ""]
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


def _repeat(trace: Trace, name: str, args: dict, confirmed: bool = False) -> ToolResult | None:
    """The answer to a call the helper already made with the same input, instead of running it again - or None
    to run it. One that worked gives its earlier result back with a note (it may have been shortened out of view
    since: an error saying 'its result is above' left the helper without it). One that failed is stopped with why,
    since the same input fails the same way - a small model re-runs a failed read instead of reading another page
    - unless the helper confirms it (a passing failure). Running the script is exempt: the same code can give a new
    result once files have changed."""
    site = _site_of(args.get("url", "")) if name == "read_webpage" else ""
    if site and trace.site_reads.get(site, 0) >= SITE_CAP and str(args.get("url", "")).strip() not in trace.tried_urls:
        return ToolResult("invalid_arguments", {"error": {"message": (
            f"You have read {SITE_CAP} pages of {site} in this task: answer from them, or read another site.")}})
    if confirmed or name in SCRIPT_TOOLS:
        return None
    if name == "read_webpage" and str(args.get("url", "")).strip() in trace.failed_urls:
        # The same page with other find words is the same failure (find only picks lines to show).
        why = trace.failed_urls[str(args["url"]).strip()]
        return ToolResult("invalid_arguments", {"error": {"message": (
            f"This page already failed ({why.rstrip('.')}): read another source." + CONFIRMABLE)}})
    earlier = trace.calls.get(_call_key(name, args))
    if earlier is None:
        return None
    if earlier.status not in ("ok", "partial"):
        why = (earlier.result.get("error") or {}).get("message", "")
        return ToolResult("invalid_arguments", {"error": {"message": (
            f"This exact call already failed ({why.rstrip('.')}): try something else." + CONFIRMABLE)}})
    if name == "web_search":
        unread = _unread(trace)[:3]
        note = "The same search as before - its results again. " + (
            f"Read one you haven't tried: {', '.join(unread)}." if unread else "Search with other words for more.")
    else:
        note = ("You made this exact call before - its result again (nothing ran). To run it again after a change, "
                "add confirm: true.")
    return ToolResult(earlier.status, {**earlier.result, "note": note})
