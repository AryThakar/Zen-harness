"""Read stored ML results. Shared by the API and the AI assistant's tools."""

from __future__ import annotations

import json

import duckdb

from .base import daily_orders, daily_revenue
from .forecast import full_weeks
from .segments import SEGMENTS

RUN_COLUMNS = ("run_id", "task", "status", "params", "metrics", "message", "started_at", "finished_at")


class NoResult(LookupError):
    """No run matches; the message says why."""


class RunNotReady(LookupError):
    """The run exists but didn't succeed (or is still running)."""


def get_run(cur: duckdb.DuckDBPyConnection, run_id: int) -> dict | None:
    row = cur.execute(f"SELECT {', '.join(RUN_COLUMNS)} FROM ml_runs WHERE run_id = ?", [run_id]).fetchone()
    return _run(row) if row else None


def list_runs(cur: duckdb.DuckDBPyConnection, task: str | None = None, limit: int = 20) -> list[dict]:
    where, params = ("WHERE task = ?", [task]) if task else ("", [])
    rows = cur.execute(
        f"SELECT {', '.join(RUN_COLUMNS)} FROM ml_runs {where} ORDER BY run_id DESC LIMIT ?", [*params, limit]
    ).fetchall()
    return [_run(r) for r in rows]


def result_run(cur: duckdb.DuckDBPyConnection, task: str, run_id: int | None = None) -> dict:
    """The requested run, or the latest successful one of ``task``."""
    if run_id is None:
        row = cur.execute(
            f"SELECT {', '.join(RUN_COLUMNS)} FROM ml_runs WHERE task = ? AND status = 'succeeded' "
            "ORDER BY run_id DESC LIMIT 1",
            [task],
        ).fetchone()
        if not row:
            raise NoResult(f"No {task} results yet; start a run first.")
        return _run(row)
    run = get_run(cur, run_id)
    if run is None or run["task"] != task:
        raise NoResult(f"Run {run_id} is not a {task} run." if run else f"Run {run_id} not found.")
    if run["status"] != "succeeded":
        raise RunNotReady(f"Run {run_id} is {run['status']}: {run['message'] or ''}".strip())
    return run


def forecast_view(cur: duckdb.DuckDBPyConnection, run: dict) -> dict:
    """The forecast points plus recent actuals before them."""
    points = cur.execute(
        "SELECT bucket, predicted, lower, upper FROM forecast_points WHERE run_id = ? ORDER BY bucket",
        [run["run_id"]],
    ).fetchall()
    p = run["params"]
    actual = daily_revenue(cur, p.get("by"), p.get("value"))
    if p.get("grain") == "week":
        actual = full_weeks(actual).tail(max(26, 3 * p["horizon"]))
    else:
        actual = actual.tail(max(90, 3 * p["horizon"]))
    return {
        "run": run,
        "history": [{"bucket": d.date(), "actual": float(v)} for d, v in actual.items()],
        "points": [{"bucket": b, "predicted": v, "lower": lo, "upper": hi} for b, v, lo, hi in points],
    }


def segments_view(cur: duckdb.DuckDBPyConnection, run: dict) -> dict:
    rows = cur.execute(
        """
        SELECT segment, count(*) AS customers, sum(revenue) AS revenue,
               median(recency_days), median(orders), median(revenue)
        FROM customer_segments WHERE run_id = ?
        GROUP BY segment ORDER BY revenue DESC
        """,
        [run["run_id"]],
    ).fetchall()
    total_customers = sum(r[1] for r in rows)
    total_revenue = sum(r[2] for r in rows)
    return {"run": run, "segments": [
        {
            "segment": seg, "customers": n, "share_pct": 100 * n / total_customers, "revenue": revenue,
            "revenue_share_pct": 100 * revenue / total_revenue if total_revenue else None,
            "median_recency_days": recency, "median_orders": orders, "median_revenue": med_revenue,
            "meaning": SEGMENTS[seg][0], "action": SEGMENTS[seg][1],
        }
        for seg, n, revenue, recency, orders, med_revenue in rows
    ]}


def segment_customers(cur: duckdb.DuckDBPyConnection, run: dict, segment: str | None,
                      limit: int, offset: int = 0) -> dict:
    where, params = "WHERE run_id = ?", [run["run_id"]]
    if segment:
        where += " AND segment = ?"
        params.append(segment)
    total = cur.execute(f"SELECT count(*) FROM customer_segments {where}", params).fetchone()[0]
    rows = cur.execute(
        f"SELECT customer_id, segment, recency_days, orders, revenue FROM customer_segments {where} "
        "ORDER BY revenue DESC, customer_id LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    keys = ("customer_id", "segment", "recency_days", "orders", "revenue")
    return {"total": total, "rows": [dict(zip(keys, r)) for r in rows]}


def anomalies_view(cur: duckdb.DuckDBPyConnection, run: dict, with_series: bool = True) -> dict:
    day_keys = ("day", "revenue", "expected_revenue", "orders", "expected_orders", "signal", "score", "direction")
    days = cur.execute(
        f"SELECT {', '.join(day_keys)} FROM anomaly_days WHERE run_id = ? ORDER BY day", [run["run_id"]]
    ).fetchall()
    line_keys = ("sale_id", "order_id", "order_date", "product", "quantity", "unit_price", "discount",
                 "score", "reason")
    lines = cur.execute(
        """
        SELECT a.sale_id, s.order_id, s.order_date, s.product, s.quantity, s.unit_price, s.discount,
               a.score, a.reason
        FROM anomaly_lines a JOIN sales s USING (sale_id)
        WHERE a.run_id = ? ORDER BY a.score DESC
        """,
        [run["run_id"]],
    ).fetchall()
    view = {"run": run, "days": [dict(zip(day_keys, r)) for r in days],
            "lines": [dict(zip(line_keys, r)) for r in lines], "series": []}
    if with_series:
        revenue = daily_revenue(cur)
        orders = daily_orders(cur).reindex(revenue.index, fill_value=0.0)
        view["series"] = [{"day": d.date(), "revenue": float(revenue[d]), "orders": int(orders[d])}
                          for d in revenue.index]
    return view


CHURN_KEYS = ("customer_id", "probability", "risk", "recency_days", "typical_gap_days", "purchases",
              "recent_revenue", "revenue", "value_at_risk", "signals")


def churn_view(cur: duckdb.DuckDBPyConnection, run: dict, limit: int = 20) -> dict:
    """Customers and value at risk per band, and the customers with most at stake."""
    bands = cur.execute(
        """
        SELECT risk, count(*), sum(value_at_risk), avg(probability) FROM churn_scores WHERE run_id = ?
        GROUP BY risk ORDER BY CASE risk WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END
        """,
        [run["run_id"]],
    ).fetchall()
    return {"run": run,
            "bands": [dict(zip(("risk", "customers", "value_at_risk", "avg_probability"), b)) for b in bands],
            "customers": churn_customers(cur, run, None, limit)["rows"]}


def churn_customers(cur: duckdb.DuckDBPyConnection, run: dict, risk: str | None, limit: int,
                    offset: int = 0) -> dict:
    where, params = "WHERE run_id = ?", [run["run_id"]]
    if risk:
        where += " AND risk = ?"
        params.append(risk)
    total = cur.execute(f"SELECT count(*) FROM churn_scores {where}", params).fetchone()[0]
    rows = cur.execute(
        f"SELECT {', '.join(CHURN_KEYS)} FROM churn_scores {where} "
        "ORDER BY value_at_risk DESC, customer_id LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return {"total": total, "rows": [dict(zip(CHURN_KEYS, r)) for r in rows]}


BASKET_KEYS = ("level", "antecedent", "consequent", "orders_together", "antecedent_orders", "expected_together",
               "support_pct", "confidence_pct", "lift")


def basket_view(cur: duckdb.DuckDBPyConnection, run: dict, level: str = "product", item: str | None = None,
                limit: int = 30) -> dict:
    """Pairs bought together, strongest first. With ``item``: what goes with it
    (matched case-insensitively), most common companion first."""
    where, params, order = "WHERE run_id = ? AND level = ?", [run["run_id"], level], "lift DESC, orders_together DESC"
    if item:
        where += " AND lower(antecedent) = lower(?)"
        params.append(item)
        order = "confidence_pct DESC, lift DESC"
    rows = cur.execute(
        f"SELECT {', '.join(BASKET_KEYS)} FROM basket_rules {where} ORDER BY {order} LIMIT ?", [*params, limit]
    ).fetchall()
    return {"run": run, "item": item, "rules": [dict(zip(BASKET_KEYS, r)) for r in rows]}


def _run(row: tuple) -> dict:
    run = dict(zip(RUN_COLUMNS, row))
    run["params"] = json.loads(run["params"])
    run["metrics"] = json.loads(run["metrics"]) if run["metrics"] else None
    return run
