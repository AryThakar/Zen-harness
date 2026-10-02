# Worker benchmark - all

Local model: Gemma 4 E4B (Q4_K_XL + MTP draft) on llama-server, worker slot, thinking on, RTX 3050 Laptop 4 GB.

| role | passed | median seconds | model calls |
|---|---|---|---|
| data | 10/11 | 17.3 | 3.7 avg |
| knowledge | 7/7 | 4.1 | 2.0 avg |
| web | 7/7 | 15.2 | 3.9 avg |
| coder | 7/7 | 16.4 | 2.6 avg |

**Total: 31/32**

| task | result | seconds | tools | detail |
|---|---|---|---|---|
| d_total | pass | 12.5 | inspect_data, query_data | truth 817860.05 |
| d_top_category | pass | 11.2 | inspect_data, query_data | truth Classic 220053.10 |
| d_size_share | pass | 17.9 | inspect_data, query_data, query_data, calculate | truth 45.89% (or as a fraction) |
| d_messy_pizzas | pass | 17.3 | inspect_data, clean_data, query_data | truth 49574 (a Total row doubles it) |
| d_chart | **fail** | 22.1 | inspect_data, query_data, make_chart | line charts: 1, points: 49 |
| d_model | pass | 27.8 | inspect_data, clean_data, query_data, train_model | features include affairs: False; score 0.723; answer cites it: True |
| h_aov | pass | 14.8 | inspect_data, query_data, calculate | truth 38.31 |
| h_weekday | pass | 14.7 | inspect_data, query_data | truth Friday |
| h_corr | pass | 12.4 | inspect_data, stats_test | truth rho 0.656 |
| h_forecast | pass | 22.1 | inspect_data, query_data, forecast | forecast made from year + quarter columns |
| h_ambiguous | pass | 32.6 | inspect_data | asked about date order: True; also gave a number: False |
| k_returns | pass | 4.5 | search_files | expects ('14',); sources ['store_policy.md - "Returns"'] |
| k_note | pass | 4.0 | search_files | expects ('net_sales',); sources ['note_1'] |
| k_chat | pass | 3.4 | recall | expects ('6 months',); sources ['conversation old1'] |
| k_missing | pass | 4.4 | search_files | should say it isn't covered; sources [] |
| h_leave | pass | 4.1 | search_files | expects ('24',); sources ['employee_handbook.docx - "Expenses"'] |
| h_bulk | pass | 3.8 | recall | expects ('8%',); sources ['conversation old2'] |
| h_summary | pass | 6.6 | read_file | mentions at least 3 of: 9:00, laptop, 24 days, 500 |
| w_duckdb_license | pass | 14.3 | web_search, read_webpage | expects ('MIT',); sources ['https://duckdb.org/faq'] |
| w_statsforecast | pass | 29.2 | web_search, read_webpage, read_webpage | expects ('Nixtla',); sources ['https://github.com/Nixtla/statsforecast', 'https://gretl.org/2026/09/23/statsforecast-vs-gretl-features-performance-compatibility-and-use-cases-compared/'] |
| w_llama_port | pass | 21.4 | web_search, read_webpage, read_webpage | expects ('8080',); sources ['https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md'] |
| w_parquet | pass | 12.2 | web_search, read_webpage | expects ('pyarrow', 'fastparquet'); sources ['https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.read_parquet.html'] |
| h_pypi | pass | 9.0 | web_search, read_webpage | expects ('Python Software Foundation',); sources ['https://en.wikipedia.org/wiki/Python_Package_Index'] |
| h_duckdb_lang | pass | 15.2 | web_search, web_search, read_webpage | expects ('C++',); sources ['https://github.com/duckdb/duckdb'] |
| h_hgb | pass | 29.5 | web_search, web_search, web_search, read_webpage | expects ('HistGradientBoostingClassifier',); sources ['https://scikit-learn.org/1.1/modules/generated/sklearn.ensemble.HistGradientBoostingClassifier.html'] |
| c_fib | pass | 7.1 | run_python | expects ('832040',) |
| c_words | pass | 19.9 | read_file, run_python | expects ('the', '19') |
| c_fix | pass | 29.6 | read_file, run_python, run_python | expects ('5.5',) |
| c_json | pass | 16.4 | run_python | 4 records + a .json file |
| h_primes | pass | 10.6 | run_python | expects ('1229',) |
| h_days | pass | 8.5 | run_python | expects ('380',) |
| h_emails | pass | 17.2 | read_file, run_python | expects ('anna@example.com', 'bob.smith@mail.example.org', 'x@test.io') |

## Answers

**d_total** (pass): The total revenue (quantity times price) in this data was 817860.049999993.

**d_top_category** (pass): The Classic category brought in the most revenue, with a total of 220053.1.

**d_size_share** (pass): The share of all revenue that comes from large (L) pizzas is 45.89%.

**d_messy_pizzas** (pass): 49574

**d_chart** (fail): The monthly revenue chart is shown above.

**d_model** (pass): The model trained to predict whether a person had an affair (`had_affair`) based on other survey answers is reasonably good.  The model uses a linear approach and achieved an **AUC score of 0.723** in 5-fold cross-validation. This is better than the baseline score of 0.5 (which would be equivalent to random guessing).  The most important features associated with predicting whether a person had an affair are: 1.  `rate_marriage` (41.1% share) 2.  `yrs_married` (35.7% share) 3.  `age` (12.4% share) 4.  `religious` (8.4% share)

**h_aov** (pass): The average order value is 38.3072622951.

**h_weekday** (pass): Friday has the most orders (distinct order ids), with 3538 orders.

**h_corr** (pass): Based on the analysis of the data, there is a significant positive correlation between the poverty rate and the murder rate across the states.  The statistical test performed was a Spearman rank correlation, which yielded the following results: *   **Correlation Coefficient (rho):** 0.6559 *   **P-value:** 1.8e-07 (p < 0.0001)  The verdict is that the correlation is larger than chance would explain, indicating a large effect. It should be noted that correlation does not imply causation.

**h_forecast** (pass): Forecast of real GDP for the next 4 quarters:  | Period | Forecasted Real GDP | 80% Confidence Interval (Low) | 80% Confidence Interval (High) | | :--- | :--- | :--- | :--- | | 2009-10-01 | 12969.3026 | 12525.1707 | 13413.4345 | | 2010-01-01 | 12948.2786 | 12504.1467 | 13392.4105 | | 2010-04-01 | 12927.8156 | 12483.6837 | 13371.9475 | | 2010-07-01 | 12907.8987 | 12463.7669 | 13352.0306 |  The forecast was generated using an Exponential Smoothing (ETS) model and the model showed strong reliability, beating the baseline by 1% on average in backtesting.

**h_ambiguous** (pass): I am waiting for the user to specify the date format (Day first or Month first) before I can calculate the total sales for February 2025.

**k_returns** (pass): Customers have 14 days to return an opened phone if the box and all accessories are complete.

**k_note** (pass): Use net_sales as the revenue column; exclude refunded orders. (note_1)

**k_chat** (pass): We agreed to forecast 6 months ahead, by month.

**k_missing** (pass): The provided documents do not contain information regarding the CEO's salary.

**h_leave** (pass): New employees get 24 days of annual leave, plus public holidays. (Source: employee_handbook.docx)

**h_bulk** (pass): In a previous conversation on 2026-09-25 15:30, it was mentioned that "Bulk orders of 50 units or more get an 8% discount."

**h_summary** (pass): The employee handbook contains the following short points:  *   Office hours are 9:00 to 18:00 on weekdays, while the warehouse opens at 7:00. *   Employees receive a laptop on their first day, which must be returned upon leaving. *   New employees are entitled to 24 days of annual leave, in addition to public holidays. *   Any expenses exceeding 500 require a manager's prior approval.

**w_duckdb_license** (pass): DuckDB is fully open-source under the MIT license.  Source: * https://duckdb.org/faq

**w_statsforecast** (pass): The `statsforecast` Python library is developed by Nixtla.  Sources: *   https://github.com/Nixtla/statsforecast *   https://gretl.org/2026/09/23/statsforecast-vs-gretl-features-performance-compatibility-and-use-cases-compared/

**w_llama_port** (pass): The default port for llama.cpp's llama-server is 8080.

**w_parquet** (pass): pandas.read_parquet can use the following engines:  *   **auto** (the default): This setting automatically tries to use 'pyarrow' and falls back to 'fastparquet' if 'pyarrow' is unavailable. *   **pyarrow** *   **fastparquet**  These engines are specified using the `engine` parameter, which accepts these values: `engine{‘auto’, ‘pyarrow’, ‘fastparquet’}`.  Source: https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.read_parquet.html

**h_pypi** (pass): The Python Package Index (PyPI) is run by the Python Software Foundation.  Source: https://en.wikipedia.org/wiki/Python_Package_Index

**h_duckdb_lang** (pass): DuckDB is primarily written in C++.  Source: https://github.com/duckdb/duckdb

**h_hgb** (pass): In scikit-learn, the class that implements histogram-based gradient boosting for classification is `sklearn.ensemble.HistGradientBoostingClassifier`. This estimator is described as a "Histogram-based Gradient Boosting Classification Tree" and is noted to be "much faster than GradientBoostingClassifier for big datasets (n_samples >= 10 000)."  Source: *   https://scikit-learn.org/1.1/modules/generated/sklearn.ensemble.HistGradientBoostingClassifier.html

**c_fib** (pass): The 30th Fibonacci number (where F1 = F2 = 1) is 832040.  ```python def fibonacci(n):     if n <= 0:         return 0     elif n == 1 or n == 2:         return 1     else:         a, b = 1, 1         for _ in range(3, n + 1):             next_fib = a + b             a = b             b = next_fib         return b  result = fibonacci(30) print(result) ```  832040

**c_words** (pass): The words in file_1 have been counted, and the three most common words and their counts are printed.  The three most common words and their counts are: the: 19 fox: 12 quick: 9  ```python from collections import Counter import re  # Content of file_1 text = """the quick brown fox jumps over the lazy dog the fox was quick and the dog was lazy but the fox and the dog became friends and the quick fox stayedthe quick brown fox jumps over the lazy dog the fox was quick and the dog was lazy but the fox and the dog became friends and the quick fox stayedthe quick brown fox jumps over the lazy dog the

**c_fix** (pass): The script in file_2 was fixed by changing 'len(number)' to 'len(numbers)' in the print statement. The fixed script runs successfully and prints: Average: 5.5

**c_json** (pass): The CSV file `file_3` was successfully converted to a JSON file named `file_3.json`. The resulting JSON file contains a list of objects, and the CSV file has 4 records.  **Output:** ``` Successfully converted file_3 to file_3.json. The CSV file has 4 records. ```

**h_primes** (pass): The number of prime numbers below 10,000 is 1229. This was computed using the Sieve of Eratosthenes implemented in a Python script.

**h_days** (pass): The number of days from 2024-02-15 to 2025-03-01 is 380. The calculation was performed using Python's datetime module.

**h_emails** (pass): The distinct email addresses from file_4, converted to lowercase and sorted, are: anna@example.com bob.smith@mail.example.org x@test.io
