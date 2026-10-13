"""A program is a file: written with files, run with run, changed with files(edit), run again (each run is a real
Python process). The file is the state - the next task goes on from it - and its report is the facts of its last run."""

from __future__ import annotations

import pytest

from app.tools.file_ops import files
from app.tools.run import around, run
from app.tools.search_index import SearchIndex
from app.tools.workspace import Workspace

SCRIPT = ('"""Adds up five numbers and their mean."""\ntotal = 0\nfor n in range(5):\n    total += n\n'
          "print('total', total)\nprint('mean', total / count)\n")


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path / "ws")


@pytest.fixture
def index(tmp_path):
    return SearchIndex(tmp_path / "search.sqlite", embedder=None)


def write(ws, index, name, content):
    r = files(ws, index, "write", name=name, content=content, helper=True)
    assert r.status == "ok", r.result
    return r


def test_a_failed_run_shows_the_file_lines_it_points_at(ws, index):
    write(ws, index, "scripts/mean.py", SCRIPT)
    r = run(ws, index, "scripts/mean.py")
    assert r.status == "invalid_arguments" and r.result["output"].strip() == "total 10"
    assert 'File "scripts/mean.py", line 6' in r.result["errors"]  # the traceback names the file
    assert r.result["at_line"] == 6 and ">> 6| print('mean', total / count)" in r.result["around"]
    assert "files(mode='edit', name='scripts/mean.py'" in r.result["error"]["message"]


def test_edit_changes_one_passage_and_the_next_run_uses_it(ws, index):
    write(ws, index, "scripts/mean.py", SCRIPT)
    run(ws, index, "scripts/mean.py")
    edited = files(ws, index, "edit", name="scripts/mean.py", old_text="total / count", new_text="total / 5", helper=True)
    assert edited.status == "ok" and "version before is kept" in edited.result["note"]
    r = run(ws, index, "scripts/mean.py")
    assert r.status == "ok" and r.result["output"].split() == ["total", "10", "mean", "2.0"]
    key = ws.find("scripts/mean.py")
    assert len(ws.versions(key)) == 1 and ws.ids("file") == [key]  # one file, its earlier version kept


def test_an_edit_quoted_with_the_readers_line_numbers_or_not_found_or_twice(ws, index):
    write(ws, index, "calc.py", "a = 1\nb = 2\nprint(a + b)\nprint(a + b)\n")
    numbered = files(ws, index, "edit", name="calc.py", old_text="2| b = 2", new_text="2| b = 20", helper=True)
    assert numbered.status == "ok" and ws.path(ws.find("calc.py")).read_text().splitlines()[1] == "b = 20"
    missing = files(ws, index, "edit", name="calc.py", old_text="b = 3", new_text="b = 4", helper=True)
    assert missing.status == "invalid_arguments" and "The closest lines: b = 20" in missing.result["error"]["message"]
    twice = files(ws, index, "edit", name="calc.py", old_text="print(a + b)", new_text="print(a)", helper=True)
    assert twice.status == "invalid_arguments" and "2 times" in twice.result["error"]["message"]
    assert files(ws, index, "edit", name="calc.py", old_text="a = 1", helper=True).result["error"]["field"] == "new_text"


def test_the_last_run_is_the_files_report(ws, index):
    write(ws, index, "scripts/mean.py", SCRIPT.replace("total / count", "total / 5"))
    run(ws, index, "scripts/mean.py")
    report = ws.report(ws.find("scripts/mean.py"))
    assert report.startswith("# Adds up five numbers and their mean") and "- Last run: passed in" in report
    assert "- It printed: total 10 mean 2.0" in report
    assert ws.lines()[ws.find("scripts/mean.py")].endswith("| Adds up five numbers and their mean")


def test_running_again_updates_what_it_made_instead_of_adding_copies(ws, index):
    """Measured: 3 runs of one script left 3 copies of its chart and its table in the workspace. A path is one
    file: written again - in this task or a later one - it is that file's new version."""
    draws = ("from PIL import Image\nImage.new('RGB', (W, 20)).save('chart.png')\n"
             "open('out.csv', 'w').write('a,b\\n%d,2\\n' % W)\n")
    write(ws, index, "draw.py", "W = 10\n" + draws)
    first = run(ws, index, "draw.py")
    files(ws, index, "edit", name="draw.py", old_text="W = 10", new_text="W = 30", helper=True)
    again = run(ws, index, "draw.py")
    assert [f["id"] for f in again.artifact["files"]] == [f["id"] for f in first.artifact["files"]]
    assert all(f.get("updated") for f in again.result["files"])
    image, table = (f["id"] for f in again.artifact["files"])
    assert ws.entry(image)["meta"]["width"] == 30 and ws.table(table)["a"].tolist() == [30]
    assert ws.entry(image)["parent"] == ws.find("draw.py")  # what made it
    assert "- It made: chart.png, out.csv" in ws.report(ws.find("draw.py"))


def test_a_program_that_asks_is_run_with_typed_input(ws, index):
    """Measured: a game waiting for keyboard input couldn't run, so the coder saved its test script instead."""
    game = ("secret = 7\nwhile True:\n    guess = int(input('Guess: '))\n    if guess == secret:\n"
            "        print('You win!')\n        break\n    print('Higher' if guess < secret else 'Lower')\n")
    write(ws, index, "game.py", game)
    blind = run(ws, index, "game.py")
    assert blind.status == "invalid_arguments" and "waits for typed input" in blind.result["error"]["message"]
    played = run(ws, index, "game.py", input="3\n9\n7")
    assert played.status == "ok" and played.result["output"].split() == ["Guess:", "Higher", "Guess:", "Lower", "Guess:",
                                                                         "You", "win!"]
    assert "Run with typed input: 3 line(s) (3, 9, 7)" in ws.report(ws.find("game.py"))


def test_only_python_runs_and_a_syntax_error_points_at_its_line(ws, index):
    write(ws, index, "page.html", "<p>hi</p>")
    assert "only Python runs here" in run(ws, index, "page.html").result["error"]["message"]
    write(ws, index, "broken.py", "x = 1\nif x > 0\n    print(x)\n")
    r = run(ws, index, "broken.py")
    assert r.result["at_line"] == 2 and ">> 2| if x > 0" in r.result["around"]
    assert "Did you mean: broken.py" in run(ws, index, "brokn.py").result["error"]["message"]


def test_around_marks_the_line_near_the_edges():
    code = "\n".join(f"line {n}" for n in range(1, 21))
    assert around(code, 1).splitlines()[0] == ">> 1| line 1" and len(around(code, 1).splitlines()) == 4
    assert around(code, 20).splitlines()[-1] == ">> 20| line 20"


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows fonts")
def test_a_font_named_by_file_name_is_found(ws, index):
    """Measured: every named font fell back to the default (the run had no WINDIR) and the helper spent four runs
    looking for fonts."""
    write(ws, index, "font.py", "from PIL import ImageFont\nprint(ImageFont.truetype('arial.ttf', 20).getname()[0])\n")
    r = run(ws, index, "font.py")
    assert r.status == "ok" and r.result["output"].strip() == "Arial", r.result
