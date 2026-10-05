"""Dashboard endpoints: KPIs, trend and breakdowns for a date range."""

from __future__ import annotations

from datetime import date

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query

from legacy.analytics import dashboard as analytics

from .deps import get_cursor
from .schemas import BreakdownOut, PeriodOut, RangeOut, SummaryOut, TrendOut

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _period(cur: duckdb.DuckDBPyConnection, start: date | None, end: date | None) -> analytics.Period | None:
    try:
        return analytics.resolve_period(cur, start, end)
    except analytics.InvalidQuery as exc:
        raise HTTPException(422, str(exc)) from exc


def _out(period: analytics.Period | None) -> PeriodOut | None:
    return PeriodOut(start=period.start, end=period.end, days=period.days) if period else None


@router.get("/range", response_model=RangeOut)
def data_range(cur: duckdb.DuckDBPyConnection = Depends(get_cursor)) -> RangeOut:
    """First and last sale date - cheap, for building period pickers."""
    available = analytics.data_range(cur)
    if available is None:
        return RangeOut(has_data=False)
    return RangeOut(has_data=True, start=available.start, end=available.end)


@router.get("/dimensions", response_model=list[str])
def dimensions(cur: duckdb.DuckDBPyConnection = Depends(get_cursor)) -> list[str]:
    """Columns that /dashboard/breakdown accepts in `by`."""
    return analytics.breakdown_dimensions(cur)


@router.get("/summary", response_model=SummaryOut)
def summary(
    start: date | None = Query(None, description="default: 90 days before the latest sale"),
    end: date | None = Query(None, description="default: the latest sale"),
    cur: duckdb.DuckDBPyConnection = Depends(get_cursor),
) -> SummaryOut:
    """Headline numbers for the period, compared with the same-length period before."""
    period = _period(cur, start, end)
    if period is None:
        return SummaryOut(has_data=False)
    result = analytics.summary(cur, period)
    return SummaryOut(
        has_data=True,
        period=_out(period),
        previous_period=_out(result["previous_period"]),
        comparison=result["comparison"],
        data_range=_out(analytics.data_range(cur)),
        kpis=result["kpis"],
        available=result["available"],
        locked=result["locked"],
    )


@router.get("/trend", response_model=TrendOut)
def trend(
    start: date | None = None,
    end: date | None = None,
    grain: str = Query("auto", pattern="^(auto|day|week|month)$"),
    cur: duckdb.DuckDBPyConnection = Depends(get_cursor),
) -> TrendOut:
    period = _period(cur, start, end)
    if period is None:
        return TrendOut(grain=None, period=None, points=[])
    result = analytics.trend(cur, period, grain)
    return TrendOut(grain=result["grain"], period=_out(period), points=result["points"])


@router.get("/breakdown", response_model=BreakdownOut)
def breakdown(
    by: str = Query("category", description="any text column, e.g. product, location, payment_mode"),
    start: date | None = None,
    end: date | None = None,
    limit: int = Query(10, ge=1, le=100),
    cur: duckdb.DuckDBPyConnection = Depends(get_cursor),
) -> BreakdownOut:
    period = _period(cur, start, end)
    dimensions = analytics.breakdown_dimensions(cur)
    if period is None:
        return BreakdownOut(by=by, period=None, dimensions=dimensions, items=[], other=None, total_revenue=0)
    try:
        result = analytics.breakdown(cur, period, by, limit)
    except analytics.InvalidQuery as exc:
        raise HTTPException(422, str(exc)) from exc
    return BreakdownOut(period=_out(period), dimensions=dimensions, **result)
