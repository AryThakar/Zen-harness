# Worker benchmark - all

Local model: Gemma 4 E4B (Q4_K_XL + MTP draft) on llama-server, worker slot, thinking on, RTX 3050 Laptop 4 GB.

| role | passed | median seconds | model calls |
|---|---|---|---|
| coder | 8/8 | 36.2 | 3.4 avg |

**Total: 8/8**

| task | result | seconds | tools | detail |
|---|---|---|---|---|
| c_fib | pass | 26.4 | run_code | saved (or asking to) with a report showing ('832040',) |
| c_words | pass | 36.2 | run_code | saved (or asking to) with a report showing ('the', '19') |
| c_fix | pass | 72.7 | read_file, fix_code, fix_code, run_code | saved (or asking to) with a report showing ('5.5',) |
| c_json | pass | 18.5 | guide, run_code | 4 records + a .json file |
| h_primes | pass | 22.8 | guide, run_code | saved (or asking to) with a report showing ('1229',) |
| h_days | pass | 39.7 | guide, run_code | saved (or asking to) with a report showing ('380',) |
| h_emails | pass | 40.1 | guide, run_code | saved (or asking to) with a report showing ('anna@example.com', 'bob.smith@mail.example.org', 'x@test.io') |
| c_edit | pass | 23.3 | guide, read_file, fix_code | stats.py changed in place, asking the user, report prints 5.0 and 9 |

## Answers

**c_fib** (pass): 

**c_words** (pass): 

**c_fix** (pass): 

**c_json** (pass): 

**h_primes** (pass): 

**h_days** (pass): 

**h_emails** (pass): 

**c_edit** (pass): 
