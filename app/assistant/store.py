"""Workspaces, their chats and every chat's messages, in one SQLite file.

    workspaces  id, name, created_at, updated_at
    chats       id, workspace_id -> workspaces (ON DELETE CASCADE), title, created_at, updated_at,
                dropped  - where the rolling window cuts this chat (kept so the prompt stays the same)
                pending  - an ask_user question waiting for its answer (JSON), or NULL
                pins     - the files pinned to the chat, by key (JSON list): their snapshot goes with every turn
    messages    id, chat_id -> chats (ON DELETE CASCADE), role, body (the message as JSON), created_at

A workspace's files (agent.md among them), tables, images, sandbox and search index (files, past chats) live
in its own folder, <root>/workspaces/<id>/. Deleting a workspace deletes its rows - its chats and
their messages go with it by cascade - and then the folder, after checking that the folder really
is inside <root>/workspaces. Deleting a chat also takes its turns out of the search index, so
recall can't find them any more.

Messages are stored as they happen (one row each), so a turn that stops half way keeps what it did.
Rows with role 'artifact' are what the UI was shown (charts, tables, sources); they are kept for
redisplay and never sent to a model. A row with role 'trace' ends each turn: how it went (main.Live.trace)
- never sent to a model nor shown in the chat.

A workspace's search index is kept current here: refresh() gives text passages the current embedding
model's vectors (after a change of model, or what was added while the model was unavailable) - in the
background, when the workspace is first opened - and sync_items() indexes its pictures and tables,
after every turn and every upload or run from the file panel.

Design points (cascade deletes, removing the folder after the rows and only inside the data root)
from Unsloth Studio's chat storage (studio/backend/storage/studio_db.py), written independently.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path

from app.tools import agent_md, files
from app.tools.search_index import PICTURE, TABLE, SearchIndex
from app.tools.workspace import Workspace

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chats (
    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    dropped INTEGER NOT NULL DEFAULT 0, pending TEXT);
CREATE INDEX IF NOT EXISTS chats_workspace ON chats (workspace_id, updated_at);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY, chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS messages_chat ON messages (chat_id, id);
"""
NEW_CHAT = "New chat"


def _items_differ(ws: Workspace, index: SearchIndex) -> bool:
    """Cheap check before a refresh (no call to the model): a picture or table added, deleted, changed or
    renamed since it was indexed, or a picture embedded by another model."""
    for kind, source in (("image", PICTURE), ("table", TABLE)):
        have = index.stamps(source)
        items = ws.ids(kind)
        if set(have) != set(items):
            return True
        for item in items:
            try:
                stat = ws.content_path(item).stat()
            except OSError:
                continue
            stamp, model, title = have[item]
            if stamp != files.item_stamp(stat) or title != ws.entry(item)["name"] \
                    or (source == PICTURE and model not in (None, index.embedder.name)):
                return True
    return False


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")  # orders actions within a second


def _id() -> str:
    return uuid.uuid4().hex[:12]


class Store:
    def __init__(self, root: str | Path, embedder=None):
        self.root = Path(root)
        (self.root / "workspaces").mkdir(parents=True, exist_ok=True)
        self.embedder = embedder
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.root / "assistant.sqlite", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.executescript(SCHEMA)
        if "pins" not in {r[1] for r in self._db.execute("PRAGMA table_info(chats)")}:  # a store made before pins
            self._db.execute("ALTER TABLE chats ADD COLUMN pins TEXT")
        self._open: dict[str, tuple[Workspace, SearchIndex]] = {}
        self._refreshing: dict[str, bool] = {}  # workspace -> asked again while running

    def close(self) -> None:
        with self._lock:
            self._open.clear()  # the index opens and closes its file per operation: nothing to close
            self._db.close()

    def _run(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock, self._db:
            return self._db.execute(sql, args).fetchall()

    # ---------------------------------------------------------------- workspaces

    def create_workspace(self, name: str) -> dict:
        now, ws_id = _now(), _id()
        self._run("INSERT INTO workspaces VALUES (?, ?, ?, ?)", (ws_id, name.strip() or "Workspace", now, now))
        self.folder(ws_id).mkdir(parents=True, exist_ok=True)
        return self.workspace(ws_id)

    def workspaces(self) -> list[dict]:
        return [dict(r) for r in self._run(
            "SELECT w.*, (SELECT count(*) FROM chats c WHERE c.workspace_id = w.id) AS chats "
            "FROM workspaces w ORDER BY updated_at DESC, rowid DESC")]

    def workspace(self, ws_id: str) -> dict | None:
        rows = self._run("SELECT * FROM workspaces WHERE id = ?", (ws_id,))
        return dict(rows[0]) if rows else None

    def rename_workspace(self, ws_id: str, name: str) -> None:
        self._run("UPDATE workspaces SET name = ?, updated_at = ? WHERE id = ?", (name.strip(), _now(), ws_id))

    def delete_workspace(self, ws_id: str) -> bool:
        """Its rows (chats and messages by cascade), then its folder: files, tables, index."""
        if self.workspace(ws_id) is None:
            return False
        with self._lock:
            self._open.pop(ws_id, None)
            with self._db:
                self._db.execute("DELETE FROM workspaces WHERE id = ?", (ws_id,))
        folder = self.folder(ws_id).resolve()
        if folder.parent != (self.root / "workspaces").resolve():
            raise RuntimeError(f"Refusing to delete {folder}: not a workspace folder.")
        shutil.rmtree(folder, ignore_errors=True)
        return True

    def folder(self, ws_id: str) -> Path:
        if not ws_id.isalnum():
            raise ValueError("bad workspace id")
        return self.root / "workspaces" / ws_id

    def open(self, ws_id: str) -> tuple[Workspace, SearchIndex]:
        """The workspace's files and its search index (files, past chats); agent.md is made the first time."""
        with self._lock:
            first = ws_id not in self._open
            if first:
                folder = self.folder(ws_id)
                self._open[ws_id] = (Workspace(folder), SearchIndex(folder / "search.sqlite", self.embedder))
                agent_md.ensure(self._open[ws_id][0])
            opened = self._open[ws_id]
        if first and self.embedder is not None:
            self.refresh(ws_id)
        return opened

    def refresh(self, ws_id: str) -> None:
        """In the background: text passages without the current model's vector get one, then the pictures
        and tables are synced. Nothing starts when there is nothing to do; one run at a time per workspace (a call
        during a run makes it run once more)."""
        ws, index = self._open[ws_id]
        if not index.stale() and not _items_differ(ws, index):
            return
        with self._lock:
            if ws_id in self._refreshing:
                self._refreshing[ws_id] = True
                return
            self._refreshing[ws_id] = False
        threading.Thread(target=self._refresh, args=(ws_id, ws, index), daemon=True, name=f"refresh-{ws_id}").start()

    def _refresh(self, ws_id: str, ws: Workspace, index: SearchIndex) -> None:
        try:
            while True:
                n = index.reindex()
                m = files.index_items(ws, index)
                if n or m:
                    log.info("Workspace %s: %d passages and %d items indexed with %s", ws_id, n, m, index.embedder.name)
                with self._lock:
                    if not self._refreshing.get(ws_id):
                        self._refreshing.pop(ws_id, None)
                        return
                    self._refreshing[ws_id] = False
        except Exception:  # noqa: BLE001 - a workspace deleted meanwhile, a file gone: the next refresh retries
            log.exception("Refreshing the search index of workspace %s failed", ws_id)
            with self._lock:
                self._refreshing.pop(ws_id, None)

    def sync_items(self, ws_id: str) -> None:
        """The workspace's pictures and tables indexed now (after a turn, an upload or a run): new ones,
        changed ones, gone ones. Text passages still waiting for a vector are given one in the background."""
        ws, index = self.open(ws_id)
        try:
            files.index_items(ws, index)
        except Exception:  # noqa: BLE001 - a turn or an upload never fails for its index; the next sync retries
            log.exception("Indexing the pictures and tables of workspace %s failed", ws_id)
        if index.stale():
            self.refresh(ws_id)

    def touch_workspace(self, ws_id: str) -> None:
        self._run("UPDATE workspaces SET updated_at = ? WHERE id = ?", (_now(), ws_id))

    # ---------------------------------------------------------------- chats

    def create_chat(self, ws_id: str, title: str = NEW_CHAT) -> dict:
        now, chat_id = _now(), _id()
        self._run("INSERT INTO chats (id, workspace_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                  (chat_id, ws_id, title, now, now))
        self.touch_workspace(ws_id)
        return self.chat(chat_id)

    def chats(self, ws_id: str) -> list[dict]:
        return [self._chat(r) for r in self._run(
            "SELECT * FROM chats WHERE workspace_id = ? ORDER BY updated_at DESC, rowid DESC", (ws_id,))]

    def chat(self, chat_id: str) -> dict | None:
        rows = self._run("SELECT * FROM chats WHERE id = ?", (chat_id,))
        return self._chat(rows[0]) if rows else None

    @staticmethod
    def _chat(row: sqlite3.Row) -> dict:
        chat = dict(row)
        chat["pending"] = json.loads(chat["pending"]) if chat["pending"] else None
        chat["pins"] = json.loads(chat.get("pins") or "[]")
        return chat

    def update_chat(self, chat_id: str, **fields) -> None:
        """title, dropped, pending (a dict, or None to clear it) or pins (a list of keys)."""
        allowed = {"title", "dropped", "pending", "pins"}
        if not fields or set(fields) - allowed:
            raise ValueError(f"only {sorted(allowed)} can be changed")
        values = {k: json.dumps(v) if k in ("pending", "pins") and v is not None else v for k, v in fields.items()}
        sets = ", ".join(f"{k} = ?" for k in values)
        self._run(f"UPDATE chats SET {sets}, updated_at = ? WHERE id = ?", (*values.values(), _now(), chat_id))

    def delete_chat(self, chat_id: str) -> bool:
        chat = self.chat(chat_id)
        if chat is None:
            return False
        self._run("DELETE FROM chats WHERE id = ?", (chat_id,))
        _, index = self.open(chat["workspace_id"])
        index.remove("chat", chat_id)  # its turns, so recall no longer finds them
        return True

    # ---------------------------------------------------------------- messages

    def append(self, chat_id: str, message: dict) -> int:
        body = {k: v for k, v in message.items() if k != "id"}
        with self._lock, self._db:
            cur = self._db.execute("INSERT INTO messages (chat_id, role, body, created_at) VALUES (?, ?, ?, ?)",
                                   (chat_id, message["role"], json.dumps(body, ensure_ascii=False, default=str),
                                    message.get("at") or _now()))
            self._db.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (_now(), chat_id))
            return cur.lastrowid

    def update_message(self, message_id: int, **fields) -> None:
        """Change fields of a stored message (a user message's context note after a cut)."""
        with self._lock, self._db:
            row = self._db.execute("SELECT body FROM messages WHERE id = ?", (message_id,)).fetchone()
            if row is None:
                raise KeyError(message_id)
            body = {**json.loads(row[0]), **fields}
            self._db.execute("UPDATE messages SET body = ? WHERE id = ?",
                             (json.dumps(body, ensure_ascii=False, default=str), message_id))

    def messages(self, chat_id: str, for_model: bool = False) -> list[dict]:
        """The chat in order; for_model leaves out what isn't conversation (artifacts, traces)."""
        rows = self._run("SELECT id, role, body, created_at FROM messages WHERE chat_id = ? ORDER BY id", (chat_id,))
        out = []
        for r in rows:
            if for_model and r["role"] in ("artifact", "trace"):
                continue
            out.append({"id": r["id"], **json.loads(r["body"]), "at": r["created_at"]})
        return out
