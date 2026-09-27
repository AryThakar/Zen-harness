import pandas as pd
import pytest

from app.ingestion.cleaner import clean
from app.ingestion.mapper import Mapping

from .conftest import TODAY, make_raw

BASE = {"order_date": "date", "product": "item", "quantity": "qty", "unit_price": "price"}


def run(rows, fields=None, **mapping_kwargs):
    raw = make_raw(rows)
    return clean(raw, Mapping(fields=fields or dict(BASE), **mapping_kwargs), today=TODAY)


def line(**overrides):
    return {"date": "2025-03-01", "item": "Pen", "qty": "2", "price": "100", **overrides}


def test_revenue_is_quantity_times_price_minus_discount():
    result = run([line(disc="10%"), line(disc="15"), line()], {**BASE, "discount": "disc"})
    sales = result.sales
    assert sales["discount"].tolist() == [20.0, 15.0, 0.0]
    assert sales["revenue"].tolist() == [180.0, 185.0, 200.0]


def test_fraction_discounts_are_detected_per_column():
    result = run([line(disc="0.1"), line(disc="0.25")], {**BASE, "discount": "disc"})
    assert result.sales["discount"].tolist() == [20.0, 50.0]


def test_percent_mode_accepts_whole_numbers():
    result = run([line(disc="10")], {**BASE, "discount": "disc"}, discount_type="percent")
    assert result.sales["discount"].tolist() == [20.0]


def test_returns_have_negative_revenue_and_a_signed_discount():
    result = run([line(qty="-1", disc="10%"), line(qty="-1", disc="-10")], {**BASE, "discount": "disc"})
    sales = result.sales
    assert sales["is_return"].all()
    assert sales["discount"].tolist() == [-10.0, -10.0]
    assert sales["revenue"].tolist() == [-90.0, -90.0]


def test_amount_only_files_get_quantity_one():
    fields = {"order_date": "date", "product": "service", "amount": "amount"}
    result = run([{"date": "2025-01-05", "service": "SEO", "amount": "45,000"}], fields)
    row = result.sales.iloc[0]
    assert (row.quantity, row.unit_price, row.revenue) == (1.0, 45000.0, 45000.0)


def test_superstore_style_totals_are_turned_into_unit_values():
    # Sales is after a 20% discount; profit gives the cost.
    fields = {"order_date": "date", "product": "item", "quantity": "qty",
              "amount": "sales", "discount": "disc", "line_profit": "profit"}
    rows = [{"date": "2024-07-08", "item": "Table", "qty": "3", "sales": "502.488",
             "disc": "0.2", "profit": "-87.9354"}]
    row = run(rows, fields, discount_type="percent").sales.iloc[0]
    assert row.revenue == pytest.approx(502.488)
    assert row.unit_price == pytest.approx(209.37)
    assert row.discount == pytest.approx(125.622)
    assert row.profit == pytest.approx(-87.9354)
    assert row.unit_cost == pytest.approx((502.488 + 87.9354) / 3, abs=1e-4)


def test_unit_cost_from_line_cost():
    row = run([line(cost="120")], {**BASE, "line_cost": "cost"}).sales.iloc[0]
    assert (row.unit_cost, row.cost, row.profit) == (60.0, 120.0, 80.0)


def test_each_bad_row_gets_a_reason():
    rows = [
        line(),
        line(date=""),
        line(date="35/03/2025"),
        line(date="2027-01-01"),
        line(item=""),
        line(qty="0"),
        line(qty="two"),
        line(price="-5"),
        line(price=""),
    ]
    result = run(rows)
    assert len(result.sales) == 1
    assert result.rejected["reasons"].tolist() == [
        "missing order_date", "bad order_date", "order_date in the future", "missing product",
        "quantity is 0", "bad quantity", "negative unit_price", "missing unit_price",
    ]
    assert result.rejected["row_number"].tolist() == [3, 4, 5, 6, 7, 8, 9, 10]
    assert result.rejected["raw"][0]["date"] == ""


def test_discount_larger_than_the_line_is_rejected():
    result = run([line(disc="500"), line(disc="-5")], {**BASE, "discount": "disc"})
    assert result.rejected["reasons"].tolist() == ["discount larger than line value", "bad discount"]


def test_summary_rows_get_one_clear_reason():
    result = run([line(), {"date": "", "item": "Total", "qty": "", "price": "200"}])
    assert result.rejected["reasons"].tolist() == ["summary row"]


def test_order_level_values_are_filled_from_the_first_line():
    fields = {**BASE, "order_id": "bill", "customer_id": "cust"}
    rows = [line(bill="B1", cust="C1"), line(bill="B1", date="", cust=""), line(bill="B2", date="", cust="")]
    result = run(rows, fields)
    assert result.sales["customer_id"].tolist() == ["C1", "C1"]
    assert result.rejected["reasons"].tolist() == ["missing order_date"]  # B2 has nothing to copy
    assert "Filled 2" in result.warnings[0]


def test_missing_order_ids_are_marked_for_generation():
    result = run([line(), line()], {**BASE})
    assert result.sales["order_id_generated"].all()


def test_text_values_are_tidied():
    fields = {**BASE, "category": "cat", "location": "store"}
    rows = [line(item="pen", cat="stationery", store=""), line(item="Pen", cat="Stationery"),
            line(item="Pen", cat="", store="Pune")]
    sales = run(rows, fields).sales
    assert sales["product"].tolist() == ["Pen", "Pen", "Pen"]
    assert sales["category"].tolist() == ["Stationery", "Stationery", "Uncategorized"]
    assert sales["location"].tolist() == [None, None, "Pune"]


def test_date_parts_are_added():
    row = run([line(date="2025-03-14 10:30")]).sales.iloc[0]
    assert (row.year, row.month, row.week, row.weekday) == (2025, 3, 11, "Friday")


def test_ambiguous_dates_produce_a_warning():
    result = run([line(date="03/04/2025")])
    assert result.sales["order_date"].iloc[0] == pd.Timestamp("2025-04-03")
    assert "ambiguous" in result.warnings[0]


def test_custom_columns_are_typed_and_existing_types_win():
    rows = [line(size="1.5", mode="UPI"), line(size="large", mode="Cash")]
    raw = make_raw(rows)
    mapping = Mapping(fields=dict(BASE), custom={"size": "size", "mode": "payment_mode"})
    result = clean(raw, mapping, custom_types={"size": "DOUBLE"}, today=TODAY)
    assert result.custom_types == {"size": "DOUBLE", "payment_mode": "VARCHAR"}
    assert result.sales["size"].tolist()[0] == 1.5
    assert pd.isna(result.sales["size"].tolist()[1])
    assert "1 value(s) in custom column 'size'" in result.warnings[0]


def test_percent_columns_are_read_one_way():
    # 1 means 1% (not 100%) because other values in the column are above 1
    result = run([line(disc="1"), line(disc="5"), line(disc="0.5")], {**BASE, "discount": "disc"},
                 discount_type="percent")
    assert result.sales["discount"].tolist() == [2.0, 10.0, 1.0]


def test_dash_dates_load():
    result = run([line(date="13-03-2025"), line(date="03-04-2025")])
    assert result.sales["order_date"].tolist() == [pd.Timestamp("2025-03-13"), pd.Timestamp("2025-04-03")]


def test_negative_amounts_without_quantity_are_refunds():
    result = run([{"date": "2025-03-01", "item": "Haircut", "amount": "500"},
                  {"date": "2025-03-02", "item": "Haircut", "amount": "-500"}],
                 {"order_date": "date", "product": "item", "amount": "amount"})
    assert result.rejected.empty
    assert result.sales["quantity"].tolist() == [1.0, -1.0]
    assert result.sales["unit_price"].tolist() == [500.0, 500.0]
    assert result.sales["revenue"].tolist() == [500.0, -500.0]
    assert result.sales["is_return"].tolist() == [False, True]


def test_order_numbers_reused_on_other_days_are_flagged():
    rows = [line(date=f"2025-03-0{d}", bill=f"B{n}") for d in (1, 2) for n in range(12)]
    result = run(rows, {**BASE, "order_id": "bill"})
    assert any("12 order numbers appear on more than one day" in w for w in result.warnings)
    unique = run([line(bill=f"B{n}") for n in range(12)], {**BASE, "order_id": "bill"})
    assert not any("appear on more than one day" in w for w in unique.warnings)
