"""run_code / fix_code - a helper's scratch script (each runs a real Python process)."""

from __future__ import annotations

import pytest

from app.tools.scratch import Scratch, around, fix_code, run_code
from app.tools.workspace import Workspace

SCRIPT = "total = 0\nfor n in range(5):\n    total += n\nprint('total', total)\nprint('mean', total / count)\n"


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path / "ws")


def test_a_failed_run_shows_the_numbered_lines_it_points_at(ws):
    scratch = Scratch()
    r = run_code(ws, scratch, SCRIPT)
    assert r.status == "invalid_arguments" and r.result["output"].strip() == "total 10"
    assert r.result["at_line"] == 5 and r.result["lines"] == 5
    assert ">> 5| print('mean', total / count)" in r.result["around"] and "   2| for n in range(5):" in r.result["around"]
    assert "fix_code(from_line, to_line, new_code)" in r.result["error"]["message"]


def test_fix_code_changes_only_those_lines_and_runs_again(ws):
    scratch = Scratch()
    run_code(ws, scratch, SCRIPT)
    r = fix_code(ws, scratch, 5, 5, "print('mean', total / 5)")
    assert r.status == "ok" and r.result["output"].split() == ["total", "10", "mean", "2.0"]
    assert scratch.code.splitlines()[0] == "total = 0" and r.artifact["code"] == scratch.code  # the whole script
    fix_code(ws, scratch, 4, 3, "print('counted', 5)")  # to_line = from_line - 1: inserted, nothing removed
    assert scratch.code.splitlines()[3:5] == ["print('counted', 5)", "print('total', total)"]
    r = fix_code(ws, scratch, 2, 3, "total = sum(range(5))")  # two lines become one
    assert r.status == "ok" and len(scratch.code.splitlines()) == 5


def test_fix_code_says_what_lines_exist(ws):
    assert "run_code first" in fix_code(ws, Scratch(), 1, 1, "x = 1").result["error"]["message"]
    scratch = Scratch()
    run_code(ws, scratch, "print(1)\nprint(2)")
    bad = fix_code(ws, scratch, 2, 7, "print(3)")
    assert bad.status == "invalid_arguments" and "has 2 lines" in bad.result["error"]["message"]
    assert run_code(ws, scratch, "   ").status == "invalid_arguments"


def test_a_syntax_error_points_at_its_line_too(ws):
    r = run_code(ws, Scratch(), "x = 1\nif x > 0\n    print(x)\n")
    assert r.result["at_line"] == 2 and ">> 2| if x > 0" in r.result["around"]


def test_around_marks_the_line_near_the_edges():
    code = "\n".join(f"line {n}" for n in range(1, 21))
    assert around(code, 1).splitlines()[0] == ">> 1| line 1" and len(around(code, 1).splitlines()) == 4
    assert around(code, 20).splitlines()[-1] == ">> 20| line 20"
