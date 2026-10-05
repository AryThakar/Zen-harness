"""Test 2 - messy versions of the same real rows. Each level adds the junk real exports have;
the app should read every level to the same rows, revenue and dates as the clean file.

  L0  plain CSV, ISO dates
  L1  + title lines above the header, a blank line and a 'Total' footer row
  L2  + ';' delimiter, decimal commas with '.' thousands, Windows-1252 encoding
  L3  + currency signs on prices, local text dates (US files month-first, the UK file
        day-first), blank lines inside the data
"""

from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd

from legacy.ingestion.cleaner import clean
from legacy.ingestion.mapper import suggest_mapping
from app.tools.table_reader import read_file

from common import COFFEE, PIZZA, REFERENCE, RETAIL, WORK

SOURCES = {
    # name: (date column, price column, quantity column, currency, date style)
    "pizza": ("date", "price", "quantity", "$", "monthfirst"),
    "coffee": ("transaction_date", "unit_price", "transaction_qty", "$", "monthfirst"),
    "retail": ("InvoiceDate", "Price", "Quantity", "£", "dayfirst"),
}


def source(name: str) -> pd.DataFrame:
    cache = WORK / f"messy_source_{name}.csv"
    if cache.exists():
        return pd.read_csv(cache, dtype=str, keep_default_na=False)
    if name == "pizza":
        df = pd.read_csv(PIZZA, dtype=str, keep_default_na=False)
    elif name == "coffee":
        df = pd.read_excel(COFFEE)
        df["transaction_date"] = pd.to_datetime(df["transaction_date"]).dt.strftime("%Y-%m-%d")
        df = df.astype(str)
    else:
        df = pd.read_excel(RETAIL, sheet_name="Year 2010-2011", nrows=40000)
        df["InvoiceDate"] = pd.to_datetime(df["InvoiceDate"]).dt.strftime("%Y-%m-%d %H:%M:%S")
        df = df.fillna("").astype(str)
        df["Customer ID"] = df["Customer ID"].str.replace(r"\.0$", "", regex=True)
    df.to_csv(cache, index=False)
    return df


def euro(x: str, currency: str | None = None) -> str:
    """'1234.5' -> '1.234,50' (and '€ 1.234,50' with a currency)."""
    try:
        v = float(x)
    except ValueError:
        return x
    s = f"{abs(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    s = ("-" if v < 0 else "") + s
    return f"{currency} {s}" if currency else s


def write_level(name: str, df: pd.DataFrame, level: int) -> str:
    date_col, price_col, qty_col, currency, style = SOURCES[name]
    df = df.copy()
    delim, encoding = ",", "utf-8"
    if level >= 2:
        delim, encoding = ";", "cp1252"
        df[price_col] = [euro(v, currency if level >= 3 else None) for v in df[price_col]]
    if level >= 3:
        d = pd.to_datetime(df[date_col])
        fmt = "%m/%d/%Y" if style == "monthfirst" else "%d/%m/%Y"
        if d.dt.hour.any():
            fmt += " %H:%M"
        df[date_col] = d.dt.strftime(fmt)
    lines = []
    if level >= 1:
        lines += [f"Sales report - {name}", "Exported on 01-10-2026 10:00 by admin", ""]
    lines.append(delim.join(df.columns))
    body = [delim.join(f'"{v}"' if delim in v or '"' in v else v for v in row) for row in df.to_numpy()]
    if level >= 3:
        for k in (len(body) // 3, 2 * len(body) // 3):
            body.insert(k, "")
    lines += body
    if level >= 1:
        total = [""] * len(df.columns)
        total[0] = "Total"
        total[list(df.columns).index(qty_col)] = str(int(pd.to_numeric(df[qty_col], errors="coerce").sum()))
        lines += ["", delim.join(total)]
    path = WORK / f"messy_{name}_L{level}.csv"
    path.write_bytes("\r\n".join(lines).encode(encoding, errors="replace"))
    return str(path)


def read(name: str, path: str):
    """Auto mapping as the app suggests it; required fields it misses are filled from the
    reference (as a user would on the mapping screen) - this test is about the format."""
    raw = read_file(path)
    suggestion = suggest_mapping(list(raw.df.columns), [], raw.original_names)
    mapping = suggestion.mapping
    for f in ("order_date", "product", "quantity", "unit_price"):
        if f in REFERENCE[name] and f not in mapping.fields:
            source = REFERENCE[name][f][0]
            mapping.custom.pop(source, None)
            mapping.fields[f] = source
    result = clean(raw, mapping, default_date_order="dayfirst")
    return raw, suggestion, result


def main(names=None):
    rows = []
    for name in names or list(SOURCES):
        df = source(name)
        base = None
        for level in range(4):
            path = write_level(name, df, level)
            try:
                raw, suggestion, result = read(name, path)
            except Exception as exc:  # noqa: BLE001
                rows.append({"dataset": name, "level": level, "error": f"{type(exc).__name__}: {exc}"})
                print(json.dumps(rows[-1]), flush=True)
                continue
            kept = result.sales.sort_index()
            row = {"dataset": name, "level": level, "header_row": raw.header_row,
                   "delimiter": raw.delimiter, "encoding": raw.encoding,
                   "mapping": suggestion.mapping.fields, "rows_kept": len(kept),
                   "rows_rejected": len(result.rejected),
                   "rejected_reasons": result.rejected["reasons"].value_counts().to_dict(),
                   "revenue": round(float(kept["revenue"].sum()), 2), "warnings": result.warnings}
            if base is None:
                base = kept
            else:
                same = len(kept) == len(base)
                row["same_rows"] = same
                row["revenue_diff_pct"] = round(100 * abs(kept["revenue"].sum() - base["revenue"].sum())
                                                / abs(base["revenue"].sum()), 4)
                if same:
                    a = pd.to_datetime(kept["order_date"]).dt.normalize().to_numpy()
                    b = pd.to_datetime(base["order_date"]).dt.normalize().to_numpy()
                    row["dates_wrong"] = int((a != b).sum())
                    row["rows_revenue_wrong"] = int((np.abs(kept["revenue"].to_numpy()
                                                            - base["revenue"].to_numpy()) > 0.01).sum())
                row["same_mapping"] = suggestion.mapping.fields == rows[-level]["mapping"]
            rows.append(row)
            print(json.dumps(row, default=str), flush=True)
    (WORK / "t2_messy.json").write_text(json.dumps(rows, indent=2, default=str))


if __name__ == "__main__":
    main(sys.argv[1:] or None)
