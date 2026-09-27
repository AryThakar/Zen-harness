"""Dashboard numbers: KPIs with period-over-period change, trends and breakdowns.

Plain functions over a DuckDB cursor so the API, the UI and (later) the AI
agent's tools all compute metrics the same way. Date ranges are inclusive days.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import duckdb

from app.ingestion.fields import FIELDS_BY_NAME

DEFAULT_DAYS = 90
GRAINS = ("day", "week", "month")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
NOT_SET = "(not set)"

# Optional columns whose absence locks dashboard numbers.
LOCKABLE = ("customer_id", "unit_cost", "category", "location", "channel", "order_id")


class InvalidQuery(ValueError):
    pass


@dataclass(frozen=True)
class Period:
    start: date
    end: date  # inclusive

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def previous(self) -> Period:
        return Period(self.start - timedelta(days=self.days), self.start - timedelta(days=1))

    def bounds(self) -> list:
        """[start, end + 1 day) for SQL."""
        return [self.start, self.end + timedelta(days=1)]


def data_range(cur: duckdb.DuckDBPyConnection) -> Period | None:
    first, last = cur.execute("SELECT min(order_date)::DATE, max(order_date)::DATE FROM sales").fetchone()
    return Period(first, last) if first else None


def resolve_period(
    cur: duckdb.DuckDBPyConnection, start: date | None = None, end: date | None = None
) -> Period | None:
    """Fill a missing start/end from the data: by default the last 90 days of data,
    so historical datasets still show something."""
    available = data_range(cur)
    if available is None:
        return None
    end = end or available.end
    start = start or max(available.start, end - timedelta(days=DEFAULT_DAYS - 1))
    if start > end:
        raise InvalidQuery("start must be on or before end")
    return Period(start, end)


def availability(cur: duckdb.DuckDBPyConnection) -> dict[str, bool]:
    """Which optional columns have any data at all."""
    row = cur.execute(
        """
        SELECT count(customer_id) > 0, count(unit_cost) > 0, count(category) > 0,
               count(location) > 0, count(channel) > 0,
               count(*) FILTER (WHERE NOT starts_with(order_id, 'AUTO-')) > 0
        FROM sales
        """
    ).fetchone()
    return dict(zip(LOCKABLE, row))


def locked_features(available: dict[str, bool]) -> dict[str, list[str]]:
    return {col: list(FIELDS_BY_NAME[col].unlocks) for col, ok in available.items() if not ok}


def _period_totals(cur: duckdb.DuckDBPyConnection, period: Period) -> dict[str, float | None]:
    row = cur.execute(
        """
        SELECT
            coalesce(sum(revenue), 0),
            count(DISTINCT order_id) FILTER (WHERE NOT is_return),
            coalesce(sum(quantity) FILTER (WHERE NOT is_return), 0),
            sum(profit),
            sum(revenue) FILTER (WHERE profit IS NOT NULL),
            count(DISTINCT customer_id),
            coalesce(-sum(revenue) FILTER (WHERE is_return), 0),
            coalesce(sum(revenue) FILTER (WHERE NOT is_return), 0)
        FROM sales WHERE order_date >= ? AND order_date < ?
        """,
        period.bounds(),
    ).fetchone()
    revenue, orders, units, profit, revenue_with_cost, customers, returned, gross = row
    new_customers = cur.execute(
        "SELECT count(*) FROM customers WHERE first_order >= ? AND first_order < ?", period.bounds()
    ).fetchone()[0]
    return {
        "revenue": revenue,
        "orders": orders,
        "avg_order_value": revenue / orders if orders else None,
        "units": units,
        "profit": profit,
        "margin_pct": 100 * profit / revenue_with_cost if profit is not None and revenue_with_cost else None,
        "customers": customers,
        "new_customers": new_customers,
        "return_rate_pct": 100 * returned / gross if gross else None,
    }


def comparison_coverage(data: Period, previous: Period) -> str:
    """'full' when the data covers the whole previous period, 'partial' when it starts
    inside it, 'none' when it starts after it. Only 'full' gives honest % changes -
    otherwise a half-covered previous year shows as +100% growth."""
    if previous.end < data.start:
        return "none"
    if previous.start < data.start:
        return "partial"
    return "full"


def summary(cur: duckdb.DuckDBPyConnection, period: Period) -> dict:
    """KPIs for ``period`` and the same-length period just before it."""
    available = availability(cur)
    current = _period_totals(cur, period)
    previous = _period_totals(cur, period.previous())
    coverage = comparison_coverage(data_range(cur), period.previous())
    hidden = set()
    if not available["unit_cost"]:
        hidden |= {"profit", "margin_pct"}
    if not available["customer_id"]:
        hidden |= {"customers", "new_customers"}

    kpis = {}
    for name, value in current.items():
        if name in hidden:
            kpis[name] = {"value": None, "previous": None, "change_pct": None}
            continue
        before = previous[name] if coverage != "none" else None
        change = None
        if coverage == "full" and value is not None and before:
            change = 100 * (value - before) / abs(before)
        kpis[name] = {"value": value, "previous": before, "change_pct": change}
    return {
        "period": period,
        "previous_period": period.previous(),
        "comparison": coverage,
        "kpis": kpis,
        "available": available,
        "locked": locked_features(available),
    }


def pick_grain(period: Period) -> str:
    if period.days <= 62:
        return "day"
    if period.days <= 366:
        return "week"
    return "month"


def trend(cur: duckdb.DuckDBPyConnection, period: Period, grain: str = "auto") -> dict:
    """Revenue, profit and orders per day/week/month, with empty buckets as zero."""
    if grain == "auto":
        grain = pick_grain(period)
    if grain not in GRAINS:
        raise InvalidQuery(f"grain must be one of {GRAINS} or 'auto'")
    rows = cur.execute(
        f"""
        WITH buckets AS (
            SELECT unnest(generate_series(
                date_trunc('{grain}', ?::TIMESTAMP), date_trunc('{grain}', ?::TIMESTAMP),
                INTERVAL 1 {grain})) AS bucket
        ),
        totals AS (
            SELECT date_trunc('{grain}', order_date) AS bucket,
                   sum(revenue) AS revenue, sum(profit) AS profit,
                   count(DISTINCT order_id) FILTER (WHERE NOT is_return) AS orders
            FROM sales WHERE order_date >= ? AND order_date < ?
            GROUP BY 1
        )
        SELECT b.bucket::DATE, coalesce(t.revenue, 0), t.profit, coalesce(t.orders, 0)
        FROM buckets b LEFT JOIN totals t USING (bucket)
        ORDER BY 1
        """,
        [period.start, period.end, *period.bounds()],
    ).fetchall()
    return {
        "grain": grain,
        "points": [
            {"bucket": b, "revenue": r, "profit": p, "orders": o,
             "partial": _is_partial(b, grain, period)}
            for b, r, p, o in rows
        ],
    }


def _is_partial(bucket: date, grain: str, period: Period) -> bool:
    """True when the bucket sticks out of the period - e.g. a week cut off by the end
    date - so its total is not comparable with full buckets."""
    if grain == "day":
        return False
    if grain == "week":
        last = bucket + timedelta(days=6)
    else:
        next_month = (bucket.replace(day=28) + timedelta(days=4)).replace(day=1)
        last = next_month - timedelta(days=1)
    return bucket < period.start or last > period.end


def breakdown_dimensions(cur: duckdb.DuckDBPyConnection) -> list[str]:
    """Columns that make sense to group by: text and yes/no columns, not IDs of single lines."""
    rows = cur.execute(
        """
        SELECT column_name FROM column_registry
        WHERE data_type IN ('VARCHAR', 'BOOLEAN') AND column_name <> 'order_id'
          AND (kind <> 'derived' OR column_name = 'weekday')
        ORDER BY CASE kind WHEN 'required' THEN 0 WHEN 'optional' THEN 1
                           WHEN 'derived' THEN 2 ELSE 3 END, column_name
        """
    ).fetchall()
    return [name for (name,) in rows]


def breakdown(
    cur: duckdb.DuckDBPyConnection, period: Period, by: str, limit: int = 10
) -> dict:
    """Top ``limit`` values of column ``by`` by revenue, plus everything else as 'other'."""
    if by not in breakdown_dimensions(cur):
        raise InvalidQuery(f"cannot break down by '{by}'")
    order = (
        "array_position(['" + "','".join(WEEKDAYS) + "'], label)" if by == "weekday"
        else "revenue DESC NULLS LAST, label"
    )
    rows = cur.execute(
        f"""
        SELECT coalesce(CAST("{by}" AS VARCHAR), '{NOT_SET}') AS label,
               sum(revenue) AS revenue, sum(profit) AS profit,
               coalesce(sum(quantity) FILTER (WHERE NOT is_return), 0) AS units,
               count(DISTINCT order_id) FILTER (WHERE NOT is_return) AS orders
        FROM sales WHERE order_date >= ? AND order_date < ?
        GROUP BY 1 ORDER BY {order}
        """,
        period.bounds(),
    ).fetchall()
    total = sum(r[1] for r in rows)
    items = [
        {"label": label, "revenue": revenue, "profit": profit, "units": units, "orders": orders,
         "share_pct": 100 * revenue / total if total else None}
        for label, revenue, profit, units, orders in rows
    ]
    top, rest = items[:limit], items[limit:]
    other = None
    if rest:
        other_revenue = sum(i["revenue"] for i in rest)
        other = {"label": f"Other ({len(rest)})", "revenue": other_revenue,
                 "share_pct": 100 * other_revenue / total if total else None}
    return {"by": by, "items": top, "other": other, "total_revenue": total}
