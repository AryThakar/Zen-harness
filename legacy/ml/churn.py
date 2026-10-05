"""Churn prediction: which active customers will probably buy nothing in the next
``horizon_days`` (30, 60 or 90).

Who is scored: repeat customers whose usual gap between purchases is shorter
than the horizon, and who bought within it - for them a quiet horizon would be a
break in their rhythm, which is what churn means. Not scored, only counted:
customers already quiet for longer (the segments' "At risk" and "Lost" groups),
infrequent buyers (a quiet 90 days is normal for someone who buys twice a year)
and one-time buyers (no rhythm yet).

How it learns, from the shop's own history:
1. Snapshots every ``horizon_days`` back from the latest sale. At each snapshot,
   features describe each active repeat customer using only the data up to it
   (days since last purchase, their usual gap, purchases and spend in the last
   and the previous period, returns...). The label is whether they then bought
   nothing in the following ``horizon_days``.
2. Test: models learn from the older snapshots and predict the most recent one,
   whose outcome is known. Candidates: logistic regression and gradient boosting,
   against a baseline that only knows days since last purchase - the rule a
   shopkeeper would use. The best is kept only if it beats that baseline.
3. The chosen model is refit on all snapshots and scores today's customers.

Each score comes with plain-language warning signs - facts about the customer
(overdue vs their usual gap, fewer purchases, lower spend, returns), not
model internals.
"""

from __future__ import annotations

import duckdb
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .base import NotEnoughData, TaskResult

HORIZONS = (30, 60, 90)
MAX_SNAPSHOTS = 6
MIN_CUSTOMERS = 100   # scored customers at the test snapshot
MIN_CLASS = 20        # customers who left, and who stayed, in training and in the test
HIGH, MEDIUM = 0.5, 0.25  # high = more likely than not to go quiet
MIN_IMPROVEMENT = 0.02  # AUC a model must gain over the baseline to be used (less is within test noise)

FEATURES = ["recency_days", "log_tenure", "log_gap", "overdue", "log_purchases", "rate_per_30d",
            "recent_purchases", "prior_purchases", "purchase_trend", "log_recent_revenue",
            "log_prior_revenue", "log_avg_basket", "return_ratio"]
MODELS = {"logistic": "logistic regression", "boosting": "gradient boosting", "baseline": "days since last purchase"}


def customer_days(cur: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """One row per customer and day: net revenue, items bought, items returned."""
    df = cur.execute(
        """
        SELECT customer_id, order_date::DATE AS day, sum(revenue) AS revenue,
               count(*) FILTER (WHERE NOT is_return) AS items,
               count(*) FILTER (WHERE is_return) AS returns
        FROM sales WHERE customer_id IS NOT NULL
        GROUP BY 1, 2 ORDER BY 1, 2
        """
    ).df()
    df["day"] = pd.to_datetime(df["day"])
    return df


def features(days: pd.DataFrame, snapshot: pd.Timestamp, horizon: int) -> pd.DataFrame:
    """Features at ``snapshot`` for customers with >= 2 purchase days, a usual gap
    of at most ``horizon`` days and the last purchase within ``horizon`` days
    (computed only from data up to the snapshot)."""
    past = days[days["day"] <= snapshot]
    bought = past[past["items"] > 0]
    if bought.empty:
        return pd.DataFrame(columns=FEATURES)
    g = bought.groupby("customer_id")["day"]
    out = pd.DataFrame({"first": g.min(), "last": g.max(), "purchases": g.size()})
    gaps = bought.assign(gap=g.diff().dt.days).groupby("customer_id")["gap"].median()
    out["typical_gap_days"] = gaps.reindex(out.index)
    out = out[(out["purchases"] >= 2) & (out["typical_gap_days"] <= horizon)
              & ((snapshot - out["last"]).dt.days <= horizon)].copy()
    if out.empty:
        return pd.DataFrame(columns=FEATURES)

    recent_start, prior_start = snapshot - pd.Timedelta(days=horizon), snapshot - pd.Timedelta(days=2 * horizon)
    per = past[past["customer_id"].isin(out.index)]
    in_recent = per["day"] > recent_start
    in_prior = (per["day"] > prior_start) & ~in_recent
    sums = lambda mask, col: per[mask].groupby("customer_id")[col].sum().reindex(out.index, fill_value=0)  # noqa: E731
    counts = lambda mask: per[mask & (per["items"] > 0)].groupby("customer_id").size().reindex(out.index, fill_value=0)  # noqa: E731

    everything = pd.Series(True, index=per.index)
    out["recency_days"] = (snapshot - out["last"]).dt.days
    out["tenure_days"] = (snapshot - out["first"]).dt.days
    out["recent_purchases"] = counts(in_recent)
    out["prior_purchases"] = counts(in_prior)
    out["recent_revenue"] = sums(in_recent, "revenue")
    out["prior_revenue"] = sums(in_prior, "revenue")
    out["revenue"] = sums(everything, "revenue")
    out["items"] = sums(everything, "items")
    out["returns"] = sums(everything, "returns")

    out["typical_gap_days"] = out["typical_gap_days"].clip(lower=1)
    out["log_tenure"] = np.log1p(out["tenure_days"])
    out["log_gap"] = np.log1p(out["typical_gap_days"])
    out["overdue"] = (out["recency_days"] / out["typical_gap_days"]).clip(upper=10)
    out["log_purchases"] = np.log1p(out["purchases"])
    out["rate_per_30d"] = 30 * out["purchases"] / out["tenure_days"].clip(lower=horizon)
    out["purchase_trend"] = out["recent_purchases"] - out["prior_purchases"]
    out["log_recent_revenue"] = np.log1p(out["recent_revenue"].clip(lower=0))
    out["log_prior_revenue"] = np.log1p(out["prior_revenue"].clip(lower=0))
    out["log_avg_basket"] = np.log1p((out["revenue"] / out["purchases"]).clip(lower=0))
    out["return_ratio"] = out["returns"] / out["items"].clip(lower=1)
    return out


def left(days: pd.DataFrame, customers: pd.Index, snapshot: pd.Timestamp, horizon: int) -> pd.Series:
    """1 if the customer bought nothing in the ``horizon`` days after ``snapshot``."""
    window = days[(days["day"] > snapshot) & (days["day"] <= snapshot + pd.Timedelta(days=horizon))
                  & (days["items"] > 0)]
    returned = set(window["customer_id"])
    return pd.Series([0 if c in returned else 1 for c in customers], index=customers)


def run(cur: duckdb.DuckDBPyConnection, horizon_days: int = 90) -> TaskResult:
    if horizon_days not in HORIZONS:
        raise ValueError(f"horizon_days must be one of {HORIZONS}")
    h = horizon_days
    days = customer_days(cur)
    if days.empty:
        raise NotEnoughData("Churn prediction needs a customer_id column: which customer made each purchase.")
    first_day, last_day = days["day"].min(), days["day"].max()

    snapshots = [last_day - pd.Timedelta(days=k * h) for k in range(1, MAX_SNAPSHOTS + 1)]
    snapshots = [s for s in snapshots if (s - first_day).days >= h]  # each needs some history before it
    if len(snapshots) < 2:
        need = 3 * h
        raise NotEnoughData(
            f"A {h}-day churn prediction needs at least {need} days of sales with customer ids, so it can learn "
            f"from one period and be tested on the next; there are {(last_day - first_day).days + 1}."
        )

    frames = []
    for k, snapshot in enumerate(snapshots, start=1):
        f = features(days, snapshot, h)
        if len(f):
            frames.append(f.assign(label=left(days, f.index, snapshot, h), snapshot=k))
    data = pd.concat(frames) if frames else pd.DataFrame(columns=[*FEATURES, "label", "snapshot"])
    test, train = data[data["snapshot"] == 1], data[data["snapshot"] > 1]
    _check_enough(train, test, h)

    y_train, y_test = train["label"].astype(int), test["label"].astype(int)
    scores = {}
    for name in MODELS:
        model = _model(name).fit(_x(train, name), y_train)
        scores[name] = model.predict_proba(_x(test, name))[:, 1]
    auc = {name: float(roc_auc_score(y_test, s)) for name, s in scores.items()}
    best = max(("logistic", "boosting"), key=auc.get)
    chosen = best if auc[best] >= auc["baseline"] + MIN_IMPROVEMENT else "baseline"

    # Refit on every snapshot and score today's active repeat customers.
    final = _model(chosen).fit(_x(data, chosen), data["label"].astype(int))
    now = features(days, last_day, h)
    probability = final.predict_proba(_x(now, chosen))[:, 1] if len(now) else np.array([])
    scored = _scores(now, probability, h)

    bought = days[days["items"] > 0]
    everyone = bought.groupby("customer_id")["day"].agg(["max", "nunique"])
    gap = bought.assign(gap=bought.groupby("customer_id")["day"].diff().dt.days).groupby("customer_id")["gap"].median()
    repeat = everyone["nunique"] >= 2
    one_time = int((~repeat).sum())
    infrequent = int((repeat & (gap.reindex(everyone.index) > h)).sum())
    lapsed = int((repeat & (gap.reindex(everyone.index) <= h) & ((last_day - everyone["max"]).dt.days > h)).sum())
    bands = scored["risk"].value_counts().reindex(["high", "medium", "low"], fill_value=0)
    metrics = {
        "horizon_days": h,
        "model": chosen,
        "model_label": MODELS[chosen],
        "auc": auc[chosen],
        "baseline_auc": auc["baseline"],
        "candidate_auc": {name: auc[name] for name in ("logistic", "boosting")},
        "top20_capture_pct": _capture(y_test, scores[chosen]),
        "baseline_top20_capture_pct": _capture(y_test, scores["baseline"]),
        "test_left_pct": 100 * float(y_test.mean()),
        "train_rows": len(train),
        "test_rows": len(test),
        "snapshots": len(snapshots),
        "scored": len(scored),
        "high": int(bands["high"]),
        "medium": int(bands["medium"]),
        "low": int(bands["low"]),
        "value_at_risk": float(scored.loc[scored["risk"] == "high", "value_at_risk"].sum()),
        "lapsed": lapsed,
        "infrequent": infrequent,
        "one_time": one_time,
    }
    return TaskResult(metrics, _message(metrics), {"churn_scores": scored})


def _check_enough(train: pd.DataFrame, test: pd.DataFrame, h: int) -> None:
    if len(test) < MIN_CUSTOMERS:
        raise NotEnoughData(
            f"Churn prediction needs at least {MIN_CUSTOMERS} regular customers (buying at least every {h} days) "
            f"active before the test date; there are {len(test)}. Try a longer horizon or add more history."
        )
    for name, part in (("training", train), ("test", test)):
        stayed, went = int((part["label"] == 0).sum()), int((part["label"] == 1).sum())
        if min(stayed, went) < MIN_CLASS:
            raise NotEnoughData(
                f"The {name} period has {went} customers who stopped buying and {stayed} who kept buying; "
                f"a fair test needs at least {MIN_CLASS} of each."
            )


def _model(name: str):
    if name == "boosting":
        return HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, max_leaf_nodes=15,
                                              min_samples_leaf=20, l2_regularization=1.0, random_state=0)
    return make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000))


def _x(frame: pd.DataFrame, name: str) -> np.ndarray:
    columns = ["recency_days"] if name == "baseline" else FEATURES
    return frame[columns].to_numpy(dtype=float)


def _capture(y: pd.Series, score: np.ndarray, share: float = 0.2) -> float:
    """Of the customers who actually left, the % found in the top ``share`` by score."""
    n = max(1, int(round(share * len(score))))
    top = np.argsort(-score, kind="stable")[:n]
    return 100 * float(y.to_numpy()[top].sum() / max(1, y.sum()))


def _scores(now: pd.DataFrame, probability: np.ndarray, h: int) -> pd.DataFrame:
    columns = ["customer_id", "probability", "risk", "recency_days", "typical_gap_days", "purchases",
               "recent_revenue", "revenue", "value_at_risk", "signals"]
    if now.empty:
        return pd.DataFrame(columns=columns)
    usual_spend = now["revenue"].clip(lower=0) / now["tenure_days"].clip(lower=h) * h
    out = pd.DataFrame({
        "customer_id": now.index,
        "probability": probability,
        "risk": np.where(probability >= HIGH, "high", np.where(probability >= MEDIUM, "medium", "low")),
        "recency_days": now["recency_days"].astype(int).to_numpy(),
        "typical_gap_days": now["typical_gap_days"].to_numpy(),
        "purchases": now["purchases"].astype(int).to_numpy(),
        "recent_revenue": now["recent_revenue"].to_numpy(),
        "revenue": now["revenue"].to_numpy(),
        "value_at_risk": (probability * usual_spend).to_numpy(),
        "signals": [_signals(row, h) for _, row in now.iterrows()],
    })
    return out.sort_values(["value_at_risk", "customer_id"], ascending=[False, True], ignore_index=True)[columns]


def _signals(c: pd.Series, h: int) -> str:
    signs = []
    if c["recency_days"] >= 14 and c["overdue"] >= 1.5:
        signs.append(f"last bought {int(c['recency_days'])} days ago; usually every {c['typical_gap_days']:.0f} days")
    if c["recent_purchases"] < c["prior_purchases"]:
        signs.append(f"{int(c['recent_purchases'])} purchase days in the last {h} days vs "
                     f"{int(c['prior_purchases'])} in the {h} before")
    if c["prior_revenue"] > 0 and c["recent_revenue"] < 0.5 * c["prior_revenue"]:
        drop = 100 * (1 - max(c["recent_revenue"], 0) / c["prior_revenue"])
        signs.append(f"spent {drop:.0f}% less than in the previous {h} days")
    if c["returns"] >= 2 and c["return_ratio"] >= 0.2:
        signs.append(f"returned {int(c['returns'])} of {int(c['items'])} items")
    if c["purchases"] == 2:
        signs.append("only 2 purchase days so far")
    return "; ".join(signs) if signs else "no warning signs"


def _message(m: dict) -> str:
    h = m["horizon_days"]
    head = (f"{m['high']:,} of {m['scored']:,} regular customers are likely to buy nothing in the next {h} days "
            f"(more likely than not); {m['medium']:,} more are at medium risk ({MEDIUM:.0%}-{HIGH:.0%} chance).")
    if m["model"] == "baseline":
        quality = (f"No model ranked customers better than simply 'longest since last purchase' "
                   f"(AUC {m['baseline_auc']:.2f}), so scores use that.")
    else:
        quality = (f"The {m['model_label']} model ranks customers better than 'longest since last purchase' "
                   f"(AUC {m['auc']:.2f} vs {m['baseline_auc']:.2f}): its top 20% held "
                   f"{m['top20_capture_pct']:.0f}% of the customers who then stopped buying "
                   f"(vs {m['baseline_top20_capture_pct']:.0f}%), tested on the most recent {h} days.")
    auc = m["auc"]
    verdict = ("Scores are barely better than guessing - treat them as a rough hint." if auc < 0.6 else
               "Scores are a useful but rough guide." if auc < 0.7 else "Scores rank customers well.")
    rest = (f"Not scored: {m['lapsed']:,} already quiet for more than {h} days, {m['infrequent']:,} who usually "
            f"buy less than once every {h} days, and {m['one_time']:,} one-time buyers.")
    return f"{head} {quality} {verdict} {rest}"
