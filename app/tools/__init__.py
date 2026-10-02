"""The assistant's tools: plain functions with direct tests, no model involved.

Every tool returns a ToolResult (core.py): a status, a compact result for the model and an
optional artifact for the UI. Data lives in one Workspace (workspace.py): every table, model
and file has an id (data_1, model_1 ...), and tools take ids, never file names.

Data and ML:  inspect_data, clean_data, query_data, make_chart, stats_test,
              train_model, predict, forecast
"""
