"""Sales forecast.

Backtests two time-series models against a simple baseline on the business's own
history (walk-forward: forecast a past period, compare with what really happened,
repeat), keeps the most accurate, and forecasts ahead with an 80% range. If
neither model beats the baseline, the baseline is used - and the result says so.
"""

from __future__ import annotations

import warnings

import duckdb
import numpy as np
import pandas as pd
from statsforecast import StatsForecast
from statsforecast.models import AutoETS, AutoTheta, SeasonalNaive

from .base import NotEnoughData, TaskResult, daily_revenue

GRAINS = {"day": {"freq": "D", "season": 7, "min_history": 56},
          "week": {"freq": "W-MON", "season": 1, "min_history": 12}}
FILTERABLE = ("category", "product", "location", "channel")
MAX_WINDOWS = 3
LABELS = {
    "baseline": "same as the previous period",
    "AutoETS": "exponential smoothing (ETS)",
    "AutoTheta": "Theta method",
}


def run(
    cur: duckdb.DuckDBPyConnection,
    horizon: int = 30,
    grain: str = "day",
    by: str | None = None,
    value: str | None = None,
) -> TaskResult:
    if grain not in GRAINS:
        raise ValueError(f"grain must be one of {list(GRAINS)}")
    if by is not None and by not in FILTERABLE:
        raise ValueError(f"by must be one of {FILTERABLE}")
    spec = GRAINS[grain]
    series = daily_revenue(cur, by, value)
    if grain == "week":
        series = full_weeks(series)

    unit = "days" if grain == "day" else "weeks"
    history = len(series)
    needed = max(3 * horizon, spec["min_history"])
    if history < needed:
        subject = f" for {by} '{value}'" if by else ""
        raise NotEnoughData(
            f"A {horizon}-{unit[:-1]} forecast{subject} needs at least {needed} {unit} of sales history; "
            f"there {'is' if history == 1 else 'are'} {history}."
        )

    df = pd.DataFrame({"unique_id": "sales", "ds": series.index, "y": series.to_numpy()})
    season = spec["season"]
    sf = StatsForecast(
        models=[SeasonalNaive(season_length=season, alias="baseline"),
                AutoETS(season_length=season), AutoTheta(season_length=season)],
        freq=spec["freq"], n_jobs=1,
    )
    windows = max(1, min(MAX_WINDOWS, (history - 2 * horizon) // horizon + 1))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        cv = sf.cross_validation(df=df, h=horizon, step_size=horizon, n_windows=windows, level=[80])
        forecast = sf.forecast(df=df, h=horizon, level=[80])

    scores = {m: _wape(cv["y"], cv[m]) for m in LABELS}
    coverage = {m: float(((cv["y"] >= cv[f"{m}-lo-80"]) & (cv["y"] <= cv[f"{m}-hi-80"])).mean()) for m in LABELS}
    best = min(("AutoETS", "AutoTheta"), key=lambda m: scores[m])
    if scores[best] >= scores["baseline"]:
        best = "baseline"

    points = pd.DataFrame({
        "bucket": pd.to_datetime(forecast["ds"]).dt.date,
        "predicted": forecast[best].clip(lower=0).to_numpy(),
        "lower": forecast[f"{best}-lo-80"].clip(lower=0).to_numpy(),
        "upper": forecast[f"{best}-hi-80"].clip(lower=0).to_numpy(),
    })
    improvement = None
    if best != "baseline" and scores["baseline"]:
        improvement = 100 * (1 - scores[best] / scores["baseline"])
    expected_total = float(points["predicted"].sum())

    metrics = {
        "model": best,
        "model_label": LABELS[best],
        "error_pct": 100 * scores[best],
        "baseline_error_pct": 100 * scores["baseline"],
        "improvement_pct": improvement,
        "range_coverage_pct": 100 * coverage[best],
        "backtest_windows": windows,
        "horizon": horizon,
        "grain": grain,
        "history": history,
        "history_start": series.index[0].date().isoformat(),
        "history_end": series.index[-1].date().isoformat(),
        "by": by,
        "value": value,
        "expected_total": expected_total,
    }
    subject = f" for {by} '{value}'" if by else ""
    if best == "baseline":
        verdict = ("Neither model beat simply repeating the previous period in backtests, "
                   "so that is what this forecast does.")
    else:
        verdict = (f"The {LABELS[best]} model was {improvement:.0f}% more accurate than repeating the previous "
                   f"period in backtests (error {100 * scores[best]:.1f}% vs {100 * scores['baseline']:.1f}%).")
    message = f"Expected revenue{subject} over the next {horizon} {unit}: {expected_total:,.0f}. {verdict}"
    return TaskResult(metrics=metrics, message=message, tables={"forecast_points": points})


def full_weeks(daily: pd.Series) -> pd.Series:
    """Monday-to-Sunday totals, leaving out weeks the data only partly covers."""
    if daily.empty:
        return daily
    weekly = daily.resample("W-MON", label="left", closed="left").sum()
    first, last = daily.index[0], daily.index[-1]
    if first.weekday() != 0:
        weekly = weekly.iloc[1:]
    if last.weekday() != 6:
        weekly = weekly.iloc[:-1]
    return weekly


def _wape(actual: pd.Series, predicted: pd.Series) -> float:
    """Weighted absolute percentage error: total absolute error / total actual.
    Unlike MAPE it doesn't explode on days with little or no revenue."""
    total = float(np.abs(actual).sum())
    return float(np.abs(actual - predicted).sum()) / total if total else float("inf")
