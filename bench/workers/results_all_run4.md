# Worker benchmark - all

Local model: Gemma 4 E4B (Q4_K_XL + MTP draft) on llama-server, worker slot, thinking on, RTX 3050 Laptop 4 GB.

| role | passed | median seconds | model calls |
|---|---|---|---|
| data | 11/11 | 16.4 | 3.8 avg |
| knowledge | 7/7 | 4.5 | 2.0 avg |
| web | 7/7 | 16.0 | 3.6 avg |
| coder | 6/7 | 18.1 | 3.6 avg |

**Total: 31/32**

| task | result | seconds | tools | detail |
|---|---|---|---|---|
| d_total | pass | 10.4 | inspect_data, query_data | truth 817860.05 |
| d_top_category | pass | 11.4 | inspect_data, query_data | truth Classic 220053.10 |
| d_size_share | pass | 17.9 | inspect_data, query_data, query_data, calculate | truth 45.89% (or as a fraction) |
| d_messy_pizzas | pass | 16.4 | inspect_data, clean_data, query_data | truth 49574 (a Total row doubles it) |
| d_chart | pass | 21.8 | inspect_data, query_data, make_chart | line charts: 1, points: 12 |
| d_model | pass | 30.5 | inspect_data, clean_data, query_data, train_model | features include affairs: False; score 0.723; answer cites it: True |
| h_aov | pass | 22.4 | inspect_data, query_data, query_data, calculate | truth 38.31 |
| h_weekday | pass | 14.5 | inspect_data, query_data | truth Friday |
| h_corr | pass | 11.5 | inspect_data, stats_test | truth rho 0.656 |
| h_forecast | pass | 33.4 | inspect_data, query_data, forecast | forecast made from year + quarter columns |
| h_ambiguous | pass | 16.0 | inspect_data | asked about date order: True; also gave a number: False |
| k_returns | pass | 5.7 | search_files | expects ('14',); sources ['store_policy.md - "Returns"'] |
| k_note | pass | 3.8 | search_files | expects ('net_sales',); sources ["note 'Revenue column' (note_1)"] |
| k_chat | pass | 5.2 | recall | expects ('6 months',); sources ['conversation old1'] |
| k_missing | pass | 4.5 | search_files | should say it isn't covered; sources [] |
| h_leave | pass | 3.9 | search_files | expects ('24',); sources ['employee_handbook.docx'] |
| h_bulk | pass | 3.8 | recall | expects ('8%',); sources ['conversation old2'] |
| h_summary | pass | 7.2 | read_file | mentions at least 3 of: 9:00, laptop, 24 days, 500 |
| w_duckdb_license | pass | 18.6 | web_search, read_webpage, read_webpage | expects ('MIT',); sources ['https://duckdb.org/faq'] |
| w_statsforecast | pass | 12.7 | web_search, read_webpage | expects ('Nixtla',); sources ['https://github.com/Nixtla/statsforecast'] |
| w_llama_port | pass | 23.9 | web_search, read_webpage, read_webpage | expects ('8080',); sources ['https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md'] |
| w_parquet | pass | 16.0 | web_search, read_webpage | expects ('pyarrow', 'fastparquet'); sources ['https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.read_parquet.html'] |
| h_pypi | pass | 21.3 | web_search, read_webpage, web_search, read_webpage | expects ('Python Software Foundation',); sources ['https://en.wikipedia.org/wiki/Python_Package_Index'] |
| h_duckdb_lang | pass | 9.9 | web_search, read_webpage | expects ('C++',); sources ['https://github.com/duckdb/duckdb'] |
| h_hgb | pass | 13.7 | web_search, read_webpage | expects ('HistGradientBoostingClassifier',); sources ['https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.HistGradientBoostingClassifier.html'] |
| c_fib | pass | 29.5 | run_python, run_python | expects ('832040',) |
| c_words | pass | 15.4 | read_file, run_python | expects ('the', '19') |
| c_fix | pass | 11.7 | read_file, run_python | expects ('5.5',) |
| c_json | **fail** | 37.2 | read_file, run_python, run_python | 4 records + a .json file |
| h_primes | pass | 7.0 | run_python | expects ('1229',) |
| h_days | pass | 21.8 | run_python, run_python, run_python | expects ('380',) |
| h_emails | pass | 18.1 | read_file, run_python | expects ('anna@example.com', 'bob.smith@mail.example.org', 'x@test.io') |

## Answers

**d_total** (pass): The total revenue (quantity times price) in this data was 817860.049999993.

**d_top_category** (pass): The pizza category that brought in the most revenue is Classic, with a total revenue of 220053.1.

**d_size_share** (pass): The share of all revenue that comes from large (L) pizzas is 45.8903329488%.

**d_messy_pizzas** (pass): The total number of pizzas sold is 49,574.

**d_chart** (pass): The monthly revenue has been shown as a line chart.

**d_model** (pass): The model trained to predict whether a person had an affair (where `affairs > 0` is represented as 1) is `model_1`.  The model's performance is summarized as follows: *   **Metric:** AUC *   **Score:** 0.723 *   **Verdict:** Better than the baseline: a useful but rough ranking (AUC 0.72; 0.5 = guessing).  The most important predictive features associated with having an affair are: 1.  `rate_marriage` (Share: 0.411) 2.  `yrs_married` (Share: 0.357) 3.  `age` (Share: 0.124) 4.  `religious` (Share: 0.084)  submit_result{answer:<|"|>The trained model (model_1) has an AUC score of 0.723, which is b

**h_aov** (pass): The average order value is 38.3072622951.

**h_weekday** (pass): The day of the week with the most orders (distinct order ids) is Friday, with 3538 orders.

**h_corr** (pass): Based on the statistical test, there is a significant positive correlation between the poverty rate and the murder rate across the states.  The Spearman rank correlation analysis found: *   **Correlation Coefficient (rho):** 0.6559 *   **P-value:** 1.8e-07 (p < 0.0001) *   **Verdict:** The correlation is larger than chance would explain (large effect).  This indicates that as the poverty rate increases, the murder rate tends to increase. Please note that correlation does not imply causation.

**h_forecast** (pass): The forecast for real GDP for the next 4 quarters is as follows:  *   **Quarter 1:** Forecast: 40106.731 (80% confidence range: 39326.06 to 40887.402) *   **Quarter 2:** Forecast: 13141.92 (80% confidence range: 12361.249 to 13922.591) *   **Quarter 3:** Forecast: 0.0 (80% confidence range: 0.0 to 780.671) *   **Quarter 4:** Forecast: 0.0 (80% confidence range: 0.0 to 780.671)  The model used was 'seasonal naive' and its reliability is rated as 'moderate'.

**h_ambiguous** (pass): Awaiting user input on date format

**k_returns** (pass): Customers may return opened phones within 14 days if the box and all accessories are complete (store_policy.md - "Returns").

**k_note** (pass): The column that was decided to be used as revenue is net_sales.  submit_result(answer="Use net_sales as the revenue column; exclude refunded orders.", sources=["note_1"])

**k_chat** (pass): We agreed to forecast 6 months ahead, by month.

**k_missing** (pass): I could not find any information regarding the CEO's salary in the provided documents.

**h_leave** (pass): New employees get 24 days of annual leave, plus public holidays.

**h_bulk** (pass): The bulk-order discount discussed previously was that "Bulk orders of 50 units or more get an 8% discount."

**h_summary** (pass): *   **Office Hours:** 9:00 to 18:00 on weekdays (Warehouse opens at 7:00). *   **Equipment:** Employees receive a laptop on their first day, which must be returned upon leaving. *   **Leave:** Employees are entitled to 24 days of annual leave, in addition to public holidays. *   **Expenses:** Any expenses exceeding 500 require prior approval from a manager.

**w_duckdb_license** (pass): DuckDB is fully open-source under the MIT license. The DuckDB Foundation's statutes ensure that DuckDB remains open-source under the MIT license in perpetuity.  Sources: - https://duckdb.org/faq

**w_statsforecast** (pass): The `statsforecast` Python library is developed by **Nixtla**, as indicated by the repository name and associated website.  Source: *   https://github.com/Nixtla/statsforecast

**w_llama_port** (pass): llama.cpp's `llama-server` listens on port **8080** by default.  According to the documentation for the LLaMA.cpp HTTP Server, the command will start a server that by default listens on `127.0.0.1:8080`.  Sources: - https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md

**w_parquet** (pass): The `pandas.read_parquet` function can use the following engines:  *   **`'pyarrow'`**: This is one of the specific Parquet libraries that can be used. *   **`'fastparquet'`**: This is the other specific Parquet library that can be used. *   **`'auto'`**: This is the default setting, and it instructs pandas to automatically try `'pyarrow'` first, and if that is unavailable, it falls back to `'fastparquet'`.  These engines are specified using the `engine` parameter in the function: `pandas.read_parquet(path, engine='auto', ...)`  Source: *   https://pandas.pydata.org/pandas-docs/stable/referenc

**h_pypi** (pass): The Python Package Index (PyPI) is run by the Python Software Foundation, which is a charity.  Source: * https://en.wikipedia.org/wiki/Python_Package_Index

**h_duckdb_lang** (pass): DuckDB is primarily written in C++.  Source: * https://github.com/duckdb/duckdb

**h_hgb** (pass): The class in scikit-learn that implements histogram-based gradient boosting for classification is `sklearn.ensemble.HistGradientBoostingClassifier`.  Source: * https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.HistGradientBoostingClassifier.html

**c_fib** (pass): The script calculates the 30th Fibonacci number (F1=1, F2=1) using an iterative approach. The code ran successfully and produced the result: The 30th Fibonacci number (F1=1, F2=1) is: 832040.

**c_words** (pass): The script successfully counted the words in file_1 (story.txt) and identified the three most common words and their counts. The most common words are 'the' (19 times), 'fox' (12 times), and 'quick' (9 times).

**c_fix** (pass): The script was fixed by changing `len(number)` to `len(numbers)` on the last line. The script calculates the average of the list `numbers` and prints the result. The output is: "Average: 5.5"

**c_json** (fail): The CSV file file_3 (people.csv) was converted to a JSON list of objects. It contains 4 records. The resulting JSON structure is provided above.

**h_primes** (pass): There are 1229 prime numbers below 10,000.

**h_days** (pass): The code runs without error and calculates that there are 380 days from 2024-02-15 to 2025-03-01.

**h_emails** (pass): The distinct email addresses extracted from `file_4`, ignoring case and printed in sorted order, are:  ``` anna@example.com bob.smith@mail.example.org x@test.io ```  The script used for this task is provided below:  ```python import re  # Content of file_4 file_content = "Contact anna@example.com or bob.smith@mail.example.org; old: x@test.io (do not use), ANNA@example.com again."  # Regex for email extraction # Simple regex: characters@characters.characters email_pattern = r"[\w\.-]+@[\w\.-]+\.\w+"  # Find all matches found_emails = re.findall(email_pattern, file_content)  # Convert to lowerca
