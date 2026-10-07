"""The assistant's HTTP API end to end, with scripted models on both slots."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.providers import ScriptedProvider
from app.providers.base import ModelTurn
from app.providers.scripted import call
from app.api import assistant as assistant_api
from app.assistant.store import Store
from app.main import create_app
from app.rag.embed import HashEmbedder
from app.tools.notes import pinned_text, save_note


class Slots:
    """Scripted models whose scripts a test can set after the app has started."""

    def __init__(self):
        self.main, self.worker = ScriptedProvider([]), ScriptedProvider([])


@pytest.fixture
def app_and_slots(tmp_path):
    slots = Slots()
    store = Store(tmp_path / "assistant", HashEmbedder())
    app = create_app(assistant_parts=(store, slots.main, slots.worker))
    with TestClient(app) as client:
        yield client, slots, store
    store.close()


def events(response) -> list[tuple[str, dict]]:
    out = []
    for block in response.text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def workspace_and_chat(client) -> tuple[str, str]:
    ws = client.post("/assistant/workspaces", json={"name": "Shop"}).json()
    chat = client.post(f"/assistant/workspaces/{ws['id']}/chats").json()
    return ws["id"], chat["id"]


def test_the_chat_page_and_health_are_served(app_and_slots):
    client, _, _ = app_and_slots
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "<title>Zen</title>" in r.text and 'src="/static/js/app.js"' in r.text
    for path in ("/static/app.css", "/static/js/app.js", "/static/fonts/fonts.css", "/static/sounds/startup.ogg"):
        assert client.get(path).status_code == 200, path
    assert 'export const API = "/assistant"' in client.get("/static/js/api.js").text
    assert client.get("/health").json() == {"status": "ok"}


def test_workspaces_and_chats_lifecycle(app_and_slots):
    client, _, store = app_and_slots
    ws_id, chat_id = workspace_and_chat(client)
    assert client.get("/assistant/workspaces").json()[0]["chats"] == 1
    assert client.patch(f"/assistant/workspaces/{ws_id}", json={"name": "Bakery"}).json()["name"] == "Bakery"
    assert client.get(f"/assistant/workspaces/{ws_id}/chats").json()[0]["id"] == chat_id
    assert client.delete(f"/assistant/chats/{chat_id}").status_code == 204
    assert client.get(f"/assistant/chats/{chat_id}/messages").status_code == 404
    folder = store.folder(ws_id)
    assert folder.exists() and client.delete(f"/assistant/workspaces/{ws_id}").status_code == 204
    assert not folder.exists() and client.get(f"/assistant/workspaces/{ws_id}/chats").status_code == 404
    for bad in ("../../etc", "nothere00000"):
        assert client.delete(f"/assistant/workspaces/{bad}").status_code == 404


def test_a_turn_streams_events_and_redisplays_without_internals(app_and_slots):
    client, slots, _ = app_and_slots
    _, chat_id = workspace_and_chat(client)
    slots.main.turns = [ModelTurn(text="", tool_calls=[call("calculate", expression="2400 * 0.15")], stop="tool_use"),
                        ModelTurn(text="It is 360.", stop="end")]
    stream = events(client.post(f"/assistant/chats/{chat_id}/messages", json={"text": "15% of 2400?"}))
    kinds = [k for k, _ in stream]
    assert kinds[0] == "tool_start" and "tool_end" in kinds and stream[-1] == ("done", {"answer": "It is 360."})
    shown = client.get(f"/assistant/chats/{chat_id}/messages").json()
    assert shown["chat"]["title"] == "15% of 2400?"
    roles = [m["role"] for m in shown["messages"]]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert shown["messages"][0]["text"] == "15% of 2400?"  # without the time stamp the model sees
    assert shown["messages"][1]["tools"][0]["name"] == "calculate" and "raw" not in shown["messages"][1]


def test_a_question_and_its_answer(app_and_slots):
    client, slots, _ = app_and_slots
    _, chat_id = workspace_and_chat(client)
    slots.main.turns = [ModelTurn(text="", tool_calls=[call("ask_user", question="Which year?", options=["2024", "2025"])],
                                  stop="tool_use"),
                        ModelTurn(text="Total for 2025: 100.", stop="end")]
    asked = events(client.post(f"/assistant/chats/{chat_id}/messages", json={"text": "Total?"}))
    assert asked[-1][0] == "question" and asked[-1][1]["options"] == ["2024", "2025"]
    assert any(k == "artifact" and d["artifact"]["type"] == "question" for k, d in asked)
    done = events(client.post(f"/assistant/chats/{chat_id}/answer", json={"answer": "2025"}))
    assert done[-1] == ("done", {"answer": "Total for 2025: 100."})
    again = events(client.post(f"/assistant/chats/{chat_id}/answer", json={"answer": "2025"}))
    assert again[-1][0] == "error"  # nothing is waiting any more


def test_uploads_go_where_they_belong(app_and_slots):
    client, _, store = app_and_slots
    ws_id, _ = workspace_and_chat(client)
    table = client.post(f"/assistant/workspaces/{ws_id}/files",
                        files={"file": ("sales.csv", b"region,amount\nNorth,10\nSouth,20\n", "text/csv")}).json()
    assert table == {"id": "data_1", "kind": "table", "name": "sales.csv", "rows": 2}
    doc = client.post(f"/assistant/workspaces/{ws_id}/files",
                      files={"file": ("policy.md", b"# Returns\n\nWithin 14 days with the bill.", "text/markdown")}).json()
    assert doc["kind"] == "document" and doc["passages"] >= 1
    other = client.post(f"/assistant/workspaces/{ws_id}/files",
                        files={"file": ("convert.py", b"print(1)", "text/x-python")}).json()
    assert other["kind"] == "file"
    items = {i["id"]: i for i in client.get(f"/assistant/workspaces/{ws_id}/items").json()}
    assert items["data_1"]["kind"] == "table" and items[doc["file"]]["kind"] == "file"
    rows = client.get(f"/assistant/workspaces/{ws_id}/items/data_1/rows").json()
    assert rows["columns"] == ["region", "amount"] and rows["total_rows"] == 2
    _, index = store.open(ws_id)
    assert index.count(("document",)) >= 1  # the document is searchable by ask_knowledge
    served = client.get(f"/assistant/workspaces/{ws_id}/items/{other['id']}/file")
    assert served.headers["content-type"] == "application/octet-stream" and "attachment" in served.headers[
        "content-disposition"] and "sandbox" in served.headers["content-security-policy"]
    bad = client.post(f"/assistant/workspaces/{ws_id}/files", files={"file": ("broken.csv", b"\x00\x01", "text/csv")})
    assert bad.status_code == 422 and "No columns with data" in bad.json()["detail"]  # refused with a reason
    assert client.get(f"/assistant/workspaces/{ws_id}/items/data_9/rows").status_code == 404


def test_upload_size_is_limited(app_and_slots):
    client, _, _ = app_and_slots
    ws_id, _ = workspace_and_chat(client)
    client.app.state.assistant_upload_limit = 10
    r = client.post(f"/assistant/workspaces/{ws_id}/files", files={"file": ("big.csv", b"a,b\n" * 100, "text/csv")})
    assert r.status_code == 413


def test_the_latest_notes_are_pinned_and_only_they_are_listed(app_and_slots):
    client, slots, store = app_and_slots
    ws_id, chat_id = workspace_and_chat(client)
    slots.main.turns = [ModelTurn(text="", tool_calls=[call("save_note", title="Revenue", content="Use net_sales.")],
                                  stop="tool_use"),
                        ModelTurn(text="Saved.", stop="end")]
    stream = events(client.post(f"/assistant/chats/{chat_id}/messages", json={"text": "Remember: revenue is net_sales"}))
    assert any(k == "artifact" and d["artifact"]["type"] == "note" for k, d in stream)
    ws, _ = store.open(ws_id)
    assert "Use net_sales." in pinned_text(ws)  # pinned at once: no approval step
    for n in range(6):
        save_note(ws, f"Fact {n}", f"Fact number {n}.")
    notes = [i for i in client.get(f"/assistant/workspaces/{ws_id}/items").json() if i["kind"] == "note"]
    assert [i["title"] for i in notes] == [f"Fact {n}" for n in range(1, 6)] and all(i["pinned"] for i in notes)
    assert client.post(f"/assistant/workspaces/{ws_id}/notes/note_1/pin", json={"pin": True}).status_code == 404


def test_web_icons_are_served_sandboxed(app_and_slots, monkeypatch):
    client, _, _ = app_and_slots
    monkeypatch.setattr(assistant_api.web, "fetch_icon", lambda page, favicon=None: (b"<svg/>", "image/svg+xml"))
    r = client.get("/assistant/web/icon", params={"page": "https://example.com/a"})
    assert r.status_code == 200 and "sandbox" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
