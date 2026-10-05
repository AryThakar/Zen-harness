"""The data analyst's report: kept on the table the task was about, shown first in the next task on it,
every section written and every number from a tool result. Driven by a scripted model."""

from __future__ import annotations

import pandas as pd
import pytest

from app.providers.base import ModelTurn
from app.providers.scripted import ScriptedProvider, call
from app.assistant.registry import ToolContext
from app.assistant.workers import PROFILES, run_worker
from app.tools.files import search_files
from app.tools.search_index import SearchIndex
from app.tools.workspace import Workspace

SECTIONS = {"question": "Which region sells most?", "data_used": "data_1 as it is; no cleaning needed.",
            "method": "Summed the amount per region with query_data.", "results": "South sells most: 200.25.",
            "caveats": "Only the regions in this table."}


@pytest.fixture
def ctx(tmp_path):
    ws = Workspace(tmp_path / "ws")
    seen = []
    c = ToolContext(ws=ws, index=SearchIndex(tmp_path / "ws" / "search.sqlite", None), conversation="c1",
                    emit=seen.append)
    c.seen = seen
    ws.add_table(pd.DataFrame({"region": ["N", "S"], "amount": [150.5, 200.25]}), "sales.csv")
    return c


def turn(*calls) -> ModelTurn:
    return ModelTurn(text="", tool_calls=list(calls), stop="tool_use")


def report(**fields) -> ModelTurn:
    return turn(call("save_report", **{**SECTIONS, **fields}))


SQL = "SELECT region, sum(amount) AS total FROM data_1 GROUP BY region ORDER BY total DESC"


def test_the_report_is_kept_on_the_table_and_main_gets_the_results(ctx):
    model = ScriptedProvider([turn(call("inspect_data", data="data_1")), turn(call("query_data", sql=SQL)), report()])
    out = run_worker(model, PROFILES["data"], "Which region sells most?", ctx, table="data_1")
    assert out["answer"] == "South sells most: 200.25." and out["table"] == "data_1" and "warnings" not in out
    kept = ctx.ws.report("data_1")
    assert kept == out["report"] and kept.startswith("# Which region sells most?\n")
    assert "## Data used and cleaning decisions\ndata_1 as it is" in kept and "## Results\nSouth sells most" in kept
    assert "- Table: sales.csv (data_1), report saved" in kept and "inspect_data" in kept.split("## Facts")[1]
    card = next(a for a in ctx.seen if a["type"] == "report")
    assert card["table"] == "data_1" and card["name"] == "sales.csv" and card["report"] == kept
    assert ctx.ws.lines()["data_1"].endswith("| Which region sells most?")
    assert search_files(ctx.index, "region sells most").result["passages"][0]["source"] == "the report on sales.csv (data_1)"


def test_the_next_task_on_the_table_starts_from_its_report(ctx):
    run_worker(ScriptedProvider([turn(call("query_data", sql=SQL)),
                                 report(data_used="Use data_1; the Total row was already removed.")]),
               PROFILES["data"], "Which region sells most?", ctx, table="data_1")
    model = ScriptedProvider([report(question="How many regions?", results="2 regions.", method="Read the report.")])
    run_worker(model, PROFILES["data"], "How many regions?", ctx, table="data_1")
    opening = model.requests[0]["messages"][0]["content"]
    assert "The table's latest report (from an earlier task on data_1)" in opening and "Total row was already removed" in opening
    assert ctx.ws.report("data_1").startswith("# How many regions?")  # the latest only


def test_every_section_and_every_number_is_checked_once(ctx):
    model = ScriptedProvider([turn(call("query_data", sql=SQL)),
                              report(caveats="", results="South sells 210.75."),  # empty section, invented number
                              report()])
    out = run_worker(model, PROFILES["data"], "Which region sells most?", ctx, table="data_1")
    told = model.requests[2]["messages"][-1]["content"]
    assert "caveats is empty" in told and "210.75" in told and "warnings" not in out


def test_without_a_named_table_the_report_goes_on_the_one_it_inspected(ctx):
    model = ScriptedProvider([turn(call("inspect_data", data="data_1")), turn(call("query_data", sql=SQL)), report()])
    out = run_worker(model, PROFILES["data"], "Which region sells most?", ctx)
    assert out["table"] == "data_1" and ctx.ws.report("data_1")


def test_tables_made_in_the_task_are_named_in_the_facts(ctx):
    model = ScriptedProvider([turn(call("inspect_data", data="data_1")),
                              turn(call("query_data", sql="SELECT * FROM data_1 WHERE amount > 160", save=True)),
                              report(data_used="Use data_2: the rows over 160.", results="1 row is over 160.")])
    out = run_worker(model, PROFILES["data"], "rows over 160", ctx, table="data_1")
    assert "- Tables made: data_2 (" in out["report"] and "from data_1)" in out["report"]
