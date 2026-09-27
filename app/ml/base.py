"""Shared pieces of the ML tasks."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import duckdb
import pandas as pd


class NotEnoughData(ValueError):
    """The data can't support this model; the message says what's missing."""


@dataclass
class TaskResult:
    """What a task returns: numbers for the UI/agent, a plain-language summary, and
    result rows to store. Computing never touches the database's write side."""

    metrics: dict
    message: str
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)  # table name -> rows (without run_id)

    def save(self, cur: duckdb.DuckDBPyConnection, run_id: int) -> None:
        for table, rows in self.tables.items():
            if rows.empty:
                continue
            staged = rows.assign(run_id=run_id)
            cur.register("staged_rows", staged)
            try:
                columns = ", ".join(f'"{c}"' for c in staged.columns)
                cur.execute(f"INSERT INTO {table} ({columns}) SELECT {columns} FROM staged_rows")
            finally:
                cur.unregister("staged_rows")


def daily_revenue(
    cur: duckdb.DuckDBPyConnection, column: str | None = None, value: str | None = None
) -> pd.Series:
    """Net revenue per calendar day from the first to the last sale, days without
    sales as 0. ``column``/``value`` restrict it, e.g. to one category."""
    where, params = "", []
    if column is not None:
        where, params = f'WHERE "{column}" = ?', [value]
    rows = cur.execute(
        f"SELECT order_date::DATE AS day, sum(revenue) FROM sales {where} GROUP BY 1 ORDER BY 1", params
    ).fetchall()
    if not rows:
        return pd.Series(dtype=float)
    series = pd.Series({pd.Timestamp(d): float(r) for d, r in rows})
    return series.asfreq("D", fill_value=0.0)


def daily_orders(cur: duckdb.DuckDBPyConnection) -> pd.Series:
    """Orders per calendar day (returns excluded), days without orders as 0."""
    rows = cur.execute(
        "SELECT order_date::DATE, count(DISTINCT order_id) FROM sales WHERE NOT is_return GROUP BY 1 ORDER BY 1"
    ).fetchall()
    if not rows:
        return pd.Series(dtype=float)
    return pd.Series({pd.Timestamp(d): float(n) for d, n in rows}).asfreq("D", fill_value=0.0)


def latest_sale_date(cur: duckdb.DuckDBPyConnection) -> date | None:
    return cur.execute("SELECT max(order_date)::DATE FROM sales").fetchone()[0]
