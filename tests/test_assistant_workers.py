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
    assert "Now:" in first["system"] and "Workspace:" in first["system"]
    assert first["messages"][0]["content"].startswith("The user's message: any rate news?")


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
    assert r.status == "ok" and r.result["answer"] == "Up 0.25." and events[0]["name"] == "read_webpage"


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
    out = shorten_tool_results(msgs, 8_000)
    for older in out[1:3]:  # the two oldest results: cut to 1,200 characters plus a note
        assert older["content"].startswith("p" * 1_200) and len(older["content"]) < 1_300
        assert "shortened" in older["content"]
    assert [m["content"] for m in out[3:]] == [m["content"] for m in msgs[3:]]  # newest two stay whole
    assert tokens(out) <= 8_000


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
