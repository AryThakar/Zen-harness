"""Unusual days and unusual sale lines.

Days: each day's orders and revenue are compared with what is normal for that
weekday at that time (a weekday profile times a rolling 4-week level), and a robust
z-score of the difference flags spikes and drops. Robust = median and MAD instead
of mean and standard deviation, so the unusual days themselves don't hide the next
ones. Both signals are checked because revenue alone is noisy - one laptop sale
can double a day - while order counts are steadier; a flash sale shows up in
orders long before it stands out in revenue.

Lines: an IsolationForest looks for sale lines whose price, quantity or discount
is far from what that product usually sells at - often a typing mistake, sometimes
a bulk order or a pricing error.
"""

from __future__ import annotations

import duckdb
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest

from .base import NotEnoughData, TaskResult, daily_orders, daily_revenue

MIN_DAYS = 28
THRESHOLD = 3.5          # robust z-score beyond which a day is flagged
LEVEL_WINDOW = 29        # days in the rolling level (centred)
MAX_LINES = 25
MIN_LINES = 200


def run(cur: duckdb.DuckDBPyConnection) -> TaskResult:
    revenue = daily_revenue(cur)
    if len(revenue) < MIN_DAYS:
        raise NotEnoughData(
            f"Finding unusual days needs at least {MIN_DAYS} days of sales; there are {len(revenue)}."
        )
    orders = daily_orders(cur).reindex(revenue.index, fill_value=0.0)
    days = unusual_days(revenue, orders)
    lines = unusual_lines(cur)

    spikes = int((days["direction"] == "spike").sum())
    drops = int((days["direction"] == "drop").sum())
    metrics = {
        "days_checked": len(revenue),
        "spikes": spikes,
        "drops": drops,
        "unusual_lines": len(lines),
        "threshold": THRESHOLD,
    }
    parts = [f"Checked {len(revenue):,} days: {spikes} unusual spike(s) and {drops} unusual drop(s)."]
    if len(days):
        worst = days.loc[days["score"].abs().idxmax()]
        parts.append(
            f"The biggest was {worst['day']:%d %b %Y}: orders {_change(worst['orders'], worst['expected_orders'])}, "
            f"revenue {_change(worst['revenue'], worst['expected_revenue'])} vs normal."
        )
    if len(lines):
        parts.append(f"{len(lines)} sale line(s) look unusual for their product.")
    return TaskResult(
        metrics=metrics,
        message=" ".join(parts),
        tables={"anomaly_days": days, "anomaly_lines": lines},
    )


def unusual_days(revenue: pd.Series, orders: pd.Series) -> pd.DataFrame:
    """Days where orders or revenue were far from normal."""
    expected_revenue, z_revenue = _score(revenue)
    expected_orders, z_orders = _score(orders)
    by_orders = z_orders.abs() >= THRESHOLD
    by_revenue = z_revenue.abs() >= THRESHOLD
    flagged = by_orders | by_revenue
    score = z_orders.where(z_orders.abs() >= z_revenue.abs(), z_revenue)
    signal = np.select([by_orders & by_revenue, by_orders], ["both", "orders"], "revenue")
    return pd.DataFrame({
        "day": revenue.index[flagged].date,
        "revenue": revenue[flagged].to_numpy(),
        "expected_revenue": expected_revenue[flagged].to_numpy(),
        "orders": orders[flagged].astype(int).to_numpy(),
        "expected_orders": expected_orders[flagged].to_numpy(),
        "signal": signal[flagged.to_numpy()],
        "score": score[flagged].to_numpy(),
        "direction": np.where(score[flagged] > 0, "spike", "drop"),
    })


def _score(series: pd.Series) -> tuple[pd.Series, pd.Series]:
    """(expected value, robust z-score) per day for one daily series."""
    weekday = series.index.weekday
    overall = series[series > 0].median()
    profile = series.groupby(weekday).median() / overall if overall else pd.Series(1.0, index=range(7))
    factor = pd.Series(profile.reindex(weekday).to_numpy(), index=series.index)

    # Weekdays the business is normally closed have factor 0: expected is 0 there.
    level = (series / factor.where(factor > 0)).rolling(LEVEL_WINDOW, center=True, min_periods=7).median()
    expected = (level * factor).fillna(0.0)

    # Compare on a log scale so a 50% drop on a small day counts like one on a big day.
    diff = np.log1p(series.clip(lower=0)) - np.log1p(expected.clip(lower=0))
    mad = (diff - diff.median()).abs().median()
    scale = 1.4826 * mad if mad else diff.std() or 1.0
    return expected, (diff - diff.median()) / scale


def _change(actual: float, expected: float) -> str:
    return f"{100 * (actual / expected - 1):+.0f}%" if expected else "n/a"


def unusual_lines(cur: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """The most unusual sale lines compared with how their product usually sells."""
    lines = cur.execute(
        """
        WITH typical AS (
            SELECT product, median(unit_price) AS price, median(quantity) AS qty, count(*) AS n
            FROM sales WHERE NOT is_return AND unit_price > 0 GROUP BY product
        )
        SELECT s.sale_id, s.product, s.unit_price, s.quantity, s.discount,
               t.price AS typical_price, t.qty AS typical_qty
        FROM sales s JOIN typical t USING (product)
        WHERE NOT s.is_return AND s.unit_price > 0 AND t.n >= 5
        """
    ).df()
    empty = pd.DataFrame({"sale_id": pd.Series(dtype="int64"), "score": pd.Series(dtype=float),
                          "reason": pd.Series(dtype=str)})
    if len(lines) < MIN_LINES:
        return empty

    price_ratio = np.log(lines["unit_price"] / lines["typical_price"])
    qty_ratio = np.log(lines["quantity"] / lines["typical_qty"])
    discount_rate = (lines["discount"] / (lines["quantity"] * lines["unit_price"])).clip(0, 1)
    features = np.column_stack([price_ratio, qty_ratio, discount_rate])

    forest = IsolationForest(n_estimators=200, contamination="auto", random_state=0).fit(features)
    score = -forest.score_samples(features)  # higher = more unusual
    outliers = forest.predict(features) == -1
    # Only lines that are actually off in some way: a typical price, quantity and
    # discount is never worth reporting, whatever the forest's score.
    off = (price_ratio.abs() > np.log(1.5)) | (qty_ratio.abs() > np.log(3)) | (discount_rate > 0.3)
    candidates = lines[outliers & off].assign(score=score[outliers & off])
    if candidates.empty:
        return empty
    top = candidates.nlargest(MAX_LINES, "score")
    return pd.DataFrame({
        "sale_id": top["sale_id"].astype("int64").to_numpy(),
        "score": top["score"].to_numpy(),
        "reason": [_reason(r) for r in top.itertuples()],
    })


def _reason(line) -> str:
    parts = []
    price = line.unit_price / line.typical_price
    if abs(np.log(price)) > np.log(1.5):
        parts.append(f"price {price:.1f}x the usual" if price > 1 else f"price {price:.0%} of the usual")
    qty = line.quantity / line.typical_qty
    if abs(np.log(qty)) > np.log(3):
        parts.append(f"quantity {qty:.0f}x the usual" if qty > 1 else f"quantity {qty:.0%} of the usual")
    rate = line.discount / (line.quantity * line.unit_price)
    if rate > 0.3:
        parts.append(f"{rate:.0%} discount")
    return ", ".join(parts)
