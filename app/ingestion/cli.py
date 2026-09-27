"""Load a sales file from the command line.

    python -m app.ingestion.cli data/samples/retail_pos_messy.csv
    python -m app.ingestion.cli export.xlsx --sheet Sales --yes --save-profile "My POS"
"""

from __future__ import annotations

import argparse
import sys

from app.config import settings
from app.db.connection import Database

from .mapper import MappingError
from .pipeline import ingest_file, preview_file
from .reader import ReadError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preview, clean and load a sales export.")
    parser.add_argument("file")
    parser.add_argument("--db", default=settings.db_path, help=f"DuckDB file (default {settings.db_path})")
    parser.add_argument("--sheet", help="Excel sheet name")
    parser.add_argument("--yes", action="store_true", help="load without asking")
    parser.add_argument("--save-profile", metavar="NAME", help="remember this mapping for next time")
    args = parser.parse_args(argv)

    db = Database(args.db)
    try:
        preview = preview_file(db, args.file, sheet=args.sheet)
    except ReadError as exc:
        print(f"Could not read the file: {exc}", file=sys.stderr)
        return 1

    s = preview.suggestion
    print(f"\n{preview.filename}: {preview.rows:,} rows, header on row {preview.header_row}")
    if s.profile:
        print(f"Recognised format: {s.profile}")
    print("\nColumn mapping")
    for m in s.matches:
        original = next(c.original for c in preview.columns if c.name == m.source)
        note = "" if m.method == "alias" else f"  [{m.method}{'' if m.score == 100 else f' {m.score:.0f}'}]"
        print(f"  {m.field:<12} <- {original}{note}")
    custom = [c for c in preview.columns if c.custom_name]
    if custom:
        print("\nCustom columns")
        for c in custom:
            print(f"  {c.custom_name:<20} {c.custom_type:<9} e.g. {', '.join(c.samples) or '-'}")
    if s.mapping.ignore:
        print(f"\nIgnored: {', '.join(s.mapping.ignore)}")
    if s.locked_features:
        print("\nAdd these columns to unlock more:")
        for col, features in s.locked_features.items():
            print(f"  {col:<12} -> {', '.join(features)}")
    if s.missing_required:
        print(f"\nCannot load: required columns not found: {', '.join(s.missing_required)}")
        return 1

    if not args.yes and input("\nLoad with this mapping? [y/N] ").strip().lower() != "y":
        print("Cancelled.")
        return 0
    try:
        report = ingest_file(
            db, args.file, s.mapping, sheet=args.sheet,
            save_profile_as=args.save_profile, profile_name=s.profile,
        )
    except (MappingError, ValueError) as exc:
        print(f"Cannot load: {exc}", file=sys.stderr)
        return 1

    print(f"\nUpload #{report.upload_id}: {report.summary()}")
    for w in report.warnings:
        print(f"  note: {w}")
    if report.rows_rejected:
        print(f"  rejected rows are in table rejected_rows (upload_id = {report.upload_id})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
