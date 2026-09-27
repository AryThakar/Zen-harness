"""ML endpoints: start runs (they work in the background), check them, read results."""

from __future__ import annotations

import json

import duckdb
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status

from app.db.connection import Database
from app.ml import runner
from app.ml.base import daily_orders, daily_revenue
from app.ml.forecast import full_weeks
from app.ml.segments import SEGMENTS

from .deps import get_cursor, get_db
from .schemas import (
    AnomaliesOut, ForecastIn, ForecastOut, RunOut, SegmentCustomersPage, SegmentsOut,
)

router = APIRouter(prefix="/ml", tags=["ml"])

RUN_COLUMNS = "run_id, task, status, params, metrics, message, started_at, finished_at"


def _start(db: Database, background: BackgroundTasks, task: str, params: dict) -> RunOut:
    run_id, created = runner.start(db, task, params)
    if created:
        background.add_task(runner.execute, db, run_id, task, params)
    cur = db.cursor()
    try:
        return _run(cur, run_id)
    finally:
        cur.close()


@router.post("/forecast", response_model=RunOut, status_code=status.HTTP_202_ACCEPTED)
def start_forecast(body: ForecastIn, background: BackgroundTasks, db: Database = Depends(get_db)) -> RunOut:
    """Forecast revenue (all sales, or one category / product / location / channel)."""
    return _start(db, background, "forecast", body.model_dump())


@router.post("/segments", response_model=RunOut, status_code=status.HTTP_202_ACCEPTED)
def start_segments(background: BackgroundTasks, db: Database = Depends(get_db)) -> RunOut:
    """Group customers by recency, frequency and spend."""
    return _start(db, background, "segments", {})


@router.post("/anomalies", response_model=RunOut, status_code=status.HTTP_202_ACCEPTED)
def start_anomalies(background: BackgroundTasks, db: Database = Depends(get_db)) -> RunOut:
    """Find unusual days and unusual sale lines."""
    return _start(db, background, "anomalies", {})


@router.get("/runs", response_model=list[RunOut])
def list_runs(
    task: str | None = Query(None, pattern="^(forecast|segments|anomalies)$"),
    limit: int = Query(20, ge=1, le=200),
    cur: duckdb.DuckDBPyConnection = Depends(get_cursor),
) -> list[RunOut]:
    where, params = ("WHERE task = ?", [task]) if task else ("", [])
    rows = cur.execute(
        f"SELECT {RUN_COLUMNS} FROM ml_runs {where} ORDER BY run_id DESC LIMIT ?", [*params, limit]
    ).fetchall()
    return [_run_out(r) for r in rows]


@router.get("/runs/{run_id}", response_model=RunOut)
def get_run(run_id: int, cur: duckdb.DuckDBPyConnection = Depends(get_cursor)) -> RunOut:
    return _run(cur, run_id)


@router.get("/forecast", response_model=ForecastOut)
def forecast_result(
    run_id: int | None = None, cur: duckdb.DuckDBPyConnection = Depends(get_cursor)
) -> ForecastOut:
    """A forecast with recent actuals before it (latest successful run by default)."""
    run = _result_run(cur, "forecast", run_id)
    points = cur.execute(
        "SELECT bucket, predicted, lower, upper FROM forecast_points WHERE run_id = ? ORDER BY bucket",
        [run.run_id],
    ).fetchall()
    p = run.params
    actual = daily_revenue(cur, p.get("by"), p.get("value"))
    if p.get("grain") == "week":
        actual = full_weeks(actual).tail(max(26, 3 * p["horizon"]))
    else:
        actual = actual.tail(max(90, 3 * p["horizon"]))
    return ForecastOut(
        run=run,
        history=[{"bucket": d.date(), "actual": v} for d, v in actual.items()],
        points=[{"bucket": b, "predicted": v, "lower": lo, "upper": hi} for b, v, lo, hi in points],
    )


@router.get("/segments", response_model=SegmentsOut)
def segments_result(
    run_id: int | None = None, cur: duckdb.DuckDBPyConnection = Depends(get_cursor)
) -> SegmentsOut:
    """Segment sizes, typical customer, revenue share, and what to do about each."""
    run = _result_run(cur, "segments", run_id)
    rows = cur.execute(
        """
        SELECT segment, count(*) AS customers, sum(revenue) AS revenue,
               median(recency_days), median(orders), median(revenue)
        FROM customer_segments WHERE run_id = ?
        GROUP BY segment ORDER BY revenue DESC
        """,
        [run.run_id],
    ).fetchall()
    total_customers = sum(r[1] for r in rows)
    total_revenue = sum(r[2] for r in rows)
    return SegmentsOut(run=run, segments=[
        {
            "segment": seg, "customers": n, "share_pct": 100 * n / total_customers, "revenue": revenue,
            "revenue_share_pct": 100 * revenue / total_revenue if total_revenue else None,
            "median_recency_days": recency, "median_orders": orders, "median_revenue": med_revenue,
            "meaning": SEGMENTS[seg][0], "action": SEGMENTS[seg][1],
        }
        for seg, n, revenue, recency, orders, med_revenue in rows
    ])


@router.get("/segments/customers", response_model=SegmentCustomersPage)
def segment_customers(
    segment: str | None = None,
    run_id: int | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    cur: duckdb.DuckDBPyConnection = Depends(get_cursor),
) -> SegmentCustomersPage:
    """Customers of one segment (or all), highest revenue first."""
    run = _result_run(cur, "segments", run_id)
    where, params = "WHERE run_id = ?", [run.run_id]
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
    return SegmentCustomersPage(total=total, rows=[dict(zip(keys, r)) for r in rows])


@router.get("/anomalies", response_model=AnomaliesOut)
def anomalies_result(
    run_id: int | None = None, cur: duckdb.DuckDBPyConnection = Depends(get_cursor)
) -> AnomaliesOut:
    """Unusual days and lines, plus the daily series they were found in."""
    run = _result_run(cur, "anomalies", run_id)
    day_keys = ("day", "revenue", "expected_revenue", "orders", "expected_orders", "signal", "score", "direction")
    days = cur.execute(
        f"SELECT {', '.join(day_keys)} FROM anomaly_days WHERE run_id = ? ORDER BY day", [run.run_id]
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
        [run.run_id],
    ).fetchall()
    revenue = daily_revenue(cur)
    orders = daily_orders(cur).reindex(revenue.index, fill_value=0.0)
    return AnomaliesOut(
        run=run,
        days=[dict(zip(day_keys, r)) for r in days],
        lines=[dict(zip(line_keys, r)) for r in lines],
        series=[{"day": d.date(), "revenue": revenue[d], "orders": int(orders[d])} for d in revenue.index],
    )


def _run(cur: duckdb.DuckDBPyConnection, run_id: int) -> RunOut:
    row = cur.execute(f"SELECT {RUN_COLUMNS} FROM ml_runs WHERE run_id = ?", [run_id]).fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Run {run_id} not found.")
    return _run_out(row)


def _result_run(cur: duckdb.DuckDBPyConnection, task: str, run_id: int | None) -> RunOut:
    """The requested run, or the latest successful one; its results must exist."""
    if run_id is None:
        row = cur.execute(
            f"SELECT {RUN_COLUMNS} FROM ml_runs WHERE task = ? AND status = 'succeeded' "
            "ORDER BY run_id DESC LIMIT 1",
            [task],
        ).fetchone()
        if not row:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No {task} results yet; start a run first.")
        return _run_out(row)
    run = _run(cur, run_id)
    if run.task != task:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Run {run_id} is a {run.task} run.")
    if run.status != "succeeded":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Run {run_id} is {run.status}: {run.message or ''}".strip())
    return run


def _run_out(row: tuple) -> RunOut:
    run_id, task, run_status, params, metrics, message, started, finished = row
    return RunOut(
        run_id=run_id, task=task, status=run_status, params=json.loads(params),
        metrics=json.loads(metrics) if metrics else None, message=message,
        started_at=started, finished_at=finished,
    )

