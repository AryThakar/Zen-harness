"""Frequently bought together: pairs of products (and of categories) that appear
in the same order more often than chance.

For a pair X -> Y over all orders with real order numbers:
    support    = orders with X and Y / all orders
    confidence = orders with X and Y / orders with X       ("62% of phone orders include a case")
    lift       = confidence / (orders with Y / all orders)  ("4.1x more often than usual")

A pair is kept when it was bought together at least ``min_orders`` times, with
lift >= 1.2, and clearly above chance: its count must exceed the independent
expectation by 3 standard deviations (Poisson), so a coincidence among a few
orders isn't reported as a pattern. Only order numbers from the file count -
generated ones (one per line) can't show baskets.
"""

from __future__ import annotations

import math

import duckdb
import pandas as pd

from .base import NotEnoughData, TaskResult

MIN_MULTI_ITEM_ORDERS = 100
MIN_LIFT = 1.2
Z = 3.0
MAX_BASKET = 50  # larger orders (wholesale, bulk) are left out of pair counting


def run(cur: duckdb.DuckDBPyConnection) -> TaskResult:
    orders, multi = cur.execute(
        """
        WITH baskets AS (
            SELECT order_id, count(DISTINCT product) AS items FROM sales
            WHERE NOT is_return AND NOT order_id_generated GROUP BY order_id
        )
        SELECT count(*), count(*) FILTER (WHERE items >= 2) FROM baskets
        """
    ).fetchone()
    if multi < MIN_MULTI_ITEM_ORDERS:
        if orders == 0:
            raise NotEnoughData("Basket analysis needs real order numbers (an order_id column) to see which "
                                "products are bought in the same order.")
        raise NotEnoughData(f"Basket analysis needs at least {MIN_MULTI_ITEM_ORDERS} orders with two or more "
                            f"different products; there are {multi} of {orders:,} orders.")
    min_orders = max(5, math.ceil(0.001 * orders))

    tables = [_rules(cur, "product", orders, min_orders)]
    has_category = cur.execute("SELECT count(category) > 0 FROM sales").fetchone()[0]
    if has_category:
        tables.append(_rules(cur, "category", orders, min_orders))
    rules = pd.concat(tables, ignore_index=True)

    products = rules[rules["level"] == "product"]
    metrics = {
        "orders": int(orders),
        "multi_item_orders": int(multi),
        "multi_item_pct": 100 * multi / orders,
        "min_orders": min_orders,
        "product_rules": int(len(products)),
        "category_rules": int((rules["level"] == "category").sum()),
    }
    if products.empty:
        message = (f"No product pairs are bought together clearly more often than chance "
                   f"({multi:,} of {orders:,} orders have two or more products).")
    else:
        top = products.iloc[0]
        message = (f"{len(products)} product pairs are bought together more often than chance. Strongest: "
                   f"{top['confidence_pct']:.0f}% of orders with {top['antecedent']} also include "
                   f"{top['consequent']} ({top['lift']:.1f}x the usual rate). {100 * multi / orders:.0f}% of "
                   f"orders have two or more products.")
    return TaskResult(metrics, message, {"basket_rules": rules})


def _rules(cur: duckdb.DuckDBPyConnection, level: str, orders: int, min_orders: int) -> pd.DataFrame:
    # ``level`` is "product" or "category", never user input.
    df = cur.execute(
        f"""
        WITH lines AS (
            SELECT DISTINCT order_id, {level} AS item FROM sales
            WHERE NOT is_return AND NOT order_id_generated AND {level} IS NOT NULL
        ),
        counts AS (SELECT item, count(*) AS n FROM lines GROUP BY item),
        small AS (SELECT order_id FROM lines GROUP BY order_id HAVING count(*) BETWEEN 2 AND {MAX_BASKET}),
        basket AS (SELECT l.* FROM lines l JOIN small USING (order_id)),
        pairs AS (
            SELECT a.item AS antecedent, b.item AS consequent, count(*) AS together
            FROM basket a JOIN basket b ON a.order_id = b.order_id AND a.item <> b.item
            GROUP BY 1, 2
        )
        SELECT p.antecedent, p.consequent, p.together, ca.n AS antecedent_orders, cb.n AS consequent_orders
        FROM pairs p JOIN counts ca ON ca.item = p.antecedent JOIN counts cb ON cb.item = p.consequent
        WHERE p.together >= ?
        """,
        [min_orders],
    ).df()
    expected = df["antecedent_orders"] * df["consequent_orders"] / orders
    out = pd.DataFrame({
        "level": level,
        "antecedent": df["antecedent"],
        "consequent": df["consequent"],
        "orders_together": df["together"].astype(int),
        "antecedent_orders": df["antecedent_orders"].astype(int),
        "expected_together": expected,
        "support_pct": 100 * df["together"] / orders,
        "confidence_pct": 100 * df["together"] / df["antecedent_orders"],
        "lift": df["together"] / expected,
    })
    keep = (out["lift"] >= MIN_LIFT) & (out["orders_together"] >= expected + Z * expected.pow(0.5))
    return out[keep].sort_values(["lift", "orders_together"], ascending=False, ignore_index=True)
