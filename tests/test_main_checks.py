"""main's honesty checks (app/assistant/checks.py): the detectors on replies measured in the user's own
test chat, and each check through a whole main turn with a scripted model."""

from __future__ import annotations

import pytest

from app.providers.base import ModelTurn
from app.providers.scripted import ScriptedProvider, call
from app.assistant.checks import ANNOUNCE_NUDGE, announced, asked_to_remember, nudge_for
from app.assistant.prompts import REMEMBER_NUDGE
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


def test_a_tool_call_written_as_text_is_nudged():
    tools = {"generate_image": None, "calculate": None}
    user = {"role": "user", "content": "a lion flying over the Himalayas"}
    # as measured (chat dacb378f2781): not valid JSON, an object quoted inside
    written = ('{\n  "action": "generate_image",\n  "action_input": "{ \\"prompt\\": \\"A majestic lion\\", '
               '\\"size\\": \\"wide\\" }"\n}')
    nudge = nudge_for(written, [user], tools)
    assert nudge and "generate_image call written out as text" in nudge
    assert "calculate" in nudge_for('```json\n{"name": "calculate", "arguments": {"expression": "2+2"}}\n```', [user], tools)
    for other in ('{"total": 12}', 'Here is the call: {"action": "generate_image"}', '{"action": "fly_away"}'):
        assert "written out" not in (nudge_for(other, [user], tools) or ""), other


def test_asked_to_remember_but_nothing_saved_gets_its_own_nudge():
    for said in ("remember that our fiscal year starts in April", "your name is Zen and my name is Ary, remember that",
                 "note this down: the deadline is Friday", "keep in mind I prefer metric units", "don't forget my PIN rule",
                 "keep this in view in every chat: always answer in metric units", "from now on reply in English"):
        assert asked_to_remember(said), said
    for said in ("do you remember what we decided?", "what did I ask you to remember?", "what is 12 times 12",
                 "can you remember the file I sent", "remember?",
                 # chat 7a918668b138: a question typed without "you" or "?", and a note named as a thing
                 "do remember when i like to drink coffee", "a note that you used to answer me on my last question",
                 "A note that you used to ans my question", "remember when we fixed the chart"):
        assert not asked_to_remember(said), said
    for said in ("please note that the deadline moved to Friday", "note that I prefer tea", "so you can remember it, "
                 "my PIN rule is 4 digits"):
        assert asked_to_remember(said), said
    user = {"role": "user", "content": "[Mon 05 Oct 2026, 14:00] remember my name is Ary", "text": "remember my name is Ary"}
    assert nudge_for("Got it, Ary. I will remember that.", [user]) == REMEMBER_NUDGE
    listed = [user, {"role": "assistant", "text": "", "tool_calls": [{"id": "c0", "name": "notes", "input": {"mode": "list"}}]},
              {"role": "tool", "tool_call_id": "c0", "content": "{}", "is_error": False}]
    assert nudge_for("Noted: your name is Ary.", listed) is not None  # listing notes saves nothing
    saved = [user, {"role": "assistant", "text": "", "tool_calls": [{"id": "c1", "name": "notes", "input": {"mode": "save"}}]},
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
    main = ScriptedProvider([answer("I will now save a note with your name.\n\nOne moment."),
                             turn(call("notes", mode="save", title="Name", content="The user is Ary.")),
                             answer("Saved a note: you are Ary.")])
    events = list(send(store, chat["id"], "put my name, Ary, in a note", main, ScriptedProvider([])))
    assert events[-1]["answer"] == "Saved a note: you are Ary."
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
    main = ScriptedProvider([turn(call("write_code", task=task)), answer("I need the real figures first.")])
    coder = ScriptedProvider([])  # never called
    list(send(store, chat["id"], "make a line chart of iphone sales over time", main, coder))
    result = [m for m in store.messages(chat["id"]) if m["role"] == "tool"][0]["content"]
    assert "Not run: the task gives numbers that no tool returned" in result and "125M" in result
    assert coder.requests == []


def test_a_helper_task_with_the_users_own_numbers_runs(store, chat):
    task = "Compound 5,000 at 7% a year for 15 years and print the final amount."
    main = ScriptedProvider([turn(call("write_code", task=task)), answer("Done.")])
    coder = ScriptedProvider([])  # no turns: the coder starts and fails, which shows it was not refused
    list(send(store, chat["id"], "what is 5,000 at 7% compounded for 15 years? use code", main, coder))
    result = [m for m in store.messages(chat["id"]) if m["role"] == "tool"][0]["content"]
    assert "Not run" not in result and len(coder.requests) == 1


def test_remember_without_a_note_is_nudged_and_the_note_saved(store, chat):
    """Measured: "your name is Zen and my name is Ary, remember that" -> "I will remember that", no note."""
    main = ScriptedProvider([answer("Understood, Ary. I will remember that."),
                             turn(call("notes", mode="save", title="Names", content="The user is Ary; the assistant is Zen.")),
                             answer("Saved: you are Ary, and I am Zen.")])
    events = list(send(store, chat["id"], "your name is Zen and my name is Ary, remember that", main, ScriptedProvider([])))
    assert events[-1]["answer"] == "Saved: you are Ary, and I am Zen."
    assert main.requests[1]["messages"][-1]["content"] == REMEMBER_NUDGE
    assert any(m["role"] == "tool" and m["name"] == "notes" for m in store.messages(chat["id"]))
