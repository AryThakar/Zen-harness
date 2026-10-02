# Worker benchmark - all

Local model: Gemma 4 E4B (Q4_K_XL + MTP draft) on llama-server, worker slot, thinking on, RTX 3050 Laptop 4 GB.

| role | passed | median seconds | model calls |
|---|---|---|---|
| web | 7/7 | 11.9 | 3.3 avg |

**Total: 7/7**

| task | result | seconds | tools | detail |
|---|---|---|---|---|
| w_duckdb_license | pass | 11.3 | web_search, read_webpage | expects ('MIT',); sources ['https://duckdb.org/faq'] |
| w_statsforecast | pass | 10.7 | web_search, read_webpage | expects ('Nixtla',); sources ['https://github.com/Nixtla/statsforecast'] |
| w_llama_port | pass | 14.4 | web_search, read_webpage | expects ('8080',); sources ['https://llama.app/docs/serve'] |
| w_parquet | pass | 11.9 | web_search, read_webpage | expects ('pyarrow', 'fastparquet'); sources ['https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.read_parquet.html'] |
| h_pypi | pass | 8.6 | web_search, read_webpage | expects ('Python Software Foundation',); sources ['https://en.wikipedia.org/wiki/Python_Package_Index'] |
| h_duckdb_lang | pass | 33.4 | web_search, read_webpage, web_search, read_webpage | expects ('C++',); sources ['https://duckdb.org/docs/current/dev/building/overview'] |
| h_hgb | pass | 14.4 | web_search, read_webpage | expects ('HistGradientBoostingClassifier',); sources ['https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.HistGradientBoostingClassifier.html'] |

## Answers

**w_duckdb_license** (pass): DuckDB is fully open-source under the MIT license. The DuckDB Foundation's statutes ensure that DuckDB remains open-source under the MIT license in perpetuity.

**w_statsforecast** (pass): The `statsforecast` Python library is developed by Nixtla.  Source: https://github.com/Nixtla/statsforecast

**w_llama_port** (pass): llama.cpp's `llama-server` listens on the default port **8080**.  This is specified in the documentation: *   "By default the server listens on http://127.0.0.1:8080." *   The command-line flag `--port` (or `--host, --port`) specifies the bind address and port, with the default being `127.0.0.1:8080`.  Source: *   https://llama.app/docs/serve

**w_parquet** (pass): The `pandas.read_parquet` function can use the following engines:  *   **'auto'**: This is the default setting. If 'auto' is used, pandas attempts to use 'pyarrow' first, and if 'pyarrow' is unavailable, it falls back to 'fastparquet'. *   **'pyarrow'**: Uses the PyArrow engine. *   **'fastparquet'**: Uses the FastParquet engine.  Source: https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.read_parquet.html

**h_pypi** (pass): The Python Package Index (PyPI) is run by the Python Software Foundation.  Source: - https://en.wikipedia.org/wiki/Python_Package_Index

**h_duckdb_lang** (pass): Based on the documentation for building DuckDB from source, the core language used for DuckDB is C++.  The documentation states that building DuckDB requires "a C++11-compliant compiler (e.g., GCC, Apple-Clang, MSVC)" and notes that "DuckDB v2.0 will require C++17."  Source: * https://duckdb.org/docs/current/dev/building/overview

**h_hgb** (pass): The class in scikit-learn that implements histogram-based gradient boosting for classification is `sklearn.ensemble.HistGradientBoostingClassifier`. This estimator is described as a "Histogram-based Gradient Boosting Classification Tree" and is noted for being much faster than `GradientBoostingClassifier` for large datasets (n_samples >= 10 000).
