"""The coder: one scratch file per task, kept with save(name, report) - main gets the report, never the
code; the user gets the file's card. Driven by a scripted model (each run is a real Python process)."""

from __future__ import annotations

import json

import pytest

from app.providers.base import ModelTurn
from app.providers.scripted import ScriptedProvider, call
from app.assistant.main import reply, send
from app.assistant.registry import ToolContext
from app.assistant.store import Store
from app.assistant.workers import PROFILES, run_worker
from app.tools.search_index import SearchIndex
from app.tools.workspace import Workspace

REPORT = {"purpose": "Prints a Fibonacci number.",
          "how_it_works": "`fib()` adds the last two numbers in a loop.",
          "inputs_outputs": "No input; prints one number.", "how_to_run": "Run it; change n for another number.",
          "limits": "Only one n at a time."}
FIB = "def fib(n):\n    a, b = 0, 1\n    for _ in range(n):\n        a, b = b, a + b\n    return a\n\nprint(fib(30))\n"


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


def save(**fields) -> dict:
    """A scripted save (call() takes the tool's name as its first argument, and save has a name field)."""
    return {**call("save"), "input": {**REPORT, **fields}}


def test_the_coder_keeps_its_file_and_main_gets_the_report_not_the_code(ctx):
    model = ScriptedProvider([turn(call("run_code", code=FIB)), turn(save(name="fib.py"))])
    out = run_worker(model, PROFILES["coder"], "fib(30)?", ctx)
    assert out["files"][0] == {"id": "file_1", "name": "fib.py", "kind": "file"} and "warnings" not in out
    assert "code" not in out and "output" not in out  # main never gets the code
    report = out["report"]
    assert report.startswith("# Prints a Fibonacci number\n") and "## How it works\n`fib()`" in report
    assert "- Last run: passed in" in report and "- It printed: 832040" in report
    assert ctx.ws.path("file_1").read_text() == FIB and ctx.ws.report("file_1") == report
    card = next(a for a in ctx.seen if a["type"] == "file")
    assert card["action"] == "saved" and card["report"] == report and card["name"] == "fib.py"
    assert ctx.ws.lines()["file_1"].endswith("| Prints a Fibonacci number")


def test_a_report_with_a_missing_section_or_an_unknown_function_gets_one_chance(ctx):
    model = ScriptedProvider([turn(call("run_code", code=FIB)),
                              turn(save(name="fib.py", limits="", how_it_works="`fibonacci()` loops.")),
                              turn(save(name="fib.py"))])
    out = run_worker(model, PROFILES["coder"], "fib", ctx)
    told = model.requests[2]["messages"][-1]["content"]
    assert "limits is empty" in told and "fibonacci" in told and "doesn't define" in told
    assert out["files"][0]["id"] == "file_1" and "warnings" not in out


def test_a_failing_script_gets_one_chance_then_is_kept_saying_it_fails(ctx):
    broken = "print(1 / 0)\n"
    model = ScriptedProvider([turn(call("run_code", code=broken)), turn(save(name="div.py")),
                              turn(save(name="div.py", how_it_works="Divides by zero."))])
    out = run_worker(model, PROFILES["coder"], "divide", ctx)
    assert "the last run failed (ZeroDivisionError" in model.requests[2]["messages"][-1]["content"]
    assert "- Last run: failed - ZeroDivisionError: division by zero" in out["report"]  # facts by code


def test_a_file_in_another_language_is_kept_without_running(ctx):
    rust = 'fn main() {\n    for i in 1..=10 { println!("{}", i); }\n}\n'
    model = ScriptedProvider([turn(save(name="count.rs", code=rust, how_it_works="A for loop over 1..=10."))])
    out = run_worker(model, PROFILES["coder"], "rust code to print 1 to 10", ctx)
    assert ctx.ws.path(out["files"][0]["id"]).read_text() == rust and "- Last run: not run (only Python runs here)" in out["report"]


def test_python_passed_in_save_is_run_before_it_is_kept(ctx):
    model = ScriptedProvider([turn(save(name="fib.py", code=FIB))])
    out = run_worker(model, PROFILES["coder"], "fib", ctx)
    assert "- It printed: 832040" in out["report"]  # what is saved has run


def test_a_plain_text_answer_is_asked_to_save(ctx):
    model = ScriptedProvider([turn(call("run_code", code=FIB)), ModelTurn(text="It prints 832040."),
                              turn(save(name="fib.py"))])
    out = run_worker(model, PROFILES["coder"], "fib", ctx)
    assert "nothing was saved" in model.requests[2]["messages"][-1]["content"] and out["files"][0]["id"] == "file_1"


def test_work_that_ran_is_kept_even_when_the_coder_never_saves(ctx):
    model = ScriptedProvider([turn(call("run_code", code=FIB)), ModelTurn(text="Saved as fib_numbers.py."),
                              ModelTurn(text="Here is the save: fib_numbers.py")])
    out = run_worker(model, PROFILES["coder"], "fib", ctx)
    assert out["files"][0]["name"] == "fib_numbers.py" and ctx.ws.path(out["files"][0]["id"]).read_text() == FIB
    assert "- It printed: 832040" in out["report"] and "didn't describe this" in out["report"]
    assert "the coder didn't call save" in out["warnings"][-1]  # still flagged: the contract wasn't kept


def test_a_given_file_is_loaded_with_its_report_and_saved_under_its_name(ctx):
    first = run_worker(ScriptedProvider([turn(call("run_code", code=FIB)), turn(save(name="fib.py"))]),
                       PROFILES["coder"], "fib", ctx)
    model = ScriptedProvider([turn(call("fix_code", from_line=7, to_line=7, new_code="print(fib(10))")),
                              turn(save(name="fib.py", purpose="Prints the 10th Fibonacci number."))])
    out = run_worker(model, PROFILES["coder"], "make it the 10th", ctx, file="fib.py")
    opening = model.requests[0]["messages"][0]["content"]
    assert "The file to work on: fib.py (file_1), 7 lines" in opening and first["report"] in opening
    assert out["files"][0]["id"] == "file_1" and "- It printed: 55" in out["report"]  # the same file, replaced
    assert ctx.ws.ids("file") == ["file_1"] and ctx.ws.report("file_1").startswith("# Prints the 10th")


def test_a_code_file_the_coder_reads_becomes_its_scratch_script(ctx):
    ctx.ws.add_bytes("file", b"total = sum([1, 2, 3])\nprint(total / 0)\n", "py", name="mean.py")
    model = ScriptedProvider([turn(call("read_file", file="file_1")),
                              turn(call("fix_code", from_line=2, to_line=2, new_code="print(total / 3)")),
                              turn(save(name="mean.py", how_it_works="Sums three numbers and prints the mean."))])
    out = run_worker(model, PROFILES["coder"], "fix file_1", ctx)
    assert "now your scratch script" in model.requests[1]["messages"][-1]["content"]
    card = out["approval_card"]["approval"]  # the user's own file: saving it asks them first
    assert card["name"] == "mean.py" and card["content"] == "total = sum([1, 2, 3])\nprint(total / 3)\n"
    assert "- It printed: 2.0" in card["report"]


def test_run_outputs_are_linked_to_the_file(ctx):
    plot = "import matplotlib.pyplot as plt\nplt.plot([1, 2, 3])\nplt.savefig('line.png')\nprint('drawn')\n"
    model = ScriptedProvider([turn(call("run_code", code=plot)),
                              turn(save(name="plot.py", how_it_works="Plots three points with matplotlib."))])
    out = run_worker(model, PROFILES["coder"], "plot", ctx)
    image = next(i for i in ctx.ws.ids("image"))
    assert ctx.ws.entry(image)["parent"] == out["files"][0]["id"] and f"- It made: {image} (line.png)" in out["report"]
    assert next(a for a in ctx.seen if a["type"] == "file")["outputs"] == [image]


# ---------------------------------------------------------------- through main's turn

@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "assistant")
    yield s
    s.close()


def test_saving_over_the_users_own_file_waits_for_their_approval(store):
    ws_id = store.create_workspace("Code")["id"]
    chat = store.create_chat(ws_id)["id"]
    ws, _ = store.open(ws_id)
    upload = ws.add_bytes("file", b"print('old')\n", "py", name="hello.py")  # the user's own file
    main = ScriptedProvider([turn(call("write_code", task="print new", file="hello.py")),
                             ModelTurn(text="Saved hello.py.", stop="end")])
    worker = ScriptedProvider([turn(call("fix_code", from_line=1, to_line=1, new_code="print('new')")),
                               turn(save(name="hello.py", purpose="Prints new.", how_it_works="One print."))])
    paused = list(send(store, chat, "make hello.py print new", main, worker))
    assert paused[-1]["type"] == "question" and paused[-1]["approval"]["action"] == "replace_file"
    assert store.open(ws_id)[0].path(upload).read_bytes() == b"print('old')\n"  # waiting: unchanged
    done = list(reply(store, chat, "Update", main, worker))
    ws, _ = store.open(ws_id)
    assert ws.path(upload).read_text() == "print('new')\n" and ws.report(upload).startswith("# Prints new")
    told = json.loads(main.requests[1]["messages"][-1]["content"])
    assert told["allowed"] and "code" not in told and told["report"].startswith("# Prints new")
    assert done[-1]["answer"] == "Saved hello.py."


def test_a_report_that_states_what_the_run_did_not_print_gets_one_chance_to_fix(ctx):
    """The report is checked against the code and what the run printed (832040), not trusted."""
    wrong = {**REPORT, "inputs_outputs": "No input; prints 832041, the 30th Fibonacci number."}
    right = {**REPORT, "inputs_outputs": "No input; prints 832040, the 30th Fibonacci number."}
    model = ScriptedProvider([turn(call("run_code", code=FIB)), turn(save(name="fib.py", **wrong)),
                              turn(save(name="fib.py", **right))])
    out = run_worker(model, PROFILES["coder"], "fib(30)?", ctx)
    told = model.requests[2]["messages"][-1]["content"]
    assert "the report states 832041, which is not in the code or anything a run printed" in told
    assert "832040" in out["report"] and not out.get("warnings")
