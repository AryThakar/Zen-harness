"""Workspaces, their chats and every chat's messages, in one SQLite file.

    workspaces  id, name, created_at, updated_at
    chats       id, workspace_id -> workspaces (ON DELETE CASCADE), title, created_at, updated_at,
                dropped  - where the rolling window cuts this chat (kept so the prompt stays the same)
                pending  - an ask_user question waiting for its answer (JSON), or NULL
    messages    id, chat_id -> chats (ON DELETE CASCADE), role, body (the message as JSON), created_at

A workspace's files, tables, models, sandbox and search index (documents, notes, past chats) live
in its own folder, <root>/workspaces/<id>/. Deleting a workspace deletes its rows - its chats and
their messages go with it by cascade - and then the folder, after checking that the folder really
is inside <root>/workspaces. Deleting a chat also takes its turns out of the search index, so
recall can't find them any more.

Messages are stored as they happen (one row each), so a turn that stops half way keeps what it did.
Rows with role 'artifact' are what the UI was shown (charts, tables, sources); they are kept for
redisplay and never sent to a model.

Design points (cascade deletes, removing the folder after the rows and only inside the data root)
from Unsloth Studio's chat storage (studio/backend/storage/studio_db.py), written independently.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path

from app.tools.search_index import SearchIndex
from app.tools.workspace import Workspace

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
        self._open: dict[str, tuple[Workspace, SearchIndex]] = {}

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
        """The workspace's files and its search index (documents, notes, past chats)."""
        with self._lock:
            if ws_id not in self._open:
                folder = self.folder(ws_id)
                self._open[ws_id] = (Workspace(folder), SearchIndex(folder / "search.sqlite", self.embedder))
            return self._open[ws_id]

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
        return chat

    def update_chat(self, chat_id: str, **fields) -> None:
        """title, dropped or pending (a dict, or None to clear it)."""
        allowed = {"title", "dropped", "pending"}
        if not fields or set(fields) - allowed:
            raise ValueError(f"only {sorted(allowed)} can be changed")
        values = {k: json.dumps(v) if k == "pending" and v is not None else v for k, v in fields.items()}
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
        """The chat in order; for_model leaves out what only the UI needs (artifacts)."""
        rows = self._run("SELECT id, role, body, created_at FROM messages WHERE chat_id = ? ORDER BY id", (chat_id,))
        out = []
        for r in rows:
            if for_model and r["role"] == "artifact":
                continue
            out.append({"id": r["id"], **json.loads(r["body"]), "at": r["created_at"]})
        return out
