# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Direct tests of query_data, make_chart and compare - no model involved."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.tools.make_chart import make_chart
from app.tools.query_data import query_data
from app.tools.compare import compare
from app.tools.workspace import Workspace


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path / "ws")


@pytest.fixture
def sales(ws):
    rng = np.random.default_rng(1)
    n = 400
    dates = pd.Timestamp("2025-01-01") + pd.to_timedelta(rng.integers(0, 365, n), unit="D")
    region = rng.choice(["North", "South", "East"], n, p=[0.5, 0.3, 0.2])
    units = rng.integers(1, 6, n)
    price = rng.normal(100, 15, n).round(2)
    df = pd.DataFrame({"date": dates, "region": region, "units": units, "price": price,
                       "amount": (units * price).round(2), "returned": rng.random(n) < 0.1})
    return ws.add_table(df, name="sales.csv")


# ---------------------------------------------------------------- query_data

def test_query_exact_numbers(ws, sales):
    df = ws.table(sales)
    r = query_data(ws, f"SELECT region, round(sum(amount), 2) AS total FROM {sales} GROUP BY region ORDER BY total DESC")
    assert r.status == "ok"
    expected = df.groupby("region")["amount"].sum().round(2).sort_values(ascending=False)
    assert [row[0] for row in r.result["rows"]] == list(expected.index)
    assert [row[1] for row in r.result["rows"]] == pytest.approx(list(expected))


def test_query_is_read_only_and_scoped(ws, sales):
    for bad in [f"DELETE FROM {sales}", "SELECT * FROM read_csv('C:/Windows/win.ini')",
                "SELECT * FROM other_table", f"SELECT 1; DROP TABLE {sales}"]:
        r = query_data(ws, bad)
        assert r.status == "invalid_arguments", bad
    r = query_data(ws, f"SELECT no_such_column FROM {sales}")
    assert r.status == "invalid_arguments" and "DuckDB error" in r.result["error"]["message"]


def test_sql_that_cannot_be_read_is_refused_saying_how_to_quote(ws, sales):
    """Measured on the 26B: 'Can\\'t' (a backslash before the quote) crashed query_data as an internal error."""
    r = query_data(ws, f"SELECT count(*) FROM {sales} WHERE region = 'Can\\'t'")
    assert r.status == "invalid_arguments" and "written twice: 'Can''t'" in r.result["error"]["message"]


def test_query_save_makes_a_table(ws, sales):
    r = query_data(ws, f"SELECT date_trunc('month', date) AS month, sum(amount) AS revenue FROM {sales} GROUP BY 1",
                   save=True)
    new = r.result["saved_as"]
    assert ws.entry(new)["parent"] == sales and len(ws.table(new)) == 12


def test_query_large_result_is_cut_for_the_model(ws, sales):
    r = query_data(ws, f"SELECT * FROM {sales}")
    assert len(r.result["rows"]) == 50 and r.result["total_rows"] == 400 and "note" in r.result
    assert len(r.artifact["rows"]) == 200


# ---------------------------------------------------------------- make_chart

def test_line_by_month_matches_the_data(ws, sales):
    r = make_chart(ws, sales, "line", x="date", y="amount", time_unit="month")
    assert r.status == "ok"
    df = ws.table(sales)
    monthly = df.groupby(df["date"].dt.to_period("M"))["amount"].sum()
    assert r.artifact["y"] == pytest.approx(list(monthly.round(4)))
    assert r.artifact["x"][0] == "2025-01-01" and r.result["points"] == 12


def test_bar_counts_without_y_and_ranks(ws, sales):
    r = make_chart(ws, sales, "bar", x="region")
    assert r.artifact["x"][0] == "North" and r.artifact["y_label"] == "rows"
    assert sum(r.artifact["y"]) == 400


def test_histogram_and_scatter(ws, sales):
    h = make_chart(ws, sales, "histogram", x="price")
    assert sum(h.artifact["y"]) == 400 and h.result["bins"] <= 40
    s = make_chart(ws, sales, "scatter", x="units", y="amount")
    assert s.result["points"] == 400


def test_chart_argument_errors_explain(ws, sales):
    assert make_chart(ws, sales, "line", x="region", y="amount").status == "invalid_arguments"
    assert make_chart(ws, sales, "scatter", x="units").status == "invalid_arguments"
    assert make_chart(ws, sales, "bar", x="region", y="region").status == "invalid_arguments"
    r = make_chart(ws, sales, "bar", x="Region", y="Amount")  # near-miss names are accepted
    assert r.status == "ok"


# ---------------------------------------------------------------- compare

def test_planted_difference_is_found(ws):
    rng = np.random.default_rng(0)
    df = pd.DataFrame({"group": ["a"] * 60 + ["b"] * 60,
                       "value": np.r_[rng.normal(10, 2, 60), rng.normal(12, 2, 60)]})
    d = ws.add_table(df, "planted")
    r = compare(ws, d, "difference", ["value", "group"])
    assert r.result["test"] == "Mann-Whitney U" and r.result["p_value"] < 0.01
    assert r.result["tends_higher"] == "b" and r.result["effect_label"] in ("medium", "large")
    df3 = pd.DataFrame({"g": ["a"] * 40 + ["b"] * 40 + ["c"] * 40,
                        "v": np.r_[rng.normal(0, 1, 40), rng.normal(0, 1, 40), rng.normal(1.5, 1, 40)]})
    r = compare(ws, ws.add_table(df3, "three"), "difference", ["g", "v"])
    assert r.result["test"] == "Kruskal-Wallis" and r.result["p_value"] < 0.01


def test_no_false_discoveries_on_random_data(ws):
    """On data with no pattern, each test should 'find' one at about the alpha rate (5%)."""
    rng = np.random.default_rng(42)
    hits = {"difference": 0, "correlation": 0, "association": 0}
    runs = 200
    for _ in range(runs):
        df = pd.DataFrame({"g": rng.choice(["a", "b", "c"], 90), "h": rng.choice(["x", "y"], 90),
                           "u": rng.normal(size=90), "v": rng.exponential(size=90)})
        d = ws.add_table(df, "null")
        hits["difference"] += compare(ws, d, "difference", ["u", "g"]).result["p_value"] < 0.05
        hits["correlation"] += compare(ws, d, "correlation", ["u", "v"]).result["p_value"] < 0.05
        hits["association"] += compare(ws, d, "association", ["g", "h"]).result["p_value"] < 0.05
    for kind, n in hits.items():
        assert n / runs <= 0.09, (kind, n)  # 5% expected; 9% leaves room for sampling noise


def test_correlation_and_association(ws, sales):
    r = compare(ws, sales, "correlation", ["units", "amount"])
    assert r.result["rho"] > 0.8 and r.result["range"][0] < r.result["rho"] < r.result["range"][1]
    assert r.result["confidence"] == "95%"  # said as a number, so "a 95% range" in an answer is grounded
    assert "larger than chance" in r.result["verdict"]
    r = compare(ws, sales, "association", ["region", "returned"])
    assert r.result["test"].startswith("chi-square") and r.result["p_value"] > 0.01


def test_wrong_question_for_the_columns_is_explained(ws, sales):
    r = compare(ws, sales, "correlation", ["region", "amount"])
    assert r.status == "invalid_arguments" and "association" in r.result["error"]["message"]
    r = compare(ws, sales, "difference", ["price", "amount"])
    assert r.status == "invalid_arguments" and "correlation" in r.result["error"]["message"]
    r = compare(ws, sales, "difference", ["units", "amount"])  # units has 5 values: used as groups
    assert r.status == "ok" and r.result["group_column"] == "units"


def test_partial_edge_periods_need_90_percent_of_their_days():
    from app.tools.periods import drop_partial_edges
    weeks = pd.Series(1.0, index=pd.date_range("2014-12-29", periods=5, freq="W-MON"))
    # data from Thursday 1 Jan to Thursday 29 Jan: first and last weeks cover 4/7 and 4/7 days
    days = pd.Series(pd.date_range("2015-01-01", "2015-01-29", freq="D"))
    y, notes = drop_partial_edges(weeks, days, "W")
    assert len(y) == 3 and len(notes) == 2
    months = pd.Series(1.0, index=pd.date_range("2025-01-01", periods=3, freq="MS"))
    # starting on 2 January (97% of the month) is not partial; ending on 20 March is
    y, notes = drop_partial_edges(months, pd.Series(pd.date_range("2025-01-02", "2025-03-20", freq="D")), "M")
    assert list(y.index.month) == [1, 2] and "last month" in notes[0]
    # monthly data dated the 1st: every row is a whole month, nothing is partial
    y, notes = drop_partial_edges(months, pd.Series(pd.date_range("2025-01-01", periods=3, freq="MS")), "M")
    assert len(y) == 3 and notes == []


def test_tiny_p_values_are_quoted_as_a_bound(ws, sales):
    r = compare(ws, sales, "correlation", ["units", "amount"])
    assert r.result["p_text"] == "p < 0.0001"


def test_line_chart_reads_month_labels_as_dates(ws):
    monthly = ws.add_table(pd.DataFrame({"month": [f"2015-{m:02d}" for m in range(1, 13)], "revenue": range(12)}), "m")
    r = make_chart(ws, monthly, "line", x="month", y="revenue", time_unit="month")
    assert r.status == "ok" and r.result["points"] == 12 and r.artifact["x"][0] == "2015-01-01"
    r = make_chart(ws, ws.add_table(pd.DataFrame({"k": ["a", "b"], "v": [1, 2]}), "t"), "line", x="k", y="v")
    assert r.status == "invalid_arguments" and "date column" in r.result["error"]["message"]


def test_a_daily_price_is_drawn_on_trading_days_with_no_zeros(ws):
    """Measured 2026-10-17: a share's closes were drawn down to 0 every weekend and on a holiday ('sum of close')."""
    days = [d for d in pd.bdate_range("2026-09-16", "2026-10-16") if d != pd.Timestamp("2026-10-02")]
    prices = ws.add_table(pd.DataFrame({"Date": days, "Close": [125010.0 - 130 * i for i in range(len(days))]}), "p")
    r = make_chart(ws, prices, "line", x="Date", y="Close")
    assert r.status == "ok" and r.result["points"] == 23  # the weekdays from first to last, weekends left out
    assert r.result["lowest"][1] > 120000 and r.artifact["y_label"] == "mean of Close"
    assert r.artifact["y"][r.artifact["x"].index("2026-10-02")] is None  # the holiday: a gap, not a 0
    # sales on the same days still add up, and a day without any is a 0
    sales = ws.add_table(pd.DataFrame({"day": days * 2, "amount": [5.0] * (2 * len(days))}), "s")
    s = make_chart(ws, sales, "line", x="day", y="amount")
    assert s.artifact["y"][s.artifact["x"].index("2026-10-02")] == 0 and s.result["highest"][1] == 10


def test_a_query_that_matches_nothing_says_what_to_check(ws, sales):
    for sql in [f"SELECT * FROM {sales} WHERE region = 'north'", f"SELECT sum(amount) FROM {sales} WHERE region = 'north'"]:
        r = query_data(ws, sql)
        assert r.status == "ok" and "SELECT DISTINCT" in r.result["note"], sql
    assert "note" not in query_data(ws, f"SELECT count(*) FROM {sales}").result


def test_auto_period_is_never_finer_than_the_data(ws, sales):
    monthly = ws.add_table(pd.DataFrame({"month": [f"2015-{m:02d}" for m in range(1, 13)],
                                         "revenue": np.arange(12) * 10.0 + 100}), name="monthly")
    chart = make_chart(ws, monthly, "line", x="month", y="revenue").artifact
    assert len(chart["x"]) == 12 and chart["x"][0] == "2015-01-01"  # months, not 49 mostly empty weeks
    daily = make_chart(ws, sales, "line", x="date", y="amount").artifact  # a year of daily rows: by week
    assert 50 <= len(daily["x"]) <= 53


def test_a_line_chart_with_only_empty_periods_says_so(ws):
    empty = ws.add_table(pd.DataFrame({"day": pd.date_range("2025-01-01", periods=60), "v": [float("nan")] * 60}), name="e")
    r = make_chart(ws, empty, "line", x="day", y="v", agg="mean")
    assert r.status == "failed" and "every period is empty" in r.result["error"]["message"]


def test_a_table_is_inspected_once(ws, sales, monkeypatch):
    from app.tools import inspect_data as module
    first = module.inspect_data(ws, sales)
    monkeypatch.setattr(module, "_column", lambda *a: (_ for _ in ()).throw(AssertionError("computed again")))
    again = module.inspect_data(ws, sales)
    assert again.status == "ok" and again.result == first.result and again.artifact == first.artifact



def test_ab_rates_match_an_independent_implementation(ws):
    """Newcombe's range and the z-test p, checked against statsmodels on the same counts."""
    from statsmodels.stats.proportion import confint_proportions_2indep, proportions_ztest
    rng = np.random.default_rng(1)
    variant = rng.choice(["control", "new page"], 4000)
    converted = rng.random(4000) < np.where(variant == "new page", 0.12, 0.09)
    d = ws.add_table(pd.DataFrame({"variant": variant, "converted": np.where(converted, "yes", "no")}), "ab")
    r = compare(ws, d, "ab", ["variant", "converted"]).result
    assert r["control"] == "control" and r["outcome_counted"] == "yes"
    (a, b) = r["groups"]
    lo, hi = confint_proportions_2indep(b["successes"], b["rows"], a["successes"], a["rows"], method="newcomb")
    assert r["difference_range"] == [round(100 * lo, 2), round(100 * hi, 2)]
    p = proportions_ztest([b["successes"], a["successes"]], [b["rows"], a["rows"]])[1]
    assert abs(r["p_value"] - p) / p < 0.01 and "more than chance" in r["verdict"]


def test_ab_on_a_number_and_its_mistakes(ws, sales):
    rng = np.random.default_rng(2)
    d = ws.add_table(pd.DataFrame({"arm": rng.choice(["A", "B"], 300), "spend": rng.gamma(2, 20, 300)}), "spend")
    r = compare(ws, d, "ab", ["arm", "spend"]).result
    assert r["test"] == "Welch's t-test" and r["control"] == "A" and "chance" in r["verdict"]
    assert compare(ws, sales, "ab", ["region", "amount"]).result["error"]["message"].startswith("An A/B test compares 2")


# ---------------------------------------------------------------- chart kinds

def test_line_and_bar_split_by_a_group_match_the_data(ws, sales):
    df = ws.table(sales)
    r = make_chart(ws, sales, "line", "date", "amount", by="region", time_unit="month")
    months = df.assign(m=df.date.dt.to_period("M")).groupby(["m", "region"]).amount.sum().unstack()
    for s in r.artifact["series"]:
        assert s["y"] == [round(v, 4) for v in months[s["name"]]]
    assert [s["group"] for s in r.result["series"]] == ["North", "South", "East"]  # largest first
    r = make_chart(ws, sales, "bar", "region", by="returned", stacked=True)
    counts = pd.crosstab(df.region, df.returned.astype(str))
    assert r.artifact["stacked"] and r.artifact["x"][0] == "North"
    for s in r.artifact["series"]:
        assert s["y"] == [counts.loc[k, s["name"]] for k in r.artifact["x"]]


def test_scatter_box_heatmap_and_pie(ws, sales):
    r = make_chart(ws, sales, "scatter", "units", "amount", by="region")
    assert r.result["trend"]["slope"] == pytest.approx(100, rel=0.1) and r.artifact["groups"][0] == "North"
    assert len(r.artifact["group"]) == len(r.artifact["x"])
    r = make_chart(ws, sales, "box", "region", "amount")
    df = ws.table(sales)
    assert r.artifact["boxes"][0]["median"] == round(df[df.region == "North"].amount.median(), 4)
    assert make_chart(ws, sales, "box", "amount").result["groups"][0]["group"] == "all rows"
    d = ws.add_table(pd.DataFrame({"day": ["Tue", "Mon", "Mon", "Sun"], "hour": [9, 9, 10, 9], "v": [1, 2, 3, 4]}), "t")
    r = make_chart(ws, d, "heatmap", "day", "v", by="hour")
    assert r.artifact["x"] == ["Mon", "Tue", "Sun"] and r.artifact["rows"] == ["9", "10"]
    assert r.artifact["values"] == [[2.0, 1.0, 4.0], [3.0, 0.0, 0.0]] and r.result["highest"][1] == 4
    r = make_chart(ws, sales, "pie", "region", "amount")
    assert sum(sl[2] for sl in r.result["slices"]) == pytest.approx(1, abs=0.01)


def test_chart_kinds_say_how_to_fix_a_wrong_call(ws, sales):
    split = make_chart(ws, sales, "histogram", "amount", by="region")  # one spread per group, the same bins
    assert split.status == "ok" and len(split.artifact["series"]) >= 2 and "y" not in split.artifact
    assert "groups of a box chart go in x" in make_chart(ws, sales, "box", "amount", by="region").result["error"]["message"]
    assert "sum or count" in make_chart(ws, sales, "pie", "region", "amount", agg="mean").result["error"]["message"]
    assert "needs x and by" in make_chart(ws, sales, "heatmap", "region").result["error"]["message"]
    same = make_chart(ws, sales, "bar", "region", "amount", by="region", agg="mean")  # by = x: no split, no error
    assert same.status == "ok" and "series" not in same.artifact and same.artifact["y_label"] == "mean of amount"


def test_a_difference_in_a_yes_no_outcome_between_two_groups_is_an_ab_test(ws):
    rng = np.random.default_rng(3)
    variant = rng.choice(["A", "B"], 3000)
    d = ws.add_table(pd.DataFrame({"variant": variant, "signed_up": np.where(rng.random(3000) < 0.1, "yes", "no")}),
                     "ab")
    r = compare(ws, d, "difference", ["variant", "signed_up"]).result
    assert r["question"] == "ab" and r["outcome_counted"] == "yes" and "two rates" in r["note"]
