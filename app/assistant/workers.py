"""Worker sub-agents: the second model slot doing one focused task with a narrow toolset.

The main slot delegates with research_web / ask_knowledge / analyze_data / write_code. Each runs a
worker profile - its own short instructions, only its own tools, a step limit - on the worker
slot, and returns a short structured result instead of raw pages or tables:

    web        web_search, read_webpage                      -> answer + URLs it read
    knowledge  search_files, recall, read_file                -> answer + the sources it was given
    data       the 8 data tools, calculate, run_python        -> answer (+ a question for the user)
    coder      run_python, read_file, calculate               -> answer + final code

A worker finishes by calling submit_result; its arguments are checked by code before the main
slot sees them: web sources must be pages the worker read (or at least found), knowledge sources
must be ones a tool returned, every number in a data answer must appear in a tool result, and
the coder's final code must have run without error. On a failed check the worker gets one
chance to fix it; after that the result goes back with the problem stated as a warning.
Charts, tables and images from worker tools go straight to the UI, not through the summary.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent.providers.base import Provider, ProviderError
from app.tools.core import ToolResult

from .context import context_block, history_budget, shorten_tool_results
from .registry import TOOLS, DataTaskIn, QuestionIn, TaskIn, Tool, ToolContext, execute, specs

SUBMIT = "submit_result"


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourcedResult(_Out):
    answer: str = Field(description="the answer to the task, only from what the tools returned")
    sources: list[str] = Field(default_factory=list, description="the sources used, exactly as the tools gave them")


class DataResult(_Out):
    answer: str = Field(description="the answer; every number copied from a tool result")
    question_for_user: str | None = Field(None, description="if a tool returned a question only the user can "
                                          "answer, put it here instead of guessing")
    options: list[str] | None = None


class CodeResult(_Out):
    answer: str = Field(description="what the code does and what it printed")
    code: str | None = Field(None, description="the final working script")


@dataclass
class Trace:
    """What a worker's tools returned - the evidence its result is checked against."""
    outputs: list[str] = field(default_factory=list)
    read_urls: set[str] = field(default_factory=set)
    found_urls: set[str] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)
    python_runs: list[tuple[str, bool]] = field(default_factory=list)  # (code, ran cleanly)
    python_outputs: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    cards: dict[str, dict] = field(default_factory=dict)  # url -> what the UI shows for a web source
    passages: list[tuple[str, str]] = field(default_factory=list)  # (source, text) the knowledge tools returned
    calls: dict[str, ToolResult] = field(default_factory=dict)  # each distinct call (name + input) -> its result
    tried_urls: set[str] = field(default_factory=set)  # every page read_webpage was asked for, read or not

    def add(self, name: str, args: dict, result: ToolResult) -> None:
        self.outputs.append(json.dumps(result.result, ensure_ascii=False, default=str))
        self.calls[_call_key(name, args)] = result
        res = result.result
        if name == "read_webpage":
            self.tried_urls.add(args.get("url", ""))
        if name == "read_webpage" and result.status == "ok":
            self.read_urls.add(res["url"])
            self.read_urls.add(args.get("url", ""))
            card = {k: v for k, v in (result.artifact or {}).items() if k != "type"}
            self.cards[res["url"]] = self.cards[args.get("url", "")] = card
        if name == "web_search":
            self.found_urls.update(r["url"] for r in res.get("results", []))
        for p in res.get("passages", []) + res.get("turns", []):
            source = p.get("source") or f"conversation {p.get('conversation')}"
            self.sources.add(source)
            self.passages.append((source, p.get("text", "")))
        if name == "read_file" and result.status == "ok":
            self.sources.update({res["file"], res["name"]})
            self.passages.append((res["name"], res.get("text", "")))
        if name == "run_python":
            self.python_runs.append((args.get("code", ""), result.status == "ok"))
            self.python_outputs.append(res.get("output", ""))
        for key in ("data", "saved_as", "model", "image"):
            if isinstance(res.get(key), str) and re.fullmatch(r"(data|model|image|file|note)_\d+", res[key]):
                self.artifacts.append(res[key])


def _call_key(name: str, args: dict) -> str:
    """Two calls are the same when their inputs only differ in case or spacing of words and addresses."""
    tidy = {k: " ".join(v.split()).lower() if k in ("query", "url", "find") and isinstance(v, str) else v
            for k, v in args.items()}
    return name + json.dumps(tidy, sort_keys=True, ensure_ascii=False, default=str)


def _repeat(trace: Trace, name: str, args: dict) -> ToolResult | None:
    """A call the worker already made with the same input is not run again - a small model re-runs
    a search after a failed read instead of reading another result - and it is told what to do
    instead. run_python is exempt: the same code can give a new result once files have changed."""
    earlier = trace.calls.get(_call_key(name, args))
    if earlier is None or name == "run_python":
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


# ---------------------------------------------------------------- checks

def _check_web(result: SourcedResult, trace: Trace, _context: str) -> list[str]:
    problems = []
    for s in result.sources:
        if s in trace.read_urls:
            continue
        if s in trace.found_urls:
            problems.append(f"{s} was only seen as a search snippet; read it with read_webpage or don't cite it")
        else:
            problems.append(f"{s} was never found or read; cite only pages you read")
    if not result.sources and trace.read_urls:
        problems.append("no sources given; list the URLs of the pages you used")
    return problems


NOTHING_FOUND = re.compile(r"\b(not|no|couldn'?t|cannot|can'?t|don'?t|doesn'?t|unable|isn'?t)\b.{0,60}"
                           r"\b(find|found|mention|contain|cover|information|available|include)", re.I)


def _check_knowledge(result: SourcedResult, trace: Trace, _context: str) -> list[str]:
    problems = [f"'{s}' is not a source any tool returned; copy sources exactly as given"
                for s in result.sources if not any(s in t or t in s for t in trace.sources)]
    if not result.sources and trace.sources and not NOTHING_FOUND.search(result.answer):
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


def _check_data(result: DataResult, trace: Trace, context: str) -> list[str]:
    missing = ungrounded_numbers(result.answer, trace.outputs + [context])
    return [f"these numbers are not in any tool result: {', '.join(missing)}; copy numbers exactly from the "
            "results, or compute them with query_data or calculate"] if missing else []


def _check_code(result: CodeResult, trace: Trace, _context: str) -> list[str]:
    if not trace.python_runs:
        return ["the code was never run; run it with run_python first"]
    if result.code and not any(ok and _same_code(code, result.code) for code, ok in trace.python_runs):
        return ["this final code never ran without error; run it and fix it first"]
    return []


def _same_code(a: str, b: str) -> bool:
    """The same program: equal syntax trees, so comments and spacing don't count. Code that doesn't
    parse is compared as text."""
    try:
        return ast.dump(ast.parse(a)) == ast.dump(ast.parse(b))
    except SyntaxError:
        return " ".join(a.split()) == " ".join(b.split())


# ---------------------------------------------------------------- profiles

FINISH = ("When you are done, call submit_result exactly once. If the tools can't answer the task, say so "
          "in the answer - never fill gaps from memory.")

PROFILES = {p.name: p for p in [
    Profile("web", "You are a web researcher. If the task gives a link, read that page with read_webpage first. "
            "Find pages with web_search, then read the best one to three with "
            "read_webpage, preferring official sources (the project's or organisation's own site, docs or "
            "repository) over third-party articles. Answer the task only from the pages you read, and list their "
            "URLs as sources. Snippets are not enough to quote. An example in a page is not a default or a rule; "
            "if pages disagree, say so. When a page is cut and the part you need is not shown, read the same page "
            "again with find='words to look for' instead of searching again. " + FINISH,
            ("web_search", "read_webpage"), 6, SourcedResult, _check_web),
    Profile("knowledge", "You answer from the user's own documents, saved notes and earlier conversations. Use "
            "search_files for documents and notes, recall for earlier chats, and read_file to read a whole file "
            "(for summaries). Answer only from what the tools return and copy each source exactly as given. " + FINISH,
            ("search_files", "recall", "read_file"), 6, SourcedResult, _check_knowledge),
    Profile("data", "You are a data analyst working on the user's tables (listed in the workspace). Start with "
            "inspect_data on the table. If it suggests cleaning steps, apply them all with one clean_data call "
            "and use the cleaned table from then on. If it returns a question, don't guess: put the question in "
            "submit_result as question_for_user with its options. When the task defines what to predict with a "
            "condition (e.g. 'bought again', 'amount > 100'), first make that column with query_data (CASE WHEN "
            "... THEN 'yes' ELSE 'no' END, save=true), then train on the new table and leave out the column it "
            "was made from. Every number in your answer must be copied from a tool result: use query_data or "
            "calculate for any arithmetic. A chart over time is a line chart. Charts and tables you make are "
            "shown to the user automatically; just mention them. Use run_python only for what no other tool "
            "does. " + FINISH,
            ("inspect_data", "clean_data", "query_data", "make_chart", "stats_test", "train_model", "predict",
             "forecast", "calculate", "run_python"), 10, DataResult, _check_data),
    Profile("coder", "You write and run Python for the user. Write a complete script, run it with run_python, "
            "read the errors and fix them (at most 3 attempts). Use read_file to read code or text files the "
            "user attached. Return the final working code and what it printed. " + FINISH,
            ("run_python", "read_file", "calculate"), 8, CodeResult, _check_code),
]}


# ---------------------------------------------------------------- the runner

def run_worker(provider: Provider, profile: Profile, task: str, ctx: ToolContext, *, user_message: str = "",
               progress: Callable[[dict], None] | None = None) -> dict:
    """Run one task on the worker slot; returns the checked result for the main slot."""
    progress = progress or (lambda event: None)
    system = f"{profile.instructions}\n\n{context_block(ctx.ws)}"
    opening = (f"The user's message: {user_message}\n\n" if user_message else "") + f"Your task: {task}"
    messages: list[dict] = [{"role": "user", "content": opening}]
    tool_specs = specs(list(profile.tools)) + [_submit_spec(profile)]
    trace = Trace()
    budget = history_budget(system, tool_specs)
    retried = False
    for step in range(profile.max_steps + 1):
        last = step == profile.max_steps
        try:
            turn = provider.complete(system, shorten_tool_results(messages, budget), tool_specs,
                                     allow_tools=not last)
        except ProviderError as exc:
            return {"status": "failed", "worker": profile.name, "error": str(exc)}
        messages.append({"role": "assistant", "text": turn.text, "tool_calls": turn.tool_calls, "raw": turn.raw,
                         "provider": provider.name, "model": turn.model, "stop": turn.stop})
        if not turn.tool_calls:
            # A plain-text answer (small models often skip submit_result): the same checks apply to it.
            if not turn.text.strip():
                return _done(ctx, profile, {"answer": ""}, trace, ["the worker returned no answer"])
            written = _json_in_text(turn.text)
            if written is not None and isinstance(_submitted(profile, written, trace, opening), dict):
                result = _attribute(profile, profile.result_model.model_validate(written).model_dump(exclude_none=True),
                                    trace)
            else:
                result = _attribute(profile, _from_text(profile, turn.text, trace), trace)
            problems = profile.check(profile.result_model.model_validate(result), trace, opening)
            if problems and not retried and not last:
                retried = True
                messages.append({"role": "user", "content": "Fix this and answer again: " + "; ".join(problems)})
                continue
            return _done(ctx, profile, result, trace, problems,
                         note="answered at the step limit" if last else None)
        for call in turn.tool_calls:
            if call["name"] == SUBMIT:
                outcome = _submitted(profile, call["input"], trace, opening)
                if isinstance(outcome, dict) and (not outcome["problems"] or retried):
                    return _done(ctx, profile, outcome["result"], trace, outcome["problems"])
                retried = retried or isinstance(outcome, dict)
                feedback = (outcome if isinstance(outcome, str) else
                            "Fix these and call submit_result again: " + "; ".join(outcome["problems"]))
                messages.append({"role": "tool", "tool_call_id": call["id"], "name": SUBMIT,
                                 "content": feedback, "is_error": True})
                continue
            progress({"type": "worker_tool", "worker": profile.name, "name": call["name"], "input": call["input"]})
            if call["name"] not in profile.tools:
                result = ToolResult("invalid_arguments", {"error": {"message": f"{call['name']} is not one of your tools."}})
            elif isinstance(call["input"], dict) and (repeat := _repeat(trace, call["name"], call["input"])):
                result = repeat
            else:
                result = execute(ctx, call["name"], call["input"])
                if isinstance(call["input"], dict):
                    trace.add(call["name"], call["input"], result)
            messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"],
                             "content": result.for_model(), "is_error": result.status not in ("ok", "partial")})
    return _done(ctx, profile, {"answer": ""}, trace, ["the worker ran out of steps without an answer"])


def _submitted(profile: Profile, raw, trace: Trace, context: str) -> dict | str:
    """{'result', 'problems'} for a well-formed submission, or an error text to send back."""
    if not isinstance(raw, dict):
        return "submit_result needs JSON arguments."
    try:
        result = profile.result_model.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        return f"submit_result: {'.'.join(map(str, first['loc']))}: {first['msg']}"
    fixed = _attribute(profile, result.model_dump(exclude_none=True), trace)
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


def _attribute(profile: Profile, result: dict, trace: Trace) -> dict:
    """Knowledge answers: drop 'sources' no tool returned (e.g. a tool's name); if none are left,
    credit the passages that contain the answer - most of its words appear in them."""
    if profile.name != "knowledge":
        return result
    given = result.get("sources", [])
    real = [s for s in given if any(s in t or t in s for t in trace.sources)]
    if not real and not NOTHING_FOUND.search(result.get("answer", "")):
        words = {w for w in re.findall(r"\w+", result.get("answer", "").lower()) if len(w) > 2}
        scored = sorted(((len(words & set(re.findall(r"\w+", text.lower()))) / len(words), source)
                         for source, text in trace.passages) if words else [], reverse=True)
        real = [source for score, source in scored[:2] if score >= 0.5]
        if not real:
            return result  # nothing supports the answer: keep what was given, so the check can flag it
    return {**result, "sources": list(dict.fromkeys(real))}


URL = re.compile(r"https?://[^\s)\]>\"']+")
CODE_BLOCK = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.S)


def _source_keys(source: str) -> list[str]:
    """Distinctive parts of a source label to recognise it in a plain-text answer: the file name,
    the note title, the conversation id."""
    keys = [source, source.split(",")[0].split(" - ")[0].strip()]
    keys += re.findall(r"'([^']+)'", source) + re.findall(r"conversation (\S+)", source)
    return [k for k in keys if len(k) >= 4]


def _from_text(profile: Profile, text: str, trace: Trace) -> dict:
    """A plain-text answer in the shape of the profile's result, so it can be checked the same way."""
    text = text.strip()
    if profile.result_model is SourcedResult:
        seen = trace.read_urls | trace.found_urls  # an address quoted from a page is not a source
        urls = [u for u in (u.rstrip(".,;:`'\"") for u in URL.findall(text)) if u in seen]
        named = [s for s in sorted(trace.sources) if any(k and k in text for k in _source_keys(s))]
        return {"answer": text, "sources": list(dict.fromkeys(urls + named))}
    if profile.result_model is CodeResult:
        blocks = CODE_BLOCK.findall(text)
        return {"answer": text, **({"code": blocks[-1].strip()} if blocks else {})}
    return {"answer": text}


def _done(ctx: ToolContext, profile: Profile, result: dict, trace: Trace, problems: list[str],
          note: str | None = None) -> dict:
    """The result for the main slot; the cited web sources go to the UI as cards. A note is for
    information (e.g. the step limit was reached); warnings are problems with the answer itself."""
    cited = [trace.cards[u] for u in result.get("sources", []) if u in trace.cards]
    if cited:
        ctx.emit({"tool": f"{profile.name}_worker", "type": "sources", "items": cited})
    out = _finish(profile, result, trace, problems)
    if note:
        out["note"] = note
    return out


def _finish(profile: Profile, result: dict, trace: Trace, problems: list[str]) -> dict:
    out = {"status": "ok", "worker": profile.name, **result}
    if profile.result_model is CodeResult and trace.python_runs:
        # What the final working code really printed - not the model's account of it.
        final = result.get("code")
        runs = [(c, okay, o) for (c, okay), o in zip(trace.python_runs, trace.python_outputs) if okay]
        match = [o for c, _, o in runs if final and _same_code(c, final)] or [o for _, _, o in runs]
        if match:
            out["output"] = match[-1][-2000:]
    if trace.artifacts:
        out["made"] = list(dict.fromkeys(trace.artifacts))
    if problems:
        out["warnings"] = problems
    return out


def _submit_spec(profile: Profile) -> dict:
    schema = profile.result_model.model_json_schema()
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    return {"name": SUBMIT, "description": "Hand in your final result (call once, at the end).", "input_schema": schema}


# ---------------------------------------------------------------- the main slot's delegate tools

DELEGATES = {
    "research_web": ("web", TaskIn, "Research a question on the web: a helper searches, reads pages and returns "
                     "a cited answer. Use it for anything current or not in your own knowledge."),
    "ask_knowledge": ("knowledge", QuestionIn, "Answer from the user's own documents, saved notes and earlier "
                      "conversations, with sources. Also for summarising an attached document."),
    "analyze_data": ("data", DataTaskIn, "Give a data task to the data analyst: cleaning, exact numbers, charts, "
                     "statistical tests, prediction models, forecasts on the user's tables."),
    "write_code": ("coder", TaskIn, "Have the coder write, run and fix a Python script (file conversions, "
                   "calculations, text processing, checking code)."),
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
                             progress=progress)
            status = out.pop("status")
            if status != "ok":
                return ToolResult("failed", {"error": {"message": f"The {profile_name} helper failed: {out.get('error')}"}})
            return ToolResult("partial" if out.get("warnings") else "ok", out)
        return Tool(name, description, model, handler)
    return {name: make(name, *spec) for name, spec in DELEGATES.items()}


MAIN_DIRECT = ("get_weather", "generate_image", "draw_diagram", "calculate", "save_note", "ask_user")


def main_tools(worker: Provider, ctx: ToolContext, **kwargs) -> dict[str, Tool]:
    """What the main slot sees: quick tools plus delegates (analyze_data only when there is a table)."""
    tools = {n: TOOLS[n] for n in MAIN_DIRECT}
    delegates = delegate_tools(worker, **kwargs)
    if not ctx.ws.ids("table"):
        delegates.pop("analyze_data")
    return {**tools, **delegates}
