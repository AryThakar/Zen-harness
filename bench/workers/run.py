"""Run the worker benchmark against the local model (llama-server, worker slot 1, thinking on).

    python -m bench.workers.run dev            # or: holdout, all; optional role filter: dev data
    python -m bench.workers.run dev --only d_total,d_chart

Writes bench/workers/results_<split>.md and .json. Every task gets a fresh workspace.
"""

from __future__ import annotations

import argparse
import json
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


class Counting:
    """Wraps the provider to count model calls and generated tokens."""

    def __init__(self, inner):
        self.inner, self.name, self.model = inner, inner.name, inner.model
        self.calls, self.tokens = 0, 0

    def complete(self, *args, **kwargs):
        self.calls += 1
        turn = self.inner.complete(*args, **kwargs)
        self.tokens += (turn.usage or {}).get("output_tokens") or 0
        return turn


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("split", choices=["dev", "holdout", "all"])
    ap.add_argument("role", nargs="?")
    ap.add_argument("--only")
    args = ap.parse_args()
    tasks = [t for t in TASKS if (args.split == "all" or t.split == args.split) and (not args.role or t.role == args.role)
             and (not args.only or t.id in args.only.split(","))]
    embedder = FastEmbedder("BAAI/bge-small-en-v1.5", "data/models")
    rows = []
    for task in tasks:
        root = Path(tempfile.mkdtemp()) / "ws"
        ws = Workspace(root)
        index = SearchIndex(root / "search.sqlite", embedder)
        truth: dict = {}
        task.setup(ws, index, truth)
        artifacts, tools = [], []
        ctx = ToolContext(ws=ws, index=index, conversation="bench", emit=artifacts.append)
        model = Counting(LlamaCppProvider("local", thinking=True, slot=1, max_tokens=4096))
        started = time.perf_counter()
        try:
            out = run_worker(model, PROFILES[task.role], task.task, ctx, user_message=task.task,
                             progress=lambda e: tools.append({"name": e["name"], "input": e["input"]}))
        except Exception as exc:  # noqa: BLE001 - a crash is a failed task, reported
            out = {"status": "crash", "answer": "", "warnings": [f"{type(exc).__name__}: {exc}"]}
        seconds = time.perf_counter() - started
        passed, detail = task.check(Run(out, tools, artifacts, ws, truth))
        rows.append({"id": task.id, "role": task.role, "split": task.split, "passed": bool(passed), "detail": detail,
                     "seconds": round(seconds, 1), "model_calls": model.calls, "tokens": model.tokens,
                     "tools": [t["name"] for t in tools], "calls": tools, "warnings": out.get("warnings", []),
                     "answer": (out.get("answer") or "")[:600], "sources": out.get("sources"),
                     "question_for_user": out.get("question_for_user")})
        mark = "PASS" if passed else "FAIL"
        print(f"{mark} {task.id:18s} {seconds:6.1f}s calls={model.calls:2d} tools={[t['name'] for t in tools]} | {detail}",
              flush=True)
    name = f"results_{args.split}" + (f"_{args.role}" if args.role else "") + (f"_{args.only.replace(',', '+')}" if args.only else "")
    (HERE / f"{name}.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    (HERE / f"{name}.md").write_text(report(rows, args.split), encoding="utf-8")
    print(f"\n{sum(r['passed'] for r in rows)}/{len(rows)} passed -> {name}.md")


def report(rows: list[dict], split: str) -> str:
    lines = [f"# Worker benchmark - {split}", "",
             "Local model: Gemma 4 E4B (Q4_K_XL + MTP draft) on llama-server, worker slot, thinking on, "
             "RTX 3050 Laptop 4 GB.", "", "| role | passed | median seconds | model calls |", "|---|---|---|---|"]
    for role in dict.fromkeys(r["role"] for r in rows):
        rr = [r for r in rows if r["role"] == role]
        secs = sorted(r["seconds"] for r in rr)
        lines.append(f"| {role} | {sum(r['passed'] for r in rr)}/{len(rr)} | {secs[len(secs) // 2]} | "
                     f"{sum(r['model_calls'] for r in rr) / len(rr):.1f} avg |")
    lines += ["", f"**Total: {sum(r['passed'] for r in rows)}/{len(rows)}**", "", "| task | result | seconds | tools | detail |",
              "|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['id']} | {'pass' if r['passed'] else '**fail**'} | {r['seconds']} | {', '.join(r['tools'])} | "
                     f"{r['detail']}{'; warnings: ' + '; '.join(r['warnings']) if r['warnings'] else ''} |")
    lines += ["", "## Answers", ""]
    for r in rows:
        lines += [f"**{r['id']}** ({'pass' if r['passed'] else 'fail'}): {r['answer']}".replace("\n", " "), ""]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
