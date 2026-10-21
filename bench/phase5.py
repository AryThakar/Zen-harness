"""Phase 5's acceptance on the real model (docs/history/REBUILD_PLAN.md, sections 3 and 5): after one pass over every tool
description and prompt, does main route each kind of request where its tools say it goes?

    maths       -> calculate (main, no helper)
    statistics  -> analyze_or_code, whose helper uses the tool built for the job: predict, forecast, compare,
                   find_patterns, analyze_text (the real helper runs)
    documents   -> files find or read; a pinned file answered from the turn's snapshot with no tool
    talk        -> no tool; emoji in at most 1 of 4 casual replies and none in serious ones (counted over each half)

Two halves, written together before either was run (2026-10-14): 'dev' is for finding and fixing what fails; 'holdout'
is run once, after the fixes, and reported as it comes. Different data and wording in each - and none of the phase 7
jobs' data - so passing the holdout says the descriptions route, not that they were fitted to these questions.

    python -m bench.phase5 dev|holdout [--only id,id] [--runs N]   (llama-server on BIZ_LLAMACPP_URL)
        -> bench/phase5_<half>.md and .json
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import tempfile
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from app.assistant import workers
from app.assistant.registry import TOOLS
from app.assistant.store import Store
from app.config import settings
from app.providers.llamacpp import LlamaCppProvider
from app.rag.embed import make_embedder

from .combos import _document
from .main import tool_probe
from .main.suites import Case, Seen, all_of, answered, avoids, no_tool, says, uses

HERE = Path(__file__).resolve().parent
EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF]")


@dataclasses.dataclass
class Probe(Case):
    kind: str = "route"  # maths, stats, docs, talk, casual, serious
    helpers: bool = False  # the real helper runs (statistics); otherwise a stand-in records main's task
    pin: str | None = None  # a file pinned to the chat before the turn


# ---------------------------------------------------------------- checks

def helper_ran(*names: str) -> Callable[[Seen], str | None]:
    """main handed the task to analyze_or_code and its helper used one of these tools, and it worked."""
    def check(s: Seen) -> str | None:
        if "analyze_or_code" not in s.main:
            return f"no analyze_or_code (main called: {', '.join(s.main) or 'nothing'})"
        used = [t for t in s.tools if t["by"] != "main" and t["name"] in names]
        if not used:
            return f"the helper used no {' or '.join(names)} (it used: {', '.join(t['name'] for t in s.tools if t['by'] != 'main') or 'nothing'})"
        return None if any(t.get("status") in ("ok", "partial") for t in used) else f"{used[0]['name']} failed"
    return check


def found_in_files(s: Seen) -> str | None:
    """A document question answered by main with files find or read - not a helper, not the web."""
    reads = [t for t in s.tools if t["by"] == "main" and t["name"] == "files"
             and (t.get("input") or {}).get("mode") in ("find", "read")]
    wrong = [n for n in s.main if n in ("research_web", "ask_knowledge", "analyze_or_code")]
    if wrong:
        return f"called {', '.join(wrong)}"
    return None if reads else f"no files find or read (main called: {', '.join(s.main) or 'nothing'})"


def no_emoji(s: Seen) -> str | None:
    return f"emoji in a serious reply: {''.join(EMOJI.findall(s.answer))}" if EMOJI.search(s.answer) else None


# ---------------------------------------------------------------- data (fixed seeds; no phase 7 job's)

def _table(df: pd.DataFrame, name: str) -> Callable[[Store, str], None]:
    return lambda store, ws_id: store.open(ws_id)[0].add_table(df, name)


def _doc(name: str, text: str) -> Callable[[Store, str], None]:
    return lambda store, ws_id: _document(store, ws_id, name, text)


def _staff() -> pd.DataFrame:
    rng = np.random.default_rng(41)
    n = 500
    overtime = rng.choice(["yes", "no"], n, p=[0.3, 0.7])
    satisfaction = rng.integers(1, 6, n)
    years = rng.integers(0, 20, n)
    risk = 1 / (1 + np.exp(-(-1.6 + 1.5 * (overtime == "yes") - 0.55 * (satisfaction - 3) - 0.05 * years)))
    return pd.DataFrame({"employee_id": [f"E{i:04d}" for i in range(n)],
                         "department": rng.choice(["sales", "support", "engineering"], n),
                         "salary_k": rng.normal(55, 12, n).round(1), "overtime": overtime,
                         "satisfaction": satisfaction, "years_at_company": years,
                         "left": np.where(rng.random(n) < risk, "yes", "no")})


def _visits() -> pd.DataFrame:
    rng = np.random.default_rng(42)
    weeks = pd.date_range("2024-01-01", periods=104, freq="W-MON")
    values = 1200 + 9 * np.arange(104) + 180 * np.sin(np.arange(104) * 2 * np.pi / 52) + rng.normal(0, 60, 104)
    return pd.DataFrame({"week": weeks.strftime("%Y-%m-%d"), "visits": values.round().astype(int)})


def _deliveries() -> pd.DataFrame:
    rng = np.random.default_rng(43)
    return pd.DataFrame({"order_id": range(1, 301), "courier": ["FastShip"] * 150 + ["ParcelGo"] * 150,
                         "delivery_days": np.r_[rng.gamma(9, 0.42, 150), rng.gamma(9, 0.35, 150)].round(1)})


def _shoppers() -> pd.DataFrame:
    rng = np.random.default_rng(44)
    groups = [(120, 2, 40, 200), (80, 14, 65, 20), (40, 30, 180, 8)]  # (customers, orders, basket, days since last)
    rows = []
    for size, orders, basket, since in groups:
        for _ in range(size):
            o = max(1, int(rng.poisson(orders)))
            b = abs(rng.normal(basket, basket * 0.2))
            rows.append((o, round(o * b, 2), round(b, 2), int(abs(rng.normal(since, since * 0.3)))))
    df = pd.DataFrame(rows, columns=["orders", "total_spent", "avg_basket", "days_since_last"])
    df.insert(0, "shopper_id", [f"S{i:03d}" for i in range(len(df))])
    return df.sample(frac=1, random_state=1).reset_index(drop=True)


def _varied(rng, said: dict[str, list[str]], counts: dict[str, int], fill: dict[str, list[str]]) -> list[str]:
    """Free text as people write it: per topic, its sentences with their details filled in at random - nearly every
    row its own words (analyze_text's themes need at least 20 distinct texts)."""
    rows = []
    for topic, n in counts.items():
        for _ in range(n):
            text = str(rng.choice(said[topic]))
            rows.append(text.format(**{k: rng.choice(v) for k, v in fill.items()}))
    return list(rng.permutation(rows))


def _tickets() -> pd.DataFrame:
    rng = np.random.default_rng(45)
    said = {"login": ["Can't log in since the {version} update on my {device}", "Password reset email never arrives, "
                      "tried {n} times", "Locked out of my account since {day}", "The two-factor code is rejected on my "
                      "{device} every time", "Login page just spins after the {version} update"],
            "billing": ["I was charged twice in {month}", "Refund for order {order} still not received after {n} days",
                        "Invoice {order} shows the wrong amount", "My subscription went up in {month} without notice",
                        "Charged for a plan I cancelled in {month}"],
            "speed": ["The app takes {n} seconds to open on my {device}", "Pages freeze when I scroll on the {device}",
                      "Search has been very slow since {day}", "Everything lags after the {version} update"]}
    fill = {"version": ["4.2", "4.3", "5.0", "5.1"], "device": ["phone", "tablet", "laptop", "iPad", "Pixel"],
            "n": [str(i) for i in range(3, 30)], "day": ["Monday", "Tuesday", "the weekend", "yesterday", "last week"],
            "month": ["March", "April", "May", "June"], "order": [f"#{i}" for i in range(4100, 4190)]}
    rows = _varied(rng, said, {"login": 25, "billing": 20, "speed": 15}, fill)
    return pd.DataFrame({"ticket_id": range(1, len(rows) + 1), "message": rows})


def _homes() -> pd.DataFrame:
    rng = np.random.default_rng(51)
    n = 400
    area = rng.normal(95, 30, n).clip(30).round()
    bedrooms = rng.integers(1, 6, n)
    area_name = rng.choice(["centre", "suburb", "outskirts"], n)
    price = 1800 * area + 9000 * bedrooms + np.select([area_name == "centre", area_name == "suburb"], [60000, 20000], 0)
    return pd.DataFrame({"home_id": range(1, n + 1), "area_m2": area, "bedrooms": bedrooms, "location": area_name,
                         "year_built": rng.integers(1960, 2024, n), "price": (price + rng.normal(0, 15000, n)).round(-2)})


def _orders_by_month() -> pd.DataFrame:
    rng = np.random.default_rng(52)
    months = pd.date_range("2022-01-01", periods=36, freq="MS")
    values = 800 + 15 * np.arange(36) + np.where(months.month.isin([3, 4]), 150, 0) + rng.normal(0, 30, 36)
    return pd.DataFrame({"month": months.strftime("%Y-%m"), "orders": values.round().astype(int)})


def _scores() -> pd.DataFrame:
    rng = np.random.default_rng(53)
    return pd.DataFrame({"student": range(1, 161), "method": ["lecture"] * 80 + ["project"] * 80,
                         "exam_score": np.r_[rng.normal(64, 11, 80), rng.normal(70, 11, 80)].clip(0, 100).round()})


def _baskets() -> pd.DataFrame:
    rng = np.random.default_rng(54)
    items = ["milk", "eggs", "apples", "rice", "coffee", "tea", "pasta", "cheese"]
    rows = []
    for order in range(1, 401):
        basket = set(rng.choice(items, rng.integers(1, 4), replace=False))
        if rng.random() < 0.35:
            basket |= {"bread", "butter"}
        elif rng.random() < 0.1:
            basket.add("bread")
        rows += [(order, item) for item in sorted(basket)]
    return pd.DataFrame(rows, columns=["order_id", "item"])


def _feedback() -> pd.DataFrame:
    rng = np.random.default_rng(55)
    said = {"battery": ["Battery is down to {pct}% by lunchtime", "The phone gets hot and the battery dies in {h} hours",
                        "Charge doesn't last a day since update {version}", "Battery drains {pct}% overnight"],
            "camera": ["Night photos come out blurry in {place}", "The camera app crashes when I switch to {mode}",
                       "Zoomed pictures look grainy past {zoom}x", "Focus hunts forever in {mode} mode"],
            "screen": ["The screen cracked after a drop from {h} feet", "The display flickers at {pct}% brightness",
                       "A green line appeared on the screen after {h} weeks"]}
    fill = {"pct": [str(i) for i in range(10, 60, 5)], "h": [str(i) for i in range(2, 9)],
            "version": ["12.1", "12.2", "13.0"], "place": ["restaurants", "the park", "my kitchen", "concerts"],
            "mode": ["portrait", "video", "night", "macro"], "zoom": ["3", "5", "10"]}
    rows = _varied(rng, said, {"battery": 24, "camera": 18, "screen": 10}, fill)
    return pd.DataFrame({"id": range(1, len(rows) + 1), "feedback": rows})


LEASE = ("# Tenancy agreement - flat 4B, Harbour Road\n\n## Rent and deposit\nRent is 1,450 a month, due on the 1st. The "
         "deposit is three months' rent, returned within 30 days of moving out.\n\n## Ending the tenancy\nEither side may "
         "end the tenancy with two months' notice in writing.\n\n## Pets\nPets are allowed only with the landlord's "
         "written permission; a small cat or dog is usually accepted.\n\n## Repairs\nThe landlord fixes the heating and "
         "plumbing within 7 days of a report.\n")
NOTES = "# Moving checklist\n\n- book the van\n- ask about the deposit return\n- change the address at the bank\n"
POLICY = ("# Home insurance policy\n\n## What is covered\nFire, theft and storm damage to the building and its contents.\n\n"
          "## Water damage\nSudden escape of water from pipes or appliances is covered up to 20,000. Gradual leaks, damp "
          "and flooding from outside are not covered.\n\n## Excess\nYou pay the first 250 of each claim.\n")
BREAD = ("# Weekend loaf\n\n500 g flour, 350 ml water, 10 g salt, 7 g yeast.\n\nMix, rest 1 hour, shape, rest 45 minutes. "
         "Bake at 230 °C for 35 minutes, until it sounds hollow when tapped.\n")
RECEIPT = "# Receipt - 55 inch TV\n\nBought 2 March 2025 for 649. The manufacturer's warranty runs 2 years from purchase.\n"


def _both(*setups: Callable[[Store, str], None]) -> Callable[[Store, str], None]:
    def setup(store: Store, ws_id: str) -> None:
        for s in setups:
            s(store, ws_id)
    return setup


CASUAL = ("casual", all_of(no_tool, answered))
SERIOUS = ("serious", all_of(no_tool, answered, no_emoji))

PROBES: dict[str, list[Probe]] = {
    "dev": [
        Probe("percent", "what's 17.5% of 3,240?", all_of(uses("calculate"), says(r"\b567\b")), kind="maths"),
        Probe("integral", "integrate x^2 * sin(x) dx", all_of(uses("calculate"), avoids("analyze_or_code")), kind="maths"),
        Probe("compound", "if I put 50,000 in at 7% a year, compounded yearly, what will it be after 12 years?",
              all_of(uses("calculate"), says(r"112,?6\d\d")), kind="maths"),
        Probe("linear", "solve 3x + 7 = 2x - 5", all_of(uses("calculate"), says(r"[-−]\s?12\b")), kind="maths"),
        Probe("attrition", "what makes people leave the company in staff.csv?", helper_ran("predict"),
              setup=_table(_staff(), "staff.csv"), kind="stats", helpers=True),
        Probe("traffic", "will our website visits keep growing? give me the next 8 weeks from visits.csv",
              helper_ran("forecast"), setup=_table(_visits(), "visits.csv"), kind="stats", helpers=True),
        Probe("couriers", "in deliveries.csv, is FastShip really slower than ParcelGo, or is that just noise?",
              helper_ran("compare"), setup=_table(_deliveries(), "deliveries.csv"), kind="stats", helpers=True),
        Probe("shopper_types", "group the shoppers in shoppers.csv into a few types", helper_ran("find_patterns"),
              setup=_table(_shoppers(), "shoppers.csv"), kind="stats", helpers=True),
        Probe("complaints", "what are people complaining about in tickets.csv?", helper_ran("analyze_text"),
              setup=_table(_tickets(), "tickets.csv"), kind="stats", helpers=True),
        Probe("notice", "how much notice do I have to give to leave my flat?", all_of(found_in_files, says(r"two|2")),
              setup=_both(_doc("lease.md", LEASE), _doc("moving.md", NOTES)), kind="docs"),
        Probe("pinned_pets", "can I keep a cat there?", all_of(no_tool, says(r"permission")),
              setup=_both(_doc("lease.md", LEASE), _doc("moving.md", NOTES)), pin="lease.md", kind="docs"),
        Probe("which_files", "which of my files mention the deposit?", all_of(found_in_files, says(r"lease"), says(r"moving")),
              setup=_both(_doc("lease.md", LEASE), _doc("moving.md", NOTES)), kind="docs"),
        Probe("explain", "explain what overfitting means in machine learning", all_of(no_tool, answered), kind="talk"),
        Probe("hey", "hey! how's your day going?", CASUAL[1], kind=CASUAL[0]),
        Probe("thanks", "thanks, that was super helpful!", CASUAL[1], kind=CASUAL[0]),
        Probe("coffee", "lol my cat just knocked my coffee all over the desk", CASUAL[1], kind=CASUAL[0]),
        Probe("weekend", "any fun ideas for a lazy sunday?", CASUAL[1], kind=CASUAL[0]),
        Probe("diagnosis", "my dad was just diagnosed with type 2 diabetes. what should we know?", SERIOUS[1],
              kind=SERIOUS[0]),
        Probe("tcp_udp", "what's the difference between TCP and UDP?", SERIOUS[1], kind=SERIOUS[0]),
    ],
    "holdout": [
        Probe("power", "what is 2^20 divided by 365?", all_of(uses("calculate"), says(r"2,?872")), kind="maths"),
        Probe("derivative", "derivative of ln(x^2 + 1)", all_of(uses("calculate"), says(r"2\s?\*?\s?x")), kind="maths"),
        Probe("interest", "a loan of 2,00,000 at 9% simple interest for 3 years - how much interest do I pay?",
              all_of(uses("calculate"), says(r"54,?000")), kind="maths"),
        Probe("cubic", "find the roots of x^3 - 6x^2 + 11x - 6", all_of(uses("calculate"), says(r"\b1\b", r"\b2\b", r"\b3\b")),
              kind="maths"),
        Probe("house_prices", "which things drive the price in homes.csv?", helper_ran("predict"),
              setup=_table(_homes(), "homes.csv"), kind="stats", helpers=True),
        Probe("next_quarter", "forecast the next three months of orders from orders_by_month.csv", helper_ran("forecast"),
              setup=_table(_orders_by_month(), "orders_by_month.csv"), kind="stats", helpers=True),
        Probe("methods", "do the two teaching methods in scores.csv give different exam results?", helper_ran("compare"),
              setup=_table(_scores(), "scores.csv"), kind="stats", helpers=True),
        Probe("bought_together", "what gets bought together in baskets.csv?", helper_ran("find_patterns"),
              setup=_table(_baskets(), "baskets.csv"), kind="stats", helpers=True),
        Probe("feedback_themes", "sort the feedback in feedback.csv into themes", helper_ran("analyze_text"),
              setup=_table(_feedback(), "feedback.csv"), kind="stats", helpers=True),
        Probe("water", "does my insurance cover a burst pipe?", all_of(found_in_files, says(r"20,?000|covered")),
              setup=_both(_doc("policy.md", POLICY), _doc("tv_receipt.md", RECEIPT)), kind="docs"),
        Probe("pinned_bake", "how long does it go in the oven?", all_of(no_tool, says(r"35")),
              setup=_both(_doc("bread.md", BREAD), _doc("policy.md", POLICY)), pin="bread.md", kind="docs"),
        Probe("warranty", "do any of my files say anything about a warranty?", all_of(found_in_files, says(r"receipt|tv")),
              setup=_both(_doc("policy.md", POLICY), _doc("tv_receipt.md", RECEIPT)), kind="docs"),
        Probe("list_tuple", "what's the difference between a list and a tuple in python?", all_of(no_tool, answered),
              kind="talk"),
        Probe("morning", "good morning! ready for another day?", CASUAL[1], kind=CASUAL[0]),
        Probe("nailed", "I finally passed my driving test!!", CASUAL[1], kind=CASUAL[0]),
        Probe("rainy", "it's raining again, so bored", CASUAL[1], kind=CASUAL[0]),
        Probe("movie", "recommend me a light movie for tonight", CASUAL[1], kind=CASUAL[0]),
        Probe("job_loss", "I got laid off this morning and I don't know what to do", SERIOUS[1], kind=SERIOUS[0]),
        Probe("sql_index", "how does a database index speed up queries?", SERIOUS[1], kind=SERIOUS[0]),
    ],
}


def _pinned(probe: Probe) -> Callable[[Store, str], None] | None:
    """The probe's set-up, then its file pinned to the probe's chat (made just before the set-up runs)."""
    if not probe.pin:
        return probe.setup

    def setup(store: Store, ws_id: str) -> None:
        if probe.setup:
            probe.setup(store, ws_id)
        ws, _ = store.open(ws_id)
        for chat in store.chats(ws_id):
            store.update_chat(chat["id"], pins=[ws.resolve(probe.pin)])
    return setup


def report(half: str, rows: list[dict]) -> Path:
    kinds = list(dict.fromkeys(r["kind"] for r in rows))
    emoji = {k: (sum(r["emoji"] for r in rows if r["kind"] == k), sum(r["kind"] == k for r in rows))
             for k in ("casual", "serious")}
    out = [f"# Phase 5 routing - {half}: {sum(r['ok'] for r in rows)}/{len(rows)} passed", "",
           " · ".join(f"{k} {sum(r['ok'] for r in rows if r['kind'] == k)}/{sum(r['kind'] == k for r in rows)}" for k in kinds),
           "", f"Emoji: casual replies {emoji['casual'][0]}/{emoji['casual'][1]} (aim: at most 1 in 4), serious "
               f"{emoji['serious'][0]}/{emoji['serious'][1]} (aim: none)", "",
           "| probe | kind | tools | check | s | reply |", "|---|---|---|---|---|---|"]
    cell = lambda s: " ".join(str(s).split()).replace("|", "\\|")
    for r in rows:
        out.append(f"| {r['case']} | {r['kind']} | {cell('; '.join(r['tools'])) or '-'} | "
                   f"{'ok' if r['ok'] else '**' + cell(r['problem']) + '**'} | {r['seconds']} | {cell(r['answer'])[:240]} |")
    path = HERE / f"phase5_{half}.md"
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    (HERE / f"phase5_{half}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("half", choices=sorted(PROBES))
    ap.add_argument("--only")
    ap.add_argument("--runs", type=int, default=1)
    args = ap.parse_args()
    TOOLS["generate_image"] = dataclasses.replace(TOOLS["generate_image"], handler=tool_probe._image)
    real = workers.run_worker
    provider = LlamaCppProvider(settings.llamacpp_model, settings.llamacpp_url, settings.assistant_thinking,
                                slot=int(os.environ.get("PROBE_SLOT", "1")), max_tokens=settings.assistant_max_tokens)
    embedder = make_embedder(settings)
    rows = []
    for probe in [p for p in PROBES[args.half] if not args.only or p.id in args.only.split(",")]:
        workers.run_worker = real if probe.helpers else tool_probe._helper
        case = dataclasses.replace(probe, setup=_pinned(probe))
        for n in range(1, args.runs + 1):
            store = Store(Path(tempfile.mkdtemp()) / "store", embedder)
            started = time.perf_counter()
            try:
                seen = tool_probe.run(case, store, provider)
                problem = case.check(seen) or (f"turn failed: {seen.error}" if seen.error else None)
            except Exception as exc:  # noqa: BLE001 - a crash is a failed probe, reported
                seen, problem = Seen(case.say, datetime.now()), f"crash: {type(exc).__name__}: {exc}"
            finally:
                store.close()
            row = {"case": probe.id, "kind": probe.kind, "run": n, "ok": problem is None, "problem": problem,
                   "seconds": round(time.perf_counter() - started, 1), "tools": [tool_probe.brief(t) for t in seen.tools],
                   "calls": seen.tools, "answer": seen.answer, "emoji": bool(EMOJI.search(seen.answer or "")),
                   "nudges": getattr(seen, "nudges", [])}
            rows.append(row)
            print(f"{probe.id:<16} {'ok  ' if row['ok'] else 'FAIL'} {row['seconds']:>6}s  "
                  f"{'; '.join(row['tools'])[:110]}  {problem or ''}", flush=True)
    print("->", report(args.half, rows).relative_to(HERE.parent))


if __name__ == "__main__":
    main()
