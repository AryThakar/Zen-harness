# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""forecast: the next periods of a value over time, with its trend and seasonal pattern, a range and an
honest reliability check.

1. The table becomes a series: rows grouped by period (day / week / month / quarter / year) and
   combined (sum, mean, last value, or a row count). Partial first and last periods are left out
   for sums and counts (a month 10 days old is not a 'drop'). With by, one series per group (the
   largest MAX_GROUPS).
2. Trend: is it really rising or falling? Mann-Kendall on the series (seasonal pattern removed,
   corrected for autocorrelation, on a log scale when it grows in proportion) combined with a sign
   test on the period-to-period changes; the slope is Theil-Sen's with its 95% range.
3. Seasonality: how much of the variation the season explains once the trend is out (0 = none,
   1 = all of it), with the season's high and low (December, Monday ...).
4. Exponential smoothing (ETS) and Theta are backtested against a seasonal naive baseline (same
   period last season) on past windows; a model is used only if it beat the baseline. The 80% range
   is the 80% quantile of the chosen model's own backtest errors (split conformal).
5. Reliability follows the backtest error itself: beating a baseline that missed by 200% does not
   make a 90% miss usable, so large past errors are said plainly.

Ported from the analyst project (analyst/ops/timeseries.py: forecast and trend_test). There, on 7
real series, the 80% ranges held 69% of the real values (somewhat too narrow, a stated limit); the
trend test raised 1-9% false alarms on simulated series without a trend and found 79-100% of real
ones (bench/honesty_bench.py, trend_scale_bench.py). Mann-Kendall is computed here directly.
"""

from __future__ import annotations

import logging
import warnings
from typing import Literal

import numpy as np
import pandas as pd
from scipy import stats

from .chart_image import save_chart
from .core import ToolError, ToolResult, number, ok, tool
from .periods import (CODE, OFFSET, WORD, bucket, coarsest, drop_partial_edges, full_range, native_freq,
                      default_agg, split_dates, split_time, weekdays_only)
from .roles import table_roles
from .workspace import Workspace, resolve_column

SEASON = {"D": 7, "W": 52, "M": 12, "Q": 4, "Y": 1, "B": 5}
HORIZON = {"D": 14, "W": 8, "M": 6, "Q": 4, "Y": 2, "B": 5}  # a weekdays-only series: the coming week
MIN_POINTS = 10
MAX_GROUPS = 5
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]


@tool
def forecast(ws: Workspace, data: str, date: str | None = None, value: str | None = None, horizon: int | None = None,
             time_unit: Literal["auto", "day", "week", "month", "quarter", "year"] = "auto",
             agg: Literal["sum", "mean", "last", "count"] | None = None, by: str | None = None) -> ToolResult:
    """Forecast ``value`` (combined per period by ``agg``: omitted, the mean for a level - a price, a rate - else the
    sum; row count without value), with its trend."""
    df = ws.table(data)
    df, date, made = _date_column(ws, data, df, date)
    if value:
        value = resolve_column(df, value, "value")
        if not pd.api.types.is_numeric_dtype(df[value]) or pd.api.types.is_bool_dtype(df[value]):
            raise ToolError(f"'{value}' is not a number column.", field="value")
        agg = agg or default_agg([value])
    else:
        agg = "count"
    by = resolve_column(df, by, "by") if by else None
    frame = df.dropna(subset=[date] + ([value] if value else []))
    if frame.empty:
        raise ToolError("No rows have both a date and a value.", invalid=False)
    numbers = [str(c) for c in frame.columns if c != date and pd.api.types.is_numeric_dtype(frame[c])
               and not pd.api.types.is_bool_dtype(frame[c])]
    if not value and not by and numbers and frame.groupby(frame[date].dt.normalize()).size().nunique() == 1:
        # measured 2026-10-22: a cafe's sales, four rows a day, forecast as "rows" in every demo run - a flat line,
        # charted beside the real forecasts
        per_day = int(frame.groupby(frame[date].dt.normalize()).size().iloc[0])
        raise ToolError(f"Every date has the same number of rows ({per_day}): a count of rows has nothing to forecast. "
                        f"Name the number column to forecast as value: {', '.join(numbers[:6])}.", field="value")
    freq = _auto_freq(frame[date]) if time_unit == "auto" else CODE[time_unit]
    label = value or "rows"
    warning = ws.issues_warning(data)
    notes = made + ([warning] if warning else [])
    if freq == "D" and weekdays_only(frame[date]):  # trading or office days: a weekend is no gap
        freq = "B"
        notes.append("The dates are weekdays only (trading or office days): weekends are not gaps, and the forecast "
                     "is for the coming weekdays.")
    if by:
        return _by_group(ws, frame, date, value, by, agg, freq, horizon, data, label, notes)
    y, series_notes = _series(frame, date, value, agg, freq)
    facts, chart = _forecast(y, freq, horizon, agg)
    notes = notes + series_notes + facts.pop("notes", [])
    result = {"data": data, "value": label, "combined_by": agg, "period": WORD[freq], **facts}
    if notes:
        result["notes"] = notes
    artifact = {"type": "chart", "chart": "forecast", "title": f"{label} forecast ({WORD[freq]}s)", **chart,
                "x_label": WORD[freq], "y_label": f"{agg} of {label}" if agg != "count" else "rows"}
    artifact["id"] = save_chart(ws, artifact, parent=data)
    return ok({**result, "chart_file": artifact["id"]}, artifact)


def _date_column(ws, data, df, date):
    """The date column: the one named, else the table's date role, else year + quarter/month columns."""
    split, made = split_time(df), []  # time kept as year + quarter (or month) columns: combined here
    if date:
        try:
            date = resolve_column(df, date, "date")
        except ToolError:
            if not split:
                raise
            date = None
    else:
        date = table_roles(ws, data, df).get("date")
    if date is None or not pd.api.types.is_datetime64_any_dtype(df[date]):
        if not split:
            if date is None:
                raise ToolError("This table has no date column. Columns: " + ", ".join(map(str, df.columns)) + ".",
                                field="date", invalid=False)
            raise ToolError(f"'{date}' is not a date column (parse_dates first if it holds dates).", field="date")
        year, part, kind = split
        df, date = df.assign(_period=split_dates(df, year, part, kind)), "_period"
        made = [f"Dates were made from the '{year}' and '{part}' columns."]
    return df, date, made


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


def _forecast(y: pd.Series, freq: str, horizon: int | None, agg: str) -> tuple[dict, dict]:
    """The facts for the model and the chart's series, for one series."""
    from statsforecast import StatsForecast
    from statsforecast.models import AutoETS, AutoTheta, Naive, SeasonalNaive

    n, word = len(y), WORD[freq]
    if n < MIN_POINTS:
        raise ToolError(f"Only {n} {word}s of history; a forecast needs at least {MIN_POINTS} "
                        "(try a smaller time_unit).", invalid=False)
    notes = []
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
    facts = {"history_points": n, "horizon": h, "model": _model_word(use), "beats_baseline": beats,
             "backtest": backtest + caveat, "reliability": strength, "forecast": points,
             "range": "low_80 to high_80: the model's past errors put 80% of outcomes in this range.",
             "trend": _trend_facts(y, freq), **_season_facts(y, freq), **_latest(y, freq)}
    if agg in ("sum", "count"):
        facts["total_over_horizon"] = number(float(mean.sum()))
    if notes:
        facts["notes"] = notes
    chart = {"x": [d.strftime("%Y-%m-%d") for d in y.index], "y": [number(v) for v in y],
             "forecast_x": [p["period"] for p in points], "forecast": [p["forecast"] for p in points],
             "low": [p["low_80"] for p in points], "high": [p["high_80"] for p in points]}
    return facts, chart


def _by_group(ws, frame, date, value, by, agg, freq, horizon, data, label, notes) -> ToolResult:
    keys = frame[by].astype(str)
    size = (frame.groupby(keys)[value].sum() if value and agg == "sum" else keys.value_counts()).abs()
    groups = [str(g) for g in size.sort_values(ascending=False).index[:MAX_GROUPS]]
    if len(size) > len(groups):
        notes = notes + [f"The {len(groups)} largest of {len(size)} {by} groups are forecast."]
    per_group, series, skipped = [], [], []
    for g in groups:
        sub = frame[keys == g]
        try:
            y, series_notes = _series(sub, date, value, agg, freq)
            facts, chart = _forecast(y, freq, horizon, agg)
        except ToolError as exc:
            skipped.append(f"{g}: {exc.message}")
            continue
        trend = facts["trend"]
        per_group.append({"group": g, "model": facts["model"], "reliability": facts["reliability"],
                          "beats_baseline": facts["beats_baseline"],
                          **({"total_over_horizon": facts["total_over_horizon"]} if "total_over_horizon" in facts else {}),
                          "next": facts["forecast"][0], "trend": trend["direction"],
                          **({"trend_per_period_pct": trend["per_period_pct"]} if "per_period_pct" in trend else {}),
                          **({"season_high": facts["season_high"]} if "season_high" in facts else {})})
        series.append({"name": g, **chart})
    if not per_group:
        raise ToolError("No group has enough history to forecast: " + "; ".join(skipped), invalid=False)
    result = {"data": data, "value": label, "combined_by": agg, "period": WORD[freq], "by": by,
              "horizon": len(series[0]["forecast_x"]), "groups": per_group,
              "range": "Each group's forecast has its own 80% range in the chart."}
    if notes or skipped:
        result["notes"] = notes + [f"Not forecast - {s}" for s in skipped]
    artifact = {"type": "chart", "chart": "forecast", "title": f"{label} forecast by {by} ({WORD[freq]}s)",
                "series": series, "x_label": WORD[freq], "y_label": f"{agg} of {label}" if agg != "count" else "rows"}
    artifact["id"] = save_chart(ws, artifact, parent=data)
    return ok({**result, "chart_file": artifact["id"]}, artifact)


# ---------------------------------------------------------------- trend and season

def _trend_facts(y: pd.Series, freq: str) -> dict:
    if len(y) < 4:
        return {"direction": "too short to tell"}
    t = trend_test(y, freq)
    p = "p < 0.0001" if t["p"] < 1e-4 else f"p = {t['p']:.2g}"
    out = {"direction": "no clear trend", "says": f"No clear upward or downward trend ({p}).",
           "p_value": number(t["p"], 4), "method": t["method"]}
    if t["p"] < 0.05:  # the size only for a real trend: a slope of noise is no fact to quote
        direction = "rising" if t["slope"] > 0 else "falling"
        out.update(direction=direction, per_period=number(t["slope"]),
                   per_period_range_95=[number(t["low"]), number(t["high"])],
                   per_period_pct=number(100 * t["relative"], 2), unit=f"per {WORD[freq]}",
                   says=f"{direction.capitalize()}: about {number(t['slope']):+g} per {WORD[freq]} "
                        f"({100 * t['relative']:+.2g}% of its average level), {p}.")
    season = SEASON.get(freq, 1)
    if season > 1 and len(y) < 2 * season + 2:
        out["note"] = (f"Less than two full {_season_word(freq)} cycles of history: a seasonal rise or fall (e.g. "
                       "towards December) can't be told apart from a trend.")
    return out


def mann_kendall_z(x: np.ndarray) -> float:
    """Mann-Kendall's S statistic as a z-score, with the tie correction and continuity correction."""
    n = len(x)
    s = sum(float(np.sign(x[k + 1:] - x[k]).sum()) for k in range(n - 1))
    _, ties = np.unique(x, return_counts=True)
    var = (n * (n - 1) * (2 * n + 5) - float(np.sum(ties * (ties - 1) * (2 * ties + 5)))) / 18
    if s == 0 or var <= 0:
        return 0.0
    return (s - np.sign(s)) / np.sqrt(var)


def trend_test(y: pd.Series, freq: str) -> dict:
    """Mann-Kendall trend test that does not mistake a season or a slow wander for a trend.

    With 2+ full seasons the seasonal pattern (the average of each month, weekday ...) is removed
    first. Serial correlation: the lag-1 autocorrelation of what is left after removing Sen's trend,
    corrected for small-sample bias ((r*n + 1) / (n - 4)), inflates the test's variance by
    (1 + r) / (1 - r) (Yue & Wang 2004). Series that grow in proportion are tested on a log scale,
    and a sign test on the period-to-period changes is combined with Mann-Kendall (Bonferroni: 2x the
    smaller p) so a steady rise is found although the correction takes it for wandering. The slope
    is Sen's (Theil-Sen), per period, with its 95% range. Measured in the analyst project: 1-9% false
    alarms on series without a trend; steady and compound growth found 100% of the time at n = 44.
    """
    x = y.to_numpy(float)
    n = len(x)
    season = SEASON.get(freq, 1)
    method = "Mann-Kendall corrected for autocorrelation"
    log_scale = bool(n >= 10 and (x > 0).all() and x.max() / x.min() >= 2)
    xt = np.log(x) if log_scale else x.copy()  # the series the test runs on
    if log_scale:
        method += ", on a log scale (the values grow in proportion)"
    if season > 1 and n >= 2 * season + 2:
        pos = np.arange(n) % season
        x = x - pd.Series(x).groupby(pos).transform("mean").to_numpy() + x.mean()
        xt = xt - pd.Series(xt).groupby(pos).transform("mean").to_numpy() + xt.mean()
        method += ", seasonal pattern removed"
    t = np.arange(n)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        slope, _, low, high = stats.theilslopes(x, t, 0.95)  # in the data's own units
        slope_t = stats.theilslopes(xt, t)[0] if log_scale else slope
        z = mann_kendall_z(xt)
        if n >= 10:
            resid = xt - slope_t * t
            r1 = np.corrcoef(resid[:-1], resid[1:])[0, 1] if np.std(resid) > 0 else 0.0
            r1 = min(max((np.nan_to_num(r1) * n + 1) / (n - 4), 0.0), 0.95)
            z = z / np.sqrt((1 + r1) / (1 - r1))
        else:
            method = "Mann-Kendall"
    p = float(2 * stats.norm.sf(abs(z))) if np.isfinite(z) else 1.0
    d = np.diff(xt)
    d = d[d != 0]
    if len(d) >= 5:
        p_sign = float(stats.binomtest(int((d > 0).sum()), len(d), 0.5).pvalue)
        p = min(1.0, 2 * min(p, p_sign))
        method += f", with a sign test on the changes ({int((d > 0).sum())} of {len(d)} were rises)"
    level = float(np.mean(np.abs(y.to_numpy(float))))
    return {"p": p, "slope": float(slope), "low": float(low), "high": float(high),
            "relative": float(slope) / level if level else 0.0, "method": method}


def _season_facts(y: pd.Series, freq: str) -> dict:
    """How much of the variation the season explains, and its high and low, with 2+ full seasons."""
    season = SEASON.get(freq, 1)
    if season <= 1 or len(y) < 2 * season:
        return {}
    window = season + (season % 2 == 0)  # odd, centred
    trend = y.rolling(window, center=True, min_periods=season // 2 + 1).mean()
    detrended = y - trend
    position = _positions(y.index, freq)
    pattern = detrended.groupby(position).mean()
    seasonal = pd.Series(position, index=y.index).map(pattern)
    rest = detrended - seasonal
    both = (seasonal + rest).dropna()
    strength = max(0.0, 1 - float(rest.dropna().var()) / float(both.var())) if both.var() > 0 else 0.0
    words = "strong" if strength >= 0.6 else "moderate" if strength >= 0.3 else "weak or none"
    out = {"seasonality": {"strength": number(strength, 2), "meaning": f"{words} ({_season_word(freq)} pattern; "
                           "0 = none, 1 = the season explains all the variation once the trend is out)"}}
    if strength >= 0.3:
        out["season_high"] = _position_name(pattern.idxmax(), freq)
        out["season_low"] = _position_name(pattern.idxmin(), freq)
    return out


def _positions(index: pd.DatetimeIndex, freq: str) -> np.ndarray:
    return {"D": index.dayofweek, "B": index.dayofweek, "W": np.minimum(index.isocalendar().week.to_numpy(), 52),
            "M": index.month, "Q": index.quarter}[freq]


def _season_word(freq: str) -> str:
    return {"D": "weekly", "B": "weekly", "W": "yearly, by week", "M": "yearly", "Q": "yearly"}[freq]


def _position_name(p, freq: str) -> str:
    p = int(p)
    return {"D": lambda: WEEKDAYS[p], "B": lambda: WEEKDAYS[p], "W": lambda: f"week {p}", "M": lambda: MONTHS[p - 1],
            "Q": lambda: f"Q{p}"}[freq]()


def _latest(y: pd.Series, freq: str) -> dict:
    """The latest period, and the same period a year earlier when the history has it."""
    out = {"latest": [y.index[-1].strftime("%Y-%m-%d"), number(float(y.iloc[-1]))]}
    year_ago = y.index[-1] - pd.DateOffset(years=1)
    if freq in ("W", "M", "Q") and year_ago in y.index and y[year_ago]:
        before = float(y[year_ago])
        out["year_earlier"] = [year_ago.strftime("%Y-%m-%d"), number(before)]
        out["change_vs_year_earlier_pct"] = number(100 * (float(y.iloc[-1]) - before) / abs(before), 2)
    return out


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
