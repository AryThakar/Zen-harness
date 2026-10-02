"""The assistant's tools: plain functions with direct tests, no model involved.

Every tool returns a ToolResult (core.py): a status, a compact result for the model and an
optional artifact for the UI. Data lives in one Workspace (workspace.py): every table, model,
image, note and file has an id (data_1, model_1, image_1, note_1, file_1), and tools take ids,
never file names. Documents, notes and past chats share one search index (search_index.py:
SQLite FTS5 + local embeddings, fused by rank).

General:        web_search, read_webpage (web.py), get_weather (weather.py),
                generate_image (image.py), search_files (files.py), recall (memory.py),
                run_python (sandbox.py), draw_diagram (diagram.py)
With the user:  ask_user (ask.py), save_note (notes.py)
Data and ML:    inspect_data, clean_data, query_data, make_chart, stats_test,
                train_model / predict (ml.py), forecast
"""
