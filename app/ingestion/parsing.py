"""Strict, vectorized parsers for the messy values found in business exports.

Every parser takes a string Series (missing values as "") and returns the parsed
values plus a ``bad`` mask: True where a non-empty value could not be read.
Rows are never silently corrupted - a value either parses cleanly or is flagged.
"""

from __future__ import annotations

import re
import warnings

import pandas as pd

NULL_TOKENS = {"", "na", "n/a", "nan", "null", "none", "nil", "-", "--"}

_CURRENCY = r"(?i)(?:rs\.?|inr|usd|eur|gbp|aud|cad|[₹$€£¥])"
_NUMBER = r"\(?[-+]?(?:\d+\.?\d*|\.\d+)\)?%?"
# Decimal comma: '1.234,50' or '3,50'. A comma followed by only 1-2 digits
# can't be a thousands separator in any convention (1,234 / 1,23,456).
_DECIMAL_COMMA = r"-?(?:\d{1,3}(?:\.\d{3})+,\d+|\d+,\d{1,2})%?"

_YEAR_FIRST = r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"
DAY_MONTH = r"^(\d{1,2})[-/.](\d{1,2})[-/.]\d{2,4}"
_TZ_SUFFIX = r"\s*(?:Z|[+-]\d{2}:?\d{2})$"
_EXCEL_SERIAL = r"\d{5}(?:\.\d+)?"

_TRUE = {"yes", "y", "true"}
_FALSE = {"no", "n", "false"}

# Custom columns with these name endings stay text even if they look numeric
# (IDs and phone numbers lose meaning - and leading zeros - as numbers).
_ID_LIKE_NAME = re.compile(r"(?:^|_)(?:id|code|no|number|phone|mobile|zip|pincode|postal_code)$")


def as_text(s: pd.Series) -> pd.Series:
    """Stripped strings with missing values as ''."""
    return s.astype("string").fillna("").str.strip().astype(object)


def is_missing(s: pd.Series) -> pd.Series:
    return s.str.lower().isin(NULL_TOKENS)


def parse_numbers(s: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Parse amounts like '₹1,23,456.50', 'Rs. 500', '(500)', '10%', '1.234,50'.

    Returns (values, bad, is_percent). Percent values are returned as written
    (10% -> 10.0); the caller decides what a percentage means.
    """
    t = as_text(s)
    # Fast path: most values in numeric columns are already plain numbers.
    plain = t.str.fullmatch(r"[-+]?(?:\d+\.?\d*|\.\d+)")
    if plain.all():
        values = pd.to_numeric(t).astype(float)
        none = pd.Series(False, index=t.index)
        return values, none, none
    if plain.any():
        values, bad, pct = parse_numbers(t[~plain])
        fast = pd.to_numeric(t[plain]).astype(float)
        return (
            pd.concat([fast, values]).reindex(t.index),
            bad.reindex(t.index, fill_value=False),
            pct.reindex(t.index, fill_value=False),
        )
    missing = is_missing(t)

    decimal_comma = t.str.fullmatch(_DECIMAL_COMMA)
    t = t.where(
        ~decimal_comma, t.str.replace(".", "", regex=False).str.replace(",", ".", regex=False)
    )
    t = (
        t.str.replace(_CURRENCY, "", regex=True)
        .str.replace(",", "", regex=False)
        .str.replace(r"\s+", "", regex=True)
    )

    ok = t.str.fullmatch(_NUMBER) & ~missing
    pct = ok & t.str.endswith("%")
    negative = ok & t.str.match(r"^\(.*\)%?$")
    digits = t.str.replace(r"[()%+]", "", regex=True)

    values = pd.to_numeric(digits.where(ok), errors="coerce").astype(float)
    values = values.where(~negative, -values.abs())
    bad = ~missing & ~ok
    return values, bad, pct


def detect_date_order(s: pd.Series) -> str | None:
    """'dayfirst' / 'monthfirst' from values like 13/03/2025, or None if ambiguous."""
    parts = as_text(s).str.extract(DAY_MONTH).dropna()
    if parts.empty:
        return None
    first, second = parts[0].astype(int), parts[1].astype(int)
    if (first > 12).any() and not (second > 12).any():
        return "dayfirst"
    if (second > 12).any() and not (first > 12).any():
        return "monthfirst"
    return None


def parse_dates(
    s: pd.Series, order: str = "auto", default_order: str = "dayfirst"
) -> tuple[pd.Series, pd.Series]:
    """Parse ISO dates, day/month dates, Excel serial numbers and timezone suffixes.

    ``order`` is 'auto' | 'dayfirst' | 'monthfirst'. Year-first dates (ISO) are
    always read as year-month-day. Timezone offsets are dropped so the local
    wall-clock time is kept.
    """
    t = as_text(s).str.replace(_TZ_SUFFIX, "", regex=True)
    missing = is_missing(t)
    out = pd.Series(pd.NaT, index=t.index, dtype="datetime64[ns]")

    year_first = ~missing & t.str.match(_YEAR_FIRST)
    serial = ~missing & ~year_first & t.str.fullmatch(_EXCEL_SERIAL)
    other = ~missing & ~year_first & ~serial

    if year_first.any():
        out[year_first] = _to_datetime(t[year_first], dayfirst=False)
    if serial.any():
        days = pd.to_numeric(t[serial])
        days = days.where((days > 20000) & (days < 80000))
        out[serial] = pd.to_datetime(days, unit="D", origin="1899-12-30")
    if other.any():
        if order == "auto":
            order = detect_date_order(t[other]) or default_order
        out[other] = _to_datetime(t[other], dayfirst=order == "dayfirst")

    bad = ~missing & out.isna()
    return out, bad


def _to_datetime(t: pd.Series, dayfirst: bool) -> pd.Series:
    """Fast path with one inferred format, then per-value parsing for the leftovers."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        out = pd.to_datetime(t, dayfirst=dayfirst, errors="coerce")
        leftover = out.isna()
        if leftover.any():
            out[leftover] = pd.to_datetime(
                t[leftover], format="mixed", dayfirst=dayfirst, errors="coerce"
            )
    return out.astype("datetime64[ns]")


def parse_booleans(s: pd.Series) -> tuple[pd.Series, pd.Series]:
    t = as_text(s).str.lower()
    missing = is_missing(t)
    values = pd.Series(pd.NA, index=t.index, dtype="boolean")
    values[t.isin(_TRUE)] = True
    values[t.isin(_FALSE)] = False
    bad = ~missing & values.isna()
    return values, bad


def infer_type(name: str, s: pd.Series, sample_size: int = 1000) -> str:
    """Pick a DuckDB type for a custom column: DOUBLE, TIMESTAMP, BOOLEAN or VARCHAR."""
    t = as_text(s)
    present = t[~is_missing(t)]
    if len(present) > sample_size:
        present = present.sample(sample_size, random_state=0)
    if present.empty or _ID_LIKE_NAME.search(name):
        return "VARCHAR"
    if present.str.lower().isin(_TRUE | _FALSE).all():
        return "BOOLEAN"
    _, bad, _ = parse_numbers(present)
    if bad.mean() <= 0.05:
        return "DOUBLE"
    _, bad = parse_dates(present)
    if bad.mean() <= 0.05:
        return "TIMESTAMP"
    return "VARCHAR"


def convert(
    s: pd.Series, sql_type: str, date_order: str = "auto", default_order: str = "dayfirst"
) -> tuple[pd.Series, pd.Series]:
    """Convert a column to ``sql_type``. Returns (values, bad)."""
    if sql_type == "DOUBLE":
        values, bad, _ = parse_numbers(s)
        return values, bad
    if sql_type == "TIMESTAMP":
        return parse_dates(s, date_order, default_order)
    if sql_type == "BOOLEAN":
        return parse_booleans(s)
    t = as_text(s)
    return t.where(~is_missing(t), None), pd.Series(False, index=t.index)


def clean_ids(s: pd.Series) -> pd.Series:
    """Trim IDs and undo float formatting ('17850.0' -> '17850'). Missing -> None."""
    t = as_text(s).str.replace(r"^(\d+)\.0+$", r"\1", regex=True)
    return t.where(~is_missing(t), None)


def unify_case(s: pd.Series) -> pd.Series:
    """Collapse whitespace and give every case-variant its most common spelling.

    'wireless mouse', 'Wireless  Mouse' -> whichever spelling appears most often.
    """
    t = as_text(s).str.replace(r"\s+", " ", regex=True)
    counts = t.value_counts()
    counts = counts[counts.index != ""]
    if counts.empty:
        return t
    canon = pd.DataFrame({
        "spelling": counts.index,
        "count": counts.to_numpy(),
        "key": counts.index.str.lower(),
        "all_lower": counts.index == counts.index.str.lower(),
    })
    # Ties go to a capitalised spelling, then alphabetical, so results are stable.
    canon = canon.sort_values(["count", "all_lower", "spelling"], ascending=[False, True, True])
    lookup = canon.drop_duplicates("key").set_index("key")["spelling"]
    mapped = t.str.lower().map(lookup)
    return mapped.where(t != "", "").astype(object)
