"""Worker sub-agents: the second model slot doing one focused task with a narrow toolset.

The main slot delegates with research_web / ask_knowledge / analyze_data / write_code. Each runs a
worker profile - its own short instructions, only its own tools, a step limit - on the worker
slot, and returns a short structured result instead of raw pages or tables:

    web        web_search, read_webpage                                 -> answer + the URLs it read
    knowledge  search_files, recall, read_file                          -> answer + the sources it was given
    data       the 9 data tools, run_code, fix_code, ask_user           -> save_report: answer + report
    coder      run_code, fix_code, read_file, view_image, ask_user      -> save: the file + its report
(run_code / fix_code: the helper's one scratch script, written whole or changed by lines; it is new
for every task - app/tools/scratch.py. Each helper's craft - how an expert works - is a short block in
its own prompt, the user's design: a small model follows a few fundamentals it reads every task more
reliably than it searches a guide. The prompts and the runner's nudges are in prompts.py.)

A worker finishes by calling submit_result (the analyst: save_report; the coder: save); its arguments
are checked by code before the main slot sees them: web sources must be pages the worker read (or at least found),
knowledge sources must be ones a tool returned, every number in a data answer must appear in a
tool result, and the coder's report must have every section and its code must have run (or its
report's facts say it fails). On a failed check the worker gets one chance to fix it; after that
the result goes back with the problem stated as a warning. The coder hands main its report, never
the code: the file itself goes to the UI as a card (docs/ASSISTANT_PLAN.md, section 5).
Charts, tables and images from worker tools go straight to the UI, not through the summary.

The data and coder helpers can ask the user directly (ask_user, at most MAX_ASKS a task): the
card shows in the chat and the helper waits in place for the answer, keeping its work (main.Turn.ask).
A question inspect_data raises (are dates day first?) is asked by the runner itself, at once, and the
cleaning step of the option the user picks is applied by code - the model never relays or guesses it.

Besides its answer, the main slot gets a briefing to decide what to do next:
    suggestion         the worker's one sentence on what else could be looked into
    question_for_user  something only the user can settle (with options): ask it, then hand the
                       task back with the answer - a worker remembers nothing between tasks
    trail              written by code from the trace, only when the answer is incomplete or
                       flagged: which tools ran, what failed and why, calls that were not run
    warnings / note    failed checks / the step limit was reached
While it works, the worker is guided the same way: tool results say what to try next, repeated
calls are answered with what to do instead, and the last two steps say so.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePath

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.providers.base import Provider, ProviderError
from app.tools.core import ToolError, ToolResult
from app.tools import ask
from app.tools.notes import pinned, source
from app.tools.reports import DATA_SECTIONS, assemble, section_problems
from app.tools.scratch import SECTIONS, Scratch, report_problems
from app.tools.scratch import save as save_file

from . import prompts
from .context import context_block, counter, now_line, prompt_limit, shorten_tool_results
from .prompts import FIX_ANSWER, FIX_FINISH, HELPER_CUT_OFF, STEPS_LEFT
from .registry import (TOOLS, AskIn, CodeTaskIn, DataTaskIn, QuestionIn, TaskIn, Tool, ToolContext, execute,
                       typed_optionals)

SUBMIT = "submit_result"
SCRIPT_TOOLS = ("run_code", "fix_code")
MAX_ASKS = 2  # questions a helper may ask the user in one task
DELEGATE_OF = {"web": "research_web", "knowledge": "ask_knowledge", "data": "analyze_data", "coder": "write_code"}
HELPER_ASK = ("Ask the user one question and wait for the answer - you keep your work and go on. Only when the "
              "task leaves a choice that changes the result (which chart, which table or column, which of two "
              "readings, a file format) and the task doesn't already say it. Give the choices as options, the "
              "recommended one first; at most 2 questions a task.")


class _Out(BaseModel):
    """Every worker can also brief the main slot: what more could be looked into, or a question
    only the user can answer (the main slot asks it, then hands the task back with the answer)."""
    model_config = ConfigDict(extra="forbid")
    suggestion: str | None = Field(None, description="optional, when the answer is incomplete: one sentence on "
                                   "what else could be looked into, or what only the user can tell")
    question_for_user: str | None = Field(None, description="a question only the user can answer (or one a tool "
                                          "returned): ask it here instead of guessing")
    options: list[str] | None = None


BRIEF = ("suggestion", "question_for_user", "options")


class SourcedResult(_Out):
    answer: str = Field(description="the answer to the task")
    sources: list[str] = Field(default_factory=list, description="the sources used, exactly as the tools gave them")


class WebResult(SourcedResult):
    downloads: list[str] = Field(default_factory=list, description="links to files the task asks for (a data table, "
                                 "a GitHub file), exactly as a page gave them - main downloads them with the user's "
                                 "approval; never paste a file's content into the answer")


class DataReport(_Out):
    """The data analyst's finish: its report, kept on the table it worked on and shown at the start of
    the next task on that table."""
    question: str = Field(description="one line: what was asked (the report's title)")
    data_used: str = Field(description="the tables used and the cleaning decisions, e.g. 'use data_4: data_1 with "
                                       "the Total row removed, dates read day-first'")
    method: str = Field(description="the tools and queries, in order, in plain words")
    results: str = Field(description="the answer to the task")
    caveats: str = Field(description="limits of the data or the method, and what to look at next")


class SaveResult(_Out):
    """The coder's finish: its scratch file kept under a name, with the report main answers from."""
    name: str = Field(description="the file's name with the right extension, e.g. 'parabola.py' (any language: "
                                   "'hello.rs', 'page.html')")
    purpose: str = Field(description="one line: what the file does (the first line of its report)")
    how_it_works: str = Field(description="its parts and flow, naming its functions as `name()`")
    inputs_outputs: str = Field(description="what it reads and what it prints or writes")
    how_to_run: str = Field(description="how the user runs it (python name.py; an .html file opens in a browser), "
                                         "and how to change the usual things")
    limits: str = Field(description="what it doesn't do yet, and sensible next steps")
    code: str | None = Field(None, description="only for a file that isn't Python: its whole content (Python is "
                                               "saved from your scratch script, as it ran)")


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


@dataclass(frozen=True)
class Profile:
    name: str
    instructions: str
    tools: tuple[str, ...]
    max_steps: int
    result_model: type[_Out]
    check: Callable[[_Out, Trace, str], list[str]]
    finish: str = SUBMIT  # the tool that hands in the result (the coder's is save)
    asks: bool = False  # may ask the user directly (ask_user, in place)

    def own_tools(self) -> dict[str, Tool]:
        tools = {n: TOOLS[n] for n in self.tools}
        if self.asks:
            tools["ask_user"] = _ask_tool(DELEGATE_OF[self.name])
        return tools


def _ask_tool(delegate: str) -> Tool:
    """A helper's ask_user: the card in the chat, the helper waiting in place for the answer."""
    def handler(ctx: ToolContext, args: AskIn) -> ToolResult:
        built = ask.ask_user(args.question, args.options, args.allow_text)  # the same checks as main's
        if built.status != "ok":
            return built
        card = {k: v for k, v in built.artifact.items() if k != "type"}
        options = card["options"]
        default = f" ({options[0]})" if options else ""
        if ctx.ask is None:  # no user to ask here (a benchmark, a test)
            return ToolResult("ok", {"answer": None, "note": f"No user can be asked now: go on with your recommended "
                                                             f"option{default} and say so in the report."})
        got = ctx.ask({"tool": delegate, **card})
        if got.get("answer"):
            return ToolResult("ok", {"question": card["question"], "answer": got["answer"]})
        why = "The user skipped the question" if got.get("skipped") else "The user didn't answer in time"
        return ToolResult("ok", {"question": card["question"], "answer": None,
                                 "note": f"{why}: go on with your recommended option{default} and say so in the report."})
    return Tool("ask_user", HELPER_ASK, AskIn, handler)


def _settle_questions(ctx: ToolContext, trace: Trace, table: str, result: ToolResult) -> None:
    """inspect_data's questions only the user can answer (03/04: 3 April or 4 March?), asked at once by
    the runner, in place; the step of the option the user picks is applied by code (clean_data), and the
    result says which table to use. Left in the result, as before, when there is no user to ask."""
    questions = [q for q in result.result.get("questions", []) if isinstance(q, dict) and q.get("options")]
    if ctx.ask is None or result.status != "ok" or not questions:
        return
    settled = []
    for q in questions:
        labels = [o["label"] if isinstance(o, dict) else str(o) for o in q["options"]]
        got = ctx.ask({"tool": "analyze_data", "question": q["question"], "options": labels, "allow_text": True,
                       "allow_skip": True})
        option = _chosen(q["options"], got.get("answer"))
        if option is None:
            settled.append(f"{q['question']} - the user didn't choose: don't guess; say in the report that the answer "
                           "depends on it.")
            continue
        if not option.get("step"):  # e.g. keep the repeated rows: nothing to change
            settled.append(f"{q['question']} The user chose '{option['label']}': nothing to clean for it; go on "
                           f"with {table}.")
            continue
        done = execute(ctx, "clean_data", {"data": table, "steps": [option["step"]]})
        trace.add("clean_data", {"data": table, "steps": [option["step"]]}, done)
        if done.status == "ok":
            table = done.result["data"]
            settled.append(f"{q['question']} The user chose '{option['label']}': {done.result['from']} was cleaned "
                           f"with it into {table}. Use {table} from now on.")
    result.result.pop("questions", None)
    result.result["user_answers"] = settled


def _chosen(options: list, said: str | None) -> dict | None:
    """The option the user's answer picks: the button's own label, or typed text that starts like one."""
    said = (said or "").strip().lower()
    if not said:
        return None
    return next((o for o in options if isinstance(o, dict) and (
        said == o["label"].lower() or said.startswith(o["label"].split("(")[0].strip().lower()))), None)


# ---------------------------------------------------------------- checks

def _check_web(result: WebResult, trace: Trace, _context: str) -> list[str]:
    problems = [f"{d} is not a link any page you read gave; give only links the tools returned"
                for d in result.downloads if not any(d in o for o in trace.outputs)]
    for s in result.sources:
        if s in trace.read_urls:
            continue
        if s in trace.found_urls:
            problems.append(f"{s} was only seen as a search snippet; read it with read_webpage or don't cite it")
        else:
            problems.append(f"{s} was never found or read; cite only pages you read")
    if not result.sources and trace.read_urls and not result.question_for_user:
        problems.append("no sources given; list the URLs of the pages you used")
    return problems


NOTHING_FOUND = re.compile(r"\b(not|no|couldn'?t|cannot|can'?t|don'?t|doesn'?t|unable|isn'?t)\b.{0,60}"
                           r"\b(find|found|mention|contain|cover|information|available|include)", re.I)


def _check_knowledge(result: SourcedResult, trace: Trace, _context: str) -> list[str]:
    problems = [f"'{s}' is not a source any tool returned; copy sources exactly as given"
                for s in result.sources if not any(s in t or t in s for t in trace.sources)]
    if (not result.sources and trace.sources and not result.question_for_user
            and not NOTHING_FOUND.search(result.answer)):
        problems.append("no sources given; list the sources (exactly as the tools gave them) your answer comes from")
    return problems


NUMBER = re.compile(r"(?<![\w.])([-+]?\d[\d,]*(?:\.\d+)?)(\s?%|\s?(?:K|M|B|bn|thousand|million|billion)\b)?")
SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9}


def numbers_in(text: str) -> list[tuple[str, float, float, bool]]:
    """(as written, value, rounding tolerance, is a percentage) for every number in the text.
    '1,200.50' -> 1200.5 (+-0.005); '1.2M' -> 1,200,000 (+-50,000); '30%' -> 30 (+-0.5)."""
    out = []
    for digits, suffix in NUMBER.findall(text):
        suffix = (suffix or "").strip()
        try:
            base = float(digits.replace(",", ""))
        except ValueError:
            continue
        decimals = len(digits.split(".")[1]) if "." in digits else 0
        scale = SCALE.get(suffix.lower(), 1.0)
        out.append((digits + suffix, base * scale, 0.5 * 10 ** -decimals * scale + 1e-9, suffix == "%"))
    return out


def ungrounded_numbers(answer: str, evidence: list[str]) -> list[str]:
    """Numbers in the answer that no tool result (or the task itself) contains, allowing for the
    rounding the answer shows and for shares written as percentages (0.3003 -> '30%'). Small whole
    numbers (up to 10) pass: 'top 5', '3 groups'."""
    pool = [v for text in evidence for _, v, _, _ in numbers_in(text)]
    missing = []
    for raw, value, tol, percent in numbers_in(answer):
        if value == int(value) and abs(value) <= 10:
            continue
        if not any(abs(v - value) <= tol or (percent and abs(v * 100 - value) <= tol) for v in pool):
            missing.append(raw)
    return missing


def _check_data(result: DataReport, trace: Trace, context: str) -> list[str]:
    """Every section written, and every number in it from a tool result (the method's SQL aside)."""
    if result.question_for_user:
        return []
    return (section_problems(DATA_SECTIONS, result.model_dump())
            + _numbers_problem(" ".join((result.data_used, result.results, result.caveats)), trace, context))


def _numbers_problem(text: str, trace: Trace, context: str) -> list[str]:
    missing = ungrounded_numbers(text, trace.outputs + [context])
    return [f"these numbers are not in any tool result: {', '.join(missing)}; copy numbers exactly from the "
            "results, or compute them with query_data (SELECT arithmetic needs no table)"] if missing else []


CLAIMS = ("purpose", "how_it_works", "inputs_outputs")  # what the file does and prints - not change examples


def _check_save(result: SaveResult, trace: Trace, context: str) -> list[str]:
    """Before the coder's file is saved: a report with every section, code that ran - or, after one
    chance to fix it, saved as it is with the report's facts saying it fails - and no number in what the
    report says the file does or prints that isn't in the code, the task or what a tool returned (a run's
    output, a file read, an image looked at). Measured on 14 real reports: those sections had no such
    number; 'How to run or change it' had one, a legitimate example ('print(fibonacci(45))'), so change
    examples and limits are not checked. A picture the runs made must have been looked at (view_image):
    in a real run the coder saved a chart unseen although the run's result said to look."""
    if result.question_for_user:
        return []
    scratch = trace.scratch
    code = result.code if result.code and result.code.strip() else (scratch.code if scratch else "")
    if not code.strip():
        return ["there is no code to save: write it with run_code first"]
    name = result.name.strip().lower()
    python = name.endswith(".py") or "." not in name
    problems = report_problems(result.model_dump(), code, python)
    drawn = scratch.images if scratch and scratch.can_look else []
    if drawn and not trace.counts.get("view_image"):  # seen: a chart saved unseen though the run said to look
        problems.append(f"you made {', '.join(drawn)} but never looked at it: check with view_image(image='{drawn[0]}', "
                        "question=...) that it shows what the task asked, then call save again")
    claims = " ".join(str(getattr(result, k) or "") for k in CLAIMS)
    missing = ungrounded_numbers(claims, trace.outputs + [code, context] + [c for c, _ in trace.python_runs])
    if missing:
        problems.append(f"the report states {', '.join(m.strip(',.;:') for m in missing)}, which is not in the code or "
                        "anything a run printed: "
                        "say only what the code does and its last run showed (run it to show more)")
    last = scratch.last if scratch else None
    if python and not result.code and last and last["code"] == code and not last["ok"]:
        error = next((ln for ln in reversed(last["errors"].splitlines()) if ln.strip()), "an error")
        problems.append(f"the last run failed ({error.strip()[:160]}): fix it with fix_code, or call save again to "
                        "keep it as it is - its report will say it fails")
    return problems


# ---------------------------------------------------------------- profiles

PROFILES = {p.name: p for p in [
    Profile("web", prompts.WEB, ("web_search", "read_webpage"), 6, WebResult, _check_web),
    Profile("knowledge", prompts.KNOWLEDGE, ("search_files", "recall", "read_file"), 6, SourcedResult, _check_knowledge),
    Profile("data", prompts.DATA,
            ("inspect_data", "clean_data", "query_data", "make_chart", "compare", "forecast", "predict",
             "find_patterns", "analyze_text", "run_code", "fix_code"), 10, DataReport, _check_data,
            finish="save_report", asks=True),
    Profile("coder", prompts.CODER, ("run_code", "fix_code", "read_file", "view_image"), 10, SaveResult, _check_save,
            finish="save", asks=True),
]}


# ---------------------------------------------------------------- the runner

def run_worker(provider: Provider, profile: Profile, task: str, ctx: ToolContext, *, user_message: str = "",
               progress: Callable[[dict], None] | None = None, file: str | None = None,
               table: str | None = None) -> dict:
    """Run one task on the worker slot; returns the checked result for the main slot. file: a stored
    file to work on - loaded into the scratch script, its report shown first; table: the table the task
    is about - its report shown first, the new one kept on it."""
    progress = progress or (lambda event: None)
    system = profile.instructions  # the same for every task of this role, so llama.cpp keeps it cached
    # A new scratch script for every task: the helper keeps nothing between tasks. A helper that can look at
    # images is pointed to view_image when a run draws one.
    ctx.scratch = Scratch(can_look="view_image" in profile.tools and ctx.look is not None)
    table = table if table and table in ctx.ws.ids("table") else None
    given = _load(ctx, file) if file else _table_report(ctx, table) if table else ""
    opening = (f"{now_line()}\n\n{context_block(ctx.ws)}\n\n" + given
               + (f"The user's message: {user_message}\n\n" if user_message else "") + f"Your task: {task}")
    messages: list[dict] = [{"role": "user", "content": opening}]
    own = profile.own_tools()
    tool_specs = [t.spec() for t in own.values()] + [_submit_spec(profile)]
    trace = Trace(existing=set(ctx.ws.ids()), scratch=ctx.scratch, table=table)
    for n in pinned(ctx.ws):  # the pinned notes in its opening are evidence too: citable like search results
        trace.sources.add(source(n))
        trace.passages.append((source(n), n["text"]))
    limit, count = prompt_limit(provider), counter(provider, system, tool_specs)  # exact, from the server
    retried, shortened = False, 0
    progress({"type": "worker_start", "worker": profile.name, "task": task, "limit": limit})
    for step in range(profile.max_steps + 1):
        last = step == profile.max_steps
        visible, shortened = shorten_tool_results(messages, limit, count, shortened)
        try:
            turn = provider.complete(system, visible, tool_specs, allow_tools=not last)
        except ProviderError as exc:
            return {"status": "failed", "worker": profile.name, "error": str(exc)}
        messages.append({"role": "assistant", "text": turn.text, "tool_calls": turn.tool_calls, "raw": turn.raw,
                         "provider": provider.name, "model": turn.model, "stop": turn.stop})
        progress({"type": "worker_turn", "worker": profile.name, "step": step, "stop": turn.stop,
                  "calls": [c["name"] for c in turn.tool_calls], "text": turn.text[:300], "usage": turn.usage,
                  "shortened": shortened})
        cut = turn.stop == "max_tokens"  # stopped by the output limit: thinking or answer unfinished
        if cut and not last:
            messages.append({"role": "user", "content": HELPER_CUT_OFF.format(finish=profile.finish)})
            progress({"type": "worker_hint", "worker": profile.name, "text": HELPER_CUT_OFF.format(finish=profile.finish)})
            continue
        if not turn.tool_calls:
            # A plain-text answer (small models often skip submit_result): the same checks apply to it.
            note = "; ".join(n for n, on in (("answered at the step limit", last),
                                             ("the reply was cut off by the length limit", cut)) if on) or None
            if not turn.text.strip():
                return _done(ctx, profile, {"answer": ""}, trace, ["the worker returned no answer"], note=note)
            written = _json_in_text(turn.text)
            outcome = _submitted(profile, written, trace, opening) if written is not None else None
            if isinstance(outcome, dict):
                result, problems = outcome["result"], outcome["problems"]
            else:
                result = _repair(profile, _from_text(profile, turn.text, trace), trace)
                if result.get("question_for_user"):  # asking is the whole result
                    problems = []
                elif profile.finish == SUBMIT:
                    problems = profile.check(profile.result_model.model_validate(result), trace, opening)
                else:  # the coder's file, the analyst's report: kept only through its finish call
                    problems = [] if retried and profile.result_model is DataReport else [
                        f"nothing was saved: finish by calling {profile.finish} with your report"]
                    if profile.result_model is DataReport:  # its plain answer is still checked, and used
                        problems += _numbers_problem(result["answer"], trace, opening)
                        if retried and not problems:
                            note = "; ".join(filter(None, [note, "no report was kept: the analyst answered in "
                                                                 "plain text"]))
            progress({"type": "worker_check", "worker": profile.name, "problems": problems,
                      "retry": bool(problems and not retried and not last)})
            if problems and not retried and not last:
                retried = True
                messages.append({"role": "user", "content": FIX_ANSWER + "; ".join(problems)})
                continue
            return _done(ctx, profile, result, trace, problems, note=note)
        for call in turn.tool_calls:
            if call["name"] == profile.finish:
                outcome = _submitted(profile, call["input"], trace, opening)
                progress({"type": "worker_check", "worker": profile.name,
                          "problems": [outcome] if isinstance(outcome, str) else outcome["problems"],
                          "retry": not (isinstance(outcome, dict) and (not outcome["problems"] or retried))})
                if isinstance(outcome, dict) and (not outcome["problems"] or retried):
                    return _done(ctx, profile, outcome["result"], trace, outcome["problems"])
                retried = retried or isinstance(outcome, dict)
                feedback = (outcome if isinstance(outcome, str) else
                            FIX_FINISH.format(finish=profile.finish) + "; ".join(outcome["problems"]))
                messages.append({"role": "tool", "tool_call_id": call["id"], "name": profile.finish,
                                 "content": feedback, "is_error": True})
                continue
            args = call["input"]
            if (call["name"] == "read_webpage" and isinstance(args, dict) and not args.get("find")
                    and trace.last_query):
                args = {**args, "find": trace.last_query}  # only used when the page is too long to show whole
            progress({"type": "worker_tool", "worker": profile.name, "name": call["name"], "input": args})
            if call["name"] not in own:
                result = ToolResult("invalid_arguments", {"error": {"message": f"{call['name']} is not one of your tools."}})
                trace.not_run += 1
            elif call["name"] == "ask_user" and trace.counts.get("ask_user", 0) >= MAX_ASKS:
                result = ToolResult("invalid_arguments", {"error": {"message": (
                    f"You have asked the user {MAX_ASKS} times in this task: decide the rest yourself, with the "
                    "recommended choices, and say so in the report.")}})
                trace.not_run += 1
            elif (profile.name == "data" and isinstance(args, dict)
                  and (table := _uninspected(ctx, trace, call["name"], args))):
                looked = execute(ctx, "inspect_data", {"data": table})
                _settle_questions(ctx, trace, table, looked)
                trace.add("inspect_data", {"data": table}, looked)
                trace.not_run += 1
                result = ToolResult("invalid_arguments", {
                    "error": {"message": f"Not run: {table} had not been inspected, so inspect_data ran first (below). "
                                         "Apply its suggested steps if there are any, then make your call on the "
                                         "right table."},
                    "inspect_data": looked.result})
            elif isinstance(args, dict) and (question := _guessed(trace, call["name"], args, opening)):
                trace.not_run += 1
                result = ToolResult("invalid_arguments", {"error": {"message": (
                    f"Not run: this settles a question only the user can answer ({question}). Don't guess: put it "
                    f"in {profile.finish} as question_for_user with its options.")}})
            elif isinstance(args, dict) and (repeat := _repeat(trace, call["name"], args)):
                result = repeat
                trace.not_run += 1
            else:
                result = execute(ctx, call["name"], args, own)
                if call["name"] == "inspect_data" and isinstance(args, dict):
                    _settle_questions(ctx, trace, ctx.ws.find(str(args.get("data", ""))) or "", result)
                if isinstance(args, dict):
                    trace.add(call["name"], args, result)
                else:
                    trace.not_run += 1  # arguments that were not JSON
                if profile.result_model is SaveResult and call["name"] == "read_file":
                    _adopt(ctx, result)
            content = result.for_model()
            messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"],
                             "content": content, "is_error": result.status not in ("ok", "partial")})
            progress({"type": "worker_result", "worker": profile.name, "name": call["name"],
                      "status": result.status, "chars": len(content), "content": content})
        left = profile.max_steps - (step + 1)  # model calls that may still use tools
        if left in STEPS_LEFT and messages[-1]["role"] == "tool":
            hint = STEPS_LEFT[left].format(finish=profile.finish)
            messages[-1]["content"] += "\n\n" + hint
            progress({"type": "worker_hint", "worker": profile.name, "text": hint})
    return _done(ctx, profile, {"answer": ""}, trace, ["the worker ran out of steps without an answer"])


def _adopt(ctx: ToolContext, result: ToolResult) -> None:
    """The coder works on one file: a code file it reads while its scratch script is empty becomes the
    scratch script (measured: it read file_2, then called fix_code on an empty scratch). fix_code then
    changes it by the line numbers read_file showed."""
    from app.tools.read_file import CODE
    res = result.result
    if (result.status != "ok" or ctx.scratch.code or not str(res.get("lines", "")).startswith("1-")
            or PurePath(res.get("name", "")).suffix.lower() not in CODE):
        return
    ctx.scratch.code = ctx.ws.path(res["file"]).read_text("utf-8", errors="replace")
    ctx.scratch.name, ctx.scratch.item = res["name"], res["file"]
    res["note"] = ((res["note"] + " ") if res.get("note") else "") + (
        "This file is now your scratch script: change only what the task asks, with fix_code (the line numbers "
        "above), and save it under its name.")


def _table_report(ctx: ToolContext, table: str) -> str:
    """The table's latest report - an earlier task's decisions and the cleaned table it made - first."""
    report = ctx.ws.report(table)
    if not report:
        return ""
    return (f"The table's latest report (from an earlier task on {table}) - use its decisions and the cleaned table "
            f"it names instead of redoing them:\n{report}\n\n")


def _load(ctx: ToolContext, file: str) -> str:
    """A stored file to work on: into the scratch script, and its report into the task's opening."""
    item = ctx.ws.resolve(file, ("file",), field="file")
    e = ctx.ws.entry(item)
    ctx.scratch.code = ctx.ws.path(item).read_text("utf-8", errors="replace")
    ctx.scratch.name, ctx.scratch.item = e["name"], item
    report = ctx.ws.report(item)
    return (f"The file to work on: {e['name']} ({item}), {len(ctx.scratch.code.splitlines())} lines, already in "
            "your scratch script: change only what the task asks, with fix_code, and save it under the same name.\n"
            + (f"Its latest report:\n{report}\n\n" if report else "It has no report yet.\n\n"))


def _submitted(profile: Profile, raw, trace: Trace, context: str) -> dict | str:
    """{'result', 'problems'} for a well-formed submission, or an error text to send back."""
    if not isinstance(raw, dict):
        return f"{profile.finish} needs JSON arguments."
    if raw.get("question_for_user"):  # asking is the whole result; small models leave the rest out
        raw = {**{k: "" for k, f in profile.result_model.model_fields.items() if f.is_required()}, **raw}
    try:
        result = profile.result_model.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        return f"{profile.finish}: {'.'.join(map(str, first['loc']))}: {first['msg']}"
    fixed = _repair(profile, result.model_dump(exclude_none=True), trace)
    return {"result": fixed, "problems": profile.check(profile.result_model.model_validate(fixed), trace, context)}


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
        found = [u for u in dict.fromkeys(links) if is_file_link(u) and any(u in o for o in trace.outputs)]
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


FINISH_TAG = re.compile(r"<(?:submit_result|save_report|save)\b(.*)", re.S)
TAG_VALUE = re.compile(r'\b(?:answer|results)\s*=\s*"(.*?)"\s*(?:/?>|$|\w+\s*=)', re.S)


def _without_finish_markup(text: str) -> str:
    """A finish call written into the text as markup - '<submit_result answer="...">' after the same
    answer in prose (measured: it reached the user as it was) - comes off; with no prose before it, the
    answer inside it is the answer."""
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


def _done(ctx: ToolContext, profile: Profile, result: dict, trace: Trace, problems: list[str],
          note: str | None = None) -> dict:
    """The result for the main slot; the cited web sources go to the UI as cards. A note is for
    information (e.g. the step limit was reached); warnings are problems with the answer itself."""
    cited = [trace.cards[u] for u in result.get("sources", []) if u in trace.cards]
    if cited:
        ctx.emit({"tool": f"{profile.name}_worker", "type": "sources", "items": cited})
    if profile.result_model is SaveResult and not result.get("question_for_user"):
        if not result.get("name") and ctx.scratch.last and ctx.scratch.last["ok"]:
            result, problems = _unsaved(ctx, result, problems)
        if result.get("name"):
            result, problems = _save(ctx, result, problems)
    if profile.result_model is DataReport and not result.get("question_for_user") and result.get("results"):
        result = _keep_report(ctx, result, trace)
    out = _finish(profile, result, trace, problems, note)
    made = [i for i in ctx.ws.ids() if i not in trace.existing]
    if made:
        out["made"] = made
    if note:
        out["note"] = note
    return out


# Wider than NOTHING_FOUND (which decides whether sources are required): a false match here only
# adds the trail, about 30 tokens.
INCOMPLETE = re.compile(r"\b(unable to|could not|couldn'?t|cannot|can'?t|(?:was|were) not able|wasn'?t able|"
                        r"failed to|not (?:found|available|covered|mentioned|possible|confirmed)|"
                        r"no (?:information|data|results?|mention))\b", re.I)


def _trail(trace: Trace) -> str:
    """What the worker did, from its trace - facts the main slot can act on (retry another way,
    ask the user, or say what couldn't be done), never the model's own account."""
    ran = ", ".join(f"{name} ({n})" if n > 1 else name for name, n in trace.counts.items())
    parts = [f"Ran {ran}." if ran else "Ran no tools."]
    if trace.failures:
        parts.append("Failed: " + "; ".join(trace.failures[-3:]) + ".")
    if trace.not_run:
        parts.append(f"{trace.not_run} call(s) not run (repeated or not allowed).")
    return " ".join(parts)


def _finish(profile: Profile, result: dict, trace: Trace, problems: list[str], note: str | None = None) -> dict:
    out = {"status": "ok", "worker": profile.name, **result}
    if out.get("suggestion"):
        out["suggestion"] = out["suggestion"][:300]
    if not out.get("question_for_user"):
        out.pop("options", None)
    answer = out.get("answer", "")
    if (problems or note or out.get("question_for_user") or out.get("suggestion") or not answer.strip()
            or INCOMPLETE.search(answer)):
        out["trail"] = _trail(trace)  # the briefing: only when the answer is incomplete or flagged
    if out.get("question_for_user") and trace.questions:
        # The options' cleaning steps, for the main slot to apply the user's choice by code. Taken off
        # the result before the model sees it.
        q = trace.questions[-1]
        steps = [o for o in q.get("options", []) if isinstance(o, dict) and o.get("step")]
        if steps and q.get("table"):
            out["choices"] = {"table": q["table"], "options": steps}
    if out.get("downloads"):
        out["download_with"] = "files(mode='download', link=...) for each one wanted: the user approves it first"
    elif "downloads" in out:
        out.pop("downloads")
    if problems:
        out["warnings"] = problems
    return out


def _keep_report(ctx: ToolContext, result: dict, trace: Trace) -> dict:
    """The analyst's report, assembled with the app's facts and kept on the table the task was about (or
    the first one it inspected); main gets the results as its answer, and the report."""
    from app.tools.files import index_report
    target = trace.table or next((t for t in trace.inspected if t in trace.existing), None)
    made = [i for i in ctx.ws.ids("table") if i not in trace.existing]
    facts = ([f"Table: {ctx.ws.entry(target)['name']} ({target}), report saved {datetime.now():%Y-%m-%d %H:%M}"]
             if target else [])
    if made:
        facts.append("Tables made: " + ", ".join(
            f"{i} ({ctx.ws.entry(i)['name']}" + (f", from {ctx.ws.entry(i)['parent']})" if ctx.ws.entry(i).get("parent")
                                                  else ")") for i in made))
    charts = trace.counts.get("make_chart", 0) + trace.counts.get("forecast", 0)
    if charts:
        facts.append(f"Charts shown: {charts}")
    facts.append(_trail(trace))
    report = assemble(DATA_SECTIONS, result, facts)
    if target:
        ctx.ws.set_report(target, report)
        index_report(ctx.ws, ctx.index, target)
        ctx.emit({"tool": "data_worker", "type": "report", "table": target, "name": ctx.ws.entry(target)["name"],
                  "report": report})
    brief = {k: result[k] for k in BRIEF if result.get(k)}
    return {"answer": result["results"], "report": report, **({"table": target} if target else {}), **brief}


FILE_NAME = re.compile(r"[\w.-]+\.py\b")


def _unsaved(ctx: ToolContext, result: dict, problems: list[str]) -> tuple[dict, list[str]]:
    """The coder ended without save although its script ran (measured: it wrote the "save" in prose,
    twice): the app keeps the script that ran - work is never lost - with a report that says the
    coder didn't write it. Still a warning: the contract was not kept."""
    named = FILE_NAME.findall(result.get("answer", ""))
    missing = "Not written: the coder didn't describe this (the app saved its last working script)."
    fields = {"name": ctx.scratch.name or (named[0] if named else "script.py"),
              "purpose": "A script the coder ran but didn't save itself", "how_it_works": missing,
              "inputs_outputs": missing, "how_to_run": missing, "limits": missing}
    ctx.scratch.code = ctx.scratch.last["code"]
    note = "the coder didn't call save: the app saved its last working script, with a report it didn't write"
    return {**result, **fields}, [p for p in problems if not p.startswith("nothing was saved")] + [note]


def _save(ctx: ToolContext, result: dict, problems: list[str]) -> tuple[dict, list[str]]:
    """The coder's file kept with its report (app/tools/scratch.py save); main gets the report, never the
    code, and the user the file's card. Saving over the user's own file waits for their approval."""
    sections = {k: result.get(k, "") for k, _ in SECTIONS}
    saved = save_file(ctx.ws, ctx.index, ctx.scratch, result["name"], sections, result.get("code"))
    brief = {k: result[k] for k in BRIEF if result.get(k)}
    if saved.status != "ok":
        return {"answer": "", **brief}, problems + [saved.result["error"]["message"]]
    if (saved.artifact or {}).get("type") == "question":  # the user's own file: they decide
        return {"answer": "", "approval_card": saved.artifact, **brief}, problems
    ctx.emit({"tool": "coder_worker", **saved.artifact})
    return {"file": saved.result["file"], "name": saved.result["name"], "report": saved.result["report"], **brief}, problems


def _submit_spec(profile: Profile) -> dict:
    schema = profile.result_model.model_json_schema()
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    # The answer first, the briefing fields last: a small model fills them in the order shown.
    schema["properties"] = dict(sorted(schema["properties"].items(), key=lambda kv: kv[0] in BRIEF))
    typed_optionals(schema)
    description = ("Keep your file in the workspace with its report (call once, at the end)." if profile.finish == "save"
                   else "Hand in your final result (call once, at the end).")
    return {"name": profile.finish, "description": description, "input_schema": schema}


# ---------------------------------------------------------------- the main slot's delegate tools

DELEGATES = {
    "research_web": ("web", TaskIn, "Research a question on the web: a helper searches, reads pages and returns "
                     "a cited answer, dated for 'latest' questions, and the links of files the task asks for (a "
                     "dataset, a script) for you to download. Use it for what is current or what you don't know - news, releases, "
                     "prices, people, events - and to read a link the user gives."),
    "ask_knowledge": ("knowledge", QuestionIn, "Answer from the user's own documents, saved notes and earlier "
                      "conversations, with sources. Use it when the user asks about their documents or notes, what was "
                      "said or decided before, or for a summary of an attached document. The user's own files "
                      "come first: when a document or note in the workspace may cover the question (judge by its "
                      "name and title), ask here before research_web."),
    "analyze_data": ("data", DataTaskIn, "Give a data task to the data analyst. Use it for any question about the "
                     "numbers in the user's tables: cleaning, "
                     "exact numbers, charts, whether a difference or A/B result is real, trends and forecasts, what "
                     "predicts or drives a column, customer segments, unusual days, items bought together, themes "
                     "in text."),
    "write_code": ("coder", CodeTaskIn, "Have the coder write one file and keep it - a Python script it runs and "
                   "fixes, or a file in any language - or change an existing one (give its name as file). For when "
                   "the user asks for code or the work needs a program (loops, simulations, files), not for working "
                   "out a formula. You get its report, never the code: answer from the report and never paste or "
                   "retype code; the user sees the file in the chat."),
}


def delegate_tools(worker: Provider, user_message: Callable[[], str] = lambda: "",
                   progress: Callable[[dict], None] | None = None) -> dict[str, Tool]:
    """The delegate tools for the main slot, bound to the worker-slot provider."""
    def make(name: str, profile_name: str, model, description: str) -> Tool:
        def handler(ctx: ToolContext, args) -> ToolResult:
            task = getattr(args, "task", None) or args.question
            if getattr(args, "data", None):
                task += f"\n(The table to use: {args.data}.)"
            out = run_worker(worker, PROFILES[profile_name], task, ctx, user_message=user_message(),
                             progress=progress, file=getattr(args, "file", None), table=getattr(args, "data", None))
            status = out.pop("status")
            if status != "ok":
                return ToolResult("failed", {"error": {"message": f"The {profile_name} helper failed: {out.get('error')}"}})
            return ToolResult("partial" if out.get("warnings") else "ok", out)
        return Tool(name, description, model, handler)
    return {name: make(name, *spec) for name, spec in DELEGATES.items()}


MAIN_DIRECT = ("get_weather", "generate_image", "view_image", "draw_diagram", "calculate", "save_note", "ask_user",
               "files")


def main_tools(worker: Provider, ctx: ToolContext, **kwargs) -> dict[str, Tool]:
    """What the main slot sees: quick tools plus delegates (analyze_data only when there is a table)."""
    tools = {n: TOOLS[n] for n in MAIN_DIRECT}
    delegates = delegate_tools(worker, **kwargs)
    if not ctx.ws.ids("table"):
        delegates.pop("analyze_data")
    return {**tools, **delegates}
