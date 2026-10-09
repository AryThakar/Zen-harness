"""Does focusing a long page by meaning find what words alone miss? Ten long pages, each with a question
worded unlike the page and a fact the shown text must contain (written before either way was run, never
tuned on). Each page is read once (live) and focused both ways: by words alone (web._focus, the app's),
and by words and meaning fused (focus_by_meaning below, the variant tried). A fact already in the page's
opening (always shown) proves nothing and is reported apart.

Result (2026-10-09): facts found outside the opening - words 5/5, words + meaning 5/5; meaning took 3-11 s
more a read (the whole page embedded). Not kept: the app focuses by words.

Run: .venv/Scripts/python.exe bench/web_focus.py
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import settings  # noqa: E402
from app.rag.embed import make_embedder  # noqa: E402
from app.tools import web  # noqa: E402

W = "https://en.wikipedia.org/wiki/"
CASES = [
    (W + "Eiffel_Tower", "how much it weighs", "tonnes"),
    (W + "Eiffel_Tower", "who drew up the original design", "Koechlin"),
    (W + "Mount_Everest", "first people to stand on the summit", "Tenzing"),
    (W + "Moon", "how far away it is from our planet", "384"),
    (W + "Taj_Mahal", "how many labourers worked on it", "20,000"),
    (W + "Python_(programming_language)", "when version three came out", "2008"),
    (W + "One_Piece", "sales of the manga volumes worldwide", "copies"),
    (W + "Great_Wall_of_China", "total length of the wall", "21,196"),
    (W + "Amazon_River", "how much water flows out at the mouth", "209,000"),
    (W + "Leonardo_da_Vinci", "the place where he died", "Amboise"),
]
PART = 1200  # characters: the page cut into parts of about this size, at line ends
PAGE = 120_000  # characters compared by meaning at most
HEADING = re.compile(r"^(#{1,6} \S|=+ .+ =+$)")
RRF_K = 60


def focus_by_meaning(text: str, find: str, embedder) -> str:
    """The page's opening plus its parts ranked by words (their best line) and by meaning (the part and
    its heading against ``find``), fused by rank, kept in that order until the size limit."""
    words = [w for w in re.findall(r"\w+", find.lower()) if len(w) > 2] or find.lower().split()
    patterns = [re.compile(r"(?<!\w)" + re.escape(w)) for w in words]
    lines = text.splitlines()
    score = {i: sum(bool(p.search(line.lower())) for p in patterns) for i, line in enumerate(lines)}
    head = text[:1500].rsplit("\n", 1)[0]
    parts, start, size, heading = [], 0, 0, None  # (first line, last line, heading line or None)
    for i, line in enumerate(lines):
        if size and size + len(line) > PART:
            parts.append((start, i - 1, heading))
            start, size = i, 0
        if HEADING.match(line.strip()):
            heading = i
        size += len(line) + 1
    parts.append((start, len(lines) - 1, heading))
    within, chars = [], 0
    for a, b, h in parts:
        chars += sum(len(lines[j]) + 1 for j in range(a, b + 1))
        if chars > PAGE:
            break
        within.append("\n".join(lines[a:b + 1])[:2 * PART])
    titles = [lines[h].strip("#= ") if h is not None else None for _, _, h in parts[:len(within)]]
    vectors = embedder.embed_documents(within, titles)
    q = embedder.embed_query(find)
    best = {n: max(score[j] for j in range(a, b + 1)) for n, (a, b, _) in enumerate(parts)}
    by_words = sorted((n for n in best if best[n]), key=lambda n: (-best[n], n))
    by_meaning = sorted(range(len(vectors)), key=lambda n: -float(vectors[n] @ q))
    fused: dict[int, float] = {}
    for ranking in (by_words, by_meaning):
        for r, n in enumerate(ranking):
            fused[n] = fused.get(n, 0) + 1 / (RRF_K + r + 1)
    keep, used = set(), len(head)
    for n in sorted(fused, key=lambda n: (-fused[n], n)):
        a, b, h = parts[n]
        block = ([h] if h is not None and h < a else []) + list(range(a, b + 1))
        extra = sum(len(lines[j]) + 1 for j in block if j not in keep)
        if used + extra > web.MAX_TEXT:
            continue
        keep.update(block)
        used += extra
    opening = head.count("\n") + 1
    return "\n".join([head] + [lines[j] for j in sorted(keep) if j >= opening])


def main() -> None:
    embedder = make_embedder(settings)  # the app's model, its server started for the run
    score = {"words": 0, "meaning": 0}
    informative = 0
    for url, find, fact in CASES:
        web._pages.clear()
        page = web.read_webpage(url).result  # one live read, then focused both ways from its text
        if page.get("read") not in ("ok", "cut"):
            print(f"  skipped (not read: {page.get('read')}) {url}")
            continue
        full = web._cached(url)[1]
        in_head = fact in full[:1500]
        row = []
        for name, focus in (("words", lambda: web._focus(full, find)[0]),
                            ("meaning", lambda: focus_by_meaning(full, find, embedder))):
            started = time.time()
            hit = fact in focus()
            if not in_head:
                score[name] += hit
            row.append(f"{name} {'yes' if hit else 'NO '} {time.time() - started:4.1f}s")
        informative += not in_head
        where = "in the opening" if in_head else ("on the page" if fact in full else "NOT ON THE PAGE")
        print(f"  {' | '.join(row)} | {len(full):6,} chars, fact {where} | {find} -> {fact}")
    print(f"facts found outside the opening: words {score['words']}/{informative}, "
          f"words + meaning {score['meaning']}/{informative}")


if __name__ == "__main__":
    main()
