"""Parse the messy values found in real exports - for any domain and locale.

Every parser takes a Series of strings and returns the parsed values plus a
``bad`` mask: True where a non-empty value could not be read. A value either
parses cleanly or is flagged; nothing is silently changed.

Ported from the analyst project (analyst/values.py, tested there on 9 real datasets);
type_columns is new: the lossless typing applied when a file enters the workspace.
"""

from __future__ import annotations

import re
import sys
import unicodedata
import warnings

import pandas as pd

NULL_TOKENS = {
    "", "na", "n/a", "n.a.", "nan", "null", "none", "nil", "-", "--", "?", "missing",
    "#n/a", "#na", "#null!", "#div/0!", "#value!", "#ref!", "#num!", "#name?",
}

# Every currency sign Unicode knows ($ € £ ¥ ₹ ₩ ₽ ...), plus ISO codes (USD, INR) and "Rs".
_SIGNS = "".join(
    c for c in map(chr, range(sys.maxunicode + 1)) if unicodedata.category(c) == "Sc"
)
_CURRENCY = rf"(?:[{re.escape(_SIGNS)}]|\b[A-Z]{{3}}\b|\b[Rr]s\.?)"
_PLAIN = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_NUMBER = r"\(?[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?\)?%?"
# Decimal comma: '1.234,50' or '3,50'. A comma followed by only 1-2 digits can't be
# a thousands separator in any convention (1,234 / 1,23,456).
_DECIMAL_COMMA = r"\(?[-+]?(?:\d{1,3}(?:[.\s']\d{3})+,\d+|\d+,\d{1,2})%?\)?"

_YEAR_FIRST = r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"
_DAY_MONTH = r"^(\d{1,2})[-/.](\d{1,2})[-/.]\d{2,4}"
_TIME_THEN_TZ = r"(\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?)\s*(?:Z|[+-]\d{2}:?\d{2})$"
CLOCK = r"\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:\s*[AaPp]\.?[Mm]\.?)?"
COMPACT_DATE = r"(?:19|20)\d\d(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])"
EARLIEST = pd.Timestamp("1700-01-01")  # historical series (Nile 1871, sunspots 1700); pandas starts at 1677
LATEST = pd.Timestamp("2100-12-31")

TRUE = {"yes", "y", "true", "t"}
FALSE = {"no", "n", "false", "f"}


def as_text(s: pd.Series) -> pd.Series:
    """Stripped strings with missing values as ''."""
    return s.astype("string").fillna("").str.strip().astype(object)


def is_missing(t: pd.Series) -> pd.Series:
    return t.str.lower().isin(NULL_TOKENS)


def parse_numbers(s: pd.Series) -> tuple[pd.Series, pd.Series, str | None]:
    """Read '₹1,23,456.50', 'USD 1,200', '€3,50', '(500)', '12.5%', '1.2E+05'.

    Returns (values, bad, unit). ``unit`` is the currency sign/code or '%' used by
    most values, else None. Percentages keep their written value (10% -> 10.0).
    """
    t = as_text(s)
    plain = t.str.fullmatch(_PLAIN)
    if plain.all():
        none = pd.Series(False, index=t.index)
        return pd.to_numeric(t).astype(float), none, None

    missing = is_missing(t)
    signs = t.str.extract(f"({_CURRENCY})", expand=False)
    unit = _most_common(signs)
    t = t.str.replace(_CURRENCY, "", regex=True).str.replace(r"\s+", "", regex=True)
    percent = t.str.endswith("%")
    if unit is None and percent[~missing].mean() > 0.5:
        unit = "%"

    decimal_comma = t.str.fullmatch(_DECIMAL_COMMA)
    t = t.where(~decimal_comma, t.str.replace(r"[.']", "", regex=True).str.replace(",", ".", regex=False))
    t = t.str.replace(r"[,']", "", regex=True)

    ok = t.str.fullmatch(_NUMBER) & ~missing & (t.str.count(r"\(") == t.str.count(r"\)"))
    negative = ok & t.str.match(r"^\(.*\)%?$")
    digits = t.str.replace(r"[()%+]", "", regex=True)
    values = pd.to_numeric(digits.where(ok), errors="coerce").astype(float)
    values = values.where(~negative, -values.abs())
    bad = ~missing & ~ok
    return values, bad, unit


def date_order(s: pd.Series) -> str | None:
    """'dayfirst' / 'monthfirst' from values like 13/03/2025, None if ambiguous."""
    parts = as_text(s).str.extract(_DAY_MONTH).dropna()
    if parts.empty:
        return None
    first, second = parts[0].astype(int), parts[1].astype(int)
    if (first > 12).any() and not (second > 12).any():
        return "dayfirst"
    if (second > 12).any() and not (first > 12).any():
        return "monthfirst"
    return None


def parse_dates(s: pd.Series, order: str | None = None) -> tuple[pd.Series, pd.Series, str | None]:
    """Parse ISO dates, day/month dates, month names, Excel serials and timezones.

    Returns (values, bad, order_used). ``order`` 'dayfirst'/'monthfirst' forces the
    reading of dates like 03/04/2025; by default the column's own values decide,
    and a fully ambiguous column is read day-first and reported as ambiguous
    (order_used 'ambiguous') so the caller can ask.
    """
    t = as_text(s).str.replace(_TIME_THEN_TZ, r"\1", regex=True)
    missing = is_missing(t)
    out = pd.Series(pd.NaT, index=t.index, dtype="datetime64[ns]")

    year_first = ~missing & t.str.match(_YEAR_FIRST)
    compact = ~missing & ~year_first & t.str.fullmatch(COMPACT_DATE)
    serial = ~missing & ~year_first & ~compact & t.str.fullmatch(r"\d{5}(?:\.\d+)?")
    # A time of day on its own ('11:38:36', '9:05 PM') is not a date: pandas would stamp today's date on it.
    clock = ~missing & t.str.fullmatch(CLOCK)
    other = ~missing & ~year_first & ~compact & ~serial & ~clock
    used = None

    if year_first.any():
        out[year_first] = _to_datetime(t[year_first], dayfirst=False)
    if compact.any():
        out[compact] = pd.to_datetime(t[compact], format="%Y%m%d", errors="coerce")
    if serial.any():
        days = pd.to_numeric(t[serial])
        # Only real day numbers: pandas' unit conversion reads uninitialised memory for NaN
        # inputs (np.empty in cast_from_unit_vectorized) and can fail at random.
        days = days[(days > 20000) & (days < 80000)]
        out[days.index] = pd.to_datetime(days, unit="D", origin="1899-12-30")
    if other.any():
        used = order or date_order(t[other])
        if used is None and t[other].str.match(_DAY_MONTH).any():
            used = "ambiguous"
        out[other] = _to_datetime(t[other], dayfirst=used != "monthfirst")

    out = out.where((out >= EARLIEST) & (out <= LATEST))
    bad = ~missing & out.isna()
    return out, bad, used


def _to_datetime(t: pd.Series, dayfirst: bool) -> pd.Series:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            out = _in_range(pd.to_datetime(t, dayfirst=dayfirst, errors="coerce"))
        except (ValueError, OverflowError):
            out = pd.Series(pd.NaT, index=t.index, dtype="datetime64[ns]")
        left = out.isna()
        if left.any():
            try:
                parsed = pd.to_datetime(t[left], format="mixed", dayfirst=dayfirst, errors="coerce")
            except (ValueError, OverflowError):
                parsed = pd.Series([_one(v, dayfirst) for v in t[left]], index=t[left].index)
            out[left] = _in_range(parsed)
    return out


def _in_range(values) -> pd.Series:
    s = pd.Series(values)
    keep = s.notna() & (s >= EARLIEST) & (s <= LATEST)
    return pd.to_datetime(s.where(keep), errors="coerce").astype("datetime64[ns]")


def _one(value: str, dayfirst: bool):
    try:
        return pd.to_datetime(value, dayfirst=dayfirst)
    except (ValueError, OverflowError):
        return pd.NaT


def parse_booleans(s: pd.Series) -> tuple[pd.Series, pd.Series]:
    t = as_text(s).str.lower()
    values = pd.Series(pd.NA, index=t.index, dtype="boolean")
    values[t.isin(TRUE)] = True
    values[t.isin(FALSE)] = False
    return values, ~is_missing(t) & values.isna()


def infer_type(s: pd.Series, sample: int = 2000) -> str:
    """'bool', 'number', 'date' or 'text' - whichever reads >= 95% of the values."""
    t = as_text(s)
    present = t[~is_missing(t)]
    if len(present) > sample:
        present = present.sample(sample, random_state=0)
    if present.empty:
        return "text"
    if present.str.lower().isin(TRUE | FALSE).all() and present.str.lower().nunique() <= 2:
        return "bool"
    # Codes with leading zeros ('00123', '007') lose meaning as numbers.
    if present.str.fullmatch(r"0\d+").mean() > 0.2:
        return "text"
    # Compact dates (19970101): 8 digits that are almost all valid calendar dates are dates;
    # random 8-digit numbers almost never are.
    if present.str.fullmatch(COMPACT_DATE).mean() >= 0.98 and present.nunique() > 1:
        return "date"
    if parse_numbers(present)[1].mean() <= 0.05:
        return "number"
    if parse_dates(present)[1].mean() <= 0.05:
        return "date"
    return "text"


def _most_common(s: pd.Series) -> str | None:
    counts = s.dropna().str.strip().value_counts()
    return str(counts.index[0]) if len(counts) else None


def type_columns(raw: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, dict]]:
    """Give each column of strings its type - only where every value reads cleanly.

    A column is converted when all its non-missing values parse as numbers, dates (with a known
    day/month order) or yes/no; otherwise it stays text (inspect_data reports columns that are
    mostly numbers or dates). Dates that read either way (03/04/2025) are noted as
    ``info[col]['pending']`` - only a person can settle the order. Missing tokens ('', 'N/A',
    '#N/A' ...) become missing values. Returns (typed frame, info per column).
    """
    out = pd.DataFrame(index=raw.index)
    info: dict[str, dict] = {}
    for name in raw.columns:
        t = as_text(raw[name])
        missing = is_missing(t)
        kind = infer_type(t)
        col: dict = {"type": "text"}
        if kind == "bool":
            values, bad = parse_booleans(t)
            if not bad.any():
                out[name], col = values, {"type": "bool"}
        elif kind == "number":
            values, bad, unit = parse_numbers(t)
            if not bad.any():
                whole = values.dropna()
                if len(whole) and (whole == whole.round()).all() and whole.abs().max() < 2**53:
                    values = values.astype("Int64")
                out[name], col = values, {"type": "number", **({"unit": unit} if unit else {})}
        elif kind == "date":
            values, bad, order = parse_dates(t)
            if order == "ambiguous":
                col["pending"] = {"as": "date", "ambiguous_order": True,
                                  "examples": t[~missing].drop_duplicates().head(3).tolist()}
            elif not bad.any():
                out[name], col = values, {"type": "date", **({"date_order": order} if order else {})}
        if name not in out:
            out[name] = t.where(~missing, None)
        info[name] = col
    return out, info

