# Worker benchmark - dev

Local model: Gemma 4 E4B (Q4_K_XL + MTP draft) on llama-server, worker slot, thinking on, RTX 3050 Laptop 4 GB.

| role | passed | median seconds | model calls |
|---|---|---|---|
| data | 2/2 | 28.9 | 5.5 avg |

**Total: 2/2**

| task | result | seconds | tools | detail |
|---|---|---|---|---|
| d_messy_pizzas | pass | 28.9 | guide, inspect_data, clean_data, query_data | truth 49574 (a Total row doubles it) |
| d_messy_pizzas | pass | 23.7 | guide, inspect_data, clean_data, query_data | truth 49574 (a Total row doubles it) |

## Answers

**d_messy_pizzas** (pass): 49,574

**d_messy_pizzas** (pass): 49,574
