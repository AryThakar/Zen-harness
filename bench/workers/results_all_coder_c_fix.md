# Worker benchmark - all

Local model: Gemma 4 E4B (Q4_K_XL + MTP draft) on llama-server, worker slot, thinking on, RTX 3050 Laptop 4 GB.

| role | passed | median seconds | model calls |
|---|---|---|---|
| coder | 1/1 | 13.4 | 4.0 avg |

**Total: 1/1**

| task | result | seconds | tools | detail |
|---|---|---|---|---|
| c_fix | pass | 13.4 | read_file, fix_code, fix_code | saved (or asking to) with a report showing ('5.5',) |

## Answers

**c_fix** (pass): 
