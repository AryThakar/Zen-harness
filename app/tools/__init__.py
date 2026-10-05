"""The assistant's tools: plain functions with direct tests, no model involved.

Every tool returns a ToolResult (core.py): a status, a compact result for the model and an optional
artifact for the UI. Data lives in one Workspace (workspace.py) - the sandbox: every table, image,
note and file has an id (data_1, image_1, note_1, file_1), and every tool takes an id or a name
(Workspace.resolve). Documents, notes and past chats share one search index (search_index.py: SQLite
FTS5 + local embeddings, fused by rank). Deleting, downloading and replacing the user's file wait for
the user's click (approval.py); scripts have no network (sandbox.py).

main's own:     files (file_ops.py, read_file.py), save_note (notes.py), get_weather (weather.py),
                generate_image (image.py), draw_diagram (diagram.py), calculate (calc.py),
                ask_user (ask.py), view_image (vision.py)
web:            web_search, read_webpage (web.py)
knowledge:      search_files (files.py), recall (memory.py), read_file
data:           inspect_data, clean_data, query_data (+ sql_guard.py), make_chart, compare, forecast,
                predict (ml.py), find_patterns, analyze_text; column roles (roles.py), periods.py
code:           run_code, fix_code (scratch.py on sandbox.py); reports.py for the reports kept on items
"""
