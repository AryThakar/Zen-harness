# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Column roles: what each column is for, read from its name and its values, the same for every tool.

    date      when a row happened                 forecast, unusual periods, recency in segments
    order     the order/visit a row belongs to    bought together, orders per customer
    item      the thing a row is about            bought together, top items
    entity    who a row belongs to                segments (customers, users, patients ...)
    amount    money per row: added up
    quantity  units per row: added up
    price     money per unit: averaged, never added up
    text      free text (comments, reviews)       analyze_text

A role is a default, never a rule: every tool also takes the column by name. The name is the main
signal - generic data words, no business domain - and the values confirm it (an order id repeats, an
amount has cents, free text is long and varied). An unclear role is left out rather than guessed.
"""

from __future__ import annotations

import re

import pandas as pd

from .workspace import Workspace

ID = {"id", "no", "nr", "num", "number", "code", "ref", "key"}
ORDER = {"order", "invoice", "bill", "transaction", "txn", "receipt", "basket", "cart", "ticket", "booking",
         "visit", "session", "trip"}
ENTITY = {"customer", "client", "user", "member", "account", "patient", "student", "employee", "buyer",
          "subscriber", "guest", "player", "person", "household", "cust", "shopper", "donor", "visitor", "email"}
# Item words by how readable the column is likely to be: a name beats a product code. The words that say
# "product" on their own (STRONG) need no repeats; a plain name or description must repeat to be an item.
ITEM = {"name": 3, "description": 3, "desc": 3, "detail": 3, "title": 3, "product": 2, "item": 2, "article": 2,
        "menu": 2, "dish": 2, "goods": 2, "service": 2, "sku": 1, "stockcode": 1, "stock": 1, "part": 1}
STRONG_ITEM = {"product", "item", "article", "goods", "sku", "stockcode", "menu", "dish", "service"}
QUANTITY = {"qty", "quantity", "units", "pieces", "pcs"}
PRICE = {"price", "mrp"}
AMOUNT = {"amount": 2, "amt": 2, "revenue": 2, "sales": 2, "total": 2, "subtotal": 1, "spend": 1, "spent": 1,
          "turnover": 1, "gross": 1, "net": 1, "payment": 1, "paid": 1, "charge": 1, "charges": 1, "income": 1}
COUNT_OR_MONEY = {"sales"}  # a number of sales or their value: money only with cents
# A column whose name also says one of these is a property of the thing, not the thing itself
# (product_category, customer_type, order_date, total_price ...).
NOT_ITEM = {"category", "type", "group", "class", "family", "brand", "department", "size", "color",
            "colour", "customer", "client", "user", "store", "shop", "city", "country", "region", "state",
            "location", "branch", "company", "supplier", "vendor", "first", "last", "full", "file", "billing",
            "shipping", "contact", "owner", "author", "manager", "staff", "agent"}  # (a person's name is no item)
NOT_KEY = {"date", "time", "status", "type", "category", "segment", "group", "class", "age", "since", "count",
           "qty", "quantity", "amount", "total", "value", "price", "line", "details", "detail", "item"}
NOT_AMOUNT = {"price", "rate", "per", "avg", "average", "mean", "pct", "percent", "share", "count", "qty", "mm", "cm",
              "km", "kg", "length", "width", "height", "depth", "weight", "mass"}
# Columns that hold codes, whatever their values look like (never read as numbers).
CODE_WORDS = ID | ORDER | ENTITY | {"sku", "stockcode", "zip", "zipcode", "postcode", "pincode", "phone", "mobile",
                                    "barcode", "ean", "upc", "isbn"}
VOCAB = ID | ORDER | ENTITY | set(ITEM) | QUANTITY | PRICE | set(AMOUNT) | NOT_ITEM | NOT_KEY | {"date", "time"}


def tokens(name: str) -> list[str]:
    """Words of a column name: 'InvoiceDate' -> invoice, date; 'customerid' -> customer, id."""
    words = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(name)).lower()
    out = []
    for w in re.findall(r"[a-z]+", words):
        out += _split_glued(w)
    return out


def _split_glued(w: str) -> list[str]:
    if w in VOCAB:
        return [w]
    for head in sorted(VOCAB, key=len, reverse=True):
        if len(head) >= 3 and w.startswith(head) and w[len(head):] in VOCAB:
            return [head, w[len(head):]]
    return [w]


def column_roles(df: pd.DataFrame) -> dict[str, str]:
    """{role: column} for the roles this table clearly has."""
    names = {c: set(tokens(c)) for c in df.columns}
    roles: dict[str, str] = {}
    dates = [c for c in df.columns if pd.api.types.is_datetime64_any_dtype(df[c])]
    if dates:
        roles["date"] = next((c for c in dates if names[c] & {"date", "time", "day"}), dates[0])
    per_value = {}  # rows per distinct value, computed once per candidate

    def repeats(c) -> float:
        if c not in per_value:
            present = df[c].dropna()
            per_value[c] = len(present) / max(1, present.nunique())
        return per_value[c]

    def key_like(c) -> bool:
        s = df[c]
        if pd.api.types.is_bool_dtype(s) or pd.api.types.is_datetime64_any_dtype(s):
            return False
        if pd.api.types.is_numeric_dtype(s):
            whole = s.dropna()
            return bool(names[c] & ID) and len(whole) > 0 and bool((whole % 1 == 0).all())
        return True

    keys = [c for c in df.columns if key_like(c) and not names[c] & NOT_KEY - ID]
    orders = [c for c in keys if names[c] & ORDER and (names[c] & ID or not pd.api.types.is_numeric_dtype(df[c]))]
    if orders:  # a column that groups rows (an order of several lines) beats a line number
        roles["order"] = max(orders, key=lambda c: (repeats(c) > 1.05, -len(names[c])))
    entities = [c for c in keys if names[c] & ENTITY and repeats(c) > 1.05]
    if entities:
        roles["entity"] = max(entities, key=lambda c: (bool(names[c] & ID), -len(names[c])))
    items = [c for c in df.columns if c not in roles.values() and names[c] & set(ITEM) and not names[c] & NOT_ITEM
             and (key_like(c) or not pd.api.types.is_numeric_dtype(df[c]))
             and df[c].nunique() >= 3 and (names[c] & STRONG_ITEM or repeats(c) >= 1.5)]
    if items:
        roles["item"] = max(items, key=lambda c: (sum(ITEM.get(w, 0) for w in names[c]), repeats(c)))
    numbers = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c]) and not pd.api.types.is_bool_dtype(df[c])
               and c not in roles.values()]
    quantity = [c for c in numbers if names[c] & QUANTITY and _whole_share(df[c]) >= 0.95]
    if quantity:
        roles["quantity"] = quantity[0]
    price = [c for c in numbers if names[c] & PRICE and not names[c] & set(AMOUNT)]
    if price:
        roles["price"] = min(price, key=lambda c: len(names[c]))
    amount = [c for c in numbers if names[c] & set(AMOUNT) and not names[c] & NOT_AMOUNT and c not in roles.values()
              and (names[c] - COUNT_OR_MONEY & set(AMOUNT) or _whole_share(df[c]) < 0.95)]
    if amount:
        roles["amount"] = max(amount, key=lambda c: (sum(AMOUNT.get(w, 0) for w in names[c]), -len(names[c])))
    text = [c for c in df.columns if c not in roles.values() and _free_text(df[c])]
    if text:
        roles["text"] = max(text, key=lambda c: df[c].dropna().astype(str).str.len().median())
    return roles


def table_roles(ws: Workspace, data: str, df: pd.DataFrame | None = None) -> dict[str, str]:
    """The roles of a stored table, worked out once and kept on it (a stored table never changes)."""
    meta = ws.entry(data).get("meta", {})
    if "roles" not in meta:
        ws.set_meta(data, "roles", column_roles(ws.table(data) if df is None else df))
    return dict(ws.entry(data)["meta"]["roles"])


def role_notes(df: pd.DataFrame, roles: dict[str, str]) -> list[str]:
    """How the money and count columns combine - the mistakes a role prevents."""
    q, p, a = roles.get("quantity"), roles.get("price"), roles.get("amount")
    notes = []
    if q and p and not a:
        notes.append(f"There is no amount column: a row's amount is \"{q}\" * \"{p}\" (sum(\"{q}\" * \"{p}\") "
                     "for revenue).")
    if p:
        notes.append(f"\"{p}\" is per unit: average it" + (f" (weighted by \"{q}\")" if q else "")
                     + ", never add it up.")
    if roles.get("order") and _unique_per_row(df, roles["order"]):
        notes.append(f"Every \"{roles['order']}\" has one row: each order holds a single line here.")
    return notes


def _whole_share(s: pd.Series) -> float:
    v = s.dropna().astype(float)
    return float((v % 1 == 0).mean()) if len(v) else 1.0


def _free_text(s: pd.Series) -> bool:
    """Long, varied text: comments and reviews, not labels or codes."""
    if pd.api.types.is_numeric_dtype(s) or pd.api.types.is_datetime64_any_dtype(s) or pd.api.types.is_bool_dtype(s):
        return False
    t = s.dropna().astype(str)
    if len(t) < 10:
        return False
    sample = t.sample(min(len(t), 2000), random_state=0)
    return bool(sample.str.len().median() >= 30 and sample.str.count(" ").median() >= 4
                and sample.nunique() >= 0.5 * len(sample))


def _unique_per_row(df: pd.DataFrame, c: str) -> bool:
    present = df[c].dropna()
    return len(present) > 0 and present.nunique() == len(present)
