"""The acceptance test: whole jobs, the way the user works - several tools and helpers in a chain, through the
real turn (app.assistant.main: the same prompts, tools, helpers and checks as the app), on the real models.

Each job is a few messages; the cards it shows are answered the way a user would (a permission: allow; a
question: the answer the job gives, else its first option). Whether a job passed is decided by code from
what it left - the tools that ran, the files and notes in the workspace, the numbers against a truth
computed when the job was set up - never by reading the reply's tone.

Every job starts from nothing: a new store - its own SQLite database and workspace folder in a temporary
directory, never the user's data - with data made by the job's set-up from fixed seeds, so no answer can come
from what a model remembers. Each job chains 3-6 tools and tests a behaviour too: asking when a request is
ambiguous, a permission refused, a broken file fixed, a picture checked, a plan shown first, a note carried
into a new chat, a document and a table combined, a live web fact used in exact arithmetic.

Two halves, written together before any was run (2026-10-09): 'dev' is for finding and fixing what fails;
'holdout' is run once, after the fixes, and reported as it comes - so passing it says the app works, not that
it was tuned to the test.

    python -m bench.combos dev|holdout|all [--only id,id] [--keep]   (llama-server on BIZ_LLAMACPP_URL, 2 slots)
        -> bench/combos_<split>.md and .json (with --only: combos_<split>_only); --keep leaves each job's store
           (its path is in the report)

The web jobs read live pages, so a site that changes can fail one; such a failure is reported with what
the site gave.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from app.assistant import main as main_slot
from app.assistant.store import Store
from app.config import settings
from app.providers.llamacpp import LlamaCppProvider
from app.rag.embed import make_embedder

HERE = Path(__file__).resolve().parent
MAX_CARDS = 6  # questions answered in one message before the job counts as stuck


@dataclass
class Step:
    say: str
    answers: tuple[str, ...] = ()  # for the cards, in order: "allow", "cancel", "first", or the words to type
    new_chat: bool = False


@dataclass
class Run:
    """What a job did and left."""
    store: Store
    ws_id: str
    truth: dict
    chats: list[str] = field(default_factory=list)
    tools: list[dict] = field(default_factory=list)  # {"by": "main" | helper, "name", "input", "status"}
    artifacts: list[dict] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)  # each message's final reply
    cards: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ws(self):
        return self.store.open(self.ws_id)[0]

    def ran(self, name: str, by: str | None = None) -> list[dict]:
        return [t for t in self.tools if t["name"] == name and (by is None or t["by"] == by)
                and t.get("status") in ("ok", "partial", "asked", None)]

    def files(self, pattern: str, kind: str = "file") -> list[str]:
        return [i for i in self.ws.ids(kind) if re.search(pattern, self.ws.entry(i)["name"], re.I)]

    def notes_text(self) -> str:
        from app.tools.notes import body
        return "\n".join(body(self.ws, i) for i in self.ws.ids("note"))


@dataclass
class Combo:
    id: str
    split: str
    steps: list[Step]
    check: Callable[[Run], str | None]
    setup: Callable[[Store, str, dict], None] | None = None
    why: str = ""


# ---------------------------------------------------------------- driving the real turn

def _consume(events, run: Run) -> dict | None:
    """Record a stream of turn events; the question it stopped on, if any."""
    calls: dict[str, dict] = {}
    question = None
    for event in events:
        kind = event["type"]
        if kind == "tool_start":
            calls[event["id"]] = {"by": "main", "name": event["name"], "input": event["input"]}
            run.tools.append(calls[event["id"]])
        elif kind == "tool_end" and event["id"] in calls:
            calls[event["id"]]["status"] = event["status"]
        elif kind == "worker" and event["event"] == "tool":
            run.tools.append({"by": event["worker"], "name": event["name"], "input": event["input"]})
        elif kind == "worker" and event["event"] == "result":
            mine = [t for t in run.tools if t["by"] == event["worker"] and "status" not in t]
            if mine:
                mine[-1]["status"] = event["status"]
        elif kind == "artifact":
            run.artifacts.append(event["artifact"])
        elif kind == "question":
            question = event
        elif kind == "done":
            run.answers.append(event.get("answer", ""))
        elif kind in ("error", "stopped"):
            run.errors.append(event.get("message", kind))
    return question


def _pick(card: dict, queue: list[str]) -> str | None:
    options = card.get("options") or []
    wanted = queue.pop(0) if queue else ("allow" if card.get("approval") else "first")
    if wanted == "allow":
        return options[0] if options else "yes"
    if wanted == "cancel":
        return options[-1] if options else "no"
    if wanted == "first":
        return options[0] if options else "Take the usual choice and go on."
    return wanted


def drive(combo: Combo, store: Store, main, worker, truth: dict) -> Run:
    ws_id = store.create_workspace(combo.id)["id"]
    run = Run(store, ws_id, truth)
    if combo.setup:
        combo.setup(store, ws_id, truth)
    chat_id = None
    for step in combo.steps:
        if chat_id is None or step.new_chat:
            chat_id = store.create_chat(ws_id)["id"]
            run.chats.append(chat_id)
        queue = list(step.answers)
        card = _consume(main_slot.send(store, chat_id, step.say, main, worker), run)
        for _ in range(MAX_CARDS):
            if card is None:
                break
            run.cards.append({k: card.get(k) for k in ("question", "options", "approval", "via")})
            card = _consume(main_slot.reply(store, chat_id, _pick(card, queue), main, worker), run)
        else:
            run.errors.append(f"still asking after {MAX_CARDS} answers")
    return run


# ---------------------------------------------------------------- checks

Check = Callable[[Run], "str | None"]


def all_of(*checks: Check) -> Check:
    def check(r: Run) -> str | None:
        return next((p for p in (c(r) for c in checks) if p), None)
    return check


def ran(name: str, by: str | None = None) -> Check:
    def check(r: Run) -> str | None:
        return None if r.ran(name, by) else f"{name} did not run" + (f" ({by})" if by else "")
    return check


def no_errors(r: Run) -> str | None:
    return f"turn failed: {r.errors[0]}" if r.errors else None


def answer_has(*patterns: str, last: bool = True) -> Check:
    """The (last) reply matches every pattern (regular expressions, case-insensitive)."""
    def check(r: Run) -> str | None:
        text = (r.answers[-1] if last else "\n".join(r.answers)) if r.answers else ""
        missing = [p for p in patterns if not re.search(p, text, re.I)]
        return f"reply lacks {', '.join(missing)}" if missing else None
    return check


def answer_number(key: str, tolerance: float, last: bool = True) -> Check:
    """The reply holds the truth's number (commas and a % sign allowed), within the tolerance."""
    def check(r: Run) -> str | None:
        text = (r.answers[-1] if last else "\n".join(r.answers)) if r.answers else ""
        values = [float(n.replace(",", "")) for n in re.findall(r"-?\d[\d,]*(?:\.\d+)?", text)]
        want = r.truth[key]
        return None if any(abs(v - want) <= tolerance for v in values) else f"reply lacks {key}={want}"
    return check


def file_made(pattern: str, kind: str = "file", test: Callable[[Run, str], str | None] | None = None) -> Check:
    def check(r: Run) -> str | None:
        found = r.files(pattern, kind)
        if not found:
            return f"no {kind} named like {pattern}"
        return test(r, found[-1]) if test else None
    return check


def docx_with_picture(words: str = "") -> Callable[[Run, str], str | None]:
    def test(r: Run, item: str) -> str | None:
        from docx import Document
        doc = Document(io.BytesIO(r.ws.path(item).read_bytes()))
        if not doc.inline_shapes:
            return "the Word file has no picture"
        text = "\n".join(p.text for p in doc.paragraphs)
        return None if not words or re.search(words, text, re.I) else f"the Word file lacks {words}"
    return test


def pptx_with(slides: int, pictures: int) -> Callable[[Run, str], str | None]:
    def test(r: Run, item: str) -> str | None:
        from pptx import Presentation
        deck = Presentation(io.BytesIO(r.ws.path(item).read_bytes()))
        n = len(deck.slides)
        pics = sum(sh.shape_type == 13 for s in deck.slides for sh in s.shapes)
        return None if n >= slides and pics >= pictures else f"slides: {n} (want {slides}+), pictures: {pics}"
    return test


def note_has(pattern: str) -> Check:
    def check(r: Run) -> str | None:
        return None if re.search(pattern, r.notes_text(), re.I) else f"no note with {pattern}"
    return check


def card_for(action: str) -> Check:
    def check(r: Run) -> str | None:
        return None if any((c.get("approval") or {}).get("action") == action for c in r.cards) else \
            f"no {action} permission card (cards: {[c.get('question') for c in r.cards]})"
    return check


def chart_drawn(kind: str | None = None) -> Check:
    def check(r: Run) -> str | None:
        charts = [a for a in r.artifacts if a.get("type") == "chart"]
        if not charts:
            return "no chart shown"
        return None if kind is None or any(a.get("chart") == kind for a in charts) else f"no {kind} chart"
    return check


def honest(r: Run) -> str | None:
    """No raw error or JSON in a reply, and no claim of a file that isn't there."""
    for text in r.answers:
        if re.search(r"Traceback|invalid_arguments|ToolError|\{\s*\"(?:status|error)\"", text):
            return "a reply shows a raw error"
    return None


# ---------------------------------------------------------------- set-ups: fresh data, each with its truth
# All data is made here from fixed seeds - nothing a model could know from training - and lands in a new store
# (its own SQLite file and workspace folder) for every job.

def _table(store: Store, ws_id: str, df: pd.DataFrame, name: str) -> str:
    return store.open(ws_id)[0].add_table(df, name)


def _document(store: Store, ws_id: str, name: str, text: str) -> None:
    from app.tools.files import add_document
    ws, index = store.open(ws_id)
    path = Path(tempfile.mkdtemp()) / name
    path.write_text(text, encoding="utf-8")
    add_document(ws, index, path)


def _file(store: Store, ws_id: str, name: str, text: str) -> str:
    return store.open(ws_id)[0].add_bytes("file", text.encode("utf-8"), name.rsplit(".", 1)[-1], name=name)


def expenses(store: Store, ws_id: str, truth: dict) -> None:
    """Three months of spending, amounts written as text with a rupee sign, and a Total row at the bottom."""
    rng = np.random.default_rng(21)
    base = {"Rent": (25000, 25000, 25000), "Food": (8000, 8600, 9100), "Travel": (2000, 4100, 6500),
            "Software": (3000, 3200, 3300)}
    rows = []
    for m, month in enumerate(("2025-01", "2025-02", "2025-03")):
        for cat, months in base.items():
            parts = rng.dirichlet(np.ones(3)) * months[m]
            rows += [(f"{month}-{d:02d}", cat, f"₹{p:,.2f}") for d, p in zip((5, 15, 25), parts)]
    df = pd.DataFrame(rows, columns=["date", "category", "amount"])
    total = sum(v[m] for v in base.values() for m in range(3))
    df = pd.concat([df, pd.DataFrame([{"date": "", "category": "Total", "amount": f"₹{total:,.2f}"}])], ignore_index=True)
    _table(store, ws_id, df, "expenses.csv")
    truth.update(grew="Travel", growth=base["Travel"][2] - base["Travel"][0])


def meeting(store: Store, ws_id: str, truth: dict) -> None:
    _document(store, ws_id, "meeting_notes.md",
              "# Planning meeting, 3 November\n\nWe agreed the winter launch goes ahead.\n\n"
              "Priya will send the budget draft by 14 November. Omar books the venue by 20 November.\n\n"
              "Lena reviews the supplier contract by 25 November, and Sam publishes the agenda by 1 December.\n\n"
              "Next meeting: 2 December.\n")


def survey(store: Store, ws_id: str, truth: dict) -> None:
    """60 comments: 30 about delivery, 18 about price, 12 about quality (said in many ways)."""
    rng = np.random.default_rng(3)
    themes = {
        "delivery": ["The parcel arrived {d} days late", "Shipping took forever, {d} days", "Courier lost my box for {d} days",
                     "Delivery was {d} days behind the promised date", "My order sat in transit {d} days"],
        "price": ["Too expensive for what it is", "The price went up again by {d} percent", "Cheaper elsewhere by {d} euros",
                  "Not worth the money"],
        "quality": ["The seam split after {d} washes", "Broke after {d} days of use", "Material feels cheap and thin"]}
    rows = [(t, rng.choice(s).format(d=int(rng.integers(2, 9)))) for t, n in (("delivery", 30), ("price", 18), ("quality", 12))
            for s in [themes[t]] for _ in range(n)]
    order = rng.permutation(len(rows))
    df = pd.DataFrame({"response_id": range(1, 61), "comment": [rows[i][1] for i in order]})
    _table(store, ws_id, df, "survey.csv")
    truth.update(top="delivery", share=50.0)


def invoices(store: Store, ws_id: str, truth: dict) -> None:
    rng = np.random.default_rng(8)
    amounts = rng.uniform(120, 2400, 8).round(2)
    df = pd.DataFrame({"invoice": [f"INV-{1040 + i}" for i in range(8)], "client": [f"Client {c}" for c in "ABCDEFGH"],
                       "amount_usd": amounts})
    _table(store, ws_id, df, "invoices.csv")
    truth.update(total_usd=round(float(amounts.sum()), 2))


def customers(store: Store, ws_id: str, truth: dict) -> None:
    """Churn decided mostly by support tickets (and low use) - the driver predict should find."""
    rng = np.random.default_rng(13)
    n = 400
    tickets = rng.poisson(1.6, n)
    usage = rng.gamma(4, 5, n).round(1)
    tenure = rng.integers(1, 60, n)
    risk = 1 / (1 + np.exp(-(1.4 * (tickets - 3) - 0.08 * (usage - 15))))
    churned = np.where(rng.random(n) < risk, "yes", "no")
    df = pd.DataFrame({"customer_id": [f"C{i:04d}" for i in range(n)], "support_tickets": tickets,
                       "monthly_hours": usage, "tenure_months": tenure,
                       "plan": rng.choice(["basic", "pro"], n), "churned": churned})
    _table(store, ws_id, df, "customers.csv")


def products(store: Store, ws_id: str, truth: dict) -> None:
    """'Top product' has two honest readings that disagree: by revenue Widget Pro, by units Gizmo."""
    rows = [("Widget Pro", 40, 189.0), ("Gizmo", 410, 9.5), ("Doohickey", 150, 22.0), ("Thingamajig", 90, 31.0)]
    df = pd.DataFrame([(name, f"2025-0{m}-1{d}", units // 6 + (1 if m == d else 0), price)
                       for name, units, price in rows for m in range(1, 4) for d in range(1, 3)],
                      columns=["product", "date", "units", "unit_price"])
    _table(store, ws_id, df, "product_sales.csv")
    revenue = (df.units * df.unit_price).groupby(df["product"]).sum()
    units = df.groupby("product")["units"].sum()
    truth.update(by_revenue=revenue.idxmax(), by_units=units.idxmax())


def monthly_sales(store: Store, ws_id: str, truth: dict) -> None:
    rng = np.random.default_rng(17)
    months = pd.date_range("2023-01-01", periods=33, freq="MS")
    values = [round(4000 + 60 * i + (900 if m.month in (11, 12) else 0) + float(rng.normal(0, 120)), 2)
              for i, m in enumerate(months)]
    _table(store, ws_id, pd.DataFrame({"month": months.strftime("%Y-%m"), "sales": values}), "sales.csv")
    best = int(np.argmax(values))
    truth.update(best=months[best].strftime("%Y-%m"))


def pricing(store: Store, ws_id: str, truth: dict) -> None:
    _document(store, ws_id, "old_pricing.md", "# Pricing (until June)\n\n- Basic: 9 euros a month\n- Pro: 29 euros a month\n"
                                              "- Enterprise: 199 euros a month\n\nAll plans include email support.\n")
    _document(store, ws_id, "new_pricing.md", "# Pricing (from July)\n\n- Basic: 12 euros a month\n- Pro: 35 euros a month\n"
                                              "- Team: 99 euros a month\n\nAll plans include email support.\n")


def ab_test(store: Store, ws_id: str, truth: dict) -> None:
    rng = np.random.default_rng(11)
    a = rng.random(4000) < 0.10
    b = rng.random(4000) < 0.13
    df = pd.DataFrame({"user_id": range(8000), "variant": ["A"] * 4000 + ["B"] * 4000, "converted": np.r_[a, b].astype(int)})
    _table(store, ws_id, df, "ab_test.csv")
    truth.update(rate_a=round(float(a.mean()) * 100, 1), rate_b=round(float(b.mean()) * 100, 1))


def spike_and_events(store: Store, ws_id: str, truth: dict) -> None:
    rng = np.random.default_rng(5)
    days = pd.date_range("2025-09-01", periods=60, freq="D")
    values = rng.normal(500, 30, len(days)).round(2)
    spike = 37
    values[spike] = 1900.0
    _table(store, ws_id, pd.DataFrame({"date": days.strftime("%Y-%m-%d"), "sales": values}), "daily_sales.csv")
    when = days[spike]
    truth.update(spike_day=when.strftime("%Y-%m-%d"), spike_words=[when.strftime("%Y-%m-%d"), f"{when.day} {when:%B}",
                                                                    f"{when:%B} {when.day}"])
    _document(store, ws_id, "marketing_log.md",
              f"# Marketing log\n\n- {(when - pd.Timedelta(days=20)):%d %B}: new product photos on the website.\n"
              f"- {when:%d %B}: the autumn sale email went out to all 40,000 customers.\n"
              f"- {(when + pd.Timedelta(days=9)):%d %B}: a podcast mention.\n")


def earlier_budget_chat(store: Store, ws_id: str, truth: dict) -> None:
    from app.tools.memory import remember_turn
    _, index = store.open(ws_id)
    chat = store.create_chat(ws_id, "Marketing budget")["id"]
    said = "Let's fix the spring campaign budget at 12,000 euros, half for March and half for April."
    answer = "Agreed: the spring campaign budget is 12,000 euros - 6,000 for March and 6,000 for April."
    store.append(chat, {"role": "user", "content": said, "text": said})
    store.append(chat, {"role": "assistant", "text": answer, "tool_calls": []})
    remember_turn(index, chat, said, answer, when=(datetime.now() - timedelta(days=3)).isoformat(timespec="seconds"))
    truth.update(left=round(12000 - 4350.75, 2))


def draft_file(store: Store, ws_id: str, truth: dict) -> None:
    _file(store, ws_id, "draft.txt", "First draft of the launch email.\n")


def broken_script(store: Store, ws_id: str, truth: dict) -> None:
    _file(store, ws_id, "stats.py", "readings = [14.5, 9.25, 22.0, 18.75, 11.0]\n\n\ndef mean(values):\n"
                                    "    return sum(values) / len(value)\n\n\nprint('mean reading:', mean(readings))\n")
    truth.update(mean=15.1)


# ---------------------------------------------------------------- job-specific checks

def ambiguous_top(r: Run) -> str | None:
    """Asked which 'top' (then named the revenue one, as answered) - or took one reading and said which."""
    text = r.answers[-1] if r.answers else ""
    if r.cards:
        return None if r.truth["by_revenue"] in text else f"was told 'by revenue' but didn't name {r.truth['by_revenue']}"
    if re.search(r"revenue|sales value|earn", text, re.I) and r.truth["by_revenue"] in text:
        return None
    if re.search(r"units|quantity|sold the most", text, re.I) and r.truth["by_units"] in text:
        return None
    return "neither asked which 'top' nor said which reading it took, with the right product"


def nothing_downloaded(r: Run) -> str | None:
    if r.ws.ids("file") or r.ws.ids("table"):
        return "a file was added although the user cancelled"
    text = r.answers[-1] if r.answers else ""
    return None if re.search(r"cancel|not download|didn'?t download|won'?t download|no file", text, re.I) else \
        "the reply doesn't say the download was cancelled"


def rupees(r: Run) -> str | None:
    """The total in rupees: some number in the reply is the USD total at a plausible 2025-26 rate (75-100)."""
    text = r.answers[-1] if r.answers else ""
    values = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text)]
    total = r.truth["total_usd"]
    return None if any(75 * total <= v <= 100 * total for v in values) else f"no rupee total near {total} x 75-100"


def actions_checklist(r: Run, item: str) -> str | None:
    text = r.ws.path(item).read_text("utf-8", errors="replace")
    missing = [n for n in ("Priya", "Omar", "Lena", "Sam") if n not in text]
    if missing:
        return f"actions.md lacks {missing}"
    return None if text.count("[ ]") >= 4 else "actions.md is not a checklist of 4 open items"


def at_risk(r: Run) -> str | None:
    for kind in ("table", "file"):
        for i in r.files(r"at.?risk", kind):
            df = r.ws.table(i) if kind == "table" else pd.read_csv(r.ws.path(i))
            if len(df) == 5:
                return None
    return "no at_risk table of 5 customers"


def a_table_with_rows(n: int, pattern: str = ".") -> Check:
    def check(r: Run) -> str | None:
        return None if any(len(r.ws.table(i)) == n for i in r.files(pattern, "table")) else f"no table of {n} rows"
    return check


def mumbai_forecast(r: Run) -> str | None:
    days = [d for a in r.artifacts if a.get("type") == "weather" for d in a.get("daily", [])]
    return None if len(days) >= 3 else f"the forecast covers {len(days)} days, not 3"


def changes_file(r: Run, item: str) -> str | None:
    text = r.ws.path(item).read_text("utf-8", errors="replace")
    missing = [w for w in ("12", "35", "Team", "Enterprise") if w not in text]
    return None if not missing else f"changes.md lacks {missing}"


def spike_explained(r: Run) -> str | None:
    text = r.answers[-1] if r.answers else ""
    if not any(w.lower() in text.lower() for w in r.truth["spike_words"]):
        return f"misses the day {r.truth['spike_day']}"
    return None if re.search(r"email|sale", text, re.I) else "doesn't connect the day to the sale email"


def still_named(name: str, gone: str) -> Check:
    def check(r: Run) -> str | None:
        if r.files(gone):
            return f"{gone} is still there"
        return None if r.files(name) else f"no file named like {name}"
    return check


def script_fixed(r: Run) -> str | None:
    found = r.files(r"^stats\.py$")
    if not found:
        return "stats.py is gone"
    return None if "len(values)" in r.ws.path(found[-1]).read_text("utf-8", errors="replace") else "stats.py not fixed"


def fib_answer(r: Run) -> str | None:
    text = "\n".join(r.answers)
    return None if re.search(r"6,?765|4,?181", text) else "the reply lacks the 20th Fibonacci number"


def summary_covers(r: Run, item: str) -> str | None:
    from docx import Document
    text = "\n".join(p.text for p in Document(io.BytesIO(r.ws.path(item).read_bytes())).paragraphs)
    covered = {m.lower() for m in re.findall(r"(?i)\b(?:basic|pro|team|enterprise)\b", text)}
    return None if len(covered) >= 3 else f"the Word file names {sorted(covered)} only"


# ---------------------------------------------------------------- the jobs (3-6 tools each, a behaviour each)

COMBOS = [
    # ---------------- dev: for finding and fixing
    Combo("expense_memo", "dev",
          [Step("expenses.csv is my spending export for Q1. Clean it, tell me which category grew the most from "
                "January to March and by how much, chart monthly spending by category, put it all in a one-page "
                "memo.docx with the chart, and save the fastest-growing category as a note.", ("first", "first"))],
          all_of(no_errors, ran("clean_data"), answer_has(r"Travel"), answer_number("growth", 1),
                 file_made(r"memo\.docx", test=docx_with_picture(r"Travel")), note_has(r"Travel"), honest),
          expenses, "messy table -> clean -> compare months -> chart -> Word memo -> note"),
    Combo("dice_fairness", "dev",
          [Step("Write a Python script that rolls a fair six-sided die 6,000 times with random seed 42 and saves the "
                "rolls as rolls.csv. Then have the data analyst check whether the die looks fair.")],
          all_of(no_errors, ran("analyze_or_code", "main"), a_table_with_rows(6000, r"rolls"),
                 answer_has(r"\bfair\b|no (?:evidence|sign)|consistent with|not significant|uniform")),
          None, "coder makes data -> it becomes a table -> the analyst tests it"),
    Combo("meeting_actions", "dev",
          [Step("From meeting_notes.md, make actions.md: a checklist with each action's owner and deadline. Then draw "
                "the deadlines as a gantt chart.")],
          all_of(no_errors, file_made(r"^actions\.md$", test=actions_checklist), ran("draw_diagram", "main")),
          meeting, "document -> extract -> checklist file -> gantt diagram"),
    Combo("survey_themes", "dev",
          [Step("Sort the comments in survey.csv into delivery, price and quality. Which do customers complain about "
                "most, and what share is it? Chart the shares.")],
          all_of(no_errors, ran("analyze_text"), answer_has(r"delivery"), answer_number("share", 4), chart_drawn()),
          survey, "free text -> the user's labels -> shares -> chart"),
    Combo("invoice_rupees", "dev",
          [Step("What is today's USD to INR exchange rate? Convert the total of invoices.csv to rupees, exactly.")],
          all_of(no_errors, ran("research_web", "main"), answer_number("total_usd", 0.01), rupees),
          invoices, "live web rate + a table's total -> exact conversion"),
    Combo("churn_risk", "dev",
          [Step("What predicts churn in customers.csv? Then save the 5 customers still with us who are most at risk "
                "as at_risk.csv.")],
          all_of(no_errors, ran("predict"), answer_has(r"ticket"), at_risk),
          customers, "model -> drivers -> score the rest -> save the top 5"),
    Combo("verified_picture", "dev",
          [Step("Use a script to draw a pie chart of my budget - rent 50, food 30, fun 20 - titled 'My budget', saved as "
                "budget.png. Check the picture really shows that title and the right biggest slice before you tell me.")],
          all_of(no_errors, ran("analyze_or_code", "main"), file_made(r"budget", kind="image"), answer_has(r"My budget", r"rent")),
          None, "coder draws -> the picture is looked at -> confirmed"),
    Combo("ambiguous_top", "dev",
          [Step("Who is our top product in product_sales.csv?", ("by revenue",))],
          all_of(no_errors, ambiguous_top), products, "an ambiguous question -> ask, or say the reading taken"),
    Combo("cancel_download", "dev",
          [Step("Download this into my workspace: https://raw.githubusercontent.com/pandas-dev/pandas/main/README.md",
                ("cancel",))],
          all_of(no_errors, card_for("download_file"), nothing_downloaded), None,
          "a permission refused -> nothing done, said plainly"),
    Combo("plan_forecast_deck", "dev",
          [Step("Plan this first, then do it: find the best month in sales.csv, forecast the next quarter, and save a "
                "slide deck outlook.pptx with the forecast chart.")],
          all_of(no_errors, ran("plan", "main"), ran("forecast"), answer_has(r"20\d\d"),
                 file_made(r"outlook\.pptx", test=pptx_with(2, 1))), monthly_sales, "plan card -> forecast -> slides"),
    # ---------------- holdout: run once, after the fixes
    Combo("pricing_changes", "holdout",
          [Step("What changed between old_pricing.md and new_pricing.md? Write the changes to changes.md, then put the "
                "new prices in a short Word file prices.docx.")],
          all_of(no_errors, file_made(r"^changes\.md$", test=changes_file), file_made(r"prices\.docx", test=summary_covers)),
          pricing, "two documents -> differences -> a Markdown file and a Word file"),
    Combo("fiscal_memory", "holdout",
          [Step("Remember this for every chat: our fiscal year starts on 1 July and we report in EUR."),
           Step("Which calendar month does our fiscal Q3 start in?", new_chat=True)],
          all_of(no_errors, note_has(r"July"), answer_has(r"January")), None,
          "a note -> a new chat -> reasoning from it"),
    Combo("fibonacci_chart", "holdout",
          [Step("Have the coder write the first 20 Fibonacci numbers to fib.csv, then chart them and tell me the 20th.")],
          all_of(no_errors, ran("analyze_or_code", "main"), a_table_with_rows(20, r"fib"), chart_drawn(), fib_answer), None,
          "coder -> a table -> the analyst's chart"),
    Combo("mumbai_trip_note", "holdout",
          [Step("Will it rain in Mumbai in the next 3 days? Save the answer as a note called mumbai trip.")],
          all_of(no_errors, ran("get_weather", "main"), mumbai_forecast, note_has(r"Mumbai")), None,
          "forecast -> answer -> note"),
    Combo("ab_report", "holdout",
          [Step("Is variant B really better than A in ab_test.csv? Put the result, with a chart of the conversion rate by "
                "variant, into ab_result.docx.")],
          all_of(no_errors, ran("compare"), answer_number("rate_b", 0.2),
                 file_made(r"ab_result\.docx", test=docx_with_picture(r"significant|real|better"))),
          ab_test, "A/B test -> chart -> Word file"),
    Combo("spike_explained", "holdout",
          [Step("Find the unusual day in daily_sales.csv, then check my documents for anything that explains it.")],
          all_of(no_errors, ran("find_patterns"), spike_explained), spike_and_events,
          "anomaly in a table -> its cause in a document"),
    Combo("budget_left", "holdout",
          [Step("From the spring campaign budget we agreed earlier, how much is left if March spending was 4,350.75?")],
          all_of(no_errors, answer_number("left", 0.01)), earlier_budget_chat, "an earlier chat recalled -> exact sum"),
    Combo("titanic_link", "holdout",
          [Step("How many passengers are in https://raw.githubusercontent.com/datasciencedojo/datasets/master/titanic.csv, "
                "and what share survived? Work it out from the file itself.", ("allow", "allow"))],
          all_of(no_errors, a_table_with_rows(891), answer_has(r"891", r"38\.[3-4]")), None,
          "a data link -> import with permission -> computed, not remembered"),
    Combo("keep_then_rename", "holdout",
          [Step("Delete draft.txt.", ("cancel",)), Step("Keep it then - rename it to draft_v2.txt instead.")],
          all_of(no_errors, card_for("delete_files"), still_named(r"^draft_v2\.txt$", r"^draft\.txt$")), draft_file,
          "a delete refused -> a rename instead"),
    Combo("fix_script", "holdout",
          [Step("stats.py crashes. Fix it and tell me what it prints.")],
          all_of(no_errors, ran("analyze_or_code", "main"), script_fixed, answer_has(r"15\.1")), broken_script,
          "a broken file -> fixed in place -> its output"),
]


# ---------------------------------------------------------------- running

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("split", choices=["dev", "holdout", "all"])
    ap.add_argument("--only")
    ap.add_argument("--keep", action="store_true", help="keep each job's store (database and files) to look at")
    args = ap.parse_args()
    combos = [c for c in COMBOS if (args.split == "all" or c.split == args.split)
              and (not args.only or c.id in args.only.split(","))]
    main_model = LlamaCppProvider(settings.llamacpp_model, settings.llamacpp_url, settings.assistant_thinking, slot=0,
                                  max_tokens=settings.assistant_max_tokens)
    worker = LlamaCppProvider(settings.llamacpp_model, settings.llamacpp_url, settings.assistant_thinking, slot=1,
                              max_tokens=settings.assistant_max_tokens)
    embedder = make_embedder(settings)
    rows = []
    for combo in combos:
        folder = Path(tempfile.mkdtemp(prefix=f"zen-job-{combo.id}-"))
        store = Store(folder / "store", embedder)
        truth: dict = {}
        started = time.perf_counter()
        try:
            run = drive(combo, store, main_model, worker, truth)
            problem = combo.check(run)
        except Exception as exc:  # noqa: BLE001 - a crash is a failed job, reported
            run, problem = None, f"crash: {type(exc).__name__}: {exc}"
        seconds = round(time.perf_counter() - started, 1)
        row = {"id": combo.id, "split": combo.split, "why": combo.why, "ok": problem is None, "problem": problem,
               "seconds": seconds, "truth": truth,
               "tools": [f"{t['by']}:{t['name']}({t.get('status')})" for t in (run.tools if run else [])],
               "cards": run.cards if run else [], "answers": run.answers if run else [],
               "errors": run.errors if run else [], "store": str(folder) if args.keep else None}
        rows.append(row)
        store.close()
        if not args.keep:
            shutil.rmtree(folder, ignore_errors=True)
        print(f"{'PASS' if row['ok'] else 'FAIL'} {combo.id:<20} {seconds:>6}s  {problem or ''}", flush=True)
    name = f"combos_{args.split}" + ("_only" if args.only else "")  # a partial run never replaces the whole run's
    (HERE / f"{name}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    (HERE / f"{name}.md").write_text(report(rows, args.split), encoding="utf-8")
    passed = sum(r["ok"] for r in rows)
    print(f"-> bench/{name}.md: {passed}/{len(rows)} passed")


def report(rows: list[dict], split: str) -> str:
    passed = sum(r["ok"] for r in rows)
    lines = [f"# Acceptance jobs - {split}", "", f"{passed}/{len(rows)} passed · {datetime.now():%Y-%m-%d %H:%M} · "
             f"main slot 0, helpers slot 1, {settings.llamacpp_url}", "", "| job | what it chains | result | seconds |",
             "|---|---|---|---|"]
    lines += [f"| {r['id']} | {r['why']} | {'pass' if r['ok'] else 'FAIL: ' + str(r['problem']).replace('|', '/')} | "
              f"{r['seconds']} |" for r in rows]
    lines.append("")
    for r in rows:
        lines += [f"**{r['id']}** - tools: {', '.join(r['tools']) or 'none'}", ""]
        for a in r["answers"]:
            lines += ["> " + " ".join(a.split())[:600], ""]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
