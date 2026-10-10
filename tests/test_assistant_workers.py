"""The worker runner, registry and rolling window, driven by a scripted model (no LLM)."""

from __future__ import annotations

import pandas as pd
import pytest

from app.providers.base import ModelTurn
from app.providers.scripted import ScriptedProvider, call
from app.assistant import registry, workers
from app.providers.base import estimate_tokens
from app.assistant.context import cut_turns, fit_turns, shorten_tool_results
from app.assistant.registry import Tool, ToolContext, execute
from app.assistant.workers import PROFILES, evidence, main_tools, run_worker, ungrounded_numbers
from app.tools.core import ToolError, ToolResult, ok
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


REPORT = {"answer": "South sells most.", "data_used": "data_1 as it is; no cleaning needed.",
          "method": "Summed the amount per region with query_data.", "caveats": "Only the regions in this table."}
ENGINEER = PROFILES["engineer"]


def report(**fields) -> ModelTurn:
    """The analyst-programmer's finish, with its sections."""
    return ModelTurn(text="", tool_calls=[call("finish", **{**REPORT, **fields})], stop="tool_use")


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


def test_a_tool_result_with_plain_notes_is_traced(ctx):
    from app.assistant.workers import Trace
    from app.tools.core import ok
    trace = Trace()
    trace.add("forecast", {"data": "data_1"}, ok({"notes": ["Dates were made from the 'year' and 'quarter' columns."]}))
    trace.add("search_files", {"query": "q"}, ok({"passages": [], "notes": [{"source": "note 'A' (note_1)", "text": "x"}]}))
    assert trace.sources == {"note 'A' (note_1)"}  # only search_files' notes are sources


def test_main_slot_sees_its_tools_and_all_three_helpers_always(ctx):
    """Always all three: with the analyst hidden until a table existed, main couldn't plan 'find data, bring it in,
    analyse it', and the coder looked like the only way."""
    names = set(main_tools(ScriptedProvider([]), ctx))
    assert names == {"get_weather", "generate_image", "view_image", "draw_diagram", "calculate", "notes", "plan",
                     "ask_user", "files", "research_web", "ask_knowledge", "analyze_or_code"}
    assert "run_code" not in names  # only inside the analyst-programmer


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
    out = run_worker(model, PROFILES["web"], "What did the central bank do?", ctx)
    assert out == {"status": "ok", "worker": "web", "answer": "Rates rose by 0.25 points.",
                   "sources": ["https://news.example.com/rates"]}
    first = model.requests[0]
    assert {t["name"] for t in first["tools"]} == {"web_search", "read_webpage", "ask_user", "submit_result"}
    # the system prompt is the role's fixed instructions (cached across tasks); what changes is in the task
    assert first["system"] == PROFILES["web"].instructions
    opening = first["messages"][0]["content"]
    assert opening.startswith("Now: ") and "Workspace:" in opening
    assert opening.endswith("Your task: What did the central bank do?")


def test_the_result_says_how_pages_were_read_and_which_failed(ctx, monkeypatch):
    card = {"type": "webpage", "url": "https://news.example.com/rates", "title": "Rates", "site": "news.example.com",
            "published": "2026-10-01T09:00:00+00:00", "via": "a browser"}

    def read(c, a):
        if "shop" in a.url:
            raise ToolError("The site refused the request (HTTP 403).", invalid=False, reason="blocked")
        return ok({"url": a.url, "title": "Rates", "text": "The bank raised rates by 0.25 points."}, card)

    monkeypatch.setitem(registry.TOOLS, "read_webpage", Tool("read_webpage", "read", registry.ReadPageIn, read))
    model = ScriptedProvider([
        tools_turn(call("read_webpage", url="https://shop.example.com/rates")),
        tools_turn(call("read_webpage", url="https://news.example.com/rates")),
        submit(answer="Rates rose by 0.25 points.", sources=["https://news.example.com/rates"]),
    ])
    out = run_worker(model, PROFILES["web"], "rates?", ctx)
    assert out["read"] == [{"url": "https://news.example.com/rates", "title": "Rates", "date": "2026-10-01",
                            "how": "a browser"}]
    assert out["failed"] == [{"url": "https://shop.example.com/rates", "why": "The site refused the request (HTTP 403)."}]


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


def test_the_web_helper_asks_the_user_in_place_when_the_task_is_unclear(ctx, fake_web):
    asked = []
    ctx.ask = lambda card: asked.append(card) or {"answer": "India"}
    model = ScriptedProvider([tools_turn(call("ask_user", question="Which country's prices?", options=["India", "US"])),
                              tools_turn(call("web_search", query="iPhone 17 price India")),
                              tools_turn(call("read_webpage", url="https://news.example.com/rates")),
                              submit(answer="Rates rose by 0.25 points.", sources=["https://news.example.com/rates"])])
    out = run_worker(model, PROFILES["web"], "What does the iPhone 17 cost?", ctx)
    assert asked[0]["tool"] == "research_web" and asked[0]["options"] == ["India", "US"]
    assert '"answer": "India"' in model.requests[1]["messages"][-1]["content"] and "warnings" not in out


def test_one_site_is_read_at_most_a_few_pages_in_one_task(ctx, monkeypatch):
    read = []
    monkeypatch.setitem(registry.TOOLS, "read_webpage", Tool("read_webpage", "r", registry.ReadPageIn,
                                                             lambda c, a: read.append(a.url) or ok({"url": a.url})))
    cap = evidence.SITE_CAP
    model = ScriptedProvider([tools_turn(call("read_webpage", url=f"https://shop.example/item/{i}")) for i in range(cap + 1)]
                             + [tools_turn(call("read_webpage", url="https://other.example/review")), ModelTurn(text="done")])
    run_worker(model, PROFILES["web"], "compare prices", ctx)
    assert len(read) == cap + 1 and read[-1] == "https://other.example/review"  # the 7th page of the shop was refused
    assert f"read {cap} pages of shop.example" in model.requests[cap + 1]["messages"][-1]["content"]


def test_tools_outside_the_profile_are_refused(ctx, fake_web):
    model = ScriptedProvider([tools_turn(call("run_code", code="import os")),
                              ModelTurn(text="ok")])
    run_worker(model, PROFILES["web"], "x", ctx)
    assert "not one of your tools" in model.requests[1]["messages"][-1]["content"]


# ---------------------------------------------------------------- data worker (real tools)

def test_data_worker_numbers_must_come_from_tool_results(ctx):
    data = ctx.ws.add_table(pd.DataFrame({"region": ["N", "S", "N"], "amount": [100.5, 200.25, 50.0]}), "s.csv")
    sql = f"SELECT region, sum(amount) AS total FROM {data} GROUP BY region ORDER BY total DESC"
    model = ScriptedProvider([
        tools_turn(call("inspect_data", data=data)),
        tools_turn(call("query_data", sql=sql)),
        report(answer="South leads with 200.25; North has 150.5, so South is 33% ahead."),  # 33% is invented
        tools_turn(call("query_data", sql="SELECT (200.25 - 150.5) / 150.5 * 100 AS ahead_pct")),  # arithmetic
        report(answer="South leads with 200.25; North has 150.5, so South is 33.06% ahead."),
    ])
    out = run_worker(model, ENGINEER, "Which region sells most?", ctx)
    assert "warnings" not in out and "33.06%" in out["answer"]
    assert "33%" in model.requests[3]["messages"][-1]["content"]


def test_the_report_on_a_table_is_titled_by_the_task(ctx):
    """The title is the task's first line, written by the app: a report without one cost the 26B a step."""
    ctx.ws.add_table(pd.DataFrame({"roll": [1, 2, 3, 4, 5, 6]}), "rolls.csv")
    model = ScriptedProvider([report(answer="The die looks fair.")])
    out = run_worker(model, ENGINEER, "Is the die fair?\nUse rolls.csv.", ctx, table="data_1")
    assert "warnings" not in out and out["report"].startswith("# Is the die fair?\n")
    assert len(model.requests) == 1


def test_data_worker_passes_a_question_up_instead_of_guessing(ctx):
    model = ScriptedProvider([tools_turn(call("finish", question_for_user="Are the dates day first or month first?",
                                              options=["day first", "month first"]))])
    out = run_worker(model, ENGINEER, "monthly sales", ctx)
    assert out["question_for_user"].startswith("Are the dates") and out["options"] == ["day first", "month first"]


@pytest.mark.parametrize("answer, evidence, missing", [
    ("Total revenue was 817,860.05.", ['{"total": 817860.05}'], []),
    ("Classic is 30% of pizzas.", ['{"top_share": 0.3003}'], []),
    ("About 1.2M orders.", ['{"rows": 1203456}'], []),
    ("Top 5 groups; growth was 42.7%.", ['{"x": 1}'], ["42.7%"]),
    ("Revenue was 817,860.", ['{"total": 817860.05}'], []),  # rounded as written
    ("Revenue was 817,900.", ['{"total": 817860.05}'], ["817,900"]),
    # conventions of method pass; a made-up p-value does not
    ("p = 0.7717, well above 0.05; with 95% confidence it is fair.", ['{"p_value": 0.771668}'], []),
    ("p = 0.03, below 0.05.", ['{"chi2": 2.53}'], ["0.03"]),
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


def test_delegate_tool_returns_the_checked_result(ctx, fake_web):
    worker = ScriptedProvider([tools_turn(call("read_webpage", url="https://news.example.com/rates")),
                               submit(answer="Up 0.25.", sources=["https://news.example.com/rates"])])
    events = []
    tools = main_tools(worker, ctx, progress=events.append)
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


def count(messages):  # the scripted stand-in for the server's exact count
    return estimate_tokens("", messages, [])


def test_rolling_window_drops_whole_old_turns_and_keeps_the_cut():
    msgs = turns(10)
    limit = count(msgs) // 2
    visible, dropped = fit_turns(msgs, limit, count)
    assert visible[0]["role"] == "user" and dropped > 0 and count(visible) <= limit * 0.75
    assert count(msgs[2 * (dropped - 1):]) > limit * 0.75  # the fewest turns that fit were dropped
    more = msgs + turns(1)
    again, dropped2 = fit_turns(more, limit, count, dropped)
    assert dropped2 == dropped and again[0] is visible[0]  # the cut stays: same prompt start, cache kept


def test_a_hidden_nudge_never_starts_the_window():
    """Measured: a cut once started the window on the hidden 'your reply was cut off' nudge, so the model
    saw its own half answer without the question it was answering."""
    nudged = [{"role": "user", "content": "question 1 " + "x" * 400},
              {"role": "assistant", "text": "", "tool_calls": [], "stop": "max_tokens"},
              {"role": "user", "content": "[Your last reply was cut off ...]", "hidden": True},
              {"role": "assistant", "text": "answer 1 " + "y" * 400, "tool_calls": []}]
    msgs = turns(3) + nudged + turns(1)
    for limit in range(count(msgs), 100, -50):
        visible, _ = fit_turns(msgs, limit, count)
        assert not visible[0].get("hidden"), limit


def test_the_current_turn_is_never_dropped():
    huge = [{"role": "user", "content": "z" * 20_000}]
    visible, dropped = fit_turns(turns(3) + huge, 1000, count)
    assert visible == huge and dropped == 3
    # Measured: once the older turns were already cut, the next step indexed past the last turn (IndexError).
    visible, dropped, fits = cut_turns(turns(3) + huge, 1000, count, dropped=3)
    assert visible == huge and dropped == 3 and not fits  # kept, and said not to fit: main shortens instead
    assert cut_turns(turns(3), 10**6, count)[2]


def test_long_worker_tasks_shorten_old_tool_results():
    msgs = [{"role": "user", "content": "task"}] + [
        {"role": "tool", "tool_call_id": str(i), "name": "read_webpage", "content": "p" * 12_000} for i in range(4)]
    out, shortened = shorten_tool_results(msgs, 8_000, count)
    for older in out[1:1 + shortened]:  # the oldest results: cut to 1,200 characters plus a note
        assert older["content"].startswith("p" * 1_200) and "shortened" in older["content"]
    assert [m["content"] for m in out[3:]] == [m["content"] for m in msgs[3:]]  # newest two stay whole
    assert count(out) <= 8_000 and shortened == 2  # all that may be cut is cut; it fits, if not 25% under
    small = msgs[:3] + [{"role": "tool", "tool_call_id": "9", "name": "calculate", "content": "4"}] * 2
    again, kept = shorten_tool_results(small, 8_000, count, shortened)
    assert kept == 2 and again[1]["content"] == out[1]["content"]  # the cut stays put: same prefix, cache kept
    assert shorten_tool_results(msgs[:2], 8_000, count) == (msgs[:2], 0)


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
    again = model.requests[3]["messages"][-1]["content"]  # the earlier results back, not an error
    assert "The same search as before" in again and '"results"' in again and "https://blog.example.com/b" in again
    assert "Read one you haven't tried: https://blog.example.com/b." in again  # docs/a was already tried
    failed = model.requests[4]["messages"][-1]["content"]
    assert "already failed" in failed and "confirm: true" in failed


def test_a_confirmed_repeat_runs_again(ctx, monkeypatch):
    """A failure that may pass (a site that was busy) is the helper's call to retry: it confirms."""
    runs = []
    answers = iter([ToolResult("failed", {"error": {"message": "The page answered HTTP 503."}}),
                    ok({"url": "https://docs.example.com/a", "title": "Docs", "text": "Port 8080."})])
    monkeypatch.setitem(registry.TOOLS, "read_webpage", Tool("read_webpage", "read", registry.ReadPageIn,
                                                             lambda c, a: runs.append(a.url) or next(answers)))
    model = ScriptedProvider([tools_turn(call("read_webpage", url="https://docs.example.com/a")),
                              tools_turn(call("read_webpage", url="https://docs.example.com/a", confirm=True)),
                              submit(answer="Port 8080.", sources=["https://docs.example.com/a"])])
    out = run_worker(model, PROFILES["web"], "default port?", ctx)
    assert runs == ["https://docs.example.com/a"] * 2 and out["answer"] == "Port 8080." and "warnings" not in out


def test_a_repeated_call_that_worked_gives_its_result_again(ctx):
    """Its earlier result may have been shortened out of view since: 'its result is above' left the helper without it."""
    ctx.ws.add_table(pd.DataFrame({"a": [1, 2, 3]}), name="t.csv")
    model = ScriptedProvider([tools_turn(call("inspect_data", data="data_1")),
                              tools_turn(call("query_data", sql="SELECT sum(a) AS s FROM data_1")),
                              tools_turn(call("query_data", sql="SELECT sum(a) AS s FROM data_1")),
                              report(answer="The sum is 6.")])
    run_worker(model, ENGINEER, "sum a", ctx)
    again = model.requests[3]["messages"][-1]["content"]
    assert '"rows": [[6]]' in again and "its result again (nothing ran)" in again


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
    page = {"url": "https://docs.example.com/b", "title": "B", "text": "Rates rose."}
    monkeypatch.setitem(registry.TOOLS, "read_webpage", Tool("read_webpage", "r", registry.ReadPageIn,
                                                             lambda c, a: ok(dict(page))))
    clean = run_worker(ScriptedProvider([tools_turn(call("read_webpage", url=page["url"])),
                                         submit(answer="Rates rose.", sources=[page["url"]])]), PROFILES["web"], "x", ctx)
    assert clean["answer"] == "Rates rose." and "trail" not in clean  # a complete answer needs no briefing


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
    steps = PROFILES["web"].max_steps
    model = ScriptedProvider([tools_turn(call("web_search", query=f"q{i}")) for i in range(steps)]
                             + [ModelTurn(text="I could not finish.")])
    run_worker(model, PROFILES["web"], "loop forever", ctx)
    assert "[2 steps left" in model.requests[steps - 2]["messages"][-1]["content"]
    assert "[Last step" in model.requests[steps - 1]["messages"][-1]["content"]
    assert "steps left" not in model.requests[steps - 3]["messages"][-1]["content"]


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
    from app.providers.llamacpp import LlamaCppProvider
    from app.assistant.context import SLOT_TOKENS, prompt_limit
    assert prompt_limit(LlamaCppProvider("m", max_tokens=8_192)) == SLOT_TOKENS - 8_192
    assert prompt_limit(ScriptedProvider([])) == SLOT_TOKENS - 4_096  # no max_tokens: the default room


def test_made_lists_only_what_the_task_created(ctx):
    ctx.ws.add_table(pd.DataFrame({"a": [1, 2, 3]}), name="t.csv")
    model = ScriptedProvider([tools_turn(call("inspect_data", data="data_1")),
                              tools_turn(call("query_data", sql="SELECT a * 2 AS b FROM data_1", save=True)),
                              report(answer="Saved the doubled values as a new table.")])
    out = run_worker(model, ENGINEER, "double a", ctx)
    assert [f["id"] for f in out["files"]] == ["data_2"]  # data_1 was the input


def test_a_question_without_an_answer_is_accepted_first_time(ctx):
    model = ScriptedProvider([tools_turn(call("finish", question_for_user="Day first or month first?",
                                              options=["day", "month"]))])
    out = run_worker(model, ENGINEER, "February total?", ctx)
    assert out["question_for_user"] == "Day first or month first?" and len(model.requests) == 1


def test_a_table_is_inspected_before_anything_is_computed_on_it(ctx):
    ctx.ws.add_table(pd.DataFrame({"pizza": ["a", "b", "Total"], "quantity": [2, 3, 5]}), name="sales.csv")
    model = ScriptedProvider([tools_turn(call("query_data", sql="SELECT count(*) FROM data_1")),
                              tools_turn(call("query_data", sql="SELECT sum(quantity) FROM data_1")),
                              report(answer="10")])
    run_worker(model, ENGINEER, "How many pizzas?", ctx)
    first = model.requests[1]["messages"][-1]["content"]  # a Total row to clean: the call waits for the cleaning
    assert "had not been inspected, so inspect_data ran first" in first and "Total" in first
    second = model.requests[2]["messages"][-1]["content"]
    assert '"rows": [[10]]' in second  # once inspected, calls run normally
    later = ScriptedProvider([tools_turn(call("query_data", sql="SELECT 1 AS x FROM data_1 LIMIT 1")),
                              report(answer="ok")])
    run_worker(later, ENGINEER, "again", ctx)
    assert "not been inspected" not in later.requests[1]["messages"][-1]["content"]  # remembered on the table


def test_a_clean_table_is_inspected_and_the_call_still_runs(ctx):
    """Nothing to clean: the call runs right after the inspection, as made - not thrown away to be written again."""
    ctx.ws.add_table(pd.DataFrame({"region": ["N", "S"], "amount": [150.5, 200.25]}), name="sales.csv")
    model = ScriptedProvider([tools_turn(call("query_data", sql="SELECT sum(amount) AS total FROM data_1")),
                              report(answer="The total is 350.75.")])
    out = run_worker(model, ENGINEER, "total?", ctx)
    told = model.requests[1]["messages"][-1]["content"]
    assert "inspect_data ran first on data_1: nothing to clean" in told and "350.75" in told and "warnings" not in out


def test_a_choice_only_the_user_can_make_is_not_guessed(ctx):
    upload = ctx.ws.root / "sales_q1.csv"
    upload.write_text("\n".join(["date,amount"] + [f"{d:02d}/{m:02d}/2025,{100 + d}" for m in (1, 2, 3)
                                                  for d in (1, 3, 5, 7, 9, 11)]), encoding="utf-8")
    ctx.ws.add_file(upload)
    guess = {"op": "parse_dates", "columns": ["date"], "order": "dayfirst"}
    model = ScriptedProvider([tools_turn(call("inspect_data", data="data_1")),
                              tools_turn(call("clean_data", data="data_1", steps=[guess])),
                              ModelTurn(text="Which order do the dates use?")])
    run_worker(model, ENGINEER, "February total?", ctx)
    assert "only the user can answer" in model.requests[2]["messages"][-1]["content"]
    told = ScriptedProvider([tools_turn(call("inspect_data", data="data_1")),
                             tools_turn(call("clean_data", data="data_1", steps=[guess])), report(answer="done")])
    run_worker(told, ENGINEER, "February total? The user says the dates are day first.", ctx)
    assert '"rows_after": 18' in told.requests[2]["messages"][-1]["content"]  # the user's answer lets it run


def test_a_reply_cut_off_by_the_length_limit_gets_another_step(ctx, fake_web):
    model = ScriptedProvider([ModelTurn(text="", stop="max_tokens"),  # cut while thinking
                              ModelTurn(text="The bank raised rates by", stop="max_tokens"),  # cut mid-answer
                              tools_turn(call("read_webpage", url="https://news.example.com/rates")),
                              submit(answer="Rates rose by 0.25 points.", sources=["https://news.example.com/rates"])])
    out = run_worker(model, PROFILES["web"], "What did the central bank do?", ctx)
    assert out["answer"] == "Rates rose by 0.25 points." and "warnings" not in out and "note" not in out
    assert "cut off by the length limit" in model.requests[1]["messages"][-1]["content"]
    assert "cut off by the length limit" in model.requests[2]["messages"][-1]["content"]


def test_a_page_that_failed_is_not_read_again_with_other_find_words(ctx, monkeypatch):
    reads = []
    refused = ToolResult("failed", {"error": {"message": "The page answered HTTP 403."}})
    results = {"results": [{"title": "Blog", "url": "https://blog.example.com/p", "snippet": "..."}]}
    monkeypatch.setitem(registry.TOOLS, "web_search", Tool("web_search", "s", registry.SearchIn, lambda c, a: ok(results)))
    monkeypatch.setitem(registry.TOOLS, "read_webpage", Tool("read_webpage", "r", registry.ReadPageIn,
                                                             lambda c, a: reads.append(a.find) or refused))
    model = ScriptedProvider([tools_turn(call("web_search", query="parquet engines")),
                              tools_turn(call("read_webpage", url="https://blog.example.com/p")),
                              tools_turn(call("web_search", query="read_parquet engine docs")),
                              tools_turn(call("read_webpage", url="https://blog.example.com/p", find="engine")),
                              submit(answer="I could not read a source.")])
    run_worker(model, PROFILES["web"], "parquet engines?", ctx)
    assert reads == [None]  # the second read of the failed page, with other find words, did not run
    assert "This page already failed" in model.requests[4]["messages"][-1]["content"]


def test_no_tool_field_reaches_the_model_untyped():
    def untyped(schema) -> list:
        if isinstance(schema, dict):
            return (["anyOf"] if "anyOf" in schema else []) + [x for v in schema.values() for x in untyped(v)]
        return [x for v in schema for x in untyped(v)] if isinstance(schema, list) else []
    every = [t.spec() for t in registry.TOOLS.values()] + [workers._submit_spec(p) for p in PROFILES.values()]
    assert not any(untyped(s["input_schema"]) for s in every)  # Gemma's template renders 'type', not anyOf
    options = registry.TOOLS["ask_user"].spec()["input_schema"]["properties"]["options"]
    assert options["type"] == "array" and options["items"] == {"type": "string"} and options["nullable"] is True



def test_the_web_helper_hands_file_links_to_main_and_never_invents_one(ctx, monkeypatch):
    """A file the task wants goes back as a link for main to download with the user's approval; a link no
    page gave is refused once."""
    from app.tools import web as web_tools
    page = {"url": "https://data.example.com/bikes", "title": "City bikes", "site": "data.example.com",
            "text": "Trips per station.", "files": ["https://data.example.com/files/trips-2025.csv"]}
    monkeypatch.setitem(registry.TOOLS, "read_webpage", Tool("read_webpage", "r", registry.TOOLS["read_webpage"].input_model,
                                                    lambda c, a: ToolResult("ok", dict(page))))
    made_up = {"answer": "The city publishes trip data.", "sources": [page["url"]],
               "downloads": ["https://data.example.com/files/trips-2026.csv"]}
    right = {**made_up, "downloads": ["https://data.example.com/files/trips-2025.csv"]}
    model = ScriptedProvider([tools_turn(call("read_webpage", url=page["url"])),
                              tools_turn(call("submit_result", **made_up)), tools_turn(call("submit_result", **right))])
    out = run_worker(model, PROFILES["web"], "Find the city bike trip data.", ctx)
    assert "trips-2026.csv is not a link any page you read gave" in model.requests[2]["messages"][-1]["content"]
    assert out["downloads"] == right["downloads"] and "files(mode='download'" in out["download_with"]
    assert "downloads" not in run_worker(ScriptedProvider([tools_turn(call("submit_result", answer="No file found."))]),
                                         PROFILES["web"], "x", ctx)


def test_an_answer_that_read_no_page_gets_one_chance_to_read_one(ctx, fake_web):
    """Measured on the 26B: asked for a public data file and an overview, the helper found a CSV's link, read
    nothing and wrote "typically such datasets include columns such as ..." - a guess main passed on."""
    guess = "Here is a dataset. Typically such datasets include a title, a year and a rating."
    model = ScriptedProvider([tools_turn(call("web_search", query="rates")), submit(answer=guess),
                              tools_turn(call("read_webpage", url="https://news.example.com/rates")),
                              submit(answer="Rates rose by 0.25 points.", sources=["https://news.example.com/rates"])])
    out = run_worker(model, PROFILES["web"], "What did the central bank do?", ctx)
    assert "without reading any page" in model.requests[2]["messages"][-1]["content"]
    assert out["answer"] == "Rates rose by 0.25 points." and "warnings" not in out
    nothing = ScriptedProvider([tools_turn(call("web_search", query="x")),
                                submit(answer="I could not find any information about it.")])
    assert "warnings" not in run_worker(nothing, PROFILES["web"], "x", ctx)  # saying nothing was found is fine


def test_a_github_files_raw_link_counts_as_the_page_found(ctx, monkeypatch):
    """Measured: search found the file's page (github.com/.../blob/...), the helper rightly gave its raw link,
    and main got no download - the raw link was 'not a link any page gave'."""
    blob = "https://github.com/someone/datasets/blob/master/Movie+Ratings.csv"
    raw = "https://raw.githubusercontent.com/someone/datasets/master/Movie+Ratings.csv"
    monkeypatch.setitem(registry.TOOLS, "web_search", Tool("web_search", "s", registry.SearchIn, lambda c, a: ok(
        {"results": [{"title": "Movie Ratings.csv", "url": blob, "snippet": "a collection of datasets"}]})))
    page = {"url": raw, "title": "Movie+Ratings.csv", "text": "Film,Genre,Rating\nUp,Animation,98"}
    monkeypatch.setitem(registry.TOOLS, "read_webpage", Tool("read_webpage", "r", registry.ReadPageIn,
                                                             lambda c, a: ok(dict(page))))
    model = ScriptedProvider([tools_turn(call("web_search", query="movie ratings csv")),
                              tools_turn(call("read_webpage", url=blob)),
                              submit(answer=f"Columns: Film, Genre, Rating. Download: {raw}", sources=[raw],
                                     downloads=[raw])])
    out = run_worker(model, PROFILES["web"], "Find a public movie ratings CSV and describe it.", ctx)
    assert out["downloads"] == [raw] and "warnings" not in out and "download_with" in out


# ---------------------------------------------------------------- a helper acts or finishes

def test_every_step_requires_a_call_and_the_last_offers_only_the_finish(ctx, fake_web):
    """Measured on the 26B: llama-server's tool_choice 'required' holds with Gemma 4, thinking on and off - so a
    helper can only act or finish, and nothing is ever read out of prose."""
    steps = PROFILES["web"].max_steps
    model = ScriptedProvider([tools_turn(call("web_search", query=f"q{i}")) for i in range(steps)]
                             + [submit(answer="I could not finish.")])
    out = run_worker(model, PROFILES["web"], "loop forever", ctx)
    assert all(r["require_tool"] for r in model.requests)
    assert len(model.requests[0]["tools"]) > 1 and [t["name"] for t in model.requests[-1]["tools"]] == ["submit_result"]
    assert out["answer"] == "I could not finish."


def test_a_reply_without_a_call_gets_one_hint_and_nothing_is_read_out_of_it(ctx, fake_web):
    model = ScriptedProvider([ModelTurn(text="Rates rose by 0.25 points."),
                              tools_turn(call("read_webpage", url="https://news.example.com/rates")),
                              submit(answer="Rates rose by 0.25 points.", sources=["https://news.example.com/rates"])])
    out = run_worker(model, PROFILES["web"], "rates?", ctx)
    assert model.requests[1]["messages"][-1]["content"] == "[Call one of your tools - or submit_result to hand in your result.]"
    assert out["sources"] == ["https://news.example.com/rates"] and "warnings" not in out


def test_a_reply_cut_off_at_the_last_step_ends_without_a_result(ctx, fake_web):
    steps = PROFILES["web"].max_steps
    model = ScriptedProvider([tools_turn(call("web_search", query=f"q{i}")) for i in range(steps)]
                             + [ModelTurn(text="", stop="max_tokens")])
    out = run_worker(model, PROFILES["web"], "rates?", ctx)
    assert out["warnings"] == ["the helper ran out of steps without finishing"] and "trail" in out


def test_a_read_runs_as_the_helper_made_it(ctx, monkeypatch):
    """Nothing is added to the helper's call (it was given the last search as find): read_webpage itself says when
    a page was cut, and how to read the rest."""
    asked = []
    results = {"results": [{"title": "Docs", "url": "https://docs.example.com/a", "snippet": "..."}]}
    monkeypatch.setitem(registry.TOOLS, "web_search", Tool("web_search", "s", registry.SearchIn, lambda c, a: ok(results)))
    monkeypatch.setitem(registry.TOOLS, "read_webpage", Tool("read_webpage", "r", registry.ReadPageIn,
                                                             lambda c, a: asked.append(a.find) or ok({"url": a.url})))
    model = ScriptedProvider([tools_turn(call("web_search", query="server default port")),
                              tools_turn(call("read_webpage", url="https://docs.example.com/a")),
                              tools_turn(call("read_webpage", url="https://docs.example.com/a", find="timeout")),
                              submit(answer="I could not find it.")])
    run_worker(model, PROFILES["web"], "default port?", ctx)
    assert asked == [None, "timeout"]


def test_sources_are_asked_for_not_guessed(ctx, monkeypatch):
    """A result without sources is not credited with pages or passages by the app: the check asks for them."""
    from app.tools.memory import remember_turn
    remember_turn(ctx.index, "old1", "How far ahead should we forecast?", "We agreed on 6 months.")
    model = ScriptedProvider([tools_turn(call("recall", query="forecast horizon")), submit(answer="We agreed on 6 months."),
                              submit(answer="We agreed on 6 months.", sources=["conversation old1"])])
    out = run_worker(model, PROFILES["knowledge"], "forecast horizon?", ctx)
    assert "no sources given" in model.requests[2]["messages"][-1]["content"]
    assert out["sources"] == ["conversation old1"] and "warnings" not in out
    page = {"url": "https://example.org/one-piece", "title": "One Piece", "site": "example.org",
            "text": "One Piece is a manga by Eiichiro Oda, first published in 1997."}
    monkeypatch.setitem(registry.TOOLS, "read_webpage", Tool("read_webpage", "r", registry.TOOLS["read_webpage"].input_model,
                                                             lambda c, a: ToolResult("ok", dict(page))))
    web = ScriptedProvider([tools_turn(call("read_webpage", url=page["url"])),
                            submit(answer="One Piece is a manga by Eiichiro Oda."),
                            submit(answer="One Piece is a manga by Eiichiro Oda.", sources=[page["url"]])])
    out = run_worker(web, PROFILES["web"], "Who created One Piece?", ctx)
    assert "no sources given" in web.requests[2]["messages"][-1]["content"] and out["sources"] == [page["url"]]


def test_a_file_link_written_in_the_answer_is_asked_for_in_downloads(ctx, monkeypatch):
    """Seen in a real run: the Iris CSV's link was in the answer, not in downloads - main got nothing to download."""
    link = "https://huggingface.co/datasets/scikit-learn/iris/resolve/main/Iris.csv?download=true"
    page = {"url": "https://huggingface.co/datasets/scikit-learn/iris/blob/main/Iris.csv", "title": "Iris.csv",
            "site": "huggingface.co", "text": "Iris dataset.", "files": [link]}
    monkeypatch.setitem(registry.TOOLS, "read_webpage", Tool("read_webpage", "r", registry.TOOLS["read_webpage"].input_model,
                                                             lambda c, a: ToolResult("ok", dict(page))))
    answer = f"A direct CSV link: {link}."
    model = ScriptedProvider([tools_turn(call("read_webpage", url=page["url"])),
                              submit(answer=answer, sources=[page["url"]]),
                              submit(answer=answer, sources=[page["url"]], downloads=[link])])
    out = run_worker(model, PROFILES["web"], "Find the Iris CSV.", ctx)
    assert "give it in downloads" in model.requests[2]["messages"][-1]["content"]
    assert out["downloads"] == [link] and "download_with" in out and "warnings" not in out
