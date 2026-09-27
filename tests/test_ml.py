"""ML tasks on synthetic data whose patterns are known in advance."""

from datetime import date

import pandas as pd
import pytest

from app.datagen import generate, planted_events
from app.db.connection import Database
from app.ingestion.pipeline import ingest_file
from app.ml import anomalies, basket, churn, forecast, results, runner, segments
from app.ml.base import NotEnoughData

from .conftest import TODAY

END = date(2026, 9, 26)
MONTHS = 12


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    path = tmp_path_factory.mktemp("ml") / "sales.csv"
    generate(months=MONTHS, end=END, orders_per_day=30, seed=3).to_csv(path, index=False)
    database = Database()
    ingest_file(database, path, today=TODAY)
    yield database
    database.close()


@pytest.fixture
def cur(db):
    cursor = db.cursor()
    yield cursor
    cursor.close()


def test_forecast_beats_the_baseline_and_has_a_range(cur):
    result = forecast.run(cur, horizon=30)
    m = result.metrics
    assert m["model"] in ("AutoETS", "AutoTheta")
    assert m["error_pct"] < m["baseline_error_pct"] and m["improvement_pct"] > 0
    points = result.tables["forecast_points"]
    assert len(points) == 30
    assert points["bucket"].iloc[0] == date(2026, 9, 27)  # the day after the last sale
    assert (points["lower"] <= points["predicted"]).all() and (points["predicted"] <= points["upper"]).all()
    assert m["expected_total"] == pytest.approx(points["predicted"].sum())


def test_weekly_and_filtered_forecasts(cur):
    weekly = forecast.run(cur, horizon=8, grain="week")
    assert len(weekly.tables["forecast_points"]) == 8
    assert pd.Timestamp(weekly.tables["forecast_points"]["bucket"].iloc[0]).weekday() == 0  # Mondays
    electronics = forecast.run(cur, horizon=14, by="category", value="Electronics")
    assert "category 'Electronics'" in electronics.message


def test_forecast_says_when_history_is_too_short(cur):
    with pytest.raises(NotEnoughData, match="needs at least 540 days"):
        forecast.run(cur, horizon=180)
    with pytest.raises(NotEnoughData, match="there are 0"):
        forecast.run(cur, horizon=7, by="product", value="No such product")


def test_full_weeks_drops_partial_weeks():
    daily = pd.Series(1.0, index=pd.date_range("2025-03-05", "2025-03-26"))  # Wed .. Wed
    weekly = forecast.full_weeks(daily)
    assert list(weekly.index) == [pd.Timestamp("2025-03-10"), pd.Timestamp("2025-03-17")]
    assert list(weekly) == [7.0, 7.0]


def test_segments_are_named_and_ordered_by_value(cur):
    result = segments.run(cur)
    rows = result.tables["customer_segments"]
    assert set(rows["segment"]) <= set(segments.SEGMENTS)
    assert 4 <= result.metrics["clusters"] <= 6
    typical = rows.groupby("segment")[["recency_days", "orders", "revenue"]].median()
    assert typical.loc["Champions", "revenue"] == typical["revenue"].max()
    assert typical.loc["Champions", "orders"] == typical["orders"].max()
    assert typical.loc["Lost", "recency_days"] > typical.loc["Champions", "recency_days"]
    # Champions are a minority that brings a large share of revenue
    share = result.metrics["segments"]["Champions"] / result.metrics["customers"]
    assert share < 0.3 and result.metrics["revenue_share_pct"]["Champions"] > 40


def test_segments_need_customers(tmp_path):
    small = Database()
    path = tmp_path / "few.csv"
    path.write_text("Date,Item,Qty,Price,Customer\n2025-01-01,Pen,1,10,A\n2025-01-02,Ink,1,5,B\n")
    ingest_file(small, path, today=TODAY)
    with pytest.raises(NotEnoughData, match="at least 50 customers with a customer_id; there are 2"):
        segments.run(small.cursor())


def test_planted_flash_sale_and_outage_are_found(cur):
    events = planted_events(MONTHS, END)
    result = anomalies.run(cur)
    days = result.tables["anomaly_days"].set_index("day")
    flash, outage = events["flash_sale"].date(), events["outage"].date()
    assert days.loc[flash, "direction"] == "spike" and days.loc[flash, "signal"] in ("orders", "both")
    assert days.loc[outage, "direction"] == "drop"
    assert len(days) <= 10  # a handful of days, not noise


def test_unusual_days_ignore_closed_weekdays():
    index = pd.date_range("2025-01-06", periods=84)  # 12 weeks
    revenue = pd.Series([0.0 if d.weekday() == 6 else 100.0 for d in index], index=index)
    orders = revenue / 10
    revenue.iloc[40] = 400.0  # one real spike on a weekday
    orders.iloc[40] = 40.0
    flagged = anomalies.unusual_days(revenue, orders)
    assert list(flagged["day"]) == [index[40].date()]


def test_unusual_lines_catch_typos(tmp_path):
    rows = generate(months=3, end=END, orders_per_day=20, seed=5)
    rows.loc[10, "unit_price"] *= 10      # a price typed with an extra zero
    rows.loc[20, "quantity"] = 60         # a bulk order
    path = tmp_path / "typos.csv"
    rows.to_csv(path, index=False)
    database = Database()
    ingest_file(database, path, today=TODAY)
    lines = anomalies.unusual_lines(database.cursor())
    reasons = " | ".join(lines["reason"])
    assert "price 10.0x the usual" in reasons
    assert "quantity 60x the usual" in reasons


def test_runner_records_success_failure_and_skips_duplicates(db):
    ok = runner.run(db, "segments")
    cur = db.cursor()
    assert cur.execute("SELECT status FROM ml_runs WHERE run_id = ?", [ok]).fetchone()[0] == "succeeded"
    assert cur.execute("SELECT count(*) FROM latest_customer_segments").fetchone()[0] > 0

    bad = runner.run(db, "forecast", {"horizon": 180})
    status, message = cur.execute("SELECT status, message FROM ml_runs WHERE run_id = ?", [bad]).fetchone()
    assert status == "failed" and "needs at least" in message

    first, created = runner.start(db, "anomalies", {})
    again, created_again = runner.start(db, "anomalies", {})
    assert created and not created_again and again == first  # a double click reuses the running run
    assert runner.mark_interrupted(db) == 1
    assert cur.execute("SELECT status FROM ml_runs WHERE run_id = ?", [first]).fetchone()[0] == "failed"


# --- churn -------------------------------------------------------------------------

def test_churn_beats_the_recency_rule_and_explains_scores(cur):
    result = churn.run(cur, horizon_days=90)
    m = result.metrics
    assert m["model"] in ("logistic", "boosting") and m["auc"] > m["baseline_auc"] + 0.03
    assert m["top20_capture_pct"] > 20  # better than picking customers at random
    assert m["scored"] == m["high"] + m["medium"] + m["low"] > 100

    scores = result.tables["churn_scores"]
    assert scores["probability"].between(0, 1).all()
    assert ((scores["probability"] >= churn.HIGH) == (scores["risk"] == "high")).all()
    assert scores["value_at_risk"].is_monotonic_decreasing
    # Only regular, still-active repeat customers are scored.
    assert (scores["purchases"] >= 2).all() and (scores["recency_days"] <= 90).all()
    assert (scores["typical_gap_days"] <= 90).all()
    assert scores["signals"].str.len().gt(0).all()
    assert f"{m['one_time']:,} one-time buyers" in result.message


def test_churn_features_use_only_the_past_and_labels_only_the_future():
    days = pd.DataFrame({
        "customer_id": ["A"] * 4 + ["B"] * 3,
        "day": pd.to_datetime(["2025-01-01", "2025-01-11", "2025-01-21", "2025-02-15",
                               "2025-01-05", "2025-01-15", "2025-01-25"]),
        "revenue": [100.0, 100.0, 50.0, 500.0, 10.0, 10.0, -10.0],
        "items": [1, 1, 1, 3, 1, 1, 0],
        "returns": [0, 0, 0, 0, 0, 0, 1],
    })
    snapshot = pd.Timestamp("2025-01-25")
    f = churn.features(days, snapshot, horizon=30)
    a = f.loc["A"]
    assert (a["recency_days"], a["typical_gap_days"], a["purchases"], a["revenue"]) == (4, 10, 3, 250)  # not 2025-02-15
    b = f.loc["B"]
    assert b["recency_days"] == 10 and b["returns"] == 1 and b["purchases"] == 2  # a return day is not a purchase
    assert churn.left(days, f.index, snapshot, 30).to_dict() == {"A": 0, "B": 1}


def test_churn_explains_missing_data(tmp_path):
    short = Database()
    path = tmp_path / "short.csv"
    path.write_text("Date,Item,Qty,Price,Customer\n2025-01-01,Pen,1,10,A\n2025-03-01,Ink,1,5,A\n")
    ingest_file(short, path, today=TODAY)
    with pytest.raises(NotEnoughData, match="needs at least 270 days"):
        churn.run(short.cursor(), horizon_days=90)
    no_customers = Database()
    path.write_text("Date,Item,Qty,Price\n2025-01-01,Pen,1,10\n")
    ingest_file(no_customers, path, today=TODAY)
    with pytest.raises(NotEnoughData, match="needs a customer_id"):
        churn.run(no_customers.cursor())


# --- basket ------------------------------------------------------------------------

def test_basket_finds_the_planted_pairs(cur):
    result = basket.run(cur)
    rules = result.tables["basket_rules"]
    products = rules[rules["level"] == "product"].set_index(["antecedent", "consequent"])
    phone_case = products.loc[("Smartphone X1", "Phone Case")]
    assert 40 < phone_case["confidence_pct"] < 60 and phone_case["lift"] > 2  # planted: 55% add a case
    assert ("Laptop 14 inch", "Wireless Mouse") in products.index
    assert ("Tablet 10 inch", "Keyboard") in products.index
    assert (rules["lift"] >= basket.MIN_LIFT).all()
    assert not any("Air Fryer" in pair for pair in products.index)  # no planted pattern, no rule
    assert "product pairs are bought together" in result.message


def test_basket_needs_real_order_numbers(tmp_path):
    small = Database()
    path = tmp_path / "lines.csv"
    path.write_text("Date,Item,Qty,Price\n2025-01-01,Pen,1,10\n2025-01-01,Ink,1,5\n")
    ingest_file(small, path, today=TODAY)
    with pytest.raises(NotEnoughData, match="real order numbers"):
        basket.run(small.cursor())


def test_churn_and_basket_results_are_read_back(db):
    cur = db.cursor()
    churn_run = results.get_run(cur, runner.run(db, "churn", {"horizon_days": 90}))
    view = results.churn_view(cur, churn_run, limit=5)
    assert [b["risk"] for b in view["bands"]] == ["high", "medium", "low"][: len(view["bands"])]
    assert len(view["customers"]) == 5
    high = results.churn_customers(cur, churn_run, "high", 500)
    assert high["total"] == churn_run["metrics"]["high"] and all(r["risk"] == "high" for r in high["rows"])

    basket_run = results.get_run(cur, runner.run(db, "basket"))
    companions = results.basket_view(cur, basket_run, item="smartphone x1")["rules"]  # any case
    assert companions[0]["consequent"] == "Phone Case"
    assert companions == sorted(companions, key=lambda r: -r["confidence_pct"])
    assert cur.execute("SELECT count(*) FROM latest_basket_rules").fetchone()[0] > 0
