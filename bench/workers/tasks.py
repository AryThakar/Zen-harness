"""Worker benchmark tasks: real data, real documents, live web; every answer checked by code.

Each task belongs to the dev set (used while improving the workers) or the holdout set (run once,
after the improvements, to see whether they generalise). Truths for data tasks are computed with
pandas from the same files, not written by hand.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from app.assistant.workers import numbers_in
from app.tools.files import add_document
from app.tools.memory import remember_turn
from app.tools.notes import save_note

DATA = Path(os.environ.get("WORKER_BENCH_DATA", r"C:\zen-data\analyst\bench\data\locked"))
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@dataclass
class Task:
    id: str
    role: str
    split: str
    task: str
    setup: Callable  # (ws, index, truth: dict) -> None
    check: Callable  # (run) -> (passed, detail)


@dataclass
class Run:
    out: dict
    tools: list[dict]       # tool calls the worker made: {"name", "input"}
    artifacts: list[dict]   # what was sent to the UI
    ws: object
    truth: dict


def has_number(text: str, value: float, tol: float) -> bool:
    return any(abs(v - value) <= tol for _, v, _, _ in numbers_in(text or ""))


def says(text: str, *words: str) -> bool:
    return all(w.lower() in (text or "").lower() for w in words)


def clean(run: Run) -> bool:
    return not run.out.get("warnings")


def called(run: Run, name: str) -> bool:
    return any(t["name"] == name for t in run.tools)


def verdict(ok: bool, detail: str) -> tuple[bool, str]:
    return ok, detail


# ---------------------------------------------------------------- data

def _pizza(ws, index, truth):
    df = pd.read_csv(DATA / "pizza_sales.csv")
    ws.add_file(DATA / "pizza_sales.csv")
    rev = df.quantity * df.price
    truth.update(total=rev.sum(), by_cat=rev.groupby(df.category).sum(), l_share=rev[df["size"] == "L"].sum() / rev.sum() * 100,
                 orders=df.order_id.nunique(), dates=pd.to_datetime(df.date))
    truth["weekday"] = truth["dates"].dt.day_name().groupby(truth["dates"].dt.day_name()).size().idxmax()
    by_day = df.assign(day=truth["dates"].dt.day_name()).groupby("day").order_id.nunique()
    truth["weekday"], truth["weekday_orders"] = by_day.idxmax(), by_day.max()


def _messy(ws, index, truth):
    ws.add_file(DATA / "pizza_messy.csv")
    truth["pizzas"] = int(pd.read_csv(DATA / "pizza_sales.csv").quantity.sum())


def _survey(ws, index, truth):
    ws.add_file(DATA / "marriage_survey_train.csv")


def _crime(ws, index, truth):
    df = pd.read_csv(DATA / "state_crime.csv")
    ws.add_file(DATA / "state_crime.csv")
    truth["rho"] = df[["poverty", "murder"]].corr(method="spearman").iloc[0, 1]


def _macro(ws, index, truth):
    ws.add_file(DATA / "us_macro_quarterly.csv")


def _ambiguous(ws, index, truth):
    path = ws.root / "sales_q1.csv"
    rows = ["date,amount"] + [f"{d:02d}/{m:02d}/2025,{100 + d}" for m in (1, 2, 3) for d in (1, 3, 5, 7, 9, 11)]
    path.write_text("\n".join(rows), encoding="utf-8")
    ws.add_file(path)


def _chart_check(run: Run):
    charts = [a for a in run.artifacts if a.get("type") == "chart" and a.get("chart") == "line"]
    ok = bool(charts) and len(charts[-1].get("x", [])) in (12, 11, 13) and clean(run)
    return verdict(ok, f"line charts: {len(charts)}, points: {len(charts[-1]['x']) if charts else 0}")


def _model_check(run: Run):
    models = [i for i in run.ws.ids("model")]
    if not models:
        return verdict(False, "no model trained")
    meta = run.ws.entry(models[-1])["meta"]
    leak = "affairs" in meta["features"]
    score = meta["score"]
    ok = not leak and 0.68 <= score <= 0.82 and has_number(run.out.get("answer", ""), score, 0.006) and clean(run)
    return verdict(ok, f"features include affairs: {leak}; score {score}; answer cites it: "
                       f"{has_number(run.out.get('answer', ''), score, 0.006)}")


def _ambiguous_check(run: Run):
    asked = bool(run.out.get("question_for_user")) or re.search(r"day.{0,20}month|month.{0,20}day|date format|order of the date",
                                                                run.out.get("answer", ""), re.I)
    guessed = has_number(run.out.get("answer", ""), 618, 0.5) or has_number(run.out.get("answer", ""), 3 * 100 + 4, 0.5)
    return verdict(bool(asked), f"asked about date order: {bool(asked)}; also gave a number: {guessed}")


DATA_TASKS = [
    Task("d_total", "data", "dev", "What was the total revenue (quantity times price) in this data?", _pizza,
         lambda r: verdict(has_number(r.out.get("answer"), r.truth["total"], 0.5) and clean(r), f"truth {r.truth['total']:.2f}")),
    Task("d_top_category", "data", "dev", "Which pizza category brought in the most revenue, and how much?", _pizza,
         lambda r: verdict(says(r.out.get("answer"), "classic") and has_number(r.out.get("answer"), r.truth["by_cat"]["Classic"], 0.5)
                           and clean(r), f"truth Classic {r.truth['by_cat']['Classic']:.2f}")),
    Task("d_size_share", "data", "dev", "What share of all revenue comes from large (L) pizzas?", _pizza,
         lambda r: verdict((has_number(r.out.get("answer"), r.truth["l_share"], 0.06)       # 45.89 (%)
                            or has_number(r.out.get("answer"), r.truth["l_share"] / 100, 0.0006))  # 0.4589
                           and clean(r), f"truth {r.truth['l_share']:.2f}% (or as a fraction)")),
    Task("d_messy_pizzas", "data", "dev", "How many pizzas were sold in total?", _messy,
         lambda r: verdict(has_number(r.out.get("answer"), r.truth["pizzas"], 0.5) and clean(r),
                           f"truth {r.truth['pizzas']} (a Total row doubles it)")),
    Task("d_chart", "data", "dev", "Show monthly revenue as a chart.", _pizza, _chart_check),
    Task("d_model", "data", "dev", "Train a model that predicts whether a person had an affair (affairs > 0) from "
         "their other answers, and say how good it is.", _survey, _model_check),
    Task("h_aov", "data", "holdout", "What is the average order value (total revenue divided by the number of distinct orders)?",
         _pizza, lambda r: verdict(has_number(r.out.get("answer"), r.truth["total"] / r.truth["orders"], 0.006) and clean(r),
                                   f"truth {r.truth['total'] / r.truth['orders']:.2f}")),
    Task("h_weekday", "data", "holdout", "Which day of the week has the most orders (distinct order ids)?", _pizza,
         lambda r: verdict(says(r.out.get("answer"), r.truth["weekday"]) and clean(r), f"truth {r.truth['weekday']}")),
    Task("h_corr", "data", "holdout", "Across states, is the poverty rate related to the murder rate? Test it properly.", _crime,
         lambda r: verdict(called(r, "stats_test") and has_number(r.out.get("answer"), r.truth["rho"], 0.006) and clean(r),
                           f"truth rho {r.truth['rho']:.3f}")),
    Task("h_forecast", "data", "holdout", "Forecast real GDP for the next 4 quarters.", _macro,
         lambda r: verdict(called(r, "forecast") and any(a.get("chart") == "forecast" for a in r.artifacts) and clean(r),
                           "forecast made from year + quarter columns")),
    Task("h_ambiguous", "data", "holdout", "What were the total sales in February 2025?", _ambiguous, _ambiguous_check),
]


# ---------------------------------------------------------------- knowledge

POLICY = """# Returns
Customers may return opened phones within 14 days if the box and all accessories are complete.
Unopened items can be returned within 30 days. A 10% restocking fee applies to opened laptops.

# Suppliers
Supplier invoices are paid within 45 days of receipt by bank transfer, after the goods are checked.

# Warranty
All electronics carry a 1-year warranty; repairs take up to 10 working days.
"""

HANDBOOK = [("Working hours", "Office hours are 9:00 to 18:00 on weekdays. The warehouse opens at 7:00."),
            ("Equipment", "Every employee gets a laptop on the first day; it must be returned when leaving."),
            ("Leave", "New employees get 24 days of annual leave, plus public holidays."),
            ("Expenses", "Expenses above 500 need a manager's approval before purchase.")]


def _knowledge(ws, index, truth):
    from docx import Document
    policy = ws.root / "store_policy.md"
    policy.write_text(POLICY, encoding="utf-8")
    add_document(ws, index, policy)
    doc = Document()
    doc.add_heading("Employee handbook", 0)
    for head, text in HANDBOOK:
        doc.add_heading(head, 1)
        doc.add_paragraph(text)
    handbook = ws.root / "employee_handbook.docx"
    doc.save(handbook)
    add_document(ws, index, handbook)
    save_note(ws, "Revenue column", "Use net_sales as the revenue column; exclude refunded orders.", index=index)
    remember_turn(index, "old1", "How far ahead should the sales forecast go?",
                  "We agreed to forecast 6 months ahead, by month.", when="2026-09-20T10:00:00")
    remember_turn(index, "old2", "What discount do we give for bulk orders?",
                  "Bulk orders of 50 units or more get an 8% discount.", when="2026-09-25T15:30:00")


NOT_FOUND = re.compile(r"\b(not|no|couldn'?t|cannot|can'?t|don'?t|doesn'?t|unable|isn'?t)\b.{0,60}"
                       r"\b(find|found|mention|contain|cover|information|available|include)", re.I)


def _sourced(r: Run, *words: str) -> tuple[bool, str]:
    ok = says(r.out.get("answer"), *words) and bool(r.out.get("sources")) and clean(r)
    return verdict(ok, f"expects {words}; sources {r.out.get('sources')}")


KNOWLEDGE_TASKS = [
    Task("k_returns", "knowledge", "dev", "How many days do customers have to return an opened phone?", _knowledge,
         lambda r: _sourced(r, "14")),
    Task("k_note", "knowledge", "dev", "Which column did we decide to use as revenue?", _knowledge,
         lambda r: _sourced(r, "net_sales")),
    Task("k_chat", "knowledge", "dev", "What forecast horizon did we agree on in an earlier conversation?", _knowledge,
         lambda r: _sourced(r, "6 months")),
    Task("k_missing", "knowledge", "dev", "What is the CEO's salary?", _knowledge,
         lambda r: verdict(bool(NOT_FOUND.search(r.out.get("answer", ""))) and not r.out.get("sources"),
                           f"should say it isn't covered; sources {r.out.get('sources')}")),
    Task("h_leave", "knowledge", "holdout", "How many days of annual leave do new employees get?", _knowledge,
         lambda r: _sourced(r, "24")),
    Task("h_bulk", "knowledge", "holdout", "What bulk-order discount did we talk about before?", _knowledge,
         lambda r: _sourced(r, "8%")),
    Task("h_summary", "knowledge", "holdout", "Summarise the employee handbook in a few short points.", _knowledge,
         lambda r: verdict(sum(says(r.out.get("answer"), w) for w in ("9", "laptop", "24", "500")) >= 3 and clean(r),
                           "mentions at least 3 of: 9:00, laptop, 24 days, 500")),
]


# ---------------------------------------------------------------- web (live)

def _nothing(ws, index, truth):
    pass


def _web(*words: str):
    def check(r: Run):
        ok = says(r.out.get("answer"), *words) and bool(r.out.get("sources")) and called(r, "read_webpage") and clean(r)
        return verdict(ok, f"expects {words}; sources {r.out.get('sources')}")
    return check


WEB_TASKS = [
    Task("w_duckdb_license", "web", "dev", "What license is DuckDB released under?", _nothing, _web("MIT")),
    Task("w_statsforecast", "web", "dev", "Which company develops the statsforecast Python library?", _nothing, _web("Nixtla")),
    Task("w_llama_port", "web", "dev", "On which port does llama.cpp's llama-server listen by default?", _nothing, _web("8080")),
    Task("w_parquet", "web", "dev", "Which engines can pandas.read_parquet use?", _nothing, _web("pyarrow", "fastparquet")),
    Task("h_pypi", "web", "holdout", "Which organization runs the Python Package Index (PyPI)?", _nothing,
         _web("Python Software Foundation")),
    Task("h_duckdb_lang", "web", "holdout", "What programming language is DuckDB written in?", _nothing, _web("C++")),
    Task("h_hgb", "web", "holdout", "In scikit-learn, which class implements histogram-based gradient boosting for "
         "classification?", _nothing, _web("HistGradientBoostingClassifier")),
]


# ---------------------------------------------------------------- coder

TEXT = ("the quick brown fox jumps over the lazy dog the fox was quick and the dog was lazy "
        "but the fox and the dog became friends and the quick fox stayed") * 3
BUGGY = """numbers = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
total = 0
for n in numbers
    total += n
print("Average:", total / len(number))
"""
EMAILS = "Contact anna@example.com or bob.smith@mail.example.org; old: x@test.io (do not use), ANNA@example.com again."


def _coder_files(ws, index, truth):
    ws.add_bytes("file", TEXT.encode(), "txt", name="story.txt")
    ws.add_bytes("file", BUGGY.encode(), "py", name="average.py")
    csv = "name,age,city\nAsha,31,Pune\nBen,45,Leeds\nChen,28,Shanghai\nDara,52,Dublin\n"
    ws.add_bytes("file", csv.encode(), "csv", name="people.csv")
    ws.add_bytes("file", EMAILS.encode(), "txt", name="contacts.txt")
    counts = Counter(TEXT.split())
    truth["top_word"], truth["top_count"] = counts.most_common(1)[0]


def _ran(r: Run, *expected: str):
    text = " ".join(r.out.get(k) or "" for k in ("answer", "code", "output"))
    ok = all(e.lower() in text.lower() for e in expected) and called(r, "run_python") and clean(r)
    return verdict(ok, f"expects {expected}")


CODER_TASKS = [
    Task("c_fib", "coder", "dev", "Write a script that prints the 30th Fibonacci number (F1 = F2 = 1).", _coder_files,
         lambda r: _ran(r, "832040")),
    Task("c_words", "coder", "dev", "Count the words in file_1 and print the three most common words with their counts.",
         _coder_files, lambda r: _ran(r, r.truth["top_word"], str(r.truth["top_count"]))),
    Task("c_fix", "coder", "dev", "Fix the script in file_2 so it runs, and show what it prints.", _coder_files,
         lambda r: _ran(r, "5.5")),
    Task("c_json", "coder", "dev", "Convert file_3 (a CSV) to a JSON file with a list of objects, and say how many "
         "records it has.", _coder_files,
         lambda r: verdict(_ran(r, "4")[0] and any(e["kind"] == "file" and e["name"].endswith(".json")
                                                 for e in r.ws._reg["items"].values()), "4 records + a .json file")),
    Task("h_primes", "coder", "holdout", "How many prime numbers are there below 10,000? Compute it.", _coder_files,
         lambda r: _ran(r, "1229")),
    Task("h_days", "coder", "holdout", "How many days are there from 2024-02-15 to 2025-03-01? Compute it with code.",
         _coder_files, lambda r: _ran(r, "380")),
    Task("h_emails", "coder", "holdout", "Extract the distinct email addresses from file_4 (ignore case) and print them sorted.",
         _coder_files, lambda r: _ran(r, "anna@example.com", "bob.smith@mail.example.org", "x@test.io")),
]

TASKS = DATA_TASKS + KNOWLEDGE_TASKS + WEB_TASKS + CODER_TASKS
