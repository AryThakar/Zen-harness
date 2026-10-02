# Worker benchmark - all

Local model: Gemma 4 E4B (Q4_K_XL + MTP draft) on llama-server, worker slot, thinking on, RTX 3050 Laptop 4 GB.

| role | passed | median seconds | model calls |
|---|---|---|---|
| data | 11/11 | 14.8 | 3.5 avg |
| knowledge | 7/7 | 5.5 | 2.3 avg |
| web | 7/7 | 12.8 | 3.1 avg |
| coder | 7/7 | 13.6 | 2.4 avg |

**Total: 32/32**

| task | result | seconds | tools | detail |
|---|---|---|---|---|
| d_total | pass | 11.2 | inspect_data, query_data | truth 817860.05 |
| d_top_category | pass | 10.9 | inspect_data, query_data | truth Classic 220053.10 |
| d_size_share | pass | 16.9 | inspect_data, query_data, query_data, calculate | truth 45.89% (or as a fraction) |
| d_messy_pizzas | pass | 14.8 | query_data, clean_data, query_data | truth 49574 (a Total row doubles it) |
| d_chart | pass | 18.2 | inspect_data, query_data, make_chart | line charts: 1, points: 12 |
| d_model | pass | 25.0 | inspect_data, clean_data, query_data, train_model | features include affairs: False; score 0.723; answer cites it: True |
| h_aov | pass | 12.3 | inspect_data, query_data | truth 38.31 |
| h_weekday | pass | 14.9 | inspect_data, query_data | truth Friday |
| h_corr | pass | 12.9 | inspect_data, stats_test | truth rho 0.656 |
| h_forecast | pass | 23.9 | inspect_data, query_data, forecast | forecast made from year + quarter columns |
| h_ambiguous | pass | 6.1 | inspect_data | asked about date order: True; also gave a number: False |
| k_returns | pass | 5.5 | search_files | expects ('14',); sources ['store_policy.md - "Returns"'] |
| k_note | pass | 3.4 | search_files | expects ('net_sales',); sources ["note 'Revenue column' (note_1)"] |
| k_chat | pass | 4.4 | recall | expects ('6 months',); sources ['conversation old1'] |
| k_missing | pass | 8.9 | search_files, search_files | should say it isn't covered; sources [] |
| h_leave | pass | 4.1 | search_files | expects ('24',); sources ['employee_handbook.docx - "Expenses"'] |
| h_bulk | pass | 10.1 | search_files, recall | expects ('8%',); sources ['conversation old2'] |
| h_summary | pass | 6.5 | read_file | mentions at least 3 of: 9:00, laptop, 24 days, 500 |
| w_duckdb_license | pass | 12.8 | web_search, read_webpage | expects ('MIT',); sources ['https://duckdb.org/faq'] |
| w_statsforecast | pass | 11.4 | web_search, read_webpage | expects ('Nixtla',); sources ['https://github.com/Nixtla/statsforecast'] |
| w_llama_port | pass | 13.4 | web_search, read_webpage | expects ('8080',); sources ['https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md'] |
| w_parquet | pass | 12.3 | web_search, read_webpage | expects ('pyarrow', 'fastparquet'); sources ['https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.read_parquet.html'] |
| h_pypi | pass | 8.1 | web_search, read_webpage | expects ('Python Software Foundation',); sources ['https://en.wikipedia.org/wiki/Python_Package_Index'] |
| h_duckdb_lang | pass | 17.8 | web_search, web_search, read_webpage | expects ('C++',); sources ['https://en.wikipedia.org/wiki/DuckDB'] |
| h_hgb | pass | 14.1 | web_search, read_webpage | expects ('HistGradientBoostingClassifier',); sources ['https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.HistGradientBoostingClassifier.html'] |
| c_fib | pass | 14.0 | run_python | expects ('832040',) |
| c_words | pass | 13.6 | read_file, run_python | expects ('the', '19') |
| c_fix | pass | 11.3 | read_file, run_python | expects ('5.5',) |
| c_json | pass | 14.2 | run_python | 4 records + a .json file |
| h_primes | pass | 6.1 | run_python | expects ('1229',) |
| h_days | pass | 4.9 | run_python | expects ('380',) |
| h_emails | pass | 17.2 | read_file, run_python | expects ('anna@example.com', 'bob.smith@mail.example.org', 'x@test.io') |

## Answers

**d_total** (pass): The total revenue (quantity times price) in this data was 817860.05.

**d_top_category** (pass): The pizza category that brought in the most revenue is **Classic**, with a total revenue of **220053.1**.

**d_size_share** (pass): 45.89%

**d_messy_pizzas** (pass): 49574

**d_chart** (pass): The monthly revenue has been calculated and visualized below as a line chart.  The chart title is "sum of monthly_revenue by month", and it displays the revenue trend across all months. The highest recorded monthly revenue was 72557.9 (in 2015-07) and the lowest was 64027.6 (in 2015-10).  The chart is generated from the table `data_2`.

**d_model** (pass): The model has been trained to predict whether a person had an affair (`affairs > 0`).  The model's performance assessment is as follows: *   **Model Type:** Linear *   **Metric:** AUC *   **Score:** 0.723 *   **Verdict:** Better than the baseline: a useful but rough ranking (AUC 0.72; 0.5 = guessing).  The most important predictive features found by the model are: 1.  `rate_marriage` (41.1% share) 2.  `yrs_married` (35.7% share) 3.  `age` (12.4% share) 4.  `religious` (8.4% share)  I have trained the model and reported how good it is.

**h_aov** (pass): 38.307262295

**h_weekday** (pass): The day of the week with the most orders (distinct order IDs) is Friday, with 3538 orders.

**h_corr** (pass): The poverty rate and the murder rate are positively correlated across states.  The statistical test (Spearman rank correlation) found a correlation coefficient ($\rho$) of **0.6559**, which indicates that as the poverty rate rises, the murder rate tends to rise as well.  The correlation is statistically significant, with a p-value of **1.8e-07** (p < 0.0001), meaning the relationship is larger than what would be expected by chance.  *Note: Correlation does not imply causation.*

**h_forecast** (pass): The forecast for Real GDP for the next 4 quarters, using an exponential smoothing (ETS) model, is as follows:  | Period | Forecast (Real GDP) | 80% Confidence Range | | :--- | :--- | :--- | | 2009-10-01 | 12969.3026 | 12525.1707 to 13413.4345 | | 2010-01-01 | 12948.2786 | 12504.1467 to 13392.4105 | | 2010-04-01 | 12927.8156 | 12483.6837 to 13371.9475 | | 2010-07-01 | 12907.8987 | 12463.7669 to 13352.0306 |  The model performed well on past data, beating the baseline by 1% average error (compared to 2% for the baseline).

**h_ambiguous** (pass): 

**k_returns** (pass): Customers may return opened phones within 14 days if the box and all accessories are complete.

**k_note** (pass): We decided to use net_sales as the revenue column; exclude refunded orders.

**k_chat** (pass): In an earlier conversation on 2026-09-20 10:00, it was agreed to forecast 6 months ahead, by month.

**k_missing** (pass): The CEO's salary is not mentioned in the available documents. The search results only provided information about annual leave and expense approvals in the employee handbook.

**h_leave** (pass): New employees get 24 days of annual leave, plus public holidays.

**h_bulk** (pass): In a conversation from 2026-09-25 15:30, it was mentioned that "Bulk orders of 50 units or more get an 8% discount."

**h_summary** (pass): Here is a summary of the employee handbook:  *   **Office Hours:** 9:00 to 18:00 on weekdays (warehouse opens at 7:00). *   **Equipment:** Employees receive a laptop on their first day, which must be returned upon leaving. *   **Leave:** New employees are entitled to 24 days of annual leave, in addition to public holidays. *   **Expenses:** Manager approval is required for any expenses exceeding 500.

**w_duckdb_license** (pass): DuckDB is fully open-source under the MIT license. The DuckDB Foundation's statutes also ensure that DuckDB remains open-source under the MIT license in perpetuity.  Sources: * https://duckdb.org/faq

**w_statsforecast** (pass): The `statsforecast` Python library is developed by Nixtla.  Sources: - https://github.com/Nixtla/statsforecast

**w_llama_port** (pass): llama.cpp's `llama-server` listens on port **8080** by default.  This is specified in the usage instructions for the server: *   `--port PORT` | port to listen (default: 8080)  Sources: *   https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md

**w_parquet** (pass): The `pandas.read_parquet` function supports the following engines:  *   **'pyarrow'**: This is one of the specific Parquet libraries that can be used. *   **'fastparquet'**: This is another specific Parquet library that can be used. *   **'auto'**: This is the default setting, which instructs pandas to first try the 'pyarrow' engine and fall back to 'fastparquet' if 'pyarrow' is not available.  These engines can be specified using the `engine` parameter in the function.  Source: *   https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.read_parquet.html

**h_pypi** (pass): The Python Package Index (PyPI) is run by the Python Software Foundation, which is described as a charity.  Source: https://en.wikipedia.org/wiki/Python_Package_Index

**h_duckdb_lang** (pass): DuckDB is written in C++.  Source: https://en.wikipedia.org/wiki/DuckDB

**h_hgb** (pass): The class in scikit-learn that implements histogram-based gradient boosting for classification is `sklearn.ensemble.HistGradientBoostingClassifier`.  Source: * https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.HistGradientBoostingClassifier.html

**c_fib** (pass): The script calculates the 30th Fibonacci number (where F1=1 and F2=1) and prints the result. The output is 832040.

**c_words** (pass): The word count in file_1 was processed. The three most common words are: the: 19 fox: 12 quick: 9

**c_fix** (pass): The script in file_2 was fixed by changing 'len(number)' to 'len(numbers)' on the last line. The script prints: Average: 5.5

**c_json** (pass): The CSV file `file_3` has been successfully converted to `people.json`. The CSV file has 4 records.

**h_primes** (pass): The number of prime numbers below 10,000 is 1229.

**h_days** (pass): There are 380 days from 2024-02-15 to 2025-03-01.

**h_emails** (pass): The distinct email addresses from file_4, converted to lowercase and sorted, are: ['anna@example.com', 'bob.smith@mail.example.org', 'x@test.io']
