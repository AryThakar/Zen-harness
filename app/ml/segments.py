"""Customer segments from RFM: Recency (days since last purchase), Frequency
(orders) and Monetary value (net revenue).

KMeans groups customers with similar RFM. Silhouette score measures how well
separated the groups are, but it almost always prefers 3 groups - too coarse to
act on - so we take the most groups (up to 6) whose silhouette is within 10% of
the best. Each group is then named by where its median customer sits compared
with all customers, so names mean the same thing on every dataset.
"""

from __future__ import annotations

import duckdb
import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score
from sklearn.preprocessing import StandardScaler

from .base import NotEnoughData, TaskResult

MIN_CUSTOMERS = 50
K_RANGE = range(3, 7)
SILHOUETTE_TOLERANCE = 0.9  # accept a finer split while it keeps 90% of the best separation
SILHOUETTE_SAMPLE = 5000

SEGMENTS = {  # name -> (what it means, what to do)
    "Champions": ("Bought recently, buy often and spend the most.", "Reward them and ask for reviews or referrals."),
    "Loyal": ("Buy regularly and spend well.", "Offer loyalty perks and suggest related products."),
    "New": ("Bought recently but only a few times so far.", "Welcome them and encourage a second order."),
    "At risk": ("Used to buy often or spend a lot, but not recently.", "Win them back with a personal offer."),
    "Lost": ("Haven't bought for a long time and bought little.", "Try one win-back campaign, then let go."),
    "Occasional": ("Bought once or twice a while ago and spent little.", "Nudge them with a timely, relevant offer."),
}


def rfm(cur: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """One row per customer: recency_days, orders, revenue (returns included)."""
    return cur.execute(
        """
        SELECT customer_id,
               date_diff('day', max(order_date), (SELECT max(order_date) FROM sales))::INTEGER AS recency_days,
               count(DISTINCT order_id) FILTER (WHERE NOT is_return)::INTEGER AS orders,
               sum(revenue) AS revenue
        FROM sales
        WHERE customer_id IS NOT NULL
        GROUP BY customer_id
        HAVING count(*) FILTER (WHERE NOT is_return) > 0
        ORDER BY customer_id
        """
    ).df()


def run(cur: duckdb.DuckDBPyConnection) -> TaskResult:
    customers = rfm(cur)
    if len(customers) < MIN_CUSTOMERS:
        raise NotEnoughData(
            f"Customer segments need at least {MIN_CUSTOMERS} customers with a customer_id; "
            f"there {'is' if len(customers) == 1 else 'are'} {len(customers)}."
        )

    # Orders and spend are heavily skewed (a few big customers), so compare them on a log scale.
    features = np.column_stack([
        customers["recency_days"],
        np.log1p(customers["orders"]),
        np.log1p(customers["revenue"].clip(lower=0)),
    ])
    scaled = StandardScaler().fit_transform(features)

    candidates = {}
    for k in K_RANGE:
        if k >= len(customers):
            break
        model = KMeans(n_clusters=k, n_init=10, random_state=0).fit(scaled)
        score = silhouette_score(scaled, model.labels_, sample_size=min(SILHOUETTE_SAMPLE, len(customers)),
                                 random_state=0)
        candidates[k] = (float(score), model.labels_)
    top = max(score for score, _ in candidates.values())
    k = max(k for k, (score, _) in candidates.items() if score >= SILHOUETTE_TOLERANCE * top)
    silhouette, labels = candidates[k]
    customers["cluster"] = labels

    names = {c: _name(customers, customers[customers["cluster"] == c]) for c in range(k)}
    customers["segment"] = customers["cluster"].map(names)

    summary = (
        customers.groupby("segment")
        .agg(customers=("customer_id", "size"), revenue=("revenue", "sum"))
        .sort_values("revenue", ascending=False)
    )
    total_revenue = customers["revenue"].sum()
    sizes = {seg: int(n) for seg, n in summary["customers"].items()}
    metrics = {
        "customers": len(customers),
        "clusters": k,
        "silhouette": silhouette,
        "silhouette_by_k": {str(n): round(s, 4) for n, (s, _) in candidates.items()},
        "segments": sizes,
        "revenue_share_pct": {seg: 100 * float(r) / total_revenue if total_revenue else None
                              for seg, r in summary["revenue"].items()},
    }
    leader = summary.index[0]
    message = (
        f"{len(customers):,} customers in {len(sizes)} segments: "
        + ", ".join(f"{seg} {n:,}" for seg, n in sizes.items())
        + f". {leader} bring {metrics['revenue_share_pct'][leader]:.0f}% of revenue."
    )
    rows = customers[["customer_id", "recency_days", "orders", "revenue", "cluster", "segment"]]
    return TaskResult(metrics=metrics, message=message, tables={"customer_segments": rows})


def _name(everyone: pd.DataFrame, group: pd.DataFrame) -> str:
    """Name a cluster from the percentiles of its median customer."""
    recent = 1 - _percentile(everyone["recency_days"], group["recency_days"].median())  # fewer days = better
    frequent = _percentile(everyone["orders"], group["orders"].median())
    valuable = _percentile(everyone["revenue"], group["revenue"].median())
    if recent >= 0.5 and frequent >= 0.66 and valuable >= 0.66:
        return "Champions"
    good_customer = frequent >= 0.5 or valuable >= 0.5
    if good_customer:
        return "Loyal" if recent >= 0.4 else "At risk"
    if recent >= 0.6:
        return "New"
    if recent < 0.3:
        return "Lost"
    return "Occasional"


def _percentile(values: pd.Series, x: float) -> float:
    """Share of customers at or below x, counting ties as half (so 1 order out of
    a crowd with 1 order sits in the middle, not at the top)."""
    below = (values < x).mean()
    equal = (values == x).mean()
    return float(below + equal / 2)
