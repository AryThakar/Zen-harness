"""ML endpoints: start runs (they work in the background), check them, read results."""

from __future__ import annotations

import duckdb
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status

from app.db.connection import Database
from app.ml import results, runner

from .deps import get_cursor, get_db
from .schemas import (
    AnomaliesOut, BasketOut, ChurnCustomersPage, ChurnIn, ChurnOut, ForecastIn, ForecastOut, RunOut,
    SegmentCustomersPage, SegmentsOut,
)

router = APIRouter(prefix="/ml", tags=["ml"])


def _start(db: Database, background: BackgroundTasks, task: str, params: dict) -> RunOut:
    run_id, created = runner.start(db, task, params)
    if created:
        background.add_task(runner.execute, db, run_id, task, params)
    cur = db.cursor()
    try:
        return RunOut(**results.get_run(cur, run_id))
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


@router.post("/churn", response_model=RunOut, status_code=status.HTTP_202_ACCEPTED)
def start_churn(background: BackgroundTasks, body: ChurnIn | None = None, db: Database = Depends(get_db)) -> RunOut:
    """Predict which regular customers will buy nothing in the next 30, 60 or 90 days."""
    return _start(db, background, "churn", (body or ChurnIn()).model_dump())


@router.post("/basket", response_model=RunOut, status_code=status.HTTP_202_ACCEPTED)
def start_basket(background: BackgroundTasks, db: Database = Depends(get_db)) -> RunOut:
    """Find products (and categories) bought together more often than chance."""
    return _start(db, background, "basket", {})


@router.get("/runs", response_model=list[RunOut])
def list_runs(
    task: str | None = Query(None, pattern="^(forecast|segments|anomalies|churn|basket)$"),
    limit: int = Query(20, ge=1, le=200),
    cur: duckdb.DuckDBPyConnection = Depends(get_cursor),
) -> list[RunOut]:
    return [RunOut(**r) for r in results.list_runs(cur, task, limit)]


@router.get("/runs/{run_id}", response_model=RunOut)
def get_run(run_id: int, cur: duckdb.DuckDBPyConnection = Depends(get_cursor)) -> RunOut:
    run = results.get_run(cur, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Run {run_id} not found.")
    return RunOut(**run)


@router.get("/forecast", response_model=ForecastOut)
def forecast_result(
    run_id: int | None = None, cur: duckdb.DuckDBPyConnection = Depends(get_cursor)
) -> ForecastOut:
    """A forecast with recent actuals before it (latest successful run by default)."""
    return ForecastOut(**results.forecast_view(cur, _result_run(cur, "forecast", run_id)))


@router.get("/segments", response_model=SegmentsOut)
def segments_result(
    run_id: int | None = None, cur: duckdb.DuckDBPyConnection = Depends(get_cursor)
) -> SegmentsOut:
    """Segment sizes, typical customer, revenue share, and what to do about each."""
    return SegmentsOut(**results.segments_view(cur, _result_run(cur, "segments", run_id)))


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
    return SegmentCustomersPage(**results.segment_customers(cur, run, segment, limit, offset))


@router.get("/anomalies", response_model=AnomaliesOut)
def anomalies_result(
    run_id: int | None = None, cur: duckdb.DuckDBPyConnection = Depends(get_cursor)
) -> AnomaliesOut:
    """Unusual days and lines, plus the daily series they were found in."""
    return AnomaliesOut(**results.anomalies_view(cur, _result_run(cur, "anomalies", run_id)))


@router.get("/churn", response_model=ChurnOut)
def churn_result(
    run_id: int | None = None, cur: duckdb.DuckDBPyConnection = Depends(get_cursor)
) -> ChurnOut:
    """Risk bands and the customers with most value at risk."""
    return ChurnOut(**results.churn_view(cur, _result_run(cur, "churn", run_id)))


@router.get("/churn/customers", response_model=ChurnCustomersPage)
def churn_customers(
    risk: str | None = Query(None, pattern="^(high|medium|low)$"),
    run_id: int | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    cur: duckdb.DuckDBPyConnection = Depends(get_cursor),
) -> ChurnCustomersPage:
    """Scored customers (optionally one risk band), most value at risk first."""
    run = _result_run(cur, "churn", run_id)
    return ChurnCustomersPage(**results.churn_customers(cur, run, risk, limit, offset))


@router.get("/basket", response_model=BasketOut)
def basket_result(
    level: str = Query("product", pattern="^(product|category)$"),
    item: str | None = Query(None, max_length=200, description="only what is bought with this product/category"),
    limit: int = Query(30, ge=1, le=500),
    run_id: int | None = None,
    cur: duckdb.DuckDBPyConnection = Depends(get_cursor),
) -> BasketOut:
    """Pairs bought together more than chance, strongest first."""
    return BasketOut(**results.basket_view(cur, _result_run(cur, "basket", run_id), level, item, limit))


def _result_run(cur: duckdb.DuckDBPyConnection, task: str, run_id: int | None) -> dict:
    try:
        return results.result_run(cur, task, run_id)
    except results.NoResult as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except results.RunNotReady as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
