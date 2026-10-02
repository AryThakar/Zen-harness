"""Test 3 - forecast on real daily revenue. Six 30-day windows at the end of each series;
for each the app sees only the data before the window, then is scored on what happened."""

from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd

from app.ml import forecast
from app.ml.base import NotEnoughData, daily_revenue

from common import WORK, snapshot

H = 30
WINDOWS = 6
SERIES = [("retail", None), ("coffee", None), ("pizza", None), ("cdnow", None)]


def wape(actual, predicted) -> float:
    return float(np.abs(actual - predicted).sum() / np.abs(actual).sum())


def one(name: str, by: str | None, value: str | None, cutoff: pd.Timestamp, full: pd.Series) -> dict:
    con = snapshot(name, end=cutoff)
    try:
        res = forecast.run(con, horizon=H, grain="day", by=by, value=value)
    except NotEnoughData as exc:
        return {"skipped": str(exc)}
    finally:
        con.close()
    pts = res.tables["forecast_points"]
    days = pd.to_datetime(pts["bucket"])
    actual = full.reindex(days, fill_value=0.0).to_numpy()
    # Baseline from PLAN.md: the same weekday in the last week before the cutoff
    # (repeat the last 7 days); extra context: the average of the last 4 same weekdays.
    last_seen = cutoff - pd.Timedelta(days=1)
    naive, avg4 = [], []
    for d in days:
        weeks = max(1, int(np.ceil((d - last_seen).days / 7)))
        same = [d - pd.Timedelta(days=7 * (weeks + j)) for j in range(4)]
        values = [float(full.get(s, 0.0)) for s in same]
        naive.append(values[0])
        avg4.append(np.mean(values))
    naive, avg4 = np.array(naive), np.array(avg4)
    app, base, ctx = wape(actual, pts["predicted"].to_numpy()), wape(actual, naive), wape(actual, avg4)
    m = res.metrics
    inside = ((actual >= pts["lower"].to_numpy()) & (actual <= pts["upper"].to_numpy())).mean()
    return {
        "model": m["model"], "app_wape": app, "naive_wape": base, "avg4_wape": ctx,
        "real_improvement_pct": 100 * (1 - app / base),
        "claimed_improvement_pct": m["improvement_pct"] or 0.0,
        "claimed_error_pct": m["error_pct"], "range_coverage": float(inside),
    }


def main(names=None):
    rows = []
    series = list(SERIES)
    con = snapshot("coffee")
    stores = [r[0] for r in con.execute("SELECT DISTINCT location FROM sales ORDER BY 1").fetchall()]
    con.close()
    series += [("coffee", s) for s in stores]
    for name, store in series:
        if names and name not in names:
            continue
        con = snapshot(name)
        full = daily_revenue(con, "location" if store else None, store)
        con.close()
        last = full.index[-1]
        for k in range(1, WINDOWS + 1):
            cutoff = last + pd.Timedelta(days=1) - pd.Timedelta(days=H * k)
            r = one(name, "location" if store else None, store, cutoff, full)
            r.update({"series": name + (f" / {store}" if store else ""), "cutoff": str(cutoff.date())})
            rows.append(r)
            print(json.dumps(r, default=str), flush=True)
    (WORK / "t3_forecast.json").write_text(json.dumps(rows, indent=2, default=str))


if __name__ == "__main__":
    main(sys.argv[1:] or None)
