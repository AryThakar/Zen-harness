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
from app.tools import agent_md


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


def test_changes_from_another_site_are_refused(app_and_slots):
    """An upload is a 'simple' request: a page on another site could send one with no preflight."""
    client, _, _ = app_and_slots
    ws = client.post("/assistant/workspaces", json={"name": "Shop"}).json()
    upload = {"file": ("notes.txt", b"hello", "text/plain")}
    for origin in ("https://evil.example", "null"):  # another site; a sandboxed page
        r = client.post(f"/assistant/workspaces/{ws['id']}/files", files=upload, headers={"Origin": origin})
        assert r.status_code == 403, origin
    assert client.post(f"/assistant/workspaces/{ws['id']}/files", files=upload,
                       headers={"Origin": "http://testserver"}).status_code == 201  # this app's own page
    assert client.get("/assistant/workspaces", headers={"Origin": "https://evil.example"}).status_code == 200


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
    assert roles == ["user", "assistant", "tool", "assistant", "trace"]  # how the turn ran, for the developer view
    trace = shown["messages"][-1]["trace"]
    assert trace["ended"] == "done" and [t["name"] for t in trace["tools"]] == ["calculate"]
    assert client.get(f"/assistant/chats/{chat_id}/traces").json()[-1]["steps"] == trace["steps"]
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


def test_the_user_deletes_a_file_and_clears_zens_working_files(app_and_slots):
    """Before 2026-10-17 only asking Zen could delete a file, and the helpers' scripts and in-between tables piled up."""
    client, _, store = app_and_slots
    ws_id, _ = workspace_and_chat(client)
    doc = client.post(f"/assistant/workspaces/{ws_id}/files",
                      files={"file": ("policy.md", b"# Returns\n\nWithin 14 days with the bill.", "text/markdown")}).json()
    ws, index = store.open(ws_id)
    script = ws.add_bytes("file", b"print(1)", ".py", "work/check.py")
    step = ws.add_table(__import__("pandas").DataFrame({"a": [1, 2]}), "work/sales-query.csv")
    searchable = index.count(("document",))
    assert client.delete(f"/assistant/workspaces/{ws_id}/items/{doc['file']}").status_code == 204
    names = {i["name"] for i in client.get(f"/assistant/workspaces/{ws_id}/items").json()}
    assert "policy.md" not in names and index.count(("document",)) < searchable  # gone from search too
    memory = next(i["id"] for i in client.get(f"/assistant/workspaces/{ws_id}/items").json() if i.get("memory"))
    assert client.delete(f"/assistant/workspaces/{ws_id}/items/{memory}").status_code == 409  # the memory stays
    assert client.delete(f"/assistant/workspaces/{ws_id}/working").json() == {"deleted": 2}
    assert script not in ws.ids() and step not in ws.ids() and memory in ws.ids()
    assert client.delete(f"/assistant/workspaces/{ws_id}/items/data_9").status_code == 404


def test_files_sent_with_a_message_are_shown_on_it_again(app_and_slots):
    client, slots, _ = app_and_slots
    ws_id, chat_id = workspace_and_chat(client)
    client.post(f"/assistant/workspaces/{ws_id}/files",
                files={"file": ("sales.csv", b"region,amount\nNorth,10\nSouth,20\n", "text/csv")})
    slots.main.turns = [ModelTurn(text="Two regions.", stop="end"), ModelTurn(text="Hello.", stop="end")]
    sent = events(client.post(f"/assistant/chats/{chat_id}/messages",
                              json={"text": "What is in it?", "files": ["data_1", "data_1", "data_7"]}))
    assert sent[-1][0] == "done"
    events(client.post(f"/assistant/chats/{chat_id}/messages", json={"text": "Thanks"}))
    users = [m for m in client.get(f"/assistant/chats/{chat_id}/messages").json()["messages"] if m["role"] == "user"]
    # once each, and only what is in the project
    assert users[0]["files"] == [{"id": "data_1", "kind": "table", "name": "sales.csv", "rows": 2}]
    assert "files" not in users[1]
    bad = client.post(f"/assistant/chats/{chat_id}/messages", json={"text": "Hi", "files": ["../../etc/passwd"]})
    assert bad.status_code == 422


def test_upload_size_is_limited(app_and_slots):
    client, _, _ = app_and_slots
    ws_id, _ = workspace_and_chat(client)
    client.app.state.assistant_upload_limit = 10
    r = client.post(f"/assistant/workspaces/{ws_id}/files", files={"file": ("big.csv", b"a,b\n" * 100, "text/csv")})
    assert r.status_code == 413


def test_remembering_edits_agent_md_which_is_in_view_and_never_in_the_history(app_and_slots):
    """'Remember X' is an edit of agent.md (no approval step); the next turn's system prompt holds it as it is now,
    and no stored message carries it - it is never in the history (the user's design: in view every turn, stripped
    from the history)."""
    client, slots, store = app_and_slots
    ws_id, chat_id = workspace_and_chat(client)
    items = client.get(f"/assistant/workspaces/{ws_id}/items").json()
    assert items[0]["name"] == "agent.md" and items[0]["memory"] is True  # made with the workspace, first in the panel
    slots.main.turns = [ModelTurn(text="", tool_calls=[call("files", mode="edit", name="agent.md",
                                                            old_text="2,000 characters.\n",
                                                            new_text="2,000 characters.\n\n- Revenue is net_sales.\n")],
                                  stop="tool_use"),
                        ModelTurn(text="Noted in agent.md.", stop="end"),
                        ModelTurn(text="Revenue is net_sales.", stop="end")]
    stream = events(client.post(f"/assistant/chats/{chat_id}/messages", json={"text": "Remember: revenue is net_sales"}))
    assert not any(k == "question" for k, _ in stream)  # changed without asking
    ws, _ = store.open(ws_id)
    assert agent_md.body(ws) == "- Revenue is net_sales."
    events(client.post(f"/assistant/chats/{chat_id}/messages", json={"text": "Which column is revenue?"}))
    assert "- Revenue is net_sales." in slots.main.requests[-1]["system"]  # in view, as it is now
    assert "(nothing remembered yet)" in slots.main.requests[0]["system"]  # the first turn saw it as it was then
    stored = json.dumps(store.messages(chat_id), ensure_ascii=False)
    assert "your standing memory" not in stored  # never in the history
    edit = client.put(f"/assistant/workspaces/{ws_id}/items/{items[0]['id']}/text", json={"text": "x" * 2001})
    assert edit.status_code == 422 and "at most 2,000 characters" in edit.json()["detail"]  # the user's edit too


def test_web_icons_are_served_sandboxed(app_and_slots, monkeypatch):
    client, _, _ = app_and_slots
    monkeypatch.setattr(assistant_api.web, "fetch_icon", lambda page, favicon=None: (b"<svg/>", "image/svg+xml"))
    r = client.get("/assistant/web/icon", params={"page": "https://example.com/a"})
    assert r.status_code == 200 and "sandbox" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


def test_the_user_edits_a_file_and_runs_it_from_the_file_panel(app_and_slots):
    client, _, store = app_and_slots
    ws_id, _ = workspace_and_chat(client)
    base = f"/assistant/workspaces/{ws_id}"
    up = client.post(f"{base}/files", files={"file": ("game.py", b"print('v1')", "text/x-python")}).json()
    item = up["id"]
    ws, _ = store.open(ws_id)
    ws.set_report(item, "# A game\n\n## Facts (written by the app)\n- Last run: passed")
    saved = client.put(f"{base}/items/{item}/text", json={"text": "name = input()\nprint('hi', name)\n"})
    assert saved.status_code == 200 and client.get(f"{base}/items/{item}/file").text.startswith("name = input()")
    assert "edited by the user" in ws.report(item)  # the report says it describes the version before
    ran = in_console(client, base, item, ["Ary"])
    assert ran[-1][0] == "end" and ran[-1][1]["exit_code"] == 0 and printed(ran) == "hi Ary\n"
    client.put(f"{base}/items/{item}/text", json={"text": "open('out.txt', 'w').write('x')\n"})
    first = in_console(client, base, item)[-1][1]["files"][0]["id"]
    again = in_console(client, base, item)[-1][1]["files"][0]
    assert again["id"] == first and again["updated"] and ws.entry(first)["parent"] == item  # run again: updated in place
    table = client.post(f"{base}/files", files={"file": ("t.csv", b"a\n1\n", "text/csv")}).json()["id"]
    assert client.put(f"{base}/items/{table}/text", json={"text": "x"}).status_code == 422


def in_console(client, base, item, lines=()) -> list[tuple[str, dict]]:
    """Run a file in the console, the lines typed ahead (the program reads each when it asks): its events."""
    session = client.post(f"{base}/items/{item}/console").json()["session"]
    for line in lines:
        assert client.post(f"{base}/console/{session}/input", json={"line": line}).json() == {"ok": True}
    with client.stream("GET", f"{base}/console/{session}") as r:
        body = "".join(r.iter_text())
    return [(e.split("\n", 1)[0].removeprefix("event: "), json.loads(e.split("data: ", 1)[1]))
            for e in body.strip().split("\n\n")]


def printed(events) -> str:
    return "".join(d["text"] for kind, d in events if kind == "out")


def test_a_program_runs_in_the_console_the_user_typing_while_it_runs(app_and_slots):
    """2.12: started from its file, its output streams as events, each typed line goes to its input()."""
    client, _, store = app_and_slots
    ws_id, _ = workspace_and_chat(client)
    base = f"/assistant/workspaces/{ws_id}"
    item = client.post(f"{base}/files", files={"file": ("ask.py", b"a = input('first? ')\nb = input('second? ')\n"
                                                                  b"print(a + b)\n", "text/x-python")}).json()["id"]
    events = in_console(client, base, item, ["ze", "n"])
    assert events[-1][0] == "end" and events[-1][1]["exit_code"] == 0
    assert printed(events) == "first? second? zen\n"  # "\n", not Windows' "\r\n"
    assert [d["text"] for kind, d in events if kind == "in"] == ["ze", "n"]
    ws, _ = store.open(ws_id)
    assert "› ze" in ws.report(item) and "zen" in ws.report(item)  # kept for the model to read
    session = client.post(f"{base}/items/{item}/console").json()["session"]
    client.post(f"{base}/console/{session}/stop")
    with client.stream("GET", f"{base}/console/{session}") as r:
        "".join(r.iter_text())
    assert client.post(f"{base}/console/{session}/input", json={"line": "x"}).status_code == 409  # it has ended
    assert client.get(f"{base}/console/nope").status_code == 404
    table = client.post(f"{base}/files", files={"file": ("t.csv", b"a\n1\n", "text/csv")}).json()["id"]
    assert client.post(f"{base}/items/{table}/console").status_code == 422  # only Python runs there


def test_the_file_panels_search_is_the_models_find(app_and_slots):
    client, _, _ = app_and_slots
    ws_id, _ = workspace_and_chat(client)
    base = f"/assistant/workspaces/{ws_id}"
    doc = client.post(f"{base}/files", files={"file": ("lease.md", b"# Lease\n\nThe deposit is three months of rent.\n",
                                                       "text/markdown")}).json()["file"]
    client.post(f"{base}/files", files={"file": ("notes.txt", b"buy milk", "text/plain")})
    found = client.get(f"{base}/find", params={"q": "deposit"}).json()
    assert found["found"] == 1 and found["files"][0]["id"] == doc and "deposit" in found["files"][0]["match"].lower()
    assert client.get(f"{base}/find", params={"q": "x"}).status_code == 422  # a query too short to mean anything
