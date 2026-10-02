"""Test 6 - unusual days. Real data with 10 injected days per run: a spike copies all of that
day's orders once (orders and revenue double), a drop removes 70% of that day's orders.
Scored: how many injected days are flagged, and whether other days get more flags than
on the untouched data. A simple rule runs on the same data for comparison."""

from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd

from app.ml.anomalies import unusual_days
from app.ml.base import daily_orders, daily_revenue

from common import WORK, snapshot

INJECTED = 10
SEEDS = 3


def app_flags(con) -> set:
    revenue = daily_revenue(con)
    orders = daily_orders(con).reindex(revenue.index, fill_value=0.0)
    days = unusual_days(revenue, orders)
    return {pd.Timestamp(d) for d in days["day"]}


def simple_flags(con) -> set:
    """Baseline: each day vs the median of the same weekday in the previous 8 weeks,
    scaled by the MAD of those 8 days; flag |z| >= 3 on orders or on revenue."""
    revenue = daily_revenue(con)
    orders = daily_orders(con).reindex(revenue.index, fill_value=0.0)
    flagged = set()
    for s in (revenue, orders):
        values = s.to_numpy()
        for i in range(56, len(s)):
            past = values[i - 56:i][::-1][6::7]  # same weekday, last 8 weeks
            med = np.median(past)
            mad = 1.4826 * np.median(np.abs(past - med))
            scale = mad if mad > 0 else max(abs(med) * 0.1, 1.0)
            if abs(values[i] - med) / scale >= 3:
                flagged.add(s.index[i])
    return flagged


def inject(df: pd.DataFrame, spikes: list, drops: list, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    day = df["order_date"].dt.normalize()
    parts = [df]
    for d in spikes:
        copy = df[day == d].copy()
        copy["order_id"] = copy["order_id"].astype(str) + "-copy"
        copy["sale_id"] = copy["sale_id"] + 10_000_000
        parts.append(copy)
    out = pd.concat(parts, ignore_index=True)
    day = out["order_date"].dt.normalize()
    for d in drops:
        ids = out.loc[day == d, "order_id"].unique()
        gone = rng.choice(ids, size=int(round(0.7 * len(ids))), replace=False)
        out = out[~((day == d) & out["order_id"].isin(gone))]
        day = out["order_date"].dt.normalize()
    return out


def run(name: str) -> list[dict]:
    con = snapshot(name)
    base_app, base_simple = app_flags(con), simple_flags(con)
    revenue = daily_revenue(con)
    con.close()
    # Candidate days: had sales, not already flagged by either method, away from the ends
    # (the app's rolling level and the simple rule's 8 weeks need history).
    days = revenue.index[60:-15]
    candidates = [d for d in days if revenue[d] > 0 and d not in base_app and d not in base_simple]
    rows = []
    for seed in range(SEEDS):
        rng = np.random.default_rng(100 + seed)
        chosen = []
        for d in rng.permutation(candidates):
            if all(abs((d - c).days) >= 7 for c in chosen):
                chosen.append(pd.Timestamp(d))
            if len(chosen) == INJECTED:
                break
        spikes, drops = chosen[: INJECTED // 2], chosen[INJECTED // 2:]
        con = snapshot(name, transform=lambda df: inject(df, spikes, drops, seed))
        got_app, got_simple = app_flags(con), simple_flags(con)
        con.close()
        for method, got, base in (("app", got_app, base_app), ("simple rule", got_simple, base_simple)):
            rows.append({
                "dataset": name, "seed": seed, "method": method,
                "spikes_found": sum(d in got for d in spikes), "drops_found": sum(d in got for d in drops),
                "injected": INJECTED,
                "other_flags_after": len(got - set(chosen)), "flags_before": len(base),
                "days": len(revenue),
            })
            print(json.dumps(rows[-1], default=str), flush=True)
    return rows


def main(names=None):
    rows = []
    for name in names or ["retail", "coffee", "pizza", "cdnow"]:
        rows += run(name)
    (WORK / "t6_anomaly_days.json").write_text(json.dumps(rows, indent=2, default=str))


if __name__ == "__main__":
    main(sys.argv[1:] or None)
