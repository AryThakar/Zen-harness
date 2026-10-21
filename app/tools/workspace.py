# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""The workspace: one folder of real files, where a file's path is its one name - for the user (the file
panel, an @ in the message box), main, every helper, a script (its working folder) and the search index.

    files/                 every file at its path: 'sales.csv', 'panda.jpg', 'charts/revenue.png'
    .zen/registry.json     what each file is: kind, path, who brought it, what it was made from, size, hash
    .zen/cache/            a table's typed copy (Parquet), rebuilt when its file changes
    .zen/versions/<key>/   earlier versions of a file, the last VERSIONS_KEPT
    .zen/reports/<key>.md  what a helper did with a file
    files/agent.md         the project's standing memory, always in view (agent_md.py)

One write path: put() (and adopt(), for a file a script wrote in place) is the only way a file arrives
or changes. It keeps paths unique (an existing name gets ' (2)'), writes atomically, keeps the version
before a change, recognises identical content where asked (an upload already here is that file), types a
table and caches it, checks an image is one, and records lineage. Everything else reads.

Paths, not ids: every model-facing text names a file by its path. Each file also has an internal key
(data_3, image_7, file_2 - kept from the first design, so old chats and the search index still
find it); keys stay inside the app and the UI's links. resolve() takes a path (any case), a bare file name
when one file has it, a copied listing line ('sales.csv: table, ...'), or a key.

Tables are their files: an uploaded CSV or Excel file stays as it came; a table a tool makes is written as
a CSV beside its source ('titanic-cleaned.csv'). The data tools read the typed cache; a script reads the
file itself (pd.read_csv('sales.csv')) or the typed copy (table('sales.csv')).

One Workspace object serves a workspace to every thread (the running turn, the API's requests), so every
change holds its lock. A workspace in the first layout (items stored by key beside registry.json) is moved
into this one when it is opened, once (_migrate).
"""

from __future__ import annotations

import functools
import hashlib
import io
import json
import os
import re
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

import pandas as pd
from rapidfuzz import fuzz, process

from app.tools.table_reader import ReadError, read_file

from .core import ToolError
from .filetypes import IMAGE as IMAGE_SUFFIXES  # noqa: F401 - the image kinds, re-exported for the sandbox
from .filetypes import IMAGE_FORMATS, SCRIPT_TABLES, TABLE
from .values import type_columns

FILES, ZEN = "files", ".zen"
PREFIX = {"table": "data", "image": "image", "file": "file"}
AGENT_FILE = "agent.md"  # the project's standing memory (agent_md.py)
TABLE_FILES = (*TABLE, *SCRIPT_TABLES)  # files that are tables
VERSIONS_KEPT = 10
MAX_PATH = 150
ITEM_REF = re.compile(r"\b((?:data|image|file|note)_\d+(?:_\w+)?)\b")  # an internal key (old chats, the UI)
BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')  # not allowed in a Windows file name
RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
PATH_RULE = ("a file path inside the workspace, like 'report.md' or 'charts/sales.png' - no '..', no part "
             "starting with '.', none of < > : \" | ? *")
# The tool that does handle a file of the wrong kind, said in the error so the model's next call fits.
WRONG_KIND = {("table", "file"): " A script reads it with pd.read_csv('{path}') or table('{path}'), and analyze_or_code "
                                 "works with it.",
              ("image", "file"): " It has no text: view_image looks at it and says what it shows.",
              ("image", "table"): " view_image looks at it and says what it shows.",
              ("file", "table"): " Its text is read with files(mode='read').",
              ("file", "image"): " Its text is read with files(mode='read').",
              ("table", "image"): " analyze_or_code charts it."}


def _locked(method):
    """The method runs holding the workspace's lock (reentrant: a locked method may call another)."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


@dataclass
class Put:
    """What put() did: the file's key and path, and whether identical content was already here (nothing
    written)."""
    key: str
    path: str
    existed: bool = False


class Workspace:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.files = self.root / FILES
        self.zen = self.root / ZEN
        self._lock = threading.RLock()
        self._registry_path = self.zen / "registry.json"
        if (self.root / "registry.json").exists() and not self._registry_path.exists():
            self._migrate()
        for d in (self.files, self.zen / "cache", self.zen / "versions", self.zen / "reports"):
            d.mkdir(parents=True, exist_ok=True)
        self._reg = json.loads(self._registry_path.read_text("utf-8")) if self._registry_path.exists() \
            else {"next": {}, "items": {}}
        notes = [k for k, e in self._reg["items"].items() if e["kind"] == "note"]
        if notes:  # notes are gone (agent.md replaces them); the ones of the first design are dropped
            for k in notes:
                del self._reg["items"][k]
            self._save()

    # ------------------------------------------------------------------ the one write path

    @_locked
    def put(self, name: str, data: bytes | pd.DataFrame, *, kind: str | None = None, by: str = "assistant",
            parent: str | None = None, meta: dict | None = None, key: str | None = None, strict: bool = False,
            dedupe: bool = False, sheet: str | None = None) -> Put:
        """Write a file - new (``name``), or a new version of ``key`` - and register it. ``strict``: the name
        came from the model, so a bad one is refused rather than tidied. ``dedupe``: identical content
        already here is that file (an upload or a download). ToolError when the content isn't what its kind
        needs (an image that isn't one, a table that can't be read)."""
        if key is not None:
            return self._replace(key, data, meta)
        kind = kind or kind_of(str(name))
        path = clean_path(name, strict=strict)
        frame = data if isinstance(data, pd.DataFrame) else None
        if frame is not None:
            kind = "table"
            if PurePosixPath(path).suffix.lower() not in (".csv", ".parquet"):
                path += ".csv"
        info: dict = {}
        if kind == "image":
            ext, info = image_info(data, path)
            path = with_suffix(path, ext)
        blob = _table_bytes(frame, path) if frame is not None else bytes(data)
        sha = hashlib.sha256(blob).hexdigest()
        if dedupe:
            same = next((k for k, e in self._reg["items"].items() if e.get("sha") == sha and e["kind"] == kind), None)
            if same:
                return Put(same, self._reg["items"][same]["name"], existed=True)
        path = self._free(path)
        new_key = self._new_id(kind)
        target = self.files / path
        _write_atomic(target, blob)
        entry = {"kind": kind, "name": path, "file": f"{FILES}/{path}", "by": by, "parent": parent,
                 "created": _now(), "sha": sha, "bytes": len(blob), "meta": {**(meta or {}), **info}}
        if kind == "table":
            try:
                self._cache_table(new_key, entry, frame, sheet)
            except ToolError:
                target.unlink(missing_ok=True)
                raise
        self._reg["items"][new_key] = entry
        self._save()
        return Put(new_key, path)

    @_locked
    def adopt(self, path: str, *, by: str = "assistant", parent: str | None = None, meta: dict | None = None) -> Put:
        """Register a file already in the folder (a script wrote it there): a new file, or - when the path is
        a file already - its new content (its earlier version was kept as the script wrote it)."""
        path = clean_path(path, strict=False)
        known = self.find_path(path)
        if known:
            self.refresh(known)
            return Put(known, path)
        target = self.files / path
        blob = target.read_bytes()
        kind = kind_of(path)
        entry = {"kind": kind, "name": path, "file": f"{FILES}/{path}", "by": by, "parent": parent,
                 "created": _now(), "sha": hashlib.sha256(blob).hexdigest(), "bytes": len(blob), "meta": dict(meta or {})}
        if kind == "image":
            try:
                entry["meta"].update(image_info(blob, path)[1])
            except ToolError:
                kind = "file"  # not a readable image: kept as a plain file (the caller says so)
        new_key = self._new_id(kind) if kind != "table" else None
        if kind == "table":
            new_key = self._new_id("table")
            try:
                self._cache_table(new_key, entry, None, None)
            except ToolError:  # not a readable table: kept as a plain file under a file's key
                kind, new_key = "file", self._new_id("file")
        entry["kind"] = kind
        self._reg["items"][new_key] = entry
        self._save()
        return Put(new_key, path)

    @_locked
    def refresh(self, key: str) -> None:
        """A file changed on disk (a script wrote it again): its size, hash and - for a table - its typed copy,
        whose notes start over (an inspection of the old rows says nothing about the new ones)."""
        e = self.entry(key)
        blob = (self.root / e["file"]).read_bytes()
        e.update(sha=hashlib.sha256(blob).hexdigest(), bytes=len(blob), updated=_now())
        if e["kind"] == "table":
            e["meta"] = {k: v for k, v in e.get("meta", {}).items() if k in ("by", "from")}
            self._cache_table(key, e, None, None)
        elif e["kind"] == "image":
            try:
                e["meta"] = {**e.get("meta", {}), **image_info(blob, e["name"])[1]}
            except ToolError:
                pass
        self._save()

    def _replace(self, key: str, data: bytes | pd.DataFrame, meta: dict | None) -> Put:
        e = self.entry(key)
        frame = data if isinstance(data, pd.DataFrame) else None
        path, info = e["name"], {}
        if e["kind"] == "image":
            ext, info = image_info(data, path)
            path = with_suffix(path, ext)
            if path.casefold() != e["name"].casefold():
                path = self._free(path, own=key)
        blob = _table_bytes(frame, path) if frame is not None else bytes(data)
        old = self.root / e["file"]
        self._keep_version(key, old)
        target = self.files / path
        _write_atomic(target, blob)
        if target != old:
            old.unlink(missing_ok=True)
        e.update(name=path, file=f"{FILES}/{path}", sha=hashlib.sha256(blob).hexdigest(), bytes=len(blob),
                 updated=_now())
        if meta is not None or info:
            e["meta"] = {**(meta if meta is not None else e.get("meta", {})), **info}
        if e["kind"] == "table":
            self._cache_table(key, e, frame, e.get("meta", {}).get("sheet"))
        self._save()
        return Put(key, path)

    def _cache_table(self, key: str, entry: dict, frame: pd.DataFrame | None, sheet: str | None) -> None:
        """The table's typed copy, its size and the file's stamp (a later change to the file shows by it)."""
        path = self.root / entry["file"]
        if frame is None:
            frame, read = _read_any_table(path, sheet)
            columns = {**read.get("columns", {})}
            entry["meta"] = {**entry.get("meta", {}), **read, "columns": columns}
        cache = self.zen / "cache" / f"{key}.parquet"
        tmp = cache.with_name(cache.name + ".part")
        frame.to_parquet(tmp, index=False)
        os.replace(tmp, cache)
        entry.update(rows=len(frame), columns=list(map(str, frame.columns)), stamp=_stamp(path))

    def _keep_version(self, key: str, current: Path) -> None:
        """The file as it is now, kept before it changes; only the newest VERSIONS_KEPT stay."""
        if not current.exists():
            return
        folder = self.zen / "versions" / key
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copy2(current, folder / f"{datetime.now():%Y%m%d-%H%M%S-%f}{current.suffix}")
        prune_versions(folder)

    # ------------------------------------------------------------------ the first API, on the one write path

    def add_file(self, path: str | Path, sheet: str | None = None, meta: dict | None = None, *, by: str = "user",
                 dedupe: bool = True) -> str:
        """A CSV / Excel file brought in (an upload, a download): kept as it came, typed into its cache."""
        path = Path(path)
        return self.put(path.name, path.read_bytes(), kind="table", by=by, meta=meta, dedupe=dedupe, sheet=sheet).key

    def add_table(self, df: pd.DataFrame, name: str, parent: str | None = None, meta: dict | None = None) -> str:
        """A table a tool made, written as a CSV file (column notes carry over from its source)."""
        meta = dict(meta or {})
        if "columns" not in meta and parent and parent in self._reg["items"] and self.kind(parent) == "table":
            parent_cols = self.entry(parent).get("meta", {}).get("columns", {})
            meta["columns"] = {c: parent_cols[c] for c in df.columns if c in parent_cols}
        return self.put(name, df, kind="table", parent=parent, meta=meta).key

    def add_bytes(self, kind: str, data: bytes, ext: str, name: str, parent: str | None = None,
                  meta: dict | None = None, *, by: str = "assistant") -> str:
        """A file the tools made (a script's output, a written file)."""
        name = with_suffix(name, ext) if not PurePosixPath(str(name)).suffix else name
        return self.put(name, data, kind=kind, parent=parent, meta=meta, by=by).key

    def add_image(self, data: bytes, name: str, parent: str | None = None, meta: dict | None = None, *,
                  by: str = "assistant", dedupe: bool = False) -> str:
        """An image - uploaded, generated or drawn - stored only when the bytes really are one (ToolError
        otherwise), with its format and size."""
        return self.put(name, data, kind="image", parent=parent, meta=meta, by=by, dedupe=dedupe).key

    def replace_bytes(self, key: str, data: bytes, meta: dict | None = None) -> None:
        self.put("", data, key=key, meta=meta)

    def replace_image(self, key: str, data: bytes, meta: dict | None = None) -> None:
        """A new picture under the same file: what was said about the old one (its prompt) goes with it."""
        self._expect(key, "image")
        self.put("", data, key=key, meta=dict(meta or {}))

    def replace_table(self, key: str, df: pd.DataFrame, meta: dict | None = None) -> None:
        self._expect(key, "table")
        self.put("", df, key=key, meta=dict(meta or {}))

    # ------------------------------------------------------------------ reading

    @_locked
    def table(self, key: str) -> pd.DataFrame:
        """The typed table: its cache while its file is unchanged, else the file read again."""
        self._expect(key, "table")
        e = self.entry(key)
        cache = self.zen / "cache" / f"{key}.parquet"
        if not cache.exists() or e.get("stamp") != _stamp(self.root / e["file"]):
            self._cache_table(key, e, None, e.get("meta", {}).get("sheet"))
            self._save()
        return pd.read_parquet(cache)

    def path(self, key: str) -> Path:
        """Where a file is on disk, for the server and the code that reads it (never shown to the model)."""
        return self.root / self.entry(key)["file"]

    content_path = path

    def entry(self, key: str) -> dict:
        if key not in self._reg["items"]:
            raise ToolError(f"There is no '{key}'.", field="id")
        return self._reg["items"][key]

    def kind(self, key: str) -> str:
        return self.entry(key)["kind"]

    def label(self, key: str) -> str:
        """How the model and the user name it: its path."""
        return self.entry(key)["name"]

    def by_user(self, key: str) -> bool:
        """Brought by the user (an upload, a download): changing it asks them first."""
        return self.entry(key).get("by") == "user"

    def protected(self) -> list[Path]:
        """The files the user brought, and agent.md: a script may read them, never change them."""
        return [self.root / e["file"] for e in self._reg["items"].values()
                if e.get("by") == "user" or e["name"].casefold() == AGENT_FILE]

    # ------------------------------------------------------------------ changing

    @_locked
    def delete(self, key: str) -> dict:
        """Remove a file, its typed copy, versions and report - only ever after the user approved it."""
        e = self.entry(key)
        del self._reg["items"][key]
        self._save()
        (self.root / e["file"]).unlink(missing_ok=True)
        (self.zen / "cache" / f"{key}.parquet").unlink(missing_ok=True)
        shutil.rmtree(self.zen / "versions" / key, ignore_errors=True)
        if e.get("report"):
            (self.root / e["report"]["file"]).unlink(missing_ok=True)
        return e

    @_locked
    def rename(self, key: str, name: str) -> str:
        """A file moved to another path; returns the new path. ToolError when the path is another file's."""
        e = self.entry(key)
        path = clean_path(name, strict=True)
        taken = self.find_path(path)
        if taken and taken != key:
            raise ToolError(f"{path} is already a file here: pick another name.", field="new_name")
        target = self.files / path
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(self.root / e["file"], target)
        e.update(name=path, file=f"{FILES}/{path}")
        if e["kind"] == "table":
            e["stamp"] = _stamp(target)
        self._save()
        return path

    @_locked
    def set_parent(self, key: str, parent: str) -> None:
        """What the file was made from (a script's output: the script)."""
        self.entry(key)["parent"] = parent
        self._save()

    @_locked
    def set_meta(self, key: str, name: str, value) -> None:
        self.entry(key).setdefault("meta", {})[name] = value
        self._save()

    # ------------------------------------------------------------------ versions

    def versions(self, key: str) -> list[dict]:
        """Earlier versions, newest first: {version, saved, bytes}."""
        self.entry(key)
        folder = self.zen / "versions" / key
        found = sorted(folder.iterdir(), reverse=True) if folder.exists() else []
        return [{"version": p.name, "saved": _saved(p.name), "bytes": p.stat().st_size} for p in found if p.is_file()]

    @_locked
    def restore(self, key: str, version: str) -> Put:
        """An earlier version made the current one (the current one is kept as a version)."""
        source = self.zen / "versions" / key / Path(version).name
        if not source.is_file():
            raise ToolError(f"No version {version} of {self.label(key)}.", field="version")
        return self.put("", source.read_bytes(), key=key)

    # ------------------------------------------------------------------ reports

    @_locked
    def set_report(self, key: str, text: str) -> None:
        """A file's latest report: what a worker did with it, for main and the next worker. One per file,
        replaced each time; kept beside it, never a file itself. Its first line is the file's line in lists."""
        e = self.entry(key)
        rel = f"{ZEN}/reports/{key}.md"
        _write_atomic(self.root / rel, text.encode("utf-8"))
        first = next((ln.strip("# ").strip() for ln in text.splitlines() if ln.strip()), "")
        e["report"] = {"file": rel, "line": first[:150], "at": _now()}
        self._save()

    def report(self, key: str) -> str | None:
        r = self.entry(key).get("report")
        path = self.root / r["file"] if r else None
        return path.read_text("utf-8") if path and path.exists() else None

    # ------------------------------------------------------------------ names (one resolver for every tool)

    @_locked
    def find_path(self, path: str, kinds: tuple[str, ...] | None = None) -> str | None:
        """The file at this path, any case."""
        want = str(path).replace("\\", "/").strip().strip("/").casefold()
        return next((k for k, e in self._reg["items"].items() if e["name"].casefold() == want
                     and (kinds is None or e["kind"] in kinds)), None)

    @_locked
    def find(self, ref: str, kinds: tuple[str, ...] | None = None) -> str | None:
        """The file a model or the user means, or None: its path in any case; its bare file name when one file
        has it; a line copied from a listing ('sales.csv: table, ...'); a key (old chats, the UI)."""
        text = " ".join(str(ref).split()).strip("'\"`").replace("\\", "/").strip()
        items = {k: e for k, e in self._reg["items"].items() if kinds is None or e["kind"] in kinds}
        if text in items:
            return text
        for candidate in (text, text.split(": ", 1)[0]):
            want = candidate.strip().strip("/").casefold()
            named = [k for k, e in items.items() if e["name"].casefold() == want]
            if named:
                return named[-1]
            if "/" not in want:
                base = [k for k, e in items.items() if e["name"].rsplit("/", 1)[-1].casefold() == want]
                if len(base) == 1:
                    return base[0]
        mentioned = [k for k in dict.fromkeys(ITEM_REF.findall(text)) if k in items]
        return mentioned[0] if len(mentioned) == 1 else None

    @_locked
    def resolve(self, ref: str, kinds: tuple[str, ...] | None = None, field: str = "id") -> str:
        """find(), or a ToolError the model can act on: what kind it is if it is the wrong kind, else the
        closest paths."""
        found = self.find(ref, kinds)
        if found:
            return found
        other = self.find(ref)
        wanted = " or ".join(kinds) if kinds and len(kinds) <= 2 else "file"
        if other:
            e = self._reg["items"][other]
            if e["name"].endswith(".chart.json") and kinds and "table" in kinds:
                # measured 2026-10-22: a forecast's chart given to make_chart as data, then scripts to combine charts
                raise ToolError(f"{e['name']} is a chart already made - the user sees it. To show its numbers another "
                                "way, chart the table they came from; charts are not data.", field=field)
            raise ToolError(f"{e['name']} is {_a(e['kind'])}, not {_a(wanted)}."
                            + WRONG_KIND.get((e["kind"], kinds[0] if kinds else ""), "").format(path=e["name"]),
                            field=field)
        pool = {k: e["name"] for k, e in self._reg["items"].items() if kinds is None or e["kind"] in kinds}
        if not pool:
            raise ToolError(f"No {wanted} '{ref}': this workspace has no {wanted}s yet.", field=field)
        close = [pool[k] for _, _, k in process.extract(str(ref), pool, scorer=fuzz.WRatio, limit=3, score_cutoff=60)]
        names = ", ".join(close or list(pool.values())[-8:])
        raise ToolError(f"No {wanted} '{ref}'. " + (f"Did you mean: {names}?" if close else f"Here: {names}."),
                        field=field)

    @_locked
    def ids(self, kind: str | None = None) -> list[str]:
        return [k for k, e in self._reg["items"].items() if kind is None or e["kind"] == kind]

    def column_notes(self, key: str) -> dict[str, dict]:
        return self.entry(key).get("meta", {}).get("columns", {})

    def skipped_note(self, key: str) -> str | None:
        """Rows set aside when the table was read - wider than its header (measured: a close written 1,25,010 split
        into three values) - said, not lost in silence."""
        lines = self.entry(key).get("meta", {}).get("skipped_lines") or []
        if not lines:
            return None
        shown = ", ".join(map(str, lines[:8])) + (" ..." if len(lines) > 8 else "")
        return (f"{len(lines)} row(s) had more values than the header and were left out (file lines {shown}): a value "
                "with commas in it (1,25,010) must be in quotes, or written without the commas.")

    def issues_warning(self, key: str) -> str | None:
        """For results computed on a table that inspect_data found problems in (summary rows, numbers
        stored as text, duplicates ...): say so, so the numbers aren't trusted as they are."""
        issues = self.entry(key).get("meta", {}).get("issues") or []
        if not issues:
            return None
        return (f"{self.label(key)} has problems inspect_data found ({'; '.join(issues[:3])}). Clean it with "
                "clean_data and the suggested steps, then use the cleaned table.")

    @_locked
    def lines(self) -> dict[str, str]:
        """One line per file, oldest first, by key: 'path: what it is' (content is never listed - tools read
        it)."""
        out = {}
        for key, e in self._reg["items"].items():
            meta = e.get("meta", {})
            if e["kind"] == "table":
                cols = e.get("columns", [])
                shown = ", ".join(cols[:8]) + (" ..." if len(cols) > 8 else "")
                info = f"table, {e.get('rows', 0):,} rows, {len(cols)} columns ({shown})"
            elif e["kind"] == "image":
                info = f"image {meta['width']}x{meta['height']}" if meta.get("width") else "image"
            elif meta.get("chart"):  # a chart file: the page draws it, a document takes its picture
                info = f"chart ({meta['chart']}" + (f", {meta['points']} points" if meta.get("points") else "") + ")"
            elif "passages" in meta:  # a document, indexed for search: say what it is about
                title = f' "{meta["title"]}"' if meta.get("title") and meta["title"] != e["name"] else ""
                size = f"{meta['pages']} pages" if meta.get("pages") else f"{meta.get('words') or 0:,} words"
                info = f"document{title}, {size}, searchable"
            else:
                info = f"{meta['pages']} pages" if meta.get("pages") else f"{e.get('bytes', 0):,} bytes"
            parent = e.get("parent")
            origin = f", from {self._reg['items'][parent]['name']}" if parent in self._reg["items"] else ""
            about = f" | {e['report']['line']}" if e.get("report") else ""
            out[key] = f"{e['name']}: {info}{origin}{about}"
        return out

    def listing(self, limit: int = 30) -> str:
        """The newest ``limit`` files, one line each, newest last."""
        lines = list(self.lines().values())
        if not lines:
            return "Workspace: empty (no files yet)."
        more = f"\n({len(lines) - limit} older files not shown)" if len(lines) > limit else ""
        return "Workspace:\n" + "\n".join(lines[-limit:]) + more

    def derived_name(self, parent: str, step: str) -> str:
        """The path of a table made from ``parent`` by ``step``, beside it: 'sales.csv' + 'cleaned' ->
        'sales-cleaned.csv'. The same step again adds nothing (put() then makes the path unique)."""
        base = PurePosixPath(self.entry(parent)["name"])
        stem = base.stem if base.suffix.lower() in TABLE_FILES else base.name
        slug = re.sub(r"[^\w]+", "-", step.lower()).strip("-")
        stem = stem if stem.endswith(f"-{slug}") else f"{stem}-{slug}"
        folder = "" if str(base.parent) in ("", ".") else f"{base.parent}/"
        return f"{folder}{stem}.csv"

    def describe(self, key: str) -> str:
        e = self.entry(key)
        size = f", {e['rows']:,} rows" if "rows" in e else ""
        parent = e.get("parent")
        origin = f", from {self._reg['items'][parent]['name']}" if parent in self._reg["items"] else ""
        return f"{e['name']}" + (f" ({size.lstrip(', ')}{origin})" if size or origin else "")

    # ------------------------------------------------------------------ internals

    def _expect(self, key: str, kind: str) -> None:
        if self.kind(key) != kind:
            raise ToolError(f"{self.label(key)} is {_a(self.kind(key))}, not {_a(kind)}.", field="id")

    def free_path(self, path: str, own: str | None = None) -> str:
        """A path a file can move to: tidied, and unique - 'name (2).ext' when another file has it (own: the file
        moving, whose own path doesn't count)."""
        return self._free(clean_path(path, strict=False), own=own)

    @_locked
    def _free(self, path: str, own: str | None = None) -> str:
        """``path``, or 'name (2).ext' ... when another file has it (any case) or something is already there."""
        taken = {e["name"].casefold() for k, e in self._reg["items"].items() if k != own}
        p = PurePosixPath(path)
        candidate, n = path, 1
        while candidate.casefold() in taken or (self.files / candidate).exists() and (own is None or (
                self.root / self._reg["items"][own]["file"]) != self.files / candidate):
            n += 1
            stem = f"{p.stem} ({n}){p.suffix}"
            candidate = stem if str(p.parent) in ("", ".") else f"{p.parent}/{stem}"
        return candidate

    @_locked
    def _new_id(self, kind: str) -> str:
        n = self._reg["next"].get(kind, 1)
        self._reg["next"][kind] = n + 1
        return f"{PREFIX[kind]}_{n}"

    @_locked
    def _save(self) -> None:
        self.zen.mkdir(parents=True, exist_ok=True)
        tmp = self._registry_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._reg, indent=1, ensure_ascii=False), "utf-8")
        os.replace(tmp, self._registry_path)

    # ------------------------------------------------------------------ the first layout, moved once

    def _migrate(self) -> None:
        """A workspace of the first layout (data_1.parquet, image_3.png, file_2.py ... beside registry.json)
        moved into this one: every file to its path under files/ (unique; a generated picture's long prompt
        name shortened; the format's extension added where the name had none), a table written as its CSV
        file with its typed copy as the cache, reports under .zen (notes are left: agent.md replaced them), who brought each file (an upload
        or a download: the user; anything the assistant made: the assistant), and its hash. The old registry
        and script folder are kept under .zen. Nothing is deleted."""
        old = json.loads((self.root / "registry.json").read_text("utf-8"))
        for d in (self.files, self.zen / "cache", self.zen / "reports", self.zen / "versions"):
            d.mkdir(parents=True, exist_ok=True)
        self._reg = {"next": old.get("next", {}), "items": {}}
        for key, e in old.get("items", {}).items():
            e = dict(e)
            meta = e.get("meta") or {}
            e["by"] = "assistant" if (meta.get("by") == "assistant" or meta.get("from") == "run_python"
                                      or meta.get("prompt") or meta.get("chart") or e.get("parent")
                                      or e["kind"] == "note") else "user"
            if e.get("report"):
                src = self.root / e["report"]["file"]
                rel = f"{ZEN}/reports/{key}.md"
                if src.exists():
                    shutil.move(src, self.root / rel)
                e["report"] = {**e["report"], "file": rel}
            if e["kind"] == "note":
                continue
            if e["kind"] == "table":
                src = self.root / f"{key}.parquet"
                if not src.exists():
                    continue
                name = str(e.get("name") or key)
                suffix = PurePosixPath(name).suffix.lower()
                if suffix not in (".csv", ".tsv", ".parquet"):
                    name = (PurePosixPath(name).stem if suffix in TABLE_FILES else name) + ".csv"
                path = self._free(clean_path(_legacy_name(name), strict=False))
                frame = pd.read_parquet(src)
                _write_atomic(self.files / path, _table_bytes(frame, path))
                shutil.move(src, self.zen / "cache" / f"{key}.parquet")
                e.update(name=path, file=f"{FILES}/{path}", stamp=_stamp(self.files / path))
            else:
                src = self.root / e.get("file", "")
                if not e.get("file") or not src.exists():
                    continue
                name = _legacy_name(str(e.get("name") or key), src.suffix, image=e["kind"] == "image")
                path = self._free(clean_path(name, strict=False))
                (self.files / path).parent.mkdir(parents=True, exist_ok=True)
                shutil.move(src, self.files / path)
                e.update(name=path, file=f"{FILES}/{path}")
            blob = (self.files / e["name"]).read_bytes()
            e.update(sha=hashlib.sha256(blob).hexdigest(), bytes=len(blob))
            self._reg["items"][key] = e
        for e in self._reg["items"].values():  # reports named files by key: by path now, like everything a model reads
            report = self.root / e["report"]["file"] if e.get("report") else None
            if report is not None and report.exists():
                report.write_text(present(self, report.read_text("utf-8")), "utf-8")
        self._save()
        os.replace(self.root / "registry.json", self.zen / "registry.v1.json")
        if (self.root / "sandbox").exists():
            shutil.move(self.root / "sandbox", self.zen / "old-sandbox")


# ---------------------------------------------------------------- paths and kinds

def clean_path(name: str, *, strict: bool) -> str:
    """A workspace path: '/' between parts, no '..', no part starting with '.', no character Windows forbids,
    no reserved device name. ``strict`` (a name from the model): refused with how to write one; otherwise
    (an upload, a script's file) tidied."""
    raw = " ".join(str(name).replace("\\", "/").split()).strip().strip("/")
    parts = [p.strip() for p in raw.split("/") if p.strip() not in ("", ".")]
    bad = (not parts or any(p == ".." or p.startswith(".") or BAD_CHARS.search(p)
                            or PurePosixPath(p).stem.lower() in RESERVED for p in parts)
           or len("/".join(parts)) > MAX_PATH)
    if bad and strict:
        raise ToolError(f"name must be {PATH_RULE}.", field="name")
    if bad:
        parts = [BAD_CHARS.sub("-", p).lstrip(".") or "file" for p in parts if p != ".."] or ["file"]
        parts = [f"_{p}" if PurePosixPath(p).stem.lower() in RESERVED else p for p in parts]
        joined = "/".join(parts)
        if len(joined) > MAX_PATH:
            p = PurePosixPath(joined)
            joined = joined[:MAX_PATH - len(p.suffix)].rstrip() + p.suffix
        return joined
    return "/".join(parts)


def kind_of(path: str) -> str:
    """What a file is, by its extension: a table, an image, or a file."""
    suffix = PurePosixPath(str(path)).suffix.lower()
    return "table" if suffix in TABLE_FILES else "image" if suffix in IMAGE_SUFFIXES else "file"


def with_suffix(path: str, ext: str) -> str:
    """The path with this extension ('.jpg' and '.jpeg' count as one)."""
    ext = "." + ext.lstrip(".")
    p = PurePosixPath(path)
    same = {p.suffix.lower(), ext.lower()} <= {".jpg", ".jpeg"} or p.suffix.lower() == ext.lower()
    if same:
        return path
    return str(p.with_suffix(ext)) if p.suffix.lower() in (*IMAGE_SUFFIXES, ".bin") else path + ext


def prune_versions(folder: Path) -> None:
    for old in sorted(p for p in folder.iterdir() if p.is_file())[:-VERSIONS_KEPT]:
        old.unlink(missing_ok=True)


def _legacy_name(name: str, suffix: str = "", image: bool = False) -> str:
    """A first-layout name as a path: a generated picture's prompt (no extension, up to 80 characters)
    shortened to a slug; the stored file's extension added where the name has none."""
    if suffix and PurePosixPath(name).suffix.lower() != suffix.lower():
        if image and len(name) > 40:
            from .image import file_name
            return file_name(name, suffix.lstrip("."))
        return with_suffix(name, suffix) if suffix else name
    return name


def _table_bytes(frame: pd.DataFrame, path: str) -> bytes:
    """A table as its file's bytes: Parquet for .parquet, tab-separated for .tsv, else CSV (dates as ISO)."""
    suffix = PurePosixPath(path).suffix.lower()
    if suffix == ".parquet":
        out = io.BytesIO()
        frame.to_parquet(out, index=False)
        return out.getvalue()
    return frame.to_csv(index=False, sep="\t" if suffix == ".tsv" else ",", date_format="%Y-%m-%d %H:%M:%S").encode("utf-8")


def _read_any_table(path: Path, sheet: str | None) -> tuple[pd.DataFrame, dict]:
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path), {}
    return read_table(path, sheet)


def _write_atomic(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.part")
    tmp.write_bytes(data)
    os.replace(tmp, target)


def _stamp(path: Path) -> list[int]:
    try:
        st = path.stat()
    except OSError:
        return [0, 0]
    return [st.st_size, st.st_mtime_ns]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _saved(version: str) -> str:
    try:
        return datetime.strptime(version[:22], "%Y%m%d-%H%M%S-%f").isoformat(timespec="seconds")
    except ValueError:
        return ""


def _a(noun: str) -> str:
    return ("an " if noun[:1] in "aeiou" else "a ") + noun


def read_table(path: str | Path, sheet: str | None = None) -> tuple[pd.DataFrame, dict]:
    """A CSV / Excel file as a typed table and what reading it found (column types, the header row,
    skipped lines) - the meta a table keeps."""
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


# What the model reads of a tool result: payloads of data (rows, printed output, document text, code) are
# passed as they are - a key-like value there is the user's data, not a reference.
DATA_FIELDS = frozenset({"rows", "first_rows", "output", "errors", "text", "head", "sample", "examples", "values",
                         "code", "content"})


def present(ws: Workspace, value):
    """A tool result as the model reads it: every internal key of a file in this workspace written as the
    file's path - a field that holds one, and one inside a sentence ('Saved as data_3'). The one place keys
    turn into paths, so no tool has to remember to (ToolResult.for_model)."""
    items = ws._reg["items"]

    def path_of(match: re.Match) -> str:
        e = items.get(match[1])
        return e["name"] if e else match[0]

    def walk(v, skip: bool = False):
        if isinstance(v, dict):
            return {k: walk(x, skip or k in DATA_FIELDS) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x, skip) for x in v]
        if isinstance(v, str) and not skip and ITEM_REF.search(v):
            return ITEM_REF.sub(path_of, v)
        return v
    return walk(value)
