"""main's honesty checks (app/assistant/checks.py): the detectors on replies measured in the user's own
test chat, and each check through a whole main turn with a scripted model."""

from __future__ import annotations

import pytest

from app.providers.base import ModelTurn
from app.providers.scripted import ScriptedProvider, call
from app.assistant.checks import ANNOUNCE_NUDGE, announced, asked_to_remember, nudge_for
from app.assistant.prompts import FILE_NUDGE, REMEMBER_NUDGE
from app.assistant.main import send
from app.assistant.store import Store

# The 9 replies that announced an action and called nothing (chat 3431cc69d2ed), as they ended.
ANNOUNCEMENTS = [
    "I will now retrieve the rest of `tts.rs` so I can give you the complete explanation.\n\nOne moment.",
    "I will continue gathering all parts of `tts.rs` and then present the complete analysis.\n\n***\n\n"
    "*(Next chunk of file data follows...)*",
    "I am retrieving the remaining parts of `tts.rs` now to complete the analysis. Here is the next section:",
    "If this attempt also fails, I will need to suggest a slight workflow change.\n\nPlease wait while I execute the "
    "update to `image_7`.",
    "First, I need to download the data using the `files` tool.\nThen, I will assign it an ID, perform the analysis "
    "using `analyze_data`, and provide you with the insights.",
    "I am writing a script now.\n\n**Action:** Writing code to fetch the data.\n**Note:** I will not report the code.",
    "Now I will perform the comprehensive data analysis.\n\n**Action:** Performing data analysis on `housing_data.csv`.",
    "Once it runs, I will provide you with the model's accuracy.\n\nStarting the ML model creation now.",
    "This is an attempt to see if spatial data improves the model.\n\nI am writing and executing an updated Python "
    "script now.",
    # chat dacb378f2781: the turn ended here, the text never added
    "I'm having a little trouble locating the exact filename for that image to use in my script. I'll try one more "
    "time to find the right file name and add the text for you.",
]
# Endings that are not promises to act now: commitments, offers, waiting for the user, finished answers.
NOT_ANNOUNCEMENTS = [
    "I have saved and pinned this information. I will remember that our fiscal year starts on 1 April.",
    "My name is **Zen**, and I will refer to you as Ary.",
    "**Note:** I will always use this tool for any calculation, even simple ones, to ensure accuracy.",
    "Since I cannot perform this specific action, I will stop pursuing it.",
    "If there is any other task, please let me know, and I will execute it immediately.",
    "Once you provide these numbers, I will run the model to give you the predicted house value.",
    "Total revenue was 817,860.05 across 21,350 orders.",
    "Which of the two tables should I use?",
    # waiting for the user's details (chat 7a918668b138: each got a nudge, and an off-topic second reply)
    "I haven't noted down your coffee preferences yet.\n\nTell me, and I'll make sure to remember it for next time.",
    "Regarding your coffee, I want to make sure I save that correctly. Please tell me what you like to drink and "
    "when, and I will add it to your notes immediately.",
    # a question in bold still asks the user (chat dacb378f2781: nudged, 50 s of checking)
    "Once you pick a topic, I will ask you a series of questions one by one. At the end, I'll calculate your "
    "score.\n\n**What topic would you like?**",
    "I'll calculate it again whenever you like. Have fun :)",
]


@pytest.mark.parametrize("text", ANNOUNCEMENTS)
def test_measured_announcements_are_caught(text):
    assert announced(text)


@pytest.mark.parametrize("text", NOT_ANNOUNCEMENTS)
def test_commitments_offers_and_answers_are_not(text):
    assert not announced(text)


def test_a_claim_is_flagged_only_when_no_tool_did_anything():
    user = {"role": "user", "content": "put my name, Ary, in a note"}
    claim = "I saved a note: your name is Ary."
    assert "saved" in nudge_for(claim, [user])
    worked = [user, {"role": "tool", "tool_call_id": "c1", "content": "{}", "is_error": False}]
    assert nudge_for(claim, worked) is None
    assert "downloaded" in nudge_for("The file has been downloaded to your workspace.", [user])
    nudged = [user, {"role": "user", "content": ANNOUNCE_NUDGE, "hidden": True, "check": True}]
    assert nudge_for(claim, nudged) is None  # once per turn


def test_a_file_named_as_made_that_isnt_there_gets_a_nudge():
    """The held-out run of 2026-10-15: two writes refused, a third worked, and the reply said the changes were saved
    to changes.md - there was none. Only a file that doesn't exist counts (replayed over 333 stored replies: none)."""
    user = {"role": "user", "content": "write the changes to changes.md and the prices to prices.docx"}
    worked = [user, {"role": "tool", "tool_call_id": "c1", "content": "{}", "is_error": False}]
    have = {"prices.docx", "old_pricing.md"}.__contains__
    reply = ("I compared old_pricing.md with the new one. I have saved the detailed changes to `changes.md` and "
             "created the `prices.docx` file.")
    assert nudge_for(reply, worked, exists=have) == FILE_NUDGE.format(name="changes.md")
    assert nudge_for("I've saved the prices to `prices.docx`. I read old_pricing.md first.", worked, exists=have) is None
    assert nudge_for("I've deleted `old.txt` for you.", worked, exists=have) is None  # deleted: meant to be gone
    assert nudge_for("The notes say it has been added to docs/SETUP.md.", worked, exists=have) is None  # quoting a file
    assert nudge_for(reply, worked) is None  # nothing to check against: no nudge


def test_asked_to_remember_but_nothing_saved_gets_its_own_nudge():
    for said in ("remember that our fiscal year starts in April", "your name is Zen and my name is Ary, remember that",
                 "note this down: the deadline is Friday", "keep in mind I prefer metric units", "don't forget my PIN rule",
                 "keep this in view in every chat: always answer in metric units", "from now on reply in English"):
        assert asked_to_remember(said), said
    for said in ("do you remember what we decided?", "what did I ask you to remember?", "what is 12 times 12",
                 "can you remember the file I sent", "remember?",
                 # chat 7a918668b138: a question typed without "you" or "?", and a note named as a thing
                 "do remember when i like to drink coffee", "a note that you used to answer me on my last question",
                 "A note that you used to ans my question", "remember when we fixed the chart",
                 # chat f1e15a9a801e: a reproach about the past, typed without "?"
                 "i given you name dont you remember", "didn't you remember my tea time"):
        assert not asked_to_remember(said), said
    for said in ("please note that the deadline moved to Friday", "note that I prefer tea", "so you can remember it, "
                 "my PIN rule is 4 digits"):
        assert asked_to_remember(said), said
    user = {"role": "user", "content": "[Mon 05 Oct 2026, 14:00] remember my name is Ary", "text": "remember my name is Ary"}
    assert nudge_for("Got it, Ary. I will remember that.", [user]) == REMEMBER_NUDGE
    read = [user, {"role": "assistant", "text": "", "tool_calls": [{"id": "c0", "name": "files",
                                                                     "input": {"mode": "read", "name": "agent.md"}}]},
            {"role": "tool", "tool_call_id": "c0", "content": "{}", "is_error": False}]
    assert nudge_for("Noted: your name is Ary.", read) is not None  # reading agent.md saves nothing
    saved = [user, {"role": "assistant", "text": "", "tool_calls": [{"id": "c1", "name": "files",
                                                                      "input": {"mode": "edit", "name": "agent.md"}}]},
             {"role": "tool", "tool_call_id": "c1", "content": "{}", "is_error": False}]
    assert nudge_for("Saved: your name is Ary.", saved) is None


# ---------------------------------------------------------------- through a whole turn

@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "assistant")
    yield s
    s.close()


@pytest.fixture
def chat(store):
    return store.create_chat(store.create_workspace("Shop")["id"])


def turn(*calls) -> ModelTurn:
    return ModelTurn(text="", tool_calls=list(calls), stop="tool_use")


def answer(text: str) -> ModelTurn:
    return ModelTurn(text=text, stop="end")


def test_an_announcement_gets_one_nudge_then_the_tool_runs(store, chat):
    main = ScriptedProvider([answer("I will now add your name to agent.md.\n\nOne moment."),
                             turn(call("files", mode="write", name="agent.md", content="# agent.md\n\n- The user is Ary.\n")),
                             answer("Added to agent.md: you are Ary.")])
    events = list(send(store, chat["id"], "put my name, Ary, in your memory", main, ScriptedProvider([])))
    assert events[-1]["answer"] == "Added to agent.md: you are Ary."
    nudges = [m for m in store.messages(chat["id"]) if m.get("check")]
    assert len(nudges) == 1 and nudges[0]["hidden"] and "called no tool" in nudges[0]["content"]
    assert main.requests[1]["messages"][-1]["content"] == nudges[0]["content"]
    # the page is told the model went back to check, so a written reply doesn't look stuck meanwhile
    kinds = [e["type"] for e in events]
    assert kinds.count("checking") == 1 and kinds.index("checking") < kinds.index("tool_start")


def test_a_claim_with_no_tool_gets_one_nudge_and_a_second_reply_stands(store, chat):
    main = ScriptedProvider([answer("I have saved a note with your name."), answer("I can't save notes right now.")])
    events = list(send(store, chat["id"], "put my name, Ary, in a note", main, ScriptedProvider([])))
    assert events[-1]["answer"] == "I can't save notes right now." and len(main.requests) == 2
    assert "says something was saved" in main.requests[1]["messages"][-1]["content"]


def test_the_same_failed_call_is_not_run_twice_in_a_turn(store, chat):
    def read(n: int) -> dict:  # built by hand: call()'s own first argument is called name
        return {"id": f"read_{n}", "name": "files", "input": {"mode": "read", "name": "nothing.txt"}}

    main = ScriptedProvider([turn(read(1)), turn(read(2)), answer("There is no such file.")])
    list(send(store, chat["id"], "read nothing.txt", main, ScriptedProvider([])))
    results = [m for m in store.messages(chat["id"]) if m["role"] == "tool"]
    assert "nothing.txt" in results[0]["content"] and results[0]["is_error"]
    assert "already failed in this turn" in results[1]["content"]


def test_a_helper_task_with_made_up_data_is_refused(store, chat):
    """Measured: invented iPhone sales figures were sent to the coder to chart, then explained."""
    task = "Plot iPhone sales: 2012: 125M, 2014: 192.7M, 2016: 211.9M, 2020: 180M, 2023: 234.6M."
    main = ScriptedProvider([turn(call("analyze_or_code", task=task)), answer("I need the real figures first.")])
    coder = ScriptedProvider([])  # never called
    list(send(store, chat["id"], "make a line chart of iphone sales over time", main, coder))
    result = [m for m in store.messages(chat["id"]) if m["role"] == "tool"][0]["content"]
    assert "Not run: the task gives numbers that no tool returned" in result and "125M" in result
    assert "confirm: true" in result  # made-up on purpose (a demo) is main's call, not a dead end
    assert coder.requests == []


def test_made_up_data_the_user_asked_for_runs_once_confirmed_and_is_labelled(store, chat):
    """Measured on the 26B: a demo table for 'show me the plan tool' was refused twice as invented data, main
    rewrote the whole task each time, and the user stopped the turn."""
    task = "Make demo_movies.csv: Inception 8.8 2500000, Coco 8.4 1300000, Up 8.3 1100000."
    main = ScriptedProvider([turn(call("analyze_or_code", task=task)),
                             turn(call("analyze_or_code", task=task, confirm=True)), answer("Made it.")])
    helper = ScriptedProvider([])  # starts and fails at once: it was not refused
    list(send(store, chat["id"], "show me a demo of the plan tool", main, helper))
    results = [m["content"] for m in store.messages(chat["id"]) if m["role"] == "tool"]
    assert "Not run" in results[0] and "Not run" not in results[1] and len(helper.requests) == 1
    opening = helper.requests[0]["messages"][0]["content"]
    assert task in opening and "Treat them as sample data, and say so" in opening


def test_a_failed_call_runs_again_once_confirmed(store, chat):
    """A failure that may pass (a site that was busy) is main's call to retry."""
    def read(n):
        return {"id": f"read_{n}", "name": "files", "input": {"mode": "read", "name": "nothing.txt",
                                                               **({"confirm": True} if n == 2 else {})}}

    main = ScriptedProvider([turn(read(1)), turn(read(2)), answer("There is no such file.")])
    list(send(store, chat["id"], "read nothing.txt", main, ScriptedProvider([])))
    results = [m for m in store.messages(chat["id"]) if m["role"] == "tool"]
    assert "already failed in this turn" not in results[1]["content"] and "nothing.txt" in results[1]["content"]


def test_a_helper_task_with_the_users_own_numbers_runs(store, chat):
    task = "Compound 5,000 at 7% a year for 15 years and print the final amount."
    main = ScriptedProvider([turn(call("analyze_or_code", task=task)), answer("Done.")])
    coder = ScriptedProvider([])  # no turns: the coder starts and fails, which shows it was not refused
    list(send(store, chat["id"], "what is 5,000 at 7% compounded for 15 years? use code", main, coder))
    result = [m for m in store.messages(chat["id"]) if m["role"] == "tool"][0]["content"]
    assert "Not run" not in result and len(coder.requests) == 1


def test_remember_without_saving_is_nudged_and_agent_md_edited(store, chat):
    """Measured: "your name is Zen and my name is Ary, remember that" -> "I will remember that", nothing kept."""
    from app.tools import agent_md
    main = ScriptedProvider([answer("Understood, Ary. I will remember that."),
                             turn(call("files", mode="write", name="agent.md",
                                       content="# agent.md\n\n- The user is Ary; the assistant is Zen.\n")),
                             answer("Saved: you are Ary, and I am Zen.")])
    events = list(send(store, chat["id"], "your name is Zen and my name is Ary, remember that", main, ScriptedProvider([])))
    assert events[-1]["answer"] == "Saved: you are Ary, and I am Zen."
    assert main.requests[1]["messages"][-1]["content"] == REMEMBER_NUDGE
    ws, _ = store.open(chat["workspace_id"])
    assert "The user is Ary" in agent_md.body(ws)


def test_a_third_picture_in_one_turn_is_stopped_unless_confirmed(store, chat, monkeypatch):
    """Measured 2026-10-16: main judged its own pictures and made 4 in a row for one request (~2 min)."""
    from app.assistant import registry
    from app.assistant.registry import Tool
    from app.tools.core import ok
    made = []
    monkeypatch.setitem(registry.TOOLS, "generate_image", Tool("generate_image", "test", registry.ImageIn,
                                                               lambda c, a: made.append(a.prompt) or ok({"saved": "x.png"})))

    def draw(n: int, **more) -> dict:
        return {"id": f"draw_{n}", "name": "generate_image", "input": {"prompt": f"a cat, take {n}", **more}}

    main = ScriptedProvider([turn(draw(1)), turn(draw(2)), turn(draw(3)), turn(draw(4, confirm=True)),
                             answer("Here are the cats.")])
    list(send(store, chat["id"], "draw a cat", main, ScriptedProvider([])))
    results = [m["content"] for m in store.messages(chat["id"]) if m["role"] == "tool"]
    assert made == ["a cat, take 1", "a cat, take 2", "a cat, take 4"]  # the third stopped, the confirmed one ran
    assert "2 pictures were made in this turn already" in results[2] and "confirm: true" in results[2]


def test_main_sends_its_counted_prompt_size_so_a_long_reply_gets_the_room_left(store, chat):
    main = ScriptedProvider([answer("Hi.")])
    list(send(store, chat["id"], "hello", main, ScriptedProvider([])))
    assert isinstance(main.requests[0]["prompt_tokens"], int) and main.requests[0]["prompt_tokens"] > 0


def test_a_helper_task_naming_the_file_it_should_make_says_what_files_is_for(store, chat):
    """Measured 2026-10-16, two chats of two: main put the chart it wanted made in files and was refused with
    'Did you mean' other files; the refusal now says files are the task's inputs."""
    task = "Draw the closing prices as a line chart."
    main = ScriptedProvider([turn(call("analyze_or_code", task=task, files=["work/mrf_line_chart.png"])),
                             answer("Drawing it.")])
    helper = ScriptedProvider([])  # never called: refused at main's call
    list(send(store, chat["id"], "chart the prices", main, helper))
    result = [m for m in store.messages(chat["id"]) if m["role"] == "tool"][0]
    assert result["is_error"] and "work/mrf_line_chart.png" in result["content"]
    assert "a file the helper is to make goes in the task, not in files" in result["content"]
    assert helper.requests == []


RECIPE = ("Analyze RELIANCE.NS.csv. 1. Load the CSV, ensure Date is datetime index, sort ascending. 2. Line chart of "
          "closing prices -> save as work/reliance_line_chart.png. 3. Feature engineering: 5-day MA, RSI. 4. Train a "
          "Random Forest regressor to predict the next close.")


def test_a_task_that_spells_out_how_is_sent_back_for_what_the_user_wants(store, chat):
    """Measured 2026-10-16: main's tasks dictated files, libraries and code steps; the helper followed them in scripts
    (39 script calls to make_chart 12, predict 1, forecast 0 across the user's chats)."""
    main = ScriptedProvider([turn(call("analyze_or_code", task=RECIPE)), answer("Let me put it plainly.")])
    helper = ScriptedProvider([])  # never called
    list(send(store, chat["id"], "download public data then do analysis and ML on it", main, helper))
    result = [m for m in store.messages(chat["id"]) if m["role"] == "tool"][0]["content"]
    assert "Not run: the task says how to do the work" in result and "work/reliance_line_chart.png" in result
    assert "forecast for coming periods" in result and "confirm: true" in result
    assert helper.requests == []


def test_what_tasks_the_users_own_words_and_a_picture_to_edit_are_not_recipes():
    from app.assistant.checks import dictated_how

    def task(text, confirm=False):
        return {"id": "c1", "name": "analyze_or_code", "input": {"task": text, **({"confirm": True} if confirm else {})}}

    said = [{"role": "user", "text": "chart the prices"}]
    assert dictated_how(task("Chart Reliance's daily closes for the last month and say the trend."), said) is None
    asked = [{"role": "user", "text": "plot it with matplotlib and save it as prices.png"}]
    assert dictated_how(task("Plot the prices with matplotlib and save it as prices.png."), asked) is None
    edit = task("Blur the two labels in 'a-dramatic-side-by-side-video-game.jpg' and add ARY at the top.")
    assert dictated_how(edit, said, exists=lambda name: name == "a-dramatic-side-by-side-video-game.jpg") is None
    assert dictated_how(task("Use seaborn for the chart."), said) is not None
