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

PREFIX = {"table": "data", "model": "model", "image": "image", "file": "file", "note": "note"}


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

    def add_bytes(self, kind: str, data: bytes, ext: str, name: str, parent: str | None = None,
                  meta: dict | None = None) -> str:
        """Store a file the tools made (an image, a chart, a script's output) under a new id."""
        new_id = self._new_id(kind)
        filename = f"{new_id}.{ext.lstrip('.')}"
        tmp = self.root / f".{filename}.part"
        tmp.write_bytes(data)
        os.replace(tmp, self.root / filename)
        self._add(new_id, kind, name, parent, file=filename, bytes=len(data), meta=meta or {})
        return new_id

    def replace_bytes(self, item_id: str, data: bytes, meta: dict | None = None) -> None:
        """Overwrite a stored file in place (a note saved again under the same title)."""
        e = self.entry(item_id)
        tmp = self.root / f".{e['file']}.part"
        tmp.write_bytes(data)
        os.replace(tmp, self.root / e["file"])
        e.update(bytes=len(data), updated=datetime.now().isoformat(timespec="seconds"))
        if meta is not None:
            e["meta"] = meta
        self._save()

    def path(self, item_id: str) -> Path:
        """Where a stored file is, for the server that sends it to the UI (never shown to the model)."""
        e = self.entry(item_id)
        if "file" not in e:
            raise ToolError(f"'{item_id}' is a {e['kind']}, not a stored file.", field="id")
        return self.root / e["file"]

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

    def set_meta(self, item_id: str, key: str, value) -> None:
        self.entry(item_id).setdefault("meta", {})[key] = value
        self._save()

    def issues_warning(self, item_id: str) -> str | None:
        """For results computed on a table that inspect_data found problems in (summary rows, numbers
        stored as text, duplicates ...): say so, so the numbers aren't trusted as they are."""
        issues = self.entry(item_id).get("meta", {}).get("issues") or []
        if not issues:
            return None
        return (f"{item_id} has problems inspect_data found ({'; '.join(issues[:3])}). Clean it with "
                "clean_data and the suggested steps, then use the cleaned table.")

    def listing(self, limit: int = 30) -> str:
        """What the workspace holds, for the model's prompt every turn: ids, names and sizes, newest
        last (content is never listed - tools read it)."""
        items = list(self._reg["items"].items())
        if not items:
            return "Workspace: empty (no files attached yet)."
        lines = []
        for item_id, e in items[-limit:]:
            meta = e.get("meta", {})
            if e["kind"] == "table":
                cols = e.get("columns", [])
                shown = ", ".join(cols[:8]) + (" ..." if len(cols) > 8 else "")
                info = f"{e['rows']:,} rows, {len(cols)} columns ({shown})"
            elif e["kind"] == "model":
                info = f"predicts {meta.get('target')} ({meta.get('metric')} {meta.get('score')})"
            elif e["kind"] == "note":
                info = "pinned note" if meta.get("pinned") else "note"
            elif e["kind"] == "image":
                info = f"image {meta.get('width')}x{meta.get('height')}"
            else:
                info = f"{meta['pages']} pages" if meta.get("pages") else f"{e.get('bytes', 0):,} bytes"
            origin = f", from {e['parent']}" if e.get("parent") else ""
            lines.append(f"{item_id}  {e['name']} - {info}{origin}")
        more = f"\n({len(items) - limit} older items not shown)" if len(items) > limit else ""
        return "Workspace:\n" + "\n".join(lines) + more

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
        self._save()

    def _save(self) -> None:
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
    if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}.*|\d{1,2}[/.]\d{1,2}[/.]\d{2,4}", str(name).strip()):
        # Small models put a date where a column name goes (often today's date from the context).
        dates = [str(c) for c in df.columns if pd.api.types.is_datetime64_any_dtype(df[c])]
        raise ToolError(f"{field} takes a column's name, not a date like '{name}'. "
                        + (f"Date columns: {', '.join(dates)}." if dates else
                           f"This table has no date column. Columns: {', '.join(map(str, df.columns))}."), field=field)
    raise ToolError(f"No column '{name}'. Columns: {', '.join(map(str, df.columns))}.", field=field)
