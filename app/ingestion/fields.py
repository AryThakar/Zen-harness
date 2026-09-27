"""The canonical sales-line format that every upload is converted into.

One row = one product sold on one bill / invoice / order.

* required  - nothing works without these
* optional  - each one unlocks extra features
* input     - accepted from files only to derive other columns
              (e.g. a line total when there is no unit price)
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    name: str
    kind: str  # "required" | "optional" | "input"
    dtype: str  # "date" | "text" | "number"
    description: str
    # Normalized source column names that map to this field, best first.
    aliases: tuple[str, ...]
    unlocks: tuple[str, ...] = ()


FIELDS: tuple[Field, ...] = (
    Field(
        "order_date", "required", "date", "When the sale happened.",
        ("order_date", "date", "invoice_date", "invoicedate", "bill_date", "sale_date",
         "sales_date", "transaction_date", "txn_date", "voucher_date", "created_at",
         "order_datetime", "order_time", "timestamp", "datetime"),
    ),
    Field(
        "product", "required", "text", "Product or service sold (name or SKU).",
        ("product", "product_name", "item", "item_name", "description", "lineitem_name",
         "dish", "dish_name", "service", "service_name", "stock_item", "article",
         "product_title", "sku_name", "product_id", "sku", "item_code", "stockcode",
         "stock_code"),
    ),
    Field(
        "quantity", "required", "number", "Units sold. Negative means a return.",
        ("quantity", "qty", "lineitem_quantity", "units", "units_sold", "quantity_sold",
         "qty_sold", "no_of_units", "pcs", "nos"),
    ),
    Field(
        "unit_price", "required", "number", "Selling price of one unit, before discount.",
        ("unit_price", "unitprice", "price", "rate", "lineitem_price", "selling_price",
         "sale_price", "price_per_unit", "unit_rate", "item_price"),
    ),
    Field(
        "order_id", "optional", "text", "Bill / invoice / order number grouping lines together.",
        ("order_id", "orderid", "order", "order_no", "order_number", "invoice", "invoice_no",
         "invoiceno", "invoice_number", "invoice_id", "bill_no", "bill_number", "bill_id",
         "receipt_no", "transaction_id", "voucher_no", "voucher_number"),
        ("order count & average order value", "frequently bought together"),
    ),
    Field(
        "customer_id", "optional", "text", "Who bought it (ID, name or email).",
        ("customer_id", "customerid", "customer", "customer_code", "customer_name", "client",
         "client_id", "client_name", "party", "party_name", "buyer", "member_id", "email",
         "customer_email"),
        ("repeat customers", "customer segments", "churn prediction", "customer lifetime value"),
    ),
    Field(
        "unit_cost", "optional", "number", "What one unit cost the business.",
        ("unit_cost", "cost_price", "purchase_price", "purchase_rate", "buying_price", "unit_cogs",
         "cost_per_unit", "cost_rate", "landing_cost", "cost"),
        ("profit & margin",),
    ),
    Field(
        "category", "optional", "text", "Product group.",
        ("category", "product_category", "item_category", "category_name", "item_group",
         "product_group", "group", "department", "product_type"),
        ("category reports & forecasts",),
    ),
    Field(
        "discount", "optional", "number", "Discount on the line in money (percentages are converted).",
        ("lineitem_discount", "discount", "discount_amount", "discount_amt", "discount_value", "disc",
         "disc_amount", "disc_amt", "discount_pct",
         "discount_percent", "disc_pct", "disc_percent", "discount_rate"),
        ("discount impact",),
    ),
    Field(
        "location", "optional", "text", "Store, branch, city or region.",
        ("store", "store_name", "branch", "outlet", "shop", "location", "city", "billing_city",
         "shipping_city", "region", "state", "country"),
        ("location comparison",),
    ),
    Field(
        "channel", "optional", "text", "Sales channel, e.g. online / store / marketplace.",
        ("channel", "sales_channel", "order_channel", "source", "order_source", "platform"),
        ("channel comparison",),
    ),
    Field(
        "amount", "input", "number",
        "Line total after discount. Used only when there is no unit price "
        "(quantity defaults to 1 if missing).",
        ("amount", "line_total", "total", "total_amount", "net_amount", "sales", "sales_amount",
         "revenue", "line_amount", "value", "invoice_amount", "net_sales", "gross_amount"),
    ),
    Field(
        "line_cost", "input", "number", "Total cost of the line. Used to derive unit_cost.",
        ("total_cost", "line_cost", "cogs", "cost_amount"),
    ),
    Field(
        "line_profit", "input", "number", "Profit of the line. Used to derive unit_cost.",
        ("profit", "line_profit", "gross_profit"),
    ),
)

FIELDS_BY_NAME = {f.name: f for f in FIELDS}
REQUIRED = tuple(f.name for f in FIELDS if f.kind == "required")

# Columns the cleaner adds. Descriptions feed the column registry the AI reads.
DERIVED_COLUMNS: dict[str, tuple[str, str]] = {
    "order_id_generated": ("BOOLEAN", "True when the file had no order number and one was generated; "
                                      "each such line then counts as its own order."),
    "is_return": ("BOOLEAN", "True when quantity is negative (a return or cancellation)."),
    "revenue": ("DOUBLE", "quantity x unit_price - discount. Negative for returns."),
    "cost": ("DOUBLE", "quantity x unit_cost. Empty when cost is unknown."),
    "profit": ("DOUBLE", "revenue - cost. Empty when cost is unknown."),
    "year": ("INTEGER", "Year of order_date."),
    "month": ("INTEGER", "Month number (1-12) of order_date."),
    "week": ("INTEGER", "ISO week number of order_date."),
    "weekday": ("VARCHAR", "Day name of order_date, e.g. Monday."),
    "upload_id": ("INTEGER", "Which upload the row came from."),
}

# Canonical columns stored in the sales table, in order.
SALES_COLUMNS: tuple[str, ...] = (
    "order_id", "order_id_generated", "order_date", "product", "quantity", "unit_price", "customer_id", "unit_cost",
    "category", "discount", "location", "channel", "is_return", "revenue", "cost", "profit",
    "year", "month", "week", "weekday",
)

SQL_TYPES = {"date": "TIMESTAMP", "text": "VARCHAR", "number": "DOUBLE"}
