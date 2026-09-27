import pytest

from app.ingestion.mapper import (
    Mapping, MappingError, Profile, custom_column_name, suggest_mapping, validate_mapping,
)
from app.ingestion.profiles import BUILTIN_PROFILES


def fields_of(suggestion):
    return suggestion.mapping.fields


def test_pos_export_columns_map_by_alias():
    s = suggest_mapping(["bill_no", "bill_date", "item_name", "qty", "rate", "customer", "payment_mode"])
    assert fields_of(s) == {
        "order_date": "bill_date", "product": "item_name", "quantity": "qty", "unit_price": "rate",
        "order_id": "bill_no", "customer_id": "customer",
    }
    assert s.mapping.custom == {"payment_mode": "payment_mode"}
    assert s.missing_required == []
    assert {m.method for m in s.matches} == {"alias"}


def test_uci_online_retail_prefers_product_name_over_stock_code():
    cols = ["invoice", "stockcode", "description", "quantity", "invoicedate", "price", "customer_id", "country"]
    s = suggest_mapping(cols, list(BUILTIN_PROFILES))
    assert s.profile == "UCI Online Retail"
    assert fields_of(s)["product"] == "description"
    assert fields_of(s)["location"] == "country"
    assert s.mapping.custom == {"stockcode": "stockcode"}
    assert s.mapping.date_order == "monthfirst"


def test_shopify_profile_maps_the_order_name_and_ignores_order_totals():
    cols = ["name", "email", "created_at", "subtotal", "total", "discount_amount", "lineitem_quantity",
            "lineitem_name", "lineitem_price", "lineitem_discount", "billing_name", "billing_city", "source"]
    s = suggest_mapping(cols, list(BUILTIN_PROFILES))
    assert s.profile == "Shopify orders export"
    assert fields_of(s)["order_id"] == "name"
    assert fields_of(s)["customer_id"] == "email"
    assert fields_of(s)["discount"] == "lineitem_discount"
    assert fields_of(s)["channel"] == "source"
    assert set(s.mapping.ignore) == {"subtotal", "total", "discount_amount", "billing_name"}
    assert s.mapping.custom == {}


def test_fuzzy_matching_catches_typos():
    s = suggest_mapping(["order_date", "product", "quantity", "unit_prce"])
    match = next(m for m in s.matches if m.field == "unit_price")
    assert (match.source, match.method) == ("unit_prce", "fuzzy")


def test_amount_is_only_used_without_a_unit_price():
    s = suggest_mapping(["date", "item", "qty", "price", "total"])
    assert "amount" not in fields_of(s)
    assert s.mapping.custom == {"total": "total"}

    s = suggest_mapping(["date", "service", "client", "amount"])
    assert fields_of(s)["amount"] == "amount"
    assert s.missing_required == []  # quantity defaults to 1 for amount-only files


def test_missing_required_and_locked_features():
    s = suggest_mapping(["product", "qty"])
    assert s.missing_required == ["order_date", "unit_price (or a line total / amount)"]
    assert "customer_id" in s.locked_features
    assert "churn prediction" in s.locked_features["customer_id"]

    s = suggest_mapping(["date", "item", "sales", "profit"])
    assert "unit_cost" not in s.locked_features  # profit lets us derive cost


def test_percent_hint_comes_from_the_original_header():
    s = suggest_mapping(["date", "item", "qty", "rate", "disc"], original_names={"disc": "Disc %"})
    assert s.mapping.discount_type == "percent"


def test_saved_profile_wins_and_restores_custom_names():
    saved = Profile("My POS", Mapping(
        fields={"order_date": "dt", "product": "itm", "quantity": "q", "unit_price": "p"},
        custom={"mode": "payment_mode"}, date_order="dayfirst",
        descriptions={"payment_mode": "How the customer paid"},
    ))
    s = suggest_mapping(["dt", "itm", "q", "p", "mode", "new_col"], [saved])
    assert s.profile == "My POS"
    assert s.mapping.custom == {"mode": "payment_mode", "new_col": "new_col"}
    assert s.mapping.date_order == "dayfirst"
    assert s.mapping.descriptions == {"payment_mode": "How the customer paid"}


def test_custom_names_avoid_reserved_columns():
    assert custom_column_name("year") == "year_custom"
    assert custom_column_name("revenue") == "revenue_custom"
    assert custom_column_name("2024_target") == "c_2024_target"


def test_validate_mapping_reports_every_problem():
    mapping = Mapping(
        fields={"order_date": "date", "product": "missing_col", "bogus": "x"},
        custom={"date": "profit", "a": "note", "b": "note"},
        date_order="sideways",
    )
    with pytest.raises(MappingError) as err:
        validate_mapping(mapping, ["date", "x", "a", "b"])
    message = str(err.value)
    for part in ("Unknown fields: bogus", "Columns not in the file: missing_col",
                 "Each file column can be used once: date", "Invalid custom column names: profit",
                 "Custom column names used more than once: note",
                 "date_order must be one of", "Match a unit price column"):
        assert part in message
