"""make_chart: a chart computed from a table by code, ready for the UI to draw.

    line       x = a date (bucketed by time_unit) or number; y = a number combined by agg;
               by = one line per group
    area       a line with its area filled; with by, the groups stacked (each one's share of the whole)
    bar        x = a category; y combined by agg (largest first, top N); by = grouped bars,
               stacked=true stacks them
    scatter    x and y = numbers (up to 2,000 points) with a trend line; by = one colour per group
    histogram  x = a number: how its values spread; by = one spread per group, the same bins, drawn over each
               other (measured: 'age of survivors vs the rest' was refused, so it was drawn with seaborn)
    box        y = a number, x = groups (optional): median, quartiles and outliers per group
    heatmap    x and by = two categories; each cell is y combined by agg (or a row count)
    pie        x = a category; y summed or counted; the 5 largest slices and 'other'

Without y, line, bar, heatmap and pie count rows. With by, at most MAX_SERIES groups are drawn (the
largest), and the result says which were left out. The model gets a short summary of what was drawn
(largest groups, first and last values, the trend), so it describes the chart from numbers, not
from imagination; the UI gets the full series.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd
from scipy import stats

from .chart_image import save_chart
from .core import ToolError, ToolResult, number, ok, tool
from .periods import CODE, WORD, bucket, coarsest, drop_partial_edges, full_range, native_freq
from .values import parse_dates
from .workspace import Workspace, resolve_column

ChartType = Literal["line", "area", "bar", "scatter", "histogram", "box", "heatmap", "pie"]
Agg = Literal["sum", "mean", "median", "count", "min", "max"]
TimeUnit = Literal["auto", "day", "week", "month", "quarter", "year"]
MAX_POINTS = 2000
MAX_SERIES = 6  # lines, bar groups or colours: more can't be told apart
MAX_BOXES = 15
MAX_CELLS = 30  # per side of a heatmap
PIE_SLICES = 5


@tool
def make_chart(ws: Workspace, data: str, type: ChartType, x: str | None = None, y: str | None = None,
               by: str | None = None, agg: Agg = "sum", time_unit: TimeUnit = "auto", top: int = 20,
               stacked: bool = False, title: str | None = None) -> ToolResult:
    """Draw a chart of a table: line, area, bar, scatter, histogram, box, heatmap or pie - kept as a chart file."""
    df = ws.table(data)
    if x is None and not (type == "box" and y):
        raise ToolError(f"A {type} chart needs x (a column).", field="x")
    x = resolve_column(df, x, "x") if x else None
    y = resolve_column(df, y, "y") if y else None
    by = resolve_column(df, by, "by") if by else None
    if by and by in (x, y):  # 'bar of tip by day' given as x=day, by=day: the split is x itself
        by = None
    if by and type in ("box", "pie"):
        hint = {"box": "the groups of a box chart go in x", "pie": "a pie shows one column's shares"}[type]
        raise ToolError(f"A {type} chart takes no by: {hint}.", field="by")
    unit = ws.column_notes(data).get(y, {}).get("unit") if y else None
    top = max(1, min(int(top), 50))
    if y is None or agg == "count":
        agg = "count"
    elif type not in ("scatter", "box"):
        _need_number(df, y, "y")

    if type == "histogram":
        chart, summary = _histogram(df, x) if not by else _histograms(df, x, by)
    elif type == "scatter":
        if not y:
            raise ToolError("A scatter chart needs y (a number column).", field="y")
        chart, summary = _scatter(df, x, y, by)
    elif type == "box":
        chart, summary = _box(df, x, y, top)
    elif type == "heatmap":
        if not by:
            raise ToolError("A heatmap needs x and by: two category columns (e.g. x=weekday, by=store).", field="by")
        chart, summary = _heatmap(df, x, by, y, agg)
    elif type == "pie":
        if agg not in ("sum", "count"):
            raise ToolError("A pie shows shares of a total: agg must be sum or count. For averages use a bar chart.",
                            field="agg")
        chart, summary = _pie(df, x, y, agg)
    elif type in ("line", "area"):
        chart, summary = _line(df, x, y, agg, time_unit, by)
    else:
        chart, summary = _bar(df, x, y, agg, top, by)

    y_label = chart.pop("y_label", None) or (f"{agg} of {y}" if y and agg != "count" else "rows")
    title = title or _title(type, x, y, by, y_label)
    artifact = {"type": "chart", "chart": type, "title": title, "x_label": x, "y_label": y_label,
                **({"unit": unit} if unit else {}), **({"stacked": True} if stacked and by and type == "bar" else {}),
                **chart}
    warning = ws.issues_warning(data)
    artifact["id"] = save_chart(ws, artifact, parent=data)  # the chart file: drawn live, a picture when a document needs one
    return ok({"chart": type, "data": data, "title": title, **summary, **({"data_warning": warning} if warning else {}),
               "chart_file": artifact["id"]}, artifact)


def _title(type, x, y, by, y_label) -> str:
    split = f", per {by}" if by else ""
    if type in ("line", "bar"):
        return f"{y_label} by {x}{split}"
    if type == "scatter":
        return f"{y} vs {x}{split}"
    if type == "histogram":
        return f"distribution of {x}"
    if type == "box":
        return f"{y or x} by {x}" if y and x else f"distribution of {y or x}"
    if type == "heatmap":
        return f"{y_label} by {by} and {x}"
    return f"share of {y_label} by {x}"


def _numeric(s: pd.Series) -> bool:
    return pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_bool_dtype(s)


def _need_number(df: pd.DataFrame, c: str, field: str) -> pd.Series:
    if not _numeric(df[c]):
        raise ToolError(f"'{c}' is not a number column; {field} needs numbers.", field=field)
    return df[c].astype(float)


def _combine(groups, y: str | None, agg: str) -> pd.Series:
    if y is None or agg == "count":
        return groups.size().astype(float)
    return groups[y].agg(agg).astype(float)


def _largest_groups(frame: pd.DataFrame, by: str, y: str | None, agg: str, n: int) -> tuple[list[str], int]:
    """The n groups that matter most (largest total, or most rows for averages), and how many there are."""
    keys = frame[by].astype(str)
    size = (frame.groupby(keys)[y].sum() if y and agg == "sum" else keys.value_counts()).abs()
    order = size.sort_values(ascending=False)
    return [str(k) for k in order.index[:n]], len(order)


def _left_out(shown: int, total: int, by: str) -> list[str]:
    return [f"The {shown} largest of {total} {by} groups are drawn."] if total > shown else []


def _dates_or_numbers(frame: pd.DataFrame, x: str) -> pd.DataFrame:
    if not pd.api.types.is_datetime64_any_dtype(frame[x]) and not pd.api.types.is_numeric_dtype(frame[x]):
        # Periods written as text ('2015-01', 'Jan 2015', '2015-03-01') are dates too.
        dates, bad, _ = parse_dates(frame[x].astype(str))
        if len(frame) and bad.mean() <= 0.05:
            frame = frame.assign(**{x: dates}).dropna(subset=[x])
    return frame


def _line(df, x, y, agg, time_unit, by):
    frame = _dates_or_numbers(df.dropna(subset=[x]), x)
    if frame.empty:  # (a date range over no dates can't be made: it ended in an internal error)
        raise ToolError(f"'{x}' has no values to draw.", invalid=False)
    is_date = pd.api.types.is_datetime64_any_dtype(frame[x])
    if not is_date and not _numeric(frame[x]):
        raise ToolError(f"A line chart needs dates or numbers on x; '{x}' is text. For values over time, give the "
                        "table's date column as x and a time_unit: the chart groups by period itself. To compare "
                        "categories use a bar chart.", field="x")
    notes = []
    if by:
        groups, total = _largest_groups(frame, by, y, agg, MAX_SERIES)
        notes += _left_out(len(groups), total, by)
        keys = frame[by].astype(str)
        frame = frame[keys.isin(groups)].assign(**{by: keys})
    if is_date:
        unit = _auto_unit(frame[x]) if time_unit == "auto" else time_unit
        freq = CODE[unit]
        key = bucket(frame[x], freq)
        grouping = f"by {unit}"
    else:
        key, freq, grouping = frame[x], None, "by value"
    if by:
        table = _combine(frame.groupby([key, frame[by]]), y, agg).unstack(by)
        table = table[[g for g in groups if g in table.columns]]
    else:
        table = _combine(frame.groupby(key), y, agg).to_frame("value")
    table = table.sort_index()
    if is_date:
        table = table.reindex(full_range(table.index, freq))
        if agg in ("sum", "count"):
            table = table.fillna(0.0)  # no rows in a period = nothing happened
            kept, edge_notes = drop_partial_edges(table.iloc[:, 0], frame[x], freq)
            table, notes = table.loc[kept.index], notes + edge_notes
        labels = [d.strftime("%Y-%m-%d") for d in table.index]
    else:
        labels = [number(float(v)) for v in table.index]
    if table.dropna(how="all").empty:
        raise ToolError("No values to draw: every period is empty." if len(table) else "No rows to draw.",
                        invalid=False)
    summary = {"points": len(table), "grouping": grouping}
    if by:
        summary["series"] = [_series_facts(str(g), table[g], labels, agg) for g in table.columns]
        chart = {"x": labels, "series": [{"name": str(g), "y": _values(table[g])} for g in table.columns]}
    else:
        v = table["value"]
        summary.update(_series_facts(None, v, labels, agg))
        chart = {"x": labels, "y": _values(v)}
    if notes:
        summary["notes"] = notes
    return chart, summary


def _series_facts(name, v: pd.Series, labels: list, agg: str) -> dict:
    present = v.dropna()
    pos = {k: i for i, k in enumerate(v.index)}
    facts = {"first": [labels[0], number(v.iloc[0])], "last": [labels[-1], number(v.iloc[-1])],
             "highest": [labels[pos[present.idxmax()]], number(present.max())],
             "lowest": [labels[pos[present.idxmin()]], number(present.min())]}
    if agg in ("sum", "count"):
        facts["total"] = number(float(present.sum()))
    return {"group": name, **facts} if name is not None else facts


def _values(v: pd.Series) -> list:
    return [None if pd.isna(a) else number(a) for a in v]


def _bar(df, x, y, agg, top, by):
    frame = df.dropna(subset=[x])
    if pd.api.types.is_datetime64_any_dtype(frame[x]):
        raise ToolError(f"'{x}' holds dates; use a line chart for values over time.", field="x")
    keys = frame[x].astype(str)
    values = _combine(frame.groupby(keys), y, agg).sort_values(ascending=False)
    if values.empty:
        raise ToolError("No rows to draw.", invalid=False)
    shown = values.head(top)
    summary = {"groups": len(values), "shown": len(shown),
               "largest": [[str(k), number(v)] for k, v in shown.head(5).items()]}
    notes = [f"Top {len(shown)} of {len(values)} {x} values shown."] if len(values) > len(shown) else []
    if agg in ("sum", "count") and (values >= 0).all() and values.sum():
        summary["top_share"] = number(float(shown.iloc[0] / values.sum()))
    if not by:
        if notes:
            summary["note"] = notes[0]
        return {"x": [str(k) for k in shown.index], "y": [number(v) for v in shown]}, summary
    sub = frame[keys.isin(shown.index)]
    groups, total = _largest_groups(sub, by, y, agg, MAX_SERIES)
    notes += _left_out(len(groups), total, by)
    split = sub[by].astype(str)
    sub = sub[split.isin(groups)]
    table = _combine(sub.groupby([sub[x].astype(str), sub[by].astype(str)]), y, agg).unstack(by)
    table = table.reindex(index=[str(k) for k in shown.index], columns=groups)
    if agg in ("sum", "count"):
        table = table.fillna(0.0)
    summary["by"] = [{"group": g, "largest": [str(table[g].idxmax()), number(table[g].max())]} for g in groups
                     if table[g].notna().any()]
    if notes:
        summary["notes"] = notes
    return {"x": list(table.index), "series": [{"name": g, "y": _values(table[g])} for g in groups]}, summary


def _scatter(df, x, y, by):
    a, b = _need_number(df, x, "x"), _need_number(df, y, "y")
    both = pd.DataFrame({"x": a, "y": b, "g": df[by].astype(str) if by else ""}).dropna(subset=["x", "y"])
    if both.empty:
        raise ToolError("No rows have both values.", invalid=False)
    sample = both.sample(MAX_POINTS, random_state=0) if len(both) > MAX_POINTS else both
    summary = {"points": len(sample), "rows_with_both": len(both)}
    if len(both) > MAX_POINTS:
        summary["note"] = f"A random {MAX_POINTS:,} of {len(both):,} points shown."
    chart = {"x": [number(v) for v in sample["x"]], "y": [number(v) for v in sample["y"]], "y_label": y}
    if len(both) >= 3 and both["x"].nunique() > 1:
        slope, intercept = np.polyfit(both["x"], both["y"], 1)
        rho = stats.spearmanr(both["x"], both["y"])[0]
        lo, hi = float(both["x"].min()), float(both["x"].max())
        chart["trend"] = [[number(lo), number(intercept + slope * lo)], [number(hi), number(intercept + slope * hi)]]
        summary["trend"] = {"slope": number(float(slope)), "spearman_rho": number(float(rho)),
                            "meaning": f"{y} changes by about {number(float(slope))} per 1 of {x} (a straight-line "
                                       "fit; test it with compare question=correlation)"}
    if by:
        groups, total = _largest_groups(both.rename(columns={"g": by}), by, None, "count", MAX_SERIES)
        g = sample["g"].where(sample["g"].isin(groups), "other")
        names = groups + (["other"] if (g == "other").any() else [])
        chart["groups"] = names
        chart["group"] = [names.index(v) for v in g]
        summary["groups"] = names
        if total > len(groups):
            summary["notes"] = [f"The {len(groups)} largest of {total} {by} groups have their own colour; the rest "
                                "are 'other'."]
    return chart, summary


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


def _histograms(df, x, by):
    """How x spreads in each of the largest groups of by: the same bins for all, so they compare."""
    v = _need_number(df, x, "x")
    frame = df.assign(_v=v).dropna(subset=["_v", by])
    if frame.empty:
        raise ToolError(f"'{x}' has no values.", invalid=False)
    groups, total = _largest_groups(frame, by, None, "count", MAX_SERIES)
    edges = np.histogram_bin_edges(frame["_v"], bins="auto")
    if len(edges) > 41:
        edges = np.histogram_bin_edges(frame["_v"], bins=40)
    series, summary = [], {"values": len(frame), "bins": len(edges) - 1, "groups": {}}
    keys = frame[by].astype(str)
    for g in groups:
        part = frame.loc[keys == g, "_v"]
        counts, _ = np.histogram(part, bins=edges)
        series.append({"name": g, "y": [int(c) for c in counts]})
        summary["groups"][g] = {"values": len(part), "median": number(part.median())}
    if total > len(groups):
        summary["note"] = f"The {len(groups)} largest of {total} {by} groups are drawn."
    return {"x": [number(e) for e in edges[:-1]], "series": series, "bin_width": number(float(edges[1] - edges[0])),
            "y_label": "rows"}, summary


def _box(df, x, y, top):
    if y is None:  # one box of x's values
        x, y = None, x
    values = _need_number(df, y, "y")
    if x is None:
        groups = {"all rows": values.dropna()}
    else:
        if _numeric(df[x]) and df[x].nunique() > MAX_BOXES:
            raise ToolError(f"'{x}' has {df[x].nunique()} different numbers; the groups of a box chart (x) must be "
                            f"categories. For two numbers use a scatter chart.", field="x")
        keys = df[x].astype(str).where(df[x].notna())
        counts = keys.value_counts()
        keep = counts.index[:min(top, MAX_BOXES)]
        groups = {str(k): values[keys == k].dropna() for k in keep}
    groups = {k: v for k, v in groups.items() if len(v)}
    if not groups:
        raise ToolError(f"'{y}' has no values.", invalid=False)
    boxes, facts = [], []
    for name, v in groups.items():
        q1, med, q3 = (float(v.quantile(q)) for q in (0.25, 0.5, 0.75))
        fence_lo, fence_hi = q1 - 1.5 * (q3 - q1), q3 + 1.5 * (q3 - q1)
        inside = v[(v >= fence_lo) & (v <= fence_hi)]
        out = v[(v < fence_lo) | (v > fence_hi)]
        boxes.append({"min": number(float(inside.min())), "q1": number(q1), "median": number(med), "q3": number(q3),
                      "max": number(float(inside.max())), "mean": number(float(v.mean())),
                      "outliers": [number(float(o)) for o in out.sample(min(len(out), 50), random_state=0)]})
        facts.append({"group": name, "rows": len(v), "median": number(med), "middle_half": [number(q1), number(q3)],
                      "outliers": len(out)})
    summary = {"groups": facts}
    if len(facts) > 1:
        high = max(facts, key=lambda f: f["median"])
        summary["highest_median"] = [high["group"], high["median"]]
    if x is not None and df[x].nunique() > len(groups):
        summary["note"] = f"The {len(groups)} largest of {df[x].nunique()} {x} groups shown."
    return {"x": list(groups), "boxes": boxes, "y_label": y}, summary


def _heatmap(df, x, by, y, agg):
    frame = df.dropna(subset=[x, by])
    if pd.api.types.is_datetime64_any_dtype(frame[x]) or pd.api.types.is_datetime64_any_dtype(frame[by]):
        raise ToolError("A heatmap's x and by are categories. For a date, first make a column like "
                        "dayname(date) or hour(time) with query_data (save=true).", field="x")
    cols = frame[x].astype(str).value_counts().index[:MAX_CELLS]
    rows = frame[by].astype(str).value_counts().index[:MAX_CELLS]
    sub = frame[frame[x].astype(str).isin(cols) & frame[by].astype(str).isin(rows)]
    table = _combine(sub.groupby([sub[by].astype(str), sub[x].astype(str)]), y, agg).unstack(x)
    table = table.reindex(index=_ordered(rows), columns=_ordered(cols))
    if agg in ("sum", "count"):
        table = table.fillna(0.0)
    cells = table.stack().dropna()
    if cells.empty:
        raise ToolError("No rows to draw.", invalid=False)
    (hr, hc), (lr, lc) = cells.idxmax(), cells.idxmin()
    summary = {"cells": int(table.size), "highest": [f"{by}={hr}, {x}={hc}", number(cells.max())],
               "lowest": [f"{by}={lr}, {x}={lc}", number(cells.min())]}
    left = [c for c, n in ((x, frame[x].nunique()), (by, frame[by].nunique())) if n > MAX_CELLS]
    if left:
        summary["note"] = f"Only the {MAX_CELLS} most common values of {', '.join(left)} are shown."
    return {"x": list(table.columns), "rows": list(table.index), "values": [_values(table.loc[r]) for r in table.index],
            "row_label": by}, summary


def _ordered(labels) -> list[str]:
    """Numbers and weekday / month names in their natural order, anything else as given."""
    labels = [str(v) for v in labels]
    for order in (["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"],
                  ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                  ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
                   "november", "december"],
                  ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]):
        if all(v.lower() in order for v in labels):
            return sorted(labels, key=lambda v: order.index(v.lower()))
    try:
        return sorted(labels, key=float)
    except ValueError:
        return labels


def _pie(df, x, y, agg):
    frame = df.dropna(subset=[x])
    values = _combine(frame.groupby(frame[x].astype(str)), y, agg).sort_values(ascending=False)
    if values.empty or values.sum() <= 0:
        raise ToolError("No values to draw.", invalid=False)
    if (values < 0).any():
        raise ToolError(f"'{y}' has negative totals; a pie can't show them. Use a bar chart.", field="y")
    shown = values.head(PIE_SLICES)
    if len(values) > PIE_SLICES:
        shown = pd.concat([shown, pd.Series({"other": values.iloc[PIE_SLICES:].sum()})])
    share = shown / values.sum()
    summary = {"slices": [[str(k), number(v), number(float(s), 3)] for (k, v), s in zip(shown.items(), share)],
               "total": number(float(values.sum())), "slice_columns": ["label", "value", "share"]}
    if len(values) > PIE_SLICES:
        summary["note"] = f"{len(values) - PIE_SLICES} smaller {x} values are combined as 'other'."
    return {"x": [str(k) for k in shown.index], "y": [number(v) for v in shown]}, summary


def _auto_unit(dates: pd.Series) -> str:
    """By the span (enough points, not too many), never finer than the data's own grain."""
    days = (dates.max() - dates.min()).days
    by_span = "D" if days <= 62 else "W" if days <= 550 else "M" if days <= 3650 else "Y"
    return WORD[coarsest(native_freq(dates), by_span)]
