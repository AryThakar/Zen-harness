# Worker benchmark history

32 tasks (data 11, knowledge 7, web 7, coder 7; dev + holdout), local model Gemma 4 E4B on the
worker slot, thinking on. Full runs on 2026-10-02, each after the fixes listed under it. Every
failure was read in its trace or tool calls before anything was changed.

| task | r3 | r4 | r5 | r6 | r7 | r8 | r9 | passed |
|---|---|---|---|---|---|---|---|---|
| d_total | ok | ok | ok | ok | ok | ok | ok | 7/7 |
| d_top_category | ok | ok | ok | ok | ok | ok | ok | 7/7 |
| d_size_share | ok | ok | ok | ok | ok | ok | ok | 7/7 |
| d_messy_pizzas | ok | ok | ok | fail | ok | ok | ok | 6/7 |
| d_chart | ok | ok | fail | ok | ok | ok | ok | 6/7 |
| d_model | ok | ok | ok | ok | ok | ok | fail | 6/7 |
| h_aov | ok | ok | ok | ok | ok | ok | ok | 7/7 |
| h_weekday | ok | ok | ok | ok | ok | ok | ok | 7/7 |
| h_corr | ok | ok | ok | ok | ok | ok | ok | 7/7 |
| h_forecast | fail | ok | ok | fail | ok | ok | ok | 5/7 |
| h_ambiguous | ok | ok | ok | ok | ok | ok | ok | 7/7 |
| k_returns | ok | ok | ok | ok | ok | ok | ok | 7/7 |
| k_note | ok | ok | ok | ok | ok | ok | ok | 7/7 |
| k_chat | ok | ok | ok | ok | ok | ok | ok | 7/7 |
| k_missing | ok | ok | ok | ok | ok | ok | ok | 7/7 |
| h_leave | ok | ok | ok | ok | ok | ok | ok | 7/7 |
| h_bulk | ok | ok | ok | ok | fail | ok | ok | 6/7 |
| h_summary | ok | ok | ok | ok | ok | ok | ok | 7/7 |
| w_duckdb_license | ok | ok | ok | ok | ok | ok | ok | 7/7 |
| w_statsforecast | ok | ok | ok | ok | ok | ok | ok | 7/7 |
| w_llama_port | ok | ok | ok | fail | fail | ok | ok | 5/7 |
| w_parquet | ok | ok | ok | ok | ok | ok | ok | 7/7 |
| h_pypi | ok | ok | ok | ok | ok | ok | ok | 7/7 |
| h_duckdb_lang | ok | ok | ok | ok | ok | ok | ok | 7/7 |
| h_hgb | ok | ok | ok | ok | ok | ok | ok | 7/7 |
| c_fib | ok | ok | ok | fail | ok | ok | ok | 6/7 |
| c_words | ok | ok | ok | ok | ok | ok | ok | 7/7 |
| c_fix | ok | ok | ok | ok | ok | ok | ok | 7/7 |
| c_json | ok | fail | ok | ok | ok | ok | ok | 6/7 |
| h_primes | ok | ok | ok | ok | ok | ok | ok | 7/7 |
| h_days | ok | ok | ok | ok | ok | ok | ok | 7/7 |
| h_emails | ok | ok | ok | fail | ok | ok | ok | 6/7 |
| **total** | 31 | 31 | 31 | 27 | 30 | 32 | 31 | 213/224 |

## Every failure and its cause

| run | task | cause | kind | fix |
|---|---|---|---|---|
| r3 | h_forecast | tables used as Python variables, 5 runs with the same NameError; year + quarter columns unsupported | tool | sandbox hint for the error; forecast combines year + quarter itself |
| r4 | c_json | the model never learned that files a script writes are kept | tool | run_python's description says so |
| r5 | d_chart | the chart's automatic period ignored the data's own grain (weeks over monthly data) | tool (bug) | one shared rule in periods.py for charts and forecasts |
| r6 | c_fib, h_emails | the code check took the printed output block for the code | tool (bug) | code handed on is always code that ran |
| r6 | h_forecast | a hint asked the model to copy SQL; it rewrote it wrong | tool | forecast does it itself |
| r6 | d_messy_pizzas | skipped inspect_data, counted rows | model | runner inspects a table before anything is computed on it |
| r6, r7 | w_llama_port | did not use find= on a 113 KB page | model | long pages are focused on the worker's latest search |
| r7 | h_bulk | not captured (passed in every run and trace since) | unknown | - |
| r9 | d_model | listed the column the target was made from as a feature (AUC 1.0) | model | train_model leaves out columns the target was computed from |

Each fix was then run 3 times on the real model on the failed task (all passed), and the whole
suite again. Traces of real runs, event by event: [traces.md](traces.md) (`python -m bench.workers.trace`).
