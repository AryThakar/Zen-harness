# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""The console (app/tools/console.py): a workspace's Python program run with the user typing - its output streamed as
it is written, typed lines to its input(), Stop and the limits, and the session kept in the file's report."""

from __future__ import annotations

import time

import pytest

from app.tools import console
from app.tools.core import ToolError
from app.tools.workspace import Workspace

GAME = '''"""A number guessing game."""
secret = 7
tries = 0
while True:
    guess = int(input("Your guess: "))
    tries += 1
    if guess == secret:
        print(f"Right in {tries} tries!")
        break
    print("Higher" if guess < secret else "Lower")
with open("score.txt", "w") as f:
    f.write(str(tries))
'''


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path / "ws")


def wait_for(session, test, seconds=20.0):
    """The events once test(events) holds (or fail after seconds)."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if test(session.events):
            return session.events
        time.sleep(0.05)
    raise AssertionError(f"not seen in {seconds} s: {session.events}")


def printed(events):
    return "".join(e["text"] for e in events if e["type"] == "out")


def test_a_program_asks_streams_and_takes_typed_lines_until_it_ends(ws):
    key = ws.put("game.py", GAME.encode(), kind="file", by="assistant").key
    session = console.start(ws, None, key)
    wait_for(session, lambda ev: printed(ev).endswith("Your guess: "))  # the prompt comes before any line end
    session.type("3")
    wait_for(session, lambda ev: "Higher" in printed(ev))
    session.type("9")
    session.type("7")
    events = wait_for(session, lambda ev: ev and ev[-1]["type"] == "end")
    assert events[-1]["exit_code"] == 0 and "Right in 3 tries!" in printed(events)
    assert [e["text"] for e in events if e["type"] == "in"] == ["3", "9", "7"]
    assert [f["path"] for f in events[-1]["files"]] == ["score.txt"]  # what it wrote is the workspace's
    report = ws.report(key)
    assert "Last run: passed" in report and "› 3" in report and "Right in 3 tries!" in report
    assert "Run with typed input: 3 line(s)" in report
    assert list(session.follow()) == events  # a page that reconnects gets the whole session again
    with pytest.raises(ToolError, match="has ended"):
        session.type("1")


def test_stop_ends_it_and_the_report_says_what_happened(ws):
    key = ws.put("game.py", GAME.encode(), kind="file", by="assistant").key
    session = console.start(ws, None, key)
    wait_for(session, lambda ev: printed(ev).endswith("Your guess: "))
    session.stop()
    events = wait_for(session, lambda ev: ev and ev[-1]["type"] == "end")
    assert events[-1]["stopped"] == "stopped" and "Your guess:" in ws.report(key)


def test_an_error_streams_as_the_programs_own_and_nothing_waits_for_ever(ws, monkeypatch):
    key = ws.put("bad.py", b"print('start')\nprint(1 / 0)\n", kind="file", by="assistant").key
    session = console.start(ws, None, key)
    events = wait_for(session, lambda ev: ev and ev[-1]["type"] == "end")
    errors = "".join(e["text"] for e in events if e["type"] == "err")
    assert events[-1]["exit_code"] != 0 and "ZeroDivisionError" in errors and "_cell.py" not in errors
    assert "Last run: failed - ZeroDivisionError" in ws.report(key)
    monkeypatch.setattr(console, "IDLE_SECONDS", 1)
    idle = console.start(ws, None, ws.put("wait.py", b"input('name? ')\n", kind="file", by="assistant").key)
    assert "nothing typed" in wait_for(idle, lambda ev: ev and ev[-1]["type"] == "end", 10)[-1]["stopped"]


def test_only_python_runs_and_a_session_belongs_to_its_workspace(ws, tmp_path):
    page = ws.put("page.html", b"<p>hi</p>", kind="file", by="assistant").key
    with pytest.raises(ToolError, match="only a Python file"):
        console.start(ws, None, page)
    session = console.start(ws, None, ws.put("hi.py", b"print('hi')\n", kind="file", by="assistant").key)
    with pytest.raises(ToolError, match="No such console session"):
        console.get(session.id, Workspace(tmp_path / "other"))
    assert console.get(session.id, ws) is session
