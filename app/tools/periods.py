"""Time periods shared by make_chart and forecast: bucketing dates, and leaving out partial edges.

A sum over a month that has only 10 days of data is not a 'drop'. For sums and counts, a first or
last period the data covers only partly is left out, with a note saying so.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

CODE = {"day": "D", "week": "W", "month": "M", "quarter": "Q", "year": "Y"}
WORD = {v: k for k, v in CODE.items()}
OFFSET = {"D": "D", "W": "W-MON", "M": "MS", "Q": "QS", "Y": "YS"}  # period starts; weeks start on Monday


def bucket(dates: pd.Series, freq: str) -> pd.Series:
    """The start of each date's period."""
    return dates.dt.to_period(freq).dt.start_time


def full_range(index: pd.Index, freq: str) -> pd.DatetimeIndex:
    """Every period start from the first to the last, so empty periods show as gaps or zeros."""
    return pd.date_range(index.min(), index.max(), freq=OFFSET[freq])


COVERED = 0.9  # a period with data on less than 90% of its days is partial (a month from the 2nd is not)


def drop_partial_edges(y: pd.Series, first: pd.Timestamp, last: pd.Timestamp, freq: str) -> tuple[pd.Series, list[str]]:
    """Leave out a first / last period that the data (first..last date) covers only partly."""
    notes = []
    if freq == "D" or len(y) <= 2:
        return y, notes
    step = pd.tseries.frequencies.to_offset(OFFSET[freq])

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
