# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
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
from app.tools import agent_md
from app.tools.search_index import SearchIndex
from app.tools.workspace import Workspace

REAL = set(TOOLS) | set(workers.DELEGATES) | {"submit_result", "finish"}
RETIRED = ("write_code", "analyze_data", "save_report", "import_table", "download_file", "save_note", "run_python",
           "run_code", "fix_code", "save_code", "read_file")
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


def _descriptions(spec) -> list[str]:
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


def _roles() -> dict[str, tuple[list[str], dict[str, dict]]]:
    """Per model: what it reads - its prompt, the app's hidden messages to it, its tools' and their fields'
    descriptions - and the input schema of each tool it has, by the name it calls it."""
    bare = _bare()
    agent_md.ensure(bare.ws)
    main = [t.spec() for t in workers.main_tools(ScriptedProvider([]), bare).values()]
    said = [prompts.MAIN, prompts.MAIN_CUT_OFF, prompts.QUICK, prompts.ANNOUNCE_NUDGE, prompts.CLAIM_NUDGE,
            prompts.REMEMBER_NUDGE, *prompts.SHOWN.values(), agent_md.in_view(bare.ws)]
    out = {"main": (said + [d for s in main for d in _descriptions(s)], {s["name"]: s["input_schema"] for s in main})}
    for name, profile in workers.PROFILES.items():
        specs = [t.spec() for t in profile.own_tools().values()] + [workers._submit_spec(profile)]
        nudges = [prompts.HELPER_CUT_OFF, prompts.NO_CALL, prompts.FULL, *prompts.STEPS_LEFT.values(), prompts.FIX_FINISH,
                  *(prompts.PICTURES.values() if profile.sees_images else ())]
        said = [profile.instructions] + [n.format(finish=profile.finish, names="x.png") for n in nudges]
        out[name] = (said + [d for s in specs for d in _descriptions(s)], {s["name"]: s["input_schema"] for s in specs})
    return out


def _readers() -> dict[str, list[str]]:
    """What each model reads."""
    return {name: texts for name, (texts, _) in _roles().items()}


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
    specs += [workers._submit_spec(p) for p in workers.PROFILES.values()]
    specs += [t.spec() for p in workers.PROFILES.values() for t in p.own_tools().values()]  # helpers' own versions
    blank = [f"{s['name']}.{f}" for s in specs for f, prop in fields(s["input_schema"]) if not prop.get("description")]
    assert not blank, blank


# A call written out in what a model reads is followed to the letter (measured on the 26B, 2026-10-14: agent.md's
# instructions said "edit" and "its whole new text" - the model sent edit, then edit's new_text field, 3 of 3 runs).
# So every tool, mode and field they name must be one the reading model has.
CALL = re.compile(r"(?<![\w.])([a-z][a-z_]*)(\s?)\(([^()]*)\)")  # files(mode='write', content=...) and files (edit)
ARG = re.compile(r"\b([a-z_]+)\s*=")
MODE = re.compile(r"\bmode\s*=\s*'(\w+)'")
NEAR_MODE = re.compile(r"\b([a-z_]+)(?:\s?\(|,)\s*mode\s*=\s*'(\w+)'")  # files (mode='download'), files, mode='write'


def _modes(schema: dict) -> set[str]:
    return set(schema.get("properties", {}).get("mode", {}).get("enum", []))


def _named_wrong(text: str, tools: dict[str, dict]) -> list[str]:
    """Each tool, mode and field the text names that the reading model doesn't have."""
    wrong = []
    snake = set(re.findall(r"(?<![\w.])([a-z]+_[a-z_]+)\b", text))
    wrong += [f"no tool {n}" for n in snake & (REAL - set(tools))]
    for name, space, inside in CALL.findall(text):
        if name not in tools or (space and "=" in inside):  # "a daily forecast (days=1 covers ...)" is prose
            continue
        fields = set(tools[name].get("properties", {}))
        wrong += [f"{name} has no field {f}" for f in ARG.findall(inside) if f not in fields]
        words = [w.strip() for w in inside.split(",")]
        if "=" not in inside and all(re.fullmatch(r"[a-z]+", w) for w in words) and _modes(tools[name]):
            wrong += [f"{name} has no mode {w}" for w in words if w not in _modes(tools[name])]  # files (find, read)
    for name, mode in NEAR_MODE.findall(text):
        if name in tools and mode not in _modes(tools[name]):
            wrong.append(f"{name} has no mode {mode}")
    every_mode = set().union(*(_modes(s) for s in tools.values()))
    wrong += [f"no tool here has mode {m}" for m in MODE.findall(text) if m not in every_mode]
    return wrong


@pytest.mark.parametrize("reader", ["main", *workers.PROFILES])
def test_every_call_a_model_reads_is_one_its_tools_take(reader):
    texts, tools = _roles()[reader]
    wrong = sorted({w for text in texts for w in _named_wrong(text, tools)})
    assert not wrong, wrong


@pytest.mark.parametrize("reader", ["main", *workers.PROFILES])
def test_a_tool_with_modes_describes_each_one_and_no_other(reader):
    """Its mode field says each mode is described with the tool: so each one is, and no mode it hasn't is."""
    _, tools = _roles()[reader]
    specs = {s["name"]: s for s in ([t.spec() for t in workers.main_tools(ScriptedProvider([]), _bare()).values()]
                                    if reader == "main" else
                                    [t.spec() for t in workers.PROFILES[reader].own_tools().values()])}
    every = {"list", "find", "read", "write", "edit", "rename", "delete", "download", "pin", "unpin"}
    for name, schema in tools.items():
        modes = _modes(schema)
        if not modes:
            continue
        said = " ".join([specs[name]["description"], *_descriptions(schema)])
        assert not [m for m in modes if not re.search(rf"\b{m}\b", specs[name]["description"])], name
        others = {m for m in re.findall(r"\b([a-z]+):", said) if m in every} - modes
        assert not others, f"{name} describes modes it hasn't: {others}"


def test_what_a_tool_says_back_names_calls_that_exist():
    """Errors and notes a tool returns name calls too (an edit without old_text: 'mode='write' with content'):
    each mode and field they name is one some tool takes - every string in app/ is read, so a new one is checked."""
    import ast
    from pathlib import Path
    tools = {}
    for _, schemas in _roles().values():
        for name, schema in schemas.items():
            merged = tools.setdefault(name, {"properties": {}})
            modes = _modes(merged) | _modes(schema)
            merged["properties"].update(schema.get("properties", {}))
            if modes:
                merged["properties"]["mode"] = {"enum": sorted(modes)}
    wrong = []
    for path in Path(__file__).resolve().parents[1].joinpath("app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docs = {id(n.body[0].value) for n in ast.walk(tree)  # docstrings are read by people, not models
                if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body
                and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
                wrong += [f"{path.name}: {w}" for w in _named_wrong(node.value, tools) if not w.startswith("no tool")]
    assert not wrong, sorted(set(wrong))
