"""Direct tests of train_model, predict and forecast - no model involved."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.tools.forecast import forecast
from app.tools.ml import predict, train_model
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


# ---------------------------------------------------------------- train_model / predict

def test_classification_finds_the_planted_signal(ws):
    data = ws.add_table(customers(), "customers")
    r = train_model(ws, data, "churned")
    assert r.status == "ok", r.result
    res = r.result
    assert res["task"] == "classification" and res["metric"] == "AUC" and res["positive_class"] in ("yes", "no")
    assert res["better_than_baseline"] and res["score"] > 0.7
    assert res["important_predictive_features"][0]["column"] == "tickets"
    excluded = {e["column"]: e["reason"] for e in res["excluded_columns"]}
    assert excluded == {"customer_id": "unique per row (an id or free text)",
                        "signup_note": "unique per row (an id or free text)"}
    assert res["model_type"] in res["scores_by_model"]
    assert "5-fold" in res["tested_on"] and ws.kind(res["model"]) == "model"


def test_regression_and_baseline(ws):
    rng = np.random.default_rng(3)
    x = rng.uniform(0, 10, 300)
    df = pd.DataFrame({"x": x, "noise": rng.normal(size=300), "y": 3 * x + rng.normal(0, 1, 300)})
    r = train_model(ws, ws.add_table(df, "lin"), "y")
    assert r.result["task"] == "regression" and r.result["score"] > 0.9
    assert r.result["average_error"] < r.result["baseline_average_error"]


def test_noise_target_is_not_better_than_baseline(ws):
    rng = np.random.default_rng(5)
    df = pd.DataFrame({"a": rng.normal(size=400), "b": rng.choice(["x", "y"], 400),
                       "target": rng.choice(["yes", "no"], 400)})
    r = train_model(ws, ws.add_table(df, "noise"), "target")
    assert r.status == "ok" and not r.result["better_than_baseline"]
    assert r.result["verdict"].startswith("Not clearly better")
    assert r.result["important_predictive_features"] == []


def test_time_split_and_leak_warning(ws):
    df = customers(500, seed=1)
    df["signup"] = pd.Timestamp("2024-01-01") + pd.to_timedelta(np.arange(500), unit="D")
    df["refund_issued"] = (df["churned"] == "yes").astype(float) * 10 + np.random.default_rng(0).normal(0, 0.1, 500)
    r = train_model(ws, ws.add_table(df, "with dates"), "churned")
    assert "latest 20% of the time span" in r.result["tested_on"]
    assert "refund_issued" in r.result["warning"]
    assert r.result["excluded_columns"][0]["column"] == "signup"


def test_multiclass(ws):
    df = customers(600, seed=2)
    r = train_model(ws, ws.add_table(df, "plans"), "plan", features=["tickets", "spend", "churned"])
    assert r.result["metric"] == "accuracy" and "baseline" in r.result


def test_predict_on_new_rows(ws):
    model = train_model(ws, ws.add_table(customers(), "train"), "churned").result["model"]
    new = customers(50, seed=9).drop(columns=["churned"])
    r = predict(ws, model, ws.add_table(new, "new customers"))
    assert r.status == "ok"
    out = ws.table(r.result["data"])
    prob = [c for c in out.columns if c.startswith("probability_")]
    assert len(out) == 50 and "predicted_churned" in out and len(prob) == 1 and out[prob[0]].between(0, 1).all()
    # a table without one of the inputs still predicts, with a warning
    r = predict(ws, model, ws.add_table(new.drop(columns=["plan"]), "no plan"))
    assert r.status == "ok" and "plan" in r.result["warning"]
    r = predict(ws, model, ws.add_table(pd.DataFrame({"zzz": [1, 2]}), "unrelated"))
    assert r.status == "failed"


def test_bad_targets_are_explained(ws):
    data = ws.add_table(customers(), "customers")
    assert train_model(ws, data, "nope").status == "invalid_arguments"
    r = train_model(ws, data, "signup_note")
    assert r.status == "invalid_arguments" and "at most 10" in r.result["error"]["message"]
    r = train_model(ws, ws.add_table(customers(30), "small"), "churned")
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


def test_forecast_says_when_unreliable(ws):
    rng = np.random.default_rng(1)
    days = pd.date_range("2024-01-01", periods=200, freq="D")
    collapse = np.where(np.arange(200) < 150, 100.0, 5.0) * rng.uniform(0.2, 1.8, 200)
    r = forecast(ws, ws.add_table(pd.DataFrame({"d": days, "v": collapse}), "collapse"), "d", "v", time_unit="day")
    assert r.result["reliability"] in ("none", "weak")
    assert "do not plan" in r.result["backtest"] or "rough guess" in r.result["backtest"]


def test_forecast_counts_rows_and_checks_inputs(ws):
    data = ws.add_table(monthly_sales(24), "sales")
    r = forecast(ws, data, "day", time_unit="week")
    assert r.result["value"] == "rows" and r.result["combined_by"] == "count"
    assert forecast(ws, data, "amount", "amount").status == "invalid_arguments"
    short = ws.add_table(monthly_sales(1), "short")
    r = forecast(ws, short, "day", "amount", time_unit="month")
    assert r.status == "failed" and "at least 10" in r.result["error"]["message"]


def test_flag_columns_made_from_the_target_are_never_inputs(ws):
    from app.tools.clean_data import clean_data
    data = ws.add_table(customers(), "customers")
    flagged = clean_data(ws, data, [{"op": "flag_outliers", "columns": ["spend"]}]).result["data"]
    r = train_model(ws, flagged, "spend")
    assert {"column": "spend_outlier", "reason": "made from the target (a leak)"} in r.result["excluded_columns"]


def test_time_split_into_year_and_quarter_is_combined(ws):
    from app.tools.inspect_data import inspect_data
    from app.tools.query_data import query_data
    quarters = pd.DataFrame({"year": np.repeat(np.arange(2000, 2015), 4), "quarter": np.tile([1, 2, 3, 4], 15),
                             "gdp": np.linspace(100, 160, 60).round(1)})
    data = ws.add_table(quarters, name="macro.csv")
    tip = inspect_data(ws, data).result["note"]
    assert "split into 'year' and 'quarter'" in tip and "forecast combines them itself" in tip
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
    r = train_model(ws, made, "had_affair", features=["age", "rating", "affairs"]).result  # asked for the leak
    assert {"column": "affairs", "reason": "had_affair was computed from it (a leak)"} in r["excluded_columns"]
    assert r["score"] < 0.9  # no longer 'predicts' the answer from the answer

