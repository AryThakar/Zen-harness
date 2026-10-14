"""The workspace as one folder of real files (docs/REBUILD_PLAN.md, phase 1): a file's path is its one name for
every reader, one write path keeps it unique and versioned, the search index follows the folder, and a workspace
of the first layout is moved into this one with nothing lost."""

from __future__ import annotations

import io
import json

import pandas as pd
import pytest
from PIL import Image

from app.assistant.registry import TOOLS, ToolContext, execute
from app.tools.files import find_files, index_items, store_file
from app.tools.sandbox import run_python
from app.tools.search_index import SearchIndex
from app.tools.workspace import ITEM_REF, Workspace, present

NETWORK = {"web_search", "read_webpage", "get_weather", "generate_image", "view_image"}  # not run offline


def _png(color="red", size=(8, 8)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "PNG")
    return out.getvalue()


@pytest.fixture
def ctx(tmp_path):
    ws = Workspace(tmp_path / "ws")
    return ToolContext(ws=ws, index=SearchIndex(tmp_path / "ws" / "search.sqlite", None))


def test_a_path_is_one_file_for_every_reader(ctx, tmp_path):
    upload = tmp_path / "sales.csv"
    upload.write_text("region,amount\nNorth,10\nSouth,20\n")
    kept = store_file(ctx.ws, ctx.index, upload)  # the user's upload
    key = kept["id"]
    assert kept["name"] == "sales.csv" and (ctx.ws.files / "sales.csv").read_bytes() == upload.read_bytes()
    line = ctx.ws.lines()[key]
    for given in ("sales.csv", "SALES.csv", line):  # main and the helpers: by name, any case, a copied line
        assert ctx.ws.resolve(given, ("table",)) == key
    asked = execute(ctx, "query_data", {"sql": 'SELECT sum(amount) AS total FROM "sales.csv"'})
    assert asked.result["rows"] == [[30]]
    ran = run_python(ctx.ws, "import pandas as pd\nprint(pd.read_csv('sales.csv')['amount'].sum())")
    assert ran.result["output"].strip() == "30"  # a script: the same file, by the same name
    index_items(ctx.ws, ctx.index)
    assert find_files(ctx.ws, ctx.index, "sales region").result["files"][0]["path"] == "sales.csv"


def test_an_identical_upload_is_the_file_already_here_and_a_new_one_gets_its_own_name(ctx, tmp_path):
    first = tmp_path / "a" / "photo.png"
    first.parent.mkdir()
    first.write_bytes(_png())
    again = tmp_path / "b" / "image.png"  # the same picture pasted again, under another name
    again.parent.mkdir()
    again.write_bytes(_png())
    one = store_file(ctx.ws, ctx.index, first)
    two = store_file(ctx.ws, ctx.index, again)
    assert two["id"] == one["id"] and two["same"] and ctx.ws.ids("image") == [one["id"]]
    other = tmp_path / "c" / "photo.png"
    other.parent.mkdir()
    other.write_bytes(_png("blue"))
    three = store_file(ctx.ws, ctx.index, other)
    assert three["name"] == "photo (2).png" and "same" not in three


def test_a_change_keeps_the_version_before_and_a_restore_brings_it_back(ctx):
    key = ctx.ws.add_bytes("file", b"one", "md", name="notes/plan.md")
    assert ctx.ws.label(key) == "notes/plan.md" and (ctx.ws.files / "notes" / "plan.md").exists()
    ctx.ws.replace_bytes(key, b"two")
    versions = ctx.ws.versions(key)
    assert len(versions) == 1 and ctx.ws.path(key).read_bytes() == b"two"
    ctx.ws.restore(key, versions[0]["version"])
    assert ctx.ws.path(key).read_bytes() == b"one" and len(ctx.ws.versions(key)) == 2  # the restore can be undone


def test_the_index_follows_the_folder_however_a_file_arrives(ctx):
    execute(ctx, "files", {"mode": "write", "name": "memo.md", "content": "# Memo\n\nThe launch moves to June."})
    run_python(ctx.ws, "open('script_notes.md', 'w').write('# Notes\\n\\nThe budget doubles in July.')")
    index_items(ctx.ws, ctx.index)
    assert find_files(ctx.ws, ctx.index, "budget doubles").result["files"][0]["path"] == "script_notes.md"  # a script wrote it
    memo = ctx.ws.find("memo.md")
    ctx.ws.rename(memo, "launch/memo.md")
    index_items(ctx.ws, ctx.index)
    hit = find_files(ctx.ws, ctx.index, "launch moves to June").result["files"][0]
    assert hit["path"] == "launch/memo.md"
    ctx.ws.delete(memo)
    assert index_items(ctx.ws, ctx.index) == 0 and not find_files(ctx.ws, ctx.index, "launch moves to June").result["files"]


def test_what_a_model_reads_names_files_by_path_and_leaves_data_alone(ctx):
    key = ctx.ws.add_table(pd.DataFrame({"a": [1]}), "sales.csv")
    said = present(ctx.ws, {"data": key, "note": f"Saved as {key}: use {key} next.", "rows": [[key]],
                            "files": [{"id": key}], "output": f"printed {key}"})
    assert said == {"data": "sales.csv", "note": "Saved as sales.csv: use sales.csv next.", "rows": [[key]],
                    "files": [{"id": "sales.csv"}], "output": f"printed {key}"}


@pytest.mark.parametrize("name", sorted(set(TOOLS) - NETWORK))
def test_no_result_a_model_reads_carries_an_internal_key(ctx, name):
    """Keys stay inside the app whatever a tool returns (ToolResult.for_model with the workspace's present)."""
    ctx.ws.add_table(pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]}), "sales.csv")
    ctx.ws.add_image(_png(), "chart.png")
    ctx.ws.add_bytes("file", b"the fox", "txt", name="story.txt")
    schema = TOOLS[name].spec()["input_schema"]
    args = {}
    for field in schema.get("required", []):
        prop = schema["properties"][field]
        args[field] = (prop["enum"][0] if "enum" in prop else {"data": "sales.csv", "image": "chart.png",
                                                                 "file": "story.txt"}.get(field, "a")
                       if prop.get("type", "string") == "string" else 1 if prop.get("type") in ("integer", "number")
                       else [] if prop.get("type") == "array" else False)
    read = execute(ctx, name, args).for_model(lambda body: present(ctx.ws, body))
    assert not set(ITEM_REF.findall(read)) & set(ctx.ws.ids()), read[:300]


def test_a_workspace_of_the_first_layout_is_moved_with_nothing_lost(tmp_path):
    root = tmp_path / "old"
    root.mkdir()
    pd.DataFrame({"region": ["N", "S"], "amount": [1, 2]}).to_parquet(root / "data_1.parquet", index=False)
    pd.DataFrame({"region": ["N"], "amount": [1]}).to_parquet(root / "data_2.parquet", index=False)
    (root / "image_1.png").write_bytes(_png())
    (root / "file_1.txt").write_bytes(b"the user's story")
    (root / "file_2.py").write_bytes(b"print(1)")
    (root / "note_1.md").write_text("# Names\n\nThe user's name is Ary.", encoding="utf-8")
    (root / "data_1.report.md").write_text("# Sales\n\n- Table: sales.csv (data_1)\n- Tables made: data_2\n", encoding="utf-8")
    (root / "sandbox").mkdir()
    (root / "sandbox" / "script.py").write_text("print(1)")
    items = {
        "data_1": {"kind": "table", "name": "sales.csv", "parent": None, "created": "2026-10-01T10:00:00", "rows": 2,
                   "columns": ["region", "amount"], "meta": {}, "report": {"file": "data_1.report.md", "line": "Sales"}},
        "data_2": {"kind": "table", "name": "sales - cleaned", "parent": "data_1", "created": "2026-10-01T10:01:00",
                   "rows": 1, "columns": ["region", "amount"], "meta": {}},
        "image_1": {"kind": "image", "name": "A majestic lion with a thick, golden mane flying through the air above",
                    "parent": None, "created": "2026-10-01T10:02:00", "file": "image_1.png",
                    "meta": {"prompt": "a lion", "width": 8, "height": 8}},
        "file_1": {"kind": "file", "name": "story.txt", "parent": None, "created": "2026-10-01T10:03:00",
                   "file": "file_1.txt", "meta": {}},
        "file_2": {"kind": "file", "name": "avg.py", "parent": None, "created": "2026-10-01T10:04:00", "file": "file_2.py",
                   "meta": {"by": "assistant"}},
        "note_1": {"kind": "note", "name": "Names", "parent": None, "created": "2026-10-01T10:05:00", "file": "note_1.md",
                   "meta": {"title": "Names", "saved": "2026-10-01T10:05:00"}},
    }
    (root / "registry.json").write_text(json.dumps({"next": {"table": 3, "image": 2, "file": 3, "note": 2},
                                                    "items": items}), encoding="utf-8")
    ws = Workspace(root)
    paths = {k: ws.label(k) for k in ws.ids()}
    assert paths == {"data_1": "sales.csv", "data_2": "sales - cleaned.csv", "image_1": "a-majestic-lion-with-a-thick-golden-mane.png",
                     "file_1": "story.txt", "file_2": "avg.py"}  # notes are left behind: agent.md replaced them
    assert ws.table("data_1")["amount"].tolist() == [1, 2] and (ws.files / "sales.csv").exists()
    assert ws.path("file_1").read_bytes() == b"the user's story" and ws.path("image_1").read_bytes() == _png()
    assert [k for k in ws.ids() if ws.by_user(k)] == ["data_1", "file_1"]  # an upload is the user's; made things not
    assert ws.report("data_1") == "# Sales\n\n- Table: sales.csv (sales.csv)\n- Tables made: sales - cleaned.csv\n"
    assert (ws.zen / "registry.v1.json").exists() and (ws.zen / "old-sandbox" / "script.py").exists()
    assert not (root / "registry.json").exists() and not list(root.glob("data_*.parquet"))
    assert Workspace(root).ids() == ws.ids()  # opened again: already moved, nothing done twice
    new = ws.add_bytes("file", b"x", "txt", name="new.txt")
    assert new == "file_3"  # the counters carried over: no key is ever reused
