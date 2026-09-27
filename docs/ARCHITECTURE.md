# Architecture

**Product:** an AI business analyst for small and medium businesses. A business
uploads its sales export; a copilot answers questions, forecasts, finds customers
at risk of leaving and draws charts.

Status legend: ✅ built · ⏳ planned (see [Roadmap](#roadmap)).

```
Upload CSV/Excel ──► reader ─► mapper ─► user confirms ─► cleaner ─► loader ─► DuckDB
                                                                              ▲   ▲   ▲
                    LangGraph agent (local LLM via Ollama)                    │   │   │
                    tools: describe_data · sql_query · search_docs ───────────┘   │   │
                           calculator · business_metrics                          │   │
                           forecast · run_ml ─────────────────────────────────────┘   │
                           visualize (Plotly JSON)                                    │
                    RAG: documents + glossary ───────────────────────────────────────┘
                              │
                    FastAPI (/uploads, /dashboard, /sales, /ml, /documents, /chat SSE)
                              │
                    Streamlit UI: dashboard, upload, explore (+ copilot side panel in week 4)
```

---

## 1. Data format ✅

One file type: the **sales line**. One row = one product sold on one bill / invoice / order.
Every business records this, and orders, customers, products and daily revenue can all
be built from it (not the other way round).

Defined in [`app/ingestion/fields.py`](../app/ingestion/fields.py).

### Required (4)

| Column | Type | Why |
|---|---|---|
| `order_date` | timestamp | every trend, forecast and "last month" question |
| `product` | text | what sells |
| `quantity` | number | units; negative = return |
| `unit_price` | number | revenue = quantity × unit_price − discount |

Files that only have a line total (typical for services) map it to `amount`:
quantity becomes 1 and unit_price is derived.

### Optional (7) - each unlocks features

| Column | Unlocks |
|---|---|
| `order_id` | order count, average order value, frequently bought together |
| `customer_id` | repeat customers, segments, churn prediction, lifetime value |
| `unit_cost` | profit and margin (also derived from `line_cost` or `line_profit`) |
| `category` | category reports and forecasts |
| `discount` | discount impact (money; `10%` and fractions are converted) |
| `location` | store / city / region comparison |
| `channel` | online vs offline vs marketplace |

Missing optional columns never block an upload; the app reports which features
they would unlock.

### Custom columns
Anything else (salesperson, payment mode, brand, ...) is stored as a real typed
column (`DOUBLE`, `TIMESTAMP`, `BOOLEAN` or `VARCHAR`) and recorded in
`column_registry` with a description and sample values, so the AI can use it.
Columns named like IDs, codes or phone numbers stay text.

---

## 2. Upload and cleaning pipeline ✅

`preview_file()` → user confirms or edits the mapping → `ingest_file()`.
Code: [`app/ingestion/`](../app/ingestion/).

| # | Step | Module | What happens |
|---|---|---|---|
| 1 | Read | `reader` | CSV (UTF-8 → Windows-1252 → detected encoding; `,` `;` tab `\|` delimiter) or Excel. Finds the real header row under title lines. Keeps file row numbers for error reports. Rows with extra fields are set aside |
| 2 | Normalize names | `reader` | `Unit Price (₹)` → `unit_price`, duplicates made unique |
| 3 | Map columns | `mapper` | saved/built-in format profile → alias list → fuzzy match (rapidfuzz ≥ 88). Leftovers become custom columns. A `%` in the original discount header marks it as a percentage |
| 4 | Fill order-level gaps | `cleaner` | exports that print the date/customer only on an order's first line get them copied to the other lines |
| 5 | Convert types | `parsing` | dates (ISO always year-first; day/month order detected from values > 12, else the configured default; Excel serials; timezone offset after a time of day dropped; outside 1900-2100 = bad), numbers (`₹1,23,456.50`, `Rs. 500`, `(500)`, `3,50`, `1.234,50`), IDs (`17850.0` → `17850`), consistent spelling of names |
| 6 | Derive | `cleaner` | quantity 1 for amount-only files, unit price from totals, unit cost from line cost/profit, money discount from rates |
| 7 | Validate | `cleaner` | each failed rule adds a reason code: `missing order_date`, `bad order_date`, `order_date in the future`, `missing product`, `quantity is 0`, `negative unit_price`, `bad discount`, `discount larger than line value`, `negative unit_cost`, ... `Total` footers get `summary row` |
| 8 | Compute | `cleaner` | `revenue`, `cost`, `profit`, `is_return`, `year`, `month`, `week`, `weekday` |
| 9 | De-duplicate | `loader` | content hash + occurrence counter (see below) |
| 10 | Load | `loader` | one transaction: add custom columns, insert new rows, store rejected rows with reasons and raw values, record the upload, rebuild `products` and `customers`, refresh sample values |

**Validation design:** plain vectorized pandas rules instead of a schema library,
because every rule must produce a readable reason per row. A row either parses
cleanly or is rejected - values are never guessed (`SKU-884` is not the number −884,
`12 pcs` is flagged, not read as 12).

**Money rules:** `revenue = quantity × unit_price − discount` holds for every row.
`discount` is stored in money with the line's sign, so for returns it reduces the refund.
With only a line total, the total is treated as the final value after discount.

**De-duplication:** the hash covers what identifies a transaction - order number (if
real), date, product, quantity, price, discount, customer, location and channel,
ignoring case - plus an occurrence counter. Two identical lines in one file stay two
rows (two customers buying the same item), while re-uploading a file or an
overlapping export skips rows already stored. Product attributes (category, cost)
and custom columns are left out so correcting them in a later export doesn't turn
old sales into new ones.

**Consistent names across uploads:** a product, category, location or channel that
differs only in case from a stored value takes the stored spelling.

**Concurrent uploads:** cleaning runs outside the write lock; inside it the custom
column types are re-read and, if another upload created one of the columns with a
different type meanwhile, the file is cleaned again with the stored types.

**Format profiles:** saved mappings, matched when all their mapped columns are in the
file. Built-in: Shopify orders export, Superstore dataset, UCI Online Retail
([`profiles.py`](../app/ingestion/profiles.py)). Users save their own with
`save_profile_as`, so the next export from the same system loads automatically.

**Upload report example:** `16 loaded · 5 rejected (bad order_date 1, quantity is 0 1,
missing product 1, bad unit_price 1, summary row 1)`.

**Speed:** about 135k rows in 4 s and 540k rows in about 15 s on one core.

---

## 3. DuckDB schema ✅ (ingestion tables)

One file, `data/business.duckdb` (`BIZ_DB_PATH`). Schema: [`app/db/schema.sql`](../app/db/schema.sql).

| Table | Holds |
|---|---|
| `sales` | canonical + derived + custom columns, `upload_id`, `row_hash`, `order_id_generated` |
| `products` (view) | per product: category, first/last sold, units sold/returned, revenue, avg net price, profit, margin % |
| `customers` (view) | per customer: first/last order, orders, revenue, avg order value, recency (days before the latest sale in the data) |
| `uploads` | filename, profile, row counts, mapping used, warnings |
| `rejected_rows` | upload, file row number, reasons, raw values |
| `format_profiles` | saved and built-in mappings |
| `column_registry` | every sales column: type, kind (required/optional/derived/custom), description; example values are computed from a repeatable 20k-row sample when read |
| `schema_migrations` | which numbered files in `app/db/migrations/` have been applied |
| `ml_runs` | every model run: task, params, status, metrics, message |
| `forecast_points`, `customer_segments`, `anomaly_days`, `anomaly_lines` | model results per run, plus `latest_*` views |
| `documents`, `doc_chunks`, `glossary` ⏳ | RAG documents, chunks with `FLOAT[768]` embeddings, metric definitions |

ML outputs are written back to DuckDB so the agent can query them with SQL.
Concurrency: one connection per process, a cursor per request, writes serialized by a lock.

**Migrations:** the schema is built by numbered SQL files, each applied once and
recorded, so an existing database file upgrades itself when the app changes.
`products` and `customers` are views, so uploads and deletes don't rebuild anything.

---

## 4. ML ✅ (forecast, segments, anomalies)

Code: [`app/ml/`](../app/ml/). Each task is a function that reads with a cursor and
returns a `TaskResult` (metrics, a plain-language message, result rows). The runner
records every run in `ml_runs` (running → succeeded / failed), computes outside the
write lock, then saves results in one transaction. The same function will back the
AI agent's tools in week 4.

| Task | Needs | Method | Output tables |
|---|---|---|---|
| **Forecast** | ≥ 3× the horizon and ≥ 8 weeks of history | statsforecast: exponential smoothing (AutoETS) and Theta vs a "repeat the previous period" baseline (seasonal naive), walk-forward backtest on up to 3 past periods, best by WAPE; the baseline is used if nothing beats it. Daily (weekly seasonality) or weekly; all sales or one category / product / location / channel | `forecast_points` (predicted + 80% range) |
| **Customer segments** | ≥ 50 customers | RFM (recency, orders, net revenue; log scale for the skewed two) → StandardScaler → KMeans for k = 3-6; the most groups whose silhouette is within 10% of the best (silhouette alone nearly always picks 3, too coarse to act on). Groups named from their median customer's percentiles: Champions, Loyal, At risk, New, Occasional, Lost - each with a meaning and a suggested action | `customer_segments` |
| **Unusual days** | ≥ 28 days | For orders and for revenue: expected = weekday profile × rolling 29-day median level; robust z-score (median/MAD) of the log difference; flag \|z\| ≥ 3.5. Both signals because revenue is noisy (one laptop doubles a day) while order counts are steady - a flash sale shows in orders first | `anomaly_days` |
| **Unusual lines** | ≥ 200 lines | IsolationForest on price, quantity and discount relative to each product's median, reported only when something is actually off (price ×1.5, quantity ×3, discount > 30%) with a plain reason | `anomaly_lines` |

**Honesty rules:** every model reports its backtest error next to the baseline's and
how often its range held; too little data gives a message saying what's missing, not
a weak result; a failed run stores why.

**Checked on the synthetic data:** ETS was 40% more accurate than the baseline (WAPE
26% vs 43%) and its 80% range held 83% of the time; the planted flash sale (orders
3.9× normal) and outage (orders -81%) are both found; planted price and quantity
typos are flagged; Champions are 18% of customers bringing 55% of revenue.

**Runs:** `POST /ml/{task}` returns 202 and runs in the background; the UI polls
`GET /ml/runs/{id}`. An identical run that is still going is reused (double clicks),
and runs cut off by a restart are marked failed at startup. Views `latest_forecast`,
`latest_customer_segments` and `latest_anomaly_days` hold the newest results for SQL.

Still to come (week 5): churn prediction and "frequently bought together".

## 5. RAG ⏳

* **Content:** business documents (catalogue, pricing/discount/return policy,
  contracts, SOPs, notes, reports) plus generated text: glossary ("Active customer =
  ordered in last 90 days"), column descriptions, summaries of past ML runs.
* **Pipeline:** extract (pypdf, python-docx) → chunk ~500 tokens / 50 overlap →
  embed with `nomic-embed-text` (Ollama, 768 dims) → `doc_chunks` → hybrid search
  (cosine similarity + DuckDB full-text BM25, merged) → top 5 with file and page.
* **Why in a data app:** answers "why" questions from documents, and the glossary
  keeps generated SQL correct for business terms.
* Brute-force cosine in DuckDB is fast enough below ~100k chunks; no separate vector DB.

## 6. Local LLM ⏳

Ollama in Docker; a 7-14B instruct model with tool calling (filter the Ollama
library by "tools"); `nomic-embed-text` for embeddings. `LLM_PROVIDER=ollama|cloud`
switches providers through LangChain's `init_chat_model`. Local mode keeps all data
on the machine.

## 7. Agent (LangGraph) ⏳

```
START → prepare_context (schema card from column_registry + UI filters + glossary hits)
      → agent (LLM + tools) ⇄ tools (parallel)   [max 6 rounds]
      → finalize (attach charts, tables, sources) → END
```

Rules: every number comes from a tool; charts only via `visualize` built from SQL;
name the missing column when a feature is locked; cite document sources.
Memory: LangGraph SQLite checkpointer per session.

## 8. Tools ⏳

| Tool | Input → output | Safety |
|---|---|---|
| `describe_data` | table/column → types, descriptions, distinct values | read-only |
| `sql_query` | SELECT → ≤ 200 rows + count | sqlglot: single SELECT; blocks ATTACH/COPY/INSTALL/PRAGMA; auto LIMIT; timeout |
| `search_docs` | question → chunks + source | - |
| `calculator` | expression → exact number | simpleeval, never `eval()` |
| `business_metrics` | growth %, margin %, CAGR, AOV, break-even, ROI, CLV | fixed tested formulas |
| `forecast` | level, name, horizon → forecast + range + backtest error | min-data check |
| `run_ml` | segments / churn / anomalies / basket → summary + stored results | column + min-data check |
| `visualize` | chart type + SQL → Plotly JSON | runs the SQL itself |

## 9. API (FastAPI) ✅ (ingestion and dashboard)

Code: [`app/main.py`](../app/main.py), [`app/api/`](../app/api/). OpenAPI docs at `/docs`.

| Endpoint | Purpose |
|---|---|
| `POST /uploads/preview` | multipart file (+ optional sheet) → staged `file_id`, detected layout, suggested mapping, field catalogue |
| `POST /uploads/check` | mapping → problems in plain sentences + locked features (the UI calls this instead of copying the rules) |
| `POST /uploads` | `{file_id, mapping?, save_profile_as?}` → clean + load → report (staged file is single-use) |
| `POST /uploads/demo` | generate and load synthetic data |
| `GET /uploads`, `GET /uploads/{id}`, `GET /uploads/{id}/rejected`, `DELETE /uploads/{id}` | history, detail with mapping and reason counts, paginated rejected rows, undo |
| `GET /dashboard/range`, `/dimensions` | first/last sale date; columns `breakdown` accepts |
| `GET /dashboard/summary`, `/trend`, `/breakdown` | KPIs + comparison, trend buckets, top-N + "Other" |
| `GET /sales` | filtered, sorted, paginated rows (all columns incl. custom) |
| `GET /schema`, `PATCH /schema/{column}` | column registry; edit descriptions |
| `GET /profiles`, `DELETE /profiles/{name}` | saved mappings (built-ins protected) |
| `GET /health` | row and upload counts |
| `POST /ml/forecast`, `/ml/segments`, `/ml/anomalies` | start a run (202, background) |
| `GET /ml/runs`, `/ml/runs/{id}` | run history and status |
| `GET /ml/forecast`, `/ml/segments`, `/ml/segments/customers`, `/ml/anomalies` | latest (or a given run's) results with the data behind them |
| `POST /documents`, `POST /chat` ⏳ | weeks 4-5 |

* **Two-step upload:** preview stores the file under `data/staging/<uuid>` (24 h
  expiry, 100 MB limit, CSV/TSV/TXT/XLSX/XLSM only) so the user can edit the
  mapping before anything touches the database.
* **Errors:** unreadable file / bad mapping → 422 with a readable message,
  unsupported type → 415, too large → 413, unknown `file_id` → 404.
* Endpoints are sync functions (FastAPI runs them in a thread pool); reads use a
  per-request cursor, writes go through the database lock.

## 10. Dashboard and UI ✅

Analytics: [`app/analytics/dashboard.py`](../app/analytics/dashboard.py) - plain
functions over a cursor, so the agent's tools can reuse them.

* **Default period:** the last 90 days *of data* (historical datasets still show something).
* **Comparison:** each KPI is compared with the same-length period before, but %
  changes are shown only when the data fully covers that period (`comparison` =
  full / partial / none). Otherwise a half-covered previous year reads as +100% growth.
* **Trend:** day/week/month picked from the period length; empty buckets filled with
  zero; buckets cut off by the period edges flagged `partial` and drawn as open circles.
* **Breakdowns:** any text column from the registry (custom ones included), top N by
  revenue plus an "Other" bucket; weekday in calendar order. Dimension names are
  checked against the registry, never interpolated from user input.
* **Locked metrics:** profit/margin need cost data, customer KPIs need `customer_id`;
  without them the tiles show "—" and say which column to add.

UI: Streamlit ([`frontend/`](../frontend/)) talking only to the API over HTTP.
Pages: Dashboard (period presets, 8 KPI tiles, trend with Revenue / Orders / Profit
switch, four breakdown charts), Insights (forecast with actual vs dashed forecast and
80% band plus error vs baseline; segment sizes, revenue share, meaning and action,
customer lists; unusual days on the orders or revenue line with dated markers, and
unusual sale lines), Upload (mapping editor with dropdowns per field,
custom-column table with keep / rename / describe, date and discount options, save
as profile, report with rejected rows), Explore (sales rows, upload history with
undo, column descriptions, saved formats).

Chart rules: one series hue (validated for contrast in light and dark mode), bars
coloured by entity never by rank, "Other" in grey, values at bar tips, hairline
grid, one y-axis per chart, a table view under every chart, amounts in ₹ lakh /
crore (`BIZ_CURRENCY` changes the symbol).

---

## Roadmap

| Week | Scope | Status |
|---|---|---|
| 1 | Data format, reader, mapper + profiles, cleaner, DuckDB loader, samples, synthetic data, CLI, tests | ✅ |
| 2 | FastAPI endpoints, dashboard KPIs, Streamlit upload / dashboard / explore screens | ✅ |
| 3 | ML: forecast, segments, anomalies (Insights page) + review fixes | ✅ |
| 4 | Agent: sql_query, calculator, business_metrics, visualize, streaming chat | ⏳ |
| 5 | RAG + glossary, churn, basket, ML as agent tools | ⏳ |
| 6 | Ollama in Docker, provider switch, 40-question eval set, README polish, demo | ⏳ |
