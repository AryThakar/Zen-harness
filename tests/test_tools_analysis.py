"""Direct tests of query_data, make_chart and stats_test - no model involved."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.tools.make_chart import make_chart
from app.tools.query_data import query_data
from app.tools.stats_test import stats_test
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


# ---------------------------------------------------------------- stats_test

def test_planted_difference_is_found(ws):
    rng = np.random.default_rng(0)
    df = pd.DataFrame({"group": ["a"] * 60 + ["b"] * 60,
                       "value": np.r_[rng.normal(10, 2, 60), rng.normal(12, 2, 60)]})
    d = ws.add_table(df, "planted")
    r = stats_test(ws, d, "difference", ["value", "group"])
    assert r.result["test"] == "Mann-Whitney U" and r.result["p_value"] < 0.01
    assert r.result["tends_higher"] == "b" and r.result["effect_label"] in ("medium", "large")
    df3 = pd.DataFrame({"g": ["a"] * 40 + ["b"] * 40 + ["c"] * 40,
                        "v": np.r_[rng.normal(0, 1, 40), rng.normal(0, 1, 40), rng.normal(1.5, 1, 40)]})
    r = stats_test(ws, ws.add_table(df3, "three"), "difference", ["g", "v"])
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
        hits["difference"] += stats_test(ws, d, "difference", ["u", "g"]).result["p_value"] < 0.05
        hits["correlation"] += stats_test(ws, d, "correlation", ["u", "v"]).result["p_value"] < 0.05
        hits["association"] += stats_test(ws, d, "association", ["g", "h"]).result["p_value"] < 0.05
    for kind, n in hits.items():
        assert n / runs <= 0.09, (kind, n)  # 5% expected; 9% leaves room for sampling noise


def test_correlation_and_association(ws, sales):
    r = stats_test(ws, sales, "correlation", ["units", "amount"])
    assert r.result["rho"] > 0.8 and r.result["range_95"][0] < r.result["rho"] < r.result["range_95"][1]
    assert "larger than chance" in r.result["verdict"]
    r = stats_test(ws, sales, "association", ["region", "returned"])
    assert r.result["test"].startswith("chi-square") and r.result["p_value"] > 0.01


def test_wrong_question_for_the_columns_is_explained(ws, sales):
    r = stats_test(ws, sales, "correlation", ["region", "amount"])
    assert r.status == "invalid_arguments" and "association" in r.result["error"]["message"]
    r = stats_test(ws, sales, "difference", ["price", "amount"])
    assert r.status == "invalid_arguments" and "correlation" in r.result["error"]["message"]
    r = stats_test(ws, sales, "difference", ["units", "amount"])  # units has 5 values: used as groups
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
    r = stats_test(ws, sales, "correlation", ["units", "amount"])
    assert r.result["p_text"] == "p < 0.0001"


def test_line_chart_reads_month_labels_as_dates(ws):
    monthly = ws.add_table(pd.DataFrame({"month": [f"2015-{m:02d}" for m in range(1, 13)], "revenue": range(12)}), "m")
    r = make_chart(ws, monthly, "line", x="month", y="revenue", time_unit="month")
    assert r.status == "ok" and r.result["points"] == 12 and r.artifact["x"][0] == "2015-01-01"
    r = make_chart(ws, ws.add_table(pd.DataFrame({"k": ["a", "b"], "v": [1, 2]}), "t"), "line", x="k", y="v")
    assert r.status == "invalid_arguments" and "date column" in r.result["error"]["message"]
