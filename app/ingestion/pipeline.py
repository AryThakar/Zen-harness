"""Upload flow: preview a file (suggested mapping) -> user confirms -> clean -> load."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.config import settings
from app.db import registry
from app.db.connection import Database

from .cleaner import clean
from .loader import UploadReport, load
from .mapper import (
    Mapping, Profile, Suggestion, custom_column_name, locked_features, suggest_mapping, validate_mapping,
)
from .parsing import infer_type
from .profiles import load_profiles, save_profile
from .reader import RawTable, read_file

PREVIEW_SAMPLES = 3


@dataclass
class ColumnPreview:
    name: str  # normalized name used in mappings
    original: str  # as written in the file
    samples: list[str]
    maps_to: str | None  # canonical field, if mapped
    custom_name: str | None
    custom_type: str | None
    default_name: str  # the name it gets if kept as a custom column


@dataclass
class Preview:
    filename: str
    rows: int
    header_row: int
    encoding: str | None
    delimiter: str | None
    sheet: str | None
    columns: list[ColumnPreview]
    suggestion: Suggestion


def preview_file(db: Database, path: str | Path, *, sheet: str | None = None) -> Preview:
    raw = read_file(path, sheet)
    profiles, existing = _read_context(db)
    suggestion = suggest_mapping(list(raw.df.columns), profiles, raw.original_names)
    mapping = suggestion.mapping
    field_of = {source: fname for fname, source in mapping.fields.items()}

    columns = []
    for name in raw.df.columns:
        custom_name = mapping.custom.get(name)
        samples = list(dict.fromkeys(v for v in raw.df[name].head(50) if v))[:PREVIEW_SAMPLES]
        columns.append(ColumnPreview(
            name=name,
            original=raw.original_names[name],
            samples=samples,
            maps_to=field_of.get(name),
            custom_name=custom_name,
            custom_type=(existing.get(custom_name) or infer_type(custom_name, raw.df[name]))
            if custom_name else None,
            default_name=custom_column_name(name),
        ))
    return Preview(
        filename=Path(path).name,
        rows=len(raw.df),
        header_row=raw.header_row,
        encoding=raw.encoding,
        delimiter=raw.delimiter,
        sheet=raw.sheet,
        columns=columns,
        suggestion=suggestion,
    )


def ingest_file(
    db: Database,
    path: str | Path,
    mapping: Mapping | None = None,
    *,
    filename: str | None = None,
    sheet: str | None = None,
    save_profile_as: str | None = None,
    profile_name: str | None = None,
    today: date | None = None,
) -> UploadReport:
    """Clean and load a file. Without ``mapping`` the suggested mapping is used.

    ``profile_name`` records which recognised format the mapping came from.
    """
    raw = read_file(path, sheet)
    profiles, existing = _read_context(db)
    profile_name = save_profile_as or profile_name
    if mapping is None:
        suggestion = suggest_mapping(list(raw.df.columns), profiles, raw.original_names)
        mapping = suggestion.mapping
        profile_name = profile_name or suggestion.profile
    validate_mapping(mapping, list(raw.df.columns))

    # Cleaning is the slow part, so it runs before taking the write lock...
    result = clean(
        raw, mapping, custom_types=existing, today=today,
        default_date_order=settings.default_date_order,
    )
    with db.transaction() as cur:
        # ...but another upload may have created one of our custom columns with a
        # different type in the meantime. Then clean again with the stored types.
        current = registry.custom_types(cur)
        if any(current.get(name, sql_type) != sql_type for name, sql_type in result.custom_types.items()):
            result = clean(
                raw, mapping, custom_types=current, today=today,
                default_date_order=settings.default_date_order,
            )
        if save_profile_as:
            save_profile(cur, save_profile_as, mapping)
        return load(
            cur, result,
            filename=filename or Path(path).name,
            mapping=mapping,
            rows_total=_rows_total(raw),
            profile_name=profile_name,
            locked_features=locked_features(mapping.fields),
        )


def _read_context(db: Database) -> tuple[list[Profile], dict[str, str]]:
    cur = db.cursor()
    try:
        return load_profiles(cur), registry.custom_types(cur)
    finally:
        cur.close()


def _rows_total(raw: RawTable) -> int:
    return len(raw.df) + len(raw.bad_lines)
