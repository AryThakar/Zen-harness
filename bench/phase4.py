"""Phase 4's acceptance on the real model (docs/history/REBUILD_PLAN.md, section 5): finding and memory, through the real turn -
the same prompts, tools, helpers and checks as the app - each from a new store in a temporary folder.

    lion_count  "How many lion images do I have?" over the 39 pictures of the user's Q1 sales project (copied, read
                only): 6, from one files(find) call - no paging through files(list), no helper (measured before: 2
                of 4 pages read, answered 2)
    pinned_doc  a store policy pinned to the chat, then a question on one of its sections: answered without
                ask_knowledge or any helper - the snapshot of the pinned file had the passage
    remember    "remember our fiscal year starts on 1 April" edits agent.md; a new chat then knows it with no tool
                (agent.md is in view in every chat)

Each turn's prompt tokens and those the model server took from its cache are kept with the results: agent.md sits in
main's system prompt, so a turn after the first should reuse the cached start of the prompt.

    python -m bench.phase4 [--only id,id] [--keep]   (llama-server on BIZ_LLAMACPP_URL, 2 slots)
        -> bench/phase4.md and .json
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import tempfile
import time
from pathlib import Path

from app.assistant import main as main_slot
from app.assistant.store import Store
from app.config import settings
from app.providers.llamacpp import LlamaCppProvider
from app.rag.embed import make_embedder
from app.tools import agent_md

from .combos import Combo, Run, Step, _consume, _pick, drive

HERE = Path(__file__).resolve().parent
PICTURES_FROM = Path(settings.assistant_dir) / "workspaces" / "bd59fd24c282"  # the user's Q1 sales project
LIONS = 6  # its pictures that show a lion: five by their prompts, and image_20.png (a winged lion; looked at) by what it shows


def _pictures(store: Store, ws_id: str, truth: dict) -> None:
    """The Q1 sales project's pictures, as they are there: generated ones with their prompts, pasted ones without -
    read from its registry, never changed."""
    ws, _ = store.open(ws_id)
    registry = json.loads((PICTURES_FROM / ".zen" / "registry.json").read_text("utf-8"))
    for e in registry["items"].values():
        if e["kind"] == "image":
            meta = {k: v for k, v in (e.get("meta") or {}).items() if k in ("prompt", "caption")}
            ws.put(e["name"], (PICTURES_FROM / e["file"]).read_bytes(), kind="image", by=e.get("by", "user"), meta=meta)
    truth["pictures"] = len(ws.ids("image"))


def lion_count(r: Run) -> str | None:
    calls = [t for t in r.tools if t["by"] == "main"]
    finds = [t for t in calls if t["name"] == "files" and (t.get("input") or {}).get("mode") == "find"]
    if any(t["name"] == "files" and (t.get("input") or {}).get("mode") == "list" for t in calls):
        return "paged through files(list)"
    if any(t["by"] != "main" for t in r.tools):
        return "a helper was used"
    if len(finds) != 1:
        return f"{len(finds)} find call(s) (one wanted)"
    if not re.search(rf"\b({LIONS}|five)\b", " ".join(r.answers), re.I):
        return f"the answer doesn't say {LIONS}"
    return None


POLICY = """# Store policy

How the shop handles returns, repairs and deliveries, for staff answering customers.

# Returns of phones

Customers may return an opened phone within 14 days if the box and every accessory are complete. Opened phones carry
a restocking fee of 10% of the price paid. Phones with a cracked screen or water damage are not taken back; staff
offer the repair service instead. A receipt or the order number is needed for every return.

# Returns of laptops

Opened laptops may come back within 30 days. The restocking fee for an opened laptop is 8% of the price paid, and it
is waived when the laptop arrived faulty. The laptop must be reset to factory settings, with the charger and the
original box. Business customers with an account return through their account manager instead.

# Warranty and repairs

Every device has a one-year warranty from the day of purchase. Repairs under warranty are free and take up to 10
working days; a loan phone is offered when the repair takes longer than 5 days. Out of warranty, staff give a quote
before any work starts, and the customer pays for the quote only when they decline the repair.

# Deliveries

Orders over 500 rupees ship free. Standard delivery takes 3 to 5 working days, express 1 to 2 days for 150 rupees.
A delivery that fails twice goes back to the warehouse, and the customer is refunded less the delivery charge.
"""


def _policy(store: Store, ws_id: str, truth: dict) -> None:
    from app.tools.file_ops import files
    ws, index = store.open(ws_id)
    truth["doc"] = files(ws, index, "write", name="store-policy.md", content=POLICY).result["file"]


def pinned_doc(r: Run) -> str | None:
    used = {t["name"] for t in r.tools if t["by"] == "main"}
    if used & {"ask_knowledge", "research_web", "analyze_or_code"}:
        return f"a helper was used: {', '.join(sorted(used & {'ask_knowledge', 'research_web', 'analyze_or_code'}))}"
    if not re.search(r"\b8\s?%|\beight per ?cent", " ".join(r.answers), re.I):
        return "the answer doesn't give the 8% fee"
    return None


def remember(r: Run) -> str | None:
    if not re.search(r"April", agent_md.body(r.ws)):
        return "agent.md doesn't hold the fiscal year"
    if len(r.answers) < 2 or not re.search(r"April", r.answers[1]):
        return "the new chat didn't know it"
    second = [t for t in r.tools if t.get("chat") == r.chats[-1]]
    if second:
        return f"the new chat used tools: {', '.join(t['name'] for t in second)}"
    return None


def drive_pinned(combo: Combo, store: Store, main, worker, truth: dict) -> Run:
    """The policy pinned to the chat before the question - the user's pin from the file panel."""
    ws_id = store.create_workspace(combo.id)["id"]
    run = Run(store, ws_id, truth)
    combo.setup(store, ws_id, truth)
    chat_id = store.create_chat(ws_id)["id"]
    store.update_chat(chat_id, pins=[truth["doc"]])
    run.chats.append(chat_id)
    for step in combo.steps:
        card = _consume(main_slot.send(store, chat_id, step.say, main, worker), run)
        for _ in range(6):
            if card is None:
                break
            card = _consume(main_slot.reply(store, chat_id, _pick(card, []), main, worker), run)
    return run


def drive_tagged(combo: Combo, store: Store, main, worker, truth: dict) -> Run:
    """As drive, each call tagged with the chat it was made in (the remember job checks the second chat's)."""
    ws_id = store.create_workspace(combo.id)["id"]
    run = Run(store, ws_id, truth)
    chat_id = None
    for step in combo.steps:
        if chat_id is None or step.new_chat:
            chat_id = store.create_chat(ws_id)["id"]
            run.chats.append(chat_id)
        before = len(run.tools)
        card = _consume(main_slot.send(store, chat_id, step.say, main, worker), run)
        for _ in range(6):
            if card is None:
                break
            card = _consume(main_slot.reply(store, chat_id, _pick(card, []), main, worker), run)
        for t in run.tools[before:]:
            t["chat"] = chat_id
    return run


JOBS = [
    Combo("lion_count", "phase4", [Step("How many lion images do I have?")], lion_count, setup=_pictures),
    Combo("pinned_doc", "phase4", [Step("A customer opened a laptop and wants to return it. What restocking fee do we "
                                        "charge?")], pinned_doc, setup=_policy),
    Combo("remember", "phase4", [Step("Remember that our fiscal year starts on 1 April."),
                                 Step("When does our fiscal year start?", new_chat=True)], remember),
]
DRIVERS = {"pinned_doc": drive_pinned, "remember": drive_tagged}


def _cache(store: Store, run: Run) -> list[dict]:
    """Each main step's prompt tokens and those taken from the server's cache, by chat and turn."""
    out = []
    for chat in run.chats:
        turn = 0
        for m in store.messages(chat):
            if m["role"] == "trace":
                turn += 1
                for s in m["trace"].get("steps", []):
                    u = s.get("usage") or {}
                    out.append({"chat": run.chats.index(chat) + 1, "turn": turn, "new": u.get("prompt_tokens"),
                                "cached": u.get("cached_tokens")})
    return out


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
    rows = []
    for job in jobs:
        folder = Path(tempfile.mkdtemp(prefix=f"zen-phase4-{job.id}-"))
        store = Store(folder / "store", embedder)
        started = time.perf_counter()
        run, cache = None, []
        try:
            run = DRIVERS.get(job.id, drive)(job, store, main_model, worker, {})
            problem = job.check(run)
            cache = _cache(store, run)
        except Exception as exc:  # noqa: BLE001 - a crash is a failed job, reported
            problem = f"crash: {type(exc).__name__}: {exc}"
        seconds = round(time.perf_counter() - started, 1)
        rows.append({"id": job.id, "ok": problem is None, "problem": problem, "seconds": seconds,
                     "tools": [f"{t['by']}:{t['name']}({(t.get('input') or {}).get('mode', '')}{t.get('status')})"
                               for t in (run.tools if run else [])],
                     "answers": run.answers if run else [], "errors": run.errors if run else [], "cache": cache,
                     "store": str(folder) if args.keep else None})
        store.close()
        if not args.keep:
            shutil.rmtree(folder, ignore_errors=True)
        print(f"{'PASS' if problem is None else 'FAIL'} {job.id:<12} {seconds:>6}s  {problem or ''}", flush=True)
    name = "phase4" + ("_only" if args.only else "")
    (HERE / f"{name}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    lines = [f"# Phase 4 acceptance - {sum(r['ok'] for r in rows)}/{len(rows)} passed", ""]
    for r in rows:
        lines += [f"## {r['id']} - {'PASS' if r['ok'] else 'FAIL: ' + str(r['problem'])} ({r['seconds']} s)", "",
                  f"Tools: {', '.join(r['tools']) or 'none'}", "",
                  "Prompt tokens per step (new / from cache): " + "; ".join(
                      f"chat {c['chat']} turn {c['turn']}: {c['new']} / {c['cached']}" for c in r["cache"]), ""]
        lines += [f"> {' '.join(a.split())[:700]}" for a in r["answers"]] + [""]
    (HERE / f"{name}.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"-> bench/{name}.md: {sum(r['ok'] for r in rows)}/{len(rows)} passed")


if __name__ == "__main__":
    main()
