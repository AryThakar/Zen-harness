# Worker benchmark - all

Local model: Gemma 4 E4B (Q4_K_XL + MTP draft) on llama-server, worker slot, thinking on, RTX 3050 Laptop 4 GB.

| role | passed | median seconds | model calls |
|---|---|---|---|
| data | 1/1 | 15.9 | 3.0 avg |

**Total: 1/1**

| task | result | seconds | tools | detail |
|---|---|---|---|---|
| h_corr | pass | 15.9 | inspect_data, stats_test | truth rho 0.656 |

## Answers

**h_corr** (pass): The Spearman rank correlation (rho) between poverty and murder is 0.6559. The p-value is 1.8e-07. The test concluded: "The correlation is larger than chance would explain (large effect)".
