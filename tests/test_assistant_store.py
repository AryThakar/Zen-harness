"""The store: workspaces, chats, messages, and what deleting each one removes."""

from __future__ import annotations

import pandas as pd
import pytest

from app.assistant.store import Store
from app.tools.memory import recall, remember_turn
from app.tools.search_index import Passage


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "assistant")
    yield s
    s.close()


def test_workspaces_chats_and_messages_keep_their_order(store):
    ws = store.create_workspace("Shop")
    first = store.create_chat(ws["id"])
    second = store.create_chat(ws["id"], "Pricing")
    assert first["title"] == "New chat" and first["dropped"] == 0 and first["pending"] is None
    store.append(first["id"], {"role": "user", "content": "hi", "at": "2026-10-03T10:00:00+05:30"})
    store.append(first["id"], {"role": "assistant", "text": "hello", "tool_calls": []})
    store.append(first["id"], {"role": "artifact", "type": "chart"})
    assert [m["role"] for m in store.messages(first["id"])] == ["user", "assistant", "artifact"]
    assert [m["role"] for m in store.messages(first["id"], for_model=True)] == ["user", "assistant"]
    assert store.messages(first["id"])[0]["at"] == "2026-10-03T10:00:00+05:30"
    assert store.chats(ws["id"])[0]["id"] == first["id"]  # the chat used last comes first
    assert {c["id"] for c in store.chats(ws["id"])} == {first["id"], second["id"]}
    assert store.workspaces()[0]["chats"] == 2


def test_chat_state_is_kept(store):
    ws = store.create_workspace("Shop")
    chat = store.create_chat(ws["id"])
    store.update_chat(chat["id"], dropped=3, pending={"call_id": "c1", "question": "Day or month first?"})
    again = store.chat(chat["id"])
    assert again["dropped"] == 3 and again["pending"]["question"] == "Day or month first?"
    store.update_chat(chat["id"], pending=None, title="Dates")
    assert store.chat(chat["id"])["pending"] is None and store.chat(chat["id"])["title"] == "Dates"
    with pytest.raises(ValueError):
        store.update_chat(chat["id"], workspace_id="other")


def test_deleting_a_chat_removes_its_messages_and_its_memory(store):
    ws = store.create_workspace("Shop")
    keep, drop = store.create_chat(ws["id"]), store.create_chat(ws["id"])
    _, index = store.open(ws["id"])
    remember_turn(index, keep["id"], "What discount for bulk?", "8% for 50 units or more.")
    remember_turn(index, drop["id"], "What discount for students?", "10% with a student card.")
    store.append(drop["id"], {"role": "user", "content": "x"})
    assert store.delete_chat(drop["id"]) and store.chat(drop["id"]) is None
    assert store.messages(drop["id"]) == []
    remembered = [turn["text"] for turn in recall(index, "student discount").result["turns"]]
    assert not any("student" in text for text in remembered)  # forgotten
    assert any("8%" in turn["text"] for turn in recall(index, "bulk discount").result["turns"])  # the other is kept
    assert not store.delete_chat(drop["id"])


def test_deleting_a_workspace_removes_everything_in_it(store):
    ws = store.create_workspace("Shop")
    other = store.create_workspace("Other")
    chat = store.create_chat(ws["id"])
    store.append(chat["id"], {"role": "user", "content": "x"})
    files, index = store.open(ws["id"])
    files.add_table(pd.DataFrame({"a": [1]}), name="t.csv")
    index.add("document", "file_1", "policy.md", [Passage("Returns within 14 days.")])
    folder = store.folder(ws["id"])
    assert folder.exists() and (folder / "search.sqlite").exists()
    assert store.delete_workspace(ws["id"])
    assert store.workspace(ws["id"]) is None and store.chat(chat["id"]) is None
    assert store.messages(chat["id"]) == [] and not folder.exists()
    assert store.workspace(other["id"]) is not None and store.folder(other["id"]).exists()  # untouched
    assert not store.delete_workspace(ws["id"])


def test_ids_cannot_reach_outside_the_data_folder(store):
    with pytest.raises(ValueError):
        store.folder("../../etc")
    assert store.workspace("../x") is None and not store.delete_workspace("../x")
