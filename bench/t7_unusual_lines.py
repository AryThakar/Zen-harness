"""Test 7 - unusual sale lines. Online Retail II's two known keying errors (80,995 and
74,215 units, cancelled right after), plus 20 injected price typos per dataset."""

from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd

from app.ml.anomalies import unusual_lines

from common import WORK, sales, snapshot

TYPOS = 20


def known_errors() -> dict:
    full = sales("retail")
    known = full[(~full["is_return"]) & (full["quantity"].isin([80995, 74215]))]
    con = snapshot("retail")
    found = unusual_lines(con)
    eligible = con.execute(
        "SELECT product, count(*) FROM sales WHERE NOT is_return AND unit_price > 0 GROUP BY 1"
    ).df().set_index("product")["count_star()"]
    con.close()
    return {
        "dataset": "retail", "test": "known keying errors",
        "errors": [{"product": r.product, "quantity": r.quantity,
                    "product_sales": int(eligible.get(r.product, 0)),
                    "reported": bool(r.sale_id in set(found["sale_id"]))} for r in known.itertuples()],
        "lines_reported": len(found),
    }


def typos(name: str, seed: int = 0) -> dict:
    full = sales(name)
    rng = np.random.default_rng(seed)
    counts = full[~full["is_return"] & (full["unit_price"] > 0)].groupby("product")["sale_id"].transform("size")
    pool = full[~full["is_return"] & (full["unit_price"] > 0)][counts >= 20]
    picked = pool.sample(TYPOS, random_state=seed)
    factors = np.array([10] * 7 + [100] * 7 + [0.1] * 6)
    rng.shuffle(factors)
    change = dict(zip(picked["sale_id"], factors))

    def edit(df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        f = df["sale_id"].map(change).fillna(1.0)
        df["unit_price"] = df["unit_price"] * f
        df["revenue"] = df["quantity"] * df["unit_price"] - df["discount"].fillna(0)
        return df

    con = snapshot(name, transform=edit)
    found = unusual_lines(con)
    con.close()
    hit = set(found["sale_id"]) & set(change)
    by_factor = {str(k): int(sum(1 for s, f in change.items() if f == k and s in hit)) for k in (10, 100, 0.1)}
    return {"dataset": name, "test": "injected price typos", "injected": TYPOS, "found": len(hit),
            "found_by_factor": by_factor, "lines_reported": len(found),
            "reported_not_injected": len(found) - len(hit)}


def main(names=None):
    rows = []
    if not names or "retail" in names:
        rows.append(known_errors())
        print(json.dumps(rows[-1], default=str), flush=True)
    for name in names or ["retail", "coffee", "pizza"]:
        rows.append(typos(name))
        print(json.dumps(rows[-1], default=str), flush=True)
    (WORK / "t7_unusual_lines.json").write_text(json.dumps(rows, indent=2, default=str))


if __name__ == "__main__":
    main(sys.argv[1:] or None)
