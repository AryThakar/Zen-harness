"""The shared contracts every tool and helper relies on (docs/ARCHITECTURE.md, section 6): one resolver
for item names, errors that keep their message, and the other system-wide rules as they land."""

from __future__ import annotations

import pandas as pd
import pytest

from app.assistant.registry import ToolContext, Tool, TaskIn, execute
from app.tools.core import ToolError, ToolResult
from app.tools.search_index import SearchIndex
from app.tools.workspace import Workspace


@pytest.fixture
def ctx(tmp_path):
    ws = Workspace(tmp_path / "ws")
    ws.add_table(pd.DataFrame({"region": ["N", "S"], "amount": [1.0, 2.0]}), "shop_sales.csv")
    ws.add_bytes("file", b"print(1)\n", "py", name="squares.py")
    return ToolContext(ws=ws, index=SearchIndex(tmp_path / "ws" / "search.sqlite", None))


@pytest.mark.parametrize("ref", ["data_1", "shop_sales.csv", "SHOP_SALES.CSV", "data_1 shop_sales.csv",
                                 "shop_sales.csv (data_1)"])
def test_every_data_tool_takes_an_id_a_name_or_a_copied_line(ctx, ref):
    line = ctx.ws.lines()["data_1"]
    for given in (ref, line):
        result = execute(ctx, "inspect_data", {"data": given})
        assert result.status == "ok" and result.result["data"] == "data_1", (given, result.result)


def test_a_wrong_name_gets_the_closest_items_and_a_wrong_kind_says_what_reads_it(ctx):
    near = execute(ctx, "inspect_data", {"data": "shop_sale.csv"}).result["error"]["message"]
    assert near == "No table 'shop_sale.csv'. Did you mean: data_1 (shop_sales.csv)?"
    wrong = execute(ctx, "inspect_data", {"data": "squares.py"}).result["error"]["message"]
    assert wrong.startswith("'squares.py' is file_1 (squares.py), a file, not a table.") and "files(read)" in wrong
    table = execute(ctx, "read_file", {"file": "shop_sales.csv"}).result  # not text: says what reads it
    assert table["kind"] == "table" and "table('data_1')" in table["note"]
    assert execute(ctx, "read_file", {"file": "squares.py"}).result["file"] == "file_1"


def test_an_error_raised_outside_a_tool_function_keeps_its_message(ctx):
    """Measured: the coder's 'is an image, not a file' reached main as 'internal error (ToolError)'."""
    def refuse(c, a):
        raise ToolError("ary_on_earth.png is an image, not a file the coder can change.", field="file")

    def crash(c, a):
        raise KeyError("x")

    tools = {"helper": Tool("helper", "", TaskIn, refuse), "buggy": Tool("buggy", "", TaskIn, crash)}
    said = execute(ctx, "helper", {"task": "x"}, tools)
    assert said.status == "invalid_arguments" and said.result["error"]["message"].startswith("ary_on_earth.png is an image")
    bug = execute(ctx, "buggy", {"task": "x"}, tools)
    assert bug.status == "failed" and "internal error (KeyError)" in bug.result["error"]["message"]


def test_every_image_is_an_image_item_with_its_size(ctx, tmp_path):
    """Uploads were stored as plain files; script images were listed as 'image NonexNone'."""
    from PIL import Image

    from app.tools.files import store_file
    photo = tmp_path / "earth.jpg"
    Image.new("RGB", (64, 48), "navy").save(photo)
    kept = store_file(ctx.ws, ctx.index, photo)
    assert kept["kind"] == "image" and ctx.ws.lines()[kept["id"]].endswith("earth.jpg - image 64x48")
    fake = tmp_path / "fake.png"
    fake.write_bytes(b"not an image")
    with pytest.raises(ToolError, match="not a readable image"):
        store_file(ctx.ws, ctx.index, fake)


def test_analyze_data_resolves_its_table_too(ctx):
    seen = {}

    def handler(c, a):
        seen["data"] = a.data
        return ToolResult("ok", {})

    from app.assistant.registry import DataTaskIn
    execute(ctx, "analyze_data", {"task": "x", "data": "shop_sales.csv"},
            {"analyze_data": Tool("analyze_data", "", DataTaskIn, handler)})
    assert seen["data"] == "data_1"


def test_one_workspace_shared_by_threads_never_reuses_an_id(tmp_path):
    """Read in the code: the turn thread and the API's requests share one Workspace; making an id and
    saving the registry weren't guarded, so two items made at once could get the same id."""
    import threading

    ws = Workspace(tmp_path / "shared")
    made, errors = [], []

    def add(n: int) -> None:
        try:
            for i in range(25):
                made.append(ws.add_bytes("file", b"x", "txt", name=f"t{n}_{i}.txt"))
                ws.lines(), ws.ids("file")  # readers walk the registry while others change it
        except Exception as exc:  # noqa: BLE001 - any crash is the failure
            errors.append(exc)

    threads = [threading.Thread(target=add, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(made) == len(set(made)) == 200
    assert sorted(Workspace(tmp_path / "shared").ids("file")) == sorted(made)  # the saved registry has them all
