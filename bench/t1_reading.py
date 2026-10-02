"""Test 1 - reading and mapping the original files, as a user would upload them.

For each file: the mapping the app suggests vs the reference, then the app's cleaning
compared row by row with an independent pandas reading of the same file (revenue,
dates, rows dropped and why)."""

from __future__ import annotations

import json
import time

import numpy as np
import pandas as pd

from app.db.connection import Database
from app.ingestion.cleaner import clean
from app.ingestion.mapper import suggest_mapping, validate_mapping
from app.ingestion.profiles import load_profiles
from app.ingestion.reader import read_file

from common import CDNOW_RAW, COFFEE, PIZZA, REFERENCE, RETAIL, RETAIL_SHEETS, WORK, prepare

# Online Retail II stock codes that are not products (postage, fees, manual adjustments...).
NON_PRODUCT_CODES = {"POST", "DOT", "M", "AMAZONFEE", "BANK CHARGES", "CRUK", "D", "S", "ADJUST",
                     "ADJUST2", "B", "PADS", "C2", "TEST001", "TEST002", "GIFT", "SP1002"}


def independent(name: str, file, sheet) -> pd.DataFrame:
    """date, qty, revenue per file row, read with plain pandas; index = file row number."""
    cache = WORK / f"indep_{name}_{(sheet or 'main').replace(' ', '_')}.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    if name == "retail":
        df = pd.read_excel(file, sheet_name=sheet)
        out = pd.DataFrame({"date": pd.to_datetime(df["InvoiceDate"]), "qty": df["Quantity"].astype(float),
                            "revenue": df["Quantity"] * df["Price"], "code": df["StockCode"].astype(str),
                            "product": df["Description"].astype("string")})
    elif name == "coffee":
        df = pd.read_excel(file)
        out = pd.DataFrame({"date": pd.to_datetime(df["transaction_date"]), "qty": df["transaction_qty"].astype(float),
                            "revenue": df["transaction_qty"] * df["unit_price"]})
    elif name == "pizza":
        df = pd.read_csv(file)
        out = pd.DataFrame({"date": pd.to_datetime(df["date"]), "qty": df["quantity"].astype(float),
                            "revenue": df["quantity"] * df["price"]})
    else:  # CDNOW as published
        df = pd.read_csv(file, sep=r"\s+", header=None, names=["c", "d", "q", "a"], dtype={"d": str})
        out = pd.DataFrame({"date": pd.to_datetime(df["d"], format="%Y%m%d"), "qty": df["q"].astype(float),
                            "revenue": df["a"].astype(float)})
    out.index = np.arange(len(out)) + 2  # header on row 1
    out.to_parquet(cache)
    return out


def score_mapping(name: str, fields: dict[str, str]) -> dict:
    ref = REFERENCE[name]
    right = [f for f in ref if fields.get(f) in ref[f]]
    wrong = {f: fields[f] for f in ref if f in fields and fields[f] not in ref[f]}
    missing = [f for f in ref if f not in fields]
    extra = {f: s for f, s in fields.items() if f not in ref}
    return {"right": right, "wrong": wrong, "missing": missing, "extra_fields": extra,
            "score": f"{len(right)}/{len(ref)}"}


def read(file, sheet):
    t0 = time.time()
    try:
        return read_file(file, sheet), round(time.time() - t0, 1), None
    except Exception as exc:  # noqa: BLE001 - the test records any failure
        return None, None, f"{type(exc).__name__}: {exc}"


def check(name: str, file, sheet, use_profiles: bool, raw, read_s) -> dict:
    db = Database(":memory:")
    cur = db.cursor()
    profiles = load_profiles(cur) if use_profiles else []
    cur.close()
    suggestion = suggest_mapping(list(raw.df.columns), profiles, raw.original_names)
    mapping = suggestion.mapping
    result = {
        "dataset": name, "sheet": sheet, "profiles": use_profiles, "profile_used": suggestion.profile,
        "read_seconds": read_s, "rows_in_file": len(raw.df) + len(raw.bad_lines),
        "header_row": raw.header_row, "columns": list(raw.df.columns),
        "mapping": mapping.fields, "mapping_score": score_mapping(name, mapping.fields),
        "missing_required": suggestion.missing_required,
    }
    try:
        validate_mapping(mapping, list(raw.df.columns))
    except Exception as exc:  # noqa: BLE001
        # The user would fix this on the mapping screen: fill the missing required fields
        # from the reference, so reading and cleaning can still be checked.
        result["cannot_load_as_suggested"] = str(exc)
        ref = REFERENCE[name]
        for f in ("order_date", "product", "quantity", "unit_price", "amount"):
            if f in ref and f not in mapping.fields:
                source = ref[f][0]
                mapping.custom.pop(source, None)
                mapping.fields = {k: v for k, v in mapping.fields.items() if v != source}
                mapping.fields[f] = source
        result["completed_by_hand"] = mapping.fields
        try:
            validate_mapping(mapping, list(raw.df.columns))
        except Exception as exc2:  # noqa: BLE001
            result["cannot_load"] = str(exc2)
            return result

    try:
        cleaned = clean(raw, mapping, default_date_order="dayfirst")
    except Exception as exc:  # noqa: BLE001
        result["clean_error"] = f"{type(exc).__name__}: {exc}"
        return result
    kept = cleaned.sales
    ind = independent(name, file, sheet)
    if raw.header_row != 1 or len(ind) != len(raw.df) + len(raw.bad_lines):
        result["alignment"] = f"rows differ: app {len(raw.df)}, pandas {len(ind)}, header row {raw.header_row}"
    common = kept.index.intersection(ind.index)
    a, b = kept.loc[common], ind.loc[common]
    qty_ok = np.isclose(a["quantity"].astype(float), b["qty"]).mean()
    rev_diff = (a["revenue"].astype(float) - b["revenue"]).abs()
    day_diff = (pd.to_datetime(a["order_date"]).dt.normalize() != b["date"].dt.normalize())
    reasons = cleaned.rejected["reasons"].value_counts().to_dict() if len(cleaned.rejected) else {}
    result.update({
        "rows_kept": len(kept), "rows_rejected": len(cleaned.rejected), "rejected_reasons": reasons,
        "rows_compared": len(common),
        "quantity_match_pct": round(100 * qty_ok, 3),
        "revenue_app": round(float(a["revenue"].sum()), 2),
        "revenue_pandas_same_rows": round(float(b["revenue"].sum()), 2),
        "revenue_diff_pct": round(100 * abs(a["revenue"].sum() - b["revenue"].sum()) / abs(b["revenue"].sum()), 4),
        "rows_revenue_wrong": int((rev_diff > 0.01).sum()),
        "rows_date_wrong": int(day_diff.sum()),
        "revenue_pandas_all_rows": round(float(ind["revenue"].sum()), 2),
        "warnings": cleaned.warnings,
    })
    if name == "retail":
        non_product = b["code"].str.upper().isin(NON_PRODUCT_CODES)
        result["kept_non_product_lines"] = int(non_product.sum())
        result["kept_non_product_revenue"] = round(float(b.loc[non_product, "revenue"].sum()), 2)
    return result


def main():
    prepare()
    runs = []
    jobs = [("retail", RETAIL, s, (True, False)) for s in RETAIL_SHEETS]
    jobs += [("coffee", COFFEE, None, (True,)), ("pizza", PIZZA, None, (True,)), ("cdnow", CDNOW_RAW, None, (True,))]
    for name, file, sheet, modes in jobs:
        raw, seconds, error = read(file, sheet)
        for use_profiles in modes:
            if error:
                runs.append({"dataset": name, "sheet": sheet, "profiles": use_profiles, "read_error": error})
            else:
                runs.append(check(name, file, sheet, use_profiles, raw, seconds))
            print(json.dumps({k: v for k, v in runs[-1].items() if k != "columns"}, default=str), flush=True)
    (WORK / "t1_reading.json").write_text(json.dumps(runs, indent=2, default=str))


if __name__ == "__main__":
    main()
