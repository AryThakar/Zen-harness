"""The trafilatura gate: does trafilatura read web pages better than today's extractor (web._html_text)?

Pages: every distinct page the web helper read in the user's chats, read by the HTML extractor (pages read
through an API route - GitHub, YouTube, MediaWiki - never reach it and are left out). Each page is fetched
once (live) and both extractors read the same HTML. trafilatura runs with its defaults, Markdown out, tables
kept (the plan's fast path).

Measured, with the rule fixed before running (docs/history/SYSTEM_PLAN.md, 13.1 gates):
  failures   pages where one gives under 20 words and the other 100 or more
  facts      numbers the helper's answers took from the page (the sentence that cites it), found in each
             output - counted only where one of the two has them, so they really are on the page
  noise      short lines (under 120 characters) of site furniture: menus, cookie and sign-in prompts,
             share and follow buttons, newsletters, footers
  time       seconds per page
trafilatura replaces the text step only if it fails no more pages, keeps every fact the old one keeps
(at least as many) and carries less noise; otherwise today's extractor stays and trafilatura is not added.
Added after the first run showed the facts too few to judge (6 numbers on 2 pages): the pages where one
keeps under a quarter of the other's words (500 or more), listed to be read - content lost there decides.

Result (2026-10-09, 35 pages): by the rule as first written trafilatura passed (failures 1 / 0, facts 6 / 6,
noise 12 / 7). Read side by side, it dropped what the user's tasks needed: MRF's live share price
(moneycontrol: 2,199 words -> 99, a JSON blob of yearly financials) and the list of sample datasets
(csvtools: 1,740 -> 81). Not added. Its one real win was a fault in ours - React's streamed parts, hidden
until a script moves them, were dropped (anime.com 23 words, allen.in 0) - fixed in web._place_streamed
(now 290 and 704; failures 0 / 0). trafilatura is not an app dependency: install it to run this again.

Run: .venv/Scripts/python.exe bench/web_extract.py
"""

from __future__ import annotations

import json
import re
import socket
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.tools import net, web  # noqa: E402

API_ROUTED = re.compile(r"(^|\.)(github\.com|githubusercontent\.com|youtube\.com|youtu\.be|wikipedia\.org|fandom\.com)$")
NOISE = re.compile(r"\b(cookies?|subscribe|sign in|sign up|log ?in|newsletter|share (on|this)|follow us|all rights "
                   r"reserved|privacy policy|terms of (use|service)|advertisement|related (articles|posts)|read more|"
                   r"skip to (main )?content|menu|download (the )?app)\b", re.I)
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
CITE = re.compile(r"\((https?://[^)\s]+)\)")


def pages_and_facts() -> dict[str, set[str]]:
    """Each page read through the HTML route -> the numbers in the answers' sentences that cite it."""
    db = sqlite3.connect(f"file:{ROOT / 'data/assistant/assistant.sqlite'}?mode=ro", uri=True)
    pages: dict[str, set[str]] = {}
    for (body,) in db.execute("SELECT body FROM messages WHERE role = 'artifact' ORDER BY id"):
        a = json.loads(body).get("artifact") or {}
        if a.get("type") == "webpage" and a.get("url") and not API_ROUTED.search(urlsplit(a["url"]).hostname or ""):
            pages.setdefault(a["url"], set())
    keys = {web.page_key(u): u for u in pages}
    for (body,) in db.execute("SELECT body FROM messages WHERE role = 'tool' AND body LIKE '%research_web%'"):
        try:
            answer = json.loads(json.loads(body)["content"]).get("answer") or ""
        except (ValueError, KeyError, TypeError):
            continue
        start = 0
        for m in CITE.finditer(answer):
            sentence = answer[start:m.start()]
            start = m.end()
            url = keys.get(web.page_key(m.group(1)))
            if url:
                pages[url].update(n.replace(",", "") for n in NUMBER.findall(sentence) if len(n.replace(",", "")) >= 2)
    return pages


def ours(body: bytes, charset: str | None, url: str) -> str:
    return web._html_text(body, charset, url)[1]


def theirs(body: bytes, charset: str | None, url: str) -> str:
    import trafilatura
    html = body.decode(charset or "utf-8", errors="replace")
    return trafilatura.extract(html, url=url, output_format="markdown", include_tables=True) or ""


def noise(text: str) -> int:
    return sum(1 for line in text.splitlines() if len(line) < 120 and NOISE.search(line))


def main() -> None:
    pages = pages_and_facts()
    totals = {name: {"failures": 0, "facts": 0, "noise": 0, "seconds": 0.0} for name in ("ours", "trafilatura")}
    facts_counted, read, lost = 0, 0, []
    with httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=False,
                      headers={"User-Agent": net.USER_AGENT, "Accept": "text/html,*/*;q=0.5"}) as http:
        for url, facts in pages.items():
            time.sleep(1.0)  # one page a second: the app's own pace for one site
            try:
                body, ctype, charset, final = web._fetch(http, url, socket.getaddrinfo)
            except Exception as exc:  # noqa: BLE001 - a page that can't be fetched is left out of both
                print(f"  skipped ({str(exc)[:60]}) {url[:90]}")
                continue
            if "html" not in (ctype or "") and not web._looks_html(body):
                print(f"  skipped (not HTML: {ctype}) {url[:90]}")
                continue
            out = {}
            for name, extract in (("ours", ours), ("trafilatura", theirs)):
                started = time.time()
                out[name] = extract(body, charset, final)
                totals[name]["seconds"] += time.time() - started
            read += 1
            words = {n: len(t.split()) for n, t in out.items()}
            for a, b in (("ours", "trafilatura"), ("trafilatura", "ours")):
                totals[a]["failures"] += words[a] < 20 and words[b] >= 100
            flat = {n: t.replace(",", "") for n, t in out.items()}
            present = {f for f in facts if any(f in t for t in flat.values())}
            facts_counted += len(present)
            for n in out:
                totals[n]["facts"] += sum(f in flat[n] for f in present)
                totals[n]["noise"] += noise(out[n])
            for a, b in (("ours", "trafilatura"), ("trafilatura", "ours")):
                if words[a] >= 500 and words[b] < words[a] / 4:
                    lost.append(f"{b} keeps {words[b]} of {a}'s {words[a]} words: {url}")
            print(f"  words {words['ours']:6} / {words['trafilatura']:6} | facts {sum(f in flat['ours'] for f in present)}"
                  f"/{sum(f in flat['trafilatura'] for f in present)} of {len(present)} | noise {noise(out['ours'])}"
                  f"/{noise(out['trafilatura'])} | {url[:80]}")
    print(f"\n{read} pages (ours / trafilatura)")
    for key in ("failures", "facts", "noise", "seconds"):
        o, t = totals["ours"][key], totals["trafilatura"][key]
        suffix = f" of {facts_counted}" if key == "facts" else ""
        print(f"  {key:9} {o:.1f} / {t:.1f}{suffix}" if key == "seconds" else f"  {key:9} {o} / {t}{suffix}")
    o, t = totals["ours"], totals["trafilatura"]
    passed = t["failures"] <= o["failures"] and t["facts"] >= o["facts"] and t["noise"] < o["noise"]
    print("by the counts:", "trafilatura ahead" if passed else "today's extractor ahead")
    print("read these before deciding - one keeps under a quarter of the other's text:")
    for line in lost:
        print("  " + line)


if __name__ == "__main__":
    main()
