# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""The main slot's turn, driven by scripted models on both slots (no LLM)."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from app.assistant.context import prompt_limit
from app.providers.base import ModelTurn, estimate_tokens
from app.providers.scripted import ScriptedProvider, call
from app.assistant import main as main_slot
from app.assistant import registry
from app.assistant.main import MAIN, reply, send
from app.assistant.registry import Tool
from app.assistant.store import Store
from app.tools.core import ok
from app.tools.memory import recall


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "assistant")
    yield s
    s.close()


@pytest.fixture
def chat(store):
    ws = store.create_workspace("Shop")
    return store.create_chat(ws["id"])


def tools_turn(*calls) -> ModelTurn:
    return ModelTurn(text="", tool_calls=list(calls), stop="tool_use")


def answer(text: str) -> ModelTurn:
    return ModelTurn(text=text, stop="end")


def run(events) -> list[dict]:
    return list(events)


def test_a_turn_with_a_direct_tool_is_saved_remembered_and_named(store, chat):
    main = ScriptedProvider([tools_turn(call("calculate", expression="2400 * 0.15")), answer("It is 360.")])
    events = run(send(store, chat["id"], "What is 15% of 2400?", main, ScriptedProvider([])))
    kinds = [e["type"] for e in events]
    assert kinds[:2] == ["tool_start", "tool_end"] and kinds[-1] == "done" and events[-1]["answer"] == "It is 360."
    saved = store.messages(chat["id"])
    assert [m["role"] for m in saved] == ["user", "assistant", "tool", "assistant", "trace"]
    trace = saved.pop()["trace"]  # how the turn went: never sent to the model, never shown in the chat
    assert trace["ended"] == "done" and [s["calls"] for s in trace["steps"]] == [["calculate"], []]
    assert [(c["name"], c["status"]) for c in trace["tools"]] == [("calculate", "ok")] and trace["seconds"] >= 0
    assert all(m["role"] != "trace" for m in store.messages(chat["id"], for_model=True))
    assert saved[0]["content"].startswith("[") and saved[0]["text"] == "What is 15% of 2400?"  # stamped once
    assert '"result": 360' in saved[2]["content"]
    first = main.requests[0]
    assert first["system"].startswith(MAIN) and "Now:" not in first["system"]  # no clock in the system prompt
    assert {t["name"] for t in first["tools"]} == {"get_weather", "generate_image", "view_image", "draw_diagram",
                                                    "calculate", "ask_user", "files", "research_web",
                                                    "ask_knowledge", "analyze_or_code"}
    assert store.chat(chat["id"])["title"] == "What is 15% of 2400?"
    _, index = store.open(chat["workspace_id"])
    other = store.create_chat(chat["workspace_id"])
    found = recall(index, "15% of 2400", exclude_conversation=other["id"]).result["turns"]
    assert found and "It is 360." in found[0]["text"] and "calculate" in found[0]["text"]  # remembered


def test_ask_user_pauses_the_turn_and_the_answer_continues_it(store, chat):
    main = ScriptedProvider([tools_turn(call("ask_user", question="Day first or month first?",
                                             options=["Day first", "Month first"])),
                             answer("Then February's total is 636.")])
    paused = run(send(store, chat["id"], "February total?", main, ScriptedProvider([])))
    assert paused[-1]["type"] == "question" and paused[-1]["options"] == ["Day first", "Month first"]
    assert any(e["type"] == "artifact" and e["artifact"]["type"] == "question" for e in paused)
    assert store.chat(chat["id"])["pending"]["question"] == "Day first or month first?"
    assert not any(e["type"] == "done" for e in paused)
    done = run(reply(store, chat["id"], "Day first", main, ScriptedProvider([])))
    assert done[-1] == {"type": "done", "answer": "Then February's total is 636."}
    assert store.chat(chat["id"])["pending"] is None
    resumed = main.requests[1]["messages"][-1]
    assert resumed["role"] == "tool" and json.loads(resumed["content"])["answer"] == "Day first"


def test_a_chain_of_questions_is_not_cut_by_the_round_limit(store, chat):
    """A quiz: one question per call, more of them than MAX_ROUNDS - each answer starts the turn's rounds
    afresh, so the chain runs to its end."""
    n = main_slot.MAX_ROUNDS + 2
    main = ScriptedProvider([tools_turn(call("ask_user", question=f"Question {i + 1}?", options=["A", "B"]))
                             for i in range(n)] + [answer("You got 10 of 10.")])
    events = run(send(store, chat["id"], "Quiz me.", main, ScriptedProvider([])))
    for i in range(n):
        assert events[-1]["type"] == "question" and events[-1]["question"] == f"Question {i + 1}?"
        events = run(reply(store, chat["id"], "A", main, ScriptedProvider([])))
    assert events[-1]["type"] == "done" and events[-1]["answer"] == "You got 10 of 10."
    assert all(r["allow_tools"] for r in main.requests)  # never forced to answer early


def test_typing_while_a_question_waits_answers_it(store, chat):
    main = ScriptedProvider([tools_turn(call("ask_user", question="Which year?")), answer("Done for 2025.")])
    run(send(store, chat["id"], "Totals please", main, ScriptedProvider([])))
    done = run(send(store, chat["id"], "2025", main, ScriptedProvider([])))
    assert done[-1]["answer"] == "Done for 2025."
    assert [m for m in store.messages(chat["id"]) if m["role"] == "user"][-1]["text"] == "Totals please"


def test_a_delegate_runs_the_worker_and_its_progress_is_streamed(store, chat, monkeypatch):
    page = {"url": "https://news.example.com/rates", "title": "Rates", "text": "The bank raised rates by 0.25."}
    monkeypatch.setitem(registry.TOOLS, "read_webpage", Tool(
        "read_webpage", "read", registry.ReadPageIn,
        lambda c, a: ok(page, {"type": "webpage", "url": page["url"], "title": "Rates"})))
    worker = ScriptedProvider([tools_turn(call("read_webpage", url="https://news.example.com/rates")),
                               tools_turn(call("submit_result", answer="Rates rose by 0.25 points.",
                                               sources=["https://news.example.com/rates"]))])
    main = ScriptedProvider([tools_turn(call("research_web", task="What did the central bank do?")),
                             answer("Rates rose by 0.25 points ([source](https://news.example.com/rates)).")])
    events = run(send(store, chat["id"], "Any rate news?", main, worker))
    steps = [e["event"] for e in events if e["type"] == "worker"]
    assert steps[:4] == ["start", "turn", "tool", "result"] and "check" in steps
    opening = worker.requests[0]["messages"][0]["content"]  # main's task, not the user's words (the contract)
    assert opening.endswith("Your task: What did the central bank do?") and "Any rate news?" not in opening
    result = json.loads([m for m in store.messages(chat["id"]) if m["role"] == "tool"][0]["content"])
    assert result["answer"] == "Rates rose by 0.25 points." and result["worker"] == "web"
    shown = [m for m in store.messages(chat["id"]) if m["role"] == "artifact"]
    assert {a["artifact"]["type"] for a in shown} >= {"webpage", "sources"}  # kept for redisplay, never sent to a model
    assert all(m["role"] != "artifact" for m in main.requests[1]["messages"])


def test_the_workspace_travels_on_the_message(store, chat):
    ws, _ = store.open(chat["workspace_id"])
    ws.add_table(pd.DataFrame({"a": [1, 2]}), name="t.csv")
    main = ScriptedProvider([answer("ok")])
    run(send(store, chat["id"], "hi", main, ScriptedProvider([])))
    assert "t.csv: table, 2 rows" in main.requests[0]["messages"][-1]["content"]  # the workspace, on the message


def test_the_system_prompt_changes_only_with_agent_md_and_context_is_appended(store, chat):
    """The system prompt is the fixed prompt and agent.md as it is now: the same turn after turn (llama.cpp reuses the
    prompt up to its first change) until agent.md changes; what the workspace holds travels on the messages."""
    from app.tools import agent_md
    from app.tools.file_ops import files
    ws, index = store.open(chat["workspace_id"])
    main = ScriptedProvider([answer("one"), answer("two"), answer("three")])
    run(send(store, chat["id"], "first", main, ScriptedProvider([])))
    ws.add_table(pd.DataFrame({"a": [1, 2]}), name="sales.csv")
    run(send(store, chat["id"], "second", main, ScriptedProvider([])))
    files(ws, index, "write", name="agent.md", content=agent_md.HEADER + "\n- The fiscal year starts in April.\n")
    run(send(store, chat["id"], "third", main, ScriptedProvider([])))
    systems = [r["system"] for r in main.requests]
    assert systems[0] == systems[1] and systems[0].startswith(MAIN) and "(nothing remembered yet)" in systems[0]
    assert systems[2].startswith(MAIN) and systems[2].endswith("- The fiscal year starts in April.]")
    first, second, third = (r["messages"][-1]["content"] for r in main.requests)
    assert "[Workspace now:\n" in first and "agent.md" in first  # a snapshot opens the chat (agent.md is a file)
    assert "+ sales.csv: table, 2 rows" in second and "fiscal year" not in second  # only the changes
    assert "fiscal year" not in third and "~ agent.md" in third  # agent.md's text is in the system prompt only
    earlier = main.requests[2]["messages"]
    assert [m["content"] for m in earlier if m["role"] == "user"][:2] == [first, second]  # history never rewritten


def test_a_failed_turn_keeps_its_error_in_the_trace(store, chat):
    """Before, an error reached the page and was gone: nothing stored said a turn had failed, or why."""
    events = run(send(store, chat["id"], "hello", ScriptedProvider([]), ScriptedProvider([])))
    assert events[-1]["type"] == "error"
    trace = store.messages(chat["id"])[-1]["trace"]
    assert trace["ended"] == "failed" and "no more turns" in trace["error"] and trace["steps"] == []


def test_a_copy_of_the_apps_notes_never_reaches_the_user(store, chat):
    echoed = ("Saved plan.md.\n\n[Workspace now:\ndata_1  sales.csv - 3 rows]\n[agent.md - your standing memory for "
              "this project, in view in every chat; to remember something, change it with files:\n- b]")
    main = ScriptedProvider([answer(echoed)])
    events = run(send(store, chat["id"], "save my plan", main, ScriptedProvider([])))
    assert events[-1]["answer"] == "Saved plan.md." and store.messages(chat["id"], for_model=True)[-1]["text"] == "Saved plan.md."


def test_the_rolling_window_cut_is_kept_between_turns(store, chat):
    long = "x" * 9_000
    main = ScriptedProvider([answer(f"answer {i} {long}") for i in range(6)])
    for i in range(6):
        said = f"from now on always answer in metric units {long}" if i == 0 else f"question {i} {long}"
        run(send(store, chat["id"], said, main, ScriptedProvider([])))
    dropped = store.chat(chat["id"])["dropped"]
    assert dropped > 0
    last = main.requests[-1]
    assert last["system"].startswith(MAIN)
    cut_at = next(m["content"] for m in last["messages"] if "earlier turn(s) of this chat are not shown" in m["content"])
    assert "[Workspace now:" in cut_at and "standing instructions" in cut_at and "always answer in metric" in cut_at
    first = store.messages(chat["id"])[0]["said"][:19]  # '[Sat 04 Oct 2026, 10:37]': the chat's own first stamp
    assert f"This chat started {first}" in cut_at  # the start is out of view after a cut; it is said here
    said = [main_slot.STAMP.sub("", m["content"]) for m in last["messages"] if m["role"] == "user"]
    assert not any(s.startswith("from now on") for s in said)  # the turn itself was cut; its instruction kept


def test_a_turn_that_outgrows_the_slot_shortens_its_older_tool_results(store, chat, monkeypatch):
    """Several big results in one turn: the current turn is never dropped, so its older tool results are
    shortened (newest two whole) - before, the step crashed (IndexError) or was sent over the limit."""
    big = Tool("calculate", "test", registry.CalcIn, lambda c, a: ok({"text": "r" * 10_400}))
    monkeypatch.setitem(registry.TOOLS, "calculate", big)
    main = ScriptedProvider([tools_turn(call("calculate", expression=str(i))) for i in range(3)] + [answer("Done.")])
    events = run(send(store, chat["id"], "Work it out.", main, ScriptedProvider([])))
    assert events[-1]["type"] == "done"
    last = main.requests[-1]
    results = [m["content"] for m in last["messages"] if m["role"] == "tool"]
    assert "shortened to save space" in results[0] and all("r" * 10_400 in r for r in results[1:])
    assert estimate_tokens(last["system"], last["messages"], last["tools"]) <= prompt_limit(main)


def test_results_too_big_to_keep_whole_are_all_shortened_rather_than_sent_over_the_limit(store, chat, monkeypatch):
    big = Tool("calculate", "test", registry.CalcIn, lambda c, a: ok({"text": "r" * 30_000}))
    monkeypatch.setitem(registry.TOOLS, "calculate", big)
    main = ScriptedProvider([tools_turn(call("calculate", expression=str(i))) for i in range(3)] + [answer("Done.")])
    events = run(send(store, chat["id"], "Work it out.", main, ScriptedProvider([])))
    assert events[-1]["type"] == "done"
    for request in main.requests:
        assert estimate_tokens(request["system"], request["messages"], request["tools"]) <= prompt_limit(main)
    results = [m["content"] for m in main.requests[-1]["messages"] if m["role"] == "tool"]
    assert "shortened to save space" in results[0] and "shortened to save space" in results[1]


def test_a_message_too_big_for_the_context_is_refused_plainly_not_sent(store, chat):
    main = ScriptedProvider([answer("never")])
    events = run(send(store, chat["id"], "x" * 60_000, main, ScriptedProvider([])))
    assert events[-1]["type"] == "error" and "too long for the model's context" in events[-1]["message"]
    assert main.requests == []


def test_a_reply_cut_off_in_its_text_gets_another_step(store, chat):
    main = ScriptedProvider([ModelTurn(text="The answer is", stop="max_tokens"), answer("The answer is 42.")])
    events = run(send(store, chat["id"], "Question?", main, ScriptedProvider([])))
    assert events[-1]["answer"] == "The answer is 42."
    hidden = [m for m in store.messages(chat["id"]) if m.get("hidden")]
    assert len(hidden) == 1 and "cut off by the length limit" in hidden[0]["content"]
    assert [r["budget"] for r in main.requests] == [None, None]  # no thinking budget


def test_a_call_cut_off_by_the_limit_is_said_to_be_too_long_and_a_second_one_ends_plainly(store, chat):
    """Measured: a whole file written into a call takes minutes; cut off, it was taken for thinking too long."""
    cut = ModelTurn(text="", stop="max_tokens", cut_call={"name": "files", "chars": 11_800})
    main = ScriptedProvider([cut, tools_turn(call("files", mode="write", name="game.html", content="<p>hi</p>")),
                             answer("It is in game.html.")])
    events = run(send(store, chat["id"], "Make me a game page.", main, ScriptedProvider([])))
    assert events[-1]["answer"] == "It is in game.html."
    told = main.requests[1]["messages"][-1]["content"]
    assert told.startswith("[App check: your call to files was cut off") and "11,800 characters" in told
    assert "files(mode='edit')" in told and not [e for e in events if e["type"] == "question"]
    again = ScriptedProvider([cut, cut])
    events = run(send(store, chat["id"], "Make it longer.", again, ScriptedProvider([])))
    assert events[-1]["type"] == "error" and "files call grew past the model's length limit twice" in events[-1]["message"]
    assert len(again.requests) == 2


def test_thinking_until_the_limit_lets_the_user_try_again_quickly(store, chat):
    """The user's design: no thinking budget; a reply that thought until the limit and said nothing shows
    a card, and 'Try again, answer quickly' retries that once with a thinking budget."""
    main = ScriptedProvider([ModelTurn(text="", stop="max_tokens"), answer("It is 1.")])
    asked = run(send(store, chat["id"], "Find 7I1 + 12I2.", main, ScriptedProvider([])))
    card = asked[-1]
    assert card["type"] == "question" and card["options"] == [main_slot.TRY_AGAIN] and card["retry"]
    assert store.chat(chat["id"])["pending"]["retry"] and len(main.requests) == 1
    done = run(reply(store, chat["id"], main_slot.TRY_AGAIN, main, ScriptedProvider([])))
    assert done[-1]["answer"] == "It is 1."
    assert main.requests[1]["budget"] == main_slot.QUICK_BUDGET and main.requests[1]["think"]
    assert main.requests[1]["messages"][-1]["content"] == main_slot.QUICK


def test_skipping_try_again_ends_the_turn(store, chat):
    main = ScriptedProvider([ModelTurn(text="", stop="max_tokens")])
    run(send(store, chat["id"], "Find 7I1 + 12I2.", main, ScriptedProvider([])))
    done = run(reply(store, chat["id"], None, main, ScriptedProvider([])))
    assert done == [{"type": "done", "answer": ""}] and store.chat(chat["id"])["pending"] is None
    assert len(main.requests) == 1


def test_a_message_typed_while_try_again_waits_is_a_new_message(store, chat):
    """'Try again' takes no text: words sent meanwhile were taken as its click, and lost."""
    main = ScriptedProvider([ModelTurn(text="", stop="max_tokens"), answer("Hello!")])
    run(send(store, chat["id"], "Find 7I1 + 12I2.", main, ScriptedProvider([])))
    done = run(send(store, chat["id"], "Never mind, just say hello.", main, ScriptedProvider([])))
    assert done[-1]["answer"] == "Hello!" and store.chat(chat["id"])["pending"] is None
    said = [m["content"] for m in main.requests[1]["messages"] if m["role"] == "user"]
    assert "Never mind, just say hello." in said[-1] and main.requests[1]["budget"] is None


def test_one_turn_at_a_time(store, chat):
    main_slot._busy.acquire()
    try:
        events = run(send(store, chat["id"], "hi", ScriptedProvider([answer("x")]), ScriptedProvider([])))
    finally:
        main_slot._busy.release()
    assert events[0]["type"] == "error" and "still being written" in events[0]["message"]


def test_a_model_server_failure_ends_the_turn_with_an_error(store, chat):
    events = run(send(store, chat["id"], "hi", ScriptedProvider([]), ScriptedProvider([])))
    assert events[-1]["type"] == "error" and "no more turns" in events[-1]["message"]
    assert not main_slot._busy.locked()


def test_a_copy_of_the_time_stamp_never_reaches_the_user(store, chat):
    main = ScriptedProvider([answer("[Sat 03 Oct 2026, 00:10] The answer is 7."),
                             answer("[Brackets] are kept when they are not a stamp.")])
    first = run(send(store, chat["id"], "Q1", main, ScriptedProvider([])))
    assert "".join(e["text"] for e in first if e["type"] == "text").strip() == "The answer is 7."
    assert first[-1]["answer"] == "The answer is 7."
    second = run(send(store, chat["id"], "Q2", main, ScriptedProvider([])))
    assert second[-1]["answer"] == "[Brackets] are kept when they are not a stamp."


def test_ask_user_options_written_as_one_text_become_buttons(store, chat):
    main = ScriptedProvider([tools_turn(call("ask_user", question="Which order?",
                                             options="- Day first (03/04 = 3 April)\n- Month first (03/04 = March 4)"))])
    events = run(send(store, chat["id"], "Feb total?", main, ScriptedProvider([])))
    assert events[-1]["options"] == ["Day first (03/04 = 3 April)", "Month first (03/04 = March 4)"]


def _ambiguous_dates(store, chat):
    upload = store.folder(chat["workspace_id"]) / "sales_q1.csv"
    upload.write_text("\n".join(["date,amount"] + [f"{d:02d}/{m:02d}/2025,{100 + d}" for m in (1, 2, 3)
                                                   for d in (1, 3, 5, 7, 9, 11)]), encoding="utf-8")
    ws, _ = store.open(chat["workspace_id"])
    ws.add_file(upload)
    return ws


FEB = "SELECT sum(amount) AS total FROM data_2 WHERE month(date) = 2"


def test_a_question_only_the_user_can_answer_is_asked_in_place_and_the_helper_goes_on(store, chat):
    """inspect_data's date question is asked by the runner at once; the click's cleaning step is applied by
    code and the same helper task goes on (before: it ended, main asked, and the task started again)."""
    ws = _ambiguous_dates(store, chat)
    worker = ScriptedProvider([tools_turn(call("inspect_data", data="sales_q1.csv")),
                               tools_turn(call("query_data", sql=FEB)),
                               tools_turn(call("finish", answer="636", caveats="One quarter of data."))])
    main = ScriptedProvider([tools_turn(call("analyze_or_code", task="February 2025 total sales",
                                             files=["sales_q1.csv"])),
                             answer("February 2025: 636.")])
    asked = run(send(store, chat["id"], "February total?", main, worker))
    question = asked[-1]  # this request's stream ends at the question; the turn waits in place
    assert question["type"] == "question" and question["inplace"] and question["tool"] == "analyze_or_code"
    assert question["options"] == ["Day first (03/04/2025 = 3 April)", "Month first (03/04/2025 = March 4)"]
    assert main_slot.live(chat["id"]) is not None and len(worker.requests) == 1  # waiting, not ended
    done = run(reply(store, chat["id"], "Day first (03/04/2025 = 3 April)", main, worker))
    assert done[-1]["type"] == "done" and done[-1]["answer"] == "February 2025: 636."
    assert len(main.requests) == 2  # one delegation: the helper kept its work
    seen = worker.requests[1]["messages"][-1]["content"]
    assert "The user chose 'Day first" in seen and "Use sales_q1-cleaned.csv from now on" in seen
    assert "data_" not in seen  # what the helper reads names files by their names
    cleaned = ws.table("data_2")
    assert str(cleaned["date"].dtype).startswith("datetime64") and cleaned["date"].dt.month.tolist()[6] == 2
    stored = [m["artifact"] for m in store.messages(chat["id"]) if m["role"] == "artifact"]
    assert {"type": "answer", "answer": "Day first (03/04/2025 = 3 April)"} in stored  # a reload closes the card


def test_a_typed_answer_also_picks_the_option(store, chat):
    ws = _ambiguous_dates(store, chat)
    worker = ScriptedProvider([tools_turn(call("inspect_data", data="sales_q1.csv")),
                               tools_turn(call("finish", answer="r", caveats="c"))])
    main = ScriptedProvider([tools_turn(call("analyze_or_code", task="Feb total", files=["sales_q1.csv"])), answer("ok")])
    run(send(store, chat["id"], "Feb total?", main, worker))
    run(reply(store, chat["id"], "month first", main, worker))  # typed instead of clicked
    assert ws.table("data_2")["date"].dt.month.tolist()[0] == 1


def test_a_helper_asks_its_own_question_in_place(store, chat):
    worker = ScriptedProvider([tools_turn(call("ask_user", question="Which language?", options=["Python", "Rust"])),
                               tools_turn(call("files", mode="write", name="hi.py", content="print('hi')")),
                               tools_turn(call("run", file="hi.py")),
                               tools_turn(call("finish", answer="hi.py prints hi.", caveats="None."))])
    main = ScriptedProvider([tools_turn(call("analyze_or_code", task="a hello script")), answer("Saved hi.py.")])
    asked = run(send(store, chat["id"], "write a hello script", main, worker))
    assert asked[-1]["question"] == "Which language?" and asked[-1]["tool"] == "analyze_or_code"
    done = run(reply(store, chat["id"], "Python", main, worker))
    assert done[-1]["answer"] == "Saved hi.py."
    assert '"answer": "Python"' in worker.requests[1]["messages"][-1]["content"]


def test_a_skipped_helper_question_goes_on_with_the_recommended_option(store, chat):
    worker = ScriptedProvider([tools_turn(call("ask_user", question="Which chart?", options=["Line", "Bar"])),
                               ModelTurn(text="I can't do that.", stop="end"),
                               ModelTurn(text="I can't do that.", stop="end")])
    ws, _ = store.open(chat["workspace_id"])
    ws.add_table(pd.DataFrame({"a": [1]}), "t.csv")
    main = ScriptedProvider([tools_turn(call("analyze_or_code", task="chart it", files=["t.csv"])), answer("ok")])
    run(send(store, chat["id"], "chart it", main, worker))
    run(reply(store, chat["id"], None, main, worker))
    told = worker.requests[1]["messages"][-1]["content"]
    assert "The user skipped the question" in told and "recommended option (Line)" in told



def test_a_message_sent_while_a_helper_waits_in_place_is_its_answer(store, chat, monkeypatch):
    """Read in the code: a helper asking in place sets no pending question, so a message typed into the
    normal box went to a new turn - refused, as one is still running. It is the answer."""
    class Waiting:
        def waiting(self):
            return True

    handed = []
    monkeypatch.setattr(main_slot, "live", lambda chat_id: Waiting())
    monkeypatch.setattr(main_slot, "reply", lambda st, cid, text, m, w: iter(handed.append(text) or [{"type": "done"}]))
    events = list(send(store, chat["id"], "the bar chart", ScriptedProvider([]), ScriptedProvider([])))
    assert handed == ["the bar chart"] and events == [{"type": "done"}]


def test_a_tables_question_is_asked_once_and_a_typed_your_call_is_an_answer(store, chat):
    """Measured: 'Are they copies?' was asked of one table twice in a chat, an hour apart; the user's 'do whatever
    you think is best' was kept nowhere."""
    ws, _ = store.open(chat["workspace_id"])
    ws.add_table(pd.DataFrame({"sex": ["m", "m", "f"], "age": [30, 30, 41]}), "people.csv")
    first = ScriptedProvider([tools_turn(call("inspect_data", data="people.csv")),
                              tools_turn(call("finish", answer="Kept the repeats."))])
    main = ScriptedProvider([tools_turn(call("analyze_or_code", task="look at it", files=["people.csv"])), answer("ok"),
                             tools_turn(call("analyze_or_code", task="again", files=["people.csv"])), answer("ok")])
    asked = run(send(store, chat["id"], "look", main, first))
    assert asked[-1]["type"] == "question" and "repeats an earlier row" in asked[-1]["question"]
    run(reply(store, chat["id"], "do whatever you think is best", main, first))
    told = first.requests[1]["messages"][-1]["content"]
    assert "The user answered 'do whatever you think is best'" in told and "recommended option (Keep them" in told
    second = ScriptedProvider([tools_turn(call("inspect_data", data="people.csv")),
                               tools_turn(call("finish", answer="Still kept."))])
    again = run(send(store, chat["id"], "again", main, second))
    assert again[-1]["type"] == "done" and not any(e.get("type") == "question" for e in again)  # not asked twice
    assert "answered this before: 'do whatever you think is best'" in second.requests[1]["messages"][-1]["content"]
