"""The assistant's tools: plain functions with direct tests, no model involved.

Every tool returns a ToolResult (core.py): a status, a compact result for the model and an optional
artifact for the UI. Data lives in one Workspace (workspace.py): a folder of real files, where a file's
path is its one name for everyone, written through one write path (unique names, versions, identical uploads
recognised); every tool takes a file's name (Workspace.resolve), and what a model reads names files by path
(workspace.present). The files and past chats share one search index (search_index.py: SQLite FTS5 +
local embeddings, fused by rank), kept in step with the folder. Deleting, downloading and changing the user's
file wait for the user's click (approval.py); scripts have no network (sandbox.py).

main's own:     files (file_ops.py, read_file.py; find: files.py), get_weather (weather.py),
                generate_image (image.py), draw_diagram (diagram.py), calculate (calc.py),
                ask_user (ask.py), view_image (vision.py)
web:            web_search, read_webpage (web.py)
knowledge:      files (find, read), recall (memory.py)
engineer:       inspect_data, clean_data, query_data (+ sql_guard.py), make_chart, compare, forecast,
                predict (ml.py), find_patterns, analyze_text; column roles (roles.py), periods.py;
                files (find, read, write, edit) and run (run.py on sandbox.py); reports.py for a table's report
Which kind of file a name is - table, document, image, code, binary - is defined once, in filetypes.py.
agent.md, the project's standing memory in view of every model, is agent_md.py.
"""
