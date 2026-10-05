"""calculate, read_file, the workspace listing and the sandbox path() helper."""

from __future__ import annotations

import pandas as pd
import pytest

from app.tools.calc import calculate
from app.tools.notes import save_note
from app.tools.read_file import HELPER_CHARS, read_file
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


# Written by the real model in bench/main/calc_probe (its own notation).
@pytest.mark.parametrize("expr, value", [("10!", 3628800), ("12! / (3! * 9!)", 220), ("(2+3)!", 120),
                                         ("sin(30 degrees)", 0.5), ("tan(35°)$", 0.7002075382),
                                         ("tan(35\\u00b0)", 0.7002075382), ("asin(0.5) * 180 / pi", 30),
                                         ("ln(50)", 3.9120230054), ("log2(1024)", 10), ("e^-0.5", 0.6065306597),
                                         ("2197^(1/3)", 13), ("(pi * ln(2)) / 8", 0.2721982613)])
def test_calculate_takes_the_models_notation(expr, value):
    r = calculate(expr)
    assert r.status == "ok" and r.result["result"] == pytest.approx(value) and "note" not in r.result


def test_a_plain_angle_is_said_to_be_radians():
    r = calculate("tan(35)")  # meant as degrees in the probe: the note lets the model correct it
    assert r.result["result"] == pytest.approx(0.4738147204) and "35°" in r.result["note"]
    assert "note" not in calculate("sin(pi/6)").result
    assert calculate("1001!").status == "invalid_arguments" and calculate("3 != 4").status == "invalid_arguments"


def test_read_code_with_line_numbers_from_a_line_on(ws):
    lines = [f"value_{i} = {i} * 2  # " + "x" * 60 for i in range(400)]
    fid = ws.add_bytes("file", "\n".join(lines).encode(), "py", name="big.py")
    first = read_file(ws, fid).result
    assert first["text"].startswith("  1| value_0 = 0 * 2") and len(first["text"]) <= HELPER_CHARS
    shown = int(first["lines"].split("-")[1].split()[0])
    assert first["next"] == f"read_file(file='big.py', start={shown + 1})"
    last = read_file(ws, fid, start=390).result
    assert last["text"].rstrip().endswith("value_399 = 399 * 2  # " + "x" * 60) and last["next"] == "end of file"
    assert read_file(ws, fid, start=999).status == "invalid_arguments"


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


def test_reading_what_isnt_text_says_which_tool_reads_it(ws):
    import io

    from PIL import Image
    t = ws.add_table(pd.DataFrame({"a": [1]}), "t")
    said = read_file(ws, t).result  # what it is, and what reads it instead
    assert said["kind"] == "table" and "table('data_1')" in said["note"]
    buf = io.BytesIO()
    Image.new("RGB", (40, 20), "red").save(buf, format="PNG")
    img = ws.add_image(buf.getvalue(), "red.png")
    note = read_file(ws, img).result["note"]
    assert note.startswith("An image (40x20)") and f"view_image(image='{img}'" in note
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


def test_a_workspace_id_queried_with_duckdb_in_a_script_gets_the_fix(ws):
    ws.add_table(pd.DataFrame({"a": [1, 2]}), name="t.csv")
    message = run_python(ws, "import duckdb\nduckdb.query('SELECT * FROM data_1').df()").result["error"]["message"]
    assert "df = table('data_1')" in message


def test_an_id_given_to_a_library_that_opens_files_itself_gets_the_fix(ws):
    ws.add_table(pd.DataFrame({"a": [1, 2]}), name="t.csv")
    message = run_python(ws, "import pandas as pd; pd.read_parquet('data_1')").result["error"]["message"]
    assert "table('data_1')" in message  # pyarrow opens files itself, so the id is not resolved


def test_a_script_never_changes_a_stored_file(ws):
    fid = ws.add_bytes("file", b"ORIGINAL", "py", name="average.py")
    note = save_note(ws, "Plan", "Forecast by month.").result["note"]
    table = ws.add_table(pd.DataFrame({"a": [1]}), "t.csv")
    for item in (fid, note, table):  # by id: refused, with what to do instead
        r = run_python(ws, f"open('{item}', 'w').write('x')")
        assert r.status == "invalid_arguments" and "write the result under a new file name" in r.result["errors"]
    by_name = run_python(ws, "print(open('average.py').read())\nopen('average.py', 'w').write('CHANGED')")
    assert by_name.result["output"].strip() == "ORIGINAL"
    assert ws.path(fid).read_bytes() == b"ORIGINAL" and by_name.result["files"][0]["id"] != fid  # a new file
    assert "Forecast by month" in read_file(ws, note).result["text"] and ws.table(table)["a"].tolist() == [1]


def test_env_file_fills_the_environment_without_overriding(tmp_path, monkeypatch):
    from app.config import load_env_file
    env = tmp_path / ".env"
    env.write_text('# secrets\nCC_TEST_KEY="sk_abc"\nexport CC_TEST_OTHER = plain\nCC_TEST_SET=from-file\nnot a line\n')
    monkeypatch.delenv("CC_TEST_KEY", raising=False)
    monkeypatch.delenv("CC_TEST_OTHER", raising=False)
    monkeypatch.setenv("CC_TEST_SET", "from-environment")
    load_env_file(env)
    import os
    assert os.environ["CC_TEST_KEY"] == "sk_abc" and os.environ["CC_TEST_OTHER"] == "plain"
    assert os.environ["CC_TEST_SET"] == "from-environment"  # a variable already set wins
    load_env_file(tmp_path / "missing.env")  # no file: nothing happens
    monkeypatch.delenv("CC_TEST_KEY")
    monkeypatch.delenv("CC_TEST_OTHER")
