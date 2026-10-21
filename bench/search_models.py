# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Search by meaning: EmbeddingGemma 2 (llama-server) against bge-small (fastembed), on held-out questions
about the user's own workspaces (data/assistant/eval/search_heldout.json - user data, not committed).

Each workspace's index is copied and re-indexed with each model (the real code path: SearchIndex.reindex),
then every question runs the way its tool runs it (files: documents + reports, top 5; notes: top 2;
chats: top 4). Reported per model:
  meaning rank   where the first right passage ranks by cosine alone (no cut-off): 1 = first
  found          a right passage among the tool's results (keywords + meaning, fused), at the cut-off
  first          the tool's first result is a right one
  unrelated      questions about nothing in the workspace that meaning still answered (should be 0)
The cut-off (min_similarity) for each model is chosen on half the questions (even positions) and the
scores are reported on the other half (odd), so the number shown is not the one it was fitted to.

Run: .venv/Scripts/python.exe bench/search_models.py [embedding server url]
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.rag.embed import FastEmbedder, ServerEmbedder  # noqa: E402
from app.tools.search_index import MIN_SIMILARITY, SearchIndex  # noqa: E402

SET = ROOT / "data/assistant/eval/search_heldout.json"
WORKSPACES = ROOT / "data/assistant/workspaces"
POOLS = {"files": (("document", "report"), 5), "notes": (("note",), 2), "chats": (("chat",), 4)}


def copy_index(ws: str, folder: Path) -> Path:
    dst = folder / ws / "search.sqlite"
    dst.parent.mkdir(parents=True)
    src = sqlite3.connect(f"file:{WORKSPACES / ws / 'search.sqlite'}?mode=ro", uri=True)
    out = sqlite3.connect(dst)
    src.backup(out)
    src.close()
    out.close()
    return dst


def cosines(index: SearchIndex, q: str, sources) -> tuple[np.ndarray, np.ndarray]:
    ids, matrix, _ = index._matrix(sources)
    return ids, matrix @ index.embedder.embed_query(q)


def score(questions, indexes, floor: float) -> dict:
    """The tool's results at this cut-off, and meaning alone, for each question."""
    rows = []
    for q in questions:
        index = indexes[q["ws"]]
        sources, k = POOLS[q["pool"]]
        index.embedder.min_similarity = floor
        ids, sims = cosines(index, q["q"], sources)
        order = ids[np.argsort(-sims)]
        right = set(q["right"])
        if right:
            rank = next(i + 1 for i, rid in enumerate(order) if int(rid) in right)
            hits, _ = index.search(q["q"], sources, k)
            got = [index_id(index, h) for h in hits]
            rows.append({"q": q["q"], "rank": rank, "found": any(g in right for g in got),
                         "first": bool(got) and got[0] in right,
                         "right_cos": float(max(s for rid, s in zip(ids, sims) if int(rid) in right))})
        else:
            rows.append({"q": q["q"], "unrelated": True, "best_cos": float(sims.max())})
    return rows


def index_id(index: SearchIndex, hit) -> int:
    with index._db() as db:
        return db.execute("SELECT id FROM passages WHERE source = ? AND ref = ? AND text = ?",
                          (hit.source, hit.ref, hit.text)).fetchone()[0]


def pick_floor(rows) -> float:
    """The cut-off that keeps the most right passages and lets through the fewest unrelated questions;
    the middle of the best range."""
    grid = np.round(np.arange(0.20, 0.95, 0.01), 2)
    good = [sum(r["right_cos"] >= t for r in rows if "rank" in r) + sum(r["best_cos"] < t for r in rows if "unrelated" in r)
            for t in grid]
    best = [t for t, g in zip(grid, good) if g == max(good)]
    return float(np.round((best[0] + best[-1]) / 2, 2))


def summary(rows) -> str:
    answerable = [r for r in rows if "rank" in r]
    unrelated = [r for r in rows if "unrelated" in r]
    n = len(answerable)
    ranks = np.array([r["rank"] for r in answerable])
    return (f"meaning rank 1: {np.mean(ranks == 1):.0%}, top 3: {np.mean(ranks <= 3):.0%} | "
            f"found {sum(r['found'] for r in answerable)}/{n}, first {sum(r['first'] for r in answerable)}/{n} | "
            f"unrelated answered by meaning {sum(r['answered'] for r in unrelated)}/{len(unrelated)}")


def main(url: str) -> None:
    questions = json.loads(SET.read_text(encoding="utf-8"))["questions"]
    models = {"bge-small": FastEmbedder("BAAI/bge-small-en-v1.5", str(ROOT / "data/models")),
              "embeddinggemma-2": ServerEmbedder(url, "embeddinggemma-2")}
    tmp = Path(tempfile.mkdtemp(prefix="search-models-"))
    for name, embedder in models.items():
        indexes = {}
        started = time.time()
        passages = 0
        for ws in sorted({q["ws"] for q in questions}):
            index = SearchIndex(copy_index(ws, tmp / name), embedder)
            if name != "bge-small":  # the copies already hold bge-small vectors
                passages += index.reindex()
            indexes[ws] = index
        took = time.time() - started
        calib, report = questions[0::2], questions[1::2]
        floor = pick_floor(score(calib, indexes, 0.0))
        rows = score(report, indexes, floor)
        for r in rows:
            if "unrelated" in r:
                r["answered"] = r["best_cos"] >= floor
        print(f"\n== {name}  (cut-off chosen on the other half: {floor}; today's setting {MIN_SIMILARITY})")
        if passages:
            print(f"   re-indexed {passages} passages in {took:.1f} s ({passages / took:.1f} a second)")
        print("   " + summary(rows))
        for r in rows:
            if "rank" in r:
                mark = "ok " if r["first"] else ("in " if r["found"] else "MISS")
                print(f"   {mark} rank {r['rank']:3}  cos {r['right_cos']:.3f}  {r['q']}")
            else:
                print(f"   {'LEAK' if r['answered'] else 'none'}  best cos {r['best_cos']:.3f}  {r['q']}")
        if name == "bge-small":
            rows = score(report, indexes, MIN_SIMILARITY)
            for r in rows:
                if "unrelated" in r:
                    r["answered"] = r["best_cos"] >= MIN_SIMILARITY
            print(f"   at today's cut-off {MIN_SIMILARITY}: " + summary(rows))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8091")
