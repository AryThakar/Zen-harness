# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""How long does the model think? Replays the user's real messages through main with thinking
unlimited and counts each reply's thinking, to set the thinking budget from data, not a guess.

    python -m bench.main.thinking_probe CHAT_ID [CHAT_ID ...] [--max-tokens 6144]

For every user message of the chats (in order), main's first call is made at that exact point: the
stored history up to and including the message (what main saw then, cut to the slot like the app
does), the current main prompt and tools. Nothing is executed and nothing is stored. The thinking is
counted in tokens by llama-server's own tokenizer. Writes bench/main/thinking_probe.json and .md.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.request
from pathlib import Path

from app.providers.llamacpp import LlamaCppProvider
from app.assistant.context import fit_turns, prompt_limit
from app.assistant.main import MAIN
from app.assistant.registry import ToolContext
from app.assistant.store import Store
from app.assistant.workers import main_tools
from app.config import Settings

HERE = Path(__file__).resolve().parent


def tokens(text: str, url: str) -> int:
    if not text:
        return 0
    body = json.dumps({"content": text, "add_special": False}).encode()
    request = urllib.request.Request(f"{url}/tokenize", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as r:
        return len(json.loads(r.read())["tokens"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("chats", nargs="+")
    ap.add_argument("--max-tokens", type=int, default=6144)
    args = ap.parse_args()
    settings = Settings()
    store = Store(settings.assistant_dir)
    model = LlamaCppProvider(settings.llamacpp_model, settings.llamacpp_url, thinking=True, slot=0,
                             max_tokens=args.max_tokens, thinking_budget=None)
    rows = []
    for chat_id in args.chats:
        chat = store.chat(chat_id)
        ws, index = store.open(chat["workspace_id"])
        ctx = ToolContext(ws=ws, index=index, conversation=chat_id)
        specs = [t.spec() for t in main_tools(model, ctx).values()]
        history = [m for m in store.messages(chat_id, for_model=True) if m["role"] in ("user", "assistant", "tool")]
        count = lambda msgs: model.count_tokens(MAIN, msgs, specs)  # noqa: E731
        for i, m in enumerate(history):
            if m["role"] != "user" or m.get("hidden"):
                continue
            visible, _ = fit_turns(history[:i + 1], prompt_limit(model), count)
            started = time.perf_counter()
            turn = model.complete(MAIN, visible, specs)
            row = {"chat": chat_id, "message": m.get("id"), "said": (m.get("text") or "")[:120],
                   "thinking": tokens(turn.thinking, settings.llamacpp_url),
                   "output": (turn.usage or {}).get("output_tokens"), "stop": turn.stop,
                   "called": [c["name"] for c in turn.tool_calls], "answered": bool(turn.text.strip()),
                   "seconds": round(time.perf_counter() - started, 1)}
            rows.append(row)
            print(f"{row['thinking']:5} thinking / {row['output']} out  {row['stop']:9} {row['called'] or ''} "
                  f"{row['seconds']:5}s  {row['said'][:70]!r}", flush=True)
    (HERE / "thinking_probe.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False), "utf-8")
    (HERE / "thinking_probe.md").write_text(report(rows, args.max_tokens), "utf-8")


def report(rows: list[dict], limit: int) -> str:
    t = sorted(r["thinking"] for r in rows)

    def pct(p: float) -> int:
        return t[min(len(t) - 1, int(round(p * (len(t) - 1))))]

    cut = [r for r in rows if r["stop"] == "max_tokens"]
    lines = ["# How long main thinks (thinking unlimited)", "",
             f"{len(rows)} replies, the user's real messages replayed at their point in the chat; reply limit "
             f"{limit} tokens.", "",
             f"Thinking tokens: median {statistics.median(t):.0f}, 75% {pct(.75)}, 90% {pct(.9)}, 95% {pct(.95)}, "
             f"max {t[-1]}.", f"Ran into the reply limit: {len(cut)}.", "",
             "| thinking | output | stop | called | seconds | message |", "|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: -r["thinking"]):
        lines.append(f"| {r['thinking']} | {r['output']} | {r['stop']} | {', '.join(r['called'])} | {r['seconds']} | "
                     f"{r['said'][:80].replace('|', '/')} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
