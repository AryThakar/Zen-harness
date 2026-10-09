"""Each rule in one place (app/assistant/prompts.py): what one model reads - its role prompt, its tools'
descriptions and their fields' descriptions - says no sentence twice."""

import re

from app.assistant import prompts, workers
from app.assistant.main import MAIN
from app.assistant.registry import TOOLS

MIN_WORDS = 6  # shorter phrases ("the table id") may repeat across tools


def _descriptions(schema) -> list[str]:
    if isinstance(schema, dict):
        own = [schema["description"]] if isinstance(schema.get("description"), str) else []
        return own + [d for k, v in schema.items() if k != "description" for d in _descriptions(v)]
    if isinstance(schema, list):
        return [d for v in schema for d in _descriptions(v)]
    return []


def _readers() -> dict[str, list[str]]:
    """Per model: its system prompt, each tool's description, and its tools' field descriptions - an
    input model shared by several tools (the delegates' task) counted once."""
    main_specs = [TOOLS[n].spec() for n in workers.MAIN_DIRECT] + [t.spec() for t in workers.delegate_tools(None).values()]
    readers = {"main": (MAIN, main_specs)}
    for name, profile in workers.PROFILES.items():
        readers[name] = (profile.instructions,
                         [t.spec() for t in profile.own_tools().values()] + [workers._submit_spec(profile)]
                         + ([workers._save_code_spec()] if profile.saves_code else []))
    return {name: [system] + [s["description"] for s in specs]
            + sorted({d for s in specs for d in _descriptions(s["input_schema"])})
            for name, (system, specs) in readers.items()}


def _sentences(text: str) -> set[str]:
    parts = re.split(r"(?<=[.;!?:])\s+|\n+|\s+-\s+", text)
    words = (" ".join(re.findall(r"[a-z0-9_']+", p.lower())) for p in parts)
    return {w for w in words if len(w.split()) >= MIN_WORDS}


def test_no_model_reads_a_rule_twice():
    for reader, texts in _readers().items():
        seen: dict[str, int] = {}
        for i, text in enumerate(texts):
            for sentence in _sentences(text):
                assert seen.setdefault(sentence, i) == i, f"{reader} reads twice: {sentence!r}"


def test_the_runners_use_the_prompts_module():
    assert MAIN is prompts.MAIN
    assert workers.PROFILES["engineer"].instructions is prompts.ENGINEER
    assert workers.PROFILES["web"].instructions is prompts.WEB
    assert workers.PROFILES["knowledge"].instructions is prompts.KNOWLEDGE


def test_each_helper_reads_its_craft_in_its_prompt(tmp_path):
    """The user's design: a helper's fundamentals are in its role prompt, read every task - not a guide it
    has to search."""
    import pandas as pd

    from app.assistant.registry import ToolContext
    from app.providers.base import ModelTurn
    from app.providers.scripted import ScriptedProvider, call
    from app.tools.search_index import SearchIndex
    from app.tools.workspace import Workspace

    ws = Workspace(tmp_path / "ws")
    ws.add_table(pd.DataFrame({"category": ["Pizza", "Sides"], "revenue": [150.5, 200.25]}), "sales.csv")
    ctx = ToolContext(ws=ws, index=SearchIndex(tmp_path / "ws" / "search.sqlite", None))
    finish = call("finish", answer="Pizza is smaller.", method="Summed revenue.", caveats="Two categories only.")
    model = ScriptedProvider([ModelTurn(text="", tool_calls=[finish], stop="tool_use")])
    workers.run_worker(model, workers.PROFILES["engineer"], "What share of revenue comes from pizza?", ctx)
    request = model.requests[0]
    for craft in ("count(DISTINCT order id)", "'It ran' is not 'it is right'"):  # both crafts, one helper
        assert craft in request["system"]
    assert "guide" not in {t["name"] for t in request["tools"]}
