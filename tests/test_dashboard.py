from datetime import date

import pytest

from app.analytics import dashboard as d
from app.ingestion.pipeline import ingest_file

from .conftest import SAMPLES, TODAY


@pytest.fixture
def cur(db):
    cursor = db.cursor()
    yield cursor
    cursor.close()


def load(db, text, write_file, name="sales.csv"):
    return ingest_file(db, write_file(text, name), today=TODAY)


SALES = """Order,Date,Item,Category,Qty,Price,Cost,Customer
O1,2025-03-01,Pen,Stationery,2,10,4,A
O1,2025-03-01,Ink,Stationery,1,50,30,A
O2,2025-03-05,Lamp,Home,1,100,60,B
O3,2025-03-10,Pen,Stationery,-1,10,4,A
O4,2025-02-20,Lamp,Home,1,100,60,C
"""


def test_empty_database_has_no_period(cur):
    assert d.resolve_period(cur) is None
    assert d.data_range(cur) is None


def test_default_period_is_last_90_days_of_data(db, cur, write_file):
    load(db, SALES, write_file)
    assert d.data_range(cur) == d.Period(date(2025, 2, 20), date(2025, 3, 10))
    assert d.resolve_period(cur) == d.Period(date(2025, 2, 20), date(2025, 3, 10))
    assert d.resolve_period(cur, end=date(2025, 3, 5)).end == date(2025, 3, 5)
    with pytest.raises(d.InvalidQuery):
        d.resolve_period(cur, date(2025, 3, 10), date(2025, 3, 1))


def test_previous_period_has_the_same_length():
    p = d.Period(date(2025, 3, 1), date(2025, 3, 31))
    assert p.days == 31
    assert p.previous() == d.Period(date(2025, 1, 29), date(2025, 2, 28))


def test_summary_kpis(db, cur, write_file):
    load(db, SALES, write_file)
    result = d.summary(cur, d.Period(date(2025, 3, 1), date(2025, 3, 10)))
    k = {name: v["value"] for name, v in result["kpis"].items()}
    # revenue: 20 + 50 + 100 - 10 (return) = 160; orders exclude the return line
    assert k["revenue"] == 160
    assert k["orders"] == 2
    assert k["avg_order_value"] == 80
    assert k["units"] == 4
    assert k["profit"] == 160 - (8 + 30 + 60 - 4)
    assert k["margin_pct"] == pytest.approx(100 * 66 / 160)
    assert k["customers"] == 2
    assert k["new_customers"] == 2  # A and B first bought in March; C in February
    assert k["return_rate_pct"] == pytest.approx(100 * 10 / 170)
    assert result["locked"] == {"location": ["location comparison"], "channel": ["channel comparison"]}


def test_changes_only_when_the_previous_period_is_fully_covered(db, cur, write_file):
    load(db, SALES, write_file)  # data starts 20 Feb

    full = d.summary(cur, d.Period(date(2025, 3, 5), date(2025, 3, 10)))  # previous: 27 Feb - 4 Mar
    assert full["comparison"] == "full"
    assert full["kpis"]["revenue"]["previous"] == 70
    assert full["kpis"]["revenue"]["change_pct"] == pytest.approx(100 * (90 - 70) / 70)

    partial = d.summary(cur, d.Period(date(2025, 3, 1), date(2025, 3, 10)))  # previous: 19-28 Feb
    assert partial["comparison"] == "partial"
    assert partial["kpis"]["revenue"]["previous"] == 100
    assert partial["kpis"]["revenue"]["change_pct"] is None

    none = d.summary(cur, d.Period(date(2025, 2, 20), date(2025, 3, 10)))  # previous: 1-19 Feb
    assert none["comparison"] == "none"
    assert none["kpis"]["revenue"]["previous"] is None
    assert none["kpis"]["revenue"]["change_pct"] is None


def test_profit_and_customers_are_hidden_when_columns_are_missing(db, cur):
    ingest_file(db, SAMPLES / "retail_pos_messy.csv", today=TODAY)  # no cost column
    result = d.summary(cur, d.resolve_period(cur))
    assert result["kpis"]["profit"]["value"] is None
    assert result["kpis"]["margin_pct"]["value"] is None
    assert result["kpis"]["customers"]["value"] is not None
    assert "unit_cost" in result["locked"]


def test_trend_fills_empty_days_and_flags_partial_buckets(db, cur, write_file):
    load(db, SALES, write_file)
    days = d.trend(cur, d.Period(date(2025, 3, 1), date(2025, 3, 5)))
    assert days["grain"] == "day"
    assert [p["revenue"] for p in days["points"]] == [70, 0, 0, 0, 100]
    assert not any(p["partial"] for p in days["points"])

    weeks = d.trend(cur, d.Period(date(2025, 3, 1), date(2025, 3, 10)), grain="week")
    assert [p["bucket"] for p in weeks["points"]] == [date(2025, 2, 24), date(2025, 3, 3), date(2025, 3, 10)]
    assert [p["partial"] for p in weeks["points"]] == [True, False, True]

    with pytest.raises(d.InvalidQuery):
        d.trend(cur, d.Period(date(2025, 3, 1), date(2025, 3, 5)), grain="hour")


def test_grain_follows_the_period_length():
    assert d.pick_grain(d.Period(date(2025, 1, 1), date(2025, 2, 1))) == "day"
    assert d.pick_grain(d.Period(date(2025, 1, 1), date(2025, 6, 1))) == "week"
    assert d.pick_grain(d.Period(date(2024, 1, 1), date(2025, 6, 1))) == "month"


def test_breakdown_top_items_and_other(db, cur, write_file):
    load(db, SALES, write_file)
    period = d.Period(date(2025, 2, 1), date(2025, 3, 31))
    result = d.breakdown(cur, period, "product", limit=1)
    assert [(i["label"], i["revenue"]) for i in result["items"]] == [("Lamp", 200)]
    assert result["other"]["label"] == "Other (2)"
    assert result["other"]["revenue"] == 60
    assert result["items"][0]["share_pct"] == pytest.approx(100 * 200 / 260)


def test_breakdown_by_weekday_keeps_calendar_order(db, cur, write_file):
    load(db, SALES, write_file)
    labels = [i["label"] for i in d.breakdown(cur, d.Period(date(2025, 2, 1), date(2025, 3, 31)), "weekday")["items"]]
    assert labels == sorted(labels, key=d.WEEKDAYS.index)


def test_breakdown_accepts_custom_columns_but_not_arbitrary_sql(db, cur, write_file):
    load(db, "Date,Item,Qty,Price,Mode\n2025-03-01,Pen,1,10,UPI\n2025-03-02,Ink,1,5,\n", write_file)
    period = d.resolve_period(cur)
    assert "payment_mode" not in d.breakdown_dimensions(cur)
    labels = {i["label"] for i in d.breakdown(cur, period, "mode")["items"]}
    assert labels == {"UPI", d.NOT_SET}
    for bad in ("revenue", "order_id", "is_return", 'product" FROM sales; --'):
        with pytest.raises(d.InvalidQuery):
            d.breakdown(cur, period, bad)


def test_an_early_end_gives_an_empty_period_not_an_error(db, cur, write_file):
    load(db, SALES, write_file)
    period = d.resolve_period(cur, end=date(2020, 1, 1))
    assert period.end == date(2020, 1, 1) and period.days == d.DEFAULT_DAYS
    assert d.summary(cur, period)["kpis"]["revenue"]["value"] == 0
