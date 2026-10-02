"""Test 5 - bought together. (a) Pairs found in the first half must still hold in the second
half. (b) With products shuffled across orders (same basket sizes and product counts) there
is nothing real to find, so every pair reported is a false one."""

from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd

from app.ml import basket

from common import WORK, sales, snapshot


def pair_stats(df: pd.DataFrame, pairs: list[tuple[str, str]]) -> pd.DataFrame:
    """Lift and z of each pair in ``df``, counted the way the app counts (distinct
    products per real, non-return order of 2..50 products)."""
    lines = df[~df["is_return"] & ~df["order_id_generated"]][["order_id", "product"]].drop_duplicates()
    orders = lines["order_id"].nunique()
    size = lines.groupby("order_id")["product"].transform("size")
    counts = lines.groupby("product")["order_id"].nunique()
    small = lines[(size >= 2) & (size <= basket.MAX_BASKET)]
    by_order = small.groupby("product")["order_id"].apply(set)
    out = []
    for a, b in pairs:
        together = len(by_order.get(a, set()) & by_order.get(b, set()))
        expected = counts.get(a, 0) * counts.get(b, 0) / orders
        out.append({"a": a, "b": b, "together": together, "expected": expected,
                    "lift": together / expected if expected else np.nan,
                    "z": (together - expected) / np.sqrt(expected) if expected else np.nan})
    return pd.DataFrame(out)


def unordered(rules: pd.DataFrame) -> list[tuple[str, str]]:
    prod = rules[rules["level"] == "product"]
    return sorted({tuple(sorted((a, b))) for a, b in zip(prod["antecedent"], prod["consequent"])})


def holdout(name: str, full: pd.DataFrame) -> dict:
    mid = full["order_date"].min() + (full["order_date"].max() - full["order_date"].min()) / 2
    con = snapshot(name, end=mid)
    res = basket.run(con)
    con.close()
    pairs = unordered(res.tables["basket_rules"])
    later = pair_stats(full[full["order_date"] >= mid], pairs)
    later.to_csv(WORK / f"t5_{name}_pairs_second_half.csv", index=False)
    # A pair with a product not sold at all in the second half can't be tested there.
    testable = later[later["expected"] > 0]
    return {"dataset": name, "test": "first half -> second half", "pairs_found": len(pairs),
            "pairs_testable": len(testable),
            "testable_lift_gt_1_pct": round(100 * (testable["lift"] > 1).mean(), 1) if len(testable) else None,
            "hold_lift_gt_1_pct": round(100 * (later["lift"] > 1).mean(), 1) if len(later) else None,
            "hold_lift_ge_1_2_and_z3_pct": round(100 * ((later["lift"] >= 1.2) & (later["z"] >= 3)).mean(), 1)
            if len(later) else None,
            "median_lift_first": float(res.tables["basket_rules"].query("level == 'product'")["lift"].median())
            if len(pairs) else None,
            "median_lift_second": float(later["lift"].median()) if len(later) else None}


def shuffled(name: str, seed: int) -> dict:
    def shuffle(df: pd.DataFrame) -> pd.DataFrame:
        rng = np.random.default_rng(seed)
        df = df.copy()
        real = (~df["is_return"] & ~df["order_id_generated"]).to_numpy()
        idx = np.where(real)[0]
        perm = rng.permutation(idx)
        for col in ("product", "category"):
            values = df[col].to_numpy(copy=True)
            values[idx] = df[col].to_numpy()[perm]  # product and its category move together
            df[col] = values
        return df

    con = snapshot(name, transform=shuffle)
    res = basket.run(con)
    con.close()
    rules = res.tables["basket_rules"]
    return {"dataset": name, "test": f"shuffled (seed {seed})", "false_pairs": len(unordered(rules)),
            "false_category_pairs": int((rules["level"] == "category").sum() // 2),
            "orders": res.metrics["orders"]}


def main(names=None):
    rows = []
    for name in names or ["retail", "pizza"]:
        full = sales(name)
        full["order_date"] = pd.to_datetime(full["order_date"])
        rows.append(holdout(name, full))
        print(json.dumps(rows[-1], default=str), flush=True)
        for seed in range(3):
            rows.append(shuffled(name, seed))
            print(json.dumps(rows[-1], default=str), flush=True)
    (WORK / "t5_basket.json").write_text(json.dumps(rows, indent=2, default=str))


if __name__ == "__main__":
    main(sys.argv[1:] or None)
