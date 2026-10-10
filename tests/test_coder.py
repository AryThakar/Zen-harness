"""Programs: the analyst-programmer (the engineer helper) works in one scratch script per task and keeps a program
with save_code(name, report) - main gets the report, never the code; the user gets the file's card. save_code's
checks answer the call itself. Driven by a scripted model (each run is a real Python process)."""

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

ENGINEER = PROFILES["engineer"]
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
    """A scripted save_code (call() takes the tool's name as its first argument, and save_code has a name field)."""
    return {**call("save_code"), "input": {**REPORT, **fields}}


def done(answer: str = "It prints 832040.") -> dict:
    return call("finish", answer=answer, method="Wrote and ran a script.", caveats="None.")


def test_a_kept_program_reaches_main_as_its_report_not_its_code(ctx):
    model = ScriptedProvider([turn(call("run_code", code=FIB)), turn(save(name="fib.py")), turn(done())])
    out = run_worker(model, ENGINEER, "fib(30)?", ctx, file="fib.py")
    assert out["files"][0] == {"id": "file_1", "name": "fib.py", "kind": "file"} and "warnings" not in out
    assert "code" not in out and "output" not in out  # main never gets the code
    report = out["report"]
    assert report.startswith("# Prints a Fibonacci number\n") and "## How it works\n`fib()`" in report
    assert "- Last run: passed in" in report and "- It printed: 832040" in report
    assert ctx.ws.path("file_1").read_text() == FIB and ctx.ws.report("file_1") == report
    card = next(a for a in ctx.seen if a["type"] == "file")
    assert card["action"] == "saved" and card["report"] == report and card["name"] == "fib.py"
    assert ctx.ws.lines()["file_1"].endswith("| Prints a Fibonacci number")
    assert "kept" in json.loads(model.requests[2]["messages"][-1]["content"])  # save_code answered the helper


def test_a_report_with_a_missing_section_or_an_unknown_function_is_sent_back_with_the_call(ctx):
    """save_code's checks answer the call: the helper fixes the report and calls again - no whole resubmission."""
    model = ScriptedProvider([turn(call("run_code", code=FIB)),
                              turn(save(name="fib.py", limits="", how_it_works="`fibonacci()` loops.")),
                              turn(save(name="fib.py")), turn(done())])
    out = run_worker(model, ENGINEER, "fib", ctx, file="fib.py")
    told = model.requests[2]["messages"][-1]["content"]
    assert "Not kept" in told and "limits is empty" in told and "fibonacci" in told and "doesn't define" in told
    assert out["files"][0]["id"] == "file_1" and "warnings" not in out


def test_a_failing_script_is_kept_only_when_the_helper_confirms_it(ctx):
    broken = "print(1 / 0)\n"
    model = ScriptedProvider([turn(call("run_code", code=broken)), turn(save(name="div.py")),
                              turn({**save(name="div.py", how_it_works="Divides by zero."),
                                    "input": {**REPORT, "name": "div.py", "how_it_works": "Divides by zero.",
                                              "confirm": True}}),
                              turn(done("It fails: division by zero."))])
    out = run_worker(model, ENGINEER, "divide", ctx, file="div.py")
    told = model.requests[2]["messages"][-1]["content"]
    assert "the last run failed (ZeroDivisionError" in told and "confirm: true" in told
    assert "- Last run: failed - ZeroDivisionError: division by zero" in out["report"]  # facts by code


def test_a_file_in_another_language_is_kept_without_running(ctx):
    rust = 'fn main() {\n    for i in 1..=10 { println!("{}", i); }\n}\n'
    model = ScriptedProvider([turn(save(name="count.rs", code=rust, how_it_works="A for loop over 1..=10.")),
                              turn(done("count.rs prints 1 to 10."))])
    out = run_worker(model, ENGINEER, "rust code to print 1 to 10", ctx, file="count.rs")
    assert ctx.ws.path(out["files"][0]["id"]).read_text() == rust and "- Last run: not run (only Python runs here)" in out["report"]


def test_python_passed_in_save_code_is_run_before_it_is_kept(ctx):
    model = ScriptedProvider([turn(save(name="fib.py", code=FIB)), turn(done())])
    out = run_worker(model, ENGINEER, "fib", ctx, file="fib.py")
    assert "- It printed: 832040" in out["report"]  # what is kept has run


def test_finishing_without_keeping_the_file_the_task_asked_for_gets_one_chance(ctx):
    model = ScriptedProvider([turn(call("run_code", code=FIB)), turn(done()), turn(save(name="fib.py")), turn(done())])
    out = run_worker(model, ENGINEER, "fib", ctx, file="fib.py")
    assert "fib.py to be kept: keep it with save_code, then finish" in model.requests[2]["messages"][-1]["content"]
    assert out["files"][0]["name"] == "fib.py"


def test_without_a_file_to_keep_a_script_is_only_the_helpers_working(ctx):
    """An answer worked out with a script needs no file: nothing is kept, nothing is asked for."""
    model = ScriptedProvider([turn(call("run_code", code=FIB)), turn(done())])
    out = run_worker(model, ENGINEER, "What is the 30th Fibonacci number?", ctx)
    assert out["answer"] == "It prints 832040." and "files" not in out and "warnings" not in out


def test_a_given_file_is_loaded_with_its_report_and_kept_under_its_name(ctx):
    first = run_worker(ScriptedProvider([turn(call("run_code", code=FIB)), turn(save(name="fib.py")), turn(done())]),
                       ENGINEER, "fib", ctx, file="fib.py")
    model = ScriptedProvider([turn(call("fix_code", from_line=7, to_line=7, new_code="print(fib(10))")),
                              turn(save(name="fib.py", purpose="Prints the 10th Fibonacci number.")),
                              turn(done("It prints 55."))])
    out = run_worker(model, ENGINEER, "make it the 10th", ctx, file="fib.py")
    opening = model.requests[0]["messages"][0]["content"]
    assert "The file to work on: fib.py (file_1), 7 lines" in opening and first["report"] in opening
    assert out["files"][0]["id"] == "file_1" and "- It printed: 55" in out["report"]  # the same file, replaced
    assert ctx.ws.ids("file") == ["file_1"] and ctx.ws.report("file_1").startswith("# Prints the 10th")


def test_run_outputs_are_linked_to_the_file(ctx):
    plot = "import matplotlib.pyplot as plt\nplt.plot([1, 2, 3])\nplt.savefig('line.png')\nprint('drawn')\n"
    model = ScriptedProvider([turn(call("run_code", code=plot)),
                              turn(save(name="plot.py", how_it_works="Plots three points with matplotlib.")),
                              turn(done("It draws a line."))])
    out = run_worker(model, ENGINEER, "plot", ctx, file="plot.py")
    image = next(i for i in ctx.ws.ids("image"))
    assert ctx.ws.entry(image)["parent"] == out["files"][0]["id"] and f"- It made: {image} (line.png)" in out["report"]
    assert next(a for a in ctx.seen if a["type"] == "file")["outputs"] == [image]


def test_a_report_that_states_what_the_run_did_not_print_is_sent_back(ctx):
    """The report is checked against the code and what the run printed (832040), not trusted."""
    wrong = {**REPORT, "inputs_outputs": "No input; prints 832041, the 30th Fibonacci number."}
    right = {**REPORT, "inputs_outputs": "No input; prints 832040, the 30th Fibonacci number."}
    model = ScriptedProvider([turn(call("run_code", code=FIB)), turn(save(name="fib.py", **wrong)),
                              turn(save(name="fib.py", **right)), turn(done())])
    out = run_worker(model, ENGINEER, "fib(30)?", ctx, file="fib.py")
    told = model.requests[2]["messages"][-1]["content"]
    assert "the report states 832041, which is not in the code or anything a run printed" in told
    assert "832040" in out["report"] and not out.get("warnings")


# ---------------------------------------------------------------- through main's turn

@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "assistant")
    yield s
    s.close()


def test_keeping_a_program_over_the_users_own_file_asks_them_in_place(store):
    """The helper waits for the user's click and goes on (before: it ended, and main asked)."""
    ws_id = store.create_workspace("Code")["id"]
    chat = store.create_chat(ws_id)["id"]
    ws, _ = store.open(ws_id)
    upload = ws.add_bytes("file", b"print('old')\n", "py", name="hello.py")  # the user's own file
    main = ScriptedProvider([turn(call("analyze_or_code", task="make it print new", file="hello.py")),
                             ModelTurn(text="Updated hello.py.", stop="end")])
    worker = ScriptedProvider([turn(call("fix_code", from_line=1, to_line=1, new_code="print('new')")),
                               turn(save(name="hello.py", purpose="Prints new.", how_it_works="One print.")),
                               turn(done("It prints new."))])
    paused = list(send(store, chat, "make hello.py print new", main, worker))
    assert paused[-1]["type"] == "question" and paused[-1]["inplace"] and "Update your file hello.py" in paused[-1]["question"]
    assert store.open(ws_id)[0].path(upload).read_bytes() == b"print('old')\n"  # waiting: unchanged
    finished = list(reply(store, chat, "Update", main, worker))
    ws, _ = store.open(ws_id)
    assert ws.path(upload).read_text() == "print('new')\n" and ws.report(upload).startswith("# Prints new")
    told = json.loads(main.requests[1]["messages"][-1]["content"])
    assert "code" not in told and told["report"].startswith("# Prints new") and told["files"][0]["id"] == upload
    assert finished[-1]["answer"] == "Updated hello.py."


def test_fix_code_on_an_empty_scratch_says_how_to_change_a_stored_script(ctx):
    ctx.ws.add_bytes("file", b"total = sum([1, 2, 3])\nprint(total / 0)\n", "py", name="mean.py")
    model = ScriptedProvider([turn(call("read_file", file="file_1")),
                              turn(call("fix_code", from_line=2, to_line=2, new_code="print(total / 3)")),
                              turn(done("It needs the file as the task's file."))])
    run_worker(model, ENGINEER, "fix file_1", ctx)
    told = model.requests[2]["messages"][-1]["content"]
    assert "There is no code yet" in told and "comes in as the task's file" in told


def test_file_takes_a_program_and_anything_else_is_refused_with_the_field_that_takes_it(ctx):
    """Measured on the 26B: main put the table there ('expenses.csv'), then the memo the script was to write
    ('memo.docx'); taken as programs to keep, the helper spent 400 s unable to keep either."""
    import pandas as pd
    from app.assistant.registry import execute
    from app.assistant.workers import main_tools
    ctx.ws.add_table(pd.DataFrame({"a": [1]}), "expenses.csv")
    ctx.ws.add_bytes("file", b"the fox", "txt", name="story.txt")
    tools = main_tools(ScriptedProvider([]), ctx)
    for file, said in (("expenses.csv", "is a table (data_1): give it as data, not file"),
                       ("memo.docx", "memo.docx is not a program: name it in the task for the script to write"),
                       ("story.txt", "is file_1, not a program: name it in the task for the helper to read")):
        r = execute(ctx, "analyze_or_code", {"task": "x", "file": file}, tools)
        assert r.status == "invalid_arguments" and said in r.result["error"]["message"], file
    for file in ("primes.py", "primes", "page.html"):  # programs: a new name, no extension, another language
        r = execute(ctx, "analyze_or_code", {"task": "x", "file": file}, tools)
        assert "not a program" not in str(r.result), file


def test_a_script_is_kept_as_python_not_under_the_name_of_what_it_wrote(ctx):
    """Kept as 'memo.docx', the script would have replaced the document it wrote with its own text."""
    write = ("from docx import Document\nd = Document()\nd.add_paragraph('Q1 memo')\nd.save('memo.docx')\n"
             "print('written')\n")
    model = ScriptedProvider([turn(call("run_code", code=write)),
                              turn(save(name="memo.docx", how_it_works="Writes a Word memo with python-docx.")),
                              turn(save(name="memo.py", how_it_works="Writes a Word memo with python-docx.")),
                              turn(done("memo.docx is written."))])
    out = run_worker(model, ENGINEER, "write the memo", ctx, file="memo.py")
    assert "keep it as 'memo.py'" in model.requests[2]["messages"][-1]["content"]
    names = {ctx.ws.entry(i)["name"]: i for i in ctx.ws.ids("file")}
    assert set(names) == {"memo.docx", "memo.py"} and "warnings" not in out
    assert ctx.ws.path(names["memo.docx"]).read_bytes()[:2] == b"PK"  # still the real document
