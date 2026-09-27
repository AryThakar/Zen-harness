"""What the assistant knows about the data: a compact schema card."""

from __future__ import annotations

import duckdb

from app.analytics import dashboard as analytics
from app.db import registry

TABLES = {
    "sales": "one row per product sold on an order (the main table)",
    "products": "view: totals per product (units_sold, units_returned, revenue, profit, margin_pct)",
    "customers": "view: per customer (first_order, last_order, orders, revenue, avg_order_value, recency_days)",
    "uploads": "upload history (filename, rows_loaded, created_at)",
    "latest_forecast": "latest forecast (bucket, predicted, lower, upper) - may be empty",
    "latest_customer_segments": "latest segments (customer_id, recency_days, orders, revenue, segment) - may be empty",
    "latest_anomaly_days": "latest unusual days (day, revenue, expected_revenue, orders, expected_orders, "
                           "signal, direction) - may be empty",
}


def schema_card(cur: duckdb.DuckDBPyConnection) -> str:
    """Tables, the sales columns with meanings and examples, data range, locked features."""
    lines = ["Tables:"]
    lines += [f"- {name}: {desc}" for name, desc in TABLES.items()]
    lines.append("")
    lines.append("sales columns (name TYPE - meaning; examples):")
    for c in registry.columns(cur):
        examples = ", ".join(str(v) for v in c["sample_values"][:4])
        meaning = c["description"] or ("custom column" if c["kind"] == "custom" else "")
        lines.append(f"- {c['column_name']} {c['data_type']} - {meaning}" + (f"; e.g. {examples}" if examples else ""))

    period = analytics.data_range(cur)
    rows = cur.execute("SELECT count(*) FROM sales").fetchone()[0]
    lines.append("")
    if period:
        lines.append(f"Data: {rows:,} sales lines from {period.start} to {period.end}.")
        locked = analytics.locked_features(analytics.availability(cur))
        if locked:
            missing = "; ".join(f"{col} (needed for {', '.join(f)})" for col, f in locked.items())
            lines.append(f"Columns with no data yet: {missing}.")
    else:
        lines.append("Data: no sales loaded yet - the user should upload a file or load demo data.")
    return "\n".join(lines)
