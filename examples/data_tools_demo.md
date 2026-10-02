# Data tools on real data

Every call below is a direct tool call with fixed arguments (no AI model). Results are what the model would receive.

## 1. A messy export: pizza sales
### Inspect the raw file
`inspect_data` - ok in 4.0s

```json
{
 "status": "ok",
 "data": "data_1",
 "name": "pizza_messy.csv",
 "rows": 48621,
 "columns": [
  {
   "name": "order_details_id",
   "type": "text",
   "unique": 48621,
   "unique_per_row": true,
   "top": [
    [
     "1",
     1
    ],
    [
     "2",
     1
    ],
    [
     "3",
     1
    ]
   ],
   "summary_rows": 1,
   "almost": {
    "as": "number",
    "unreadable": 0,
    "examples": []
   }
  },
  {
   "name": "order_id",
   "type": "number",
   "missing": 1,
   "unique": 21350,
   "min": 1.0,
   "median": 10682.5,
   "max": 21350.0
  },
  {
   "name": "date",
   "type": "date",
   "missing": 1,
   "unique": 358,
   "first": "2015-01-01",
   "last": "2015-12-31"
  },
  {
   "name": "time",
   "type": "text",
   "missing": 1,
   "unique": 16382,
   "top": [
    [
     "12:32:00",
     26
    ],
    [
     "11:59:10",
     24
    ],
    [
     "12:26:04",
     23
    ]
   ]
  },
  {
   "name": "pizza_id",
   "type": "text",
   "missing": 1,
   "unique": 91,
   "top": [
    [
     "big_meat_s",
     1811
    ],
    [
     "thai_ckn_l",
     1365
    ],
    [
     "five_cheese_l",
     1359
    ]
   ]
  },
  {
   "name": "name",
   "type": "text",
   "missing": 1,
   "unique": 32,
   "top": [
    [
     "The Classic Deluxe Pizza",
     2416
    ],
    [
     "The Barbecue Chicken Pizza",
     2372
    ],
    [
     "The Hawaiian Pizza",
     2370
    ]
   ]
  },
  {
   "name": "category",
   "type": "text",
   "missing": 1,
   "unique": 4,
   "top": [
    [
     "Classic",
     14579
    ],
    [
     "Supreme",
     11777
    ],
    [
     "Veggie",
     11449
    ]
   ]
  },
  {
   "name": "size",
   "type": "text",
   "missing": 1,
   "unique": 5,
   "top": [
    [
     "L",
     18526
    ],
    [
     "M",
     15385
    ],
    [
     "S",
     14137
    ]
   ]
  },
  {
   "name": "quantity",
   "type": "number",
   "unique": 5,
   "min": 1.0,
   "median": 1.0,
   "max": 49574.0
  },
  {
   "name": "price",
   "type": "number",
   "missing": 1,
   "unique": 25,
   "min": 9.75,
   "median": 16.5,
   "max": 35.95,
   "unit": "$"
  }
 ],
 "duplicate_rows": 0,
 "sample_rows": [
  {
   "order_details_id": "1",
   "order_id": 1,
   "date": "2015-01-01",
   "time": "11:38:36",
   "pizza_id": "hawaiian_m",
   "name": "The Hawaiian Pizza",
   "category": "Classic",
   "size": "M",
   "quantity": 1,
   "price": 13.25
  },
  {
   "order_details_id": "2",
   "order_id": 2,
   "date": "2015-01-01",
   "time": "11:57:40",
   "pizza_id": "classic_dlx_m",
   "name": "The Classic Deluxe Pizza",
   "category": "Classic",
   "size": "M",
   "quantity": 1,
   "price": 16.0
  },
  {
   "order_details_id": "3",
   "order_id": 2,
   "date": "2015-01-01",
   "time": "11:57:40",
   "pizza_id": "five_cheese_l",
   "name": "The Five Cheese Pizza",
   "category": "Veggie",
   "size": "L",
   "quantity": 1,
   "price": 18.5
  }
 ],
 "suggested_steps": [
  {
   "op": "drop_rows",
   "column": "order_details_id",
   "values": [
    "Total"
   ],
   "why": "1 row labelled 'Total' look like summary rows, not data; they would distort totals"
  },
  {
   "op": "parse_numbers",
   "columns": [
    "order_details_id"
   ],
   "why": "order_details_id holds numbers stored as text"
  }
 ],
 "questions": [],
 "note": "Missing values are left as they are: fill or drop them (fill_missing / drop_missing) only when an analysis needs it, with a method that fits the column."
}
```

### Clean with the suggested steps (+ the user's answer)
`clean_data` - ok in 0.2s

```json
{
 "status": "ok",
 "data": "data_2",
 "from": "data_1",
 "rows_before": 48621,
 "rows_after": 48620,
 "log": [
  "1. drop_rows (order_details_id in ['Total']) - removed 1 rows",
  "2. parse_numbers - order_details_id: 48,620 numbers"
 ]
}
```

### Top pizzas by revenue (exact)
`query_data` - ok in 0.1s

```json
{
 "status": "ok",
 "columns": [
  "name",
  "revenue",
  "pizzas"
 ],
 "rows": [
  [
   "The Thai Chicken Pizza",
   43434.25,
   2371
  ],
  [
   "The Barbecue Chicken Pizza",
   42768.0,
   2432
  ],
  [
   "The California Chicken Pizza",
   41409.5,
   2370
  ],
  [
   "The Classic Deluxe Pizza",
   38180.5,
   2453
  ],
  [
   "The Spicy Italian Pizza",
   34831.25,
   1924
  ]
 ],
 "total_rows": 5
}
```

### Daily revenue, saved as a table
`query_data` - ok in 0.1s

```json
{
 "status": "ok",
 "columns": [
  "date",
  "revenue"
 ],
 "rows": [
  [
   "2015-01-01",
   2713.85
  ],
  [
   "2015-01-02",
   2731.9
  ],
  [
   "2015-01-03",
   2662.4
  ],
  [
   "2015-01-04",
   1755.45
  ],
  [
   "2015-01-05",
   2065.95
  ]
 ],
 "total_rows": 358,
 "note": "Showing 5 of 358 rows.",
 "saved_as": "data_3"
}
```

### Chart: weekly revenue
`make_chart` - ok in 0.0s

```json
{
 "status": "ok",
 "chart": "line",
 "data": "data_3",
 "title": "sum of revenue by date",
 "points": 51,
 "grouping": "by week",
 "first": [
  "2015-01-05",
  15999.05
 ],
 "last": [
  "2015-12-21",
  11427.85
 ],
 "highest": [
  "2015-11-23",
  19753.9
 ],
 "lowest": [
  "2015-09-21",
  11026.4
 ],
 "notes": [
  "The last week (2015-12-28) is incomplete (data ends 2015-12-31), so it was left out.",
  "The first week (2014-12-29) is incomplete (data starts 2015-01-01), so it was left out."
 ]
}
```

### Chart: pizzas sold by category
`make_chart` - ok in 0.0s

```json
{
 "status": "ok",
 "chart": "bar",
 "data": "data_2",
 "title": "sum of quantity by category",
 "groups": 4,
 "shown": 4,
 "largest": [
  [
   "Classic",
   14888.0
  ],
  [
   "Supreme",
   11987.0
  ],
  [
   "Veggie",
   11649.0
  ],
  [
   "Chicken",
   11050.0
  ]
 ],
 "top_share": 0.3003
}
```

### Is price different between sizes?
`stats_test` - ok in 0.1s

```json
{
 "status": "ok",
 "question": "difference",
 "data": "data_2",
 "test": "Kruskal-Wallis",
 "value_column": "price",
 "group_column": "size",
 "rows": 48620,
 "effect_size": 0.7585,
 "effect_measure": "epsilon_squared",
 "effect_label": "large",
 "p_value": 0.0,
 "groups": [
  {
   "group": "XXL",
   "rows": 28,
   "median": 35.95,
   "mean": 35.95
  },
  {
   "group": "XL",
   "rows": 544,
   "median": 25.5,
   "mean": 25.5
  },
  {
   "group": "L",
   "rows": 18526,
   "median": 20.5,
   "mean": 19.8026
  },
  {
   "group": "M",
   "rows": 15385,
   "median": 16.25,
   "mean": 15.9512
  },
  {
   "group": "S",
   "rows": 14137,
   "median": 12.0,
   "mean": 12.3643
  }
 ],
 "verdict": "The difference is larger than chance would explain (large effect).",
 "p_text": "p < 0.0001",
 "alpha": 0.05
}
```

### Are category and size associated?
`stats_test` - ok in 0.0s

```json
{
 "status": "ok",
 "question": "association",
 "data": "data_2",
 "test": "chi-square test of independence",
 "rows": 48620,
 "table_shape": [
  4,
  5
 ],
 "effect_size": 0.1515,
 "effect_measure": "cramers_v",
 "effect_label": "small",
 "p_value": 0.0,
 "most_over_represented": [
  {
   "category": "Classic",
   "size": "XL",
   "rows": 544,
   "expected": 163.1
  },
  {
   "category": "Classic",
   "size": "S",
   "rows": 5975,
   "expected": 4239.1
  },
  {
   "category": "Veggie",
   "size": "L",
   "rows": 5263,
   "expected": 4362.5
  }
 ],
 "verdict": "The association is larger than chance would explain (small effect).",
 "p_text": "p < 0.0001",
 "alpha": 0.05
}
```

### Forecast weekly revenue, 6 weeks
`forecast` - ok in 1.1s

```json
{
 "status": "ok",
 "data": "data_3",
 "value": "revenue",
 "combined_by": "sum",
 "period": "week",
 "history_points": 51,
 "horizon": 6,
 "model": "Theta",
 "beats_baseline": true,
 "backtest": "On past data the Theta model missed by 10% on average, vs 12% for repeating the last value.",
 "reliability": "strong",
 "forecast": [
  {
   "period": "2015-12-28",
   "forecast": 15088.7804,
   "low_80": 11773.7994,
   "high_80": 18403.7614
  },
  {
   "period": "2016-01-04",
   "forecast": 15067.6693,
   "low_80": 11752.6882,
   "high_80": 18382.6503
  },
  {
   "period": "2016-01-11",
   "forecast": 15046.5582,
   "low_80": 11731.5771,
   "high_80": 18361.5392
  },
  {
   "period": "2016-01-18",
   "forecast": 15025.4471,
   "low_80": 11710.466,
   "high_80": 18340.4281
  },
  {
   "period": "2016-01-25",
   "forecast": 15004.3359,
   "low_80": 11689.3549,
   "high_80": 18319.317
  },
  {
   "period": "2016-02-01",
   "forecast": 14983.2248,
   "low_80": 11668.2438,
   "high_80": 18298.2059
  }
 ],
 "total_over_horizon": 90216.0157,
 "notes": [
  "The last week (2015-12-28) is incomplete (data ends 2015-12-31), so it was left out.",
  "The first week (2014-12-29) is incomplete (data starts 2015-01-01), so it was left out.",
  "The range rests on only 18 past errors; treat it as rough."
 ]
}
```

## 2. Prediction: the marriage survey
### Add a yes/no target, saved as a table
`query_data` - ok in 0.1s

```json
{
 "status": "ok",
 "columns": [
  "rate_marriage",
  "age",
  "yrs_married",
  "children",
  "religious",
  "educ",
  "occupation",
  "occupation_husb",
  "affairs",
  "had_affair"
 ],
 "rows": [
  [
   3,
   22.0,
   2.5,
   0.0,
   2,
   17,
   2,
   6,
   3.1999998,
   "yes"
  ],
  [
   4,
   32.0,
   2.5,
   0.0,
   3,
   14,
   3,
   6,
   0.0,
   "no"
  ],
  [
   5,
   32.0,
   13.0,
   3.0,
   3,
   14,
   2,
   2,
   0.0,
   "no"
  ],
  [
   5,
   42.0,
   23.0,
   5.5,
   3,
   12,
   5,
   5,
   0.0,
   "no"
  ],
  [
   3,
   32.0,
   9.0,
   1.0,
   3,
   14,
   2,
   5,
   0.0,
   "no"
  ]
 ],
 "total_rows": 5093,
 "note": "Showing 5 of 5,093 rows.",
 "saved_as": "data_5"
}
```

### Train with every column (the leak check should object)
`train_model` - ok in 1.9s

```json
{
 "status": "ok",
 "model": "model_1",
 "data": "data_5",
 "target": "had_affair",
 "task": "classification",
 "rows": 5093,
 "tested_on": "5-fold cross-validation (each row predicted by a model that did not see it)",
 "model_type": "gradient boosting",
 "metric": "AUC",
 "score": 1.0,
 "baseline": 0.5,
 "scores_by_model": {
  "linear": 0.9955,
  "gradient boosting": 1.0
 },
 "better_than_baseline": true,
 "verdict": "Better than the baseline: a strong ranking (AUC 1.00; 0.5 = guessing).",
 "important_predictive_features": [
  {
   "column": "affairs",
   "share": 1.0
  }
 ],
 "excluded_columns": [],
 "positive_class": "yes",
 "warning": "affairs predict had_affair almost perfectly on their own: check they are not recorded after the outcome (a leak). If so, train again without them.",
 "note": "Important features are associated with the outcome; they are not proven causes."
}
```

### Train without the leaking column
`train_model` - ok in 2.2s

```json
{
 "status": "ok",
 "model": "model_2",
 "data": "data_5",
 "target": "had_affair",
 "task": "classification",
 "rows": 5093,
 "tested_on": "5-fold cross-validation (each row predicted by a model that did not see it)",
 "model_type": "linear",
 "metric": "AUC",
 "score": 0.7465,
 "baseline": 0.5,
 "scores_by_model": {
  "linear": 0.7465,
  "gradient boosting": 0.7421
 },
 "better_than_baseline": true,
 "verdict": "Better than the baseline: a useful but rough ranking (AUC 0.75; 0.5 = guessing).",
 "important_predictive_features": [
  {
   "column": "rate_marriage",
   "share": 0.407
  },
  {
   "column": "yrs_married",
   "share": 0.392
  },
  {
   "column": "age",
   "share": 0.097
  },
  {
   "column": "religious",
   "share": 0.079
  }
 ],
 "excluded_columns": [],
 "positive_class": "yes",
 "note": "Important features are associated with the outcome; they are not proven causes."
}
```

### Predict for new people
`predict` - ok in 0.0s

```json
{
 "status": "ok",
 "data": "data_7",
 "from": "data_6",
 "model": "model_2",
 "rows": 1273,
 "column": "predicted_had_affair",
 "average_probability": 0.3162,
 "predicted_counts": {
  "no": 1048,
  "yes": 225
 },
 "model_quality": "AUC 0.7465 vs 0.5 baseline, tested on 5-fold cross-validation (each row predicted by a model that did not see it)"
}
```
