"""Phase 2's acceptance on the real model (docs/REBUILD_PLAN.md, section 5): the jobs that failed in the user's chat
of 2026-10-10, run through the real turn - the same prompts, tools, helpers and checks as the app - each from a new
store in a temporary folder, never the user's data.

    picture_name   "add my name at the top right of the picture, stylish": a real font, in 3 runs or fewer
                   (measured before: 8 whole rewrites, 15 minutes, Linux font paths, the default font)
    calculus       the max(|x-1|, 2/(1+x^2)) problem: main solves it with calculate, no helper (before: 8 minutes in
                   the helper, then Stop)
    stop_continue  Stop while the helper works, then "continue": the same program file goes on (before: main sent
                   the task again from zero, three times)
    long_program   a program changed step by step: no conversation overflow (before: 16,388 > 16,384 tokens, all lost)
    leetcode       LeetCode's Two Sum example: 3 searches or fewer in a row, LeetCode not fetched a second time, and
                   the answer says LeetCode couldn't be read (before: 9 searches in a row, then LeetCode again twice)

    python -m bench.phase2 [--only id,id] [--keep]   (llama-server on BIZ_LLAMACPP_URL, 2 slots)
        -> bench/phase2.md and .json
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import tempfile
import time
from pathlib import Path

from PIL import Image

from app.assistant import main as main_slot
from app.assistant.store import Store
from app.config import settings
from app.providers.llamacpp import LlamaCppProvider
from app.rag.embed import make_embedder

from .combos import Combo, Run, Step, _consume, _pick, drive

HERE = Path(__file__).resolve().parent


def _panda(store: Store, ws_id: str, truth: dict) -> None:
    """A picture to put the name on: a sky-blue gradient, as a generated picture would be."""
    ws, _ = store.open(ws_id)
    img = Image.new("RGB", (1344, 768))
    img.putdata([(110 + x // 20, 170 + y // 12, 235) for y in range(768) for x in range(1344)])
    out = io.BytesIO()
    img.save(out, "JPEG", quality=90)
    ws.add_image(out.getvalue(), "panda.jpg", by="user")


def picture_name(r: Run) -> str | None:
    runs = r.ran("run", by="engineer")
    if not runs:
        return "no program ran"
    if len(r.tools) and len([t for t in r.tools if t["name"] == "run"]) > 3:
        return f"{len([t for t in r.tools if t['name'] == 'run'])} runs (3 or fewer wanted)"
    scripts = [i for i in r.ws.ids("file") if r.ws.entry(i)["name"].endswith(".py")]
    code = " ".join(r.ws.path(i).read_text("utf-8", errors="replace") for i in scripts)
    if "/usr/share/fonts" in code:
        return "looked for Linux fonts"
    if not re.search(r"truetype\(\s*['\"][\w ./-]+\.ttf", code, re.I):
        return "no TrueType font named (the default font is tiny)"
    if not [i for i in r.ws.ids("image") if r.ws.entry(i)["name"] != "panda.jpg"]:
        return "no picture made"
    return None


def calculus(r: Run) -> str | None:
    if r.ran("analyze_or_code"):
        return "sent to the helper"
    if not r.ran("calculate", by="main"):
        return "calculate not used"
    said = " ".join(r.answers)
    if not re.search(r"3\.00[56]", said):
        return "the integral 3.006 is not in the answer"
    if not (re.search(r"1\.57[45]", said) and re.search(r"-?0\.54[34]", said)):
        return "the points -0.5437 and 1.5747 are not both in the answer"
    return None


def stop_continue(r: Run) -> str | None:
    scripts = [i for i in r.ws.ids("file") if r.ws.entry(i)["name"].endswith(".py")]
    if len(scripts) != 1:
        return f"{len(scripts)} program files (one, carried on, wanted)"
    stopped = [m for c in r.chats for m in r.store.messages(c) if m["role"] == "tool"
               and '"status": "stopped"' in str(m.get("content"))]
    if not stopped:
        return "the stopped call kept nothing"
    if not r.ran("run", by="engineer"):
        return "nothing ran after continue"
    return None


def long_program(r: Run) -> str | None:
    full = [t for t in r.tools if t.get("status") == "failed" and t["name"] == "analyze_or_code"]
    if full or any("context" in e for e in r.errors):
        return "the helper failed"
    runs = len([t for t in r.tools if t["name"] == "run"])
    if runs < 3:
        return f"only {runs} run(s): not a long job"
    return None


def leetcode(r: Run) -> str | None:
    searches, longest = 0, 0
    for t in r.tools:
        if t["name"] == "web_search":
            searches += 1
            longest = max(longest, searches)
        elif t["name"] == "read_webpage":
            searches = 0
    if longest > 3 and not any((t.get("input") or {}).get("confirm") for t in r.tools if t["name"] == "web_search"):
        return f"{longest} searches in a row"
    fetched = [t for t in r.tools if t["name"] == "read_webpage" and "leetcode.com" in str((t.get("input") or {}).get("url"))
               and "refused reading earlier in this chat" not in str(t.get("summary"))]
    if len(fetched) > 1:
        return f"leetcode.com fetched {len(fetched)} times"
    if not re.search(r"leetcode", " ".join(r.answers), re.I) or not re.search(
            r"could ?n[o']t|cannot|can'?t|blocked|refus|bot check|not able|unable", " ".join(r.answers), re.I):
        return "the answer doesn't say LeetCode couldn't be read"
    return None


JOBS = [
    Combo("picture_name", "phase2", [Step("Add my name Ary at the top right corner of panda.jpg - big, in a stylish font "
                                          "that looks part of the picture, not just stuck on.")], picture_name,
          setup=_panda),
    Combo("calculus", "phase2", [Step(
        "Solve this step by step. Let f(x) = max(|x - 1|, 2/(1 + x^2)). 1. Find the set S of points where f is not "
        "differentiable. 2. Evaluate the integral of f from the smallest to the largest element of S.")], calculus),
    Combo("stop_continue", "phase2", [Step("Write a Python program that simulates 10,000 rolls of two dice and prints how "
                                           "often each total comes up, then a bar chart of it."),
                                      Step("continue")], stop_continue),
    Combo("long_program", "phase2", [Step(
        "Write a program that reads no input and prints the first 15 prime numbers. Then change it to also print their "
        "sum, then to print them as a table with their index, then to also save the table as primes.csv - run it after "
        "each change so I can see it works.")], long_program),
    Combo("leetcode", "phase2", [Step("Can you go to LeetCode, find the description for the problem 'Two Sum', and copy "
                                      "the exact text inside the Example 1 output block?"),
                                 Step("and give me the full problem description too")], leetcode),
]


def drive_with_stop(combo: Combo, store: Store, main, worker, truth: dict) -> Run:
    """The first message is stopped as soon as the helper's first run ends (the user pressing Stop); the rest go on."""
    ws_id = store.create_workspace(combo.id)["id"]
    run = Run(store, ws_id, truth)
    chat_id = store.create_chat(ws_id)["id"]
    run.chats.append(chat_id)
    for n, step in enumerate(combo.steps):
        events = main_slot.send(store, chat_id, step.say, main, worker)
        if n == 0:
            def stopping(stream):
                for event in stream:
                    if event.get("type") == "worker" and event.get("event") == "result" and event.get("name") == "run":
                        main_slot.stop(chat_id)
                    yield event
            events = stopping(events)
        card = _consume_with_summaries(events, run)
        for _ in range(6):
            if card is None:
                break
            card = _consume_with_summaries(main_slot.reply(store, chat_id, _pick(card, []), main, worker), run)
    return run


def _consume_with_summaries(events, run: Run):
    """As combos._consume, keeping each helper result's summary on its call (the leetcode check reads it)."""
    def tap(stream):
        for event in stream:
            if event.get("type") == "worker" and event.get("event") == "result":
                mine = [t for t in run.tools if t["by"] == event.get("worker") and "status" not in t]
                if mine:
                    mine[-1]["summary"] = event.get("summary", "")
            yield event
    return _consume(tap(events), run)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    jobs = [j for j in JOBS if not args.only or j.id in args.only.split(",")]
    main_model = LlamaCppProvider(settings.llamacpp_model, settings.llamacpp_url, settings.assistant_thinking, slot=0,
                                  max_tokens=settings.assistant_max_tokens)
    worker = LlamaCppProvider(settings.llamacpp_model, settings.llamacpp_url, settings.assistant_thinking, slot=1,
                              max_tokens=settings.assistant_max_tokens)
    embedder = make_embedder(settings)
    from bench import combos
    combos._consume = _consume_with_summaries  # drive() records each helper result's summary too
    rows = []
    for job in jobs:
        folder = Path(tempfile.mkdtemp(prefix=f"zen-phase2-{job.id}-"))
        store = Store(folder / "store", embedder)
        started = time.perf_counter()
        try:
            if job.id == "stop_continue":
                run = drive_with_stop(job, store, main_model, worker, {})
            else:
                run = drive(job, store, main_model, worker, {})
            problem = job.check(run)
        except Exception as exc:  # noqa: BLE001 - a crash is a failed job, reported
            run, problem = None, f"crash: {type(exc).__name__}: {exc}"
        seconds = round(time.perf_counter() - started, 1)
        row = {"id": job.id, "ok": problem is None, "problem": problem, "seconds": seconds,
               "tools": [f"{t['by']}:{t['name']}({t.get('status')})" for t in (run.tools if run else [])],
               "answers": run.answers if run else [], "errors": run.errors if run else [],
               "store": str(folder) if args.keep else None}
        rows.append(row)
        store.close()
        if not args.keep:
            shutil.rmtree(folder, ignore_errors=True)
        print(f"{'PASS' if row['ok'] else 'FAIL'} {job.id:<15} {seconds:>6}s  {problem or ''}", flush=True)
    name = "phase2" + ("_only" if args.only else "")
    (HERE / f"{name}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    lines = [f"# Phase 2 acceptance - {sum(r['ok'] for r in rows)}/{len(rows)} passed", ""]
    for r in rows:
        lines += [f"## {r['id']} - {'PASS' if r['ok'] else 'FAIL: ' + str(r['problem'])} ({r['seconds']} s)", "",
                  "Tools: " + ", ".join(r["tools"]), "", *[f"> {' '.join(a.split())[:600]}" for a in r["answers"]], ""]
    (HERE / f"{name}.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"-> bench/{name}.md: {sum(r['ok'] for r in rows)}/{len(rows)} passed")


if __name__ == "__main__":
    main()
