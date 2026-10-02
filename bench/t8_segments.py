"""Test 8 - customer segments. Segments made at a cutoff vs what each customer spent in
the next 90 days. Better groups should spend more later; compared with plain RFM-score
quintiles (the textbook rule) by rank correlation with future spend."""

from __future__ import annotations

import json
import sys

import pandas as pd
from scipy.stats import spearmanr

from app.ml import segments

from common import WORK, sales, snapshot

AHEAD = 90
# Order fixed before running: how good each segment name claims the customers are.
RANK = {"Champions": 6, "Loyal": 5, "New": 4, "At risk": 3, "Occasional": 2, "Lost": 1}
BETTER, WORSE = ("Champions", "Loyal"), ("At risk", "Lost", "Occasional")


def run(name: str) -> dict:
    full = sales(name)
    full["order_date"] = pd.to_datetime(full["order_date"])
    cutoff = full["order_date"].max().normalize() - pd.Timedelta(days=AHEAD - 1)
    con = snapshot(name, end=cutoff)
    res = segments.run(con)
    con.close()
    seg = res.tables["customer_segments"].set_index("customer_id")
    later = full[full["order_date"] >= cutoff].groupby("customer_id")["revenue"].sum()
    seg["future"] = later.reindex(seg.index).fillna(0.0).clip(lower=0)
    seg["bought_again"] = later.reindex(seg.index).fillna(0.0) > 0

    # RFM quintile score: 1-5 for each of recency (fewer days better), orders, revenue.
    q = lambda s: pd.qcut(s.rank(method="first"), 5, labels=False) + 1  # noqa: E731
    rfm = q(-seg["recency_days"]) + q(seg["orders"]) + q(seg["revenue"])
    by_segment = seg.groupby("segment").agg(customers=("future", "size"), future_spend=("future", "mean"),
                                            bought_again_pct=("bought_again", "mean"))
    by_segment["bought_again_pct"] *= 100
    better = by_segment.loc[by_segment.index.intersection(BETTER), "future_spend"]
    worse = by_segment.loc[by_segment.index.intersection(WORSE), "future_spend"]
    return {
        "dataset": name, "cutoff": str(cutoff.date()), "customers": len(seg), "clusters": res.metrics["clusters"],
        "segments": by_segment.round(2).reset_index().to_dict("records"),
        "better_beat_worse": bool(len(better) and len(worse) and better.min() > worse.max()),
        "spearman_segment_rank": float(spearmanr(seg["segment"].map(RANK), seg["future"])[0]),
        "spearman_rfm_quintiles": float(spearmanr(rfm, seg["future"])[0]),
    }


def main(names=None):
    rows = []
    for name in names or ["retail", "cdnow"]:
        rows.append(run(name))
        print(json.dumps(rows[-1], default=str), flush=True)
    (WORK / "t8_segments.json").write_text(json.dumps(rows, indent=2, default=str))


if __name__ == "__main__":
    main(sys.argv[1:] or None)
