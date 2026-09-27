# AI Business Analyst

Upload your sales export, ask questions in plain English, and get answers,
forecasts and charts - running on your own machine.

**Stack:** Python · FastAPI · DuckDB · pandas · scikit-learn · LangGraph · Ollama (local LLM) · RAG · Plotly

> Status: **week 1 of 6 done** - the data foundation: upload → map → clean → DuckDB.
> The design for everything else is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## What works today

* **Reads real, messy exports** - CSV or Excel, title lines above the header,
  unknown encodings, `;` delimiters, `₹1,23,456.50` amounts, day- or month-first
  dates, `Total` footer rows.
* **One standard format** - 4 required columns (`order_date`, `product`, `quantity`,
  `unit_price`), 7 optional ones that unlock features (customer → churn, cost →
  profit, ...), plus any custom columns the business wants to keep.
* **Maps columns automatically** - built-in profiles (Shopify, Superstore, UCI Online
  Retail), 150+ aliases and fuzzy matching; save your own profile once and the next
  export loads by itself.
* **Never guesses silently** - every rejected row gets a reason and its file row
  number; overlapping or repeated uploads are de-duplicated.
* **Realistic synthetic data** - a generator with planted trends, seasonality,
  customer churn, basket patterns and anomalies for the ML features to find.

```text
$ python -m app.ingestion.cli data/samples/retail_pos_messy.csv --yes

retail_pos_messy.csv: 21 rows, header on row 5

Column mapping
  order_date   <- Bill Date
  product      <- Item Name
  quantity     <- Qty
  unit_price   <- Rate (₹)
  order_id     <- Bill No
  customer_id  <- Customer
  category     <- Category
  discount     <- Disc %

Custom columns
  payment_mode         VARCHAR   e.g. UPI, Cash, Card

Add these columns to unlock more:
  unit_cost    -> profit & margin
  location     -> location comparison
  channel      -> channel comparison

Upload #1: 16 loaded · 5 rejected (bad order_date 1, quantity is 0 1, missing product 1, bad unit_price 1, summary row 1)
  rejected rows are in table rejected_rows (upload_id = 1)
```

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# load a sample export into data/business.duckdb
python -m app.ingestion.cli data/samples/shopify_orders.csv

# generate 18 months of realistic data and load it
python -m app.datagen
python -m app.ingestion.cli data/generated/synthetic_sales_canonical.csv --yes

# run the tests
pytest
```

Explore the result with DuckDB:

```python
import duckdb
con = duckdb.connect("data/business.duckdb")
con.sql("SELECT category, round(sum(revenue)) AS revenue, round(sum(profit)) AS profit "
        "FROM sales GROUP BY 1 ORDER BY 2 DESC").show()
```

## Project layout

```
app/
  config.py            settings (BIZ_DB_PATH, BIZ_DEFAULT_DATE_ORDER)
  datagen.py           synthetic sales data generator
  db/                  DuckDB connection + schema.sql
  ingestion/
    fields.py          the standard format: required / optional columns and aliases
    reader.py          CSV/Excel reading, header detection
    parsing.py         strict number / date / type parsers
    mapper.py          column matching and mapping validation
    profiles.py        built-in and saved format profiles
    cleaner.py         derivations, validation with reasons
    loader.py          de-duplication, transactional load, summary tables
    pipeline.py        preview_file() / ingest_file()
    cli.py             command-line upload
data/samples/          small example exports (see its README)
docs/ARCHITECTURE.md   full design and roadmap
tests/                 pytest suite
```

## Roadmap

| Week | Scope | |
|---|---|---|
| 1 | Data format, cleaning pipeline, DuckDB, samples, synthetic data | ✅ |
| 2 | FastAPI endpoints, dashboard, Streamlit data view | |
| 3 | ML: sales forecast, customer segments, anomaly detection | |
| 4 | LangGraph agent with SQL, calculator, metrics and chart tools | |
| 5 | RAG over business documents, churn prediction, basket analysis | |
| 6 | Local LLM with Ollama in Docker, evaluation set, demo | |
