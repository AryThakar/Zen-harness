"""Charts as files (docs/REBUILD_PLAN.md, phase 3): a chart - from make_chart, forecast or a script's chart() - is one
chart file the page draws live; its picture is drawn from that file only when a document or a download needs it."""

from __future__ import annotations

import io
import json

import pandas as pd
import pytest

from app.assistant.registry import ToolContext, execute
from app.tools.chart_image import picture_of, spec_of
from app.tools.file_ops import files
from app.tools.search_index import SearchIndex
from app.tools.workspace import Workspace


@pytest.fixture
def ctx(tmp_path):
    ws = Workspace(tmp_path / "ws")
    seen = []
    c = ToolContext(ws=ws, index=SearchIndex(tmp_path / "ws" / "search.sqlite", None), emit=seen.append)
    c.seen = seen
    ws.add_table(pd.DataFrame({"age": [5, 20, 30, 41, 60, 22, 35, 8], "survived": list("ynynnyyn"),
                               "month": pd.date_range("2025-01-01", periods=8, freq="MS"),
                               "sales": [10, 12, 9, 14, 15, 13, 18, 20]}), "titanic.csv")
    return c


def test_a_chart_is_one_chart_file_and_no_picture_copy(ctx):
    r = execute(ctx, "make_chart", {"data": "titanic.csv", "type": "line", "x": "month", "y": "sales"})
    key = r.result["chart_file"]
    assert ctx.ws.label(key) == "charts/sum-of-sales-by-month.chart.json" and ctx.ws.ids("image") == []
    assert ctx.ws.lines()[key] == "charts/sum-of-sales-by-month.chart.json: chart (line, 8 points), from titanic.csv"
    card = next(a for a in ctx.seen if a["type"] == "chart")
    assert card["id"] == key and spec_of(ctx.ws, key)["y"] == card["y"]  # the card and the file: one chart
    again = execute(ctx, "make_chart", {"data": "titanic.csv", "type": "line", "x": "month", "y": "sales"})
    assert again.result["chart_file"] == key and len(ctx.ws.versions(key)) == 1  # the same chart again: its next version
    read = r.for_model(lambda body: __import__("app.tools.workspace", fromlist=["present"]).present(ctx.ws, body))
    assert '"chart_file": "charts/sum-of-sales-by-month.chart.json"' in read  # the model reads the path


def test_a_histogram_split_by_group_and_an_area_chart(ctx):
    """Measured: 'age of survivors vs the rest' was refused, so it was drawn with seaborn - a third style."""
    h = execute(ctx, "make_chart", {"data": "titanic.csv", "type": "histogram", "x": "age", "by": "survived"})
    spec = spec_of(ctx.ws, h.result["chart_file"])
    assert sorted(s["name"] for s in spec["series"]) == ["n", "y"] and all(len(s["y"]) == len(spec["x"]) for s in spec["series"])
    assert sum(sum(s["y"]) for s in spec["series"]) == 8 and h.result["groups"]["y"]["values"] == 4
    area = execute(ctx, "make_chart", {"data": "titanic.csv", "type": "area", "x": "month", "y": "sales"})
    assert area.status == "ok" and spec_of(ctx.ws, area.result["chart_file"])["chart"] == "area"
    assert picture_of(ctx.ws, h.result["chart_file"]).read_bytes()[:4] == b"\x89PNG"  # drawn when needed
    assert picture_of(ctx.ws, area.result["chart_file"]) is not None


def test_a_programs_chart_is_a_live_chart_too(ctx):
    files(ctx.ws, ctx.index, "write", name="sine.py", helper=True,
          content="import math\nxs = [i / 10 for i in range(20)]\nchart('line', x=xs, y=[math.sin(v) for v in xs], title='Sine')\n")
    r = execute(ctx, "run", {"file": "sine.py"})
    assert r.result["files"] == [{"path": "charts/sine.chart.json", "kind": "chart"}]
    card = next(a for a in ctx.seen if a["type"] == "chart")  # shown as make_chart's are
    assert card["title"] == "Sine" and card["tool"] == "run" and ctx.ws.label(card["id"]) == "charts/sine.chart.json"
    bad = files(ctx.ws, ctx.index, "write", name="bad.py", helper=True, content="chart('line', x=[1, 2], y=[1])\n")
    assert "x has 2 values but y has 1" in execute(ctx, "run", {"file": "bad.py"}).result["errors"]
    assert bad.status == "ok"


def test_a_document_takes_a_charts_picture(ctx):
    from docx import Document
    key = execute(ctx, "make_chart", {"data": "titanic.csv", "type": "bar", "x": "survived"}).result["chart_file"]
    name = ctx.ws.label(key)
    made = files(ctx.ws, ctx.index, "write", name="report.docx", content=f"# Report\n\n![Survivors]({name})\n")
    assert made.status == "ok", made.result
    doc = Document(io.BytesIO(ctx.ws.path(made.result["file"]).read_bytes()))
    assert len(doc.inline_shapes) == 1
    missing = files(ctx.ws, ctx.index, "write", name="r2.docx", content="![x](charts/nope.chart.json)\n")
    assert missing.status == "invalid_arguments" and name in missing.result["error"]["message"]  # what there is
    assert json.loads(ctx.ws.path(key).read_text())["chart"] == "bar"


def test_a_box_charts_picture_names_its_axes_as_drawn(ctx):
    """Measured: the box picture lies down (values along the bottom) but titled the bottom axis with the groups."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from app.tools import chart_image
    key = execute(ctx, "make_chart", {"data": "titanic.csv", "type": "box", "x": "survived", "y": "age"}).result["chart_file"]
    spec = spec_of(ctx.ws, key)
    fig, ax = plt.subplots()
    chart_image._box(ax, spec)
    chart_image._style(ax, spec)
    assert (ax.get_xlabel(), ax.get_ylabel()) == (spec["y_label"], spec["x_label"])
    plt.close(fig)
    assert picture_of(ctx.ws, key).name.endswith(f".v{chart_image.DRAWING}.png")  # a new drawing is not served stale


@pytest.mark.parametrize("kind, x, y, by", [("bar", "survived", "sales", None), ("line", "month", "sales", "survived"),
                                            ("box", "survived", "age", None), ("scatter", "age", "sales", None)])
def test_a_charts_numbers_download_as_a_table_with_every_point(ctx, kind, x, y, by):
    """2.13: a chart downloads as its picture and as its numbers (CSV) - each point drawn is a row."""
    from app.tools.chart_image import table_of
    key = execute(ctx, "make_chart", {"data": "titanic.csv", "type": kind, "x": x, "y": y,
                                      **({"by": by} if by else {})}).result["chart_file"]
    spec = spec_of(ctx.ws, key)
    columns, rows = table_of(spec)
    assert len(columns) >= 2 and all(len(r) == len(columns) for r in rows)
    assert len(rows) == len(spec.get("x") or [])  # a category, period or box a row
    assert len(columns) == 1 + (len(spec["series"]) if spec.get("series") else 5 if kind == "box" else 1)
