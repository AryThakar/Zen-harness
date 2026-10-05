"""The legacy product's SQL guard: app.tools.sql_guard over its database's tables."""

from __future__ import annotations

from app.tools import sql_guard as _guard
from app.tools.sql_guard import MAX_ROWS, TIMEOUT_SECONDS, QueryResult, SqlRejected  # noqa: F401 - its API

ALLOWED_TABLES = {
    "sales", "products", "customers", "uploads", "column_registry",
    "ml_runs", "forecast_points", "customer_segments", "anomaly_days", "anomaly_lines",
    "latest_forecast", "latest_customer_segments", "latest_anomaly_days",
    "churn_scores", "basket_rules", "latest_churn_scores", "latest_basket_rules", "documents",
}


def check(sql: str, existing: frozenset[str] = frozenset()) -> str:
    return _guard.check(sql, existing, allowed=ALLOWED_TABLES)


def run(cur, sql: str, max_rows: int = MAX_ROWS, timeout: float = TIMEOUT_SECONDS) -> QueryResult:
    return _guard.run(cur, sql, max_rows, timeout, allowed=ALLOWED_TABLES)
