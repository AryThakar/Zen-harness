"""Shared helpers for the benchmark: the real datasets, the reference mappings, and
databases loaded through the app's own upload pipeline."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pandas as pd

from app.db.connection import Database
from app.ingestion.mapper import Mapping
from app.ingestion.pipeline import ingest_file

BENCH = Path(__file__).resolve().parent
DATA = BENCH / "data"
WORK = BENCH / "work"
WORK.mkdir(exist_ok=True)

RETAIL = DATA / "online_retail_ii" / "online_retail_II.xlsx"
RETAIL_SHEETS = ("Year 2009-2010", "Year 2010-2011")
COFFEE = DATA / "coffee" / "Coffee Shop Sales.xlsx"
CDNOW_RAW = DATA / "cdnow" / "CDNOW_master.txt"
PIZZA = WORK / "pizza_export.csv"
CDNOW = WORK / "cdnow_export.csv"

# The correct mapping for each file, written before running (bench/PLAN.md).
# field -> acceptable source columns (normalized names); the first is used to load.
REFERENCE = {
    "retail": {
        "order_id": ["invoice"], "order_date": ["invoicedate"], "product": ["description", "stockcode"],
        "quantity": ["quantity"], "unit_price": ["price"], "customer_id": ["customer_id"],
        "location": ["country"],
    },
    "coffee": {
        "order_id": ["transaction_id"], "order_date": ["transaction_date"],
        "product": ["product_detail", "product_type", "product_id"], "quantity": ["transaction_qty"],
        "unit_price": ["unit_price"], "category": ["product_category", "product_type"],
        "location": ["store_location"],
    },
    "pizza": {
        "order_id": ["order_id"], "order_date": ["date"], "product": ["name", "pizza_id"],
        "quantity": ["quantity"], "unit_price": ["price"], "category": ["category"],
    },
    # Pizza again with the size-specific SKU as the product (one price per product).
    "pizza_sku": {
        "order_id": ["order_id"], "order_date": ["date"], "product": ["pizza_id"],
        "quantity": ["quantity"], "unit_price": ["price"], "category": ["category"],
    },
    "cdnow": {
        "customer_id": ["customer_id"], "order_date": ["date"], "product": ["product"],
        "quantity": ["quantity"], "amount": ["amount"],
    },
}


def prepare() -> None:
    """The only preparation: join Pizza's four tables into one till export, and give
    CDNOW a header and a product column (for the ML tests; it has no product)."""
    if not PIZZA.exists():
        p = DATA / "pizza" / "pizza_sales"
        details = pd.read_csv(p / "order_details.csv")
        orders = pd.read_csv(p / "orders.csv")
        pizzas = pd.read_csv(p / "pizzas.csv")
        types = pd.read_csv(p / "pizza_types.csv", encoding="latin-1")
        out = (details.merge(orders, on="order_id").merge(pizzas, on="pizza_id")
               .merge(types[["pizza_type_id", "name", "category"]], on="pizza_type_id"))
        out = out[["order_details_id", "order_id", "date", "time", "pizza_id", "name", "category", "size",
                   "quantity", "price"]].sort_values("order_details_id")
        out.to_csv(PIZZA, index=False)
    if not CDNOW.exists():
        df = pd.read_csv(CDNOW_RAW, sep=r"\s+", header=None, names=["customer_id", "date", "quantity", "amount"],
                         dtype={"customer_id": str, "date": str})
        df["date"] = pd.to_datetime(df["date"], format="%Y%m%d").dt.strftime("%Y-%m-%d")
        df["product"] = "CD"
        df.to_csv(CDNOW, index=False)


def reference_mapping(name: str) -> Mapping:
    return Mapping(fields={f: sources[0] for f, sources in REFERENCE[name].items()})


def files(name: str) -> list[tuple[Path, str | None]]:
    return {
        "retail": [(RETAIL, s) for s in RETAIL_SHEETS],
        "coffee": [(COFFEE, None)],
        "pizza": [(PIZZA, None)],
        "pizza_sku": [(PIZZA, None)],
        "cdnow": [(CDNOW, None)],
    }[name]


def database(name: str) -> Path:
    """A DuckDB file holding ``name`` loaded with the reference mapping through the
    app's own pipeline (built once, then reused)."""
    path = WORK / f"{name}.duckdb"
    if path.exists():
        return path
    prepare()
    db = Database(path)
    reports = []
    for file, sheet in files(name):
        mapping = reference_mapping(name)
        if name == "retail":
            mapping.date_order = "monthfirst"  # as the app's built-in UCI profile says
        r = ingest_file(db, file, mapping, sheet=sheet)
        reports.append({"file": file.name, "sheet": sheet, "summary": r.summary(), "loaded": r.rows_loaded,
                        "rejected": r.rows_rejected, "duplicates": r.rows_duplicate,
                        "reasons": r.rejected_reasons, "warnings": r.warnings})
    db.close()
    (WORK / f"{name}_load.json").write_text(json.dumps(reports, indent=2, default=str))
    return path


def snapshot(name: str, start=None, end=None, transform=None) -> duckdb.DuckDBPyConnection:
    """An in-memory connection whose ``sales`` table holds only the rows with
    start <= order_date < end - the app's ML code sees nothing else.
    ``transform(df) -> df`` may change the rows first (injected anomalies, shuffles)."""
    src = duckdb.connect(str(database(name)), read_only=True)
    where, params = [], []
    if start is not None:
        where.append("order_date >= ?")
        params.append(pd.Timestamp(start))
    if end is not None:
        where.append("order_date < ?")
        params.append(pd.Timestamp(end))
    sql = "SELECT * FROM sales" + (" WHERE " + " AND ".join(where) if where else "")
    df = src.execute(sql, params).df()
    src.close()
    if transform is not None:
        df = transform(df)
    con = duckdb.connect()
    con.register("frame", df)
    con.execute("CREATE TABLE sales AS SELECT * FROM frame")
    con.unregister("frame")
    return con


def sales(name: str) -> pd.DataFrame:
    con = duckdb.connect(str(database(name)), read_only=True)
    try:
        return con.execute("SELECT * FROM sales").df()
    finally:
        con.close()
