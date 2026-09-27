"""Write cleaned rows to DuckDB in one transaction.

De-duplication: each row gets a hash of its business content plus an
occurrence counter. Two identical lines in one file (two customers buying the
same item at the same price) stay two rows, while re-uploading the same file -
or an export that overlaps an earlier one - skips what is already stored.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field

import duckdb
import pandas as pd

from .cleaner import CleanResult
from .fields import SALES_COLUMNS
from .mapper import Mapping

SAMPLE_VALUES = 5


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
    real_order_id = sales["order_id"].where(~sales["order_id_generated"], "").fillna("")
    key = (
        real_order_id.astype(str)
        + "|" + sales["order_date"].dt.strftime("%Y-%m-%d %H:%M:%S")
        + "|" + sales["product"].astype(str)
        + "|" + sales["quantity"].round(6).astype(str)
        + "|" + sales["unit_price"].round(4).astype(str)
        + "|" + sales["customer_id"].fillna("").astype(str)
    )
    occurrence = key.groupby(key).cumcount().astype(str)
    return pd.Series(
        [hashlib.md5(k.encode()).hexdigest() for k in key + "|" + occurrence],
        index=sales.index,
    )


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
    sales = result.sales.copy()
    sales["row_hash"] = row_hashes(sales)
    upload_id = cur.execute("SELECT nextval('upload_id_seq')").fetchone()[0]
    sales["upload_id"] = upload_id
    generated = sales["order_id_generated"]
    sales.loc[generated, "order_id"] = (
        f"AUTO-{upload_id}-" + pd.Series(sales.index[generated], index=sales.index[generated]).astype(str)
    )

    _add_custom_columns(cur, result.custom_types, mapping.descriptions)

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
            INSERT INTO sales ({target})
            SELECT {select} FROM staged_sales s
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
    refresh_summaries(cur)
    _update_samples(cur)

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
    refresh_summaries(cur)
    _update_samples(cur)
    return True


def custom_column_types(cur: duckdb.DuckDBPyConnection) -> dict[str, str]:
    rows = cur.execute(
        "SELECT column_name, data_type FROM column_registry WHERE kind = 'custom'"
    ).fetchall()
    return dict(rows)


def _add_custom_columns(
    cur: duckdb.DuckDBPyConnection, types: dict[str, str], descriptions: dict[str, str]
) -> None:
    for name, sql_type in types.items():
        cur.execute(f'ALTER TABLE sales ADD COLUMN IF NOT EXISTS "{name}" {sql_type}')
        cur.execute(
            """
            INSERT INTO column_registry (column_name, data_type, kind, description, sample_values)
            VALUES (?, ?, 'custom', ?, '[]')
            ON CONFLICT (column_name) DO UPDATE SET
                description = coalesce(excluded.description, column_registry.description)
            """,
            [name, sql_type, descriptions.get(name)],
        )


def refresh_summaries(cur: duckdb.DuckDBPyConnection) -> None:
    """Rebuild the products and customers tables from sales."""
    cur.execute(
        """
        CREATE OR REPLACE TABLE products AS
        SELECT
            product,
            mode(category)                                   AS category,
            min(order_date)                                  AS first_sold,
            max(order_date)                                  AS last_sold,
            coalesce(sum(quantity) FILTER (WHERE NOT is_return), 0)  AS units_sold,
            coalesce(-sum(quantity) FILTER (WHERE is_return), 0)     AS units_returned,
            sum(revenue)                                     AS revenue,
            sum(revenue) / nullif(sum(quantity), 0)          AS avg_net_price,
            sum(profit)                                      AS profit,
            100 * sum(profit) / nullif(sum(revenue) FILTER (WHERE profit IS NOT NULL), 0)
                                                             AS margin_pct
        FROM sales
        GROUP BY product
        """
    )
    cur.execute(
        """
        CREATE OR REPLACE TABLE customers AS
        SELECT
            customer_id,
            min(order_date)                                        AS first_order,
            max(order_date)                                        AS last_order,
            count(DISTINCT order_id) FILTER (WHERE NOT is_return)  AS orders,
            sum(revenue)                                           AS revenue,
            sum(revenue) / nullif(count(DISTINCT order_id) FILTER (WHERE NOT is_return), 0)
                                                                   AS avg_order_value,
            -- measured from the latest sale in the data, so old datasets still make sense
            date_diff('day', max(order_date), (SELECT max(order_date) FROM sales))
                                                                   AS recency_days
        FROM sales
        WHERE customer_id IS NOT NULL
        GROUP BY customer_id
        """
    )


def _update_samples(cur: duckdb.DuckDBPyConnection) -> None:
    """Store a few example values per text column so the AI knows what's in them."""
    columns = cur.execute(
        "SELECT column_name FROM column_registry WHERE data_type = 'VARCHAR'"
    ).fetchall()
    for (name,) in columns:
        values = cur.execute(
            f'SELECT "{name}", count(*) AS n FROM sales WHERE "{name}" IS NOT NULL '
            f"GROUP BY 1 ORDER BY n DESC, 1 LIMIT {SAMPLE_VALUES}"
        ).fetchall()
        cur.execute(
            "UPDATE column_registry SET sample_values = ? WHERE column_name = ?",
            [json.dumps([v for v, _ in values]), name],
        )
