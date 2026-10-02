"""make_chart: a chart computed from a table by code, ready for the UI to draw.

    line       x = a date (bucketed by time_unit) or number; y = a number combined by agg
    bar        x = a category; y = a number combined by agg (largest first, top N)
    scatter    x and y = numbers (up to 2,000 points)
    histogram  x = a number (y is not used)

Without y, line and bar count rows. The model gets a short summary of what was drawn (largest
groups, first and last values), so it describes the chart from numbers, not from imagination.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd

from .core import ToolError, ToolResult, number, ok, tool
from .periods import CODE, WORD, bucket, coarsest, drop_partial_edges, full_range, native_freq
from .values import parse_dates
from .workspace import Workspace, resolve_column

ChartType = Literal["line", "bar", "scatter", "histogram"]
Agg = Literal["sum", "mean", "median", "count", "min", "max"]
TimeUnit = Literal["auto", "day", "week", "month", "quarter", "year"]
MAX_POINTS = 2000


@tool
def make_chart(ws: Workspace, data: str, type: ChartType, x: str, y: str | None = None, agg: Agg = "sum",
               time_unit: TimeUnit = "auto", top: int = 20, title: str | None = None) -> ToolResult:
    """Draw a line, bar, scatter or histogram chart of a table."""
    df = ws.table(data)
    x = resolve_column(df, x, "x")
    y = resolve_column(df, y, "y") if y else None
    notes = ws.column_notes(data)
    unit = notes.get(y, {}).get("unit") if y else None
    top = max(1, min(int(top), 50))
    if type == "histogram":
        chart, summary = _histogram(df, x)
    elif type == "scatter":
        if not y:
            raise ToolError("A scatter chart needs y (a number column).", field="y")
        chart, summary = _scatter(df, x, y)
    else:
        if y is None:
            agg = "count"
        elif agg != "count":
            _need_number(df, y, "y")
        chart, summary = _line(df, x, y, agg, time_unit) if type == "line" else _bar(df, x, y, agg, top)
    y_label = chart.pop("y_label", None) or (f"{agg} of {y}" if y and agg != "count" else "rows")
    title = title or (f"{y_label} by {x}" if type in ("line", "bar") else f"{y} vs {x}" if type == "scatter"
                      else f"distribution of {x}")
    artifact = {"type": "chart", "chart": type, "title": title, "x_label": x, "y_label": y_label,
                **({"unit": unit} if unit else {}), **chart}
    warning = ws.issues_warning(data)
    return ok({"chart": type, "data": data, "title": title, **summary, **({"data_warning": warning} if warning else {})},
              artifact)


def _need_number(df: pd.DataFrame, c: str, field: str) -> pd.Series:
    s = df[c]
    if not pd.api.types.is_numeric_dtype(s) or pd.api.types.is_bool_dtype(s):
        raise ToolError(f"'{c}' is not a number column; {field} needs numbers.", field=field)
    return s.astype(float)


def _combine(groups, y: str | None, agg: str) -> pd.Series:
    if y is None or agg == "count":
        return groups.size().astype(float)
    return groups[y].agg(agg).astype(float)


def _line(df, x, y, agg, time_unit):
    frame = df.dropna(subset=[x])
    if not pd.api.types.is_datetime64_any_dtype(frame[x]) and not pd.api.types.is_numeric_dtype(frame[x]):
        # Periods written as text ('2015-01', 'Jan 2015', '2015-03-01') are dates too.
        dates, bad, _ = parse_dates(frame[x].astype(str))
        if len(frame) and bad.mean() <= 0.05:
            frame = frame.assign(**{x: dates}).dropna(subset=[x])
    if pd.api.types.is_datetime64_any_dtype(frame[x]):
        unit = _auto_unit(frame[x]) if time_unit == "auto" else time_unit
        freq = CODE[unit]
        values = _combine(frame.groupby(bucket(frame[x], freq)), y, agg)
        values = values.reindex(full_range(values.index, freq))
        notes = []
        if agg in ("sum", "count"):
            values = values.fillna(0.0)  # no rows in a period = nothing happened
            values, notes = drop_partial_edges(values, frame[x], freq)
        labels = [d.strftime("%Y-%m-%d") for d in values.index]
        note = f"by {unit}"
    elif pd.api.types.is_numeric_dtype(frame[x]) and not pd.api.types.is_bool_dtype(frame[x]):
        values = _combine(frame.groupby(x), y, agg).sort_index()
        labels, note = [number(float(v)) for v in values.index], "by value"
    else:
        raise ToolError(f"A line chart needs dates or numbers on x; '{x}' is text. For values over time, give the "
                        "table's date column as x and a time_unit: the chart groups by period itself.", field="x")
    v = values.dropna()
    if v.empty:
        raise ToolError("No values to draw: every period is empty." if len(values) else "No rows to draw.",
                        invalid=False)
    summary = {"points": len(values), "grouping": note,
               "first": [labels[0], number(values.iloc[0])], "last": [labels[-1], number(values.iloc[-1])],
               "highest": [_label(v.idxmax()), number(v.max())], "lowest": [_label(v.idxmin()), number(v.min())]}
    if pd.api.types.is_datetime64_any_dtype(frame[x]) and notes:
        summary["notes"] = notes
    return {"x": labels, "y": [None if pd.isna(a) else number(a) for a in values]}, summary


def _bar(df, x, y, agg, top):
    frame = df.dropna(subset=[x])
    if pd.api.types.is_datetime64_any_dtype(frame[x]):
        raise ToolError(f"'{x}' holds dates; use a line chart for values over time.", field="x")
    values = _combine(frame.groupby(frame[x].astype(str)), y, agg).sort_values(ascending=False)
    if values.empty:
        raise ToolError("No rows to draw.", invalid=False)
    shown = values.head(top)
    summary = {"groups": len(values), "shown": len(shown),
               "largest": [[str(k), number(v)] for k, v in shown.head(5).items()]}
    if len(values) > len(shown):
        summary["note"] = f"Top {len(shown)} of {len(values)} groups shown."
    if agg in ("sum", "count") and (values >= 0).all() and values.sum():
        summary["top_share"] = number(float(shown.iloc[0] / values.sum()))
    return {"x": [str(k) for k in shown.index], "y": [number(v) for v in shown]}, summary


def _scatter(df, x, y):
    a, b = _need_number(df, x, "x"), _need_number(df, y, "y")
    both = pd.DataFrame({"x": a, "y": b}).dropna()
    if both.empty:
        raise ToolError("No rows have both values.", invalid=False)
    sample = both.sample(MAX_POINTS, random_state=0) if len(both) > MAX_POINTS else both
    summary = {"points": len(sample), "rows_with_both": len(both),
               **({"note": f"A random {MAX_POINTS:,} of {len(both):,} points shown."} if len(both) > MAX_POINTS else {})}
    return {"x": [number(v) for v in sample["x"]], "y": [number(v) for v in sample["y"]], "y_label": y}, summary


def _histogram(df, x):
    v = _need_number(df, x, "x").dropna()
    if v.empty:
        raise ToolError(f"'{x}' has no values.", invalid=False)
    edges = np.histogram_bin_edges(v, bins="auto")
    if len(edges) > 41:
        edges = np.histogram_bin_edges(v, bins=40)
    counts, edges = np.histogram(v, bins=edges)
    peak = int(np.argmax(counts))
    summary = {"values": len(v), "bins": len(counts), "min": number(v.min()), "median": number(v.median()),
               "max": number(v.max()), "most_common_range": [number(edges[peak]), number(edges[peak + 1])]}
    return {"x": [number(e) for e in edges[:-1]], "y": [int(c) for c in counts],
            "bin_width": number(float(edges[1] - edges[0])), "y_label": "rows"}, summary


def _auto_unit(dates: pd.Series) -> str:
    """By the span (enough points, not too many), never finer than the data's own grain."""
    days = (dates.max() - dates.min()).days
    by_span = "D" if days <= 62 else "W" if days <= 550 else "M" if days <= 3650 else "Y"
    return WORD[coarsest(native_freq(dates), by_span)]


def _label(v):
    return v.strftime("%Y-%m-%d") if isinstance(v, pd.Timestamp) else number(float(v)) if isinstance(v, (int, float, np.number)) else str(v)
