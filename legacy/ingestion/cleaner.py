"""Turn a raw table and a confirmed mapping into clean canonical sales rows.

Every check that fails adds a short reason code to the row ("bad order_date",
"quantity is 0", ...). Rows with any reason are rejected, never half-loaded.

Money rules
-----------
* With unit_price:   revenue = quantity x unit_price - discount
* With only amount:  amount is the final line value (after discount); quantity
                     defaults to 1 (-1 for a negative amount: a refund or credit
                     note) and unit_price is derived so the rule above holds.
* discount is stored in money with the same sign as the line, so for returns
  (negative quantity) it reduces the refund.
* unit_cost comes from unit_cost, else line_cost / quantity,
  else (revenue - line_profit) / quantity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from .fields import SALES_COLUMNS
from .mapper import Mapping
from .parsing import (
    DAY_MONTH, as_text, clean_ids, convert, detect_date_order, infer_type, is_missing,
    parse_dates, parse_numbers, unify_case,
)
from app.tools.table_reader import RawTable

MONEY_DECIMALS = 4
# Exports often fill these only on the first line of each order.
ORDER_LEVEL_FIELDS = ("order_date", "customer_id", "location", "channel")
SUMMARY_WORDS = {"total", "totals", "grand total", "sub total", "subtotal", "net total"}


@dataclass
class CleanResult:
    # SALES_COLUMNS + custom columns; index = file row number
    sales: pd.DataFrame
    rejected: pd.DataFrame  # row_number, reasons, raw
    custom_types: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class _Checks:
    def __init__(self, index: pd.Index):
        self.index = index
        self.masks: dict[str, pd.Series] = {}

    def add(self, reason: str, mask: pd.Series) -> None:
        mask = pd.Series(mask, index=self.index).fillna(False).astype(bool)
        if reason in self.masks:
            mask = self.masks[reason] | mask
        self.masks[reason] = mask

    def get(self, reason: str) -> pd.Series:
        return self.masks.get(reason, pd.Series(False, index=self.index))

    def failed(self) -> tuple[pd.Series, pd.Series]:
        """(failed mask, reasons) - reasons is '' for rows that passed."""
        reasons = pd.Series("", index=self.index, dtype=object)
        if not self.masks:
            return pd.Series(False, index=self.index), reasons
        table = pd.DataFrame(self.masks)
        failed = table.any(axis=1)
        if failed.any():
            reasons[failed] = [
                "; ".join(table.columns[row]) for row in table[failed].to_numpy()
            ]
        return failed, reasons


def clean(
    raw: RawTable,
    mapping: Mapping,
    *,
    custom_types: dict[str, str] | None = None,
    today: date | None = None,
    default_date_order: str = "dayfirst",
) -> CleanResult:
    """Clean ``raw`` using ``mapping``. ``custom_types`` fixes types of custom columns
    that already exist in the database."""
    fields = mapping.fields
    warnings: list[str] = []
    df = _fill_order_level(raw.df, fields, warnings)
    idx = df.index
    checks = _Checks(idx)
    out = pd.DataFrame(index=idx)

    def number(fname: str) -> tuple[pd.Series, pd.Series, pd.Series]:
        values, bad, pct = parse_numbers(df[fields[fname]])
        checks.add(f"bad {fname}", bad)
        return values, bad, pct

    # --- order_date -------------------------------------------------------
    raw_dates = as_text(df[fields["order_date"]])
    dates, bad = parse_dates(raw_dates, mapping.date_order, default_date_order)
    checks.add("missing order_date", is_missing(raw_dates))
    checks.add("bad order_date", bad)
    today_ts = pd.Timestamp(today or date.today())
    checks.add("order_date in the future", dates > today_ts + pd.Timedelta(days=1))
    if (
        mapping.date_order == "auto"
        and raw_dates.str.match(DAY_MONTH).any()
        and detect_date_order(raw_dates) is None
    ):
        warnings.append(
            f"Dates like 03/04/2025 are ambiguous; they were read as {default_date_order}. "
            "Set date_order if that is wrong."
        )
    out["order_date"] = dates

    # --- product ----------------------------------------------------------
    product = unify_case(df[fields["product"]])
    checks.add("missing product", product == "")
    out["product"] = product

    # --- quantity ---------------------------------------------------------
    if "quantity" in fields:
        qty, bad, _ = number("quantity")
        checks.add("missing quantity", qty.isna() & ~bad)
    else:  # amount-only files: one line = one unit of service
        qty = pd.Series(1.0, index=idx)
    checks.add("quantity is 0", qty == 0)
    safe_qty = qty.where(qty != 0)

    # --- discount (split into a rate or an amount of money) ---------------
    rate = pd.Series(np.nan, index=idx)
    money = pd.Series(np.nan, index=idx)
    if "discount" in fields:
        values, _, pct = number("discount")
        signed_rate, signed_money = _split_discount(values, pct, mapping.discount_type)
        rate, money = signed_rate.abs(), signed_money.abs()
        checks.add("bad discount", rate > 1)

    # --- gross line value and unit price ----------------------------------
    if "unit_price" in fields:
        price, bad, _ = number("unit_price")
        checks.add("missing unit_price", price.isna() & ~bad)
        gross = qty * price
        disc = money.fillna(rate * gross.abs()).fillna(0.0)
    else:
        amount, bad, _ = number("amount")
        checks.add("missing amount", amount.isna() & ~bad)
        if "quantity" not in fields:  # a negative amount is a refund of one unit, not a negative price
            qty = qty.where(~(amount < 0), -1.0)
            safe_qty = qty.where(qty != 0)
        checks.add("bad discount", rate >= 1)
        sign = np.sign(amount)
        gross = amount.where(rate.isna(), amount / (1 - rate.where(rate < 1)))
        gross = gross.where(money.isna(), amount + sign * money)
        disc = (gross - amount).abs().fillna(0.0)
        price = gross / safe_qty
    checks.add("negative unit_price", price < 0)
    # A discount may carry the line's sign (returns are negative);
    # a negative discount on a normal sale is invalid.
    if "discount" in fields:
        checks.add("bad discount", ((signed_money < 0) | (signed_rate < 0)) & (gross > 0))
    checks.add("discount larger than line value", disc > gross.abs() + 1e-9)
    discount = np.sign(gross) * disc
    revenue = gross - discount

    # --- unit cost --------------------------------------------------------
    unit_cost = pd.Series(np.nan, index=idx)
    if "unit_cost" in fields:
        unit_cost, _, _ = number("unit_cost")
    elif "line_cost" in fields:
        line_cost, _, _ = number("line_cost")
        unit_cost = line_cost / safe_qty
    elif "line_profit" in fields:
        line_profit, _, _ = number("line_profit")
        unit_cost = (revenue - line_profit) / safe_qty
    checks.add("negative unit_cost", unit_cost < -1e-9)

    # --- text dimensions --------------------------------------------------
    out["order_id"] = clean_ids(df[fields["order_id"]]) if "order_id" in fields else None
    out["order_id_generated"] = out["order_id"].isna()
    out["customer_id"] = clean_ids(df[fields["customer_id"]]) if "customer_id" in fields else None
    if "category" in fields:
        category = unify_case(df[fields["category"]])
        out["category"] = category.where(category != "", "Uncategorized")
    else:
        out["category"] = None
    for fname in ("location", "channel"):
        if fname in fields:
            values = unify_case(df[fields[fname]])
            out[fname] = values.where(values != "", None)
        else:
            out[fname] = None

    # --- numbers and derived columns --------------------------------------
    out["quantity"] = qty
    out["unit_price"] = price.round(MONEY_DECIMALS)
    out["unit_cost"] = unit_cost.round(MONEY_DECIMALS)
    out["discount"] = discount.round(MONEY_DECIMALS) + 0.0  # + 0.0 turns -0.0 into 0.0
    out["revenue"] = revenue.round(MONEY_DECIMALS)
    out["cost"] = (qty * unit_cost).round(MONEY_DECIMALS)
    out["profit"] = (out["revenue"] - out["cost"]).round(MONEY_DECIMALS)
    out["is_return"] = qty < 0
    out["year"] = dates.dt.year.astype("Int64")
    out["month"] = dates.dt.month.astype("Int64")
    out["week"] = dates.dt.isocalendar().week.astype("Int64")
    out["weekday"] = dates.dt.day_name()

    if "order_id" in fields:
        _warn_repeated_order_ids(out["order_id"], dates, qty > 0, warnings)

    # --- custom columns ---------------------------------------------------
    known = custom_types or {}
    types: dict[str, str] = {}
    for source, name in mapping.custom.items():
        sql_type = known.get(name) or infer_type(name, df[source])
        values, bad = convert(df[source], sql_type, mapping.date_order, default_date_order)
        if bad.any():
            warnings.append(
                f"{int(bad.sum())} value(s) in custom column '{name}' could not be read "
                f"as {sql_type} and were left empty."
            )
        out[name] = values
        types[name] = sql_type

    # --- split good and rejected rows -------------------------------------
    failed, reasons = checks.failed()
    summary = df.apply(lambda col: col.str.lower().isin(SUMMARY_WORDS)).any(axis=1)
    reasons[failed & summary & is_missing(raw_dates)] = "summary row"
    rejected = _rejected_rows(raw, failed, reasons)
    columns = list(SALES_COLUMNS) + list(types)
    return CleanResult(
        sales=out.loc[~failed, columns],
        rejected=rejected,
        custom_types=types,
        warnings=warnings,
    )


REPEATED_ORDER_SHARE = 0.05


def _warn_repeated_order_ids(order_ids: pd.Series, dates: pd.Series, sale: pd.Series, warnings: list[str]) -> None:
    """Warn when many order numbers appear on more than one day: systems that restart
    bill numbers (daily, yearly, per store) would have separate orders counted as one."""
    lines = pd.DataFrame({"order": order_ids, "day": dates.dt.date})[sale & order_ids.notna() & dates.notna()]
    if lines.empty:
        return
    days = lines.groupby("order")["day"].nunique()
    repeated = days[days > 1]
    if len(repeated) >= 10 and len(repeated) >= REPEATED_ORDER_SHARE * len(days):
        example = repeated.index[0]
        on = sorted(lines.loc[lines["order"] == example, "day"].unique())[:2]
        warnings.append(
            f"{len(repeated):,} order numbers appear on more than one day (e.g. {example} on "
            f"{on[0]} and {on[1]}). If your system restarts bill numbers, those orders are counted as one - "
            "combine the number with the date or store in the file to keep them apart."
        )


def _fill_order_level(df: pd.DataFrame, fields: dict[str, str], warnings: list[str]) -> pd.DataFrame:
    """Copy blank order-level values from earlier lines of the same order."""
    if "order_id" not in fields:
        return df
    order_ids = as_text(df[fields["order_id"]])
    groups = order_ids.where(order_ids != "")
    df = df.copy()
    filled = 0
    for fname in ORDER_LEVEL_FIELDS:
        if fname not in fields:
            continue
        col = df[fields[fname]]
        values = col.where(col != "")
        forward = values.groupby(groups).ffill()
        take = values.isna() & forward.notna()
        if take.any():
            df.loc[take, fields[fname]] = forward[take]
            filled += int(take.sum())
    if filled:
        warnings.append(
            f"Filled {filled} empty order-level value(s) (date, customer, location or channel) "
            "from earlier lines of the same order."
        )
    return df


def _split_discount(
    values: pd.Series, pct: pd.Series, discount_type: str
) -> tuple[pd.Series, pd.Series]:
    """Return (rate 0-1, money). Exactly one of them is set per row."""
    nan = pd.Series(np.nan, index=values.index)
    if discount_type == "amount":
        return nan, values
    # Decided per column, never per row: a column where every plain value is <= 1
    # holds fractions (0.1 = 10%); otherwise plain values are percentage points
    # (so 1 means 1%, not 100%). Values written with '%' are always percentages.
    plain = values.where(~pct)
    fractions = plain.dropna().abs().le(1).all()
    rate = (values / 100).where(pct)
    if discount_type == "percent":
        return rate.fillna(plain if fractions else plain / 100), nan
    if fractions and plain.notna().any():
        return rate.fillna(plain), nan
    return rate, plain


def _rejected_rows(raw: RawTable, failed: pd.Series, reasons: pd.Series) -> pd.DataFrame:
    bad = raw.df.loc[failed].rename(columns=raw.original_names)
    rows = pd.DataFrame({
        "row_number": bad.index.astype(int),
        "reasons": reasons.loc[failed].values,
        "raw": bad.to_dict("records"),
    })
    if raw.bad_lines:
        extra = pd.DataFrame({
            "row_number": [n for n, _ in raw.bad_lines],
            "reasons": "wrong number of fields",
            "raw": [{"fields": fields} for _, fields in raw.bad_lines],
        })
        rows = pd.concat([rows, extra], ignore_index=True)
    return rows.sort_values("row_number", ignore_index=True)
