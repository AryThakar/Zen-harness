"""Every Mermaid diagram the model has written in the user's chats - draw_diagram calls and ```mermaid
blocks in answers - before and after draw_diagram's mending, for checking against the real renderer.

    python -m bench.tools.mermaid_corpus     -> bench/tools/mermaid_corpus.json

The check itself runs in the UI's page (Mermaid is a browser library): each case's code is parsed
with the page's mermaid.parse, as the UI would draw it. Read-only on the chat store.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from app.config import settings
from app.tools.core import ToolError
from app.tools.diagram import _unfence, mend

HERE = Path(__file__).resolve().parent
BLOCK = re.compile(r"```mermaid\s*\n(.*?)```", re.S)


def collect(db_path: Path) -> list[dict]:
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cases, seen = [], set()
    for chat, role, body in db.execute("SELECT chat_id, role, body FROM messages ORDER BY id"):
        message = {"role": role, **json.loads(body)}
        codes = []
        if message.get("role") == "assistant":
            codes += [("call", c["input"].get("mermaid", "")) for c in message.get("tool_calls") or []
                      if c.get("name") == "draw_diagram" and isinstance(c.get("input"), dict)]
            codes += [("answer", b) for b in BLOCK.findall(message.get("text") or "")]
        for source, code in codes:
            key = " ".join(code.split())
            if not key or key in seen:
                continue
            seen.add(key)
            try:
                kind, mended = mend(_unfence(code))
                problem = None
            except ToolError as exc:
                kind, mended, problem = None, None, exc.message
            cases.append({"chat": chat, "source": source, "kind": kind, "code": code, "mended": mended,
                          "refused": problem})
    return cases


def main() -> None:
    cases = collect(Path(settings.assistant_dir) / "assistant.sqlite")
    (HERE / "mermaid_corpus.json").write_text(json.dumps(cases, ensure_ascii=False, indent=1), encoding="utf-8")
    kinds = {}
    for c in cases:
        kinds[c["kind"] or "refused"] = kinds.get(c["kind"] or "refused", 0) + 1
    print(len(cases), "diagrams:", kinds)


if __name__ == "__main__":
    main()
