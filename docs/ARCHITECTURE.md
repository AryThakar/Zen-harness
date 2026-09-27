# Architecture

**Product:** an AI business analyst for small and medium businesses. A business
uploads its sales export; a copilot answers questions, forecasts, finds customers
at risk of leaving and draws charts.

Status legend: ✅ built · ⏳ planned (see [Roadmap](#roadmap)).

```
Upload CSV/Excel ──► reader ─► mapper ─► user confirms ─► cleaner ─► loader ─► DuckDB
                                                                              ▲   ▲   ▲
                    LangGraph agent (Claude now, local model via Ollama later)│   │   │
                    tools: describe_data · run_sql · business_summary ────────┘   │   │
                           calculate · business_metric · make_chart               │   │
                           forecast · customer_segments · unusual_days ───────────┘   │
                           churn_risk · bought_together                               │
                           search_docs ──► RAG: documents → passages → BM25 + vectors ┘
                              │
                    FastAPI (/uploads, /dashboard, /sales, /ml, /documents, /chat SSE)
                              │
                    Streamlit UI: dashboard, insights, Ask AI, upload, documents, explore
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

One file, `data/business.duckdb` (`BIZ_DB_PATH`). Schema: the numbered files in
[`app/db/migrations/`](../app/db/migrations/). Chat history lives separately in
`data/chat.sqlite` (`BIZ_CHAT_DB_PATH`), written by the LangGraph checkpointer.

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
| `churn_scores`, `basket_rules` | churn probability, warning signs and value at risk per customer; product and category pairs with support, confidence, lift (+ `latest_*` views) |
| `documents`, `doc_chunks` | uploaded documents (filename, kind, pages, words, content hash) and their passages with page, section and a float32 embedding (BLOB) tagged with the model that made it |

ML outputs are written back to DuckDB so the agent can query them with SQL.
Concurrency: one connection per process, a cursor per request, writes serialized by a lock.

**Migrations:** the schema is built by numbered SQL files, each applied once and
recorded, so an existing database file upgrades itself when the app changes.
`products` and `customers` are views, so uploads and deletes don't rebuild anything.

**Crash safety:** DuckDB 1.5 can't replay an `ALTER TABLE` from its write-ahead log
when the table has a `DEFAULT nextval(...)` column - after a crash the file won't
open. Custom columns are added with `ALTER TABLE`, so ids now come from `nextval()`
in the inserts (migration 004 drops the defaults), and the database checkpoints
after applying migrations. A test kills a process right after such an upload and
reopens the file.

**AI access:** the connection runs with `enable_external_access = false`, so no SQL
- including SQL written by the assistant - can read files, attach databases or load
extensions (uploads are parsed in Python, so the app never needs it).

---

## 4. ML ✅

Code: [`app/ml/`](../app/ml/). Each task is a function that reads with a cursor and
returns a `TaskResult` (metrics, a plain-language message, result rows). The runner
records every run in `ml_runs` (running → succeeded / failed), computes outside the
write lock, then saves results in one transaction. The same functions back the
AI assistant's tools.

| Task | Needs | Method | Output tables |
|---|---|---|---|
| **Forecast** | ≥ 3× the horizon and ≥ 8 weeks of history | statsforecast: exponential smoothing (AutoETS) and Theta vs a "repeat the previous period" baseline (seasonal naive), walk-forward backtest on up to 3 past periods, best by WAPE; the baseline is used if nothing beats it. Daily (weekly seasonality) or weekly; all sales or one category / product / location / channel | `forecast_points` (predicted + 80% range) |
| **Customer segments** | ≥ 50 customers | RFM (recency, orders, net revenue; log scale for the skewed two) → StandardScaler → KMeans for k = 3-6; the most groups whose silhouette is within 10% of the best (silhouette alone nearly always picks 3, too coarse to act on). Groups named from their median customer's percentiles: Champions, Loyal, At risk, New, Occasional, Lost - each with a meaning and a suggested action | `customer_segments` |
| **Unusual days** | ≥ 28 days | For orders and for revenue: expected = weekday profile × rolling 29-day median level; robust z-score (median/MAD) of the log difference; flag \|z\| ≥ 3.5. Both signals because revenue is noisy (one laptop doubles a day) while order counts are steady - a flash sale shows in orders first | `anomaly_days` |
| **Unusual lines** | ≥ 200 lines | IsolationForest on price, quantity and discount relative to each product's median, reported only when something is actually off (price ×1.5, quantity ×3, discount > 30%) with a plain reason | `anomaly_lines` |
| **Churn risk** | customer ids, ≥ 3× the horizon of history, ≥ 100 regular customers | Who buys nothing in the next 30/60/90 days, among *regular* customers (≥ 2 purchase days, usual gap ≤ horizon, bought within it - for them a quiet period breaks their rhythm; lapsed, infrequent and one-time buyers are counted, not scored). Snapshots every horizon back in time: features from data up to the snapshot only (days since last purchase, usual gap, overdue ratio, purchases and spend in the last vs previous period, basket size, return rate), label = no purchase in the next horizon. Logistic regression and gradient boosting learn on older snapshots and are tested on the latest one; a model is used only if its AUC beats "days since last purchase" by ≥ 0.02. Refit on all snapshots to score today. Each customer gets factual warning signs and value at risk (chance × usual spend per period) | `churn_scores` |
| **Bought together** | real order numbers, ≥ 100 orders with 2+ products | Pair counts in SQL (orders with > 50 products left out); support, confidence, lift over all orders; kept when bought together ≥ max(5, 0.1% of orders) times, lift ≥ 1.2 and ≥ 3 standard deviations above the independent expectation - so a coincidence among a handful of orders isn't a "pattern". Products and categories | `basket_rules` |

**Honesty rules:** every model reports its backtest error next to the baseline's and
how often its range held; too little data gives a message saying what's missing, not
a weak result; a failed run stores why.

**Checked on the synthetic data:** ETS was 40% more accurate than the baseline (WAPE
26% vs 43%) and its 80% range held 83% of the time; the planted flash sale (orders
3.9× normal) and outage (orders -81%) are both found; planted price and quantity
typos are flagged; Champions are 18% of customers bringing 55% of revenue. Churn (90
days): logistic regression AUC 0.72 vs 0.61 for days-since-last-purchase, its top 20%
held 32% of the customers who then stopped (random: 20%); at 30 days the data supports
only AUC 0.61, and the screen says the scores are rough. Bought together: every planted
pair is found at about its planted rate (49% of Smartphone X1 orders include a case,
planted 55%; laptop + mouse 41%, planted 50%) and no unplanted pair passes.

**Runs:** `POST /ml/{task}` returns 202 and runs in the background; the UI polls
`GET /ml/runs/{id}`. An identical run that is still going is reused (double clicks),
and runs cut off by a restart are marked failed at startup. `latest_*` views hold the
newest results of each task for SQL.

## 5. Document search (RAG) ✅

Code: [`app/rag/`](../app/rag/). The shop's own documents - policies, price lists,
supplier terms, notes, incident logs - answer the questions numbers can't: "can a
customer return an opened phone?", "why was 15 April so slow?".

| Step | Module | What happens |
|---|---|---|
| Extract | `extract` | PDF (pypdf, per page), Word .docx (python-docx: paragraphs and tables in order, headings tracked), .txt / .md (UTF-8 or Windows-1252; `#` headings). Wrapped PDF lines rejoined. A PDF with no text says it may be a scan (no OCR); password-protected or broken files get a plain message |
| Chunk | `chunking` | ~250-word passages of whole paragraphs (long ones split at sentence ends), never across a page or heading, 40 words of overlap; a lone title or heading is merged into the next passage |
| Embed | `embed` | fastembed runs `BAAI/bge-small-en-v1.5` (384 dims, ~65 MB ONNX, CPU, downloaded once to `data/models`); the heading is embedded with its passage. Swappable: `BIZ_EMBED_MODEL`, and an Ollama embedder in week 6 |
| Store | `library` | document + passages in one transaction; the embedding is a float32 BLOB tagged with its model. Same file again → 409; same filename, new content → replaces the old version |
| Search | `search`, `library` | BM25 over the passages (plural-aware tokens) and cosine similarity over the vectors, merged by reciprocal rank fusion (1 / (60 + rank) from each list). Top 5 with file, page and heading. The index (postings + vector matrix) lives in memory and is rebuilt when the passages change |

* **Why hybrid:** keywords catch exact terms (product codes, names, "7 days");
  embeddings catch the same meaning in other words. On the sample documents the
  embedding model finds "who is our contact person at the electronics supplier?" on
  the right page, which keywords alone miss.
* **Degrades, doesn't fail:** if the model can't be loaded (offline on first use),
  documents are still stored and searched by keywords; `POST /documents/reindex` adds
  the vectors later, and passages from an old model are re-embedded the same way.
* **No vector database:** a numpy dot product over a few thousand 384-dim vectors takes
  microseconds; DuckDB stores them with everything else.
* **Sample documents** for the demo shop, one per format, with an operations log dated
  to the demo data's planted events ([`samples.py`](../app/rag/samples.py)).

## 6. Model providers ✅ (Claude) · ⏳ (local)

Code: [`app/agent/providers/`](../app/agent/providers/). The graph talks to a small
`Provider` interface: `complete(system, messages, tools) -> ModelTurn` (answer text,
tool calls, stop reason) with a callback for streamed text. Conversations are stored
in a provider-neutral format (user / assistant / tool messages); each adapter
converts to and from its API.

* **Claude** (now, `BIZ_LLM_PROVIDER=anthropic`, needs `ANTHROPIC_API_KEY`): the
  official `anthropic` SDK, streaming, model `claude-opus-5` by default
  (`BIZ_LLM_MODEL`), prompt caching, server-side fallback to another model if the
  first is overloaded. The assistant's raw content blocks are stored and replayed
  unchanged on the next turn (the API requires that for its internal blocks);
  tool calls that never got a result are dropped. Tool inputs stream as they are
  written, so each one is validated against its schema before it runs; a refused
  or cut-off turn never runs tools.
* **Ollama** (week 6): the same interface over a local model; nothing else changes.
* **Scripted** (tests and demos): replays a list of turns, so the graph, tools,
  API and UI are tested end to end without a model.

Why not LangChain's `init_chat_model`: the adapters need to keep model-internal
blocks byte-for-byte and control streaming, validation and stop reasons per
provider; a thin interface of our own is less code than working around a generic one.

## 7. Agent (LangGraph) ✅

Code: [`app/agent/graph.py`](../app/agent/graph.py).

```
START → agent (model turn: system prompt + conversation) → tool calls? ─ no ─► END
            ▲                                                  │ yes
            └──────────── tools (validate → run → results) ◄───┘
```

* **System prompt** ([`prompts.py`](../app/agent/prompts.py)), rebuilt every turn
  with a live **schema card** ([`context.py`](../app/agent/context.py)): the tables,
  every sales column with its meaning and examples (custom columns included), the
  data's date range and which optional columns are missing.
* **Rules** it gives the model: every number comes from a tool (arithmetic through
  `calculate` / `business_metric`); headline KPIs through `business_summary`;
  at most one chart; name the column to add when a question needs one the data
  lacks; "last month" means relative to the latest sale; answer first, briefly.
* **Limits:** at most 8 tool rounds per question; after that tools are switched off
  (`tool_choice: none`) and the model answers with what it has.
* **Memory:** LangGraph checkpointer (SQLite) keyed by thread id, so follow-up
  questions see the earlier exchanges.
* **Streaming:** LangGraph's custom stream carries `text`, `restart` (discard the
  current turn's text after a retry), `tool_start` and `tool_end` (with a short
  summary and any chart or table) events to the API.

## 8. Tools ✅

Code: [`app/agent/tools.py`](../app/agent/tools.py). Each tool has a pydantic input
model (unknown fields rejected); bad input goes back to the model as an error
result to fix, never as a crash. Results are text for the model plus an optional
artifact (chart or table data) for the UI.

| Tool | Input → output |
|---|---|
| `describe_data` | optional table → tables and meanings, or one table's columns and sample rows |
| `run_sql` | one SELECT → up to 200 rows (the model sees 40) + total count; table artifact |
| `business_summary` | optional start/end → the dashboard KPIs with changes vs the previous period |
| `calculate` | arithmetic expression → exact result (simpleeval, never `eval`) |
| `business_metric` | growth %, margin %, markup %, CAGR, AOV, break-even units, ROI, CLV, price after discount → value with its formula |
| `make_chart` | line or bar, SQL, x, y, unit, title → chart artifact drawn by the UI in house style |
| `forecast` | horizon, day/week, optional slice → forecast with 80% range and backtest vs baseline |
| `customer_segments` | → segments with sizes, revenue share, meaning and action |
| `unusual_days` | → unusual days (most unusual first, with % vs normal) and sale lines with the reason |
| `churn_risk` | horizon, limit → customers likely to stop buying with chance, warning signs, value at risk, model quality |
| `bought_together` | optional product or category → companions with share of orders and lift; close matches for a misspelt name |
| `search_docs` | query → top 5 passages with file, page and section to cite |

The ML tools run the same code as the Insights page and store their runs the same way.

**Safety layers for AI-written SQL** ([`sql_guard.py`](../app/agent/sql_guard.py)):

1. Parsed with sqlglot: exactly one statement, and it must be a query; any
   insert/update/delete/DDL/COPY/ATTACH/PRAGMA/SET node anywhere is rejected.
2. Tables must be on an allowlist (sales data and ML result tables; not
   `schema_migrations`, `format_profiles` or the raw rejected rows); schema-qualified names,
   string "tables" (`FROM 'file.csv'`) and table functions other than `unnest`,
   `generate_series` and `range` are rejected; functions like `read_*`, `glob`,
   `getenv` are blocked by name.
3. The SQL that runs is sqlglot's regenerated version of the checked tree.
4. At most 200 rows are fetched; a query running over 10 s is interrupted.
5. Underneath it all, the database itself has external access switched off.

Data values and document passages reach the model only as tool results, labelled as
information rather than instructions, and the system prompt says to ignore any
instructions inside them. Every tool is read-only and none reaches the network, so text
planted in the data or a document ("ignore your instructions") can at worst produce a
wrong answer, not change or leak data.

## 9. API (FastAPI) ✅

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
| `GET /chat/status` | which model answers and whether it is set up |
| `POST /chat` | `{message, thread_id?}` → server-sent events: `thread`, `text`, `restart`, `tool_start`, `tool_end`, `done`, `error` |
| `GET /chat/threads/{id}` | a conversation as question / steps / answer, for redisplay |
| `POST /ml/churn`, `/ml/basket`; `GET /ml/churn`, `/ml/churn/customers`, `/ml/basket` | churn scores by risk band; bought-together pairs (optionally for one product) |
| `GET /documents`, `POST /documents`, `POST /documents/samples` | library status and list; add a PDF/DOCX/TXT/MD (409 duplicate, 422 unreadable, 413 too large); add the sample documents |
| `GET /documents/search`, `GET /documents/{id}/passages`, `DELETE /documents/{id}`, `POST /documents/reindex` | hybrid search; a document's passages; delete; embed passages that have no vector for the current model |

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
unusual sale lines; churn risk with ranking quality vs the simple rule and the customers
most at stake with their warning signs; bought together, per product), Upload (mapping editor with dropdowns per field,
custom-column table with keep / rename / describe, date and discount options, save
as profile, report with rejected rows), Documents (upload, sample documents, a
search box that shows how each passage matched, passages per document, delete), Explore (sales rows, upload history with
undo, column descriptions, saved formats), Ask AI (streaming answers; the steps it
took - queries with their SQL and result tables, calculations, models - in a
collapsible box above each answer; its charts drawn in house style; suggested
first questions; setup help when no model is configured).

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
| 4 | Agent: provider adapters (Claude), SQL guard, 9 tools, LangGraph graph with memory, streaming chat API, Ask AI page | ✅ |
| 5 | Document search (hybrid BM25 + embeddings), churn prediction, bought together, 3 agent tools, Documents page | ✅ |
| 6 | Ollama provider in Docker, 40-question eval set, README polish, demo | ⏳ |
