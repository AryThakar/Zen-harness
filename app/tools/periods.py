"""Time periods shared by make_chart and forecast: bucketing dates, and leaving out partial edges.

A sum over a month that has only 10 days of data is not a 'drop'. For sums and counts, a first or
last period the data covers only partly is left out, with a note saying so.
split_time: time kept as a year column plus a quarter or month column (no date column) - the SQL
that makes a date, for inspect_data and forecast to hand the model.
"""

from __future__ import annotations

import re

import numpy as np
import pandas as pd

CODE = {"day": "D", "week": "W", "month": "M", "quarter": "Q", "year": "Y"}
WORD = {v: k for k, v in CODE.items()}
OFFSET = {"D": "D", "W": "W-MON", "M": "MS", "Q": "QS", "Y": "YS"}  # period starts; weeks start on Monday


ORDER = "DWMQY"  # finest to coarsest


def native_freq(dates: pd.Series) -> str:
    """The data's own grain, from the usual gap between its distinct dates (monthly data dated the
    1st -> 'M'). An automatic period is never finer: weeks over monthly data are mostly empty."""
    days = pd.Series(dates.dropna().dt.normalize().unique()).sort_values()
    gap = days.diff().dt.days.median() if len(days) > 1 else 1
    return "D" if gap <= 1.5 else "W" if gap <= 8 else "M" if gap <= 35 else "Q" if gap <= 100 else "Y"


def coarsest(*freqs: str) -> str:
    return max(freqs, key=ORDER.index)


def bucket(dates: pd.Series, freq: str) -> pd.Series:
    """The start of each date's period."""
    return dates.dt.to_period(freq).dt.start_time


def full_range(index: pd.Index, freq: str) -> pd.DatetimeIndex:
    """Every period start from the first to the last, so empty periods show as gaps or zeros."""
    return pd.date_range(index.min(), index.max(), freq=OFFSET[freq])


COVERED = 0.9  # a period with data on less than 90% of its days is partial (a month from the 2nd is not)


def drop_partial_edges(y: pd.Series, dates: pd.Series, freq: str) -> tuple[pd.Series, list[str]]:
    """Leave out a first / last period that the data's dates cover only partly - unless each row
    already stands for a whole period or more (monthly data dated the 1st, charted by month)."""
    notes = []
    if freq == "D" or len(y) <= 2:
        return y, notes
    step = pd.tseries.frequencies.to_offset(OFFSET[freq])
    distinct = pd.Series(dates.dropna().dt.normalize().unique()).sort_values()
    period_days = ((pd.Timestamp(y.index[0]) + step) - pd.Timestamp(y.index[0])).days
    if len(distinct) > 1 and distinct.diff().dt.days.median() >= COVERED * period_days:
        return y, notes
    first, last = distinct.iloc[0], distinct.iloc[-1]

    def days(start: pd.Timestamp) -> int:
        return ((start + step) - start).days

    start = pd.Timestamp(y.index[-1])
    if ((last.normalize() - start).days + 1) / days(start) < COVERED:
        notes.append(f"The last {WORD[freq]} ({start:%Y-%m-%d}) is incomplete (data ends {last:%Y-%m-%d}), "
                     "so it was left out.")
        y = y.iloc[:-1]
    start = pd.Timestamp(y.index[0])
    end = start + step - np.timedelta64(1, "D")
    if ((end - first.normalize()).days + 1) / days(start) < COVERED:
        notes.append(f"The first {WORD[freq]} ({start:%Y-%m-%d}) is incomplete (data starts {first:%Y-%m-%d}), "
                     "so it was left out.")
        y = y.iloc[1:]
    return y, notes


MONTH_NAMES = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def month_numbers(s: pd.Series) -> pd.Series:
    """1-12 from month numbers or names ('3', 'March', 'Mar'); missing where neither."""
    numbers = pd.to_numeric(s, errors="coerce")
    names = s.astype(str).str.strip().str[:3].str.lower().map({m: i for i, m in enumerate(MONTH_NAMES, 1)})
    return numbers.where(numbers.notna(), names)


def split_time(df: pd.DataFrame) -> tuple[str, str, str] | None:
    """(year column, quarter or month column, 'quarter' | 'month') when time is split that way
    and the table has no date column; None otherwise. Months may be numbers or names."""
    if any(pd.api.types.is_datetime64_any_dtype(df[c]) for c in df.columns):
        return None
    names = {c: re.sub(r"[^0-9a-z]+", "_", str(c).lower()).strip("_") for c in df.columns}
    year = next((c for c, n in names.items()
                 if n in ("year", "yr", "fiscal_year") and _whole(df[c], 1800, 2200)), None)
    if year is None:
        return None
    for c, n in names.items():
        if n in ("quarter", "qtr") and _whole(df[c], 1, 4):
            return year, c, "quarter"
        if n in ("month", "mon", "mth") and _whole(month_numbers(df[c]), 1, 12):
            return year, c, "month"
    return None


def split_dates(df: pd.DataFrame, year: str, part: str, kind: str) -> pd.Series:
    """The first day of each row's quarter or month."""
    months = (pd.to_numeric(df[part], errors="coerce") - 1) * 3 + 1 if kind == "quarter" else month_numbers(df[part])
    return pd.to_datetime(pd.DataFrame({"year": pd.to_numeric(df[year], errors="coerce"), "month": months, "day": 1}),
                          errors="coerce")


def split_time_note(df: pd.DataFrame, table: str) -> str | None:
    found = split_time(df)
    if not found:
        return None
    year, part, kind = found
    named = kind == "month" and pd.to_numeric(df[part], errors="coerce").isna().all()
    if named:  # month names: DuckDB reads them with strptime (%B January, %b Jan)
        form = "%b" if df[part].dropna().astype(str).str.strip().str.len().max() <= 3 else "%B"
        date = f"CAST(strptime(CAST(\"{year}\" AS VARCHAR) || ' ' || \"{part}\", '%Y {form}') AS DATE)"
    else:
        month = f'("{part}" - 1) * 3 + 1' if kind == "quarter" else f'"{part}"'
        date = f"make_date(CAST(\"{year}\" AS INTEGER), CAST({month} AS INTEGER), 1)"
    return (f"Time is split into '{year}' and '{part}' columns, with no date column. forecast and find_patterns combine "
            f"them themselves (no date needed). To chart over time, first make a date column with query_data and "
            f"save=true: SELECT *, {date} AS date FROM {table}")


def _whole(s: pd.Series, low: int, high: int) -> bool:
    v = pd.to_numeric(s, errors="coerce").dropna()
    return len(v) > 0 and bool((v % 1 == 0).all()) and bool(v.between(low, high).all())
