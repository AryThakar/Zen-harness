"""clean_data: apply chosen cleaning steps to a table and save the result as a new table.

Each step is a fixed, tested operation with parameters; nothing is guessed. The original table is
never changed, and the log says exactly what every step did (counts, examples). Destructive
choices are explicit: outliers are only removed by remove_outliers, missing values are only
filled or dropped by a step that names its method. All steps run or none: if one is invalid,
nothing is saved.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from .core import ToolError, ToolResult, number, ok, tool
from .values import parse_dates, parse_numbers
from .workspace import Workspace, resolve_column


class _Step(BaseModel):
    model_config = ConfigDict(extra="forbid")
    why: str | None = Field(None, description="ignored; inspect_data's reason may be passed along")


class ParseNumbers(_Step):
    op: Literal["parse_numbers"]
    columns: list[str] = Field(min_length=1)


class ParseDates(_Step):
    op: Literal["parse_dates"]
    columns: list[str] = Field(min_length=1)
    order: Literal["auto", "dayfirst", "monthfirst"] = "auto"


class DropDuplicates(_Step):
    op: Literal["drop_duplicates"]
    columns: list[str] | None = Field(None, description="key columns; omit to compare whole rows")
    keep: Literal["first", "last"] = "first"


class FillMissing(_Step):
    op: Literal["fill_missing"]
    columns: list[str] = Field(min_length=1)
    method: Literal["mean", "median", "mode", "zero", "value", "forward"]
    value: float | str | None = Field(None, description="the fill value, for method='value'")


class DropRows(_Step):
    op: Literal["drop_rows"]
    column: str
    values: list[str | float] = Field(min_length=1, description="drop rows whose value in column is one of these "
                                                                "(e.g. a 'Total' summary row)")


class DropMissing(_Step):
    op: Literal["drop_missing"]
    columns: list[str] | None = Field(None, description="drop rows missing any of these; omit for any column")


class TidyText(_Step):
    op: Literal["tidy_text"]
    columns: list[str] = Field(min_length=1)


class FlagOutliers(_Step):
    op: Literal["flag_outliers"]
    columns: list[str] = Field(min_length=1)
    factor: float = Field(3.0, ge=1.5, le=10, description="fences at Q1 - factor x IQR and Q3 + factor x IQR")


class RemoveOutliers(_Step):
    op: Literal["remove_outliers"]
    columns: list[str] = Field(min_length=1)
    factor: float = Field(3.0, ge=1.5, le=10)


class RenameColumns(_Step):
    op: Literal["rename_columns"]
    mapping: dict[str, str] = Field(min_length=1, description="old name -> new name")


class DropColumns(_Step):
    op: Literal["drop_columns"]
    columns: list[str] = Field(min_length=1)


Step = Annotated[Union[ParseNumbers, ParseDates, DropDuplicates, DropRows, FillMissing, DropMissing, TidyText,
                       FlagOutliers, RemoveOutliers, RenameColumns, DropColumns], Field(discriminator="op")]


class CleanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data: str
    steps: list[Step] = Field(min_length=1, max_length=20)


@tool
def clean_data(ws: Workspace, data: str, steps: list[dict]) -> ToolResult:
    """Run the steps in order on a copy of the table; save it as a new table with a log."""
    args = CleanInput(data=data, steps=steps)
    df = ws.table(args.data)
    notes = {c: dict(n) for c, n in ws.column_notes(args.data).items()}
    rows_before = len(df)
    log = []
    for i, step in enumerate(args.steps, start=1):
        try:
            df, line = _APPLY[step.op](df, step, notes)
        except ToolError as exc:
            raise ToolError(f"Step {i} ({step.op}): {exc.message} Nothing was saved.", field=f"steps.{i - 1}") from exc
        log.append(f"{i}. {line}")
    name = ws.entry(args.data)["name"]
    new_id = ws.add_table(df.reset_index(drop=True), name=f"{name} (cleaned)", parent=args.data,
                          meta={"columns": {c: notes.get(c, {}) for c in df.columns},
                                "steps": [s.model_dump(exclude_none=True, exclude={"why"}) for s in args.steps]})
    result = {"data": new_id, "from": args.data, "rows_before": rows_before, "rows_after": len(df), "log": log}
    preview = df.head(20).astype(object).where(df.head(20).notna(), None)
    artifact = {"type": "table", "title": f"{ws.describe(new_id)}: cleaned", "columns": list(map(str, df.columns)),
                "rows": preview.values.tolist(), "notes": log}
    return ok(result, artifact)


# ---------------------------------------------------------------- the operations

def _cols(df: pd.DataFrame, names: list[str] | None) -> list[str]:
    return [resolve_column(df, n, "columns") for n in names] if names else list(df.columns)


def _numeric(df: pd.DataFrame, c: str, what: str) -> pd.Series:
    if not pd.api.types.is_numeric_dtype(df[c]) or pd.api.types.is_bool_dtype(df[c]):
        raise ToolError(f"{what} needs a number column; '{c}' is not (parse_numbers first if it holds numbers).")
    return df[c].astype(float)


def _parse_numbers(df, step: ParseNumbers, notes):
    df, parts = df.copy(), []
    for c in _cols(df, step.columns):
        if pd.api.types.is_numeric_dtype(df[c]):
            parts.append(f"{c} already numbers")
            continue
        values, bad, unit = parse_numbers(df[c].astype(object).where(df[c].notna(), ""))
        whole = values.dropna()
        if len(whole) and (whole == whole.round()).all() and whole.abs().max() < 2**53:
            values = values.astype("Int64")
        examples = df.loc[bad, c].drop_duplicates().head(3).tolist()
        df[c] = values
        notes[c] = {"type": "number", **({"unit": unit} if unit else {})}
        parts.append(f"{c}: {int(values.notna().sum()):,} numbers"
                     + (f", {int(bad.sum()):,} unreadable became missing (e.g. {', '.join(map(repr, examples))})"
                        if bad.any() else ""))
    return df, "parse_numbers - " + "; ".join(parts)


def _parse_dates(df, step: ParseDates, notes):
    df, parts = df.copy(), []
    for c in _cols(df, step.columns):
        if pd.api.types.is_datetime64_any_dtype(df[c]):
            parts.append(f"{c} already dates")
            continue
        order = None if step.order == "auto" else step.order
        values, bad, used = parse_dates(df[c].astype(object).where(df[c].notna(), ""), order)
        if used == "ambiguous":
            raise ToolError(f"The dates in '{c}' could be day-first or month-first; set order to 'dayfirst' "
                            "or 'monthfirst' (ask the user).")
        examples = df.loc[bad, c].drop_duplicates().head(3).tolist()
        df[c] = values
        notes[c] = {"type": "date", **({"date_order": used} if used else {})}
        span = f" ({values.min():%Y-%m-%d} to {values.max():%Y-%m-%d})" if values.notna().any() else ""
        parts.append(f"{c}: {int(values.notna().sum()):,} dates{span}"
                     + (f", {int(bad.sum()):,} unreadable became missing (e.g. {', '.join(map(repr, examples))})"
                        if bad.any() else ""))
    return df, "parse_dates - " + "; ".join(parts)


def _drop_duplicates(df, step: DropDuplicates, notes):
    subset = _cols(df, step.columns) if step.columns else None
    out = df.drop_duplicates(subset=subset, keep=step.keep)
    key = f"same {', '.join(subset)}" if subset else "identical rows"
    return out, f"drop_duplicates ({key}, kept the {step.keep}) - removed {len(df) - len(out):,} rows"


def _fill_missing(df, step: FillMissing, notes):
    df, parts = df.copy(), []
    for c in _cols(df, step.columns):
        gaps = int(df[c].isna().sum())
        if step.method in ("mean", "median", "zero"):
            x = _numeric(df, c, f"fill_missing with {step.method}")
            fill = {"mean": x.mean(), "median": x.median(), "zero": 0.0}[step.method]
            if pd.api.types.is_integer_dtype(df[c]) and fill != round(fill):
                df[c] = df[c].astype(float)
            df[c] = df[c].fillna(fill)
            parts.append(f"{c}: {gaps:,} filled with {step.method} {number(float(fill))}")
        elif step.method == "mode":
            mode = df[c].mode(dropna=True)
            if mode.empty:
                raise ToolError(f"'{c}' has no values to take the most common one from.")
            df[c] = df[c].fillna(mode.iloc[0])
            parts.append(f"{c}: {gaps:,} filled with the most common value {mode.iloc[0]!r}")
        elif step.method == "value":
            if step.value is None:
                raise ToolError("method 'value' needs value.")
            value = step.value
            if pd.api.types.is_numeric_dtype(df[c]):
                try:
                    value = float(value)
                except ValueError:
                    raise ToolError(f"'{c}' holds numbers; the fill value {step.value!r} is not one.") from None
                if pd.api.types.is_integer_dtype(df[c]) and value != round(value):
                    df[c] = df[c].astype(float)
            df[c] = df[c].fillna(value)
            parts.append(f"{c}: {gaps:,} filled with {value!r}")
        else:
            df[c] = df[c].ffill()
            left = int(df[c].isna().sum())
            parts.append(f"{c}: {gaps - left:,} filled from the row above (table order)"
                         + (f", {left:,} at the start stay missing" if left else ""))
    return df, "fill_missing - " + "; ".join(parts)


def _drop_rows(df, step: DropRows, notes):
    c = resolve_column(df, step.column, "column")
    if pd.api.types.is_numeric_dtype(df[c]) and not pd.api.types.is_bool_dtype(df[c]):
        try:
            hit = df[c].isin([float(v) for v in step.values])
        except ValueError:
            raise ToolError(f"'{c}' holds numbers; give numbers in values.") from None
    else:
        wanted = {str(v).strip().casefold() for v in step.values}
        hit = df[c].astype(str).str.strip().str.casefold().isin(wanted) & df[c].notna()
    return df[~hit], f"drop_rows ({c} in {list(step.values)}) - removed {int(hit.sum()):,} rows"


def _drop_missing(df, step: DropMissing, notes):
    subset = _cols(df, step.columns)
    out = df.dropna(subset=subset)
    where = ", ".join(subset) if step.columns else "any column"
    return out, f"drop_missing ({where}) - removed {len(df) - len(out):,} rows"


def _tidy_text(df, step: TidyText, notes):
    df, parts = df.copy(), []
    for c in _cols(df, step.columns):
        if pd.api.types.is_numeric_dtype(df[c]) or pd.api.types.is_datetime64_any_dtype(df[c]):
            raise ToolError(f"tidy_text works on text columns; '{c}' is not text.")
        s = df[c]
        present = s.notna()
        tidy = s[present].astype(str).str.strip().str.replace(r"\s+", " ", regex=True)
        key = tidy.str.casefold()
        # Each group of spellings becomes its most common form ('north', 'North ' -> 'North').
        canonical = tidy.groupby(key).agg(lambda v: v.value_counts().index[0])
        tidy = key.map(canonical)
        changed = int((tidy != s[present]).sum())
        merged = int(s[present].nunique() - tidy.nunique())
        df.loc[present, c] = tidy
        parts.append(f"{c}: {changed:,} values tidied, {merged:,} spelling variants merged")
    return df, "tidy_text - " + "; ".join(parts)


def _fences(x: pd.Series, factor: float) -> tuple[float, float]:
    q1, q3 = x.quantile(0.25), x.quantile(0.75)
    iqr = q3 - q1
    return q1 - factor * iqr, q3 + factor * iqr


def _flag_outliers(df, step: FlagOutliers, notes):
    df, parts = df.copy(), []
    for c in _cols(df, step.columns):
        x = _numeric(df, c, "flag_outliers")
        lo, hi = _fences(x, step.factor)
        flag = f"{c}_outlier"
        df[flag] = (x < lo) | (x > hi)
        notes[flag] = {"type": "bool"}
        parts.append(f"{c}: {int(df[flag].sum()):,} outside {number(lo)} to {number(hi)}, marked in {flag}")
    return df, "flag_outliers - " + "; ".join(parts)


def _remove_outliers(df, step: RemoveOutliers, notes):
    keep = pd.Series(True, index=df.index)
    parts = []
    for c in _cols(df, step.columns):
        x = _numeric(df, c, "remove_outliers")
        lo, hi = _fences(x, step.factor)
        out = (x < lo) | (x > hi)
        parts.append(f"{c}: {int(out.sum()):,} outside {number(lo)} to {number(hi)}")
        keep &= ~out
    return df[keep], f"remove_outliers - removed {int((~keep).sum()):,} rows ({'; '.join(parts)})"


def _rename_columns(df, step: RenameColumns, notes):
    mapping = {resolve_column(df, old, "mapping"): new.strip() for old, new in step.mapping.items()}
    clash = [n for n in mapping.values() if not n or (n in df.columns and n not in mapping)]
    if clash or len(set(mapping.values())) < len(mapping):
        raise ToolError(f"New names must be non-empty and unique; {clash or 'two columns share a name'}.")
    for old, new in mapping.items():
        if old in notes:
            notes[new] = notes.pop(old)
    return df.rename(columns=mapping), "rename_columns - " + ", ".join(f"{o} -> {n}" for o, n in mapping.items())


def _drop_columns(df, step: DropColumns, notes):
    cols = _cols(df, step.columns)
    return df.drop(columns=cols), f"drop_columns - {', '.join(cols)}"


_APPLY = {"parse_numbers": _parse_numbers, "parse_dates": _parse_dates, "drop_duplicates": _drop_duplicates, "drop_rows": _drop_rows,
          "fill_missing": _fill_missing, "drop_missing": _drop_missing, "tidy_text": _tidy_text,
          "flag_outliers": _flag_outliers, "remove_outliers": _remove_outliers,
          "rename_columns": _rename_columns, "drop_columns": _drop_columns}
