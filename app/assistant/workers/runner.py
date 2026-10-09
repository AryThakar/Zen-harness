"""One helper task: the opening message, the step loop (guards, tools, pictures, the steps left), the
finish call and its check, and the result main gets - with the coder's file and the analyst's report kept."""

from __future__ import annotations

import base64
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import PurePath

from pydantic import ValidationError

from app.providers.base import Provider, ProviderError
from app.tools.core import ToolError, ToolResult
from app.tools.notes import pinned, source
from app.tools.reports import DATA_SECTIONS, assemble
from app.tools.scratch import SECTIONS, Scratch
from app.tools.scratch import save as save_file
from app.tools.vision import prepare
from app.tools.workspace import ITEM_REF

from ..context import IMAGES_SHOWN, context_block, counter, fewer_images, now_line, prompt_limit, shorten_tool_results
from ..prompts import FIX_ANSWER, FIX_FINISH, HELPER_CUT_OFF, PICTURES, STEPS_LEFT
from ..registry import ToolContext, execute, typed_optionals
from .checks import _numbers_problem
from .contracts import BRIEF, MAX_ASKS, SUBMIT, DataReport, Profile, SaveResult
from .evidence import Trace, _guessed, _repeat, _uninspected
from .repairs import _from_text, _json_in_text, _repair


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


def run_worker(provider: Provider, profile: Profile, task: str, ctx: ToolContext, *,
               progress: Callable[[dict], None] | None = None, file: str | None = None,
               table: str | None = None) -> dict:
    """Run one task on the worker slot; returns the checked result for the main slot. file: a stored
    file to work on - loaded into the scratch script, its report shown first; table: the table the task
    is about - its report shown first, the new one kept on it.

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
    given = _load(ctx, file) if file else _table_report(ctx, table) if table else ""
    opening = f"{now_line()}\n\n{context_block(ctx.ws)}\n\n" + given + f"Your task: {task}"
    messages: list[dict] = [{"role": "user", "content": opening}]
    named = _task_images(ctx, task) if sees else []
    if named:  # on the opening itself: one user turn, not two in a row
        shown = _pictures(ctx, named, "task")
        messages[0] = {**messages[0], "content": f"{opening}\n\n{shown['content']}", "images": shown["images"]}
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
        visible, shortened = shorten_tool_results(fewer_images(messages), limit, count, shortened)
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
            outcome = _submitted(profile, written, trace, opening, task) if written is not None else None
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
        drawn_before, read = len(ctx.scratch.images), []
        for call in turn.tool_calls:
            if call["name"] == profile.finish:
                outcome = _submitted(profile, call["input"], trace, opening, task)
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
    return _done(ctx, profile, {"answer": ""}, trace, ["the worker ran out of steps without an answer"])


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


def _submitted(profile: Profile, raw, trace: Trace, context: str, task: str = "") -> dict | str:
    """{'result', 'problems'} for a well-formed submission, or an error text to send back. A field the code
    already knows is filled in rather than sent back: the analyst's report title is the task it was given
    (measured on the 26B: a report without one cost a whole step to resend)."""
    if not isinstance(raw, dict):
        return f"{profile.finish} needs JSON arguments."
    if profile.result_model is DataReport and not str(raw.get("question") or "").strip() and task.strip():
        first = task.strip().splitlines()[0]
        raw = {**raw, "question": first if len(first) <= 120 else first[:120].rsplit(" ", 1)[0] + " ..."}
    if raw.get("question_for_user"):  # asking is the whole result; small models leave the rest out
        raw = {**{k: "" for k, f in profile.result_model.model_fields.items() if f.is_required()}, **raw}
    try:
        result = profile.result_model.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        return f"{profile.finish}: {'.'.join(map(str, first['loc']))}: {first['msg']}"
    fixed = _repair(profile, result.model_dump(exclude_none=True), trace)
    return {"result": fixed, "problems": profile.check(profile.result_model.model_validate(fixed), trace, context)}


def _done(ctx: ToolContext, profile: Profile, result: dict, trace: Trace, problems: list[str],
          note: str | None = None) -> dict:
    """The result for the main slot, in the one shape every helper hands back: answer (what main tells the
    user from), sources (web, knowledge), report (the data analyst's and the coder's, kept on the item),
    files (what the task saved or made: id, name, kind - the coder's file first), and the briefing (warnings,
    note, trail, suggestion, question_for_user). The cited web sources go to the UI as cards. A note is for
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
    listed = {f["id"] for f in out.get("files", [])}
    files = out.get("files", []) + [{"id": i, "name": ctx.ws.entry(i)["name"], "kind": ctx.ws.kind(i)}
                                    for i in ctx.ws.ids() if i not in trace.existing and i not in listed]
    if files:
        out["files"] = files
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
    if trace.charts:
        facts.append(f"Charts shown: {trace.charts}")
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
    return {"answer": str(result.get("purpose") or "").strip(), "report": saved.result["report"],
            "files": [{"id": saved.result["file"], "name": saved.result["name"], "kind": "file"}], **brief}, problems


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
