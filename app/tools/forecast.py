"""forecast: the next periods of a value over time, with a range and an honest reliability check.

1. The table becomes a series: rows grouped by period (day / week / month / quarter / year) and
   combined (sum, mean, last value, or a row count). Partial first and last periods are left out
   for sums and counts (a month 10 days old is not a 'drop').
2. Exponential smoothing (ETS) and Theta are backtested against a seasonal naive baseline (same
   period last season) on past windows; a model is used only if it beat the baseline.
3. The 80% range is the 80% quantile of the chosen model's own backtest errors (split conformal).
4. Reliability follows the backtest error itself: beating a baseline that missed by 200% does not
   make a 90% miss usable, so large past errors are said plainly.

Ported from the analyst project (analyst/ops/timeseries.py). There, on 7 real series, the 80%
ranges held 69% of the real values: they are somewhat too narrow, a stated limit.
"""

from __future__ import annotations

import logging
import warnings
from typing import Literal

import numpy as np
import pandas as pd

from .core import ToolError, ToolResult, number, ok, tool
from .periods import (CODE, OFFSET, WORD, bucket, coarsest, drop_partial_edges, full_range, native_freq,
                      split_dates, split_time)
from .workspace import Workspace, resolve_column

SEASON = {"D": 7, "W": 52, "M": 12, "Q": 4, "Y": 1}
HORIZON = {"D": 14, "W": 8, "M": 6, "Q": 4, "Y": 2}
MIN_POINTS = 10


@tool
def forecast(ws: Workspace, data: str, date: str, value: str | None = None, horizon: int | None = None,
             time_unit: Literal["auto", "day", "week", "month", "quarter", "year"] = "auto",
             agg: Literal["sum", "mean", "last", "count"] = "sum") -> ToolResult:
    """Forecast ``value`` (combined per period by ``agg``; row count without value)."""
    df = ws.table(data)
    split, made = split_time(df), []  # time kept as year + quarter (or month) columns: combined here
    try:
        date = resolve_column(df, date, "date")
    except ToolError:
        if not split:
            raise
        date = None
    if date is None or not pd.api.types.is_datetime64_any_dtype(df[date]):
        if not split:
            raise ToolError(f"'{date}' is not a date column (parse_dates first if it holds dates).", field="date")
        year, part, kind = split
        df, date = df.assign(_period=split_dates(df, year, part, kind)), "_period"
        made = [f"Dates were made from the '{year}' and '{part}' columns."]
    if value:
        value = resolve_column(df, value, "value")
        if not pd.api.types.is_numeric_dtype(df[value]) or pd.api.types.is_bool_dtype(df[value]):
            raise ToolError(f"'{value}' is not a number column.", field="value")
    else:
        agg = "count"
    frame = df.dropna(subset=[date] + ([value] if value else []))
    if frame.empty:
        raise ToolError("No rows have both a date and a value.", invalid=False)
    freq = _auto_freq(frame[date]) if time_unit == "auto" else CODE[time_unit]
    y, notes = _series(frame, date, value, agg, freq)
    warning = ws.issues_warning(data)
    return _forecast(y, freq, horizon, agg, data, value or "rows", made + notes + ([warning] if warning else []))


def _auto_freq(dates: pd.Series) -> str:
    """The data's own grain, coarsened when the span is long (enough points, not too many)."""
    span = (dates.max() - dates.min()).days
    return coarsest(native_freq(dates), "D" if span <= 120 else "W" if span <= 400 else "M")


def _series(frame, date, value, agg, freq) -> tuple[pd.Series, list[str]]:
    period = bucket(frame[date], freq)
    groups = frame.groupby(period)
    if agg == "count":
        y = groups.size().astype(float)
    else:
        col = groups[value]
        y = (col.sum() if agg == "sum" else col.mean() if agg == "mean"
             else frame.sort_values(date).groupby(period)[value].last()).astype(float)
    y = y.reindex(full_range(y.index, freq))
    notes = []
    if agg in ("sum", "count"):
        y = y.fillna(0.0)  # a period without rows had nothing
        y, notes = drop_partial_edges(y, frame[date], freq)
    elif y.isna().mean() > 0.2:
        raise ToolError(f"{y.isna().mean():.0%} of the {WORD[freq]}s have no value; too many gaps to forecast. "
                        "Check the dates: if they were made from year + quarter columns, quarter q starts in month "
                        "(q - 1) * 3 + 1 - or give forecast the original table, which combines them itself.",
                        invalid=False)
    else:
        y = y.interpolate(limit_direction="both")
    return y, notes


def _forecast(y: pd.Series, freq: str, horizon: int | None, agg: str, data: str, label: str,
              notes: list[str]) -> ToolResult:
    from statsforecast import StatsForecast
    from statsforecast.models import AutoETS, AutoTheta, Naive, SeasonalNaive

    n, word = len(y), WORD[freq]
    if n < MIN_POINTS:
        raise ToolError(f"Only {n} {word}s of history; a forecast needs at least {MIN_POINTS} "
                        "(try a smaller time_unit).", invalid=False)
    h = max(1, min(int(horizon or HORIZON[freq]), n // 3))
    if horizon and h < horizon:
        notes.append(f"Horizon shortened to {h} {word}s: at most a third of the {n} {word}s of history.")
    season = SEASON[freq] if n >= 2 * SEASON[freq] + h and SEASON[freq] > 1 else 1
    base_name = "SeasonalNaive" if season > 1 else "Naive"
    models = [AutoETS(season_length=season), AutoTheta(season_length=season),
              SeasonalNaive(season_length=season) if season > 1 else Naive()]
    frame = pd.DataFrame({"unique_id": "s", "ds": y.index, "y": y.to_numpy(float)})
    sf = StatsForecast(models=models, freq=OFFSET[freq], n_jobs=1)
    windows = max(1, min(3, (n - max(2 * season, 8)) // h))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        logging.getLogger("statsforecast").setLevel(logging.ERROR)
        cv = sf.cross_validation(df=frame, h=h, n_windows=windows, step_size=h)
        names = [c for c in cv.columns if c not in ("unique_id", "ds", "cutoff", "y")]
        mae = {m: float((cv[m] - cv["y"]).abs().mean()) for m in names}
        best = min(mae, key=mae.get)
        fc = sf.forecast(df=frame, h=h)

    scale = float(cv["y"].abs().mean()) or 1.0
    beats = best != base_name and mae[best] < mae[base_name]
    use = best if beats else base_name
    mean = fc[use].to_numpy(float)
    errors = (cv["y"] - cv[use]).abs().dropna().to_numpy(float)
    q = float(np.sort(errors)[min(len(errors) - 1, int(np.ceil((len(errors) + 1) * 0.8)) - 1)])
    lo, hi = mean - q, mean + q
    if agg in ("sum", "count") and (y >= 0).all():
        mean, lo, hi = np.clip(mean, 0, None), np.clip(lo, 0, None), np.clip(hi, 0, None)
    if len(errors) < 20:
        notes.append(f"The range rests on only {len(errors)} past errors; treat it as rough.")
    error, base_error = mae[use] / scale, mae[base_name] / scale
    strength, caveat = _reliability(error, beats)
    baseline_words = "repeating the same period of last season" if season > 1 else "repeating the last value"
    if beats:
        backtest = (f"On past data the {_model_word(best)} model missed by {error:.0%} on average, vs "
                    f"{base_error:.0%} for {baseline_words}.")
    else:
        backtest = f"No model beat {baseline_words} on past data (its error {base_error:.0%}), so the forecast repeats that pattern."
    dates = pd.to_datetime(fc["ds"])
    points = [{"period": d.strftime("%Y-%m-%d"), "forecast": number(m), "low_80": number(a), "high_80": number(b)}
              for d, m, a, b in zip(dates, mean, lo, hi)]
    result = {"data": data, "value": label, "combined_by": agg, "period": word, "history_points": n,
              "horizon": h, "model": _model_word(use), "beats_baseline": beats, "backtest": backtest + caveat,
              "reliability": strength, "forecast": points,
              "range": "low_80 to high_80: the model's past errors put 80% of outcomes in this range."}
    if agg in ("sum", "count"):
        result["total_over_horizon"] = number(float(mean.sum()))
    if notes:
        result["notes"] = notes
    artifact = {"type": "chart", "chart": "forecast", "title": f"{label} forecast ({word}s)",
                "x": [d.strftime("%Y-%m-%d") for d in y.index], "y": [number(v) for v in y],
                "forecast_x": [p["period"] for p in points], "forecast": [p["forecast"] for p in points],
                "low": [p["low_80"] for p in points], "high": [p["high_80"] for p in points],
                "x_label": word, "y_label": f"{agg} of {label}" if agg != "count" else "rows"}
    return ok(result, artifact)


def _reliability(error: float, beats: bool) -> tuple[str, str]:
    """Strength and a plain caveat from the average backtest error (relative to the values).
    Plain-language cut-offs, not tuned on a benchmark."""
    if error > 1.0:
        return "none", (f" Its past errors were larger than the values themselves ({error:.0%}): this series cannot "
                        "be forecast reliably (it may have changed course) - do not plan on these numbers.")
    if error > 0.5:
        return "weak", f" Its past errors were large ({error:.0%}): treat this forecast as a rough guess."
    if error > 0.25:
        return "moderate", f" Expect sizeable misses (past errors {error:.0%})."
    return ("strong" if beats else "moderate"), ""


def _model_word(name: str) -> str:
    return {"AutoETS": "exponential smoothing (ETS)", "AutoTheta": "Theta", "SeasonalNaive": "seasonal naive",
            "Naive": "naive (last value)"}.get(name, name)
