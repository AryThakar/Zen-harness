"""calculate, read_file, the workspace listing and the sandbox path() helper."""

from __future__ import annotations

import pandas as pd
import pytest

from app.tools.calc import calculate
from app.tools.notes import save_note
from app.tools.read_file import PART_CHARS, read_file
from app.tools.sandbox import run_python
from app.tools.workspace import Workspace


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path / "ws")


@pytest.mark.parametrize("expr, value", [("2400 * 0.15", 360.0), ("2 ^ 10", 1024), ("1,200 * 2", 2400),
                                         ("round((5240000 - 4810000) / 4810000 * 100, 2)", 8.94),
                                         ("sqrt(16) + log10(1000)", 7.0)])
def test_calculate(expr, value):
    r = calculate(expr)
    assert r.status == "ok" and r.result["result"] == pytest.approx(value)


@pytest.mark.parametrize("expr", ["1 / 0", "__import__('os').getcwd()", "x + 1", "'a' * 3", "", "9 ** 9 ** 9"])
def test_calculate_refuses(expr):
    assert calculate(expr).status == "invalid_arguments"


def test_read_code_with_line_numbers_in_parts(ws):
    lines = [f"value_{i} = {i} * 2  # " + "x" * 60 for i in range(400)]
    fid = ws.add_bytes("file", "\n".join(lines).encode(), "py", name="big.py")
    first = read_file(ws, fid).result
    assert first["parts"] > 1 and first["text"].startswith("  1| value_0 = 0 * 2")
    assert len(first["text"]) <= PART_CHARS and "read part 2" in first["note"]
    last = read_file(ws, fid, part=first["parts"]).result
    assert last["text"].rstrip().endswith("value_399 = 399 * 2  # " + "x" * 60) and "note" not in last
    assert read_file(ws, fid, part=99).status == "invalid_arguments"


def test_read_documents_and_notes_without_numbers(ws):
    from docx import Document
    doc = Document()
    doc.add_heading("Refund policy", 1)
    doc.add_paragraph("Opened phones can be returned within 14 days.")
    path = ws.root / "policy.docx"
    doc.save(path)
    fid = ws.add_bytes("file", path.read_bytes(), "docx", name="policy.docx")
    text = read_file(ws, fid).result["text"]
    assert "returned within 14 days" in text and "1|" not in text
    note = save_note(ws, "Plan", "Clean data_1, then forecast.").result["note"]
    assert "forecast" in read_file(ws, note).result["text"]


def test_read_file_refuses_tables_and_binaries(ws):
    t = ws.add_table(pd.DataFrame({"a": [1]}), "t")
    assert "inspect_data" in read_file(ws, t).result["error"]["message"]
    b = ws.add_bytes("file", b"\x00\x01\x02binary", "bin", name="blob.bin")
    assert read_file(ws, b).status == "failed"


def test_listing_shows_ids_not_content(ws):
    assert ws.listing() == "Workspace: empty (no files attached yet)."
    t = ws.add_table(pd.DataFrame({c: [1, 2] for c in "abcdefghij"}), "sales.csv")
    ws.add_table(pd.DataFrame({"a": [1]}), "sales.csv (cleaned)", parent=t)
    ws.add_bytes("file", b"print(1)", "py", name="convert.py")
    listing = ws.listing()
    assert "data_1  sales.csv - 2 rows, 10 columns (a, b, c, d, e, f, g, h ...)" in listing
    assert "data_2  sales.csv (cleaned) - 1 rows, 1 columns (a), from data_1" in listing
    assert "file_1  convert.py - 8 bytes" in listing
    assert "older items not shown" in ws.listing(limit=1)


def test_sandbox_reads_stored_files_by_id(ws):
    fid = ws.add_bytes("file", b"alpha,beta\n1,2\n", "csv", name="raw.csv")
    r = run_python(ws, f"print(open(path('{fid}')).read().splitlines()[0])")
    assert r.result["output"].strip() == "alpha,beta"
    assert "No stored file" in run_python(ws, "path('file_9')").result["errors"]


def test_sandbox_open_understands_ids_and_original_names(ws):
    ws.add_bytes("file", b"alpha beta\n", "txt", name="story.txt")
    r = run_python(ws, "print(open('file_1').read().strip(), '|', open('story.txt').read().strip())")
    assert r.result["output"].strip() == "alpha beta | alpha beta"


def test_data_files_are_read_without_line_numbers(ws):
    fid = ws.add_bytes("file", b"name,age\nAsha,31\n", "csv", name="people.csv")
    assert read_file(ws, fid).result["text"] == "name,age\nAsha,31"


def test_known_table_problems_travel_with_results(ws, tmp_path):
    from app.tools.inspect_data import inspect_data
    from app.tools.query_data import query_data
    path = tmp_path / "t.csv"
    path.write_text("id,qty\n" + "\n".join(f"{i},1" for i in range(30)) + "\nTotal,30\n", encoding="utf-8")
    data = ws.add_file(path)
    assert "data_warning" not in query_data(ws, f"SELECT sum(qty) FROM {data}").result  # not inspected yet
    inspect_data(ws, data)
    warned = query_data(ws, f"SELECT sum(qty) FROM {data}").result
    assert "summary rows" in warned["data_warning"] and "clean_data" in warned["data_warning"]


def test_a_workspace_id_used_as_a_variable_gets_the_fix(ws):
    ws.add_table(pd.DataFrame({"a": [1, 2]}), name="t.csv")
    message = run_python(ws, "df = data_1.copy()").result["error"]["message"]
    assert "table('data_1')" in message and "to_parquet" in message
    assert "Workspace items" not in run_python(ws, "1 / 0").result["error"]["message"]


def test_an_id_given_to_a_library_that_opens_files_itself_gets_the_fix(ws):
    ws.add_table(pd.DataFrame({"a": [1, 2]}), name="t.csv")
    message = run_python(ws, "import pandas as pd; pd.read_parquet('data_1')").result["error"]["message"]
    assert "table('data_1')" in message  # pyarrow opens files itself, so the id is not resolved


def test_a_script_never_changes_a_stored_file(ws):
    fid = ws.add_bytes("file", b"ORIGINAL", "py", name="average.py")
    stored = ws.root / ws.entry(fid)["file"]
    code = f"print(open('{fid}').read())\nwith open('{fid}', 'w') as f:\n    f.write('CHANGED')"
    r = run_python(ws, code)
    assert r.result["output"].strip() == "ORIGINAL" and stored.read_text() == "ORIGINAL"
    assert r.result["files"][0]["id"] != fid  # the written copy is a new item
    again = run_python(ws, f"print(open('{fid}').read())")  # the id still means the stored file
    assert again.result["output"].strip() == "ORIGINAL"

