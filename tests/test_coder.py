# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Programs: the analyst-programmer writes a program as a workspace file (files), runs it (run), changes it
(files edit) and runs it again. The file is the state - the next task goes on from it, a Stop or a failed model loses
nothing - and main gets the facts (what ran, the files made or changed, what the last run printed), never the code.
Driven by a scripted model (each run is a real Python process)."""

from __future__ import annotations

import json

import pytest

from app.providers.base import Cancelled, ModelTurn
from app.providers.scripted import ScriptedProvider, call
from app.assistant.main import reply, send
from app.assistant.registry import ToolContext
from app.assistant.store import Store
from app.assistant.workers import PROFILES, run_worker
from app.tools.search_index import SearchIndex
from app.tools.workspace import Workspace

ENGINEER = PROFILES["engineer"]
FIB = ('"""Prints a Fibonacci number."""\n\ndef fib(n):\n    a, b = 0, 1\n    for _ in range(n):\n'
       "        a, b = b, a + b\n    return a\n\nprint(fib(30))\n")


@pytest.fixture
def ctx(tmp_path):
    ws = Workspace(tmp_path / "ws")
    seen = []
    c = ToolContext(ws=ws, index=SearchIndex(tmp_path / "ws" / "search.sqlite", None), conversation="c1",
                    emit=seen.append)
    c.seen = seen
    return c


def turn(*calls) -> ModelTurn:
    return ModelTurn(text="", tool_calls=list(calls), stop="tool_use")


def done(answer: str = "It prints 832040.") -> ModelTurn:
    return turn(call("finish", answer=answer))


def test_a_program_reaches_main_as_facts_not_code(ctx):
    model = ScriptedProvider([turn(call("files", mode="write", name="scripts/fib.py", content=FIB)),
                              turn(call("run", file="scripts/fib.py")), done()])
    out = run_worker(model, ENGINEER, "Write a program that prints the 30th Fibonacci number.", ctx)
    assert out["answer"] == "It prints 832040." and "warnings" not in out
    assert out["files"] == [{"id": "file_1", "name": "scripts/fib.py", "kind": "file"}]
    assert out["last_run_printed"].strip() == "832040" and out["ran"] == "Ran files, run."
    assert "def fib" not in json.dumps(out)  # main never gets the code
    assert next(a for a in ctx.seen if a["type"] == "file")["name"] == "scripts/fib.py"  # the user gets the file
    assert ctx.ws.report("file_1").startswith("# Prints a Fibonacci number")


def test_a_program_whose_last_run_failed_is_not_called_fixed(ctx):
    """Measured 2026-10-17: "I fixed the indentation and ran the script successfully" - its last run had failed."""
    broken = "def demo():\n    for x in [1, 2]:\n    print(x)\n\ndemo()\n"
    model = ScriptedProvider([turn(call("files", mode="write", name="work/demo.py", content=broken)),
                              turn(call("run", file="work/demo.py")),
                              done("I fixed the indentation error and the script ran successfully."),
                              done("demo.py still fails with an IndentationError at line 3: the loop body needs "
                                   "indenting.")])
    out = run_worker(model, ENGINEER, "make demo.py run", ctx)
    sent_back = model.requests[3]["messages"][-1]["content"]  # the finish's result: sent back, with why
    assert "the latest run of work/demo.py" in sent_back and "IndentationError" in sent_back
    assert out["answer"].startswith("demo.py still fails")  # said plainly: it stands


def test_the_next_task_goes_on_from_the_file(ctx):
    run_worker(ScriptedProvider([turn(call("files", mode="write", name="fib.py", content=FIB)),
                                 turn(call("run", file="fib.py")), done()]), ENGINEER, "fib", ctx)
    model = ScriptedProvider([turn(call("files", mode="edit", name="fib.py", old_text="print(fib(30))",
                                        new_text="print(fib(10))")),
                              turn(call("run", file="fib.py")), done("It prints 55.")])
    out = run_worker(model, ENGINEER, "make it the 10th", ctx, files=["fib.py"])
    opening = model.requests[0]["messages"][0]["content"]
    assert "Files for this task:\nfib.py:" in opening and "| Prints a Fibonacci number" in opening
    assert out["files"] == [{"id": "file_1", "name": "fib.py", "kind": "file", "changed": True}]
    assert ctx.ws.ids("file") == ["file_1"] and len(ctx.ws.versions("file_1")) == 1  # one file, the old text kept
    assert "- It printed: 55" in ctx.ws.report("file_1")


def test_a_file_in_another_language_is_written_not_run(ctx):
    model = ScriptedProvider([turn(call("files", mode="write", name="hello.rs", content='fn main() { println!("hi"); }')),
                              turn(call("run", file="hello.rs")), done("hello.rs prints hi when built with cargo.")])
    out = run_worker(model, ENGINEER, "a hello world in Rust", ctx)
    told = model.requests[2]["messages"][-1]["content"]
    assert "only Python runs here" in told and out["files"][0]["name"] == "hello.rs"


def test_changing_the_users_own_file_asks_them_in_place(store):
    """The helper waits for the user's click and goes on; the version before is kept."""
    ws_id = store.create_workspace("Code")["id"]
    chat = store.create_chat(ws_id)["id"]
    ws, _ = store.open(ws_id)
    upload = ws.add_bytes("file", b"print('old')\n", "py", name="hello.py", by="user")  # the user's own file
    main = ScriptedProvider([turn(call("analyze_or_code", task="make it print new", files=["hello.py"])),
                             ModelTurn(text="Updated hello.py.", stop="end")])
    worker = ScriptedProvider([turn(call("files", mode="edit", name="hello.py", old_text="old", new_text="new")),
                               turn(call("run", file="hello.py")), done("It prints new.")])
    paused = list(send(store, chat, "make hello.py print new", main, worker))
    assert paused[-1]["type"] == "question" and paused[-1]["inplace"] and "Update your file hello.py" in paused[-1]["question"]
    assert store.open(ws_id)[0].path(upload).read_bytes() == b"print('old')\n"  # waiting: unchanged
    finished = list(reply(store, chat, "Update", main, worker))
    ws, _ = store.open(ws_id)
    assert ws.path(upload).read_text() == "print('new')\n" and len(ws.versions(upload)) == 1
    told = json.loads(main.requests[1]["messages"][-1]["content"])
    assert told["files"] == [{"id": "hello.py", "name": "hello.py", "kind": "file", "changed": True}]  # paths, for main
    assert finished[-1]["answer"] == "Updated hello.py."


def test_a_failed_model_hands_back_what_was_done(ctx):
    """Measured: an overflowed helper came back as 'failed' alone, and five runs of work were lost."""
    model = ScriptedProvider([turn(call("files", mode="write", name="fib.py", content=FIB)),
                              turn(call("run", file="fib.py"))])  # then the model fails (no more turns)
    out = run_worker(model, ENGINEER, "fib", ctx)
    assert out["status"] == "ok" and "stopped early" in out["stopped"] and out["warnings"]
    assert out["files"][0]["name"] == "fib.py" and out["last_run_printed"].strip() == "832040"


def test_a_full_conversation_finishes_with_what_is_done(ctx):
    model = ScriptedProvider([turn(call("files", mode="write", name="big.py", content="x = 1\n" + "# filler\n" * 600)),
                              turn(call("run", file="big.py")),
                              turn(call("files", mode="write", name="big2.py", content="y = 2\n" + "# more\n" * 600)),
                              done("Wrote both.")])
    model.slot_tokens, model.max_tokens = 10_400, 500  # a small slot: the prompt (~7,300) and one file fit, two do not
    out = run_worker(model, ENGINEER, "write two files", ctx)
    sent = [m for r in model.requests for m in r["messages"]]
    assert any("shortened here: the rest is in the file" in json.dumps(m.get("tool_calls") or []) for m in sent)
    assert out["answer"] == "Wrote both." and "warnings" not in out  # the old file text gave way; the work finished


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "assistant")
    yield s
    s.close()


def test_a_stop_keeps_what_the_helper_did_as_the_calls_result(store):
    ws_id = store.create_workspace("Code")["id"]
    chat = store.create_chat(ws_id)["id"]

    def stop(_messages):
        raise Cancelled

    worker = ScriptedProvider([turn(call("files", mode="write", name="fib.py", content=FIB)),
                               turn(call("run", file="fib.py")), stop])
    main = ScriptedProvider([turn(call("analyze_or_code", task="fib"))])
    events = list(send(store, chat, "fib please", main, worker))
    assert events[-1]["type"] == "stopped"
    kept = next(m for m in store.messages(chat) if m["role"] == "tool")
    said = json.loads(kept["content"])
    assert said["status"] == "stopped" and said["files"] == ["fib.py"] and said["last_run_printed"].strip() == "832040"
    assert said["ran"] == "Ran files, run."
