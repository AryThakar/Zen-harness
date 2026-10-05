"""save_note, ask_user and draw_diagram - direct tests."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.tools import notes
from app.tools.ask import answer_result, ask_user
from app.tools.diagram import draw_diagram
from app.tools.notes import pinned, pinned_text, save_note
from app.tools.workspace import Workspace


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path / "ws")


# ---------------------------------------------------------------- save_note

def test_same_title_updates_instead_of_duplicating(ws):
    a = save_note(ws, "Revenue column", "Use net_sales as revenue.").result
    b = save_note(ws, "revenue  COLUMN", "Use net_sales as revenue; exclude refunds.").result
    assert a["note"] == b["note"] == "note_1" and b["action"] == "updated"
    assert ws.ids("note") == ["note_1"]
    assert "exclude refunds" in ws.path("note_1").read_text("utf-8")


def test_the_five_latest_notes_are_pinned_oldest_first(ws):
    for n in range(7):
        save_note(ws, f"Fact {n}", f"Fact number {n}.")
    save_note(ws, "Fact 1", "Fact number 1, corrected.")  # an update makes it the latest again
    assert [p["title"] for p in pinned(ws)] == ["Fact 3", "Fact 4", "Fact 5", "Fact 6", "Fact 1"]
    text = pinned_text(ws)
    assert "a later one supersedes an earlier one" in text and "Fact number 1, corrected." in text
    assert "Fact number 0" not in text and "Fact 2" not in text
    long = save_note(ws, "Long", "y" * (notes.PINNED_CHARS + 50)).result["note"]
    assert next(p for p in pinned(ws) if p["id"] == long)["text"].endswith(" ...")  # cut in view, whole in search


def test_notes_are_pruned_oldest_first_past_the_limits(ws, monkeypatch):
    monkeypatch.setattr(notes, "MAX_NOTES", 10)
    for n in range(10):
        save_note(ws, f"Fact {n}", f"Fact number {n}.")
    assert len(ws.ids("note")) == 10
    r = save_note(ws, "Fact 10", "Fact number 10.").result  # past the count: 20% freed, oldest first
    assert r["pruned"] == "3 oldest note(s) removed to make room"
    assert [ws.entry(i)["meta"]["title"] for i in ws.ids("note")] == [f"Fact {n}" for n in range(3, 11)]
    monkeypatch.setattr(notes, "MAX_NOTES_CHARS", 100)
    save_note(ws, "Big", "z" * 60)  # past the total length
    assert sum(len(notes.body(ws, i)) for i in ws.ids("note")) <= 80
    later = datetime.now() + notes.MAX_AGE + timedelta(days=1)
    save_note(ws, "New", "Fresh.", now=later)  # a year later every other note is too old
    assert [ws.entry(i)["meta"]["title"] for i in ws.ids("note")] == ["New"]


def test_note_arguments(ws):
    assert save_note(ws, "", "x").status == "invalid_arguments"
    assert save_note(ws, "t", "   ").status == "invalid_arguments"
    assert save_note(ws, "t", "x" * (notes.MAX_NOTE + 1)).status == "invalid_arguments"
    assert "a few lines" in save_note(ws, "t", "x" * (notes.MAX_NOTE + 1)).result["error"]["message"]


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
