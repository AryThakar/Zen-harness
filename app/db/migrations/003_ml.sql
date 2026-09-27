-- 003: machine-learning runs and their results.

CREATE SEQUENCE IF NOT EXISTS ml_run_id_seq START 1;

-- One row per model run, started from the UI, the API or (later) the AI agent.
CREATE TABLE IF NOT EXISTS ml_runs (
    run_id       INTEGER PRIMARY KEY DEFAULT nextval('ml_run_id_seq'),
    task         VARCHAR NOT NULL,      -- forecast / segments / anomalies
    status       VARCHAR NOT NULL,      -- running / succeeded / failed
    params       JSON NOT NULL,
    metrics      JSON,
    message      VARCHAR,               -- the result, or why it failed, in plain words
    started_at   TIMESTAMP NOT NULL DEFAULT current_timestamp,
    finished_at  TIMESTAMP
);

CREATE TABLE IF NOT EXISTS forecast_points (
    run_id     INTEGER NOT NULL,
    bucket     DATE NOT NULL,           -- day, or Monday of the week
    predicted  DOUBLE NOT NULL,
    lower      DOUBLE,                  -- 80% prediction range
    upper      DOUBLE
);

CREATE TABLE IF NOT EXISTS customer_segments (
    run_id        INTEGER NOT NULL,
    customer_id   VARCHAR NOT NULL,
    recency_days  INTEGER NOT NULL,     -- days since last purchase, before the latest sale in the data
    orders        INTEGER NOT NULL,
    revenue       DOUBLE NOT NULL,
    cluster       INTEGER NOT NULL,
    segment       VARCHAR NOT NULL      -- Champions / Loyal / New / At risk / Lost / Occasional
);

CREATE TABLE IF NOT EXISTS anomaly_days (
    run_id            INTEGER NOT NULL,
    day               DATE NOT NULL,
    revenue           DOUBLE NOT NULL,
    expected_revenue  DOUBLE NOT NULL,
    orders            INTEGER NOT NULL,
    expected_orders   DOUBLE NOT NULL,
    signal            VARCHAR NOT NULL, -- orders / revenue / both: which one was unusual
    score             DOUBLE NOT NULL,  -- the larger robust z-score; |score| >= 3.5 is flagged
    direction         VARCHAR NOT NULL  -- spike / drop
);

CREATE TABLE IF NOT EXISTS anomaly_lines (
    run_id   INTEGER NOT NULL,
    sale_id  BIGINT NOT NULL,
    score    DOUBLE NOT NULL,           -- higher = more unusual
    reason   VARCHAR NOT NULL
);

-- The latest successful result of each task: what the UI shows and the AI queries.
CREATE VIEW latest_forecast AS
SELECT * FROM forecast_points
WHERE run_id = (SELECT max(run_id) FROM ml_runs WHERE task = 'forecast' AND status = 'succeeded');

CREATE VIEW latest_customer_segments AS
SELECT * FROM customer_segments
WHERE run_id = (SELECT max(run_id) FROM ml_runs WHERE task = 'segments' AND status = 'succeeded');

CREATE VIEW latest_anomaly_days AS
SELECT * FROM anomaly_days
WHERE run_id = (SELECT max(run_id) FROM ml_runs WHERE task = 'anomalies' AND status = 'succeeded');
