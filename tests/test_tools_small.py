"""save_note, ask_user and draw_diagram - direct tests."""

from __future__ import annotations

import pytest

from app.tools.ask import answer_result, ask_user
from app.tools.diagram import draw_diagram
from app.tools.notes import MAX_PINNED, pinned_text, save_note, set_pin
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


def test_pin_needs_approval_and_has_a_size_limit(ws):
    r = save_note(ws, "Plan", "1. Clean data_1. 2. Forecast monthly revenue.", pin=True)
    assert r.result["pin"] == "waiting for the user's approval" and r.artifact["pin_request"]
    assert pinned_text(ws) == ""  # not pinned until the user approves
    set_pin(ws, "note_1", True)
    assert "Forecast monthly revenue" in pinned_text(ws) and "(note_1)" in pinned_text(ws)
    assert save_note(ws, "Plan", "Changed plan.").result["pin"] == "pinned"  # an update keeps the pin
    big = save_note(ws, "Big", "x" * (MAX_PINNED - 5), pin=True)
    assert big.status == "invalid_arguments" and "limited to" in big.result["error"]["message"]
    set_pin(ws, "note_1", False)
    assert pinned_text(ws) == ""


def test_note_arguments(ws):
    assert save_note(ws, "", "x").status == "invalid_arguments"
    assert save_note(ws, "t", "   ").status == "invalid_arguments"
    assert save_note(ws, "t", "x" * 20_001).status == "invalid_arguments"


# ---------------------------------------------------------------- ask_user

def test_question_shape_and_answers():
    r = ask_user("Which column is the outcome?", ["churned", "status"])
    assert r.artifact == {"type": "question", "question": "Which column is the outcome?",
                          "options": ["churned", "status"], "allow_text": True, "allow_skip": True}
    assert answer_result("q", " status ") == {"question": "q", "answer": "status"}
    skipped = answer_result("q", None)
    assert skipped["skipped"] and "default" in skipped["note"]


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
