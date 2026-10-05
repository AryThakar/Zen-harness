"""How much of main's prompt llama.cpp has to read again each turn: the old layout (workspace listing
and pinned notes in the system prompt) against the new one (a static system prompt; the workspace
and notes appended to the user's messages, app/assistant/context.py). The same 6-turn chat both
ways; the workspace changes between turns (a table after turn 2, a note after turn 3, a file after
turn 4). Numbers are llama-server's own: prompt_n (read) and cache_n (reused) per request.

    python -m bench.main.cache_probe        (llama-server running; worker slot) -> bench/main/cache_probe.md
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd

from app.providers.llamacpp import LlamaCppProvider
from app.assistant.context import message_context
from app.assistant.main import MAIN, stamp
from app.assistant.registry import ToolContext
from app.assistant.workers import main_tools
from app.config import settings
from app.tools.notes import pinned_text, save_note
from app.tools.workspace import Workspace

HERE = Path(__file__).resolve().parent
TURNS = 6
REPLY = ("Here is a careful answer with a few details. " * 40).strip()  # ~400 tokens of history per turn


def change(ws: Workspace, turn: int) -> None:
    if turn == 2:
        ws.add_table(pd.DataFrame({"month": range(12), "sales": range(12)}), "sales.csv")
    elif turn == 3:
        save_note(ws, "Fiscal year", "The fiscal year starts in April.")
    elif turn == 4:
        ws.add_bytes("file", b"print(1)", "py", name="report.py")


def run(layout: str, provider: LlamaCppProvider) -> list[dict]:
    ws = Workspace(Path(tempfile.mkdtemp()) / "ws")
    specs = [t.spec() for t in main_tools(provider, ToolContext(ws=ws, index=None)).values()]
    history, before, rows = [], None, []
    for turn in range(1, TURNS + 1):
        said = f"{stamp()} question {turn}: how are the numbers looking?"
        if layout == "old":
            system = f"{MAIN}\n\n" + "\n\n".join(p for p in (ws.listing(), pinned_text(ws)) if p)
            content = said
        else:
            system = MAIN
            note, before = message_context(ws, before)
            content = f"{said}\n\n{note}" if note else said
        history.append({"role": "user", "content": content})
        reply = provider.complete(system, history, specs, allow_tools=False)
        usage = reply.usage or {}
        rows.append({"turn": turn, "read": usage.get("prompt_tokens"), "reused": usage.get("cached_tokens")})
        history.append({"role": "assistant", "text": REPLY})
        change(ws, turn)
    return rows


def main() -> None:
    provider = LlamaCppProvider(settings.llamacpp_model, settings.llamacpp_url, thinking=False, slot=1, max_tokens=8)
    results = {layout: run(layout, provider) for layout in ("old", "new")}
    out = ["# Prompt cache: old layout vs new", "",
           "Tokens llama-server read again (prompt_n) and reused from its cache (cache_n), per request.", "",
           "| turn | what changed before it | old: read | old: reused | new: read | new: reused |",
           "|---|---|---|---|---|---|"]
    changed = {3: "a table added", 4: "a note saved", 5: "a file added"}
    for old, new in zip(results["old"], results["new"]):
        out.append(f"| {old['turn']} | {changed.get(old['turn'], '-')} | {old['read']} | {old['reused']} | "
                   f"{new['read']} | {new['reused']} |")
    for layout, rows in results.items():
        later = rows[1:]  # the first request is cold in both
        out.append(f"\n{layout}: read {sum(r['read'] or 0 for r in later):,} tokens over turns 2-{TURNS}")
    (HERE / "cache_probe.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    print("\n".join(out))


if __name__ == "__main__":
    main()
