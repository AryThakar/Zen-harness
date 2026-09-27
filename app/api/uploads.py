"""Upload flow: POST /uploads/preview -> (user edits mapping) -> POST /uploads."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, timedelta

import duckdb
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status

from app.datagen import generate
from app.db.connection import Database
from app.ingestion.fields import FIELDS
from app.ingestion.loader import UploadReport, delete_upload
from app.ingestion.mapper import MappingError
from app.ingestion.pipeline import ingest_file, preview_file
from app.ingestion.reader import ReadError, list_sheets

from .deps import get_cursor, get_db, get_staging
from .schemas import (
    ConfirmIn, DemoDataIn, FieldInfo, MappingModel, PreviewOut, RejectedPage, UploadDetailOut,
    UploadOut, UploadReportOut,
)
from .staging import FileTooLarge, Staging, StagingError

router = APIRouter(prefix="/uploads", tags=["uploads"])

FIELD_CATALOG = [
    FieldInfo(name=f.name, kind=f.kind, dtype=f.dtype, description=f.description, unlocks=list(f.unlocks))
    for f in FIELDS
]
UPLOAD_COLUMNS = (
    "upload_id, filename, profile_name, rows_total, rows_loaded, rows_rejected, rows_duplicate, "
    "warnings, created_at"
)


@router.post("/preview", response_model=PreviewOut)
def preview_upload(
    file: UploadFile = File(...),
    sheet: str | None = Form(None),
    db: Database = Depends(get_db),
    staging: Staging = Depends(get_staging),
) -> PreviewOut:
    """Store the file and suggest how its columns map to the standard format."""
    try:
        staged = staging.save(file.file, file.filename or "", sheet)
    except FileTooLarge as exc:
        raise HTTPException(413, str(exc)) from exc
    except StagingError as exc:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(exc)) from exc
    try:
        preview = preview_file(db, staged.path, sheet=sheet)
        sheets = list_sheets(staged.path)
    except ReadError as exc:
        staging.delete(staged.file_id)
        raise HTTPException(422, f"Could not read the file: {exc}") from exc

    s = preview.suggestion
    return PreviewOut(
        file_id=staged.file_id,
        filename=staged.filename,
        rows=preview.rows,
        header_row=preview.header_row,
        encoding=preview.encoding,
        delimiter=preview.delimiter,
        sheet=preview.sheet or (sheets[0] if sheets else None),
        sheets=sheets,
        columns=[asdict(c) for c in preview.columns],
        mapping=MappingModel.from_mapping(s.mapping),
        matches=[asdict(m) for m in s.matches],
        profile=s.profile,
        missing_required=s.missing_required,
        locked_features=s.locked_features,
        fields=FIELD_CATALOG,
    )


@router.post("", response_model=UploadReportOut, status_code=status.HTTP_201_CREATED)
def confirm_upload(
    body: ConfirmIn,
    db: Database = Depends(get_db),
    staging: Staging = Depends(get_staging),
) -> UploadReportOut:
    """Clean and load a previewed file with the (possibly edited) mapping."""
    try:
        staged = staging.get(body.file_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown or expired file_id; upload the file again.") from exc
    try:
        report = ingest_file(
            db, staged.path,
            body.mapping.to_mapping() if body.mapping else None,
            filename=staged.filename,
            sheet=staged.sheet,
            save_profile_as=body.save_profile_as,
            profile_name=body.profile_name,
        )
    except (MappingError, ReadError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    staging.delete(body.file_id)
    return _report_out(report)


@router.post("/demo", response_model=UploadReportOut, status_code=status.HTTP_201_CREATED)
def load_demo_data(
    body: DemoDataIn | None = None,
    db: Database = Depends(get_db),
    staging: Staging = Depends(get_staging),
) -> UploadReportOut:
    """Generate realistic synthetic sales (an electronics retailer) and load them."""
    body = body or DemoDataIn()
    df = generate(months=body.months, end=date.today() - timedelta(days=1), orders_per_day=body.orders_per_day)
    path = staging.root / f"demo-{body.months}m.csv"
    df.to_csv(path, index=False, date_format="%Y-%m-%d %H:%M:%S")
    try:
        report = ingest_file(db, path, filename="demo data (synthetic)")
    finally:
        path.unlink(missing_ok=True)
    return _report_out(report)


@router.get("", response_model=list[UploadOut])
def list_uploads(cur: duckdb.DuckDBPyConnection = Depends(get_cursor)) -> list[UploadOut]:
    rows = cur.execute(f"SELECT {UPLOAD_COLUMNS} FROM uploads ORDER BY upload_id DESC").fetchall()
    return [_upload_out(r) for r in rows]


@router.get("/{upload_id}", response_model=UploadDetailOut)
def get_upload(upload_id: int, cur: duckdb.DuckDBPyConnection = Depends(get_cursor)) -> UploadDetailOut:
    row = cur.execute(f"SELECT {UPLOAD_COLUMNS}, mapping FROM uploads WHERE upload_id = ?", [upload_id]).fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Upload {upload_id} not found.")
    reasons = cur.execute(
        """
        SELECT reason, count(*) FROM (
            SELECT unnest(string_split(reasons, '; ')) AS reason FROM rejected_rows WHERE upload_id = ?
        ) GROUP BY 1 ORDER BY 2 DESC, 1
        """,
        [upload_id],
    ).fetchall()
    base = _upload_out(row[:-1])
    return UploadDetailOut(
        **base.model_dump(),
        mapping=MappingModel(**json.loads(row[-1])),
        rejected_reasons=dict(reasons),
    )


@router.get("/{upload_id}/rejected", response_model=RejectedPage)
def rejected_rows(
    upload_id: int,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    cur: duckdb.DuckDBPyConnection = Depends(get_cursor),
) -> RejectedPage:
    total = cur.execute("SELECT count(*) FROM rejected_rows WHERE upload_id = ?", [upload_id]).fetchone()[0]
    rows = cur.execute(
        "SELECT row_number, reasons, raw FROM rejected_rows WHERE upload_id = ? "
        "ORDER BY row_number LIMIT ? OFFSET ?",
        [upload_id, limit, offset],
    ).fetchall()
    return RejectedPage(
        total=total,
        rows=[{"row_number": n, "reasons": r, "raw": json.loads(raw)} for n, r, raw in rows],
    )


@router.delete("/{upload_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_upload(upload_id: int, db: Database = Depends(get_db)) -> None:
    """Undo an upload: its sales rows and rejected rows are deleted."""
    with db.transaction() as cur:
        if not delete_upload(cur, upload_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Upload {upload_id} not found.")


def _report_out(report: UploadReport) -> UploadReportOut:
    return UploadReportOut(**asdict(report), summary=report.summary())


def _upload_out(row: tuple) -> UploadOut:
    names = [c.strip() for c in UPLOAD_COLUMNS.split(",")]
    data = dict(zip(names, row))
    data["warnings"] = json.loads(data["warnings"])
    return UploadOut(**data)
