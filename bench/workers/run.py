"""Run the worker benchmark against the local model (llama-server, worker slot 1, thinking on).

    python -m bench.workers.run dev            # or: holdout, all; optional role filter: dev data
    python -m bench.workers.run dev --only d_total,d_chart
    python -m bench.workers.run guide --repeat 2   # the harder data tasks (GUIDE_TASKS)

Writes bench/workers/results_<split>.md and .json. Every task gets a fresh workspace.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

from app.providers.llamacpp import THINKING_BUDGET, LlamaCppProvider
from app.assistant.registry import ToolContext
from app.assistant.workers import PROFILES, run_worker
from app.rag.embed import FastEmbedder
from app.tools.search_index import SearchIndex
from app.tools.workspace import Workspace

from .tasks import TASKS, Run

HERE = Path(__file__).resolve().parent


class Counting:
    """Wraps the provider to count model calls, generated tokens and each call's thinking tokens
    (llama-server's own tokenizer)."""

    def __init__(self, inner):
        self.inner, self.name, self.model = inner, inner.name, inner.model
        self.calls, self.tokens, self.thinking = 0, 0, []

    def __getattr__(self, name):  # max_tokens, count_tokens ... from the real provider
        return getattr(self.inner, name)

    def complete(self, *args, **kwargs):
        self.calls += 1
        turn = self.inner.complete(*args, **kwargs)
        self.tokens += (turn.usage or {}).get("output_tokens") or 0
        self.thinking.append(_tokens(turn.thinking, self.inner.base_url))
        return turn


def _tokens(text: str, url: str) -> int:
    if not text:
        return 0
    import urllib.request
    body = json.dumps({"content": text, "add_special": False}).encode()
    request = urllib.request.Request(f"{url}/tokenize", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as r:
        return len(json.loads(r.read())["tokens"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("split", choices=["dev", "holdout", "all", "guide", "fresh"])
    ap.add_argument("role", nargs="?")
    ap.add_argument("--only")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--thinking-budget", type=int, default=None,
                    help="thinking tokens per reply (0 = unlimited); default: the app's setting")
    ap.add_argument("--max-tokens", type=int, default=4096)
    args = ap.parse_args()
    tasks = [t for t in TASKS if ((args.split == "all" and t.split in ("dev", "holdout")) or t.split == args.split)
             and (not args.role or t.role == args.role) and (not args.only or t.id in args.only.split(","))]
    tasks = [t for t in tasks for _ in range(args.repeat)]
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
        budget = THINKING_BUDGET if args.thinking_budget is None else (args.thinking_budget or None)
        model = Counting(LlamaCppProvider("local", thinking=True, slot=1, max_tokens=args.max_tokens,
                                          thinking_budget=budget))
        profile = PROFILES[task.role]
        started = time.perf_counter()
        try:
            out = run_worker(model, profile, task.task, ctx, user_message=task.task, file=task.file,
                             progress=lambda e: e["type"] == "worker_tool" and tools.append(
                                 {"name": e["name"], "input": e["input"]}))
        except Exception as exc:  # noqa: BLE001 - a crash is a failed task, reported
            out = {"status": "crash", "answer": "", "warnings": [f"{type(exc).__name__}: {exc}"]}
        seconds = time.perf_counter() - started
        passed, detail = task.check(Run(out, tools, artifacts, ws, truth))
        rows.append({"id": task.id, "role": task.role, "split": task.split, "passed": bool(passed), "detail": detail,
                     "seconds": round(seconds, 1), "model_calls": model.calls, "tokens": model.tokens,
                     "tools": [t["name"] for t in tools], "calls": tools, "warnings": out.get("warnings", []),
                     "thinking": model.thinking,
                     "answer": (out.get("answer") or "")[:3000], "sources": out.get("sources"),
                     "question_for_user": out.get("question_for_user"),
                     **{k: out[k] for k in ("suggestion", "trail", "note", "file", "report") if out.get(k)},
                     **({"approval": {k: v for k, v in out["approval_card"]["approval"].items() if k != "content"}}
                        if out.get("approval_card") else {})})
        mark = "PASS" if passed else "FAIL"
        print(f"{mark} {task.id:18s} {seconds:6.1f}s calls={model.calls:2d} tools={[t['name'] for t in tools]} | {detail}",
              flush=True)
    name = (f"results_{args.split}" + (f"_{args.role}" if args.role else "")
            + (f"_{args.only.replace(',', '+')}" if args.only else "") + ""
            + (f"_think{args.thinking_budget}" if args.thinking_budget is not None else ""))
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
