"""The worker runner, registry and rolling window, driven by a scripted model (no LLM)."""

from __future__ import annotations

import pandas as pd
import pytest

from app.agent.providers.base import ModelTurn
from app.agent.providers.scripted import ScriptedProvider, call
from app.assistant import registry, workers
from app.assistant.context import fit_turns, shorten_tool_results, tokens
from app.assistant.registry import Tool, ToolContext, execute
from app.assistant.workers import PROFILES, main_tools, run_worker, ungrounded_numbers
from app.tools.core import ToolResult, ok
from app.tools.search_index import SearchIndex
from app.tools.workspace import Workspace


@pytest.fixture
def ctx(tmp_path):
    ws = Workspace(tmp_path / "ws")
    seen = []
    c = ToolContext(ws=ws, index=SearchIndex(tmp_path / "ws" / "search.sqlite", None), conversation="c1",
                    emit=seen.append)
    c.seen = seen
    return c


def submit(**fields) -> ModelTurn:
    return ModelTurn(text="", tool_calls=[call("submit_result", **fields)], stop="tool_use")


def tools_turn(*calls) -> ModelTurn:
    return ModelTurn(text="", tool_calls=list(calls), stop="tool_use")


# ---------------------------------------------------------------- registry

def test_registry_validates_before_running(ctx):
    assert execute(ctx, "nope", {}).status == "invalid_arguments"
    r = execute(ctx, "calculate", {"expression": "2 + 2", "extra": 1})
    assert r.status == "invalid_arguments" and "extra" in r.result["error"]["message"]
    assert execute(ctx, "calculate", "not json").status == "invalid_arguments"
    assert execute(ctx, "calculate", {"expression": "2400 * 0.15"}).result["result"] == 360
    spec = registry.TOOLS["make_chart"].spec()
    assert "title" not in spec["input_schema"] and "title" in spec["input_schema"]["properties"]  # a real field


def test_artifacts_go_to_the_ui(ctx):
    execute(ctx, "draw_diagram", {"mermaid": "flowchart TD\n A --> B"})
    assert ctx.seen and ctx.seen[0]["tool"] == "draw_diagram" and ctx.seen[0]["type"] == "diagram"


def test_main_slot_sees_data_delegate_only_with_a_table(ctx):
    names = set(main_tools(ScriptedProvider([]), ctx))
    assert names == {"get_weather", "generate_image", "draw_diagram", "calculate", "save_note", "ask_user",
                     "research_web", "ask_knowledge", "write_code"}
    ctx.ws.add_table(pd.DataFrame({"a": [1]}), "t.csv")
    assert "analyze_data" in main_tools(ScriptedProvider([]), ctx)
    assert "run_python" not in names  # only inside the data and coder workers


# ---------------------------------------------------------------- web worker (fake web tools)

@pytest.fixture
def fake_web(monkeypatch):
    results = {"results": [{"title": "Rates", "url": "https://news.example.com/rates", "snippet": "up 0.25"},
                           {"title": "Other", "url": "https://other.example.com/", "snippet": "..."}]}
    page = {"url": "https://news.example.com/rates", "title": "Rates", "text": "The bank raised rates by 0.25 points."}
    monkeypatch.setitem(registry.TOOLS, "web_search", Tool("web_search", "search", registry.SearchIn,
                                                           lambda c, a: ok(results)))
    monkeypatch.setitem(registry.TOOLS, "read_webpage", Tool("read_webpage", "read", registry.ReadPageIn,
                                                             lambda c, a: ok(page)))


def test_web_worker_answers_from_pages_it_read(ctx, fake_web):
    model = ScriptedProvider([
        tools_turn(call("web_search", query="central bank rates")),
        tools_turn(call("read_webpage", url="https://news.example.com/rates")),
        submit(answer="Rates rose by 0.25 points.", sources=["https://news.example.com/rates"]),
    ])
    out = run_worker(model, PROFILES["web"], "What did the central bank do?", ctx, user_message="any rate news?")
    assert out == {"status": "ok", "worker": "web", "answer": "Rates rose by 0.25 points.",
                   "sources": ["https://news.example.com/rates"]}
    first = model.requests[0]
    assert {t["name"] for t in first["tools"]} == {"web_search", "read_webpage", "submit_result"}
    # the system prompt is the role's fixed instructions (cached across tasks); what changes is in the task
    assert first["system"] == PROFILES["web"].instructions
    opening = first["messages"][0]["content"]
    assert opening.startswith("Now: ") and "Workspace:" in opening and "The user's message: any rate news?" in opening


def test_citing_an_unread_page_gets_one_chance_to_fix(ctx, fake_web):
    model = ScriptedProvider([
        tools_turn(call("web_search", query="rates")),
        submit(answer="Rates rose.", sources=["https://other.example.com/"]),  # only a snippet
        tools_turn(call("read_webpage", url="https://news.example.com/rates")),
        submit(answer="Rates rose by 0.25 points.", sources=["https://news.example.com/rates"]),
    ])
    out = run_worker(model, PROFILES["web"], "rates?", ctx)
    assert "warnings" not in out and out["sources"] == ["https://news.example.com/rates"]
    feedback = model.requests[2]["messages"][-1]
    assert feedback["is_error"] and "only seen as a search snippet" in feedback["content"]


def test_a_second_bad_submission_returns_with_a_warning(ctx, fake_web):
    model = ScriptedProvider([submit(answer="Made up.", sources=["https://invented.example/"]),
                              submit(answer="Still made up.", sources=["https://invented.example/"])])
    out = run_worker(model, PROFILES["web"], "rates?", ctx)
    assert out["answer"] == "Still made up." and "never found or read" in out["warnings"][0]


def test_step_limit_forces_a_plain_answer(ctx, fake_web):
    model = ScriptedProvider([tools_turn(call("web_search", query=f"q{i}")) for i in range(6)]
                             + [ModelTurn(text="I could not finish.")])
    out = run_worker(model, PROFILES["web"], "loop forever", ctx)
    assert model.requests[-1]["allow_tools"] is False
    assert out["answer"] == "I could not finish." and out["note"] == "answered at the step limit" and "warnings" not in out


def test_tools_outside_the_profile_are_refused(ctx, fake_web):
    model = ScriptedProvider([tools_turn(call("run_python", code="import os")),
                              ModelTurn(text="ok")])
    run_worker(model, PROFILES["web"], "x", ctx)
    assert "not one of your tools" in model.requests[1]["messages"][-1]["content"]


# ---------------------------------------------------------------- data worker (real tools)

def test_data_worker_numbers_must_come_from_tool_results(ctx):
    data = ctx.ws.add_table(pd.DataFrame({"region": ["N", "S", "N"], "amount": [100.5, 200.25, 50.0]}), "s.csv")
    sql = f"SELECT region, sum(amount) AS total FROM {data} GROUP BY region ORDER BY total DESC"
    model = ScriptedProvider([
        tools_turn(call("query_data", sql=sql)),
        submit(answer="South leads with 200.25; North has 150.5, so South is 33% ahead."),  # 33% is invented
        tools_turn(call("calculate", expression="(200.25 - 150.5) / 150.5 * 100")),
        submit(answer="South leads with 200.25; North has 150.5, so South is 33.06% ahead."),
    ])
    out = run_worker(model, PROFILES["data"], "Which region sells most?", ctx)
    assert "warnings" not in out and "33.06%" in out["answer"]
    assert "33%" in model.requests[2]["messages"][-1]["content"]


def test_data_worker_passes_a_question_up_instead_of_guessing(ctx):
    model = ScriptedProvider([submit(answer="Need the date order first.",
                                     question_for_user="Are the dates day first or month first?",
                                     options=["day first", "month first"])])
    out = run_worker(model, PROFILES["data"], "monthly sales", ctx)
    assert out["question_for_user"].startswith("Are the dates") and out["options"] == ["day first", "month first"]


@pytest.mark.parametrize("answer, evidence, missing", [
    ("Total revenue was 817,860.05.", ['{"total": 817860.05}'], []),
    ("Classic is 30% of pizzas.", ['{"top_share": 0.3003}'], []),
    ("About 1.2M orders.", ['{"rows": 1203456}'], []),
    ("Top 5 groups; growth was 42.7%.", ['{"x": 1}'], ["42.7%"]),
    ("Revenue was 817,860.", ['{"total": 817860.05}'], []),  # rounded as written
    ("Revenue was 817,900.", ['{"total": 817860.05}'], ["817,900"]),
])
def test_number_grounding(answer, evidence, missing):
    assert ungrounded_numbers(answer, evidence) == missing


# ---------------------------------------------------------------- knowledge and coder workers

def test_knowledge_sources_must_be_ones_the_tools_returned(ctx):
    from app.tools.notes import save_note
    save_note(ctx.ws, "Revenue column", "Use net_sales as revenue.", index=ctx.index)
    model = ScriptedProvider([
        tools_turn(call("search_files", query="revenue column")),
        submit(answer="Use net_sales.", sources=["note 'Revenue column' (note_1)"]),
    ])
    assert "warnings" not in run_worker(model, PROFILES["knowledge"], "Which column is revenue?", ctx)
    model = ScriptedProvider([submit(answer="x", sources=["handbook.pdf, page 9"]),
                              submit(answer="x", sources=["handbook.pdf, page 9"])])
    assert "not a source any tool returned" in run_worker(model, PROFILES["knowledge"], "q", ctx)["warnings"][0]


def test_coder_must_hand_in_code_that_ran(ctx):
    good = "print(sum(range(10)))"
    model = ScriptedProvider([
        submit(answer="It prints 45.", code=good),  # never ran
        tools_turn(call("run_python", code=good)),
        submit(answer="It prints 45.", code=good),
    ])
    out = run_worker(model, PROFILES["coder"], "sum 0..9", ctx)
    assert "warnings" not in out and out["code"] == good
    assert "never run" in model.requests[1]["messages"][-1]["content"]


def test_delegate_tool_returns_the_checked_result(ctx, fake_web):
    worker = ScriptedProvider([tools_turn(call("read_webpage", url="https://news.example.com/rates")),
                               submit(answer="Up 0.25.", sources=["https://news.example.com/rates"])])
    events = []
    tools = main_tools(worker, ctx, user_message=lambda: "rates?", progress=events.append)
    r = tools["research_web"].handler(ctx, registry.TaskIn(task="Find the latest rate change."))
    assert r.status == "ok" and r.result["answer"] == "Up 0.25."
    # the flow, in order: start, model turn, tool call, its result, model turn, the check of the submission
    assert [e["type"] for e in events] == ["worker_start", "worker_turn", "worker_tool", "worker_result",
                                           "worker_turn", "worker_check"]
    assert events[2]["name"] == "read_webpage" and events[3]["status"] == "ok" and events[5]["problems"] == []


# ---------------------------------------------------------------- rolling window

def turns(n, size=400):
    out = []
    for i in range(n):
        out += [{"role": "user", "content": f"question {i} " + "x" * size},
                {"role": "assistant", "text": f"answer {i} " + "y" * size, "tool_calls": []}]
    return out


def test_rolling_window_drops_whole_old_turns_and_keeps_the_cut():
    msgs = turns(20)
    budget = tokens(msgs) // 2
    visible, dropped = fit_turns(msgs, budget)
    assert visible[0]["role"] == "user" and tokens(visible) <= budget * 0.75 + 300
    assert visible[-1] == msgs[-1]
    # one more turn: still under budget, so the cut stays put (same prompt prefix -> cache reuse)
    more = msgs + turns(1)
    again, dropped2 = fit_turns(more, budget, dropped)
    assert dropped2 == dropped and again[0] == visible[0]


def test_the_current_turn_is_never_dropped():
    huge = [{"role": "user", "content": "z" * 100_000}]
    visible, dropped = fit_turns(turns(3) + huge, 1000)
    assert visible == huge and dropped == 3


def test_long_worker_tasks_shorten_old_tool_results():
    msgs = [{"role": "user", "content": "task"}] + [
        {"role": "tool", "tool_call_id": str(i), "name": "read_webpage", "content": "p" * 12_000} for i in range(4)]
    out, shortened = shorten_tool_results(msgs, 8_000)
    for older in out[1:3]:  # the two oldest results: cut to 1,200 characters plus a note
        assert older["content"].startswith("p" * 1_200) and len(older["content"]) < 1_300
        assert "shortened" in older["content"]
    assert [m["content"] for m in out[3:]] == [m["content"] for m in msgs[3:]]  # newest two stay whole
    assert tokens(out) <= 8_000 and shortened == 2
    small = msgs[:3] + [{"role": "tool", "tool_call_id": "9", "name": "calculate", "content": "4"}] * 2
    again, kept = shorten_tool_results(small, 8_000, shortened)
    assert kept == 2 and again[1]["content"] == out[1]["content"]  # the cut stays put: same prefix, cache kept
    assert shorten_tool_results(msgs[:2], 8_000) == (msgs[:2], 0)


def test_plain_text_answers_get_the_same_checks(ctx):
    data = ctx.ws.add_table(pd.DataFrame({"region": ["N", "S"], "amount": [150.5, 200.25]}), "s.csv")
    model = ScriptedProvider([
        tools_turn(call("query_data", sql=f"SELECT region, amount FROM {data}")),
        ModelTurn(text="South sells 210.75."),            # not in any result: one chance to fix
        ModelTurn(text="South sells the most: 200.25."),
    ])
    out = run_worker(model, PROFILES["data"], "who sells most?", ctx)
    assert out["answer"] == "South sells the most: 200.25." and "warnings" not in out
    assert "210.75" in model.requests[2]["messages"][-1]["content"]


def test_plain_text_web_answer_is_checked_by_its_urls(ctx, fake_web):
    model = ScriptedProvider([
        tools_turn(call("read_webpage", url="https://news.example.com/rates")),
        ModelTurn(text="Rates rose 0.25 points (https://news.example.com/rates)."),
    ])
    out = run_worker(model, PROFILES["web"], "rates?", ctx)
    assert out["sources"] == ["https://news.example.com/rates"] and "warnings" not in out


def test_plain_text_code_answer_is_checked(ctx):
    code = "print(2 + 2)"
    model = ScriptedProvider([tools_turn(call("run_python", code=code)),
                              ModelTurn(text=f"Done:\n```python\n{code}\n```\nIt prints 4.")])
    out = run_worker(model, PROFILES["coder"], "add", ctx)
    assert out["code"] == code and "warnings" not in out


def test_cited_web_sources_reach_the_ui_as_cards(ctx, monkeypatch):
    page = {"url": "https://news.example.com/rates", "title": "Rates", "text": "Up 0.25."}
    card = {"type": "webpage", "url": "https://news.example.com/rates", "title": "Rates", "site": "Daily News",
            "favicon": "https://news.example.com/icon.png", "image": "https://news.example.com/a.jpg"}
    monkeypatch.setitem(registry.TOOLS, "read_webpage", Tool("read_webpage", "read", registry.ReadPageIn,
                                                             lambda c, a: ok(page, card)))
    model = ScriptedProvider([tools_turn(call("read_webpage", url="https://news.example.com/rates")),
                              submit(answer="Up 0.25.", sources=["https://news.example.com/rates"])])
    run_worker(model, PROFILES["web"], "rates?", ctx)
    sources = [a for a in ctx.seen if a["type"] == "sources"]
    assert sources and sources[0]["items"][0]["site"] == "Daily News" and sources[0]["items"][0]["image"].endswith("a.jpg")


def test_knowledge_plain_text_sources_are_recognised_and_required(ctx):
    from app.tools.memory import remember_turn
    remember_turn(ctx.index, "old1", "How far ahead should we forecast?", "We agreed on 6 months.")
    model = ScriptedProvider([tools_turn(call("recall", query="forecast horizon")),
                              ModelTurn(text="We agreed on 6 months (conversation old1).")])
    out = run_worker(model, PROFILES["knowledge"], "forecast horizon?", ctx)
    assert out["sources"] == ["conversation old1"] and "warnings" not in out
    # no source named, but the answer is in a passage: that passage is credited
    model = ScriptedProvider([tools_turn(call("recall", query="forecast horizon")), ModelTurn(text="We agreed on 6 months.")])
    assert run_worker(model, PROFILES["knowledge"], "forecast horizon?", ctx)["sources"] == ["conversation old1"]
    # an answer nothing supports, without sources: one retry, then a warning
    model = ScriptedProvider([tools_turn(call("recall", query="forecast horizon")),
                              ModelTurn(text="The budget is 12,000 euros."), ModelTurn(text="The budget is 12,000 euros.")])
    out = run_worker(model, PROFILES["knowledge"], "budget?", ctx)
    assert "no sources given" in out["warnings"][0]


def test_quoted_addresses_are_not_sources_and_code_output_travels(ctx, fake_web):
    model = ScriptedProvider([tools_turn(call("read_webpage", url="https://news.example.com/rates")),
                              ModelTurn(text="The example runs on http://0.0.0.0:9091 (https://news.example.com/rates).")])
    out = run_worker(model, PROFILES["web"], "q", ctx)
    assert out["sources"] == ["https://news.example.com/rates"] and "warnings" not in out
    code = "print(6 * 7)"
    model = ScriptedProvider([tools_turn(call("run_python", code=code)), submit(answer="It ran.", code=code)])
    assert run_worker(model, PROFILES["coder"], "q", ctx)["output"].strip() == "42"


def test_result_written_as_json_text_counts_and_tool_names_are_not_sources(ctx):
    from app.tools.memory import remember_turn
    remember_turn(ctx.index, "old1", "How far ahead should we forecast?", "We agreed to forecast 6 months ahead, by month.")
    text = '```json\n{"answer": "We agreed to forecast 6 months ahead, by month.", "sources": ["recall"]}\n```'
    model = ScriptedProvider([tools_turn(call("recall", query="forecast horizon")), ModelTurn(text=text)])
    out = run_worker(model, PROFILES["knowledge"], "forecast horizon?", ctx)
    assert out["answer"] == "We agreed to forecast 6 months ahead, by month."
    assert out["sources"] == ["conversation old1"] and "warnings" not in out


def test_final_code_with_added_comments_counts_as_the_code_that_ran(ctx):
    ran = "x = 2\nprint(x * 21)"
    shown = "# compute the answer\nx = 2  # two\n\nprint(x * 21)\n"
    model = ScriptedProvider([tools_turn(call("run_python", code=ran)), submit(answer="42", code=shown)])
    out = run_worker(model, PROFILES["coder"], "q", ctx)
    assert "warnings" not in out and out["output"].strip() == "42"
    other = ScriptedProvider([tools_turn(call("run_python", code=ran)), submit(answer="42", code="print(42)"),
                              submit(answer="42", code="print(42)")])
    assert "never ran" in run_worker(other, PROFILES["coder"], "q", ctx)["warnings"][0]


def test_a_repeated_call_is_not_run_again_and_says_what_to_do(ctx, monkeypatch):
    runs = []
    results = {"results": [{"title": "Docs", "url": "https://docs.example.com/a", "snippet": "..."},
                           {"title": "Blog", "url": "https://blog.example.com/b", "snippet": "..."}]}
    busy = ToolResult("failed", {"error": {"message": "The page answered HTTP 503."}})
    monkeypatch.setitem(registry.TOOLS, "web_search", Tool("web_search", "search", registry.SearchIn,
                                                           lambda c, a: runs.append(a.query) or ok(results)))
    monkeypatch.setitem(registry.TOOLS, "read_webpage", Tool("read_webpage", "read", registry.ReadPageIn,
                                                             lambda c, a: runs.append(a.url) or busy))
    model = ScriptedProvider([
        tools_turn(call("web_search", query="server default port")),
        tools_turn(call("read_webpage", url="https://docs.example.com/a")),
        tools_turn(call("web_search", query="Server  default port")),  # the same search again
        tools_turn(call("read_webpage", url="https://docs.example.com/a")),  # the same failed read again
        ModelTurn(text="I couldn't confirm it."),
    ])
    run_worker(model, PROFILES["web"], "default port?", ctx)
    assert runs == ["server default port", "https://docs.example.com/a"]  # neither repeat ran
    again = model.requests[3]["messages"][-1]["content"]
    assert "already ran this exact search" in again and "https://blog.example.com/b" in again
    assert "docs.example.com/a" not in again  # already tried
    assert "already failed" in model.requests[4]["messages"][-1]["content"]


def test_an_incomplete_answer_comes_with_a_briefing(ctx, monkeypatch):
    results = {"results": [{"title": "Docs", "url": "https://docs.example.com/a", "snippet": "..."}]}
    busy = ToolResult("failed", {"error": {"message": "The page answered HTTP 503 (the site is not answering right now)."}})
    monkeypatch.setitem(registry.TOOLS, "web_search", Tool("web_search", "s", registry.SearchIn, lambda c, a: ok(results)))
    monkeypatch.setitem(registry.TOOLS, "read_webpage", Tool("read_webpage", "r", registry.ReadPageIn, lambda c, a: busy))
    model = ScriptedProvider([
        tools_turn(call("web_search", query="server default port")),
        tools_turn(call("read_webpage", url="https://docs.example.com/a")),
        submit(answer="I couldn't confirm the default port.", suggestion="The project's README on GitHub may list it."),
    ])
    out = run_worker(model, PROFILES["web"], "default port?", ctx)
    assert out["suggestion"] == "The project's README on GitHub may list it."
    assert out["trail"].startswith("Ran web_search, read_webpage. Failed: read_webpage https://docs.example.com/a: "
                                   "The page answered HTTP 503")
    clean = run_worker(ScriptedProvider([tools_turn(call("web_search", query="q")),
                                         submit(answer="Rates rose.", sources=[])]), PROFILES["web"], "x", ctx)
    assert "trail" not in clean  # a complete answer needs no briefing


def test_any_worker_can_hand_a_question_to_the_user(ctx):
    ctx.ws.add_bytes("file", b"Leave: 24 days a year.", ".txt", "handbook_2024.txt")
    model = ScriptedProvider([
        tools_turn(call("read_file", file="file_1")),
        submit(answer="", question_for_user="Which year's handbook do you mean?", options=["2024", "2025"]),
    ])
    out = run_worker(model, PROFILES["knowledge"], "How many leave days?", ctx)
    assert out["question_for_user"] == "Which year's handbook do you mean?" and out["options"] == ["2024", "2025"]
    assert "warnings" not in out and "trail" in out  # asking is not a failed check
    spec = workers._submit_spec(PROFILES["knowledge"])["input_schema"]["properties"]
    assert list(spec)[:2] == ["answer", "sources"] and list(spec)[-3:] == list(workers.BRIEF)


def test_the_last_steps_are_announced(ctx, fake_web):
    model = ScriptedProvider([tools_turn(call("web_search", query=f"q{i}")) for i in range(6)]
                             + [ModelTurn(text="I could not finish.")])
    run_worker(model, PROFILES["web"], "loop forever", ctx)
    assert "[2 steps left" in model.requests[4]["messages"][-1]["content"]
    assert "[Last step" in model.requests[5]["messages"][-1]["content"]
    assert "steps left" not in model.requests[3]["messages"][-1]["content"]


def test_printed_output_in_place_of_code_becomes_the_code_that_ran(ctx):
    ran = "def fib(n):\n    a, b = 0, 1\n    for _ in range(n):\n        a, b = b, a + b\n    return a\nprint(fib(30))"
    model = ScriptedProvider([tools_turn(call("run_python", code=ran)), submit(answer="832040", code="832040")])
    out = run_worker(model, PROFILES["coder"], "fib(30)?", ctx)
    assert "warnings" not in out and out["code"] == ran and out["output"].strip() == "832040"
    text = f"Here it is:\n```python\n{ran}\n```\nIt printed:\n```\n832040\n```"
    plain = run_worker(ScriptedProvider([tools_turn(call("run_python", code=ran)), ModelTurn(text=text)]),
                       PROFILES["coder"], "fib(30)?", ctx)
    assert "warnings" not in plain and plain["code"] == ran  # the output block is not the code


def test_a_coder_whose_every_run_failed_is_flagged(ctx):
    model = ScriptedProvider([tools_turn(call("run_python", code="1 / 0")),
                              submit(answer="Here is the script."), submit(answer="Here is the script.")])
    out = run_worker(model, PROFILES["coder"], "divide", ctx)
    assert "no run finished without error" in out["warnings"][0]


def test_addresses_differing_only_in_case_are_different_calls(ctx, monkeypatch):
    read = []
    monkeypatch.setitem(registry.TOOLS, "read_webpage", Tool("read_webpage", "r", registry.ReadPageIn,
                                                             lambda c, a: read.append(a.url) or ok({"url": a.url})))
    model = ScriptedProvider([tools_turn(call("read_webpage", url="https://youtu.be/abcDEF12345")),
                              tools_turn(call("read_webpage", url="https://youtu.be/abcdef12345")),
                              ModelTurn(text="done")])
    run_worker(model, PROFILES["web"], "two videos", ctx)
    assert read == ["https://youtu.be/abcDEF12345", "https://youtu.be/abcdef12345"]  # video ids are case-sensitive


def test_a_tool_that_crashes_does_not_end_the_task(ctx, monkeypatch):
    def broken(c, a):
        raise KeyError("boom")
    monkeypatch.setitem(registry.TOOLS, "calculate", Tool("calculate", "c", registry.CalcIn, broken))
    r = execute(ctx, "calculate", {"expression": "1 + 1"})
    assert r.status == "failed" and "internal error (KeyError)" in r.result["error"]["message"]


def test_the_reply_room_is_the_providers_own_limit():
    from app.assistant.context import SLOT_TOKENS, history_budget
    assert history_budget("", [], 8_192) == history_budget("", [], 4_096) - 4_096
    assert history_budget("", []) < SLOT_TOKENS - 4_000


def test_made_lists_only_what_the_task_created(ctx):
    ctx.ws.add_table(pd.DataFrame({"a": [1, 2, 3]}), name="t.csv")
    model = ScriptedProvider([tools_turn(call("inspect_data", data="data_1")),
                              tools_turn(call("query_data", sql="SELECT a * 2 AS b FROM data_1", save=True)),
                              submit(answer="Saved.")])
    out = run_worker(model, PROFILES["data"], "double a", ctx)
    assert out["made"] == ["data_2"]  # data_1 was the input


def test_a_question_without_an_answer_is_accepted_first_time(ctx):
    model = ScriptedProvider([submit(question_for_user="Day first or month first?", options=["day", "month"])])
    out = run_worker(model, PROFILES["data"], "February total?", ctx)
    assert out["question_for_user"] == "Day first or month first?" and len(model.requests) == 1


def test_long_pages_are_focused_on_the_last_search(ctx, monkeypatch):
    asked = []
    results = {"results": [{"title": "Docs", "url": "https://docs.example.com/a", "snippet": "..."}]}
    monkeypatch.setitem(registry.TOOLS, "web_search", Tool("web_search", "s", registry.SearchIn, lambda c, a: ok(results)))
    monkeypatch.setitem(registry.TOOLS, "read_webpage", Tool("read_webpage", "r", registry.ReadPageIn,
                                                             lambda c, a: asked.append(a.find) or ok({"url": a.url})))
    model = ScriptedProvider([tools_turn(call("web_search", query="server default port")),
                              tools_turn(call("read_webpage", url="https://docs.example.com/a")),
                              tools_turn(call("read_webpage", url="https://docs.example.com/a", find="timeout")),
                              ModelTurn(text="done")])
    run_worker(model, PROFILES["web"], "default port?", ctx)
    assert asked == ["server default port", "timeout"]  # the worker's own find wins


def test_a_table_is_inspected_before_anything_is_computed_on_it(ctx):
    ctx.ws.add_table(pd.DataFrame({"pizza": ["a", "b", "Total"], "quantity": [2, 3, 5]}), name="sales.csv")
    model = ScriptedProvider([tools_turn(call("query_data", sql="SELECT count(*) FROM data_1")),
                              tools_turn(call("query_data", sql="SELECT sum(quantity) FROM data_1")),
                              submit(answer="10")])
    run_worker(model, PROFILES["data"], "How many pizzas?", ctx)
    first = model.requests[1]["messages"][-1]["content"]
    assert "had not been inspected, so inspect_data ran first" in first and "Total" in first
    second = model.requests[2]["messages"][-1]["content"]
    assert '"rows": [[10]]' in second  # once inspected, calls run normally
    later = ScriptedProvider([tools_turn(call("query_data", sql="SELECT 1 AS x FROM data_1 LIMIT 1")),
                              submit(answer="ok")])
    run_worker(later, PROFILES["data"], "again", ctx)
    assert "not been inspected" not in later.requests[1]["messages"][-1]["content"]  # remembered on the table


def test_a_tidied_copy_of_the_code_that_ran_is_handed_on_as_it_ran(ctx):
    ran = "import json\nimport io\nrows = [{'a': 1}, {'a': 2}]\nprint(len(rows))\njson.dumps(rows)"
    tidied = ran.replace("import io\n", "")  # the model dropped an unused import while copying
    out = run_worker(ScriptedProvider([tools_turn(call("run_python", code=ran)), submit(answer="2", code=tidied)]),
                     PROFILES["coder"], "count", ctx)
    assert "warnings" not in out and out["code"] == ran
    other = "import pandas as pd\nprint(pd.DataFrame({'a': [1, 2]}).shape[0])"
    flagged = run_worker(ScriptedProvider([tools_turn(call("run_python", code=ran)), submit(answer="2", code=other),
                                           submit(answer="2", code=other)]), PROFILES["coder"], "count", ctx)
    assert "never ran" in flagged["warnings"][0]  # a different script that never ran is still caught


def test_a_tool_question_asked_in_prose_is_handed_on_as_a_question(ctx):
    upload = ctx.ws.root / "sales_q1.csv"  # an upload: add_file types the columns and notes the ambiguous dates
    upload.write_text("\n".join(["date,amount"] + [f"{d:02d}/{m:02d}/2025,{100 + d}" for m in (1, 2, 3)
                                                  for d in (1, 3, 5, 7, 9, 11)]), encoding="utf-8")
    ctx.ws.add_file(upload)
    model = ScriptedProvider([tools_turn(call("inspect_data", data="data_1")),
                              ModelTurn(text="The dates are ambiguous. Which order do they use, day or month first?")])
    out = run_worker(model, PROFILES["data"], "February total?", ctx)
    assert out["question_for_user"].startswith("The dates in date could be read two ways")
    assert out["options"] == ["Day first (03/04/2025 = 3 April)", "Month first (03/04/2025 = March 4)"]
    assert "trail" in out and "warnings" not in out


def test_a_choice_only_the_user_can_make_is_not_guessed(ctx):
    upload = ctx.ws.root / "sales_q1.csv"
    upload.write_text("\n".join(["date,amount"] + [f"{d:02d}/{m:02d}/2025,{100 + d}" for m in (1, 2, 3)
                                                  for d in (1, 3, 5, 7, 9, 11)]), encoding="utf-8")
    ctx.ws.add_file(upload)
    guess = {"op": "parse_dates", "columns": ["date"], "order": "dayfirst"}
    model = ScriptedProvider([tools_turn(call("inspect_data", data="data_1")),
                              tools_turn(call("clean_data", data="data_1", steps=[guess])),
                              ModelTurn(text="Which order do the dates use?")])
    run_worker(model, PROFILES["data"], "February total?", ctx)
    assert "only the user can answer" in model.requests[2]["messages"][-1]["content"]
    told = ScriptedProvider([tools_turn(call("inspect_data", data="data_1")),
                             tools_turn(call("clean_data", data="data_1", steps=[guess])), submit(answer="done")])
    run_worker(told, PROFILES["data"], "February total? The user says the dates are day first.", ctx)
    assert '"rows_after": 18' in told.requests[2]["messages"][-1]["content"]  # the user's answer lets it run
