"""Format profiles: saved column mappings for exports from known systems."""

from __future__ import annotations

import json

import duckdb

from .mapper import Mapping, Profile


class ProfileError(ValueError):
    """A profile can't be saved under that name."""

# Built-in profiles cover exports whose columns the alias list can't guess alone
# (Shopify's order number is just "Name") or whose dates are month-first.
BUILTIN_PROFILES: tuple[Profile, ...] = (
    Profile(
        "Shopify orders export",
        Mapping(
            fields={
                "order_id": "name",
                "order_date": "created_at",
                "product": "lineitem_name",
                "quantity": "lineitem_quantity",
                "unit_price": "lineitem_price",
            },
            # Order-level money columns would double count per line (the per-line
            # "Lineitem discount" is picked up by the alias list instead), and
            # names, addresses and phones are personal data the analysis doesn't need.
            ignore=[
                "subtotal", "shipping", "taxes", "total", "discount_amount", "refunded_amount",
                "billing_name", "billing_street", "billing_address1", "billing_address2",
                "billing_company", "billing_zip", "billing_phone", "shipping_name",
                "shipping_street", "shipping_address1", "shipping_address2",
                "shipping_company", "shipping_zip", "shipping_phone", "phone",
            ],
        ),
        builtin=True,
    ),
    Profile(
        "Superstore dataset",
        Mapping(
            fields={
                "order_id": "order_id",
                "order_date": "order_date",
                "product": "product_name",
                "quantity": "quantity",
                "amount": "sales",
                "line_profit": "profit",
            },
            date_order="monthfirst",
            discount_type="percent",
        ),
        builtin=True,
    ),
    Profile(
        "UCI Online Retail",
        Mapping(
            fields={
                "order_id": "invoice",
                "order_date": "invoicedate",
                "product": "description",
                "quantity": "quantity",
                "unit_price": "price",
            },
            date_order="monthfirst",
        ),
        builtin=True,
    ),
)


def load_profiles(cur: duckdb.DuckDBPyConnection) -> list[Profile]:
    rows = cur.execute("SELECT name, mapping, builtin FROM format_profiles ORDER BY profile_id").fetchall()
    return [Profile(name, Mapping.from_dict(json.loads(m)), builtin) for name, m, builtin in rows]


def save_profile(cur: duckdb.DuckDBPyConnection, name: str, mapping: Mapping) -> None:
    """Create or replace a user profile. Built-in profiles can't be overwritten."""
    existing = cur.execute("SELECT builtin FROM format_profiles WHERE name = ?", [name]).fetchone()
    if existing and existing[0]:
        raise ProfileError(f"'{name}' is a built-in profile; choose another name.")
    cur.execute(
        """
        INSERT INTO format_profiles (profile_id, name, mapping) VALUES (nextval('profile_id_seq'), ?, ?)
        ON CONFLICT (name) DO UPDATE SET mapping = excluded.mapping
        """,
        [name, json.dumps(mapping.to_dict())],
    )


def seed_builtin_profiles(cur: duckdb.DuckDBPyConnection) -> None:
    for p in BUILTIN_PROFILES:
        cur.execute(
            """
            INSERT INTO format_profiles (profile_id, name, mapping, builtin)
            VALUES (nextval('profile_id_seq'), ?, ?, true)
            ON CONFLICT (name) DO UPDATE SET mapping = excluded.mapping
            """,
            [p.name, json.dumps(p.mapping.to_dict())],
        )
