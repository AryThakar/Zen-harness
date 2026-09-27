"""Write cleaned rows to DuckDB in one transaction.

Spelling: names that differ only in case from values already stored ('wireless
mouse' vs 'Wireless Mouse') take the stored spelling, so one product stays one
product across uploads.

De-duplication: each row gets a hash of what identifies a transaction - order
number (if real), date, product, quantity, price, discount, customer, location and
channel, ignoring case - plus an occurrence counter. Two identical lines in one
file stay two rows (two customers buying the same item), while re-uploading a file
or an export that overlaps an earlier one skips what is already stored. Product
attributes (category, cost) and custom columns are left out on purpose: correcting
them in a later export shouldn't turn old sales into new ones.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field

import duckdb
import pandas as pd

from app.db import registry

from .cleaner import CleanResult
from .fields import SALES_COLUMNS
from .mapper import Mapping

TEXT_DIMENSIONS = ("product", "category", "location", "channel")


@dataclass
class UploadReport:
    upload_id: int
    filename: str
    rows_total: int
    rows_loaded: int
    rows_rejected: int
    rows_duplicate: int
    rejected_reasons: dict[str, int]
    warnings: list[str] = field(default_factory=list)
    custom_columns: dict[str, str] = field(default_factory=dict)
    locked_features: dict[str, list[str]] = field(default_factory=dict)

    def summary(self) -> str:
        parts = [f"{self.rows_loaded:,} loaded"]
        if self.rows_rejected:
            reasons = ", ".join(f"{r} {n}" for r, n in self.rejected_reasons.items())
            parts.append(f"{self.rows_rejected:,} rejected ({reasons})")
        if self.rows_duplicate:
            parts.append(f"{self.rows_duplicate:,} duplicates skipped")
        return " · ".join(parts)


def row_hashes(sales: pd.DataFrame) -> pd.Series:
    """Stable content hash per row, with an occurrence counter for identical rows."""
    def text(col: str) -> pd.Series:
        return sales[col].fillna("").astype(str).str.lower()

    real_order_id = sales["order_id"].where(~sales["order_id_generated"], "").fillna("").astype(str).str.lower()
    key = (
        real_order_id
        + "|" + sales["order_date"].dt.strftime("%Y-%m-%d %H:%M:%S")
        + "|" + text("product")
        + "|" + sales["quantity"].round(6).astype(str)
        + "|" + sales["unit_price"].round(4).astype(str)
        + "|" + sales["discount"].round(4).astype(str)
        + "|" + text("customer_id")
        + "|" + text("location")
        + "|" + text("channel")
    )
    occurrence = key.groupby(key).cumcount().astype(str)
    return pd.Series(
        [hashlib.md5(k.encode()).hexdigest() for k in key + "|" + occurrence],
        index=sales.index,
    )


def match_existing_spelling(cur: duckdb.DuckDBPyConnection, sales: pd.DataFrame) -> pd.DataFrame:
    """Give names the spelling already used in the database when only the case differs."""
    sales = sales.copy()
    for col in TEXT_DIMENSIONS:
        values = sales[col].dropna().unique()
        if not len(values):
            continue
        cur.register("new_values", pd.DataFrame({"value": values}))
        try:
            rows = cur.execute(
                f"""
                WITH existing AS (
                    SELECT lower("{col}") AS key, arg_max("{col}", n) AS spelling
                    FROM (SELECT "{col}", count(*) AS n FROM sales WHERE "{col}" IS NOT NULL GROUP BY 1)
                    GROUP BY 1
                )
                SELECT v.value, e.spelling FROM new_values v JOIN existing e ON lower(v.value) = e.key
                WHERE v.value <> e.spelling
                """
            ).fetchall()
        finally:
            cur.unregister("new_values")
        if rows:
            sales[col] = sales[col].replace(dict(rows))
    return sales


def load(
    cur: duckdb.DuckDBPyConnection,
    result: CleanResult,
    *,
    filename: str,
    mapping: Mapping,
    rows_total: int,
    profile_name: str | None = None,
    locked_features: dict[str, list[str]] | None = None,
) -> UploadReport:
    """Insert ``result`` using a cursor that is already inside a transaction."""
    sales = match_existing_spelling(cur, result.sales)
    sales["row_hash"] = row_hashes(sales)
    upload_id = cur.execute("SELECT nextval('upload_id_seq')").fetchone()[0]
    sales["upload_id"] = upload_id
    generated = sales["order_id_generated"]
    sales.loc[generated, "order_id"] = (
        f"AUTO-{upload_id}-" + pd.Series(sales.index[generated], index=sales.index[generated]).astype(str)
    )

    registry.add_custom_columns(cur, result.custom_types, mapping.descriptions)

    custom = list(result.custom_types)
    columns = ["upload_id", "row_hash", *SALES_COLUMNS, *custom]
    select = ", ".join(
        f'CAST(s."{c}" AS {result.custom_types[c]})' if c in result.custom_types else f's."{c}"'
        for c in columns
    )
    target = ", ".join(f'"{c}"' for c in columns)
    cur.register("staged_sales", sales[columns])
    try:
        inserted = cur.execute(
            f"""
            INSERT INTO sales (sale_id, {target})
            SELECT nextval('sale_id_seq'), {select} FROM staged_sales s
            WHERE NOT EXISTS (SELECT 1 FROM sales t WHERE t.row_hash = s.row_hash)
            """
        ).fetchone()[0]
    finally:
        cur.unregister("staged_sales")

    rejected = result.rejected
    if len(rejected):
        staged = pd.DataFrame({
            "row_number": rejected["row_number"].astype(int),
            "reasons": rejected["reasons"].astype(str),
            "raw": [json.dumps(r, default=str) for r in rejected["raw"]],
        })
        cur.register("staged_rejected", staged)
        try:
            cur.execute(
                "INSERT INTO rejected_rows SELECT ?, row_number, reasons, raw::JSON FROM staged_rejected",
                [upload_id],
            )
        finally:
            cur.unregister("staged_rejected")

    rows_duplicate = len(sales) - inserted
    cur.execute(
        "INSERT INTO uploads (upload_id, filename, profile_name, rows_total, rows_loaded, "
        "rows_rejected, rows_duplicate, mapping, warnings) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [upload_id, filename, profile_name, rows_total, inserted, len(rejected), rows_duplicate,
         json.dumps(mapping.to_dict()), json.dumps(result.warnings)],
    )

    reasons = Counter(r for rs in rejected["reasons"] for r in rs.split("; "))
    return UploadReport(
        upload_id=upload_id,
        filename=filename,
        rows_total=rows_total,
        rows_loaded=inserted,
        rows_rejected=len(rejected),
        rows_duplicate=rows_duplicate,
        rejected_reasons=dict(reasons.most_common()),
        warnings=result.warnings,
        custom_columns=result.custom_types,
        locked_features=locked_features or {},
    )


def delete_upload(cur: duckdb.DuckDBPyConnection, upload_id: int) -> bool:
    """Remove an upload and its rows (inside a transaction). False if it doesn't exist."""
    if not cur.execute("SELECT 1 FROM uploads WHERE upload_id = ?", [upload_id]).fetchone():
        return False
    for table in ("sales", "rejected_rows", "uploads"):
        cur.execute(f"DELETE FROM {table} WHERE upload_id = ?", [upload_id])
    return True
