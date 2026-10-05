"""inspect_data: a first look at a table, and what could be cleaned.

It reports and suggests; it never changes the data. Every suggestion is a ready clean_data step
with the reason. Where the data itself can't decide (03/04/2025: 3 April or 4 March? rows that repeat
exactly, with no id to tell a copy from a real repeat), it returns a question whose options each carry
the step to run, or none - the helper's runner asks it, so the choice is the user's, not a guess. Codes
(ids, order numbers, leading zeros) are never suggested as numbers. It also names the columns' roles (date, order, item, amount ...,
app/tools/roles.py): the defaults the other data tools use, and how money columns combine.
"""

from __future__ import annotations

import pandas as pd

from .core import ToolResult, jsonable, number, ok, tool
from .periods import split_time_note
from .roles import CODE_WORDS, ID, ORDER, role_notes, table_roles, tokens
from .values import as_text, is_missing, parse_dates, parse_numbers
from .workspace import Workspace

OUTLIER_FACTOR = 3.0  # Tukey's "far out" fences: beyond Q1 - 3 IQR or Q3 + 3 IQR
ALMOST = 0.8  # a text column whose values are at least 80% numbers (or dates) is probably meant to be one
SAMPLE_ROWS = 3
# Labels of summary rows that exports append ('Total', 'Grand Total', 'Subtotal'): not data.
SUMMARY = r"^(?:grand\s+|sub)?totals?\b|^sum$"
STEP_ORDER = {"drop_duplicates": 0, "drop_rows": 1}


@tool
def inspect_data(ws: Workspace, data: str) -> ToolResult:
    """Columns, types, missing values, duplicates, outliers, and suggested cleaning steps.
    Computed once per table and kept on it: a stored table never changes (cleaning makes a new one),
    and on 50,000 rows the inspection takes seconds."""
    cached = ws.entry(data).get("meta", {}).get("inspection")
    if cached and "roles" in cached["result"]:  # (older inspections, before roles, are redone)
        return ok(cached["result"], cached["artifact"])
    df = ws.table(data)
    notes = ws.column_notes(data)
    columns, steps, questions = [], [], []
    for name in df.columns:
        profile, col_steps, col_questions = _column(name, df[name], notes.get(name, {}), len(df))
        columns.append(profile)
        steps += col_steps
        questions += col_questions
    steps.sort(key=lambda st: STEP_ORDER.get(st["op"], 2))  # remove rows first, then fix values
    duplicates = int(df.duplicated().sum())
    if duplicates and _has_row_id(df):  # the repeated rows repeat an id too: copies
        steps.insert(0, {"op": "drop_duplicates", "why": f"{_n(duplicates, 'row')} exact copies of an earlier row "
                                                         "(their id repeats too)"})
    elif duplicates:  # no id: two identical orders and a copied line look the same - the user knows
        questions.insert(0, _duplicates_question(duplicates))
    if any(c.get("missing") for c in columns):
        missing_note = ("Missing values are left as they are: fill or drop them (fill_missing / drop_missing) "
                        "only when an analysis needs it, with a method that fits the column.")
    else:
        missing_note = None
    sample = df.head(SAMPLE_ROWS).astype(object).where(df.head(SAMPLE_ROWS).notna(), None).to_dict("records")
    # Remembered on the table, so tools working on it can warn until a cleaned copy is used. (Repeats that
    # may be real are not a problem until the user says they are copies.)
    ws.set_meta(data, "issues", [s["why"] for s in steps] + [q["question"] for q in questions if not q.get("optional")])
    roles = table_roles(ws, data, df)
    result = {"data": data, "name": ws.entry(data)["name"], "rows": len(df), "columns": columns, "roles": roles,
              "duplicate_rows": duplicates, "sample_rows": sample, "suggested_steps": steps, "questions": questions}
    note = " ".join(filter(None, [missing_note, split_time_note(df, data), *role_notes(df, roles)]))
    if note:
        result["note"] = note
    role_of = {column: role for role, column in roles.items()}
    artifact = {"type": "table", "title": f"Columns of {ws.describe(data)}",
                "columns": ["column", "type", "role", "missing", "unique", "summary"],
                "rows": [[c["name"], c["type"], role_of.get(c["name"], ""), c.get("missing", 0), c["unique"], _summary(c)]
                         for c in columns]}
    ws.set_meta(data, "inspection", {"result": jsonable(result), "artifact": jsonable(artifact)})
    return ok(result, artifact)


def _column(name: str, s: pd.Series, note: dict, rows: int) -> tuple[dict, list[dict], list[dict]]:
    kind = _kind(s)
    present = s.dropna()
    p: dict = {"name": name, "type": kind}
    if len(present) < rows:
        p["missing"] = rows - len(present)
    p["unique"] = int(present.nunique())
    steps, questions = [], []
    if p["unique"] == 1:
        p["constant"] = True
    elif p["unique"] == len(present) and len(present) > 20 and (
            kind == "text" or kind == "number" and (present.astype(float) % 1 == 0).all()):
        p["unique_per_row"] = True  # an id or free text: not useful for grouping or as a model input

    if kind == "number":
        x = present.astype(float)
        p.update(min=number(x.min()), median=number(x.median()), max=number(x.max()))
        if note.get("unit"):
            p["unit"] = note["unit"]
        far = _far_outliers(x)
        if far:
            # Reported, not suggested: a flag column is noise for most questions (and a leak when the
            # column is later the thing to predict). clean_data can flag or remove them on request.
            p["far_outliers"] = far
    elif kind == "date":
        p.update(first=present.min(), last=present.max())
    elif kind == "text":
        top = present.value_counts().head(3)
        p["top"] = [[str(v), int(n)] for v, n in top.items()]
        labels = present.astype(str).str.strip()
        summary = labels.str.casefold().str.match(SUMMARY)
        if 0 < summary.sum() <= max(3, 0.01 * rows):
            found = sorted(labels[summary].unique())
            p["summary_rows"] = int(summary.sum())
            steps.append({"op": "drop_rows", "column": name, "values": found,
                          "why": f"{_n(int(summary.sum()), 'row')} labelled {', '.join(map(repr, found))} look like "
                                 "summary rows, not data; they would distort totals"})
            present = present[~summary.to_numpy()]
        pending = note.get("pending") or _almost(present, code=_code_column(name, present))
        if pending:
            p["almost"] = pending
            if pending.get("ambiguous_order"):
                questions.append(_date_order_question(name, pending["examples"]))
            else:
                op = "parse_numbers" if pending["as"] == "number" else "parse_dates"
                bad = pending["unreadable"]
                why = (f"{name} holds {pending['as']}s, but {_n(bad, 'value')} don't read as one "
                       f"(e.g. {', '.join(map(repr, pending['examples']))}); they would become missing" if bad
                       else f"{name} holds {pending['as']}s stored as text")
                steps.append({"op": op, "columns": [name], "why": why})
        variants = _variants(present)
        if variants:
            p["spelling_variants"] = variants
            steps.append({"op": "tidy_text", "columns": [name],
                          "why": f"some values of {name} differ only in case or spacing "
                                 f"(e.g. {' / '.join(map(repr, variants[0]))}); they would be merged into the most common spelling"})
    return p, steps, questions


def _kind(s: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(s):
        return "bool"
    if pd.api.types.is_numeric_dtype(s):
        return "number"
    if pd.api.types.is_datetime64_any_dtype(s):
        return "date"
    return "text"


def _far_outliers(x: pd.Series) -> int:
    q1, q3 = x.quantile(0.25), x.quantile(0.75)
    iqr = q3 - q1
    if iqr <= 0:
        return 0
    return int(((x < q1 - OUTLIER_FACTOR * iqr) | (x > q3 + OUTLIER_FACTOR * iqr)).sum())


def _almost(present: pd.Series, code: bool = False) -> dict | None:
    """A text column that is mostly numbers or dates (the rest typos, notes, codes). A code column
    (C489449 among 489434, 00012) is never one of numbers."""
    t = as_text(present)
    t = t[~is_missing(t)]
    if len(t) < 5:
        return None
    for kind, parse in (("number", parse_numbers), ("date", parse_dates)):
        if kind == "number" and code:
            continue
        bad = parse(t)[1]
        if 1 - bad.mean() >= ALMOST:
            return {"as": kind, "unreadable": int(bad.sum()), "examples": t[bad].drop_duplicates().head(3).tolist()}
    return None


def _code_column(name: str, present: pd.Series) -> bool:
    """Ids and codes: named so (invoice, stockcode, customer_id ...) or written with leading zeros."""
    t = as_text(present)
    return bool(set(tokens(name)) & CODE_WORDS) or t.str.fullmatch(r"0\d+").mean() > 0.2


def _has_row_id(df: pd.DataFrame) -> bool:
    """An id: a column that tells every distinct row apart, and is named as one (order_id, invoice no) or
    tells nearly every row apart (90%) - then rows that repeat exactly repeat their id too, so are copies.
    (A customer column over few distinct rows is no id.)"""
    distinct = df.drop_duplicates()
    return any(distinct[c].notna().all() and distinct[c].nunique() == len(distinct) > 1
               and (set(tokens(c)) & (ID | ORDER) or len(distinct) >= 0.9 * len(df)) for c in df.columns)


def _duplicates_question(count: int) -> dict:
    repeat, they = ("repeats", "Is it a copy") if count == 1 else ("repeat", "Are they copies")
    return {"question": f"{_n(count, 'row')} {repeat} an earlier row exactly, and no column tells them apart. {they} "
                        "to remove, or real repeats (e.g. the same order placed twice)?",
            "optional": True,
            "options": [{"label": "Keep them (real repeats)", "step": None},
                        {"label": "Remove the copies", "step": {"op": "drop_duplicates"}}]}


def _variants(present: pd.Series) -> list[list[str]]:
    """Groups of values that are the same apart from case and spacing, e.g. ['North', 'north ']."""
    values = pd.Series(present.astype(str).unique())
    key = values.str.strip().str.replace(r"\s+", " ", regex=True).str.casefold()
    groups = values.groupby(key).agg(list)
    return [g[:3] for g in groups if len(g) > 1][:3]


def _date_order_question(name: str, examples: list[str]) -> dict:
    ex = examples[0] if examples else "03/04/2025"
    return {"column": name,
            "question": f"The dates in {name} could be read two ways (e.g. {ex}). Which order do they use?",
            "options": [{"label": "Day first (03/04/2025 = 3 April)",
                         "step": {"op": "parse_dates", "columns": [name], "order": "dayfirst"}},
                        {"label": "Month first (03/04/2025 = March 4)",
                         "step": {"op": "parse_dates", "columns": [name], "order": "monthfirst"}}]}


def _n(count: int, noun: str) -> str:
    return f"{count:,} {noun}" + ("" if count == 1 else "s")


def _summary(c: dict) -> str:
    if c["type"] == "number":
        return f"{c['min']} to {c['max']} (median {c['median']})"
    if c["type"] == "date":
        return f"{c['first']:%Y-%m-%d} to {c['last']:%Y-%m-%d}"
    if c.get("top"):
        return ", ".join(f"{v} ({n})" for v, n in c["top"])
    return ""
