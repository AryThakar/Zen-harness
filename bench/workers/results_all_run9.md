# Worker benchmark - all

Local model: Gemma 4 E4B (Q4_K_XL + MTP draft) on llama-server, worker slot, thinking on, RTX 3050 Laptop 4 GB.

| role | passed | median seconds | model calls |
|---|---|---|---|
| data | 10/11 | 14.0 | 3.8 avg |
| knowledge | 7/7 | 4.1 | 2.0 avg |
| web | 7/7 | 13.3 | 3.6 avg |
| coder | 7/7 | 11.4 | 2.6 avg |

**Total: 31/32**

| task | result | seconds | tools | detail |
|---|---|---|---|---|
| d_total | pass | 9.8 | inspect_data, query_data | truth 817860.05 |
| d_top_category | pass | 11.2 | inspect_data, query_data | truth Classic 220053.10 |
| d_size_share | pass | 20.0 | inspect_data, query_data, query_data, calculate | truth 45.89% (or as a fraction) |
| d_messy_pizzas | pass | 14.8 | query_data, clean_data, query_data | truth 49574 (a Total row doubles it) |
| d_chart | pass | 17.4 | inspect_data, query_data, make_chart | line charts: 1, points: 12 |
| d_model | **fail** | 46.9 | inspect_data, clean_data, query_data, query_data, query_data, train_model, query_data | features include affairs: True; score 1.0; answer cites it: True |
| h_aov | pass | 11.5 | inspect_data, query_data | truth 38.31 |
| h_weekday | pass | 13.2 | inspect_data, query_data | truth Friday |
| h_corr | pass | 14.0 | inspect_data, stats_test | truth rho 0.656 |
| h_forecast | pass | 26.6 | inspect_data, query_data, forecast | forecast made from year + quarter columns |
| h_ambiguous | pass | 5.9 | inspect_data | asked about date order: True; also gave a number: False |
| k_returns | pass | 4.0 | search_files | expects ('14',); sources ['store_policy.md - "Returns"'] |
| k_note | pass | 4.6 | search_files | expects ('net_sales',); sources ["note 'Revenue column' (note_1)"] |
| k_chat | pass | 3.6 | recall | expects ('6 months',); sources ['conversation old1'] |
| k_missing | pass | 3.8 | search_files | should say it isn't covered; sources [] |
| h_leave | pass | 4.1 | search_files | expects ('24',); sources ['employee_handbook.docx - "Expenses"'] |
| h_bulk | pass | 5.4 | recall | expects ('8%',); sources ['conversation old2'] |
| h_summary | pass | 4.8 | read_file | mentions at least 3 of: 9:00, laptop, 24 days, 500 |
| w_duckdb_license | pass | 11.5 | web_search, read_webpage | expects ('MIT',); sources ['https://duckdb.org/faq'] |
| w_statsforecast | pass | 13.3 | web_search, read_webpage | expects ('Nixtla',); sources ['https://github.com/Nixtla/statsforecast'] |
| w_llama_port | pass | 9.5 | web_search, read_webpage | expects ('8080',); sources ['https://www.simplified.guide/llama-cpp/server-start'] |
| w_parquet | pass | 25.5 | web_search, read_webpage, web_search, read_webpage | expects ('pyarrow', 'fastparquet'); sources ['https://pandas.pydata.org/docs/reference/api/pandas.read_parquet.html'] |
| h_pypi | pass | 14.9 | web_search, read_webpage | expects ('Python Software Foundation',); sources ['https://github.com/pypi'] |
| h_duckdb_lang | pass | 31.6 | web_search, read_webpage, web_search, read_webpage | expects ('C++',); sources ['https://en.wikipedia.org/wiki/DuckDB'] |
| h_hgb | pass | 9.2 | web_search, read_webpage | expects ('HistGradientBoostingClassifier',); sources ['https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.HistGradientBoostingClassifier.html'] |
| c_fib | pass | 11.4 | run_python | expects ('832040',) |
| c_words | pass | 18.7 | read_file, run_python | expects ('the', '19') |
| c_fix | pass | 21.2 | read_file, run_python, run_python | expects ('5.5',) |
| c_json | pass | 10.9 | run_python | 4 records + a .json file |
| h_primes | pass | 5.5 | run_python | expects ('1229',) |
| h_days | pass | 6.3 | run_python | expects ('380',) |
| h_emails | pass | 16.2 | read_file, run_python | expects ('anna@example.com', 'bob.smith@mail.example.org', 'x@test.io') |

## Answers

**d_total** (pass): The total revenue (quantity times price) in the data is 817860.05.

**d_top_category** (pass): The pizza category that brought in the most revenue is 'Classic', with a total revenue of 220053.1.

**d_size_share** (pass): The share of all revenue that comes from large (L) pizzas is 45.8903329488%.

**d_messy_pizzas** (pass): The total number of pizzas sold is 49574.

**d_chart** (pass): The monthly revenue as a line chart is shown above.

**d_model** (fail): The model trained to predict whether a person had an affair (`had_affair`) using the available data achieved an Area Under the Curve (AUC) score of **1.0**. This is significantly better than the baseline of 0.5 (random guessing).  However, the model training process flagged a critical issue: the feature `affairs` was identified as having a 1.0 share in predicting the outcome. This indicates that the model is essentially predicting the target variable directly from itself, which is a data leak and does not allow for true prediction based on other answers.  I recommend retraining the model witho

**h_aov** (pass): The average order value is 38.307262295.

**h_weekday** (pass): The day of the week with the most orders (distinct order IDs) is Friday, with 3538 orders.

**h_corr** (pass): Based on the statistical test performed (Spearman rank correlation), there is a significant relationship between the poverty rate and the murder rate across the states.  The test results are as follows: *   **Correlation Coefficient ($\rho$):** 0.6559 *   **P-value:** 1.8e-07 (which is less than 0.05) *   **Verdict:** The correlation is larger than chance would explain (large effect). *   **Direction:** The two variables tend to rise together.  In conclusion, the poverty rate is positively and significantly related to the murder rate.

**h_forecast** (pass): The forecast for real GDP for the next 4 quarters is as follows:  | Period | Forecast (Real GDP) | 80% Confidence Range (Low to High) | | :--- | :--- | :--- | | 2009-10-01 | 12969.3026 | 12525.1707 to 13413.4345 | | 2010-01-01 | 12948.2786 | 12504.1467 to 13392.4105 | | 2010-04-01 | 12927.8156 | 12483.6837 to 13371.9475 | | 2010-07-01 | 12907.8987 | 12463.7669 to 13352.0306 |  The Exponential Smoothing (ETS) model was used for this forecast, and it performed well in backtesting, beating the baseline by 1% on average.

**h_ambiguous** (pass): 

**k_returns** (pass): Customers may return opened phones within 14 days if the box and all accessories are complete.

**k_note** (pass): We decided to use net_sales as the revenue column; exclude refunded orders.

**k_chat** (pass): We agreed to forecast 6 months ahead, by month.

**k_missing** (pass): The documents do not cover the CEO's salary.

**h_leave** (pass): New employees get 24 days of annual leave, plus public holidays.

**h_bulk** (pass): In a previous conversation, it was mentioned that bulk orders of 50 units or more get an 8% discount.

**h_summary** (pass): Here is a summary of the employee handbook: *   **Office Hours:** 9:00 to 18:00 on weekdays (Warehouse opens at 7:00). *   **Equipment:** Employees receive a laptop on their first day, which must be returned upon leaving. *   **Leave:** New employees are entitled to 24 days of annual leave, plus public holidays. *   **Expenses:** Expenses exceeding 500 require manager approval before purchase.

**w_duckdb_license** (pass): DuckDB is fully open-source under the **MIT license**. According to the DuckDB FAQ, its development takes place on GitHub in the `duckdb/duckdb` repository, and the DuckDB Foundation ensures that it remains open-source under this license in perpetuity.  Sources: * https://duckdb.org/faq

**w_statsforecast** (pass): The `statsforecast` Python library is developed by **Nixtla**, as indicated by the GitHub repository name `Nixtla/statsforecast`.  Sources: *   https://github.com/Nixtla/statsforecast

**w_llama_port** (pass): llama.cpp's `llama-server` listens on port 8080 by default for a normal local launch.  Source: * https://www.simplified.guide/llama-cpp/server-start

**w_parquet** (pass): The `pandas.read_parquet` function supports the following engines:  *   **'auto'**: This is the default setting. It attempts to use 'pyarrow', and falls back to 'fastparquet' if 'pyarrow' is unavailable. *   **'pyarrow'**: Uses the PyArrow library for reading the parquet file. *   **'fastparquet'**: Uses the fastparquet library for reading the parquet file.  Source: https://pandas.pydata.org/docs/reference/api/pandas.read_parquet.html

**h_pypi** (pass): The Python Package Index (PyPI) is owned and operated by the Python Software Foundation (PSF).  Source: * https://github.com/pypi

**h_duckdb_lang** (pass): DuckDB is written in C++ (specifically, it can be built with just a C++17 compiler).  Sources: * https://en.wikipedia.org/wiki/DuckDB

**h_hgb** (pass): The class in scikit-learn that implements histogram-based gradient boosting for classification is `HistGradientBoostingClassifier`.  Source: * https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.HistGradientBoostingClassifier.html

**c_fib** (pass): The script calculates the 30th Fibonacci number (F1=F2=1) and prints the result. The 30th Fibonacci number is 832040.

**c_words** (pass): The word count of file_1 is calculated, and the three most common words and their counts are: the: 19 fox: 12 quick: 9

**c_fix** (pass): The script in file_2 was fixed by changing 'len(number)' to 'len(numbers)'. The script now runs correctly and prints 'Average: 5.5'.

**c_json** (pass): The CSV file `file_3` (people.csv) has been successfully converted to a JSON file named `people.json`. The file contains 4 records.

**h_primes** (pass): The number of prime numbers below 10,000 is 1229.

**h_days** (pass): The number of days from 2024-02-15 to 2025-03-01 is 380.

**h_emails** (pass): The distinct email addresses extracted from file_4 (contacts.txt), converted to lowercase, and sorted are: ['anna@example.com', 'bob.smith@mail.example.org', 'x@test.io']
