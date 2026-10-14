"""Actions that wait for the user's permission: import_table, download_file, delete_files - the request
(nothing downloaded or deleted), the action once allowed, and the whole turn pausing for it."""

from __future__ import annotations

import json
import socket

import httpx
import pandas as pd
import pytest

from app.providers.base import ModelTurn
from app.providers.scripted import ScriptedProvider, call
from app.assistant.main import reply, send
from app.assistant.store import Store
from app.tools import approval, web
from app.tools.core import ToolError
from app.tools.files import find_files
from app.tools.search_index import SearchIndex
from app.tools.workspace import Workspace

CSV = b"region,sales\nNorth,10\nSouth,20\n"
PY = b"def average(xs):\n    return sum(xs) / len(xs)\n"
LFS = b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 31\n"


def public(host, port, proto=0):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("140.82.112.3", port))]


def site(files: dict[str, bytes], seen: list | None = None) -> httpx.Client:
    """A fake web: url -> body (html by its start), or a redirect (('redirect', to))."""
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        body = files.get(str(request.url))
        if body is None:
            return httpx.Response(404)
        if isinstance(body, tuple):
            return httpx.Response(302, headers={"location": body[1]})
        kind = "text/html" if body.lstrip().startswith(b"<") else "text/plain"
        return httpx.Response(200, content=body, headers={"content-type": kind, "content-length": str(len(body))})
    return httpx.Client(transport=httpx.MockTransport(handle))


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path / "ws")


@pytest.fixture
def index(tmp_path):
    return SearchIndex(tmp_path / "search.sqlite", embedder=None)  # keywords only


RAW = "https://raw.githubusercontent.com/o/r/main"


def test_a_github_file_page_is_described_from_its_raw_file_without_downloading_it():
    seen = []
    http = site({f"{RAW}/src/avg.py": PY}, seen)
    r = approval.download_request("https://github.com/o/r/blob/main/src/avg.py", http=http, resolve=public)
    card = r.artifact
    assert card["type"] == "question" and card["options"] == ["Download", "Cancel"] and not card["allow_text"]
    assert card["approval"] == {"action": "download_file", "url": f"{RAW}/src/avg.py", "name": "avg.py",
                                "type": "text/plain", "bytes": len(PY), "host": "raw.githubusercontent.com",
                                "github": True}
    assert f"avg.py ({len(PY)} bytes) from GitHub" in card["question"]
    assert seen[0].headers["accept-encoding"] == "identity"  # the stated size is the file's own


def test_a_half_converted_raw_blob_link_is_repaired():
    """Measured twice: the model turned a valid github.com/.../blob/... link into
    raw.githubusercontent.com/.../blob/..., which answers 404."""
    http = site({f"{RAW}/src/avg.py": PY})
    r = approval.download_request("https://raw.githubusercontent.com/o/r/blob/main/src/avg.py", http=http,
                                  resolve=public)
    assert r.artifact["approval"]["url"] == f"{RAW}/src/avg.py"


def test_download_file_takes_github_only():
    with pytest.raises(ToolError, match="Only a file on GitHub"):
        approval.download_request("https://example.com/tool.py", http=site({}), resolve=public)
    away = site({f"{RAW}/x.py": ("redirect", "https://evil.example.com/x.py"),
                 "https://evil.example.com/x.py": PY})
    with pytest.raises(ToolError, match="leads away from GitHub"):
        approval.download_request(f"{RAW}/x.py", http=away, resolve=public)


def test_a_repository_page_names_its_files_instead():
    tree = json.dumps({"tree": [{"path": "data/sales.csv", "type": "blob"}, {"path": "app.py", "type": "blob"}]})
    http = site({"https://github.com/o/r": b"<html>repo</html>",
                 f"{web.GITHUB_API}/o/r/git/trees/HEAD?recursive=1": tree.encode()})
    with pytest.raises(ToolError) as files:
        approval.download_request("https://github.com/o/r", http=http, resolve=public)
    assert "raw.githubusercontent.com/o/r/HEAD/app.py" in files.value.message
    with pytest.raises(ToolError) as tables:
        approval.import_request("https://github.com/o/r", http=http, resolve=public)
    assert "sales.csv" in tables.value.message and "app.py" not in tables.value.message
    assert "Files it has: " in files.value.message  # all of them shown: no count needed


def test_a_long_file_list_says_how_many_there_are():
    """Measured: ten listed files were reported to the user as a repository of ten items."""
    tree = json.dumps({"tree": [{"path": f"src/m{i}.rs", "type": "blob"} for i in range(57)]})
    http = site({"https://github.com/o/r": b"<html>repo</html>",
                 f"{web.GITHUB_API}/o/r/git/trees/HEAD?recursive=1": tree.encode()})
    with pytest.raises(ToolError) as files:
        approval.download_request("https://github.com/o/r", http=http, resolve=public)
    assert f"({web.FILE_LINKS} of 57; a folder's link lists that folder)" in files.value.message


def test_a_file_in_git_lfs_is_described_by_its_real_size():
    r = approval.import_request(f"{RAW}/big.csv", http=site({f"{RAW}/big.csv": LFS}), resolve=public)
    ask = r.artifact["approval"]
    assert ask["url"] == "https://media.githubusercontent.com/media/o/r/main/big.csv" and ask["bytes"] == 31


def test_too_big_or_not_a_table_is_refused_before_asking(monkeypatch):
    monkeypatch.setattr(web, "MAX_FILE_BYTES", 10)
    with pytest.raises(ToolError, match="at most"):
        approval.download_request(f"{RAW}/a.py", http=site({f"{RAW}/a.py": PY}), resolve=public)
    with pytest.raises(ToolError, match="not a CSV"):
        approval.import_request("https://example.com/a.bin", resolve=public, http=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"\x00\x01",
                                                                   headers={"content-type": "application/octet-stream"}))))


def test_nothing_happens_unless_the_user_allows_it(ws, index):
    ask = approval.import_request(f"{RAW}/sales.csv", http=site({f"{RAW}/sales.csv": CSV}), resolve=public).artifact
    for said in ("Cancel", None, "no, use the other file"):
        result, card = approval.carry_out(ws, index, ask["approval"], ask["options"], said)
        assert result["allowed"] is False and result["cancelled"] and card is None and ws.ids() == []
        # what happened, in the past: "did not allow" alone was read as "not allowed yet - please click"
        assert "shown the Import button" in result["note"] and "it is cancelled" in result["note"]
    assert "use the other file" in result["note"]  # what the user typed reaches the model


def test_an_allowed_import_becomes_a_table(ws, index):
    http = site({f"{RAW}/sales.csv": CSV})
    ask = approval.import_request(f"{RAW}/sales.csv", http=http, resolve=public).artifact
    result, card = approval.carry_out(ws, index, ask["approval"], ask["options"], "Import", http=http, resolve=public)
    assert result["allowed"] and result["data"] == "data_1" and result["rows"] == 2 and card["type"] == "table"
    assert ws.table("data_1")["sales"].sum() == 30


def test_an_allowed_download_is_kept_by_what_it_is(ws, index):
    http = site({f"{RAW}/avg.py": PY, f"{RAW}/sales.csv": CSV, f"{RAW}/README.md": b"# Guide\n\nRefunds take 14 days."})
    kept = {}
    for name in ("avg.py", "sales.csv", "README.md"):
        ask = approval.download_request(f"{RAW}/{name}", http=http, resolve=public).artifact
        kept[name], _ = approval.carry_out(ws, index, ask["approval"], ask["options"], "yes", http=http, resolve=public)
    assert kept["avg.py"]["kind"] == "file" and ws.path(kept["avg.py"]["id"]).read_bytes() == PY
    assert kept["sales.csv"]["kind"] == "table"
    assert kept["README.md"]["kind"] == "document" and find_files(ws, index, "refunds").result["files"]


def test_delete_asks_then_removes_items_and_their_passages(ws, index):
    from app.tools.file_ops import files
    note = files(ws, index, "write", name="plan.md", content="# Plan\n\nForecast six months ahead.").result["file"]
    table = ws.add_table(pd.DataFrame({"a": [1]}), "t.csv")
    with pytest.raises(ToolError, match="No file file_9"):
        approval.delete_request(ws, ["file_9"])
    ask = approval.delete_request(ws, [note, table, note]).artifact
    assert ask["options"] == ["Delete", "Cancel"] and ask["approval"]["items"] == [note, table]
    assert "can't be undone" in ask["question"] and ws.ids() == [note, table]  # nothing deleted yet
    result, _ = approval.carry_out(ws, index, ask["approval"], ask["options"], "Delete")
    assert result["deleted"] == [note, table] and ws.ids() == []
    assert not (ws.files / "t.csv").exists() and not (ws.files / "plan.md").exists()
    assert not (ws.zen / "cache" / f"{table}.parquet").exists()
    assert not find_files(ws, index, "forecast").result["files"]


# ---------------------------------------------------------------- the turn pauses for it

def files_call(mode: str, name: str) -> dict:
    """A scripted files() call (call() takes the tool's name as its first argument)."""
    return {**call("files", mode=mode), "input": {"mode": mode, "name": name}}


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "assistant")
    yield s
    s.close()


def test_the_turn_waits_for_permission_and_the_action_runs_by_code(store):
    ws_id = store.create_workspace("Shop")["id"]
    chat = store.create_chat(ws_id)["id"]
    ws, _ = store.open(ws_id)
    keep = ws.add_bytes("file", b"keep", "txt", name="keep.txt")
    drop = ws.add_bytes("file", b"old", "txt", name="old.txt")
    main = ScriptedProvider([ModelTurn(text="", tool_calls=[files_call("delete", "old.txt")], stop="tool_use"),
                             ModelTurn(text="Deleted old.txt.", stop="end"),
                             ModelTurn(text="", tool_calls=[files_call("delete", keep)], stop="tool_use"),
                             ModelTurn(text="Kept it.", stop="end")])
    paused = list(send(store, chat, "delete old.txt", main, ScriptedProvider([])))
    assert paused[-1]["type"] == "question" and paused[-1]["approval"]["items"] == [drop]
    assert drop in store.open(ws_id)[0].ids()  # waiting: nothing deleted
    done = list(reply(store, chat, "Delete", main, ScriptedProvider([])))
    assert done[-1]["answer"] == "Deleted old.txt." and drop not in store.open(ws_id)[0].ids()
    told = json.loads(main.requests[1]["messages"][-1]["content"])
    assert told["allowed"] is True and told["deleted"] == [drop]
    list(send(store, chat, "and keep.txt", main, ScriptedProvider([])))
    list(reply(store, chat, "Cancel", main, ScriptedProvider([])))
    assert keep in store.open(ws_id)[0].ids()  # refused: still there
    assert json.loads(main.requests[3]["messages"][-1]["content"])["allowed"] is False
