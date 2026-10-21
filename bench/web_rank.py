# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Should web_search reorder its results by meaning? Measured on the user's own stored searches: a result
is useful when the answer that followed cited its page. Compared: the engines' own order, meaning alone
(the query against each result's title and snippet), and both fused by rank (RRF, as the search index does).

The data leans to the engines' order: the helper mostly read the top results, so pages lower down were
seldom read and could never be cited. A reorder has to win despite that to be adopted.

Run: .venv/Scripts/python.exe bench/web_rank.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import settings  # noqa: E402
from app.rag.embed import make_embedder  # noqa: E402
from app.tools.web import page_key  # noqa: E402

RRF_K = 60


def searches() -> list[tuple[str, list[dict], set[str]]]:
    """(query, results, cited page keys) for each stored search followed by a cited answer."""
    db = sqlite3.connect(f"file:{ROOT / 'data/assistant/assistant.sqlite'}?mode=ro", uri=True)
    out, pending, chat_of = [], [], None
    for chat, body in db.execute("SELECT chat_id, body FROM messages WHERE role = 'artifact' ORDER BY id"):
        if chat != chat_of:
            pending, chat_of = [], chat
        a = json.loads(body).get("artifact") or {}
        if a.get("type") == "links" and a.get("items"):
            pending.append((a.get("query") or "", a["items"]))
        elif a.get("type") == "sources" and a.get("tool") == "web_worker":
            cited = {page_key(i["url"]) for i in a.get("items", []) if i.get("url")}
            out += [(q, items, cited) for q, items in pending if any(page_key(i["url"]) in cited for i in items)]
            pending = []
    return out


def rank_of_first(order: list[int], useful: set[int]) -> int:
    return next(r + 1 for r, i in enumerate(order) if i in useful)


def main() -> None:
    e = make_embedder(settings)  # the app's model, its server started for the run
    data = searches()
    ranks = {"engines": [], "meaning": [], "fused": []}
    for query, items, cited in data:
        useful = {i for i, r in enumerate(items) if page_key(r["url"]) in cited}
        q = e.embed_query(query)
        docs = e.embed_documents([r.get("snippet") or "" for r in items], [r.get("title") for r in items])
        sims = docs @ q
        engines = list(range(len(items)))
        meaning = [int(i) for i in np.argsort(-sims, kind="stable")]
        score = {i: 1 / (RRF_K + engines.index(i) + 1) + 1 / (RRF_K + meaning.index(i) + 1) for i in engines}
        fused = sorted(engines, key=lambda i: (-score[i], i))
        for name, order in (("engines", engines), ("meaning", meaning), ("fused", fused)):
            ranks[name].append(rank_of_first(order, useful))
    n = len(data)
    print(f"{n} searches followed by an answer that cited one of their results")
    for name, r in ranks.items():
        r = np.array(r)
        print(f"  {name:8} first useful result: mean rank {r.mean():.2f} | at 1: {np.mean(r == 1):.0%} | "
              f"in top 3: {np.mean(r <= 3):.0%}")


if __name__ == "__main__":
    main()
