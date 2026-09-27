-- Core tables for ingestion. ML, RAG and chat tables are added with those features.

CREATE SEQUENCE IF NOT EXISTS upload_id_seq START 1;
CREATE SEQUENCE IF NOT EXISTS sale_id_seq START 1;
CREATE SEQUENCE IF NOT EXISTS profile_id_seq START 1;

-- One row per product sold on one bill / invoice / order.
-- Custom columns chosen by users are appended with ALTER TABLE.
CREATE TABLE IF NOT EXISTS sales (
    sale_id      BIGINT PRIMARY KEY DEFAULT nextval('sale_id_seq'),
    upload_id    INTEGER NOT NULL,
    row_hash     VARCHAR NOT NULL,      -- de-duplicates overlapping uploads
    order_id     VARCHAR NOT NULL,      -- generated as AUTO-<upload>-<row> when missing
    order_date   TIMESTAMP NOT NULL,
    product      VARCHAR NOT NULL,
    quantity     DOUBLE NOT NULL,
    unit_price   DOUBLE NOT NULL,
    customer_id  VARCHAR,
    unit_cost    DOUBLE,
    category     VARCHAR,
    discount     DOUBLE NOT NULL DEFAULT 0,
    location     VARCHAR,
    channel      VARCHAR,
    is_return    BOOLEAN NOT NULL,
    revenue      DOUBLE NOT NULL,
    cost         DOUBLE,
    profit       DOUBLE,
    year         INTEGER NOT NULL,
    month        INTEGER NOT NULL,
    week         INTEGER NOT NULL,
    weekday      VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS uploads (
    upload_id       INTEGER PRIMARY KEY,
    filename        VARCHAR NOT NULL,
    profile_name    VARCHAR,
    rows_total      INTEGER NOT NULL,
    rows_loaded     INTEGER NOT NULL,
    rows_rejected   INTEGER NOT NULL,
    rows_duplicate  INTEGER NOT NULL,
    mapping         JSON NOT NULL,
    warnings        JSON NOT NULL,
    created_at      TIMESTAMP NOT NULL DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS rejected_rows (
    upload_id   INTEGER NOT NULL,
    row_number  INTEGER NOT NULL,       -- row in the original file (header = row 1 of the table)
    reasons     VARCHAR NOT NULL,
    raw         JSON NOT NULL
);

-- Saved column mappings, so the next export from the same system loads automatically.
CREATE TABLE IF NOT EXISTS format_profiles (
    profile_id  INTEGER PRIMARY KEY DEFAULT nextval('profile_id_seq'),
    name        VARCHAR NOT NULL UNIQUE,
    mapping     JSON NOT NULL,
    builtin     BOOLEAN NOT NULL DEFAULT false,
    created_at  TIMESTAMP NOT NULL DEFAULT current_timestamp
);

-- Every column of the sales table with its meaning. The AI agent reads this.
CREATE TABLE IF NOT EXISTS column_registry (
    column_name    VARCHAR PRIMARY KEY,
    data_type      VARCHAR NOT NULL,
    kind           VARCHAR NOT NULL,    -- required / optional / derived / custom
    description    VARCHAR,
    sample_values  JSON
);
