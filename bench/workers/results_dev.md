# Worker benchmark - dev

Local model: Gemma 4 E4B (Q4_K_XL + MTP draft) on llama-server, worker slot, thinking on, RTX 3050 Laptop 4 GB.

| role | passed | median seconds | model calls |
|---|---|---|---|
| data | 6/6 | 19.8 | 4.5 avg |
| knowledge | 3/4 | 4.2 | 2.2 avg |
| web | 3/4 | 11.4 | 3.2 avg |
| coder | 4/4 | 20.8 | 3.5 avg |

**Total: 16/18**

| task | result | seconds | tools | detail |
|---|---|---|---|---|
| d_total | pass | 10.8 | inspect_data, query_data | truth 817860.05 |
| d_top_category | pass | 12.1 | inspect_data, query_data | truth Classic 220053.10 |
| d_size_share | pass | 20.5 | inspect_data, query_data, query_data, calculate | truth 45.89% |
| d_messy_pizzas | pass | 19.8 | inspect_data, query_data, clean_data, query_data | truth 49574 (a Total row doubles it) |
| d_chart | pass | 18.2 | inspect_data, query_data, make_chart | line charts: 1, points: 12 |
| d_model | pass | 38.7 | inspect_data, clean_data, query_data, query_data, train_model, train_model | features include affairs: False; score 0.723; answer cites it: True |
| k_returns | pass | 4.2 | search_files | expects ('14',); sources ['store_policy.md - "Returns"'] |
| k_note | pass | 3.2 | search_files | expects ('net_sales',); sources ["note 'Revenue column' (note_1)"] |
| k_chat | **fail** | 7.2 | recall | expects ('6 months',); sources []; warnings: no sources given; list the sources (exactly as the tools gave them) your answer comes from |
| k_missing | pass | 3.0 | search_files | should say it isn't covered; sources [] |
| w_duckdb_license | pass | 11.4 | web_search, read_webpage | expects ('MIT',); sources ['https://duckdb.org/faq'] |
| w_statsforecast | pass | 9.5 | web_search, read_webpage | expects ('Nixtla',); sources ['https://github.com/Nixtla/statsforecast'] |
| w_llama_port | **fail** | 27.9 | web_search, read_webpage, read_webpage | expects ('8080',); sources ['https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md', 'https://www.simplified.guide/llama-cpp/server-set-host-port'] |
| w_parquet | pass | 10.9 | web_search, read_webpage | expects ('pyarrow', 'fastparquet'); sources ['https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.read_parquet.html'] |
| c_fib | pass | 20.8 | run_python, run_python | expects ('832040',) |
| c_words | pass | 26.1 | read_file, run_python, run_python | expects ('the', '19') |
| c_fix | pass | 12.6 | read_file, run_python | expects ('5.5',) |
| c_json | pass | 15.3 | run_python | 4 records + a .json file |

## Answers

**d_total** (pass): The total revenue (quantity times price) in the data was 817860.05.

**d_top_category** (pass): The pizza category that brought in the most revenue is **Classic**, with a total revenue of **220053.1**.

**d_size_share** (pass): The share of all revenue that comes from large (L) pizzas is 45.8903329488%.

**d_messy_pizzas** (pass): 49574 pizzas were sold in total.

**d_chart** (pass): The monthly revenue is shown in the line chart below.  {"status": "ok", "chart": "line", "data": "data_2", "title": "sum of revenue by month", "points": 12, "grouping": "by month", "first": ["2015-01-01", 69793.3], "last": ["2015-12-01", 64701.15], "highest": ["2015-07-01", 72557.9], "lowest": ["2015-10-01", 64027.6]}

**d_model** (pass): The model trained to predict whether a person had an affair (defined as `affairs > 0`) based on their other answers (`rate_marriage`, `age`, `yrs_married`, `children`, `religious`, `educ`, `occupation`, `occupation_husb`) is **model_2**.  This model achieved an **AUC score of 0.723** in 5-fold cross-validation. This is considered better than the baseline of 0.5 (which is equivalent to random guessing).  The most predictive features identified by the model are: *   `rate_marriage` (Share: 0.411) *   `yrs_married` (Share: 0.357) *   `age` (Share: 0.124) *   `religious` (Share: 0.084)

**k_returns** (pass): Customers may return opened phones within 14 days if the box and all accessories are complete. (Source: store_policy.md - "Returns")

**k_note** (pass): Use net_sales as the revenue column; exclude refunded orders.

**k_chat** (fail): ```json {   "answer": "We agreed to forecast 6 months ahead, by month.",   "sources": [     "recall"   ] } ```

**k_missing** (pass): I did not find any information about the CEO's salary in the user's documents.

**w_duckdb_license** (pass): DuckDB is fully open-source under the MIT license. The DuckDB Foundation's statutes also ensure that DuckDB remains open-source under the MIT license in perpetuity.

**w_statsforecast** (pass): The `statsforecast` Python library is developed by Nixtla, as indicated by the repository path on GitHub (`Nixtla/statsforecast`).  Sources: - https://github.com/Nixtla/statsforecast

**w_llama_port** (fail): The provided documentation does not explicitly state the default port on which llama.cpp's llama-server listens.  Sources used: * https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md * https://www.simplified.guide/llama-cpp/server-set-host-port

**w_parquet** (pass): The `pandas.read_parquet` function can use the following engines: `'auto'`, `'pyarrow'`, and `'fastparquet'`.  The `engine` parameter is used to specify the Parquet library to use. If set to `'auto'`, pandas will attempt to use `'pyarrow'` and fall back to `'fastparquet'` if `'pyarrow'` is not available.  Sources: - https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.read_parquet.html

**c_fib** (pass): The script calculates the 30th Fibonacci number (F1=1, F2=1) using an iterative approach. The code ran successfully and printed the value 832040.

**c_words** (pass): The script successfully read the content of file_1, counted all the words, and printed the three most common words and their respective counts.  Output: the: 19 fox: 12 quick: 9

**c_fix** (pass): The script in file_2 was fixed by changing 'len(number)' to 'len(numbers)' in the last line to correctly calculate the average. The script ran successfully and printed "Average: 5.5".

**c_json** (pass): The CSV file file_3 has been converted to a JSON file (output.json) containing a list of objects. The file contains 4 records.
