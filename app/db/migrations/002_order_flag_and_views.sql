-- 002: store whether an order number was generated; compute summaries on read.

-- Files without order numbers get AUTO-<upload>-<row>. Store that fact instead of
-- guessing it from the prefix later (a real invoice series could start with AUTO-).
-- Rows loaded before this migration only have the prefix to go on.
ALTER TABLE sales ADD COLUMN IF NOT EXISTS order_id_generated BOOLEAN DEFAULT false;
UPDATE sales SET order_id_generated = starts_with(order_id, 'AUTO-');

-- products and customers used to be tables rebuilt on every upload and delete.
-- As views they are always current and cost nothing when writing.
DROP TABLE IF EXISTS products;
DROP TABLE IF EXISTS customers;

CREATE VIEW products AS
SELECT
    product,
    mode(category)                                                  AS category,
    min(order_date)                                                 AS first_sold,
    max(order_date)                                                 AS last_sold,
    coalesce(sum(quantity) FILTER (WHERE NOT is_return), 0)         AS units_sold,
    coalesce(-sum(quantity) FILTER (WHERE is_return), 0)            AS units_returned,
    sum(revenue)                                                    AS revenue,
    sum(revenue) / nullif(sum(quantity), 0)                         AS avg_net_price,
    sum(profit)                                                     AS profit,
    100 * sum(profit) / nullif(sum(revenue) FILTER (WHERE profit IS NOT NULL), 0) AS margin_pct
FROM sales
GROUP BY product;

CREATE VIEW customers AS
SELECT
    customer_id,
    min(order_date)                                                 AS first_order,
    max(order_date)                                                 AS last_order,
    count(DISTINCT order_id) FILTER (WHERE NOT is_return)           AS orders,
    sum(revenue)                                                    AS revenue,
    sum(revenue) / nullif(count(DISTINCT order_id) FILTER (WHERE NOT is_return), 0) AS avg_order_value,
    -- measured from the latest sale in the data, so old datasets still make sense
    date_diff('day', max(order_date), (SELECT max(order_date) FROM sales)) AS recency_days
FROM sales
WHERE customer_id IS NOT NULL
GROUP BY customer_id;

-- Example values are computed from a sample when the registry is read.
ALTER TABLE column_registry DROP COLUMN IF EXISTS sample_values;
