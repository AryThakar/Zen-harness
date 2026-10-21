# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Direct tests of predict and forecast - no model involved."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.tools.forecast import forecast
from app.tools.ml import predict
from app.tools.workspace import Workspace


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path / "ws")


def customers(n=600, seed=0):
    """Churn depends on support tickets and plan; customer_id and signup_note are noise."""
    rng = np.random.default_rng(seed)
    tickets = rng.poisson(2, n)
    plan = rng.choice(["basic", "pro", "team"], n)
    spend = rng.gamma(2, 50, n).round(2)
    logit = -2 + 0.6 * tickets + np.where(plan == "basic", 0.8, -0.4)
    churned = rng.random(n) < 1 / (1 + np.exp(-logit))
    return pd.DataFrame({"customer_id": np.arange(10_000, 10_000 + n), "plan": plan, "tickets": tickets,
                         "spend": spend, "signup_note": [f"note {i}" for i in range(n)],
                         "churned": np.where(churned, "yes", "no")})


# ---------------------------------------------------------------- predict

def test_classification_finds_the_planted_signal_and_its_direction(ws):
    data = ws.add_table(customers(), "customers")
    r = predict(ws, data, "churned")
    assert r.status == "ok", r.result
    res = r.result
    assert res["task"] == "classification" and res["metric"] == "AUC" and res["positive_class"] == "yes"
    assert res["better_than_baseline"] and res["score"] > 0.7
    top = res["drivers"][0]
    assert top["column"] == "tickets" and top["direction"] == "higher values go with higher chance of churned = yes"
    plan = next(d for d in res["drivers"] if d["column"] == "plan")
    assert plan["direction"].startswith("highest chance of churned = yes for basic")
    excluded = {e["column"]: e["reason"] for e in res["excluded_columns"]}
    assert excluded == {"customer_id": "unique per row (an id or free text)",
                        "signup_note": "unique per row (an id or free text)"}
    assert res["model_type"] in res["scores_by_model"] and "5-fold" in res["tested_on"]
    # the drivers as a chart file (measured: without one the analyst typed them into a script to chart them)
    assert r.artifact["type"] == "chart" and r.artifact["x"][0] == "tickets" and r.artifact["id"] == res["chart_file"]
    assert ws.label(res["chart_file"]) == "charts/what-predicts-churned.chart.json"
    assert r.artifact["y"] == [d["share"] for d in res["drivers"]]


def test_predictions_are_saved_out_of_sample(ws):
    """Every row with a known outcome was scored by a model that didn't see it (5-fold): their AUC in the
    saved table equals the reported score; rows without the outcome get the final model's prediction."""
    from sklearn.metrics import roc_auc_score
    df = customers(700, seed=4)
    df.loc[650:, "churned"] = None
    data = ws.add_table(df, "customers")
    res = predict(ws, data, "churned").result
    saved = ws.table(res["predictions"]["saved_as"])
    known = saved.iloc[:650]
    assert roc_auc_score(known.churned == "yes", known.probability_yes) == pytest.approx(res["score"], abs=1e-3)
    assert res["predictions"]["rows_without_target"] == 50 and saved.probability_yes.iloc[650:].notna().all()
    assert set(saved.predicted_churned) <= {"yes", "no"} and ws.entry(res["predictions"]["saved_as"])["parent"] == data


def test_regression_and_baseline(ws):
    rng = np.random.default_rng(3)
    x = rng.uniform(0, 10, 300)
    df = pd.DataFrame({"x": x, "noise": rng.normal(size=300), "y": 3 * x + rng.normal(0, 1, 300)})
    r = predict(ws, ws.add_table(df, "lin"), "y")
    assert r.result["task"] == "regression" and r.result["score"] > 0.9
    assert r.result["average_error"] < r.result["baseline_average_error"]
    assert r.result["drivers"][0] == {"column": "x", "share": r.result["drivers"][0]["share"],
                                      "direction": "higher values go with higher predicted y"}


def test_noise_target_is_not_better_than_baseline(ws):
    rng = np.random.default_rng(5)
    df = pd.DataFrame({"a": rng.normal(size=400), "b": rng.choice(["x", "y"], 400),
                       "target": rng.choice(["yes", "no"], 400)})
    r = predict(ws, ws.add_table(df, "noise"), "target")
    assert r.status == "ok" and not r.result["better_than_baseline"]
    assert r.result["verdict"].startswith("Not clearly better") and r.result["drivers"] == []
    assert any("no more useful than the average" in n for n in r.result["notes"])
    assert r.artifact["type"] == "table" and "chart_file" not in r.result  # nothing worth drawing


def test_time_split_and_leak_warning(ws):
    df = customers(500, seed=1)
    df["signup"] = pd.Timestamp("2024-01-01") + pd.to_timedelta(np.arange(500), unit="D")
    df["refund_issued"] = (df["churned"] == "yes").astype(float) * 10 + np.random.default_rng(0).normal(0, 0.1, 500)
    r = predict(ws, ws.add_table(df, "with dates"), "churned")
    assert "latest 20% of the time span" in r.result["tested_on"]
    assert "refund_issued" in r.result["warning"]
    assert r.result["excluded_columns"][0]["column"] == "signup"


def test_a_yes_no_copy_of_the_outcome_is_named_as_a_leak(ws):
    df = customers(400, seed=6)
    df["still_active"] = df["churned"].map({"yes": "no", "no": "yes"})  # read as yes/no -> True/False
    r = predict(ws, ws.add_table(df, "copy"), "churned").result
    assert "still_active predict churned almost perfectly" in r["warning"]


def test_directions_hold_the_other_columns_fixed(ws):
    """Big items tend to have the poor grade, but with size held fixed a better grade sells higher: the
    driver's direction follows the model, not the raw average."""
    rng = np.random.default_rng(7)
    size = rng.uniform(0.5, 1.5, 2000)
    grade = np.where(rng.random(2000) < size - 0.5, "poor", "good")  # bigger -> more often poor
    price = 1000 * size ** 2 * np.where(grade == "good", 1.5, 1.0) * rng.lognormal(0, 0.05, 2000)
    r = predict(ws, ws.add_table(pd.DataFrame({"size": size, "grade": grade, "price": price}), "gems"), "price").result
    raw = pd.DataFrame({"grade": grade, "price": price}).groupby("grade").price.mean()
    assert raw["poor"] > raw["good"]  # the raw average says poor sells higher
    grade_driver = next(d for d in r["drivers"] if d["column"] == "grade")
    assert grade_driver["direction"] == "highest predicted price for good, lowest for poor"


def test_multiclass(ws):
    df = customers(600, seed=2)
    r = predict(ws, ws.add_table(df, "plans"), "plan", features=["tickets", "spend", "churned"])
    assert r.result["metric"] == "accuracy" and "baseline" in r.result


def test_predict_for_a_table_of_new_rows(ws):
    data = ws.add_table(customers(), "train")
    new = customers(50, seed=9).drop(columns=["churned"])
    r = predict(ws, data, "churned", new_data=ws.add_table(new, "new customers"))
    assert r.status == "ok"
    out = ws.table(r.result["new_data"]["saved_as"])
    assert len(out) == 50 and "predicted_churned" in out and out["probability_yes"].between(0, 1).all()
    # a table without one of the inputs still predicts, with a warning
    r = predict(ws, data, "churned", new_data=ws.add_table(new.drop(columns=["plan"]), "no plan"))
    assert r.status == "ok" and "plan" in r.result["new_data"]["warning"]
    r = predict(ws, data, "churned", new_data=ws.add_table(pd.DataFrame({"zzz": [1, 2]}), "unrelated"))
    assert r.status == "failed"


def test_bad_targets_are_explained(ws):
    data = ws.add_table(customers(), "customers")
    assert predict(ws, data, "nope").status == "invalid_arguments"
    r = predict(ws, data, "signup_note")
    assert r.status == "invalid_arguments" and "at most 10" in r.result["error"]["message"]
    r = predict(ws, ws.add_table(customers(30), "small"), "churned")
    assert r.status == "failed" and "at least 50" in r.result["error"]["message"]


# ---------------------------------------------------------------- forecast

def monthly_sales(months=48, seed=0, growth=0.01):
    """Daily orders whose monthly totals have a trend and a December peak; data ends mid-month."""
    rng = np.random.default_rng(seed)
    days = pd.date_range("2021-01-01", periods=months * 30 + 15, freq="D")
    season = 1 + 0.5 * (days.month == 12) + 0.2 * np.sin(2 * np.pi * days.month / 12)
    level = 100 * (1 + growth) ** (np.arange(len(days)) / 30)
    return pd.DataFrame({"day": days, "amount": rng.poisson(level * season).astype(float)})


def test_forecast_seasonal_series(ws):
    data = ws.add_table(monthly_sales(), "sales")
    r = forecast(ws, data, "day", "amount", time_unit="month", horizon=6)
    assert r.status == "ok", r.result
    res = r.result
    assert res["period"] == "month" and res["horizon"] == 6 and len(res["forecast"]) == 6
    assert res["beats_baseline"] and res["reliability"] in ("strong", "moderate")
    assert any("incomplete" in n for n in res["notes"])  # the half month at the end
    p = res["forecast"][0]
    assert p["low_80"] <= p["forecast"] <= p["high_80"]
    dec = [x for x in res["forecast"] if x["period"].endswith("-12-01")]
    assert dec and dec[0]["forecast"] > np.median([x["forecast"] for x in res["forecast"]])


def test_forecast_reports_the_trend_and_the_season(ws):
    rising = ws.add_table(monthly_sales(), "rising")
    res = forecast(ws, rising, value="amount", time_unit="month").result  # the date column found by its role
    assert res["trend"]["direction"] == "rising" and res["trend"]["per_period"] > 0
    assert res["seasonality"]["strength"] >= 0.6 and res["season_high"] == "December"
    assert res["change_vs_year_earlier_pct"] > 0
    flat = ws.add_table(monthly_sales(growth=0.0, seed=3), "flat")
    trend = forecast(ws, flat, value="amount", time_unit="month").result["trend"]
    assert trend["direction"] == "no clear trend" and "per_period" not in trend  # no slope of noise to quote
    short = forecast(ws, ws.add_table(monthly_sales(18), "short"), value="amount", time_unit="month").result
    assert "can't be told apart from a trend" in short["trend"]["note"]


def test_trend_test_false_alarms_and_power():
    """Monthly series of 48 points: few false alarms on noise, most planted trends found."""
    from app.tools.forecast import trend_test
    rng = np.random.default_rng(0)
    idx = pd.date_range("2020-01-01", periods=48, freq="MS")
    noise = sum(trend_test(pd.Series(100 + rng.normal(0, 10, 48), idx), "M")["p"] < 0.05 for _ in range(200))
    found = sum(trend_test(pd.Series(100 + 0.5 * np.arange(48) + rng.normal(0, 10, 48), idx), "M")["p"] < 0.05
                for _ in range(200))
    assert noise / 200 <= 0.08 and found / 200 >= 0.85


def test_forecast_by_group_and_month_names(ws):
    a, b = monthly_sales(seed=1), monthly_sales(seed=2, growth=-0.01)
    both = pd.concat([a.assign(store="A"), b.assign(store="B", amount=b.amount / 2)])
    r = forecast(ws, ws.add_table(both, "stores"), value="amount", by="store", time_unit="month")
    groups = {g["group"]: g for g in r.result["groups"]}
    assert set(groups) == {"A", "B"} and groups["A"]["trend"] == "rising" and groups["B"]["trend"] == "falling"
    assert [s["name"] for s in r.artifact["series"]] == ["A", "B"]
    named = pd.DataFrame({"year": np.repeat(np.arange(2010, 2016), 12), "month": ["January", "February", "March",
                          "April", "May", "June", "July", "August", "September", "October", "November", "December"] * 6,
                          "passengers": np.arange(72) + 100})
    r = forecast(ws, ws.add_table(named, "named"), value="passengers")
    assert r.status == "ok" and r.result["history_points"] == 72 and "made from the 'year' and 'month'" in r.result["notes"][0]


def test_forecast_says_when_unreliable(ws):
    rng = np.random.default_rng(1)
    days = pd.date_range("2024-01-01", periods=200, freq="D")
    collapse = np.where(np.arange(200) < 150, 100.0, 5.0) * rng.uniform(0.2, 1.8, 200)
    r = forecast(ws, ws.add_table(pd.DataFrame({"d": days, "v": collapse}), "collapse"), "d", "v", time_unit="day")
    assert r.result["reliability"] in ("none", "weak")
    assert "do not plan" in r.result["backtest"] or "rough guess" in r.result["backtest"]


def test_forecast_counts_rows_and_checks_inputs(ws):
    data = ws.add_table(monthly_sales(24), "sales")
    # one row every day: a count of rows is a flat line - refused, naming the columns to forecast (measured 2026-10-22:
    # a cafe's four rows a day forecast as "rows" in every demo run)
    r = forecast(ws, data, "day", time_unit="week")
    assert r.status == "invalid_arguments" and "same number of rows" in r.result["error"]["message"]
    days = monthly_sales(24)
    events = ws.add_table(pd.concat([days, days.iloc[::3]], ignore_index=True), "events")  # 1 or 2 rows a day
    r = forecast(ws, events, "day", time_unit="week")
    assert r.result["value"] == "rows" and r.result["combined_by"] == "count"
    assert forecast(ws, data, "amount", "amount").status == "invalid_arguments"
    short = ws.add_table(monthly_sales(1), "short")
    r = forecast(ws, short, "day", "amount", time_unit="month")
    assert r.status == "failed" and "at least 10" in r.result["error"]["message"]


def test_a_share_price_on_trading_days_is_averaged_and_forecast_for_the_coming_weekdays(ws):
    """Measured 2026-10-17: a month of MRF closes was '26% gaps' to forecast (weekends), and a year of them forecast
    by week with the default sum came out as five closes added (~600,000 a week)."""
    days = [d for d in pd.bdate_range("2026-09-16", "2026-10-16") if d != pd.Timestamp("2026-10-02")]  # a holiday
    month = pd.DataFrame({"Date": days, "Close": [125010.0 - 130 * i + (i % 3) * 200 for i in range(len(days))]})
    r = forecast(ws, ws.add_table(month, "mrf"), value="Close")
    assert r.status == "ok", r.result
    res = r.result
    assert res["period"] == "weekday" and res["combined_by"] == "mean" and res["trend"]["direction"] == "falling"
    assert [p["period"] for p in res["forecast"]] == ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"]
    assert any("weekdays only" in n for n in res["notes"])
    year = pd.DataFrame({"Date": pd.bdate_range("2025-10-17", "2026-10-16")})
    year["Close"] = 120000.0 + np.arange(len(year)) * 10
    weekly = forecast(ws, ws.add_table(year, "mrf_year"), value="Close").result
    assert weekly["period"] == "week" and weekly["combined_by"] == "mean"
    assert 115000 < weekly["forecast"][0]["forecast"] < 130000  # a price, not five of them added


def test_flag_columns_made_from_the_target_are_never_inputs(ws):
    from app.tools.clean_data import clean_data
    data = ws.add_table(customers(), "customers")
    flagged = clean_data(ws, data, [{"op": "flag_outliers", "columns": ["spend"]}]).result["data"]
    r = predict(ws, flagged, "spend")
    assert {"column": "spend_outlier", "reason": "made from the target (a leak)"} in r.result["excluded_columns"]


def test_time_split_into_year_and_quarter_is_combined(ws):
    from app.tools.inspect_data import inspect_data
    from app.tools.query_data import query_data
    quarters = pd.DataFrame({"year": np.repeat(np.arange(2000, 2015), 4), "quarter": np.tile([1, 2, 3, 4], 15),
                             "gdp": np.linspace(100, 160, 60).round(1)})
    data = ws.add_table(quarters, name="macro.csv")
    tip = inspect_data(ws, data).result["note"]
    assert "split into 'year' and 'quarter'" in tip and "forecast and find_patterns combine them" in tip
    for date in ("year", "quarter", "2009-12-31"):  # whatever the model gives: the split columns are used
        r = forecast(ws, data, date=date, value="gdp", horizon=4, agg="last")
        assert r.status == "ok" and r.result["period"] == "quarter" and r.result["history_points"] == 60, date
        assert "made from the 'year' and 'quarter' columns" in r.result["notes"][0]
    made = query_data(ws, tip.split("save=true: ")[1], save=True).result["saved_as"]  # the chart route works too
    assert str(ws.table(made)["date"].iloc[5])[:10] == "2001-04-01"


def test_a_date_given_where_the_date_column_goes_is_explained(ws):
    data = ws.add_table(pd.DataFrame({"day": pd.date_range("2020-01-01", periods=20, freq="QS"), "gdp": range(20)}),
                        name="g.csv")
    message = forecast(ws, data, date="2026-10-02", value="gdp").result["error"]["message"]
    assert message == "date takes a column's name, not a date like '2026-10-02'. Date columns: day."


def test_a_target_computed_from_a_column_never_learns_from_it(ws):
    from app.tools.query_data import query_data
    rng = np.random.default_rng(0)
    people = pd.DataFrame({"age": rng.integers(20, 60, 300), "rating": rng.integers(1, 6, 300),
                           "affairs": rng.poisson(0.4, 300).astype(float)})
    data = ws.add_table(people, name="survey.csv")
    made = query_data(ws, f"SELECT *, CASE WHEN affairs > 0 THEN 'yes' ELSE 'no' END AS had_affair FROM {data}",
                      save=True).result["saved_as"]
    r = predict(ws, made, "had_affair", features=["age", "rating", "affairs"]).result  # asked for the leak
    assert {"column": "affairs", "reason": "had_affair was computed from it (a leak)"} in r["excluded_columns"]
    assert r["score"] < 0.9  # no longer 'predicts' the answer from the answer

