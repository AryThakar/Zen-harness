"""One helper task: the opening message, the step loop (guards, tools, pictures, the steps left), the finish call with
its checks, and the result main gets - in one envelope, with the files the task made or changed and a table's report.

A helper acts or finishes: every step requires a tool call (llama-server's tool_choice 'required', measured with
Gemma 4 on the 26B, thinking on and off) and the last step offers only its finish. Nothing is read out of prose and
nothing is repaired: a check that fails names the problem, and the helper fixes it.

Nothing done is lost: a program is a file in the workspace (written, run, edited - files and run), so the next task
goes on from it; a helper whose model fails, or whose conversation outgrows the slot, hands back what it did instead
of an error; and a Stop keeps the trace for main (ctx.trace, main._tools)."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import PurePath

from pydantic import ValidationError

from app.providers.base import Provider, ProviderError
from app.tools.approval import carry_out
from app.tools.core import ToolError, ToolResult
from app.tools import agent_md
from app.tools.reports import DATA_SECTIONS, assemble
from app.tools.vision import prepare
from app.tools.workspace import ITEM_REF, present

from ..context import (IMAGES_SHOWN, context_block, counter, fewer_images, now_line, prompt_limit, shorten_call_args,
                       shorten_tool_results)
from ..prompts import FIX_FINISH, FULL, HELPER_CUT_OFF, NO_CALL, PICTURES, SHORT_FINISH, STEPS_LEFT, call_cut_off
from ..registry import ToolContext, execute, split_confirm, typed_optionals
from .contracts import BRIEF, DELEGATE_OF, MAX_ASKS, Profile, WebResult, WorkResult
from .evidence import SEARCHES_BEFORE_READING, Trace, _guessed, _repeat, _unread, _uninspected


def _settle_questions(ctx: ToolContext, trace: Trace, table: str, result: ToolResult) -> None:
    """inspect_data's questions only the user can answer (03/04: 3 April or 4 March?), asked at once by
    the runner, in place; the step of the option the user picks is applied by code (clean_data), and the
    result says which table to use. Left in the result, as before, when there is no user to ask."""
    questions = [q for q in result.result.get("questions", []) if isinstance(q, dict) and q.get("options")]
    if result.status != "ok" or not questions:
        return
    asked_about, known = table, _answers(ctx, table)
    if ctx.ask is None and not any(_question_key(q) in known for q in questions):
        return
    settled, left = [], []
    for q in questions:
        before = known.get(_question_key(q))
        if before:  # answered before for this table or the one it was made from: never asked twice
            settled.append(f"{q['question']} The user answered this before: '{before['answer']}'."
                           + (f" Use {before['use']}, the table cleaned with it." if before.get("use") else ""))
            continue
        if ctx.ask is None:
            left.append(q)
            continue
        labels = [o["label"] if isinstance(o, dict) else str(o) for o in q["options"]]
        got = ctx.ask({"tool": DELEGATE_OF.get("engineer", "analyze_or_code"), "question": q["question"],
                       "options": labels, "allow_text": True, "allow_skip": True})
        said = " ".join(str(got.get("answer") or "").split())
        option = _chosen(q["options"], said)
        if option is None and said:  # typed words, not an option: theirs to act on - "your call" leaves it to us
            settled.append(f"{q['question']} The user answered '{said}': act on it - if they leave it to you, take "
                           f"the recommended option ({labels[0]}), and say which you took in your answer.")
            _remember_answer(ctx, asked_about, q, said, None)
            continue
        if option is None:
            settled.append(f"{q['question']} - the user didn't choose: don't guess; say in your answer that it "
                           "depends on it.")
            continue
        if not option.get("step"):  # e.g. keep the repeated rows: nothing to change
            settled.append(f"{q['question']} The user chose '{option['label']}': nothing to clean for it; go on "
                           f"with {table}.")
            _remember_answer(ctx, asked_about, q, option["label"], None)
            continue
        done = execute(ctx, "clean_data", {"data": table, "steps": [option["step"]]})
        trace.add("clean_data", {"data": table, "steps": [option["step"]]}, done)
        if done.status == "ok":
            table = done.result["data"]
            settled.append(f"{q['question']} The user chose '{option['label']}': {done.result['from']} was cleaned "
                           f"with it into {table}. Use {table} from now on.")
            _remember_answer(ctx, asked_about, q, option["label"], table)
    result.result["questions"] = left
    if not left:
        result.result.pop("questions", None)
    result.result["user_answers"] = settled


def _question_key(q: dict) -> str:
    """What a question decides - its options' cleaning steps (an op and its columns) - so the same question asked
    again, of this table or one made from it, is known even when its wording (a count) differs."""
    steps = [o.get("step") or {} for o in q.get("options", []) if isinstance(o, dict)]
    ops = sorted(f"{s.get('op', '')}:{','.join(map(str, s.get('columns') or []))}" for s in steps if s.get("op"))
    return "|".join(ops) or " ".join(str(q.get("question", "")).split())


def _answers(ctx: ToolContext, table: str) -> dict:
    """The user's answers kept on this table and the tables it was made from (the nearest one wins)."""
    out, seen, key = {}, set(), table
    while key and key in ctx.ws.ids() and key not in seen:
        seen.add(key)
        for k, v in (ctx.ws.entry(key).get("meta", {}).get("answers") or {}).items():
            out.setdefault(k, v)
        key = ctx.ws.entry(key).get("parent")
    return out


def _remember_answer(ctx: ToolContext, table: str, q: dict, answer: str, use: str | None) -> None:
    """The user's answer kept on the table it was about - measured: the same duplicate-rows question was asked twice
    of one table in a chat, an hour apart."""
    if table not in ctx.ws.ids():
        return
    meta = ctx.ws.entry(table).get("meta", {})
    kept = dict(meta.get("answers") or {})
    kept[_question_key(q)] = {"question": q["question"], "answer": answer, **({"use": use} if use else {})}
    ctx.ws.set_meta(table, "answers", kept)
    if q["question"] in (meta.get("issues") or []):  # decided: no longer a problem to warn about on this table
        ctx.ws.set_meta(table, "issues", [i for i in meta["issues"] if i != q["question"]])


def _chosen(options: list, said: str | None) -> dict | None:
    """The option the user's answer picks: the button's own label, or typed text that starts like one."""
    said = (said or "").strip().lower()
    if not said:
        return None
    return next((o for o in options if isinstance(o, dict) and (
        said == o["label"].lower() or said.startswith(o["label"].split("(")[0].strip().lower()))), None)


def run_worker(provider: Provider, profile: Profile, task: str, ctx: ToolContext, *,
               progress: Callable[[dict], None] | None = None, files: list[str] | None = None) -> dict:
    """Run one task on the worker slot; returns the checked result for the main slot. files: the workspace files
    the task is about (main's call) - listed in the opening, a table's latest report first (the new one is kept
    on it), a picture shown.

    The opening carries main's task, not the user's message: a helper doesn't see the chat (the contract), and
    given the user's words it did the parts main had given to others - measured on the 26B, the web helper asked
    for an invoice total that was the analyst's part, and the analyst built the slide deck (one slide, in a
    script) that main meant to write from Markdown with the chart."""
    progress = progress or (lambda event: None)
    system = profile.instructions  # the same for every task of this role, so llama.cpp keeps it cached
    sees = profile.sees_images and bool(getattr(provider, "sees_images", lambda: False)())
    given = [k for k in (ctx.ws.find(str(f)) for f in files or []) if k]
    table = next((k for k in given if ctx.ws.kind(k) == "table"), None)
    named = list(dict.fromkeys([k for k in given if ctx.ws.kind(k) == "image"] + (_task_images(ctx, task) if sees else [])))
    # A report or a task written in the first design's words may name files by key: by path, as everywhere.
    opening = present(ctx.ws, f"{now_line()}\n\n{context_block(ctx.ws)}\n\n" + _given(ctx, given)
                      + (_table_report(ctx, table) if table else "") + _pages_seen(ctx, profile)
                      + f"{profile.task_lead}: {task}")
    messages: list[dict] = [{"role": "user", "content": opening}]
    if sees and named:  # on the opening itself: one user turn, not two in a row
        shown = _pictures(ctx, named[:IMAGES_SHOWN], "task")
        messages[0] = {**messages[0], "content": f"{opening}\n\n{shown['content']}", "images": shown["images"]}
    own = profile.own_tools()
    finish_spec = _submit_spec(profile)
    tool_specs = [t.spec() for t in own.values()] + [finish_spec]
    trace = Trace(existing=set(ctx.ws.ids()), table=table, stamps=_stamps(ctx))
    ctx.trace = trace
    if agent_md.body(ctx.ws):  # agent.md, in its opening, is evidence too: citable like a file it read
        trace.sources.add(agent_md.AGENT_FILE)
        trace.passages.append((agent_md.AGENT_FILE, agent_md.body(ctx.ws)))
    limit, count = prompt_limit(provider), counter(provider, system, tool_specs)  # exact, from the server
    retried, shortened, cut = False, 0, 0
    cuts: list[dict] = []  # the cut-offs in a row: each one's cut call ({} for one cut while thinking)
    progress({"type": "worker_start", "worker": profile.name, "task": task, "limit": limit})
    short_again = False  # the last step's finish was cut off: one step more, for a short one (SHORT_FINISH)
    for step in range(profile.max_steps + 2):
        if step > profile.max_steps and not short_again:
            break
        last = step >= profile.max_steps
        visible, shortened = shorten_tool_results(fewer_images(messages), limit, count, shortened)
        if count(visible) > limit:  # results shortened and still too long: the calls' own long texts next
            visible, cut = shorten_call_args(visible, limit, count, cut)
        if count(visible) > limit and not last:  # still too long: this step is the last - finish with what is done
            last = True
            visible = visible + [{"role": "user", "content": FULL.format(finish=profile.finish)}]
        if (size := count(visible)) > limit:  # then the newest results too, as main's ladder does: the finish must
            # fit (measured: without this step a web helper with two long price pages handed back nothing, 3 times)
            visible, _ = shorten_tool_results(visible, limit, count, 0, keep_last=0)
            size = count(visible)
        if size > limit:
            return _done(ctx, profile, {"answer": ""}, trace, [], task=task,
                         stopped=CONTEXT_FULL)
        try:  # the last step offers only the finish: the task always ends with a result
            turn = provider.complete(system, visible, [finish_spec] if last else tool_specs, require_tool=True,
                                     on_event=lambda kind, text: kind == "writing" and progress(
                                         {"type": "worker_writing", "worker": profile.name, **json.loads(text)}),
                                     prompt_tokens=size, sampling=profile.sampling)
        except ProviderError as exc:
            return _done(ctx, profile, {"answer": ""}, trace, [], task=task, stopped=_plain(exc))
        messages.append({"role": "assistant", "text": turn.text, "tool_calls": turn.tool_calls, "raw": turn.raw,
                         "provider": provider.name, "model": turn.model, "stop": turn.stop,
                         **({"thinking": turn.thinking} if turn.thinking else {})})  # back with the step (llamacpp.py)
        progress({"type": "worker_turn", "worker": profile.name, "step": step, "stop": turn.stop,
                  "calls": [c["name"] for c in turn.tool_calls], "text": turn.text[:300], "usage": turn.usage,
                  "shortened": shortened, **({"cut_call": turn.cut_call} if turn.cut_call else {})})
        cuts = cuts + [turn.cut_call or {}] if turn.stop == "max_tokens" and not turn.tool_calls else []
        if _cut_again(cuts):  # each cut-off costs minutes (measured: three in a row, 10 minutes): end with what is done
            return _done(ctx, profile, {"answer": ""}, trace, [], task=task,
                         stopped=f"its replies ran past the length limit {len(cuts)} times in a row")
        if not turn.tool_calls:  # cut off by the length limit (a call is required otherwise)
            if last and not short_again:  # measured 2026-10-17: 9 searches and 14 pages read, the finish cut off
                # by the length limit, and main got an empty answer - and ran the whole search again
                short_again = True
                messages.append({"role": "user", "content": SHORT_FINISH.format(finish=profile.finish)})
                progress({"type": "worker_hint", "worker": profile.name, "text": messages[-1]["content"]})
                continue
            if last:
                return _done(ctx, profile, {"answer": ""}, trace, ["the helper ran out of steps without finishing"],
                             task=task)
            hint = (call_cut_off(turn.cut_call, main=False, edits="edit" in _modes(own.get("files")))
                    if turn.cut_call else
                    (HELPER_CUT_OFF if turn.stop == "max_tokens" else NO_CALL).format(finish=profile.finish))
            messages.append({"role": "user", "content": hint})
            progress({"type": "worker_hint", "worker": profile.name, "text": hint})
            continue
        drawn, read = [], []
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
                if last:  # sent back on its last step: one step more to fix it (measured 2026-10-22: a cafe forecast's
                    # answer sent back for numbers worked out in its head, on step 12 of 12 - "ran out of steps", the
                    # answer lost, and main handed the whole task over twice more: 15 minutes)
                    short_again = True
                feedback = (outcome if isinstance(outcome, str) else
                            FIX_FINISH.format(finish=profile.finish) + "; ".join(outcome["problems"]))
                messages.append({"role": "tool", "tool_call_id": call["id"], "name": profile.finish,
                                 "content": feedback, "is_error": True})
                continue
            progress({"type": "worker_tool", "worker": profile.name, "name": call["name"], "input": args})
            if call["name"] not in own:
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
                    f"Not run: this settles a question only the user can answer ({question}). Don't guess: "
                    + ("ask the user with ask_user, with its options." if profile.asks else
                       f"put it in {profile.finish} as question_for_user with its options."))}})
            elif call["name"] == "web_search" and not confirmed and trace.searches_in_a_row >= SEARCHES_BEFORE_READING:
                trace.not_run += 1
                result = _read_instead(trace)
            elif (call["name"] in ("web_search", "read_webpage") and trace.table_pages and not trace.table_said
                  and not confirmed):
                trace.not_run += 1
                trace.table_said = True  # said once: a wall refused ten calls in a row (measured), a nudge doesn't
                result = _table_found(trace)
            elif isinstance(args, dict) and (repeat := _repeat(trace, call["name"], args, confirmed)):
                result = repeat
                trace.not_run += 1
            else:
                result = _approved(ctx, profile, execute(ctx, call["name"], args, own))
                if call["name"] == "inspect_data" and isinstance(args, dict):
                    _settle_questions(ctx, trace, ctx.ws.find(str(args.get("data", ""))) or "", result)
                if isinstance(args, dict):
                    trace.add(call["name"], args, result)
                else:
                    trace.not_run += 1  # arguments that were not JSON
                if call["name"] == "run":
                    drawn += [f["id"] for f in (result.artifact or {}).get("files", [])
                              if f.get("id") and ctx.ws.kind(f["id"]) == "image"]
                if call["name"] == "files" and result.result.get("kind") == "image":
                    read.append(result.result["file"])
            content = result.for_model(lambda body: present(ctx.ws, body))
            messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"],
                             "content": content, "is_error": result.status not in ("ok", "partial")})
            progress({"type": "worker_result", "worker": profile.name, "name": call["name"],
                      "status": result.status, "chars": len(content), "content": content})
        left = profile.max_steps - (step + 1)  # model calls that may still use tools
        if left in STEPS_LEFT and messages[-1]["role"] == "tool":
            hint = STEPS_LEFT[left].format(finish=profile.finish)
            messages[-1]["content"] += "\n\n" + hint
            progress({"type": "worker_hint", "worker": profile.name, "text": hint})
        for kind, items in (("drawn", drawn), ("read", read)):
            if sees and items:  # after this step's results: the helper sees them on its next step
                messages.append(_pictures(ctx, items, kind))
                progress({"type": "worker_hint", "worker": profile.name, "text": messages[-1]["content"]})
    return _done(ctx, profile, {"answer": ""}, trace, ["the helper ran out of steps without finishing"], task=task)


CONTEXT_FULL = "its conversation grew larger than the model's context"


def _cut_again(cuts: list[dict]) -> bool:
    """Cut-offs in a row that won't end by themselves: the same too-long call twice, or three of any kind (one cut
    while thinking, then one mid-call, recovers - measured)."""
    return len(cuts) >= 3 or (len(cuts) >= 2 and all(c.get("name") for c in cuts[-2:]))


def _modes(tool) -> set[str]:
    """A tool's modes (files: find, read, write ...), from its input's mode field; none for a tool without modes."""
    field = tool.input_model.model_fields.get("mode") if tool is not None else None
    return set(getattr(field.annotation, "__args__", ())) if field is not None else set()


def _plain(exc: ProviderError) -> str:
    """Why the helper's model stopped, in words main can pass on - not the server's error page."""
    said = str(exc)
    if "exceeds the available context" in said or "context size" in said:
        return CONTEXT_FULL
    if any(code in said for code in ("(502)", "(503)", "(504)", "(530)")) or "<html" in said.lower() or "reach" in said:
        return "the model server could not be reached"
    return "the model stopped with an error"


def _approved(ctx: ToolContext, profile: Profile, result: ToolResult) -> ToolResult:
    """A call that waits for the user's permission (changing a file the user brought) is asked in place: the card
    shows, the helper waits, and the action is done - or not - by code. Nobody to ask (a test): refused."""
    card = result.artifact or {}
    if card.get("type") != "question" or not card.get("approval"):
        return result
    if ctx.ask is None:
        return ToolResult("invalid_arguments", {"error": {"message": (
            "That is the user's own file and nobody can be asked now: write yours under another name.")}})
    got = ctx.ask({"tool": DELEGATE_OF[profile.name], **{k: card[k] for k in ("question", "options", "allow_text",
                                                                             "allow_skip")}})
    told, shown = carry_out(ctx.ws, ctx.index, card["approval"], card["options"], got.get("answer"))
    if shown:
        ctx.emit({"tool": f"{profile.name}_worker", **shown})
    return ToolResult("ok" if told.get("allowed") and told.get("status") != "failed" else "failed",
                      {k: v for k, v in told.items() if k != "status"})


def _table_found(trace: Trace) -> ToolResult:
    """A page read already holds the data table: finish with it, instead of hunting its rows elsewhere (measured
    2026-10-17, twice: the table found at 77 s, then 6 more minutes of searches and reads for single days' closes - the
    prompt's 'one such page is enough' did not stop it)."""
    page = trace.table_pages[0]
    return ToolResult("invalid_arguments", {"error": {"message": (
        f"Not run: {page} already holds the data table - main brings it in exact, every row. Give its link in "
        "downloads and call submit_result now, with a short summary. If the task needs another table (another "
        "company, another period), make the same call with confirm: true.")}})


def _read_instead(trace: Trace) -> ToolResult:
    """The searches so far found pages nobody read: read one of them (measured on the 26B: 9 searches in a row for
    LeetCode's exact wording in snippets, 300 s, while the pages it kept listing went unread)."""
    unread = _unread(trace)[:4]
    pages = "; ".join(unread) if unread else "none yet - search once more with other words"
    return ToolResult("invalid_arguments", {"error": {"message": (
        f"Not run: {trace.searches_in_a_row} searches in a row without reading a page. Searches choose pages; the "
        f"answer comes from reading them. Read one of the pages they found (the most often listed first): {pages} - "
        "or finish with what you have. To search once more anyway, make the same call with confirm: true.")}})


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
        size = f"{meta['width']}x{meta['height']}" if meta.get("width") else ""
        if len(images) < IMAGES_SHOWN:
            try:
                data, mime = prepare(ctx.ws.path(item).read_bytes())
            except ToolError:
                names.append(f"{e['name']} ({size + ', ' if size else ''}not a readable image)")
                continue
            images.append({"mime": mime, "data": base64.b64encode(data).decode()})
            names.append(f"{e['name']} ({size})" if size else e["name"])
        else:
            names.append(f"{e['name']} (not shown)")
    return {"role": "user", "content": PICTURES[kind].format(names=", ".join(names)), "images": images}


def _task_images(ctx: ToolContext, task: str) -> list[str]:
    """Images the task names - by their file name, or the name without its extension ('the passengers chart' for
    passengers.png) - at most IMAGES_SHOWN."""
    images = ctx.ws.ids("image")
    named = [i for i in ITEM_REF.findall(task) if i in images]
    for item in reversed(images):  # the newest first
        name = ctx.ws.entry(item)["name"]
        stem = PurePath(name).stem
        if item not in named and (name.lower() in task.lower() or (
                len(stem) >= 4 and re.search(rf"\b{re.escape(stem)}\b", task, re.I))):
            named.append(item)
    return named[:IMAGES_SHOWN]


def _given(ctx: ToolContext, given: list[str]) -> str:
    """The files main named for the task, one line each (what they are, and a file's report line)."""
    if not given:
        return ""
    lines = ctx.ws.lines()
    return "Files for this task:\n" + "\n".join(lines.get(k, ctx.ws.label(k)) for k in given) + "\n\n"


def _table_report(ctx: ToolContext, table: str) -> str:
    """The table's latest report - an earlier task's decisions and the cleaned table it made - first."""
    report = ctx.ws.report(table)
    if not report:
        return ""
    return (f"The table's latest report (from an earlier task on {ctx.ws.label(table)}) - use its decisions and the "
            f"cleaned table it names instead of redoing them:\n{report}\n\n")


def _pages_seen(ctx: ToolContext, profile: Profile) -> str:
    """For the web helper: the pages already read in this chat, and the sites that refused - a helper remembers
    nothing between tasks, and measured, the next task went back to the site that had just refused it."""
    if profile.name != "web":
        return ""
    from app.tools.web import seen_in_chat
    said = seen_in_chat(ctx.conversation)
    return f"{said}\n\n" if said else ""


def _stamps(ctx: ToolContext) -> dict[str, str]:
    """Each file's content hash before the task: what changed during it is told apart from what was there."""
    return {k: ctx.ws.entry(k).get("sha", "") for k in ctx.ws.ids()}


def _submitted(profile: Profile, raw, trace: Trace, context: str) -> dict | str:
    """{'result', 'problems'} for a well-formed submission, or an error text to send back."""
    if not isinstance(raw, dict):
        return f"{profile.finish} needs JSON arguments."
    if raw.get("question_for_user") and str(raw.get("answer") or "").strip():
        # Asking is the whole result: the question pauses the turn for the user, and the answer beside it was never
        # shown (measured 2026-10-21: the Nobel prize found and cited, then "Would you like more details on how
        # IceCube works?" - the user got only the question). An offer of more is no question.
        return (f"{profile.finish}: you gave an answer and a question_for_user. Ask only when you can't answer without "
                "the user's choice (question_for_user with its options, and no answer); otherwise give the answer "
                "alone - an idea for more goes in suggestion.")
    if raw.get("question_for_user"):  # asking is the whole result; small models leave the rest out
        raw = {**{k: "" for k, f in profile.result_model.model_fields.items() if f.is_required()}, **raw}
    try:
        result = profile.result_model.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        return f"{profile.finish}: {'.'.join(map(str, first['loc']))}: {first['msg']}"
    return {"result": result.model_dump(exclude_none=True), "problems": profile.check(result, trace, context)}


def _done(ctx: ToolContext, profile: Profile, result: dict, trace: Trace, problems: list[str], *, task: str = "",
          stopped: str | None = None) -> dict:
    """The result for main, one envelope for every helper: what the model wrote (answer, caveats, the briefing;
    sources and download links for the web) and what the app saw (status, what ran, the files made or changed, the
    pages read and the ones that failed, warnings). ``stopped``: why the helper ended early - what it did is still
    handed back, never lost."""
    if isinstance(result.get("sources"), list) and profile.result_model is WebResult:
        result = {**result, "sources": [u for s in result["sources"] if (u := trace.cited(s))]}
    cited = [trace.cards[u] for u in result.get("sources", []) if u in trace.cards]
    if cited:
        ctx.emit({"tool": f"{profile.name}_worker", "type": "sources", "items": cited})
    asking = bool(result.get("question_for_user"))
    out = {"status": "ok", "worker": profile.name, **{k: v for k, v in result.items() if k not in BRIEF}}
    if profile.result_model is WorkResult:
        target = trace.table or next((t for t in trace.inspected if t in trace.existing), None)
        if target and not asking and result.get("answer") and not stopped:
            out.update(_keep_report(ctx, profile, result, trace, target, task))
    if stopped:
        out["stopped"] = f"The helper stopped early: {stopped}. What it did is below - it is kept; nothing is lost."
        problems = problems or ["the helper stopped before it finished"]
        if not result.get("answer"):  # what it read, so main can tell the user or ask for less - not send it again
            pages = [{"url": u, **({"title": c["title"]} if c.get("title") else {})}
                     for u, c in list(trace.cards.items())[-6:] if u in trace.read_urls]
            if pages:
                out["pages_read"] = pages
            if CONTEXT_FULL in stopped:
                out["next"] = ("What it read filled its context before it could answer: the same task again fills it "
                               "again. Ask for less at once (one figure, from one page), or tell the user what was "
                               "read and what wasn't found.")
    out["ran"] = _ran(trace)
    made = [k for k in ctx.ws.ids() if k not in trace.existing]
    changed = [k for k, sha in trace.stamps.items() if k in ctx.ws.ids() and ctx.ws.entry(k).get("sha", "") != sha]
    if made or changed:
        out["files"] = ([{"id": k, "name": ctx.ws.label(k), "kind": ctx.ws.kind(k)} for k in made]
                        + [{"id": k, "name": ctx.ws.label(k), "kind": ctx.ws.kind(k), "changed": True} for k in changed])
    if trace.last_output:
        out["last_run_printed"] = trace.last_output[-600:]
    out.update({k: result[k] for k in BRIEF if result.get(k)})
    return _finish(out, trace, problems)


def _ran(trace: Trace) -> str:
    """What the helper did, from its trace - facts main can pass on (it used predict, it ran a script), never the
    model's own account."""
    ran = ", ".join(f"{name} ({n})" if n > 1 else name for name, n in trace.counts.items())
    parts = [f"Ran {ran}." if ran else "Ran no tools."]
    if trace.failures:
        parts.append("Failed: " + "; ".join(trace.failures[-3:]) + ".")
    if trace.not_run:
        parts.append(f"{trace.not_run} call(s) not run (repeated, or not allowed).")
    return " ".join(parts)


def _finish(out: dict, trace: Trace, problems: list[str]) -> dict:
    if out.get("suggestion"):
        out["suggestion"] = out["suggestion"][:300]
    if not out.get("question_for_user"):
        out.pop("options", None)
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
    """The report kept on the table the task was about (or the first one it inspected): the task as its title, the
    answer and caveats, and the app's facts; the next task on that table starts from it."""
    from app.tools.files import index_report
    first = (task.strip().splitlines() or ["A task on " + ctx.ws.label(target)])[0]
    made = [i for i in ctx.ws.ids("table") if i not in trace.existing]
    facts = [f"Table: {ctx.ws.label(target)}, report saved {datetime.now():%Y-%m-%d %H:%M}"]
    if made:
        facts.append("Tables made: " + ", ".join(ctx.ws.describe(i) for i in made))
    if trace.charts:
        facts.append(f"Charts shown: {trace.charts}")
    facts.append(_ran(trace))
    sections = {"question": first if len(first) <= TITLE_CHARS else first[:TITLE_CHARS].rsplit(" ", 1)[0] + " ...",
                "results": result.get("answer", ""), "caveats": result.get("caveats") or "None given."}
    report = assemble(DATA_SECTIONS, sections, facts)
    ctx.ws.set_report(target, report)
    index_report(ctx.ws, ctx.index, target)
    ctx.emit({"tool": f"{profile.name}_worker", "type": "report", "table": target, "name": ctx.ws.label(target),
              "report": report})
    return {"report": report, "table": target}


def _submit_spec(profile: Profile) -> dict:
    schema = profile.result_model.model_json_schema()
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    # The answer first, the briefing fields last: a small model fills them in the order shown.
    schema["properties"] = dict(sorted(schema["properties"].items(), key=lambda kv: (kv[0] != "answer", kv[0] in BRIEF)))
    if profile.result_model is WebResult:  # sources are the read pages' numbers (a link as read is accepted too)
        schema["properties"]["sources"] = {"type": "array", "items": {"type": "integer"},
                                           "description": schema["properties"]["sources"].get("description", "")}
    typed_optionals(schema)
    description = ("Hand in your result, once, at the end: main answers the user from it, and on a task about a table "
                   "it is kept as that table's report for the next task." if profile.result_model is WorkResult
                   else "Hand in your final result (call once, at the end).")
    return {"name": profile.finish, "description": description, "input_schema": schema}
