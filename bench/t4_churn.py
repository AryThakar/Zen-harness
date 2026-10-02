"""Test 4 - churn on real customers. Cut the data at T, let the app score customers, then
check who really bought nothing in the hidden h days after T."""

from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

from app.ml import churn
from app.ml.base import NotEnoughData

from common import WORK, sales, snapshot


def capture(y: np.ndarray, score: np.ndarray, share: float = 0.2) -> float:
    n = max(1, int(round(share * len(score))))
    top = np.argsort(-score, kind="stable")[:n]
    return 100 * float(y[top].sum() / max(1, y.sum()))


def one(name: str, h: int, back: int, full: pd.DataFrame) -> dict:
    last = full["order_date"].max().normalize()
    cutoff = last - pd.Timedelta(days=h * back)
    con = snapshot(name, end=cutoff + pd.Timedelta(days=1))  # data up to and including the cutoff day
    try:
        res = churn.run(con, horizon_days=h)
    except NotEnoughData as exc:
        return {"dataset": name, "h": h, "cutoff": str(cutoff.date()), "skipped": str(exc)}
    finally:
        con.close()
    scores = res.tables["churn_scores"]
    # The app's "today" is the last sale day it saw; its horizon starts there.
    seen_last = full.loc[full["order_date"] < cutoff + pd.Timedelta(days=1), "order_date"].max().normalize()
    window = full[(full["order_date"].dt.normalize() > seen_last)
                  & (full["order_date"].dt.normalize() <= seen_last + pd.Timedelta(days=h))
                  & (~full["is_return"])]
    came_back = set(window["customer_id"].dropna())
    y = np.array([0 if c in came_back else 1 for c in scores["customer_id"]])
    p = scores["probability"].to_numpy(dtype=float)
    recency = scores["recency_days"].to_numpy(dtype=float)
    m = res.metrics
    out = {"dataset": name, "h": h, "cutoff": str(cutoff.date()), "model": m["model"], "scored": len(scores),
           "left_pct": round(100 * y.mean(), 1)}
    if 0 < y.sum() < len(y):
        out.update({
            "real_auc": roc_auc_score(y, p), "real_baseline_auc": roc_auc_score(y, recency),
            "claimed_auc": m["auc"], "claimed_baseline_auc": m["baseline_auc"],
            "real_top20": capture(y, p), "real_baseline_top20": capture(y, recency),
            "claimed_top20": m["top20_capture_pct"],
            "high_risk_left_pct": round(100 * y[scores["risk"].to_numpy() == "high"].mean(), 1)
            if (scores["risk"] == "high").any() else None,
        })
    return out


def main(names=None):
    rows = []
    for name in names or ["retail", "cdnow"]:
        full = sales(name)
        full["order_date"] = pd.to_datetime(full["order_date"])
        for h in (30, 60, 90):
            for back in (1, 2):
                r = one(name, h, back, full)
                rows.append(r)
                print(json.dumps(r, default=str), flush=True)
    (WORK / "t4_churn.json").write_text(json.dumps(rows, indent=2, default=str))


if __name__ == "__main__":
    main(sys.argv[1:] or None)
