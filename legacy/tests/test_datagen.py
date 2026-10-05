from datetime import date

import pytest

from legacy.datagen import _rupees, generate, to_messy_export
from legacy.ingestion.pipeline import ingest_file

from .conftest import TODAY

END = date(2026, 9, 26)


@pytest.fixture(scope="module")
def synthetic():
    return generate(months=3, end=END, orders_per_day=15, seed=1)


def test_generation_is_deterministic(synthetic):
    again = generate(months=3, end=END, orders_per_day=15, seed=1)
    assert synthetic.equals(again)


def test_planted_patterns_exist(synthetic):
    assert (synthetic.quantity < 0).any()  # returns
    phone_orders = set(synthetic.loc[synthetic["product"] == "Smartphone X1", "order_id"])
    case_orders = set(synthetic.loc[synthetic["product"] == "Phone Case", "order_id"])
    assert len(phone_orders & case_orders) > 0.3 * len(phone_orders)  # bought together
    assert synthetic.customer_id.isna().any() and synthetic.customer_id.notna().any()


def test_canonical_output_loads_without_rejections(db, synthetic, tmp_path):
    path = tmp_path / "canonical.csv"
    synthetic.to_csv(path, index=False)
    report = ingest_file(db, path, today=TODAY)
    assert report.rows_rejected == 0
    assert report.locked_features == {}
    assert report.custom_columns == {"payment_mode": "VARCHAR"}


def test_messy_export_cleans_to_the_same_numbers(db, synthetic, tmp_path):
    path = tmp_path / "messy.csv"
    path.write_text(to_messy_export(synthetic), encoding="utf-8")
    report = ingest_file(db, path, today=TODAY)
    assert report.rows_loaded == len(synthetic)
    assert report.rejected_reasons == {"summary row": 1}

    cur = db.cursor()
    revenue, profit = cur.execute("SELECT sum(revenue), sum(profit) FROM sales").fetchone()
    cur.close()
    expected = (synthetic.quantity * synthetic.unit_price - synthetic.discount).sum()
    assert revenue == pytest.approx(expected, rel=1e-9)
    assert profit == pytest.approx(expected - (synthetic.quantity * synthetic.unit_cost).sum(), rel=1e-9)


def test_rupee_formatting_uses_indian_grouping():
    assert _rupees(1234567.5) == "₹12,34,567.50"
    assert _rupees(999) == "₹999.00"
    assert _rupees(-1299) == "-₹1,299.00"
