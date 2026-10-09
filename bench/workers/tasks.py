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
    file: str | None = None  # the file to keep or change, as main gives it (analyze_or_code file=...)


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
    """predict ran without the target's own source as a predictor, and the answer gives its score. (The model
    item this read was dropped from the app on 2026-10-08: predict reports its drivers as a table card and its
    score in the result.)"""
    drivers = [a for a in run.artifacts if a.get("type") == "table" and str(a.get("title", "")).startswith("What predicts")]
    if not drivers:
        return verdict(False, "no model trained")
    leak = any(str(row[0]).lower() == "affairs" for row in drivers[-1].get("rows", []))
    answer = run.out.get("answer", "")
    cited = any(has_number(answer, s / 100, 0.006) or has_number(answer, s, 0.6) for s in range(68, 83))
    ok = not leak and cited and clean(run)
    return verdict(ok, f"features include affairs: {leak}; answer cites a score of 0.68-0.82: {cited}")


def _ambiguous_check(run: Run):
    # Asking: a helper asks in place with ask_user now (shown as a card in the app); before, it handed the
    # question back to main (question_for_user) or said it in the answer.
    asked_card = any(t.get("name") == "ask_user" and re.search(r"day|month|date", str(t.get("input")), re.I)
                     for t in run.tools)
    asked = asked_card or bool(run.out.get("question_for_user")) or re.search(r"day.{0,20}month|month.{0,20}day|date format|order of the date",
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
         lambda r: verdict(called(r, "compare") and has_number(r.out.get("answer"), r.truth["rho"], 0.006) and clean(r),
                           f"truth rho {r.truth['rho']:.3f}")),
    Task("h_forecast", "data", "holdout", "Forecast real GDP for the next 4 quarters.", _macro,
         lambda r: verdict(called(r, "forecast") and any(a.get("chart") == "forecast" for a in r.artifacts) and clean(r),
                           "forecast made from year + quarter columns")),
    Task("h_ambiguous", "data", "holdout", "What were the total sales in February 2025?", _ambiguous, _ambiguous_check),
]


# ---------------------------------------------------------------- harder data tasks (step 8: the guide)
# Each needs a method an expert knows and a small model often gets wrong; the guide has a section on
# each (on other data). Run with and without the guide: python -m bench.workers.run guide [--no-guide].

def _pizza_hard(ws, index, truth):
    import itertools
    df = pd.read_csv(DATA / "pizza_sales.csv")
    ws.add_file(DATA / "pizza_sales.csv")
    df["rev"] = df.quantity * df.price
    top = df.groupby(["category", "name"]).rev.sum().sort_values(ascending=False).groupby("category").head(2)
    truth["top2"] = [n.replace("The ", "").replace(" Pizza", "") for _, n in top.index]
    monthly = df.assign(m=pd.to_datetime(df.date).dt.month_name()).groupby("m", sort=False).rev.sum()
    change = monthly.pct_change() * 100
    truth["drop_month"], truth["drop_pct"] = change.idxmin(), change.min()
    pairs = Counter()
    for _, names in df.groupby("order_id").name:
        pairs.update(itertools.combinations(sorted(set(names)), 2))
    (truth["pair"], truth["pair_orders"]), = pairs.most_common(1)
    truth["l_share_by_cat"] = (df[df["size"] == "L"].groupby("category").rev.sum()
                               / df.groupby("category").rev.sum() * 100).to_dict()
    s = df.groupby("name").rev.sum().sort_values(ascending=False)
    truth["pareto"] = int(((s.cumsum() - s) / s.sum() < 0.8).sum())  # types needed to reach 80%
    truth["median_order"] = df.groupby("order_id").rev.sum().median()


def _short(name: str) -> str:
    return name.replace("The ", "").replace(" Pizza", "")


GUIDE_TASKS = [
    Task("g_top2", "data", "guide", "What are the two best-selling pizzas by revenue in each category?", _pizza_hard,
         lambda r: verdict(all(says(r.out.get("answer"), n) for n in r.truth["top2"]) and clean(r),
                           f"truth {r.truth['top2']}")),
    Task("g_drop", "data", "guide", "Which month had the largest month-on-month percent drop in revenue, and how big "
         "was it?", _pizza_hard,
         lambda r: verdict((says(r.out.get("answer"), r.truth["drop_month"]) or says(r.out.get("answer"), "2015-12"))
                           and any(has_number(r.out.get("answer"), v, 0.06) for v in (r.truth["drop_pct"], -r.truth["drop_pct"]))
                           and clean(r),
                           f"truth {r.truth['drop_month']} {r.truth['drop_pct']:.2f}%")),
    Task("g_pair", "data", "guide", "Which two pizzas (by name) are most often ordered together in the same order, "
         "and in how many orders?", _pizza_hard,
         lambda r: verdict(all(says(r.out.get("answer"), _short(n)) for n in r.truth["pair"])
                           and has_number(r.out.get("answer"), r.truth["pair_orders"], 0.5) and clean(r),
                           f"truth {r.truth['pair']} in {r.truth['pair_orders']} orders")),
    Task("g_large_share", "data", "guide", "Within each category, what share of that category's revenue comes from "
         "large (L) pizzas?", _pizza_hard,
         lambda r: verdict(all(has_number(r.out.get("answer"), v, 0.06) or has_number(r.out.get("answer"), v / 100, 0.0006)
                               for v in r.truth["l_share_by_cat"].values()) and clean(r),
                           "truth " + ", ".join(f"{k} {v:.2f}%" for k, v in r.truth["l_share_by_cat"].items()))),
    Task("g_pareto", "data", "guide", "How many pizza types (names), taken from the biggest down, make up 80% of "
         "revenue?", _pizza_hard,
         lambda r: verdict(has_number(r.out.get("answer"), r.truth["pareto"], 0.5) and clean(r),
                           f"truth {r.truth['pareto']} of 32")),
    Task("g_median_order", "data", "guide", "What is the median order value (an order can hold several pizzas)?",
         _pizza_hard, lambda r: verdict(has_number(r.out.get("answer"), r.truth["median_order"], 0.006) and clean(r),
                                        f"truth {r.truth['median_order']} (the median line is 16.5)")),
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
    ws.add_bytes("file", b"nums = [3, 8, 1, 9, 4]\nprint(sum(nums) / len(nums))\n", "py", name="stats.py")
    counts = Counter(TEXT.split())
    truth["top_word"], truth["top_count"] = counts.most_common(1)[0]


def _ran(r: Run, *expected: str):
    """The helper ran its code and kept the file, and the report's facts (written by the app) show the output."""
    report = r.out.get("report") or ""
    ok = (all(e.lower() in report.lower() for e in expected) and (called(r, "run_code") or called(r, "fix_code"))
          and bool(r.out.get("files")) and "- Last run: passed" in report and clean(r))
    return verdict(ok, f"kept with a report showing {expected}")


def _edited(r: Run):
    """A change to the user's own file, with nobody to ask (a benchmark): theirs stays as it was, and the changed
    program is kept under another name, its report printing the mean and the largest number."""
    report = r.out.get("report") or ""
    stats = next(i for i, e in r.ws._reg["items"].items() if e.get("name") == "stats.py")
    untouched = r.ws.path(stats).read_bytes() == b"nums = [3, 8, 1, 9, 4]\nprint(sum(nums) / len(nums))\n"
    ok = (untouched and "5.0" in report and "9" in report.split("It printed:")[-1] and called(r, "fix_code")
          and bool(r.out.get("files")) and clean(r))
    return verdict(ok, "stats.py untouched, the change kept under another name, report prints 5.0 and 9")


CODER_TASKS = [
    Task("c_fib", "coder", "dev", "Write a script that prints the 30th Fibonacci number (F1 = F2 = 1).", _coder_files,
         lambda r: _ran(r, "832040"), file="fibonacci.py"),
    Task("c_words", "coder", "dev", "Count the words in file_1 and print the three most common words with their counts.",
         _coder_files, lambda r: _ran(r, r.truth["top_word"], str(r.truth["top_count"])), file="word_counts.py"),
    Task("c_fix", "coder", "dev", "Fix the script in file_2 so it runs, and show what it prints.", _coder_files,
         lambda r: _ran(r, "5.5"), file="average_fixed.py"),
    Task("c_json", "coder", "dev", "Convert file_3 (a CSV) to a JSON file with a list of objects, and say how many "
         "records it has.", _coder_files,
         lambda r: verdict(_ran(r, "4")[0] and any(e["kind"] == "file" and e["name"].endswith(".json")
                                                 for e in r.ws._reg["items"].values()), "4 records + a .json file"),
         file="csv_to_json.py"),
    Task("h_primes", "coder", "holdout", "How many prime numbers are there below 10,000? Compute it.", _coder_files,
         lambda r: _ran(r, "1229"), file="primes.py"),
    Task("h_days", "coder", "holdout", "How many days are there from 2024-02-15 to 2025-03-01? Compute it with code.",
         _coder_files, lambda r: _ran(r, "380"), file="days_between.py"),
    Task("h_emails", "coder", "holdout", "Extract the distinct email addresses from file_4 (ignore case) and print them sorted.",
         _coder_files, lambda r: _ran(r, "anna@example.com", "bob.smith@mail.example.org", "x@test.io"),
         file="emails.py"),
    Task("c_edit", "coder", "dev", "Change the script so it also prints the largest number, after the mean.",
         _coder_files, _edited, file="stats.py"),
]

# Fresh hard tasks on other data, written before the guide was revised after the GUIDE_TASKS runs and
# never tuned on: the clean measure of whether the guide helps (python -m bench.workers.run fresh).

def _cdnow(ws, index, truth):
    c = pd.read_csv(DATA / "cdnow.csv", parse_dates=["date"])
    ws.add_file(DATA / "cdnow.csv")
    truth["repeat_pct"] = (c.groupby("customer_id").date.nunique() >= 2).mean() * 100
    last = c.date.max()
    truth["recent"] = [c[c.date > last - pd.Timedelta(days=w)].customer_id.nunique() for w in (89, 90, 91)]
    first = c.groupby("customer_id").date.min()
    jan = first[(first >= "1997-01-01") & (first < "1997-02-01")].index
    truth["jan_again_pct"] = (c[c.customer_id.isin(jan) & (c.date >= "1997-02-01")].customer_id.nunique()
                              / len(jan) * 100)
    truth["median_customer"] = c.groupby("customer_id").amount.sum().median()


def _coffee(ws, index, truth):
    k = pd.read_excel(DATA / "coffee_shop_sales.xlsx")
    ws.add_file(DATA / "coffee_shop_sales.xlsx")
    k["rev"] = k.transaction_qty * k.unit_price
    m = k.assign(m=k.transaction_date.dt.month).groupby(["store_location", "m"]).rev.sum().unstack()
    growth = (m[6] / m[1] - 1) * 100
    truth["growth_store"], truth["growth_pct"] = growth.idxmax(), growth.max()
    top = k.groupby(["store_location", "product_type"]).rev.sum().sort_values(ascending=False).groupby(
        "store_location").head(3)
    truth["top3"] = [float(v) for v in top.values]


FRESH_TASKS = [
    Task("f_repeat", "data", "fresh", "What share of customers came back and bought again on a later day?", _cdnow,
         lambda r: verdict(has_number(r.out.get("answer"), r.truth["repeat_pct"], 0.06) and clean(r),
                           f"truth {r.truth['repeat_pct']:.2f}% (distinct purchase days, not rows)")),
    Task("f_recent", "data", "fresh", "How many customers made a purchase in the last 90 days covered by the data?",
         _cdnow, lambda r: verdict(any(has_number(r.out.get("answer"), v, 0.5) for v in r.truth["recent"]) and clean(r),
                                   f"truth {r.truth['recent']} (90 days back from the data's last date, not today)")),
    Task("f_cohort", "data", "fresh", "Of the customers whose first purchase was in January 1997, what share bought "
         "again in a later month?", _cdnow,
         lambda r: verdict(has_number(r.out.get("answer"), r.truth["jan_again_pct"], 0.06) and clean(r),
                           f"truth {r.truth['jan_again_pct']:.2f}%")),
    Task("f_median_customer", "data", "fresh", "What is the median total amount a customer spent?", _cdnow,
         lambda r: verdict(has_number(r.out.get("answer"), r.truth["median_customer"], 0.006) and clean(r),
                           f"truth {r.truth['median_customer']:.3f} (the median row is 25.98)")),
    Task("f_store_growth", "data", "fresh", "Which store's revenue grew the most from January to June 2023, in "
         "percent?", _coffee,
         lambda r: verdict(says(r.out.get("answer"), r.truth["growth_store"])
                           and has_number(r.out.get("answer"), r.truth["growth_pct"], 0.06) and clean(r),
                           f"truth {r.truth['growth_store']} {r.truth['growth_pct']:.2f}%")),
    Task("f_top3_store", "data", "fresh", "What are the top 3 product types by revenue in each store, with their "
         "revenue?", _coffee,
         lambda r: verdict(all(has_number(r.out.get("answer"), v, 0.5) for v in r.truth["top3"]) and clean(r),
                           f"truth {r.truth['top3']}")),
]

TASKS = DATA_TASKS + KNOWLEDGE_TASKS + WEB_TASKS + CODER_TASKS + GUIDE_TASKS + FRESH_TASKS
