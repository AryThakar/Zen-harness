"""Direct tests of the workspace, inspect_data and clean_data - no model involved."""

from __future__ import annotations

import pandas as pd
import pytest

from app.tools.clean_data import clean_data
from app.tools.inspect_data import inspect_data
from app.tools.workspace import Workspace

MESSY = """Monthly export
order_id,Order Date,Region,Amount,Units,Note
1001,03/04/2025,North,"₹1,200.50",2,
1002,05/04/2025,north ,₹800,1,gift
1003,07/04/2025,South,₹450,1,
1003,07/04/2025,South,₹450,1,
1004,09/04/2025,East,abc,3,
1005,11/04/2025,South,"₹99,000",1,
"""


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path / "ws")


@pytest.fixture
def messy(ws, tmp_path):
    path = tmp_path / "orders.csv"
    path.write_text(MESSY, encoding="utf-8")
    return ws.add_file(path)


def test_add_file_types_only_what_reads_cleanly(ws, messy):
    assert messy == "data_1"
    df = ws.table(messy)
    assert list(df.columns) == ["order_id", "order_date", "region", "amount", "units", "note"]
    assert str(df["units"].dtype) == "Int64" and str(df["order_id"].dtype) == "Int64"
    # 'abc' keeps amount as text; ambiguous dd/mm vs mm/dd keeps the dates as text
    assert df["amount"].dtype == object and df["order_date"].dtype == object
    notes = ws.column_notes(messy)
    assert notes["amount"] == {"type": "text"}  # inspect_data reports it as mostly numbers
    assert notes["order_date"]["pending"]["ambiguous_order"] is True
    assert df["note"].isna().sum() == 5  # empty cells are missing, not ''


def test_registry_survives_reopening(ws, messy, tmp_path):
    again = Workspace(tmp_path / "ws")
    assert again.ids("table") == ["data_1"]
    assert again.table("data_1").shape == (6, 6)
    with pytest.raises(Exception):
        again.table("data_9")


def test_inspect_reports_and_suggests_without_changing(ws, messy):
    r = inspect_data(ws, messy)
    assert r.status == "ok"
    res = r.result
    assert res["rows"] == 6 and res["duplicate_rows"] == 1
    cols = {c["name"]: c for c in res["columns"]}
    assert cols["amount"]["almost"]["as"] == "number"
    assert cols["region"]["spelling_variants"] == [["North", "north"]] or cols["region"]["spelling_variants"]
    ops = [s["op"] for s in res["suggested_steps"]]
    assert ops[0] == "drop_duplicates" and "parse_numbers" in ops and "tidy_text" in ops
    # the date order is a question with one ready step per option, not a guess
    (q,) = res["questions"]
    assert q["column"] == "order_date"
    assert [o["step"]["order"] for o in q["options"]] == ["dayfirst", "monthfirst"]
    assert ws.ids("table") == ["data_1"]  # nothing saved


def test_suggested_steps_run_as_given(ws, messy):
    res = inspect_data(ws, messy).result
    steps = res["suggested_steps"] + [res["questions"][0]["options"][0]["step"]]
    r = clean_data(ws, messy, steps)
    assert r.status == "ok", r.result
    df = ws.table(r.result["data"])
    assert r.result == {**r.result, "data": "data_2", "from": "data_1", "rows_before": 6, "rows_after": 5}
    assert df["order_date"].dt.month.tolist() == [4] * 5  # day first: 3 April ... 11 April
    assert df["amount"].tolist()[:3] == [1200.5, 800.0, 450.0] and pd.isna(df["amount"].iloc[3])
    assert set(df["region"]) == {"North", "South", "East"}
    assert ws.column_notes("data_2")["amount"]["unit"] == "₹"
    assert any("unreadable became missing (e.g. 'abc')" in line for line in r.result["log"])
    assert ws.table("data_1").shape == (6, 6)  # the original is untouched


def test_destructive_steps_are_explicit(ws, messy):
    base = clean_data(ws, messy, [{"op": "parse_numbers", "columns": ["amount"]}]).result["data"]
    flagged = ws.table(clean_data(ws, base, [{"op": "flag_outliers", "columns": ["amount"]}]).result["data"])
    assert len(flagged) == 6 and flagged["amount_outlier"].sum() == 1  # 99,000 is marked, still there
    r = clean_data(ws, base, [{"op": "remove_outliers", "columns": ["amount"]}])
    assert r.result["rows_after"] == 5
    r = clean_data(ws, base, [{"op": "fill_missing", "columns": ["amount"], "method": "median"}])
    assert "1 filled with median 800" in r.result["log"][0]


def test_invalid_step_saves_nothing(ws, messy):
    before = ws.ids()
    r = clean_data(ws, messy, [{"op": "drop_duplicates"}, {"op": "fill_missing", "columns": ["region"], "method": "mean"}])
    assert r.status == "invalid_arguments"
    assert "Step 2 (fill_missing)" in r.result["error"]["message"] and "Nothing was saved" in r.result["error"]["message"]
    assert ws.ids() == before
    r = clean_data(ws, messy, [{"op": "explode"}])
    assert r.status == "invalid_arguments"
    r = clean_data(ws, messy, [{"op": "drop_columns", "columns": ["nope"]}])
    assert r.status == "invalid_arguments" and "Columns:" in r.result["error"]["message"]


def test_ambiguous_dates_need_an_order(ws, messy):
    r = clean_data(ws, messy, [{"op": "parse_dates", "columns": ["Order Date"]}])
    assert r.status == "invalid_arguments" and "dayfirst" in r.result["error"]["message"]
    r = clean_data(ws, messy, [{"op": "parse_dates", "columns": ["Order Date"], "order": "monthfirst"}])
    assert ws.table(r.result["data"])["order_date"].dt.month.tolist()[:2] == [3, 5]


def test_rename_and_tidy(ws, messy):
    r = clean_data(ws, messy, [{"op": "tidy_text", "columns": ["region"]},
                               {"op": "rename_columns", "mapping": {"region": "area"}}])
    df = ws.table(r.result["data"])
    assert "area" in df and df["area"].value_counts()["South"] == 3
    r = clean_data(ws, messy, [{"op": "rename_columns", "mapping": {"region": "units"}}])
    assert r.status == "invalid_arguments"


def test_summary_row_is_found_and_dropped_first(ws, tmp_path):
    path = tmp_path / "with_total.csv"
    rows = "\n".join(f"{i},North,{10 + i}" for i in range(1, 41))
    path.write_text(f"id,region,amount\n{rows}\nTotal,,1220\n", encoding="utf-8")
    data = ws.add_file(path)
    res = inspect_data(ws, data).result
    first = res["suggested_steps"][0]
    assert first["op"] == "drop_rows" and first["column"] == "id" and first["values"] == ["Total"]
    parse = next(s for s in res["suggested_steps"] if s["op"] == "parse_numbers")
    assert parse["why"] == "id holds numbers stored as text"  # once the Total row is gone, nothing is lost
    r = clean_data(ws, data, res["suggested_steps"])
    df = ws.table(r.result["data"])
    assert len(df) == 40 and df["amount"].sum() == 1220 and str(df["id"].dtype) == "Int64"
