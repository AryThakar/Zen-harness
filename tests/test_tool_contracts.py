"""Every tool's contract, checked the same way for all - a tool added is checked with no test of its own.

- a call missing a required field is refused, naming the field;
- an item that isn't there is refused with the closest ones or what there is; an item of the wrong kind is
  refused saying what it is (and, where one exists, which tool takes it);
- what a model reads - its prompt, its tools' and fields' descriptions, and the errors above - names only tools
  that exist (before 2026-10-10 errors told main to use import_table and download_file, which it never had).
"""

from __future__ import annotations

import re

import pandas as pd
import pytest

from app.assistant import prompts, workers
from app.assistant.registry import ITEM_FIELDS, TOOLS, ToolContext, execute
from app.providers.scripted import ScriptedProvider
from app.tools.search_index import SearchIndex
from app.tools.workspace import Workspace

REAL = set(TOOLS) | set(workers.DELEGATES) | {"submit_result", "finish", "save_code"}
RETIRED = ("write_code", "analyze_data", "save_report", "import_table", "download_file", "save_note", "run_python")
SCRIPT_CALLS = {"table", "path", "open"}  # what a helper's script calls (run_code's description)
WRONG = {"table": "image_1", "image": "file_1", "file": "data_1"}  # an item of another kind, per kind wanted
KIND_NAMES = {"table": "a table", "image": "an image", "file": "a file"}


@pytest.fixture
def ctx(tmp_path):
    ws = Workspace(tmp_path / "ws")
    ws.add_table(pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]}), "sales.csv")
    ws.add_image(_png(), "chart.png")
    ws.add_bytes("file", b"the fox and the dog", "txt", name="story.txt")
    return ToolContext(ws=ws, index=SearchIndex(tmp_path / "ws" / "search.sqlite", None))


def _png() -> bytes:
    import io

    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(out, "PNG")
    return out.getvalue()


def _filled(schema: dict) -> dict:
    """A plausible value for every required field, from the schema alone: the enum's first, a word, two words,
    a number, false."""
    out = {}
    for field in schema.get("required", []):
        prop = schema["properties"][field]
        if "enum" in prop:
            out[field] = prop["enum"][0]
        elif prop.get("type") == "array":  # a list of objects (steps) may be empty; words come two (columns)
            out[field] = [] if (prop.get("items") or {}).get("type") == "object" else ["a", "b"]
        else:
            out[field] = {"string": "a", "integer": 1, "number": 1, "boolean": False}[prop.get("type", "string")]
    return out


def _tool_names_in(text: str) -> set[str]:
    return set(re.findall(r"(?<![\w.])([a-z]+_[a-z_]+)\(", text))  # pd.read_csv( is a library's call


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_a_missing_required_field_is_refused_naming_it(ctx, name):
    required = TOOLS[name].spec()["input_schema"].get("required", [])
    if not required:
        pytest.skip("no required field")
    r = execute(ctx, name, {})
    assert r.status == "invalid_arguments" and r.result["error"]["field"] in required, r.result


ITEM_TOOLS = sorted((name, field, kinds[0]) for name, tool in TOOLS.items()
                    for field, kinds in ITEM_FIELDS.items() if field in tool.spec()["input_schema"]["properties"]
                    and field in tool.spec()["input_schema"].get("required", []))


@pytest.mark.parametrize("name, field, kind", ITEM_TOOLS)
def test_an_item_not_there_or_of_another_kind_is_refused_saying_so(ctx, name, field, kind):
    base = _filled(TOOLS[name].spec()["input_schema"])
    missing = execute(ctx, name, {**base, field: "nothing_here.csv"})
    assert missing.status == "invalid_arguments" and missing.result["error"]["field"] == field
    assert "Did you mean" in missing.result["error"]["message"] or "Here:" in missing.result["error"]["message"]
    wrong = execute(ctx, name, {**base, field: WRONG[kind]})
    message = wrong.result["error"]["message"]
    assert wrong.status == "invalid_arguments" and f"not {KIND_NAMES[kind]}" in message, message
    for said in (missing.result["error"]["message"], message):
        assert _tool_names_in(said) <= REAL | SCRIPT_CALLS and not any(old in said for old in RETIRED), said


def _readers() -> dict[str, list[str]]:
    """What each model reads: its prompt, and its tools' and their fields' descriptions."""
    def texts(spec) -> list[str]:
        found = []

        def walk(node):
            if isinstance(node, dict):
                if isinstance(node.get("description"), str):
                    found.append(node["description"])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
        walk(spec)
        return found

    main = [t.spec() for t in workers.main_tools(ScriptedProvider([]), _bare()).values()]
    out = {"main": [prompts.MAIN] + [d for s in main for d in texts(s)]}
    for name, profile in workers.PROFILES.items():
        specs = [t.spec() for t in profile.own_tools().values()] + [workers._submit_spec(profile)] + (
            [workers._save_code_spec()] if profile.saves_code else [])
        out[name] = [profile.instructions] + [d for s in specs for d in texts(s)]
    return out


def _bare() -> ToolContext:
    import tempfile
    from pathlib import Path
    root = Path(tempfile.mkdtemp())
    return ToolContext(ws=Workspace(root / "ws"), index=SearchIndex(root / "s.sqlite", None))


@pytest.mark.parametrize("reader", ["main", *workers.PROFILES])
def test_what_a_model_reads_names_only_tools_that_exist(reader):
    for text in _readers()[reader]:
        assert not any(old in text for old in RETIRED), text[:200]
        assert _tool_names_in(text) <= REAL | SCRIPT_CALLS, text[:200]


def test_every_field_a_model_fills_says_what_it_holds():
    """A field without a description is a guess (measured before: an untyped field written wrong 10 of 10 times)."""
    def fields(schema, path=""):
        for key, prop in schema.get("properties", {}).items():
            yield f"{path}{key}", prop
            if prop.get("type") == "array" and isinstance(prop.get("items"), dict):
                yield from fields(prop["items"], f"{path}{key}[].")
    specs = [t.spec() for t in TOOLS.values()] + [t.spec() for t in workers.delegate_tools(None).values()]
    specs += [workers._submit_spec(p) for p in workers.PROFILES.values()] + [workers._save_code_spec()]
    blank = [f"{s['name']}.{f}" for s in specs for f, prop in fields(s["input_schema"]) if not prop.get("description")]
    assert not blank, blank
