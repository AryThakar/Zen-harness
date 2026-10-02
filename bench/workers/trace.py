"""Trace real worker runs: every event in order, with timings - what the model was given, what it
called, what each tool returned, the hints and checks, and the result handed to the main slot.

    python -m bench.workers.trace h_forecast k_missing w_llama_port      # or no ids: one per role

Writes bench/workers/traces.md. Unlike the benchmark (pass / fail per task), this shows the flow
itself, so a wrong turn can be seen where it happens.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

from app.agent.providers.llamacpp import LlamaCppProvider
from app.assistant.registry import ToolContext
from app.assistant.workers import PROFILES, run_worker
from app.rag.embed import FastEmbedder
from app.tools.search_index import SearchIndex
from app.tools.workspace import Workspace

from .tasks import TASKS, Run

HERE = Path(__file__).resolve().parent
DEFAULT = ["d_messy_pizzas", "h_forecast", "h_ambiguous", "k_missing", "h_bulk", "w_llama_port", "c_fix", "c_json"]


def trace(task, embedder) -> str:
    root = Path(tempfile.mkdtemp()) / "ws"
    ws = Workspace(root)
    index = SearchIndex(root / "search.sqlite", embedder)
    truth: dict = {}
    task.setup(ws, index, truth)
    shown, events = [], []
    ctx = ToolContext(ws=ws, index=index, conversation="trace", emit=shown.append)
    model = LlamaCppProvider("local", thinking=True, slot=1, max_tokens=4096)
    started = time.perf_counter()
    out = run_worker(model, PROFILES[task.role], task.task, ctx, user_message=task.task,
                     progress=lambda e: events.append((time.perf_counter() - started, e)))
    seconds = time.perf_counter() - started
    tools = [{"name": e["name"], "input": e["input"]} for _, e in events if e["type"] == "worker_tool"]
    passed, detail = task.check(Run(out, tools, shown, ws, truth))
    lines = [f"## {task.id} ({task.role} worker) - {'pass' if passed else 'FAIL'}, {seconds:.1f} s", "",
             f"Task: {task.task}", "", f"Check: {detail}", "", "| s | event | what happened |", "|---|---|---|"]
    for t, e in events:
        lines.append(f"| {t:.1f} | {e['type'].removeprefix('worker_')} | {_cell(_describe(e))} |")
    lines += ["", "Handed to the main slot:", "", "```json", json.dumps(_short(out), indent=1, ensure_ascii=False),
              "```", "", "Shown to the user directly: "
              + (", ".join(f"{a.get('type')} ({a.get('tool')})" for a in shown) or "nothing"), "", ""]
    return "\n".join(lines)


def _describe(e: dict) -> str:
    kind = e["type"]
    if kind == "worker_start":
        return f"task given; room for history: {e['budget']:,} tokens"
    if kind == "worker_turn":
        u = e.get("usage") or {}
        what = f"calls {', '.join(e['calls'])}" if e["calls"] else f"answers in text: {e['text'][:160]!r}"
        cut = f"; {e['shortened']} old results shortened" if e.get("shortened") else ""
        return (f"model step {e['step']}: {what} - read {u.get('prompt_tokens', '?')} new tokens, wrote "
                f"{u.get('output_tokens', '?')} ({u.get('tokens_per_second') or 0:.0f} tok/s){cut}")
    if kind == "worker_tool":
        return f"-> {e['name']} {json.dumps(e['input'], ensure_ascii=False)[:220]}"
    if kind == "worker_result":
        return f"<- {e['status']}, {e['chars']:,} chars: {e['content'][:260]}"
    if kind == "worker_hint":
        return f"hint added: {e['text']}"
    if kind == "worker_check":
        if not e["problems"]:
            return "submission checked: no problems"
        return f"submission checked: {'; '.join(e['problems'])} -> {'one retry' if e['retry'] else 'kept as warnings'}"
    return json.dumps(e, ensure_ascii=False)[:200]


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def _short(out: dict) -> dict:
    return {k: (v[:400] + " ..." if isinstance(v, str) and len(v) > 400 else v) for k, v in out.items()}


def main() -> None:
    ids = sys.argv[1:] or DEFAULT
    tasks = {t.id: t for t in TASKS}
    embedder = FastEmbedder("BAAI/bge-small-en-v1.5", "data/models")
    parts = ["# Worker traces", "", "Real runs on the local model (Gemma 4 E4B, worker slot, thinking on). Each row is "
             "one event, in the order it happened; 'read N new tokens' is what llama.cpp had to process (the rest "
             "came from its prompt cache).", ""]
    for tid in ids:
        parts.append(trace(tasks[tid], embedder))
        print(f"traced {tid}", flush=True)
    (HERE / "traces.md").write_text("\n".join(parts), encoding="utf-8")
    print("-> bench/workers/traces.md")


if __name__ == "__main__":
    main()
