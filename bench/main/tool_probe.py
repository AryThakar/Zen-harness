"""Main's tools, asked of the real model through the real turn (app.assistant.main.send): the same
instructions, tools, helpers and checks as in the app. Per case: every tool that ran, in order - main's
own and its helpers' - with its input and status, and the reply; the case's check (bench/main/suites.py)
says whether the turn did what it should.

    python -m bench.main.tool_probe SUITE [label] [--runs N] [--only id,id] [--old] [--helpers]
        -> bench/main/probe_<suite>_<label>.md and .json        (llama-server running)

Main and its helpers both run on the worker slot, so the app's main slot stays free for the user.
generate_image is a stand-in that records the prompt (each real image costs Pollen). The helpers
(research_web, ask_knowledge, analyze_data, write_code) are stand-ins too, unless --helpers: then the
real workers run, for end-to-end cases. --old runs the suite with the wording it had before a change
(suites.OLD), to measure the change against it.
"""

from __future__ import annotations

import argparse
import os
import dataclasses
import json
import tempfile
import time
from datetime import datetime
from pathlib import Path

from app.providers.llamacpp import LlamaCppProvider
from app.assistant import main as main_slot
from app.assistant import workers
from app.assistant.registry import TOOLS
from app.assistant.store import Store
from app.config import settings
from app.rag.embed import make_embedder
from app.tools.core import ok

from .suites import OLD, SUITES, Case, Seen

HERE = Path(__file__).resolve().parent


def _image(ctx, args):
    """generate_image without the service, answered like the real tool."""
    item = ctx.ws.add_bytes("image", b"stand-in", "png", name=args.prompt[:80], meta={"prompt": args.prompt})
    return ok({"image": item, "width": 1024, "height": 1024, "summary": "The image was generated and is shown to the user."},
              {"type": "image", "id": item, "prompt": args.prompt})


def _helper(provider, profile, task, ctx, **kwargs) -> dict:
    """A helper that is not run: the task main gave it is what is measured."""
    return {"status": "ok", "worker": profile.name,
            "answer": "(Not run in this test. Tell the user what you asked the helper to do.)"}


def run(case: Case, store: Store, provider: LlamaCppProvider) -> Seen:
    ws_id = store.create_workspace("probe")["id"]
    chat_id = store.create_chat(ws_id)["id"]
    if case.setup:
        case.setup(store, ws_id)
    for user, assistant in case.history:
        store.append(chat_id, {"role": "user", "content": f"{main_slot.stamp()} {user}", "text": user})
        store.append(chat_id, {"role": "assistant", "text": assistant, "tool_calls": []})
    seen = Seen(case.say, datetime.now().astimezone())
    calls: dict[str, dict] = {}
    for event in main_slot.send(store, chat_id, case.say, provider, provider):
        kind = event["type"]
        if kind == "tool_start":
            calls[event["id"]] = {"by": "main", "name": event["name"], "input": event["input"]}
            seen.tools.append(calls[event["id"]])
        elif kind == "tool_end" and event["id"] in calls:
            calls[event["id"]]["status"] = event["status"]
        elif kind == "worker" and event["event"] == "tool":
            seen.tools.append({"by": event["worker"], "name": event["name"], "input": event["input"]})
        elif kind == "worker" and event["event"] == "result":
            mine = [t for t in seen.tools if t["by"] == event["worker"] and "status" not in t]
            if mine:
                mine[-1]["status"] = event["status"]
        elif kind == "artifact":
            seen.artifacts.append(event["artifact"])
        elif kind == "question":
            seen.asked = {k: event.get(k) for k in ("question", "options", "via", "approval")}
            calls.get(event.get("call_id"), {}).setdefault("status", "asked")
        elif kind == "done":
            seen.answer = event["answer"]
        elif kind in ("error", "stopped"):
            seen.error = event.get("message", kind)
    seen.ws = store.open(ws_id)[0]
    # The hidden checks that fired (checks.py), each with the reply it answered: to measure whether a check
    # caught a real failure or misfired (SYSTEM_PLAN section 9).
    said = store.messages(chat_id, for_model=True)
    seen.nudges = [{"nudge": str(m.get("content", ""))[:90], "after": str(prev.get("text") or "")[:400]}
                   for prev, m in zip(said, said[1:]) if m.get("check")]
    return seen


def brief(tool: dict) -> str:
    args = json.dumps(tool.get("input"), ensure_ascii=False)
    who = "" if tool["by"] == "main" else f"{tool['by']}:"
    return f"{who}{tool['name']}({args[:140] + ('…' if len(args) > 140 else '')}) {tool.get('status', '?')}"


def report(suite: str, label: str, rows: list[dict]) -> Path:
    cases = list(dict.fromkeys(r["case"] for r in rows))
    passed = sum(r["ok"] for r in rows)
    out = [f"# {suite}: {label}", "", f"Passed {passed}/{len(rows)} runs · "
           + " · ".join(f"{c} {sum(r['ok'] for r in rows if r['case'] == c)}/{sum(r['case'] == c for r in rows)}"
                        for c in cases), "",
           "| case | run | tools | check | s | reply |", "|---|---|---|---|---|---|"]
    cell = lambda s: " ".join(str(s).split()).replace("|", "\\|")
    for r in rows:
        out.append(f"| {r['case']} | {r['run']} | {cell('; '.join(r['tools'])) or '-'} | "
                   f"{'ok' if r['ok'] else '**' + cell(r['problem']) + '**'} | {r['seconds']} | {cell(r['answer'])[:240]} |")
    path = HERE / f"probe_{suite}_{label}.md"
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    (HERE / f"probe_{suite}_{label}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("suite", choices=sorted(SUITES))
    ap.add_argument("label", nargs="?", default="run")
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--only")
    ap.add_argument("--old", action="store_true")
    ap.add_argument("--helpers", action="store_true")
    args = ap.parse_args()
    if args.old:
        OLD[args.suite]()
    TOOLS["generate_image"] = dataclasses.replace(TOOLS["generate_image"], handler=_image)
    if not args.helpers:
        workers.run_worker = _helper
    provider = LlamaCppProvider(settings.llamacpp_model, settings.llamacpp_url, settings.assistant_thinking, slot=int(os.environ.get("PROBE_SLOT", "1")),
                                max_tokens=settings.assistant_max_tokens)
    embedder = make_embedder(settings)  # the app's own search model
    cases = [c for c in SUITES[args.suite] if not args.only or c.id in args.only.split(",")]
    rows = []
    for case in cases:
        for n in range(1, args.runs + 1):
            store = Store(Path(tempfile.mkdtemp()) / "store", embedder)
            started = time.perf_counter()
            try:
                seen = run(case, store, provider)
                problem = case.check(seen) or (f"turn failed: {seen.error}" if seen.error else None)
            except Exception as exc:  # noqa: BLE001 - a crash is a failed case, reported
                seen, problem = Seen(case.say, datetime.now()), f"crash: {type(exc).__name__}: {exc}"
            finally:
                store.close()
            row = {"case": case.id, "run": n, "ok": problem is None, "problem": problem,
                   "seconds": round(time.perf_counter() - started, 1), "tools": [brief(t) for t in seen.tools],
                   "calls": seen.tools, "asked": seen.asked, "answer": seen.answer,
                   "nudges": getattr(seen, "nudges", [])}
            rows.append(row)
            print(f"{case.id:<16} run{n} {'ok  ' if row['ok'] else 'FAIL'} {row['seconds']:>6}s  "
                  f"{'; '.join(row['tools'])[:110]}  {problem or ''}", flush=True)
    print("->", report(args.suite, args.label, rows).relative_to(HERE.parent.parent))


if __name__ == "__main__":
    main()
