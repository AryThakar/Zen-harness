"""Generate realistic synthetic sales data for demos and tests.

The business: an electronics & accessories retailer with city stores, a website
and a marketplace. Patterns are planted on purpose so each ML feature has
something real to find:

* growth trend, weekly rhythm and a festive Oct-Nov peak    -> forecasting
* loyal / regular / occasional customers, some of them stop -> segments, churn
* phone -> case + screen guard, laptop -> mouse + bag       -> frequently bought together
* a flash-sale spike day and an outage day                  -> anomaly detection
* an 8% Electronics price rise part-way through             -> "why did sales change?"
* about 2% of lines returned

Usage:
    python -m legacy.datagen                              # canonical CSV, 18 months
    python -m legacy.datagen --format messy --months 6    # POS-style export with junk to clean
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

CITIES = ("Mumbai", "Pune", "Ahmedabad", "Bengaluru", "Delhi")
CHANNELS = (("store", 0.55), ("online", 0.35), ("marketplace", 0.10))
PAYMENT_MODES = (("UPI", 0.45), ("Card", 0.30), ("Cash", 0.15), ("Netbanking", 0.10))
WEEKDAY_FACTOR = (0.85, 0.9, 0.95, 1.0, 1.1, 1.35, 1.25)  # Monday..Sunday
MONTH_FACTOR = {1: 0.85, 2: 0.85, 10: 1.35, 11: 1.5, 12: 1.2}


@dataclass(frozen=True)
class Product:
    name: str
    category: str
    price: float
    cost: float
    popularity: float


PRODUCTS = (
    Product("Smartphone X1", "Electronics", 18999, 15200, 5),
    Product("Smartphone Pro", "Electronics", 34999, 28500, 2),
    Product("Earbuds Pro", "Electronics", 4999, 2900, 6),
    Product("Smart Watch", "Electronics", 8999, 6100, 4),
    Product("Bluetooth Speaker", "Electronics", 3499, 2100, 4),
    Product("Laptop 14 inch", "Electronics", 52999, 45800, 1.5),
    Product("Tablet 10 inch", "Electronics", 21999, 17600, 1.5),
    Product("Phone Case", "Accessories", 799, 250, 6),
    Product("Screen Guard", "Accessories", 349, 80, 6),
    Product("Fast Charger 33W", "Accessories", 1299, 600, 5),
    Product("USB-C Cable", "Accessories", 399, 120, 8),
    Product("Power Bank 20000mAh", "Accessories", 1899, 1100, 5),
    Product("Wireless Mouse", "Accessories", 1299, 650, 5),
    Product("Laptop Bag", "Accessories", 1999, 950, 2),
    Product("Keyboard", "Accessories", 1499, 800, 3),
    Product("Memory Card 128GB", "Accessories", 999, 600, 4),
    Product("Electric Kettle", "Home Appliances", 1799, 1100, 3),
    Product("Mixer Grinder", "Home Appliances", 3999, 2700, 2),
    Product("Air Fryer", "Home Appliances", 6999, 4800, 2),
    Product("Steam Iron", "Home Appliances", 1599, 950, 2),
    Product("Beard Trimmer", "Personal Care", 1499, 850, 3),
    Product("Hair Dryer", "Personal Care", 1299, 750, 2),
    Product("Electric Toothbrush", "Personal Care", 2499, 1400, 2),
)

# product -> [(add-on product, probability)]
BASKET_RULES = {
    "Smartphone X1": (("Phone Case", 0.55), ("Screen Guard", 0.45), ("Fast Charger 33W", 0.2)),
    "Smartphone Pro": (("Phone Case", 0.6), ("Screen Guard", 0.5), ("Earbuds Pro", 0.15)),
    "Laptop 14 inch": (("Wireless Mouse", 0.5), ("Laptop Bag", 0.45)),
    "Tablet 10 inch": (("Keyboard", 0.3), ("Screen Guard", 0.3)),
    "Smart Watch": (("Screen Guard", 0.2),),
}

CUSTOMER_TYPES = (  # name, share, purchase weight, chance of churning
    ("loyal", 0.10, 8.0, 0.15),
    ("regular", 0.30, 3.0, 0.30),
    ("occasional", 0.60, 1.0, 0.20),
)


def planted_events(months: int = 18, end: date | None = None) -> dict[str, pd.Timestamp]:
    """The days on which the generator plants its events, for tests and demos."""
    days = _days(months, end)
    n = len(days)
    return {
        "flash_sale": days[int(n * 0.4)],   # 3x the usual orders, 20% off
        "outage": days[int(n * 0.7)],       # 15% of the usual orders
        "price_rise": days[int(n * 0.6)],   # Electronics +8% from this day
    }


def _days(months: int, end: date | None) -> pd.DatetimeIndex:
    end = end or date.today() - timedelta(days=1)
    start = (pd.Timestamp(end) - pd.DateOffset(months=months)).date() + timedelta(days=1)
    return pd.date_range(start, end, freq="D")


def generate(
    months: int = 18,
    end: date | None = None,
    orders_per_day: float = 40,
    seed: int = 7,
) -> pd.DataFrame:
    """Sales lines in the canonical format plus a ``payment_mode`` custom column."""
    rng = np.random.default_rng(seed)
    end = end or date.today() - timedelta(days=1)
    days = _days(months, end)
    events = planted_events(months, end)
    spike_day, outage_day, price_rise_day = events["flash_sale"], events["outage"], events["price_rise"]

    # About 3 orders per customer over 18 months on average; loyal ones ~10.
    customers = _make_customers(rng, days, n=max(300, int(orders_per_day * 150)))
    by_name = {p.name: p for p in PRODUCTS}
    popularity = np.array([p.popularity for p in PRODUCTS])
    popularity = popularity / popularity.sum()

    rows = []
    order_no = 0
    for i, day in enumerate(days):
        growth = 1 + 0.015 * (i / 30)
        expected = orders_per_day * growth * WEEKDAY_FACTOR[day.weekday()] * MONTH_FACTOR.get(day.month, 1.0)
        if day == spike_day:
            expected *= 3
        if day == outage_day:
            expected *= 0.15
        active = customers[(customers.start <= day) & (customers.churn > day)]
        n_orders = rng.poisson(expected)
        if len(active):
            weights = active.weight.to_numpy()
            picks = rng.choice(len(active), size=n_orders, p=weights / weights.sum())
            ids, cities = active.customer_id.to_numpy()[picks], active.city.to_numpy()[picks]

        for k in range(n_orders):
            order_no += 1
            channel = _pick(rng, CHANNELS)
            guest = channel == "store" and rng.random() < 0.3
            if guest or active.empty:
                customer_id, city = None, CITIES[rng.integers(len(CITIES))]
            else:
                customer_id, city = ids[k], cities[k]
            hour = rng.integers(10, 21) if channel == "store" else rng.integers(0, 24)
            when = day + pd.Timedelta(hours=int(hour), minutes=int(rng.integers(60)))

            if day == spike_day:
                rate = 0.20
            elif rng.random() < (0.35 if day.month in (10, 11) else 0.15):
                rate = float(rng.choice([0.05, 0.10, 0.15]))
            else:
                rate = 0.0
            payment = _pick(rng, PAYMENT_MODES)

            first = PRODUCTS[rng.choice(len(PRODUCTS), p=popularity)]
            items = [first.name]
            for addon, prob in BASKET_RULES.get(first.name, ()):
                if rng.random() < prob and addon not in items:
                    items.append(addon)
            if rng.random() < 0.15:
                extra = PRODUCTS[rng.choice(len(PRODUCTS), p=popularity)].name
                if extra not in items:
                    items.append(extra)

            for name in items:
                p = by_name[name]
                price = p.price * (1.08 if p.category == "Electronics" and day >= price_rise_day else 1)
                price = round(price)
                if p.category == "Accessories":
                    qty = int(rng.choice([1, 2, 3], p=[0.7, 0.2, 0.1]))
                else:
                    qty = int(rng.choice([1, 2], p=[0.95, 0.05]))
                line = {
                    "order_id": f"ORD-{order_no:06d}",
                    "order_date": when,
                    "product": p.name,
                    "quantity": qty,
                    "unit_price": float(price),
                    "customer_id": customer_id,
                    "unit_cost": float(p.cost),
                    "category": p.category,
                    "discount": round(rate * qty * price, 2),
                    "location": city,
                    "channel": channel,
                    "payment_mode": payment,
                }
                rows.append(line)
                returned = when + pd.Timedelta(days=int(rng.integers(2, 15)))
                if rng.random() < 0.02 and returned.date() <= end:
                    rows.append({**line, "order_date": returned, "quantity": -qty,
                                 "discount": -line["discount"]})

    df = pd.DataFrame(rows)
    return df.sort_values(["order_date", "order_id"], ignore_index=True)


def to_messy_export(df: pd.DataFrame) -> str:
    """Render ``df`` like a POS sales register: title lines, renamed columns,
    day-first dates, rupee symbols and Indian digit grouping."""
    out = pd.DataFrame({
        "Bill No": df.order_id,
        "Bill Date": df.order_date.dt.strftime("%d/%m/%Y %H:%M"),
        "Item": df["product"],
        "Group": df.category,
        "Qty": df.quantity,
        "Rate": df.unit_price.map(_rupees),
        "Disc Amt": df.discount.map(lambda v: _rupees(v) if v else ""),
        "Customer Code": df.customer_id.fillna(""),
        "Purchase Rate": df.unit_cost.map(_rupees),
        "Store": df.location,
        "Sales Channel": df.channel.str.title(),
        "Payment Mode": df.payment_mode,
    })
    first, last = df.order_date.min(), df.order_date.max()
    title = (
        "Gupta Digital World - Consolidated Sales Register\n"
        f"Period: {first:%d/%m/%Y} to {last:%d/%m/%Y}\n"
        "Printed by: admin\n\n"
    )
    total = f"Total,,,,{df.quantity.sum()},,,,,,,\n"
    return title + out.to_csv(index=False) + total


def _rupees(value: float) -> str:
    """12345.5 -> '₹12,345.50' with Indian grouping (1,23,456)."""
    sign = "-" if value < 0 else ""
    whole, frac = f"{abs(value):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join([head, *groups, tail]) if head else ",".join([*groups, tail])
    return f"{sign}₹{whole}.{frac}"


def _make_customers(rng: np.random.Generator, days: pd.DatetimeIndex, n: int) -> pd.DataFrame:
    kinds = rng.choice(len(CUSTOMER_TYPES), size=n, p=[t[1] for t in CUSTOMER_TYPES])
    first, last = days[0], days[-1]
    span = (last - first).days
    # A third are customers from before the data starts; the rest join over time.
    start_offsets = np.where(rng.random(n) < 0.35, 0, rng.integers(0, int(span * 0.85), n))
    starts = first + pd.to_timedelta(start_offsets, unit="D")
    churn = []
    for kind, s in zip(kinds, starts):
        if rng.random() < CUSTOMER_TYPES[kind][3]:
            remaining = (last - s).days
            churn.append(s + pd.Timedelta(days=int(rng.integers(30, max(31, remaining)))))
        else:
            churn.append(last + pd.Timedelta(days=1))
    return pd.DataFrame({
        "customer_id": [f"CUST-{i + 1:05d}" for i in range(n)],
        "kind": [CUSTOMER_TYPES[k][0] for k in kinds],
        "weight": [CUSTOMER_TYPES[k][2] for k in kinds],
        "city": rng.choice(CITIES, size=n),
        "start": starts,
        "churn": pd.to_datetime(churn),
    })


def _pick(rng: np.random.Generator, options: tuple[tuple[str, float], ...]) -> str:
    names, probs = zip(*options)
    return names[rng.choice(len(names), p=probs)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--months", type=int, default=18)
    parser.add_argument("--end", type=date.fromisoformat, default=None, help="last day, YYYY-MM-DD")
    parser.add_argument("--orders-per-day", type=float, default=40)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--format", choices=("canonical", "messy"), default="canonical")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    df = generate(args.months, args.end, args.orders_per_day, args.seed)
    out = args.out or Path("data/generated") / f"synthetic_sales_{args.format}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    if args.format == "messy":
        out.write_text(to_messy_export(df), encoding="utf-8")
    else:
        df.to_csv(out, index=False, date_format="%Y-%m-%d %H:%M:%S")
    print(f"Wrote {len(df):,} lines ({df.order_id.nunique():,} orders) to {out}")


if __name__ == "__main__":
    started = datetime.now()
    main()
    print(f"Done in {(datetime.now() - started).total_seconds():.1f}s")
