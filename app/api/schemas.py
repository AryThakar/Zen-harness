"""Request and response models for the HTTP API."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.ingestion.mapper import Mapping

FILE_ID = r"^[0-9a-f]{32}$"


class MappingModel(BaseModel):
    fields: dict[str, str] = Field(default_factory=dict, description="canonical field -> file column")
    custom: dict[str, str] = Field(default_factory=dict, description="file column -> custom column name")
    ignore: list[str] = Field(default_factory=list, description="file columns to drop")
    date_order: Literal["auto", "dayfirst", "monthfirst"] = "auto"
    discount_type: Literal["auto", "percent", "amount"] = "auto"
    descriptions: dict[str, str] = Field(default_factory=dict, description="custom column -> meaning")

    def to_mapping(self) -> Mapping:
        return Mapping.from_dict(self.model_dump())

    @classmethod
    def from_mapping(cls, mapping: Mapping) -> MappingModel:
        return cls(**mapping.to_dict())


# --- uploads -----------------------------------------------------------------

class FieldInfo(BaseModel):
    name: str
    kind: Literal["required", "optional", "input"]
    dtype: str
    description: str
    unlocks: list[str]


class ColumnPreviewOut(BaseModel):
    name: str
    original: str
    samples: list[str]
    maps_to: str | None
    custom_name: str | None
    custom_type: str | None


class FieldMatchOut(BaseModel):
    field: str
    source: str
    method: Literal["profile", "alias", "fuzzy"]
    score: float


class PreviewOut(BaseModel):
    file_id: str
    filename: str
    rows: int
    header_row: int
    encoding: str | None
    delimiter: str | None
    sheet: str | None
    sheets: list[str]
    columns: list[ColumnPreviewOut]
    mapping: MappingModel
    matches: list[FieldMatchOut]
    profile: str | None
    missing_required: list[str]
    locked_features: dict[str, list[str]]
    fields: list[FieldInfo]


class ConfirmIn(BaseModel):
    file_id: str = Field(pattern=FILE_ID)
    mapping: MappingModel | None = Field(None, description="omit to use the suggested mapping")
    save_profile_as: str | None = Field(None, min_length=1, max_length=80)
    profile_name: str | None = Field(None, max_length=80, description="recognised format, for the record")


class UploadReportOut(BaseModel):
    upload_id: int
    filename: str
    rows_total: int
    rows_loaded: int
    rows_rejected: int
    rows_duplicate: int
    rejected_reasons: dict[str, int]
    warnings: list[str]
    custom_columns: dict[str, str]
    locked_features: dict[str, list[str]]
    summary: str


class UploadOut(BaseModel):
    upload_id: int
    filename: str
    profile_name: str | None
    rows_total: int
    rows_loaded: int
    rows_rejected: int
    rows_duplicate: int
    warnings: list[str]
    created_at: datetime


class UploadDetailOut(UploadOut):
    mapping: MappingModel
    rejected_reasons: dict[str, int]


class RejectedRowOut(BaseModel):
    row_number: int
    reasons: str
    raw: dict[str, Any]


class RejectedPage(BaseModel):
    total: int
    rows: list[RejectedRowOut]


class DemoDataIn(BaseModel):
    months: int = Field(18, ge=1, le=36)
    orders_per_day: float = Field(40, gt=0, le=500)


# --- data --------------------------------------------------------------------

class ColumnInfoOut(BaseModel):
    column_name: str
    data_type: str
    kind: str
    description: str | None
    sample_values: list[Any]


class DescriptionIn(BaseModel):
    description: str | None = Field(None, max_length=500)


class ProfileOut(BaseModel):
    name: str
    builtin: bool
    mapping: MappingModel
    created_at: datetime


class SalesPage(BaseModel):
    total: int
    columns: list[str]
    rows: list[dict[str, Any]]


# --- dashboard ---------------------------------------------------------------

class PeriodOut(BaseModel):
    start: date
    end: date
    days: int


class KpiOut(BaseModel):
    value: float | None
    previous: float | None
    change_pct: float | None


class SummaryOut(BaseModel):
    has_data: bool
    period: PeriodOut | None = None
    previous_period: PeriodOut | None = None
    comparison: Literal["full", "partial", "none"] | None = Field(
        None, description="how much of the previous period the data covers; % changes only when 'full'")
    data_range: PeriodOut | None = None
    kpis: dict[str, KpiOut] = Field(default_factory=dict)
    available: dict[str, bool] = Field(default_factory=dict)
    locked: dict[str, list[str]] = Field(default_factory=dict)


class TrendPointOut(BaseModel):
    bucket: date
    revenue: float
    profit: float | None
    orders: int
    partial: bool


class TrendOut(BaseModel):
    grain: str | None
    period: PeriodOut | None
    points: list[TrendPointOut]


class BreakdownItemOut(BaseModel):
    label: str
    revenue: float
    profit: float | None = None
    units: float | None = None
    orders: int | None = None
    share_pct: float | None


class BreakdownOut(BaseModel):
    by: str
    period: PeriodOut | None
    dimensions: list[str]
    items: list[BreakdownItemOut]
    other: BreakdownItemOut | None
    total_revenue: float


class HealthOut(BaseModel):
    status: Literal["ok"]
    sales_rows: int
    uploads: int
