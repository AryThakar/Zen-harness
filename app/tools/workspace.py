"""One folder for everything the tools make, with an id for every artifact.

    data_1   a table (an uploaded file, a cleaned copy, a saved query result, predictions)
    image_1  an image (uploaded, generated, drawn by a script)
    file_1   any other file (code, documents, text)
    note_1   a note (the AI's memory; never listed as a file)

Tools return ids, and take an id or a name: Workspace.resolve is the one resolver every tool uses
(registry.execute applies it to every 'data', 'image' and 'file' input), so the model never has to
tell 'sales_cleaned_v2.csv' from 'sales_cleaned.csv', and a copied listing line still works.
registry.json records what each id is, where it came from (parent) and when it was made. Tables are
stored as Parquet (types survive).

One Workspace object serves a workspace to every thread - the running turn and the API's requests
(an upload, the item list) - so every change and every walk over the registry holds its lock: two
items made at once never share an id, and a save never meets a registry that is changing.
"""

from __future__ import annotations

import functools
import io
import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz, process

from app.tools.table_reader import ReadError, read_file

from .core import ToolError
from .values import type_columns

PREFIX = {"table": "data", "image": "image", "file": "file", "note": "note"}
IMAGE_FORMATS = {"PNG": ("png", "image/png"), "JPEG": ("jpg", "image/jpeg"), "WEBP": ("webp", "image/webp"),
                 "GIF": ("gif", "image/gif")}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
ITEM_REF = re.compile(r"\b((?:data|image|file|note)_\d+(?:_\w+)?)\b")
# The tool that does handle an item of the wrong kind, said in the error so the model's next call fits.
WRONG_KIND = {("table", "file"): " A table: a script reads it with table('{id}'), and analyze_data works with it.",
              ("image", "file"): " An image: it has no text - view_image looks at it and says what it shows.",
              ("image", "table"): " An image, not a table: view_image looks at it and says what it shows.",
              ("file", "table"): " A file, not a table: its text is read with files(read) or read_file."}


def _locked(method):
    """The method runs holding the workspace's lock (reentrant: a locked method may call another)."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class Workspace:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._registry_path = self.root / "registry.json"
        self._reg = json.loads(self._registry_path.read_text("utf-8")) if self._registry_path.exists() \
            else {"next": {}, "items": {}}

    # ------------------------------------------------------------------ adding

    def add_file(self, path: str | Path, sheet: str | None = None, meta: dict | None = None) -> str:
        """Read a CSV / Excel file (title lines, encodings and delimiters are handled by the
        reader), type its columns losslessly, and store it as a new table."""
        df, read = read_table(path, sheet)
        return self.add_table(df, name=Path(path).name, meta={**read, **(meta or {})})

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

    def add_image(self, data: bytes, name: str, parent: str | None = None, meta: dict | None = None) -> str:
        """An image item - uploaded, generated or drawn by a script, all the same kind - stored only when
        the bytes really are an image, with its format and size (vision and the UI need them; listings
        said 'image NonexNone' when they were missing). ToolError when it isn't an image."""
        ext, info = image_info(data, name)
        return self.add_bytes("image", data, ext, name, parent, meta={**(meta or {}), **info})

    @_locked
    def replace_bytes(self, item_id: str, data: bytes, meta: dict | None = None) -> None:
        """Overwrite a stored file in place (a note saved again, a file files() updated). No versions:
        the user's own files are only replaced after they approve it."""
        e = self.entry(item_id)
        tmp = self.root / f".{e['file']}.part"
        tmp.write_bytes(data)
        os.replace(tmp, self.root / e["file"])
        e.update(bytes=len(data), updated=datetime.now().isoformat(timespec="seconds"))
        if meta is not None:
            e["meta"] = meta
        self._save()

    @_locked
    def replace_image(self, item_id: str, data: bytes, meta: dict | None = None) -> None:
        """A new version of an image item, under the same id (a script's chart drawn again in the same
        task): checked like add_image; the stored file follows the new format."""
        e = self.entry(item_id)
        self._expect(item_id, "image")
        ext, info = image_info(data, e["name"])
        old, new = e["file"], f"{item_id}.{ext}"
        if new != old:
            e["file"] = new
        self.replace_bytes(item_id, data, {**(meta or {}), **info})
        if new != old:
            (self.root / old).unlink(missing_ok=True)

    @_locked
    def replace_table(self, item_id: str, df: pd.DataFrame, meta: dict | None = None) -> None:
        """A new version of a table under the same id (a script's output written again in the same task).
        Its meta starts over: an inspection of the old rows says nothing about the new ones."""
        e = self.entry(item_id)
        self._expect(item_id, "table")
        tmp = self.root / f".{item_id}.parquet.part"
        df.to_parquet(tmp, index=False)
        os.replace(tmp, self.root / f"{item_id}.parquet")
        e.update(rows=len(df), columns=list(map(str, df.columns)), meta=dict(meta or {}),
                 updated=datetime.now().isoformat(timespec="seconds"))
        self._save()

    @_locked
    def delete(self, item_id: str) -> dict:
        """Remove an item and its stored file - only ever after the user approved it (approval.py).
        Items made from it stay; their 'from' names an id that is gone."""
        e = self.entry(item_id)
        stored = e.get("file") or (f"{item_id}.parquet" if e["kind"] == "table" else None)
        del self._reg["items"][item_id]
        self._save()
        for name in (stored, e.get("report", {}).get("file")):
            if name:
                (self.root / name).unlink(missing_ok=True)
        return e

    @_locked
    def set_parent(self, item_id: str, parent: str) -> None:
        """What the item was made from (a script's output: the script)."""
        self.entry(item_id)["parent"] = parent
        self._save()

    @_locked
    def rename(self, item_id: str, name: str) -> None:
        self.entry(item_id)["name"] = name
        self._save()

    # ------------------------------------------------------------------ reports

    @_locked
    def set_report(self, item_id: str, text: str) -> None:
        """An item's latest report (report.md): what a worker did with it, for main and the next
        worker. One per item, replaced each time; kept beside the item, never an item itself (so it
        can't be handed back as work). Its first line is the item's line in lists."""
        e = self.entry(item_id)
        filename = f"{item_id}.report.md"
        tmp = self.root / f".{filename}.part"
        tmp.write_text(text, "utf-8")
        os.replace(tmp, self.root / filename)
        first = next((ln.strip("# ").strip() for ln in text.splitlines() if ln.strip()), "")
        e["report"] = {"file": filename, "line": first[:150], "at": datetime.now().isoformat(timespec="seconds")}
        self._save()

    def report(self, item_id: str) -> str | None:
        r = self.entry(item_id).get("report")
        path = self.root / r["file"] if r else None
        return path.read_text("utf-8") if path and path.exists() else None

    def path(self, item_id: str) -> Path:
        """Where a stored file is, for the server that sends it to the UI (never shown to the model)."""
        e = self.entry(item_id)
        if "file" not in e:
            raise ToolError(f"'{item_id}' is a {e['kind']}, not a stored file.", field="id")
        return self.root / e["file"]

    @_locked
    def entry(self, item_id: str) -> dict:
        if item_id not in self._reg["items"]:
            raise ToolError(f"There is no '{item_id}'. Available: {', '.join(self._reg['items']) or 'nothing yet'}.",
                            field="id")
        return self._reg["items"][item_id]

    def kind(self, item_id: str) -> str:
        return self.entry(item_id)["kind"]

    # ------------------------------------------------------------------ names (one resolver for every tool)

    @_locked
    def find(self, ref: str, kinds: tuple[str, ...] | None = None) -> str | None:
        """The item a model means, or None: its id; its name in any case (the newest of that name); or
        a line copied from a listing - 'data_1  sales.csv - 6 rows ...', 'sales.csv (data_1)' - by the
        one id in it (measured: the model passed 'data_1 shop_sales.csv' to files(read) after reading
        the list, and every read failed)."""
        text = " ".join(str(ref).split()).strip("'\"`")
        items = [i for i, e in self._reg["items"].items() if kinds is None or e["kind"] in kinds]
        if text in items:
            return text
        mentioned = [i for i in dict.fromkeys(ITEM_REF.findall(text)) if i in items]
        if len(mentioned) == 1:
            return mentioned[0]
        named = [i for i in items if self._reg["items"][i]["name"].casefold() == text.casefold()]
        return named[-1] if named else None

    @_locked
    def resolve(self, ref: str, kinds: tuple[str, ...] | None = None, field: str = "id") -> str:
        """find(), or a ToolError the model can act on: what kind it is if it is the wrong kind, else the
        closest items as 'id (name)' - never a whole listing line, which the model would copy back."""
        found = self.find(ref, kinds)
        if found:
            return found
        other = self.find(ref)
        wanted = " or ".join(kinds) if kinds and len(kinds) <= 2 else "item"
        if other:
            e = self._reg["items"][other]
            raise ToolError(f"'{ref}' is {other} ({e['name']}), {_a(e['kind'])}, not {_a(wanted)}."
                            + WRONG_KIND.get((e["kind"], kinds[0] if kinds else ""), "").format(id=other), field=field)
        pool = {i: e["name"] for i, e in self._reg["items"].items() if kinds is None or e["kind"] in kinds}
        if not pool:
            raise ToolError(f"No {wanted} '{ref}': this workspace has no {wanted}s yet.", field=field)
        close = [i for _, _, i in process.extract(str(ref), pool, scorer=fuzz.WRatio, limit=3, score_cutoff=60)]
        names = ", ".join(f"{i} ({pool[i]})" for i in (close or list(pool)[-8:]))
        raise ToolError(f"No {wanted} '{ref}'. " + (f"Did you mean: {names}?" if close else f"Here: {names}."),
                        field=field)

    @_locked
    def ids(self, kind: str | None = None) -> list[str]:
        return [i for i, e in self._reg["items"].items() if kind is None or e["kind"] == kind]

    def column_notes(self, item_id: str) -> dict[str, dict]:
        return self.entry(item_id).get("meta", {}).get("columns", {})

    @_locked
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

    @_locked
    def lines(self) -> dict[str, str]:
        """One line per item, oldest first: id, name and size (content is never listed - tools read
        it). Notes are not files: they reach the model as pinned notes and through search."""
        out = {}
        for item_id, e in self._reg["items"].items():
            meta = e.get("meta", {})
            if e["kind"] == "note":
                continue
            if e["kind"] == "table":
                cols = e.get("columns", [])
                shown = ", ".join(cols[:8]) + (" ..." if len(cols) > 8 else "")
                info = f"{e['rows']:,} rows, {len(cols)} columns ({shown})"
            elif e["kind"] == "image":
                info = f"image {meta['width']}x{meta['height']}" if meta.get("width") else "image"
            elif "passages" in meta:  # an uploaded document, indexed for search: say what it is about
                title = f' "{meta["title"]}"' if meta.get("title") and meta["title"] != e["name"] else ""
                size = f"{meta['pages']} pages" if meta.get("pages") else f"{meta.get('words') or 0:,} words"
                info = f"document{title}, {size}, searchable"
            else:
                info = f"{meta['pages']} pages" if meta.get("pages") else f"{e.get('bytes', 0):,} bytes"
            origin = f", from {e['parent']}" if e.get("parent") else ""
            about = f" | {e['report']['line']}" if e.get("report") else ""
            out[item_id] = f"{item_id}  {e['name']} - {info}{origin}{about}"
        return out

    def listing(self, limit: int = 30) -> str:
        """The newest ``limit`` items, one line each, newest last."""
        lines = list(self.lines().values())
        if not lines:
            return "Workspace: empty (no files attached yet)."
        more = f"\n({len(lines) - limit} older items not shown)" if len(lines) > limit else ""
        return "Workspace:\n" + "\n".join(lines[-limit:]) + more

    def describe(self, item_id: str) -> str:
        e = self.entry(item_id)
        size = f", {e['rows']:,} rows" if "rows" in e else ""
        origin = f", from {e['parent']}" if e.get("parent") else ""
        return f"{item_id} ({e['name']}{size}{origin})"

    # ------------------------------------------------------------------ internals

    def _expect(self, item_id: str, kind: str) -> None:
        if self.kind(item_id) != kind:
            raise ToolError(f"'{item_id}' is a {self.kind(item_id)}, not a {kind}.", field="id")

    @_locked
    def _new_id(self, kind: str) -> str:
        n = self._reg["next"].get(kind, 1)
        self._reg["next"][kind] = n + 1
        return f"{PREFIX[kind]}_{n}"

    @_locked
    def _add(self, item_id: str, kind: str, name: str, parent: str | None, **fields) -> None:
        self._reg["items"][item_id] = {"kind": kind, "name": name, "parent": parent,
                                       "created": datetime.now().isoformat(timespec="seconds"), **fields}
        self._save()

    @_locked
    def _save(self) -> None:
        tmp = self._registry_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._reg, indent=1, ensure_ascii=False), "utf-8")
        os.replace(tmp, self._registry_path)


def _a(noun: str) -> str:
    return ("an " if noun[:1] in "aeiou" else "a ") + noun


def read_table(path: str | Path, sheet: str | None = None) -> tuple[pd.DataFrame, dict]:
    """A CSV / Excel file as a typed table and what reading it found (column types, the header row,
    skipped lines) - the meta a table item keeps."""
    try:
        raw = read_file(path, sheet)
    except ReadError as exc:
        raise ToolError(str(exc), invalid=False) from exc
    df, info = type_columns(raw.df)
    return df.reset_index(drop=True), {
        "columns": info, "original_names": raw.original_names, "header_row": raw.header_row,
        **({"sheet": raw.sheet} if raw.sheet else {}),
        **({"skipped_lines": [n for n, _ in raw.bad_lines]} if raw.bad_lines else {})}


def image_info(data: bytes, name: str) -> tuple[str, dict]:
    """(file extension, {width, height, mime}) of image bytes; ToolError when they aren't a PNG, JPEG,
    WebP or GIF image."""
    from PIL import Image, UnidentifiedImageError
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt, (width, height) = img.format, img.size
            img.verify()
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ToolError(f"{name} is not a readable image.", invalid=False) from exc
    if fmt not in IMAGE_FORMATS:
        raise ToolError(f"{name} is a {fmt} image; images are kept as PNG, JPEG, WebP or GIF.", invalid=False)
    ext, mime = IMAGE_FORMATS[fmt]
    return ext, {"width": width, "height": height, "mime": mime}


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
