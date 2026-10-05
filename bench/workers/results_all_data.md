# Worker benchmark - all

Local model: Gemma 4 E4B (Q4_K_XL + MTP draft) on llama-server, worker slot, thinking on, RTX 3050 Laptop 4 GB.

| role | passed | median seconds | model calls |
|---|---|---|---|
| data | 10/11 | 44.6 | 4.7 avg |

**Total: 10/11**

| task | result | seconds | tools | detail |
|---|---|---|---|---|
| d_total | pass | 23.4 | guide, inspect_data, query_data | truth 817860.05 |
| d_top_category | pass | 18.9 | inspect_data, query_data | truth Classic 220053.10 |
| d_size_share | pass | 24.6 | inspect_data, guide, query_data, query_data | truth 45.89% (or as a fraction) |
| d_messy_pizzas | **fail** | 68.3 | guide, inspect_data, clean_data, query_data, clean_data | truth 49574 (a Total row doubles it) |
| d_chart | pass | 65.0 | guide, query_data, query_data, make_chart | line charts: 1, points: 12 |
| d_model | pass | 66.1 | guide, query_data, clean_data, query_data, train_model | features include affairs: False; score 0.723; answer cites it: True |
| h_aov | pass | 25.8 | inspect_data, query_data | truth 38.31 |
| h_weekday | pass | 44.6 | guide, inspect_data, query_data | truth Friday |
| h_corr | pass | 95.7 | guide, stats_test, stats_test | truth rho 0.656 |
| h_forecast | pass | 36.0 | guide, forecast, forecast | forecast made from year + quarter columns |
| h_ambiguous | pass | 83.5 | guide, query_data, clean_data | asked about date order: True; also gave a number: False |

## Answers

**d_total** (pass): The total revenue is 817860.05.

**d_top_category** (pass): The pizza category 'Classic' brought in the most revenue with a total of 220053.1.

**d_size_share** (pass): Large (L) pizzas account for 45.89% of the total revenue.

**d_messy_pizzas** (fail): 99,148

**d_chart** (pass): A line chart showing the monthly revenue over time has been generated. The highest recorded monthly revenue was $72,557.9 in July 2015, and the lowest was $64,027.6 in October 2015.

**d_model** (pass): The model (model_1) achieved an Area Under the Curve (AUC) score of 0.723. This score is better than the random baseline (0.5), indicating a useful but rough ranking. The most important predictive features are rate_marriage (0.411 share), yrs_married (0.357 share), age (0.124 share), and religious (0.084 share).

**h_aov** (pass): 38.3072622951

**h_weekday** (pass): Friday has the most orders, with a count of 3,538 distinct order IDs. The full breakdown is: Friday: 3,538, Thursday: 3,239, Saturday: 3,158, Wednesday: 3,024, Tuesday: 2,973, Monday: 2,794, Sunday: 2,624.

**h_corr** (pass): The Spearman rank correlation coefficient ($\rho$) between the poverty rate and the murder rate is 0.6559. The p-value is $1.8 \times 10^{-7}$, which is significantly lower than the standard 0.05 threshold. The test verdict is: "The correlation is larger than chance would explain (large effect)." This indicates that as poverty increases, the murder rate tends to increase across the states.

**h_forecast** (pass): The forecast for real GDP for the next 4 quarters is as follows: - Period 2009-10-01: Forecast 12969.3026 (80% range: 12525.1707 to 13413.4345) - Period 2010-01-01: Forecast 12948.2786 (80% range: 12504.1467 to 13392.4105) - Period 2010-04-01: Forecast 12927.8156 (80% range: 12483.6837 to 13371.9475) - Period 2010-07-01: Forecast 12907.8987 (80% range: 12463.7669 to 13352.0306) The model's backtest showed that on past data, the exponential smoothing (ETS) model missed by 1% on average, which is better than 2% for simply repeating the same period of last season. The reliability is strong.

**h_ambiguous** (pass): 
