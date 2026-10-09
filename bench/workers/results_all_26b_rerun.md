# Worker benchmark - all

Model: gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf on llama-server (http://127.0.0.1:8080), worker slot, thinking on.

| role | passed | median seconds | model calls |
|---|---|---|---|
| data | 1/2 | 176.1 | 5.0 avg |
| coder | 2/6 | 134.5 | 6.2 avg |

**Total: 3/8**

| task | result | seconds | tools | detail |
|---|---|---|---|---|
| d_model | **fail** | 94.4 | inspect_data, clean_data, ask_user | no model trained; warnings: the worker returned no answer |
| h_ambiguous | pass | 176.1 | inspect_data, ask_user, clean_data, run_code, query_data | asked about date order: True; also gave a number: False |
| c_fib | pass | 71.9 | run_code | saved (or asking to) with a report showing ('832040',) |
| c_words | pass | 45.8 | read_file, run_code | saved (or asking to) with a report showing ('the', '19') |
| c_json | **fail** | 130.3 | run_code, run_code, run_code, run_code | 4 records + a .json file |
| h_primes | **fail** | 708.5 | read_file, run_code, read_file, ask_user, read_file, read_file, run_code, ask_user, read_file | saved (or asking to) with a report showing ('1229',); warnings: the coder didn't call save: the app saved its last working script, with a report it didn't write |
| h_days | **fail** | 134.5 | read_file, read_file | saved (or asking to) with a report showing ('380',); warnings: the worker returned no answer |
| h_emails | **fail** | 429.4 | run_code, read_file, run_code, run_code, run_code, run_code, run_code, run_code, run_code, run_code | saved (or asking to) with a report showing ('anna@example.com', 'bob.smith@mail.example.org', 'x@test.io'); warnings: the worker returned no answer; the coder didn't call save: the app saved its last working script, with a report it didn't write |

## Answers

**d_model** (fail): 

**h_ambiguous** (pass): The total sales in February 2025 were 636.

**c_fib** (pass): Calculate and print the 30th Fibonacci number (F1=1, F2=1).

**c_words** (pass): Counts the words in file_1 and prints the three most common words with their counts.

**c_json** (fail): 

**h_primes** (fail): A script the coder ran but didn't save itself

**h_days** (fail): 

**h_emails** (fail): A script the coder ran but didn't save itself
