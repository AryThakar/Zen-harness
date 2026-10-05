# Worker benchmark - guide

Local model: Gemma 4 E4B (Q4_K_XL + MTP draft) on llama-server, worker slot, thinking on, RTX 3050 Laptop 4 GB.

| role | passed | median seconds | model calls |
|---|---|---|---|
| data | 10/12 | 47.6 | 4.5 avg |

**Total: 10/12**

| task | result | seconds | tools | detail |
|---|---|---|---|---|
| g_top2 | **fail** | 43.2 | guide, inspect_data, query_data | truth ['Thai Chicken', 'Barbecue Chicken', 'Classic Deluxe', 'Spicy Italian', 'Italian Supreme', 'Hawaiian', 'Four Cheese', 'Mexicana'] |
| g_top2 | pass | 39.5 | inspect_data, query_data | truth ['Thai Chicken', 'Barbecue Chicken', 'Classic Deluxe', 'Spicy Italian', 'Italian Supreme', 'Hawaiian', 'Four Cheese', 'Mexicana'] |
| g_drop | pass | 42.4 | guide, inspect_data, query_data, query_data | truth December -8.09% |
| g_drop | pass | 111.3 | inspect_data, query_data, query_data, query_data | truth December -8.09% |
| g_pair | pass | 46.3 | guide, query_data, query_data | truth ('The Hawaiian Pizza', 'The Thai Chicken Pizza') in 270 orders |
| g_pair | pass | 54.5 | guide, query_data, query_data | truth ('The Hawaiian Pizza', 'The Thai Chicken Pizza') in 270 orders |
| g_large_share | pass | 40.0 | inspect_data, query_data | truth Chicken 52.24%, Classic 33.86%, Supreme 45.27%, Veggie 53.80% |
| g_large_share | pass | 88.2 | inspect_data, query_data | truth Chicken 52.24%, Classic 33.86%, Supreme 45.27%, Veggie 53.80% |
| g_pareto | pass | 47.6 | guide, inspect_data, query_data | truth 22 of 32 |
| g_pareto | **fail** | 100.1 | guide, inspect_data, query_data, query_data | truth 22 of 32 |
| g_median_order | pass | 120.3 | inspect_data, query_data, query_data, run_code, fix_code, run_code, query_data | truth 32.5 (the median line is 16.5) |
| g_median_order | pass | 29.7 | inspect_data, query_data | truth 32.5 (the median line is 16.5) |

## Answers

**g_top2** (fail): The two best-selling pizzas by revenue in each category are: Chicken: The Thai Chicken Pizza ($43,434.25) and The Barbecue Chicken Pizza ($42,768.00); Classic: The Classic Deluxe Pizza ($38,180.50) and The Hawaiian Pizza ($32,273.25); Supreme: The Spicy Italian Pizza ($34,831.25). The full query returned 8 total results.

**g_top2** (pass): The query returned 8 rows, identifying the top two revenue-generating pizzas for the four categories: * Chicken: The Thai Chicken Pizza (43434.25), The Barbecue Chicken Pizza (42768.0) * Classic: The Classic Deluxe Pizza (38180.5), The Hawaiian Pizza (32273.25) * Supreme: The Spicy Italian Pizza (34831.25), The Italian Supreme Pizza (33476.75) * Veggie: The Four Cheese Pizza (32265.7), The Mexicana Pizza (26780.75)

**g_drop** (pass): The month with the largest month-on-month percent drop in revenue was December 2015, with a drop of -8.1%.

**g_drop** (pass): The month with the largest month-on-month percent drop in revenue was December 2015 (2015-12), with a drop of -8.08888655288%. The revenue for December 2015 was 64701.15, down from 70395.35 in the previous month.

**g_pair** (pass): The two pizzas most often ordered together are "The Hawaiian Pizza" and "The Thai Chicken Pizza", and they appear together in 270 orders.

**g_pair** (pass): The two pizzas most often ordered together are "The Hawaiian Pizza" and "The Thai Chicken Pizza", appearing in 270 orders.

**g_large_share** (pass): The share of revenue from large (L) pizzas within each category is: - Classic: 0.338638719473 - Supreme: 0.452737071139 - Veggie: 0.537985755751 - Chicken: 0.522352292651

**g_large_share** (pass): The share of revenue from large (L) pizzas within each category is as follows:  | Category | Share of Revenue from Large (L) Pizzas | | :--- | :--- | | Classic | 33.86% | | Supreme | 45.27% | | Chicken | 52.24% | | Veggie | 53.80% |  Detailed results: - Supreme: Revenue $208197.0, Large Revenue $94258.5, Share 45.2737071139 - Chicken: Revenue $195919.5, Large Revenue $102339.0, Share 52.2352292651 - Veggie: Revenue $193690.45, Large Revenue $104202.7, Share 53.7985739617 - Classic: Revenue $220053.1, Large Revenue $74518.5, Share 33.8638719473

**g_pareto** (pass): 22 pizza types make up approximately 81.20% of the total revenue, generating a cumulative revenue of 664100.3.

**g_pareto** (fail): 21

**g_median_order** (pass): The median order value is 32.5.

**g_median_order** (pass): The median order value is 32.5
