import json

import pandas as pd
import pytest

from app.ingestion.mapper import MappingError
from app.ingestion.pipeline import ingest_file, preview_file

from .conftest import SAMPLES, TODAY


def query(db, sql, params=None):
    cur = db.cursor()
    try:
        return cur.execute(sql, params or []).fetchall()
    finally:
        cur.close()


def test_messy_pos_export(db):
    report = ingest_file(db, SAMPLES / "retail_pos_messy.csv", today=TODAY)
    assert (report.rows_total, report.rows_loaded, report.rows_rejected) == (21, 16, 5)
    assert report.rejected_reasons == {
        "bad order_date": 1, "quantity is 0": 1, "missing product": 1, "bad unit_price": 1,
        "summary row": 1,
    }
    assert report.custom_columns == {"payment_mode": "VARCHAR"}
    assert "location" in report.locked_features

    rejected = query(db, "SELECT row_number, reasons FROM rejected_rows ORDER BY row_number")
    assert rejected == [(19, "bad order_date"), (20, "quantity is 0"), (21, "missing product"),
                        (22, "bad unit_price"), (27, "summary row")]
    # 'wireless mouse' / 'accessories' were unified with the common spelling.
    products = {p for (p,) in query(db, "SELECT DISTINCT product FROM sales")}
    assert "Wireless Mouse" in products and "wireless mouse" not in products
    assert query(db, "SELECT revenue FROM sales WHERE order_id = 'B-1001' AND product = 'Wireless Mouse'") == [(2338.2,)]


def test_shopify_export(db):
    report = ingest_file(db, SAMPLES / "shopify_orders.csv", today=TODAY)
    assert (report.rows_loaded, report.rows_rejected) == (11, 0)
    rows = query(db, "SELECT order_id, customer_id, location, channel, discount, revenue FROM sales "
                     "WHERE order_id = '#1002' ORDER BY product")
    assert rows == [
        ("#1002", "rahul@example.com", "Pune", "web", 24.9, 224.1),
        ("#1002", "rahul@example.com", "Pune", "web", 899.9, 8099.1),
    ]
    assert query(db, "SELECT profile_name FROM uploads") == [("Shopify orders export",)]
    columns = {c for (c,) in query(db, "SELECT column_name FROM column_registry WHERE kind = 'custom'")}
    assert "billing_name" not in columns and "total" not in columns


def test_superstore_dataset(db):
    report = ingest_file(db, SAMPLES / "superstore_sample.csv", today=TODAY)
    assert report.rows_loaded == 8
    total_sales, total_profit = query(db, "SELECT sum(revenue), sum(profit) FROM sales")[0]
    assert total_sales == pytest.approx(1506.192, abs=1e-3)
    assert total_profit == pytest.approx(113.3002, abs=1e-3)
    ship = query(db, "SELECT ship_date FROM sales WHERE order_id = 'CA-2024-100293'")[0][0]
    assert pd.Timestamp(ship) == pd.Timestamp("2024-03-18")  # month-first, from the profile


def test_service_invoices_without_quantity(db):
    report = ingest_file(db, SAMPLES / "agency_invoices.csv", today=TODAY)
    assert report.rows_loaded == 10
    assert query(db, "SELECT sum(revenue), max(quantity) FROM sales") == [(396500.0, 1.0)]
    assert query(db, "SELECT revenue, orders FROM customers WHERE customer_id = 'Acme Foods'") == [(153500.0, 4)]


def test_reuploading_skips_duplicates(db):
    first = ingest_file(db, SAMPLES / "retail_pos_messy.csv", today=TODAY)
    second = ingest_file(db, SAMPLES / "retail_pos_messy.csv", today=TODAY)
    assert (second.rows_loaded, second.rows_duplicate) == (0, first.rows_loaded)
    assert query(db, "SELECT count(*) FROM sales") == [(16,)]


def test_identical_lines_are_kept_but_overlaps_are_not(db, write_file):
    header = "Date,Item,Qty,Price\n"
    same = "2025-03-01,Milk,1,30\n"
    first = ingest_file(db, write_file(header + same * 2, "day1.csv"), today=TODAY)
    assert first.rows_loaded == 2  # two customers bought the same thing
    overlap = ingest_file(db, write_file(header + same * 3 + "2025-03-02,Milk,1,30\n", "days1-2.csv"), today=TODAY)
    assert (overlap.rows_loaded, overlap.rows_duplicate) == (2, 2)
    assert query(db, "SELECT count(*) FROM sales") == [(4,)]


def test_generated_order_ids_are_unique_per_upload(db, write_file):
    ingest_file(db, write_file("Date,Item,Qty,Price\n2025-03-01,Pen,1,10\n2025-03-01,Ink,1,50\n"), today=TODAY)
    assert query(db, "SELECT order_id FROM sales ORDER BY sale_id") == [("AUTO-1-2",), ("AUTO-1-3",)]


def test_custom_columns_are_added_and_registered(db, write_file):
    path = write_file("Date,Item,Qty,Price,Salesperson,Gift Wrap\n2025-03-01,Pen,1,10,Asha,yes\n")
    preview = preview_file(db, path)
    mapping = preview.suggestion.mapping
    mapping.descriptions = {"salesperson": "Who made the sale"}
    ingest_file(db, path, mapping, today=TODAY)

    registry = query(db, "SELECT column_name, data_type, description, sample_values FROM column_registry "
                         "WHERE kind = 'custom' ORDER BY column_name")
    assert registry == [
        ("gift_wrap", "BOOLEAN", None, "[]"),
        ("salesperson", "VARCHAR", "Who made the sale", '["Asha"]'),
    ]
    assert query(db, "SELECT salesperson, gift_wrap FROM sales") == [("Asha", True)]


def test_saved_profile_is_recognised_next_time(db, write_file):
    path = write_file("Dt,Itm,Q,P,Mode\n01/03/2025,Pen,1,10,UPI\n")
    preview = preview_file(db, path)
    assert preview.suggestion.missing_required  # nothing matches these short names
    mapping = preview.suggestion.mapping
    mapping.fields = {"order_date": "dt", "product": "itm", "quantity": "q", "unit_price": "p"}
    mapping.custom = {"mode": "payment_mode"}
    ingest_file(db, path, mapping, save_profile_as="Corner shop POS", today=TODAY)

    again = preview_file(db, write_file("Dt,Itm,Q,P,Mode\n02/03/2025,Ink,2,50,Cash\n", "next.csv"))
    assert again.suggestion.profile == "Corner shop POS"
    assert again.suggestion.missing_required == []
    assert again.suggestion.mapping.custom == {"mode": "payment_mode"}


def test_builtin_profiles_cannot_be_overwritten(db, write_file):
    path = write_file("Date,Item,Qty,Price\n2025-03-01,Pen,1,10\n")
    with pytest.raises(ValueError, match="built-in"):
        ingest_file(db, path, save_profile_as="Shopify orders export", today=TODAY)
    assert query(db, "SELECT count(*) FROM sales") == [(0,)]  # nothing half-loaded


def test_missing_required_columns_stop_the_upload(db, write_file):
    with pytest.raises(MappingError, match="order_date"):
        ingest_file(db, write_file("Item,Qty,Price\nPen,1,10\n"), today=TODAY)


def test_summary_tables(db):
    ingest_file(db, SAMPLES / "retail_pos_messy.csv", today=TODAY)
    earbuds = query(db, "SELECT units_sold, units_returned, revenue FROM products WHERE product = 'Earbuds Pro'")
    assert earbuds == [(1.0, 1.0, 0.0)]
    c501 = query(db, "SELECT orders, recency_days FROM customers WHERE customer_id = 'C-501'")
    assert c501 == [(2, 17)]  # B-1001 and B-1005; last activity 8 Mar, latest sale 25 Mar


def test_excel_upload_end_to_end(db, tmp_path):
    path = tmp_path / "sales.xlsx"
    pd.DataFrame({
        "Invoice No": ["I1", "I1", "I2"],
        "Invoice Date": pd.to_datetime(["2025-03-01", "2025-03-01", "2025-03-04"]),
        "Product": ["Pen", "Ink", "Pen"],
        "Qty": [2, 1, 1],
        "Rate": [10, 50, 10],
        "Cost Price": [4, 30, 4],
    }).to_excel(path, index=False)
    report = ingest_file(db, path, today=TODAY)
    assert report.rows_loaded == 3
    assert query(db, "SELECT sum(revenue), sum(profit) FROM sales") == [(80.0, 38.0)]


def test_upload_record_keeps_the_mapping(db):
    report = ingest_file(db, SAMPLES / "agency_invoices.csv", today=TODAY)
    mapping = json.loads(query(db, "SELECT mapping FROM uploads WHERE upload_id = ?", [report.upload_id])[0][0])
    assert mapping["fields"]["amount"] == "amount"
