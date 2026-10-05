"""Request and response models for the HTTP API."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from legacy.ingestion.mapper import Mapping

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
    default_name: str = Field(description="name used if this column is kept as a custom column")


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


class MappingCheckOut(BaseModel):
    problems: list[str] = Field(description="what must change before the mapping can be loaded")
    missing_required: list[str]
    locked_features: dict[str, list[str]]


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


class RangeOut(BaseModel):
    has_data: bool
    start: date | None = None
    end: date | None = None


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


# --- ML ------------------------------------------------------------------------

class ForecastIn(BaseModel):
    horizon: int = Field(30, ge=1, le=180, description="days or weeks ahead")
    grain: Literal["day", "week"] = "day"
    by: Literal["category", "product", "location", "channel"] | None = Field(
        None, description="forecast one slice, e.g. by=category, value=Electronics")
    value: str | None = None

    @model_validator(mode="after")
    def _by_and_value_together(self) -> ForecastIn:
        if (self.by is None) != (self.value is None):
            raise ValueError("give both 'by' and 'value', or neither")
        return self


class RunOut(BaseModel):
    run_id: int
    task: str
    status: Literal["running", "succeeded", "failed"]
    params: dict[str, Any]
    metrics: dict[str, Any] | None
    message: str | None
    started_at: datetime
    finished_at: datetime | None


class ForecastPointOut(BaseModel):
    bucket: date
    predicted: float
    lower: float | None
    upper: float | None


class ActualPointOut(BaseModel):
    bucket: date
    actual: float


class ForecastOut(BaseModel):
    run: RunOut
    history: list[ActualPointOut]
    points: list[ForecastPointOut]


class SegmentOut(BaseModel):
    segment: str
    customers: int
    share_pct: float
    revenue: float
    revenue_share_pct: float | None
    median_recency_days: float
    median_orders: float
    median_revenue: float
    meaning: str
    action: str


class SegmentsOut(BaseModel):
    run: RunOut
    segments: list[SegmentOut]


class SegmentCustomerOut(BaseModel):
    customer_id: str
    segment: str
    recency_days: int
    orders: int
    revenue: float


class SegmentCustomersPage(BaseModel):
    total: int
    rows: list[SegmentCustomerOut]


class AnomalyDayOut(BaseModel):
    day: date
    revenue: float
    expected_revenue: float
    orders: int
    expected_orders: float
    signal: Literal["orders", "revenue", "both"]
    score: float
    direction: Literal["spike", "drop"]


class AnomalyLineOut(BaseModel):
    sale_id: int
    order_id: str
    order_date: datetime
    product: str
    quantity: float
    unit_price: float
    discount: float
    score: float
    reason: str


class DailyPointOut(BaseModel):
    day: date
    revenue: float
    orders: int


class AnomaliesOut(BaseModel):
    run: RunOut
    days: list[AnomalyDayOut]
    lines: list[AnomalyLineOut]
    series: list[DailyPointOut]


class ChurnIn(BaseModel):
    horizon_days: Literal[30, 60, 90] = Field(90, description="predict who buys nothing in this many days")


class ChurnBandOut(BaseModel):
    risk: Literal["high", "medium", "low"]
    customers: int
    value_at_risk: float
    avg_probability: float


class ChurnCustomerOut(BaseModel):
    customer_id: str
    probability: float
    risk: Literal["high", "medium", "low"]
    recency_days: int
    typical_gap_days: float
    purchases: int
    recent_revenue: float
    revenue: float
    value_at_risk: float
    signals: str


class ChurnOut(BaseModel):
    run: RunOut
    bands: list[ChurnBandOut]
    customers: list[ChurnCustomerOut]


class ChurnCustomersPage(BaseModel):
    total: int
    rows: list[ChurnCustomerOut]


class BasketRuleOut(BaseModel):
    level: Literal["product", "category"]
    antecedent: str
    consequent: str
    orders_together: int
    antecedent_orders: int
    expected_together: float
    support_pct: float
    confidence_pct: float
    lift: float


class BasketOut(BaseModel):
    run: RunOut
    item: str | None
    rules: list[BasketRuleOut]


# --- documents -----------------------------------------------------------------

class DocumentOut(BaseModel):
    doc_id: int
    filename: str
    kind: Literal["pdf", "docx", "txt", "md"]
    title: str | None
    pages: int | None
    words: int
    chunks: int
    uploaded_at: datetime


class DocumentAddedOut(DocumentOut):
    replaced: DocumentOut | None = Field(None, description="an earlier upload with the same filename, now replaced")
    semantic: bool = Field(description="false if the passages couldn't be embedded; search uses keywords until reindexed")


class LibraryStatusOut(BaseModel):
    documents: int
    passages: int
    embedded: int = Field(description="passages with a vector from the current embedding model")
    embed_model: str
    semantic_error: str | None


class LibraryOut(BaseModel):
    status: LibraryStatusOut
    documents: list[DocumentOut]


class PassageOut(BaseModel):
    chunk_id: int
    ord: int
    page: int | None
    section: str | None
    text: str


class SearchHitOut(BaseModel):
    chunk_id: int
    doc_id: int
    filename: str
    title: str
    page: int | None
    section: str | None
    text: str
    source: str
    score: float
    keyword: float | None = Field(description="BM25 score, if the passage shares words with the query")
    similarity: float | None = Field(description="cosine similarity of meaning, if embedded")


class SearchOut(BaseModel):
    query: str
    mode: Literal["hybrid", "keyword"]
    note: str | None
    hits: list[SearchHitOut]


class SamplesOut(BaseModel):
    added: list[DocumentAddedOut]
    skipped: list[str] = Field(description="sample files already in the library")
