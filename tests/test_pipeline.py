import json
from pathlib import Path

import pandas as pd
import pytest

from app.db import registry
from app.db.connection import Database
from app.ingestion import pipeline
from app.ingestion.mapper import MappingError
from app.ingestion.pipeline import ingest_file, preview_file
from app.ingestion.profiles import ProfileError

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

    cur = db.cursor()
    custom = [c for c in registry.columns(cur) if c["kind"] == "custom"]
    cur.close()
    assert [(c["column_name"], c["data_type"], c["description"], c["sample_values"]) for c in custom] == [
        ("gift_wrap", "BOOLEAN", None, [True]),
        ("salesperson", "VARCHAR", "Who made the sale", ["Asha"]),
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
    with pytest.raises(ProfileError, match="built-in"):
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


def test_same_item_in_two_stores_is_not_a_duplicate(db, write_file):
    header = "Date,Item,Qty,Price,Store\n"
    ingest_file(db, write_file(header + "2025-01-05,Tea,1,10,Pune\n", "pune.csv"), today=TODAY)
    report = ingest_file(db, write_file(header + "2025-01-05,Tea,1,10,Mumbai\n", "mumbai.csv"), today=TODAY)
    assert (report.rows_loaded, report.rows_duplicate) == (1, 0)


def test_spelling_follows_what_is_already_stored(db, write_file):
    header = "Date,Item,Qty,Price,Category\n"
    ingest_file(db, write_file(header + "2025-01-05,Wireless Mouse,1,10,Accessories\n", "a.csv"), today=TODAY)
    ingest_file(db, write_file(header + "2025-01-06,wireless mouse,1,10,ACCESSORIES\n", "b.csv"), today=TODAY)
    assert query(db, "SELECT DISTINCT product, category FROM sales") == [("Wireless Mouse", "Accessories")]
    # and a re-export in different case is recognised as the same sales
    again = ingest_file(db, write_file(header + "2025-01-05,WIRELESS MOUSE,1,10,accessories\n", "c.csv"), today=TODAY)
    assert again.rows_duplicate == 1


def test_generated_order_ids_are_flagged_not_guessed(db, write_file):
    ingest_file(db, write_file("Order,Date,Item,Qty,Price\nAUTO-77,2025-03-01,Pen,1,10\n,2025-03-01,Ink,1,5\n"),
                today=TODAY)
    assert query(db, "SELECT order_id, order_id_generated FROM sales ORDER BY sale_id") == [
        ("AUTO-77", False), ("AUTO-1-3", True),
    ]


def test_concurrent_custom_column_types_are_reconciled(db, write_file, monkeypatch):
    # Another upload created 'batch' as a number after this upload read the types.
    ingest_file(db, write_file("Date,Item,Qty,Price,Batch\n2025-03-01,Pen,1,10,7\n", "first.csv"), today=TODAY)
    real = registry.custom_types
    calls = {"n": 0}

    def stale_then_real(cur):
        calls["n"] += 1
        return {} if calls["n"] == 1 else real(cur)

    monkeypatch.setattr(pipeline.registry, "custom_types", stale_then_real)
    report = ingest_file(db, write_file("Date,Item,Qty,Price,Batch\n2025-03-02,Ink,1,5,B-12\n", "second.csv"),
                         today=TODAY)
    assert report.rows_loaded == 1
    assert report.custom_columns == {"batch": "DOUBLE"}
    assert "could not be read as DOUBLE" in report.warnings[0]


def test_week_one_database_is_upgraded(tmp_path):
    import duckdb

    path = tmp_path / "old.duckdb"
    old = duckdb.connect(str(path))
    old.execute((Path(pipeline.__file__).parent.parent / "db" / "migrations" / "001_initial.sql").read_text())
    old.execute("INSERT INTO sales (sale_id, upload_id, row_hash, order_id, order_date, product, quantity, unit_price, "
                "discount, is_return, revenue, year, month, week, weekday) VALUES "
                "(1, 1, 'h1', 'AUTO-1-2', '2025-01-01', 'Pen', 1, 10, 0, false, 10, 2025, 1, 1, 'Wednesday'), "
                "(2, 1, 'h2', 'INV-9', '2025-01-02', 'Pen', 2, 10, 0, false, 20, 2025, 1, 1, 'Thursday')")
    old.execute("CREATE TABLE products AS SELECT product, sum(revenue) AS revenue FROM sales GROUP BY 1")
    old.close()

    db = Database(path)
    assert query(db, "SELECT order_id, order_id_generated FROM sales ORDER BY sale_id") == [
        ("AUTO-1-2", True), ("INV-9", False),
    ]
    assert query(db, "SELECT table_type FROM information_schema.tables WHERE table_name = 'products'") == [("VIEW",)]
    assert query(db, "SELECT units_sold FROM products") == [(3.0,)]
    assert db.migrate() == []  # nothing left to apply
    db.close()
