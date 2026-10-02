"""One folder for everything the tools make, with an id for every artifact.

    data_1   a table (an uploaded file, a cleaned copy, a saved query result, predictions)
    model_1  a trained model

Tools take and return ids, so the model never has to tell 'sales_cleaned_v2.csv' from
'sales_cleaned.csv'. registry.json records what each id is, where it came from (parent) and
when it was made. Tables are stored as Parquet (types survive), models with joblib.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

import joblib
import pandas as pd

from app.ingestion.reader import ReadError, read_file

from .core import ToolError
from .values import type_columns

PREFIX = {"table": "data", "model": "model"}


class Workspace:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._registry_path = self.root / "registry.json"
        self._reg = json.loads(self._registry_path.read_text("utf-8")) if self._registry_path.exists() \
            else {"next": {}, "items": {}}

    # ------------------------------------------------------------------ adding

    def add_file(self, path: str | Path, sheet: str | None = None) -> str:
        """Read a CSV / Excel file (title lines, encodings and delimiters are handled by the
        reader), type its columns losslessly, and store it as a new table."""
        try:
            raw = read_file(path, sheet)
        except ReadError as exc:
            raise ToolError(str(exc), invalid=False) from exc
        df, info = type_columns(raw.df)
        df = df.reset_index(drop=True)
        return self.add_table(df, name=Path(path).name, meta={
            "columns": info, "original_names": raw.original_names, "header_row": raw.header_row,
            **({"sheet": raw.sheet} if raw.sheet else {}),
            **({"skipped_lines": [n for n, _ in raw.bad_lines]} if raw.bad_lines else {})})

    def add_table(self, df: pd.DataFrame, name: str, parent: str | None = None, meta: dict | None = None) -> str:
        meta = dict(meta or {})
        if "columns" not in meta and parent and self.kind(parent) == "table":
            # Column notes (units, date order) carry over to a derived table where the column still exists.
            parent_cols = self.entry(parent).get("meta", {}).get("columns", {})
            meta["columns"] = {c: parent_cols[c] for c in df.columns if c in parent_cols}
        new_id = self._new_id("table")
        df.to_parquet(self.root / f"{new_id}.parquet", index=False)
        self._add(new_id, "table", name, parent, rows=len(df), columns=list(map(str, df.columns)), meta=meta)
        return new_id

    def add_model(self, model, name: str, parent: str | None = None, meta: dict | None = None) -> str:
        new_id = self._new_id("model")
        joblib.dump(model, self.root / f"{new_id}.joblib")
        self._add(new_id, "model", name, parent, meta=meta or {})
        return new_id

    # ------------------------------------------------------------------ reading

    def table(self, item_id: str) -> pd.DataFrame:
        self._expect(item_id, "table")
        return pd.read_parquet(self.root / f"{item_id}.parquet")

    def model(self, item_id: str):
        self._expect(item_id, "model")
        return joblib.load(self.root / f"{item_id}.joblib")

    def entry(self, item_id: str) -> dict:
        if item_id not in self._reg["items"]:
            raise ToolError(f"There is no '{item_id}'. Available: {', '.join(self._reg['items']) or 'nothing yet'}.",
                            field="id")
        return self._reg["items"][item_id]

    def kind(self, item_id: str) -> str:
        return self.entry(item_id)["kind"]

    def ids(self, kind: str | None = None) -> list[str]:
        return [i for i, e in self._reg["items"].items() if kind is None or e["kind"] == kind]

    def column_notes(self, item_id: str) -> dict[str, dict]:
        return self.entry(item_id).get("meta", {}).get("columns", {})

    def describe(self, item_id: str) -> str:
        e = self.entry(item_id)
        size = f", {e['rows']:,} rows" if "rows" in e else ""
        origin = f", from {e['parent']}" if e.get("parent") else ""
        return f"{item_id} ({e['name']}{size}{origin})"

    # ------------------------------------------------------------------ internals

    def _expect(self, item_id: str, kind: str) -> None:
        if self.kind(item_id) != kind:
            raise ToolError(f"'{item_id}' is a {self.kind(item_id)}, not a {kind}.", field="id")

    def _new_id(self, kind: str) -> str:
        n = self._reg["next"].get(kind, 1)
        self._reg["next"][kind] = n + 1
        return f"{PREFIX[kind]}_{n}"

    def _add(self, item_id: str, kind: str, name: str, parent: str | None, **fields) -> None:
        self._reg["items"][item_id] = {"kind": kind, "name": name, "parent": parent,
                                       "created": datetime.now().isoformat(timespec="seconds"), **fields}
        tmp = self._registry_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._reg, indent=1, ensure_ascii=False), "utf-8")
        os.replace(tmp, self._registry_path)


def resolve_column(df: pd.DataFrame, name: str, field: str = "column") -> str:
    """The column called ``name``; a near miss in case or spacing is accepted ('Unit Price' for
    'unit_price'), anything else is an error listing the columns."""
    if name in df.columns:
        return name
    wanted = re.sub(r"[^0-9a-z]+", "_", str(name).lower()).strip("_")
    for c in df.columns:
        if re.sub(r"[^0-9a-z]+", "_", str(c).lower()).strip("_") == wanted:
            return c
    raise ToolError(f"No column '{name}'. Columns: {', '.join(map(str, df.columns))}.", field=field)
