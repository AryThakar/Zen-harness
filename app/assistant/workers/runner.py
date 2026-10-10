"""One helper task: the opening message, the step loop (guards, tools, pictures, the steps left), save_code and
the finish call with their checks, and the result main gets - with a kept program and a table's report.

A helper acts or finishes: every step requires a tool call (llama-server's tool_choice 'required', measured with
Gemma 4 on the 26B, thinking on and off) and the last step offers only its finish. Before 2026-10-10 a helper
could answer in prose, and the runner read results out of it (JSON in text, finish calls written as markup,
sources named in passing) and repaired them - each repair added after one failure. Now nothing is read out of
prose and nothing is repaired: a check that fails names the problem, and the helper fixes it."""

from __future__ import annotations

import base64
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import PurePath

from pydantic import ValidationError

from app.providers.base import Provider, ProviderError
from app.tools.approval import carry_out
from app.tools.core import ToolError, ToolResult, ok
from app.tools.filetypes import is_code
from app.tools.notes import pinned, source
from app.tools.reports import DATA_SECTIONS, assemble
from app.tools.scratch import SECTIONS, Scratch
from app.tools.scratch import save as save_file
from app.tools.vision import prepare
from app.tools.workspace import ITEM_REF

from ..context import IMAGES_SHOWN, context_block, counter, fewer_images, now_line, prompt_limit, shorten_tool_results
from ..prompts import FIX_FINISH, HELPER_CUT_OFF, NO_CALL, PICTURES, STEPS_LEFT
from ..registry import ToolContext, execute, split_confirm, typed_optionals
from .checks import save_problems
from .contracts import (BRIEF, DELEGATE_OF, MAX_ASKS, SAVE_CODE, SAVE_CODE_DESCRIPTION, Profile, SaveCodeIn,
                        WorkResult)
from .evidence import Trace, _guessed, _repeat, _uninspected


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
        got = ctx.ask({"tool": DELEGATE_OF.get("engineer", "analyze_or_code"), "question": q["question"],
                       "options": labels, "allow_text": True, "allow_skip": True})
        option = _chosen(q["options"], got.get("answer"))
        if option is None:
            settled.append(f"{q['question']} - the user didn't choose: don't guess; say in your answer that it "
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


def run_worker(provider: Provider, profile: Profile, task: str, ctx: ToolContext, *,
               progress: Callable[[dict], None] | None = None, file: str | None = None,
               table: str | None = None) -> dict:
    """Run one task on the worker slot; returns the checked result for the main slot. file: the program to keep
    or change (checked at main's call: program_problem) - an existing one is loaded into the scratch script with
    its report, a new name is the one to keep it under; table: the table the task is about - its report shown
    first, the new one kept on it.

    The opening carries main's task, not the user's message: a helper doesn't see the chat (the contract), and
    given the user's words it did the parts main had given to others - measured on the 26B, the web helper asked
    for an invoice total that was the analyst's part, and the analyst built the slide deck (one slide, in a
    script) that main meant to write from Markdown with the chart."""
    progress = progress or (lambda event: None)
    system = profile.instructions  # the same for every task of this role, so llama.cpp keeps it cached
    # A new scratch script for every task: the helper keeps nothing between tasks.
    ctx.scratch = Scratch()
    sees = profile.sees_images and bool(getattr(provider, "sees_images", lambda: False)())
    table = table if table and table in ctx.ws.ids("table") else None
    given = ((_given_file(ctx, file) if file and profile.saves_code else "")
             + (_table_report(ctx, table) if table else ""))
    opening = f"{now_line()}\n\n{context_block(ctx.ws)}\n\n" + given + f"Your task: {task}"
    messages: list[dict] = [{"role": "user", "content": opening}]
    named = _task_images(ctx, task) if sees else []
    if named:  # on the opening itself: one user turn, not two in a row
        shown = _pictures(ctx, named, "task")
        messages[0] = {**messages[0], "content": f"{opening}\n\n{shown['content']}", "images": shown["images"]}
    own = profile.own_tools()
    finish_spec = _submit_spec(profile)
    tool_specs = [t.spec() for t in own.values()] + ([_save_code_spec()] if profile.saves_code else []) + [finish_spec]
    trace = Trace(existing=set(ctx.ws.ids()), scratch=ctx.scratch, table=table, wanted_file=ctx.scratch.name)
    for n in pinned(ctx.ws):  # the pinned notes in its opening are evidence too: citable like search results
        trace.sources.add(source(n))
        trace.passages.append((source(n), n["text"]))
    limit, count = prompt_limit(provider), counter(provider, system, tool_specs)  # exact, from the server
    retried, shortened = False, 0
    progress({"type": "worker_start", "worker": profile.name, "task": task, "limit": limit})
    for step in range(profile.max_steps + 1):
        last = step == profile.max_steps
        visible, shortened = shorten_tool_results(fewer_images(messages), limit, count, shortened)
        try:  # the last step offers only the finish: the task always ends with a result
            turn = provider.complete(system, visible, [finish_spec] if last else tool_specs, require_tool=True)
        except ProviderError as exc:
            return {"status": "failed", "worker": profile.name, "error": str(exc)}
        messages.append({"role": "assistant", "text": turn.text, "tool_calls": turn.tool_calls, "raw": turn.raw,
                         "provider": provider.name, "model": turn.model, "stop": turn.stop})
        progress({"type": "worker_turn", "worker": profile.name, "step": step, "stop": turn.stop,
                  "calls": [c["name"] for c in turn.tool_calls], "text": turn.text[:300], "usage": turn.usage,
                  "shortened": shortened})
        if not turn.tool_calls:  # cut off by the length limit while thinking (a call is required otherwise)
            if last:
                return _done(ctx, profile, {"answer": ""}, trace, ["the helper ran out of steps without finishing"],
                             task=task)
            hint = (HELPER_CUT_OFF if turn.stop == "max_tokens" else NO_CALL).format(finish=profile.finish)
            messages.append({"role": "user", "content": hint})
            progress({"type": "worker_hint", "worker": profile.name, "text": hint})
            continue
        drawn_before, read = len(ctx.scratch.images), []
        for call in turn.tool_calls:
            args, confirmed = split_confirm(call["input"])
            if call["name"] == profile.finish:
                outcome = _submitted(profile, args, trace, opening)
                progress({"type": "worker_check", "worker": profile.name,
                          "problems": [outcome] if isinstance(outcome, str) else outcome["problems"],
                          "retry": not (isinstance(outcome, dict) and (not outcome["problems"] or retried))})
                if isinstance(outcome, dict) and (not outcome["problems"] or retried):
                    return _done(ctx, profile, outcome["result"], trace, outcome["problems"], task=task)
                retried = retried or isinstance(outcome, dict)
                feedback = (outcome if isinstance(outcome, str) else
                            FIX_FINISH.format(finish=profile.finish) + "; ".join(outcome["problems"]))
                messages.append({"role": "tool", "tool_call_id": call["id"], "name": profile.finish,
                                 "content": feedback, "is_error": True})
                continue
            progress({"type": "worker_tool", "worker": profile.name, "name": call["name"], "input": args})
            if call["name"] == SAVE_CODE and profile.saves_code:
                result = _save_code(ctx, profile, args, trace, opening, confirmed)
                trace.counts[SAVE_CODE] = trace.counts.get(SAVE_CODE, 0) + 1
            elif call["name"] not in own:
                result = ToolResult("invalid_arguments", {"error": {"message": f"{call['name']} is not one of your tools."}})
                trace.not_run += 1
            elif call["name"] == "ask_user" and trace.counts.get("ask_user", 0) >= MAX_ASKS:
                result = ToolResult("invalid_arguments", {"error": {"message": (
                    f"You have asked the user {MAX_ASKS} times in this task: decide the rest yourself, with the "
                    "recommended choices, and say so in your answer.")}})
                trace.not_run += 1
            elif isinstance(args, dict) and (table_id := _uninspected(ctx, trace, call["name"], args)):
                result = _inspect_first(ctx, trace, table_id, call["name"], args, own)
            elif isinstance(args, dict) and (question := _guessed(trace, call["name"], args, opening)):
                trace.not_run += 1
                result = ToolResult("invalid_arguments", {"error": {"message": (
                    f"Not run: this settles a question only the user can answer ({question}). Don't guess: put it "
                    f"in {profile.finish} as question_for_user with its options.")}})
            elif isinstance(args, dict) and (repeat := _repeat(trace, call["name"], args, confirmed)):
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
                if call["name"] == "read_file" and result.result.get("kind") == "image":
                    read.append(result.result["file"])
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
        for kind, items in (("drawn", ctx.scratch.images[drawn_before:]), ("read", read)):
            if sees and items:  # after this step's results: the helper sees them on its next step
                messages.append(_pictures(ctx, items, kind))
                progress({"type": "worker_hint", "worker": profile.name, "text": messages[-1]["content"]})
    return _done(ctx, profile, {"answer": ""}, trace, ["the helper ran out of steps without finishing"], task=task)


def _inspect_first(ctx: ToolContext, trace: Trace, table: str, name: str, args: dict, own: dict) -> ToolResult:
    """A call on a table nobody has inspected: inspect_data runs first - a helper that skips it computes on hidden
    summary rows, duplicates and text numbers. When the table needs nothing (no cleaning, no question), the call
    runs right after, as made; otherwise it waits for the cleaned table (measured: throwing the call away every time
    made the helper write its query again for a table that needed nothing)."""
    looked = execute(ctx, "inspect_data", {"data": table})
    _settle_questions(ctx, trace, table, looked)
    trace.add("inspect_data", {"data": table}, looked)
    res = looked.result
    if looked.status == "ok" and not res.get("suggested_steps") and not res.get("questions") \
            and not res.get("user_answers"):
        result = execute(ctx, name, args, own)
        trace.add(name, args, result)
        result.result["note"] = " ".join(filter(None, [f"inspect_data ran first on {table}: nothing to clean.",
                                                       result.result.get("note")]))
        return result
    trace.not_run += 1
    return ToolResult("invalid_arguments", {
        "error": {"message": f"Not run: {table} had not been inspected, so inspect_data ran first (below). Apply its "
                             "suggested steps, then make your call on the table it gives."},
        "inspect_data": res})


def _pictures(ctx: ToolContext, items: list[str], kind: str) -> dict:
    """A message showing these images to the helper - the newest IMAGES_SHOWN; any more are named only."""
    images, names = [], []
    for item in list(dict.fromkeys(items)):
        e = ctx.ws.entry(item)
        meta = e.get("meta", {})
        size = f", {meta['width']}x{meta['height']}" if meta.get("width") else ""
        if len(images) < IMAGES_SHOWN:
            try:
                data, mime = prepare(ctx.ws.path(item).read_bytes())
            except ToolError:
                names.append(f"{item} ({e['name']}{size}, not a readable image)")
                continue
            images.append({"mime": mime, "data": base64.b64encode(data).decode()})
            names.append(f"{item} ({e['name']}{size})")
        else:
            names.append(f"{item} ({e['name']}{size}, not shown)")
    return {"role": "user", "content": PICTURES[kind].format(names=", ".join(names)), "images": images}


def _task_images(ctx: ToolContext, task: str) -> list[str]:
    """Images the task names - by id (image_3), or by its file name or the name without its extension
    ('the passengers chart' for passengers.png) - at most IMAGES_SHOWN."""
    images = ctx.ws.ids("image")
    named = [i for i in ITEM_REF.findall(task) if i in images]
    for item in reversed(images):  # the newest first
        name = ctx.ws.entry(item)["name"]
        stem = PurePath(name).stem
        if item not in named and (name.lower() in task.lower() or (
                len(stem) >= 4 and re.search(rf"\b{re.escape(stem)}\b", task, re.I))):
            named.append(item)
    return named[:IMAGES_SHOWN]


def _table_report(ctx: ToolContext, table: str) -> str:
    """The table's latest report - an earlier task's decisions and the cleaned table it made - first."""
    report = ctx.ws.report(table)
    if not report:
        return ""
    return (f"The table's latest report (from an earlier task on {table}) - use its decisions and the cleaned table "
            f"it names instead of redoing them:\n{report}\n\n")


def program_problem(ws, file: str) -> str | None:
    """Why main's file=... is not a program to keep or change, with the field that takes it - None when it is one.
    Measured on the 26B: main put the table there ('expenses.csv'), then the memo the script was to write
    ('memo.docx'); taken as programs to keep, the helper spent 400 s unable to keep either."""
    name = " ".join(str(file).split())
    item = ws.find(name)
    if item is None:
        return None if not PurePath(name).suffix or is_code(name) else (
            f"{name} is not a program: name it in the task for the script to write. file is the program to keep, "
            "e.g. 'make_memo.py'.")
    e = ws.entry(item)
    if e["kind"] == "table":
        return f"{name} is a table ({item}): give it as data, not file."
    if e["kind"] != "file" or not is_code(e["name"]):
        return (f"{name} is {item}, not a program: name it in the task for the helper to read or look at. file is "
                "the program to keep or change.")
    return None


def _given_file(ctx: ToolContext, file: str) -> str:
    """The program the task is about (program_problem has passed it): an existing one goes into the scratch script
    with its report, to change and keep under its name; a new name is the one to keep it under."""
    item = ctx.ws.find(file, ("file",))
    if item is None:
        name = " ".join(str(file).split())
        ctx.scratch.name = name if PurePath(name).suffix else f"{name}.py"
        return f"The program to make: {ctx.scratch.name} - keep it with save_code once it works.\n\n"
    e = ctx.ws.entry(item)
    ctx.scratch.code = ctx.ws.path(item).read_text("utf-8", errors="replace")
    ctx.scratch.name, ctx.scratch.item = e["name"], item
    report = ctx.ws.report(item)
    return (f"The file to work on: {e['name']} ({item}), {len(ctx.scratch.code.splitlines())} lines, already in "
            "your scratch script: change only what the task asks, with fix_code, and keep it under the same name "
            "with save_code.\n" + (f"Its latest report:\n{report}\n\n" if report else "It has no report yet.\n\n"))


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
    return {"result": result.model_dump(exclude_none=True), "problems": profile.check(result, trace, context)}


def _save_code(ctx: ToolContext, profile: Profile, raw, trace: Trace, context: str, confirmed: bool) -> ToolResult:
    """save_code: the scratch script (or another language's file) kept with its report, after its checks - which
    answer this call, so the helper fixes the report and calls again. Over a file the user brought, the user is
    asked in place first."""
    try:
        fields = SaveCodeIn.model_validate(raw if isinstance(raw, dict) else {})
    except ValidationError as exc:
        first = exc.errors()[0]
        return ToolResult("invalid_arguments", {"error": {"message": f"{'.'.join(map(str, first['loc']))}: "
                                                                     f"{first['msg']}"}})
    given = bool(fields.code and fields.code.strip())
    if not given and PurePath(fields.name.strip()).suffix.lower() not in (".py", ""):
        # the scratch script is Python; what its run wrote (a picture, a table, a document) is kept already under
        # its own name - saving the script under that name would have replaced it (seen: 'memo.docx')
        return ToolResult("invalid_arguments", {"error": {"message": (
            f"Not kept: your scratch script is Python - keep it as '{PurePath(fields.name.strip()).stem}.py'. What "
            f"its run wrote ({fields.name.strip()}) is kept already."), "field": "name"}})
    code = fields.code if given else ctx.scratch.code
    problems = save_problems(fields, code, trace, context, ctx.scratch, confirmed)
    if problems:
        return ToolResult("invalid_arguments", {"error": {"message": "Not kept: " + "; ".join(problems)}})
    sections = {k: getattr(fields, k) for k, _ in SECTIONS}
    try:
        saved = save_file(ctx.ws, ctx.index, ctx.scratch, fields.name, sections, fields.code)
    except ToolError as exc:
        return ToolResult("invalid_arguments", {"error": exc.error()})
    if saved.status != "ok":
        return saved
    if (saved.artifact or {}).get("type") == "question":  # the user's own file: they decide, in place
        card = saved.artifact
        if ctx.ask is None:
            return ToolResult("invalid_arguments", {"error": {"message": (
                f"{fields.name} is the user's own file and nobody can be asked now: keep yours under another name.")}})
        got = ctx.ask({"tool": DELEGATE_OF[profile.name],
                       **{k: card[k] for k in ("question", "options", "allow_text", "allow_skip")}})
        told, shown = carry_out(ctx.ws, ctx.index, card["approval"], card["options"], got.get("answer"))
        if not told.get("allowed"):
            return ToolResult("ok", {"kept": False, **{k: v for k, v in told.items() if k != "status"}})
        if shown:
            ctx.emit({"tool": f"{profile.name}_worker", **shown})
        kept = {"file": told["file"], "name": told["name"], "report": told["report"]}
    else:
        ctx.emit({"tool": f"{profile.name}_worker", **saved.artifact})
        kept = {k: saved.result[k] for k in ("file", "name", "report")}
    trace.saved = kept
    return ok({"kept": f"{kept['name']} ({kept['file']}), with its report; the user sees it in the chat",
               "report": kept["report"]})


def _done(ctx: ToolContext, profile: Profile, result: dict, trace: Trace, problems: list[str],
          note: str | None = None, task: str = "") -> dict:
    """The result for the main slot, in the one shape every helper hands back: answer (what main tells the
    user from), sources (web, knowledge), report (a kept program's, or the report kept on the table), files
    (what the task saved or made: id, name, kind - a kept program first), and the briefing (warnings, note,
    trail, suggestion, question_for_user). The cited web sources go to the UI as cards. A note is for
    information (e.g. the step limit was reached); warnings are problems with the answer itself."""
    cited = [trace.cards[u] for u in result.get("sources", []) if u in trace.cards]
    if cited:
        ctx.emit({"tool": f"{profile.name}_worker", "type": "sources", "items": cited})
    asking = bool(result.get("question_for_user"))
    if profile.result_model is WorkResult:
        result = _shaped(ctx, profile, result, trace, task, asking)
    out = _finish(profile, result, trace, problems, note)
    saved = [{"id": trace.saved["file"], "name": trace.saved["name"], "kind": "file"}] if trace.saved else []
    listed = {f["id"] for f in saved}
    files = saved + [{"id": i, "name": ctx.ws.entry(i)["name"], "kind": ctx.ws.kind(i)}
                     for i in ctx.ws.ids() if i not in trace.existing and i not in listed]
    if files:
        out["files"] = files
    if note:
        out["note"] = note
    return out


def _shaped(ctx: ToolContext, profile: Profile, result: dict, trace: Trace, task: str, asking: bool) -> dict:
    """The engineer's finish as main gets it: the answer and its caveats, and a report - the one kept on the table
    the task was about (with the method), or the kept program's."""
    brief = {k: result[k] for k in BRIEF if result.get(k)}
    out = {"answer": result.get("answer", ""), **({"caveats": result["caveats"]} if result.get("caveats") else {})}
    target = trace.table or next((t for t in trace.inspected if t in trace.existing), None)
    if target and not asking and result.get("answer"):
        out.update(_keep_report(ctx, profile, result, trace, target, task))
    elif trace.saved:
        out["report"] = trace.saved["report"]
    return {**out, **brief}


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
    # Pages, by the trace (facts, not the model's account): how each cited one was read, and which could not be
    # read and why - so main can say "the shop's page refused us" instead of guessing.
    read = [r for u in out.get("sources", []) if (r := _how_read(u, trace.cards.get(u)))]
    if read:
        out["read"] = read
    failed = [{"url": u, "why": why} for u, why in trace.failed_urls.items() if u and u not in trace.read_urls]
    if failed:
        out["failed"] = failed[-5:]
    if problems:
        out["warnings"] = problems
    return out


def _how_read(url: str, card: dict | None) -> dict | None:
    """A cited page's title, date and how it was read (directly, through a site's API, by a browser) - None
    when its card says nothing more than its address."""
    if not card:
        return None
    date = card.get("updated") or card.get("published")
    out = {"url": url, **({"title": card["title"]} if card.get("title") else {}),
           **({"date": str(date)[:10]} if date else {})}
    if len(out) == 1 and not card.get("via"):
        return None
    return {**out, "how": card.get("via") or "read directly"}


TITLE_CHARS = 120


def _keep_report(ctx: ToolContext, profile: Profile, result: dict, trace: Trace, target: str, task: str) -> dict:
    """The report kept on the table the task was about (or the first one it inspected): the task as its title,
    the finish's sections, and the app's facts; the next task on that table starts from it."""
    from app.tools.files import index_report
    first = (task.strip().splitlines() or ["A task on " + target])[0]
    made = [i for i in ctx.ws.ids("table") if i not in trace.existing]
    facts = [f"Table: {ctx.ws.entry(target)['name']} ({target}), report saved {datetime.now():%Y-%m-%d %H:%M}"]
    if made:
        facts.append("Tables made: " + ", ".join(
            f"{i} ({ctx.ws.entry(i)['name']}" + (f", from {ctx.ws.entry(i)['parent']})" if ctx.ws.entry(i).get("parent")
                                                  else ")") for i in made))
    if trace.charts:
        facts.append(f"Charts shown: {trace.charts}")
    if trace.saved:
        facts.append(f"Program kept: {trace.saved['name']} ({trace.saved['file']})")
    facts.append(_trail(trace))
    sections = {"question": first if len(first) <= TITLE_CHARS else first[:TITLE_CHARS].rsplit(" ", 1)[0] + " ...",
                "data_used": result.get("data_used") or "As in the facts below.", "method": result.get("method", ""),
                "results": result.get("answer", ""), "caveats": result.get("caveats", "")}
    report = assemble(DATA_SECTIONS, sections, facts)
    ctx.ws.set_report(target, report)
    index_report(ctx.ws, ctx.index, target)
    ctx.emit({"tool": f"{profile.name}_worker", "type": "report", "table": target, "name": ctx.ws.entry(target)["name"],
              "report": report})
    return {"report": report, "table": target}


def _save_code_spec() -> dict:
    schema = SaveCodeIn.model_json_schema()
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    typed_optionals(schema)
    return {"name": SAVE_CODE, "description": SAVE_CODE_DESCRIPTION, "input_schema": schema}


def _submit_spec(profile: Profile) -> dict:
    schema = profile.result_model.model_json_schema()
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    # The answer first, the briefing fields last: a small model fills them in the order shown.
    schema["properties"] = dict(sorted(schema["properties"].items(), key=lambda kv: kv[0] in BRIEF))
    typed_optionals(schema)
    description = ("Hand in your result, once, at the end: main answers the user from it, and on a task about a table "
                   "it is kept as that table's report for the next task." if profile.result_model is WorkResult
                   else "Hand in your final result (call once, at the end).")
    return {"name": profile.finish, "description": description, "input_schema": schema}
