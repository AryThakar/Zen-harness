-- 005: business documents for search (RAG), churn scores, basket rules.
-- Ids come from nextval() in the inserts (see 004).

CREATE SEQUENCE IF NOT EXISTS doc_id_seq START 1;
CREATE SEQUENCE IF NOT EXISTS chunk_id_seq START 1;

-- One row per uploaded document (policy, catalogue, contract, notes...).
-- Only the extracted text is kept, split into chunks.
CREATE TABLE IF NOT EXISTS documents (
    doc_id        INTEGER PRIMARY KEY,
    filename      VARCHAR NOT NULL,
    kind          VARCHAR NOT NULL,     -- pdf / docx / txt / md
    title         VARCHAR,
    pages         INTEGER,              -- PDFs only
    words         INTEGER NOT NULL,
    chunks        INTEGER NOT NULL,
    content_hash  VARCHAR NOT NULL,     -- sha256 of the file, to spot re-uploads
    uploaded_at   TIMESTAMP NOT NULL DEFAULT current_timestamp
);

-- Passages of ~250 words that search returns, with where they came from.
CREATE TABLE IF NOT EXISTS doc_chunks (
    chunk_id      BIGINT PRIMARY KEY,
    doc_id        INTEGER NOT NULL,
    ord           INTEGER NOT NULL,     -- position in the document
    page          INTEGER,              -- PDF page (1-based)
    section       VARCHAR,              -- nearest heading, when the document has them
    text          VARCHAR NOT NULL,
    embed_model   VARCHAR,              -- NULL until embedded; re-embedded when the model changes
    embedding     BLOB                  -- float32 vector, L2-normalised
);

-- Chance each active repeat customer buys nothing in the next horizon.
CREATE TABLE IF NOT EXISTS churn_scores (
    run_id            INTEGER NOT NULL,
    customer_id       VARCHAR NOT NULL,
    probability       DOUBLE NOT NULL,  -- of no purchase in the next horizon_days
    risk              VARCHAR NOT NULL, -- high / medium / low
    recency_days      INTEGER NOT NULL, -- days since last purchase, before the latest sale in the data
    typical_gap_days  DOUBLE NOT NULL,  -- median days between their purchases
    purchases         INTEGER NOT NULL, -- distinct purchase days
    recent_revenue    DOUBLE NOT NULL,  -- spend in the last horizon_days
    revenue           DOUBLE NOT NULL,  -- lifetime net revenue
    value_at_risk     DOUBLE NOT NULL,  -- probability x their usual spend per horizon
    signals           VARCHAR NOT NULL  -- plain-language warning signs
);

-- "Customers who buy X also buy Y": product and category pairs bought together
-- more often than chance.
CREATE TABLE IF NOT EXISTS basket_rules (
    run_id             INTEGER NOT NULL,
    level              VARCHAR NOT NULL, -- product / category
    antecedent         VARCHAR NOT NULL, -- X
    consequent         VARCHAR NOT NULL, -- Y
    orders_together    INTEGER NOT NULL,
    antecedent_orders  INTEGER NOT NULL,
    expected_together  DOUBLE NOT NULL,  -- if the two were bought independently
    support_pct        DOUBLE NOT NULL,  -- share of all orders with both
    confidence_pct     DOUBLE NOT NULL,  -- share of X's orders that also have Y
    lift               DOUBLE NOT NULL   -- confidence / Y's overall share; > 1 = bought together more than chance
);

CREATE VIEW latest_churn_scores AS
SELECT * FROM churn_scores
WHERE run_id = (SELECT max(run_id) FROM ml_runs WHERE task = 'churn' AND status = 'succeeded');

CREATE VIEW latest_basket_rules AS
SELECT * FROM basket_rules
WHERE run_id = (SELECT max(run_id) FROM ml_runs WHERE task = 'basket' AND status = 'succeeded');
