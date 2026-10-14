"""agent.md, ask_user and draw_diagram - direct tests."""

from __future__ import annotations

import json

import pytest

from app.tools import agent_md
from app.tools.ask import answer_result, ask_user
from app.tools.diagram import draw_diagram
from app.tools.file_ops import files
from app.tools.search_index import SearchIndex
from app.tools.workspace import Workspace


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path / "ws")


# ---------------------------------------------------------------- agent.md

def test_agent_md_is_made_once_and_remembering_is_an_edit(ws, tmp_path):
    """'Remember X' is an edit of one file always in view (measured with notes: a standing preference fell out of
    view after 5 newer notes)."""
    index = SearchIndex(tmp_path / "s.sqlite", None)
    key = agent_md.ensure(ws)
    assert agent_md.ensure(ws) == key and ws.label(key) == "agent.md" and agent_md.body(ws) == ""
    assert "(nothing remembered yet)" in agent_md.in_view(ws)
    done = files(ws, index, "edit", name="agent.md", old_text="2,000 characters.\n",
                 new_text="2,000 characters.\n\n- The user's name is Ary.\n- Never discount more than 15%.\n")
    assert done.status == "ok" and done.artifact["type"] != "question"  # its own: changed without asking
    assert agent_md.body(ws) == "- The user's name is Ary.\n- Never discount more than 15%."
    view = agent_md.in_view(ws)
    assert view.startswith("[agent.md - your standing memory") and "Never discount more than 15%." in view


def test_agent_md_is_capped_and_stays(ws, tmp_path):
    index = SearchIndex(tmp_path / "s.sqlite", None)
    agent_md.ensure(ws)
    too_long = files(ws, index, "write", name="agent.md", content=agent_md.HEADER + "x" * agent_md.MAX_CHARS)
    assert too_long.status == "invalid_arguments" and "at most 2,000 characters" in too_long.result["error"]["message"]
    assert "under notes/" in too_long.result["error"]["message"]
    for mode, extra in (("delete", {}), ("rename", {"new_name": "memory.md"})):
        r = files(ws, index, mode, name="agent.md", **extra)
        assert r.status == "invalid_arguments" and "can't be deleted or renamed" in r.result["error"]["message"]
    assert ws.files / "agent.md" in ws.protected()  # scripts may read it, never change it


def test_notes_of_the_first_design_are_dropped(tmp_path):
    root = tmp_path / "old"
    (root / ".zen").mkdir(parents=True)
    (root / ".zen" / "registry.json").write_text(json.dumps({"next": {"note": 2}, "items": {
        "note_1": {"kind": "note", "name": "Names", "file": ".zen/notes/note_1.md", "by": "assistant"}}}), "utf-8")
    ws = Workspace(root)
    assert ws.ids() == [] and "note_1" not in json.loads((root / ".zen" / "registry.json").read_text("utf-8"))["items"]


# ---------------------------------------------------------------- ask_user

def test_question_shape_and_answers():
    r = ask_user("Which column is the outcome?", ["churned", "status"])
    assert r.artifact == {"type": "question", "question": "Which column is the outcome?",
                          "options": ["churned", "status"], "allow_text": True, "allow_skip": True}
    assert answer_result("q", " status ") == {"question": "q", "answer": "status"}
    skipped = answer_result("q", None)
    assert skipped["skipped"] and "want you to decide" in skipped["note"] and "don't ask again" in skipped["note"]


def test_bad_questions():
    assert ask_user("").status == "invalid_arguments"
    assert ask_user("Pick", ["a", "a"]).status == "invalid_arguments"
    assert ask_user("Pick", [str(i) for i in range(7)]).status == "invalid_arguments"
    assert ask_user("Pick", [], allow_text=False).status == "invalid_arguments"


# ---------------------------------------------------------------- draw_diagram

@pytest.mark.parametrize("code", [
    "flowchart TD\n  A[Upload] --> B{Valid?}\n  B -->|yes| C[(Store)]\n  B -->|no > retry| D>Reject]",
    "graph LR; A-->B",
    "```mermaid\nsequenceDiagram\n  User->>API: upload (file\n```",  # free text may hold a lone '('
    "erDiagram\n  CUSTOMER ||--o{ ORDER : places\n  ORDER }|..|{ ITEM : contains",
    "classDiagram\n  class Animal {\n    +int age\n  }",
    "%% a comment first\nflowchart TD\n  A[\"Price (USD)\"] --> B",
])
def test_valid_diagrams_pass(code):
    r = draw_diagram(code)
    assert r.status == "ok", r.result
    assert not r.artifact["mermaid"].startswith("```")


@pytest.mark.parametrize("code, message", [
    ("A --> B", "first line must name the diagram type"),
    ("flowchart TD\n  A[Upload --> B", "unclosed '['"),
    ("flowchart TD\n  A --> B]", "without its opening bracket"),
    ('flowchart TD\n  A["Upload] --> B', "unclosed '\"'"),
    ("", "empty"),
])
def test_broken_diagrams_are_explained(code, message):
    r = draw_diagram(code)
    assert r.status == "invalid_arguments" and message in r.result["error"]["message"]


def test_diagrams_are_mended_the_way_mermaid_needs():
    # From a real chat: brackets inside labels, a trailing % comment, an inactive deactivation.
    flow = draw_diagram("graph TD\n  A[Clock] --> C[Hold: Q(next) = Q(current)]; % note\n"
                        "  C -->|J=1 (set)| D{J=1 (set)?}\n  // a comment\n  D --> E[\"kept (quoted)\"]")
    assert flow.artifact["mermaid"] == ('graph TD\n  A[Clock] --> C["Hold: Q(next) = Q(current)"];\n'
                                        '  C -->|"J=1 (set)"| D{"J=1 (set)?"}\n  %% a comment\n'
                                        '  D --> E["kept (quoted)"]')
    seq = draw_diagram("sequenceDiagram\n  A->>+B: hi\n  B-->>-A: bye\n  B-->>-A: again\n  deactivate A")
    assert seq.artifact["mermaid"] == "sequenceDiagram\n  A->>+B: hi\n  B-->>-A: bye\n  B-->>A: again"


def test_mermaid_blocks_in_an_answer_are_mended_too():
    from app.tools.diagram import mend_blocks
    text = "See:\n```mermaid\ngraph TD\n  A[x (y)] --> B\n```\n```python\nprint(1)\n```"
    assert mend_blocks(text) == 'See:\n```mermaid\ngraph TD\n  A["x (y)"] --> B\n```\n```python\nprint(1)\n```'
