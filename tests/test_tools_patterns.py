"""Direct tests of find_patterns - planted patterns are found, shuffled data gives none. No model involved."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.tools.find_patterns import find_patterns
from app.tools.workspace import Workspace


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path / "ws")


def orders(n_orders=3000, seed=0, planted=True):
    """Orders of 2-5 items from 30 products; 'tea' and 'biscuits' are planted to go together."""
    rng = np.random.default_rng(seed)
    rows = []
    for o in range(n_orders):
        k = rng.integers(2, 6)
        items = list(rng.choice([f"product {i}" for i in range(30)], k, replace=False))
        if planted and rng.random() < 0.15:
            items += ["tea", "biscuits"]
        elif rng.random() < 0.05:
            items.append(rng.choice(["tea", "biscuits"]))
        customer = f"c{rng.integers(0, 400)}"
        day = pd.Timestamp("2024-01-01") + pd.to_timedelta(int(rng.integers(0, 365)), unit="D")
        rows += [{"order_id": o, "customer_id": customer, "date": day, "product": it,
                  "quantity": int(rng.integers(1, 4)), "price": 5.0} for it in items]
    return pd.DataFrame(rows)


def test_together_finds_the_planted_pair_and_nothing_in_shuffled_baskets(ws):
    df = orders()
    r = find_patterns(ws, ws.add_table(df, "orders"), "together")  # basket and item found by their roles
    assert r.result["basket"] == "order_id" and r.result["item"] == "product"
    top = r.result["top_pairs"][0]
    assert {top["a"], top["b"]} == {"tea", "biscuits"} and top["lift"] > 3 and top["q_value"] < 0.001
    assert ws.table(r.result["saved_as"]).iloc[0][["a", "b"]].tolist() == [top["a"], top["b"]]
    shuffled = df.assign(product=np.random.default_rng(1).permutation(df["product"].to_numpy()))
    r = find_patterns(ws, ws.add_table(shuffled, "shuffled"), "together")
    assert r.result["pairs_found"] == 0 and "more than chance" in r.result["finding"]


def test_together_says_which_columns_it_needs(ws):
    r = find_patterns(ws, ws.add_table(pd.DataFrame({"a": [1, 2], "b": ["x", "y"]}), "t"), "together")
    assert r.status == "invalid_arguments" and "Give basket" in r.result["error"]["message"]


def test_segments_recover_planted_customer_groups(ws):
    """Two kinds of customers: many one-off small buyers, and regulars who buy often and spend more."""
    rng = np.random.default_rng(2)
    rows = []
    for c in range(300):
        regular = c < 100
        for o in range(rng.integers(6, 12) if regular else 1):
            rows.append({"customer_id": f"c{c}", "order_id": f"{c}-{o}",
                         "date": pd.Timestamp("2024-01-01") + pd.to_timedelta(int(rng.integers(0, 360)), unit="D"),
                         "amount": float(rng.gamma(5, 20 if regular else 5))})
    data = ws.add_table(pd.DataFrame(rows), "sales")
    r = find_patterns(ws, data, "segments")
    res = r.result
    assert res["what"] == "customer_id values" and res["count"] == 300 and len(res["groups"]) == 2
    assert res["separation"]["silhouette"] >= 0.5
    saved = ws.table(res["saved_as"]).set_index("customer_id")["segment"]
    regulars = saved[[f"c{c}" for c in range(100)]]
    assert regulars.nunique() == 1 and (saved.drop(regulars.index) != regulars.iloc[0]).all()
    assert "high orders" in next(g for g in res["groups"] if g["size"] == 100)["stands_out"]


def test_segments_of_rows_say_when_groups_overlap(ws):
    rng = np.random.default_rng(3)
    data = ws.add_table(pd.DataFrame({"a": rng.normal(size=500), "b": rng.normal(size=500)}), "noise")
    res = find_patterns(ws, data, "segments").result
    assert res["what"] == "rows" and res["separation"]["silhouette"] < 0.5


def test_unusual_days_find_a_planted_spike_and_a_closed_day(ws):
    rng = np.random.default_rng(4)
    days = pd.date_range("2024-01-01", periods=200, freq="D")
    counts = rng.poisson(50 * (1 + 0.3 * (days.dayofweek >= 5)))
    counts[120], counts[60] = 160, 0  # a spike and a closed day
    rows = pd.DataFrame({"date": np.repeat(days, counts), "amount": 10.0})
    r = find_patterns(ws, ws.add_table(rows, "visits"), "unusual")
    found = {e["period"]: e for e in r.result["events"]}
    assert r.result["looked_at"] == "rows per day"
    assert found[str(days[120].date())]["direction"] == "above" and found[str(days[60].date())]["direction"] == "below"
    assert r.result["unusual_events"] <= 4  # the weekends are the season, not unusual
    assert r.artifact["chart"] == "unusual" and len(r.artifact["flagged"]) >= 2


def test_unusual_rows_name_the_column(ws):
    rng = np.random.default_rng(5)
    df = pd.DataFrame({"depth": rng.normal(60, 1.5, 1000), "price": rng.gamma(3, 1000, 1000)})
    df.loc[17, "depth"] = 95.0
    r = find_patterns(ws, ws.add_table(df, "gems"), "unusual")
    top = r.result["top"][0]
    assert top == {"row": 18, "column": "depth", "value": 95.0, "typical": top["typical"], "score": top["score"]}
    assert r.result["unusual_rows"] <= 10 and ws.table(r.result["saved_as"])["unusual_in"].iloc[0] == "depth"
