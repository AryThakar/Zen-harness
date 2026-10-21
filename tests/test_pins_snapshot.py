# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Pinned and pointed-at files (docs/history/REBUILD_PLAN.md 2.2): the snapshot - what each file is, its coordinates and the
parts of it that match the message - attached to the turn being answered and never stored; pins from the page and
from the model; a file pointed at with @ named on its message."""

from __future__ import annotations

import io

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.assistant import snapshot
from app.assistant.main import send
from app.assistant.store import Store
from app.providers.base import ModelTurn
from app.providers.scripted import ScriptedProvider, call
from app.tools.file_ops import files

FILL = "Each team reported its figures to the finance office at the end of the quarter for the review. "
REPORT = f"""# Summary

Q1 went well overall. {FILL * 2}

# Revenue

Revenue grew 12% in the North region, led by phones. {FILL * 2}

# Costs

Shipping costs rose 4% after the carrier changed. {FILL * 2}
"""


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "assistant")
    yield s
    s.close()


@pytest.fixture
def chat(store):
    return store.create_chat(store.create_workspace("Shop")["id"])


@pytest.fixture
def ws_index(store, chat):
    return store.open(chat["workspace_id"])


def answer(text: str) -> ModelTurn:
    return ModelTurn(text=text, stop="end")


def run(events) -> list[dict]:
    return list(events)


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (8, 8), "gold").save(out, "PNG")
    return out.getvalue()


def test_the_snapshot_gives_each_files_coordinates_and_what_matches(ws_index):
    ws, index = ws_index
    doc = files(ws, index, "write", name="q1-report.md", content=REPORT).result["file"]
    code = files(ws, index, "write", name="stats.py", content="import math\n\n\ndef mean(xs):\n    return sum(xs) / len(xs)\n"
                                                              "\n\nclass Report:\n    pass\n").result["file"]
    table = ws.add_table(pd.DataFrame({"region": ["North", "South", "North"], "amount": [10, 20, 30]}), "sales.csv")
    picture = ws.add_image(_png(), "image.png", meta={"caption": "A lion on a rock"}, by="user")
    text = snapshot.build(ws, index, [doc, table], [code, picture], "Why did revenue grow in the North?")
    assert text.startswith(snapshot.HEAD) and text.endswith("]")
    assert "- q1-report.md (pinned) - document" in text
    assert "Outline: Summary · Revenue · Costs" in text  # the coordinates: its headings
    assert '"Revenue": Revenue grew 12% in the North region' in text and "Shipping costs" not in text  # what matches
    assert "Rows matching this message (2 shown of 2)" in text and "region=North | amount=10" in text
    assert "any count, total or average of it comes from analyze_or_code" in text  # a sample, never its numbers
    assert "- stats.py (with this message)" in text and "defines mean (line 4), Report (line 8)" in text
    assert "Shows: A lion on a rock" in text
    assert snapshot.build(ws, index, [], [], "hi") == ""  # nothing pinned or pointed at: nothing added
    start = snapshot.build(ws, index, [doc], [], "summarise it")  # nothing matches: its start
    assert "Its start (nothing in it matched the message)" in start and "Q1 went well" in start


def test_a_pinned_files_snapshot_is_on_its_turn_only_never_stored(store, chat, ws_index):
    """The user's design: the snapshot follows the conversation, like thinking - fresh each turn, gone after it."""
    ws, index = ws_index
    doc = files(ws, index, "write", name="q1-report.md", content=REPORT).result["file"]
    store.update_chat(chat["id"], pins=[doc])
    main = ScriptedProvider([answer("Phones."), answer("The carrier.")])
    run(send(store, chat["id"], "Why did revenue grow?", main, ScriptedProvider([])))
    run(send(store, chat["id"], "Why did shipping costs rise?", main, ScriptedProvider([])))
    first, second = (r["messages"] for r in main.requests)
    assert "Revenue grew 12%" in first[-1]["content"] and "q1-report.md (pinned)" in first[-1]["content"]
    earlier = [m["content"] for m in second if m["role"] == "user"]
    assert snapshot.HEAD not in earlier[0]  # the first turn's snapshot is gone from it
    assert "Shipping costs rose 4%" in earlier[1] and "Revenue grew 12%" not in earlier[1]  # this turn's, fresh
    assert all(snapshot.HEAD not in str(m.get("content")) for m in store.messages(chat["id"]))  # never stored


def test_the_model_pins_and_unpins_and_the_page_sees_it(store, chat, ws_index):
    ws, index = ws_index
    files(ws, index, "write", name="q1-report.md", content=REPORT)
    main = ScriptedProvider([ModelTurn(text="", tool_calls=[call("files", mode="pin", name="q1-report.md")], stop="tool_use"),
                             answer("Pinned."),
                             ModelTurn(text="", tool_calls=[call("files", mode="unpin", name="q1-report.md")],
                                       stop="tool_use"),
                             answer("Unpinned.")])
    events = run(send(store, chat["id"], "Keep the Q1 report in view", main, ScriptedProvider([])))
    cards = [e["artifact"] for e in events if e["type"] == "artifact"]
    assert cards and cards[-1]["type"] == "pins" and cards[-1]["pins"][0]["name"] == "q1-report.md"
    assert store.chat(chat["id"])["pins"] == [ws.find_path("q1-report.md")]
    run(send(store, chat["id"], "Stop showing it", main, ScriptedProvider([])))
    assert store.chat(chat["id"])["pins"] == []


def test_the_page_pins_files_and_points_at_one_with_its_message(tmp_path):
    from app.main import create_app
    from app.rag.embed import HashEmbedder
    app = create_app()
    app.state.assistant_store = store = Store(tmp_path / "a", HashEmbedder())
    app.state.assistant_main = main = ScriptedProvider([answer("It is a table of sales.")])
    app.state.assistant_worker = ScriptedProvider([])
    client = TestClient(app)
    ws_id = client.post("/assistant/workspaces", json={"name": "w"}).json()["id"]
    chat_id = client.post(f"/assistant/workspaces/{ws_id}/chats").json()["id"]
    ws, _ = store.open(ws_id)
    table = ws.add_table(pd.DataFrame({"region": ["North"], "amount": [1]}), "sales.csv")
    pinned = client.put(f"/assistant/chats/{chat_id}/pins", json={"pins": [table, table]})
    assert pinned.status_code == 200 and pinned.json() == {"pins": [{"id": table, "name": "sales.csv"}]}
    assert client.put(f"/assistant/chats/{chat_id}/pins", json={"pins": ["data_99"]}).status_code == 404
    assert client.put(f"/assistant/chats/{chat_id}/pins", json={"pins": []}).json() == {"pins": []}
    client.post(f"/assistant/chats/{chat_id}/messages", json={"text": "What is in it?", "files": [table]})  # @sales.csv
    sent = main.requests[0]["messages"][-1]["content"]
    assert "[Files with this message: sales.csv]" in sent and "sales.csv (with this message)" in sent
    stored = next(m for m in store.messages(chat_id) if m["role"] == "user")
    assert "[Files with this message: sales.csv]" in stored["content"] and snapshot.HEAD not in stored["content"]
    assert stored["files"][0]["name"] == "sales.csv"  # shown on the message again
