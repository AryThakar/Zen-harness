"""The assistant's API: workspaces, chats, the conversation itself, uploads and what the UI shows.

    GET    /assistant/status                                  is the model server up; the assistant's and the user's names
    GET    /assistant/tools                                   how the UI names each tool and helper while it works
    GET    /assistant/workspaces                              list (newest first)
    POST   /assistant/workspaces              {name}          create
    PATCH  /assistant/workspaces/{ws}         {name}          rename
    DELETE /assistant/workspaces/{ws}                         delete it and everything in it
    GET    /assistant/workspaces/{ws}/chats                   list
    POST   /assistant/workspaces/{ws}/chats                   new chat
    DELETE /assistant/chats/{chat}                            delete (and forget it in recall)
    GET    /assistant/chats/{chat}/messages                   the chat, for redisplay (rows of a running turn marked live;
                                                              each turn's trace after it)
    GET    /assistant/chats/{chat}/traces                     how each turn went: steps, times, tokens, checks, errors
    GET    /assistant/chats/{chat}/stream                     the running turn's events from its start - events
    POST   /assistant/chats/{chat}/messages   {text, files?}  send (files: ids uploaded with it, shown on it) - events
    POST   /assistant/chats/{chat}/answer     {answer|null}   answer the waiting question - events
    POST   /assistant/chats/{chat}/stop                       stop its running turn (the Stop button)
    GET    /assistant/workspaces/{ws}/items                   files, tables, images, the 5 pinned notes
    POST   /assistant/workspaces/{ws}/files   (upload)        a table, a document (indexed) or a file
    GET    /assistant/workspaces/{ws}/items/{item}/file       the stored file (images inline)
    GET    /assistant/workspaces/{ws}/items/{item}/rows       a table's first rows
    PUT    /assistant/workspaces/{ws}/items/{item}/text  {text}     the user's own edit of a text file
    POST   /assistant/workspaces/{ws}/items/{item}/run   {input?}   run a Python file (the sandbox's engine)
    GET    /assistant/web/icon?page=&favicon=   /web/image?url=   favicons and preview images,
                                                fetched by the server with the public-address checks

Stored files are served with a sandboxing Content-Security-Policy and, unless they are images, as
downloads: an uploaded .html or a site's SVG icon must never run as a page of this app.
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.assistant import main as main_slot
from app.assistant.workers import catalogue
from app.assistant.store import Store
from app.config import settings
from app.tools import file_ops, filetypes, sandbox
from app.tools import files as files_tool
from app.tools import notes as notes_tool
from app.tools import web
from app.tools.core import ToolError, jsonable

router = APIRouter(prefix="/assistant", tags=["assistant"])
log = logging.getLogger(__name__)

SAFE = {"Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'",
        "X-Content-Type-Options": "nosniff"}
ID = re.compile(r"^[0-9a-f]{12}$")
ITEM = re.compile(r"^(data|image|file|note)_\d+(?:_\w+)?$")


class NameIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class TextIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    files: list[Annotated[str, Field(pattern=ITEM.pattern)]] = Field(
        default_factory=list, max_length=20, description="ids of the files uploaded with this message, shown on it")


class AnswerIn(BaseModel):
    answer: str | None = Field(None, max_length=2000, description="null when the user skips the question")


def _store(request: Request) -> Store:
    return request.app.state.assistant_store


def _workspace(request: Request, ws_id: str) -> dict:
    ws = _store(request).workspace(ws_id) if ID.match(ws_id) else None
    if ws is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such workspace.")
    return ws


def _chat(request: Request, chat_id: str) -> dict:
    chat = _store(request).chat(chat_id) if ID.match(chat_id) else None
    if chat is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such chat.")
    return chat


# ---------------------------------------------------------------- status, workspaces, chats

@router.get("/status")
def assistant_status(request: Request) -> dict:
    main = request.app.state.assistant_main
    ready, message = main.ready()
    loaded = getattr(main, "loaded_model", None)
    return {"ready": ready, "message": message, "model": (loaded() if ready and loaded else None) or main.name,
            "assistant": settings.assistant_name, "user": settings.assistant_user_name}


@router.get("/tools")
def tool_labels() -> dict:
    """Every tool's labels (doing, done) and each helper's name: defined with the tools, so the UI has no list
    of its own to fall out of step."""
    return catalogue()


@router.get("/workspaces")
def list_workspaces(request: Request) -> list[dict]:
    return _store(request).workspaces()


@router.post("/workspaces", status_code=status.HTTP_201_CREATED)
def create_workspace(body: NameIn, request: Request) -> dict:
    return _store(request).create_workspace(body.name)


@router.patch("/workspaces/{ws_id}")
def rename_workspace(ws_id: str, body: NameIn, request: Request) -> dict:
    _workspace(request, ws_id)
    _store(request).rename_workspace(ws_id, body.name)
    return _store(request).workspace(ws_id)


@router.delete("/workspaces/{ws_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workspace(ws_id: str, request: Request) -> Response:
    _workspace(request, ws_id)
    main_slot.end_turn({c["id"] for c in _store(request).chats(ws_id)})
    _store(request).delete_workspace(ws_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/workspaces/{ws_id}/chats")
def list_chats(ws_id: str, request: Request) -> list[dict]:
    _workspace(request, ws_id)
    return [{**c, "running": main_slot.live(c["id"]) is not None} for c in _store(request).chats(ws_id)]


@router.post("/workspaces/{ws_id}/chats", status_code=status.HTTP_201_CREATED)
def create_chat(ws_id: str, request: Request) -> dict:
    _workspace(request, ws_id)
    return _store(request).create_chat(ws_id)


@router.delete("/chats/{chat_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_chat(chat_id: str, request: Request) -> Response:
    _chat(request, chat_id)
    main_slot.end_turn({chat_id})
    _store(request).delete_chat(chat_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/chats/{chat_id}/messages")
def chat_messages(chat_id: str, request: Request) -> dict:
    """The chat as the UI shows it: what the user wrote (without the time stamp the model sees),
    the answers, the tools used and what they showed, and how each turn ran (its trace, for the developer's
    view under the answer) - no model internals, no hidden notes."""
    chat = _chat(request, chat_id)
    turn = main_slot.live(chat_id)
    out = []
    for m in _store(request).messages(chat_id):
        if m.get("hidden"):
            continue
        attached = {"files": m["files"]} if m["role"] == "user" and m.get("files") else {}
        if turn is not None and m["id"] > turn.after_id:  # the page follows these through /stream
            out.append({"role": m["role"], "live": True, **({"text": m.get("text", "")} if m["role"] == "user" else {}),
                        **attached, **(_answered(m["name"], m["content"]) if m["role"] == "tool" else {})})
            continue
        if m["role"] == "user":
            out.append({"role": "user", "text": m.get("text", m.get("content", "")), "at": m["at"], **attached})
        elif m["role"] == "assistant":
            out.append({"role": "assistant", "text": m.get("text", ""), "at": m["at"],
                        "tools": [{"id": c["id"], "name": c["name"], "input": c["input"]} for c in m.get("tool_calls", [])]})
        elif m["role"] == "tool":
            out.append({"role": "tool", "id": m["tool_call_id"], "name": m["name"], "is_error": m.get("is_error", False),
                        "summary": m["content"][:main_slot.SUMMARY_CHARS], **_answered(m["name"], m["content"])})
        elif m["role"] == "artifact":
            out.append({"role": "artifact", "artifact": m["artifact"], "at": m["at"]})
        elif m["role"] == "trace":
            out.append({"role": "trace", "trace": m["trace"], "at": m["at"]})
    return {"chat": chat, "messages": out, "running": turn is not None}


@router.get("/chats/{chat_id}/traces")
def chat_traces(chat_id: str, request: Request) -> list[dict]:
    """Each finished turn's trace (main.Live.trace), oldest first: for a developer's view and the evaluation."""
    _chat(request, chat_id)
    return [{"at": m["at"], **m["trace"]} for m in _store(request).messages(chat_id) if m["role"] == "trace"]


def _answered(name: str, content: str) -> dict:
    """The user's answer when this tool result carries one (ask_user, or a helper's question)."""
    try:
        result = json.loads(content)
    except ValueError:
        return {}
    if not isinstance(result, dict) or not (name == "ask_user" or "helper_asked" in result):
        return {}
    if result.get("skipped") or result.get("user_skipped"):
        return {"answer": None, "skipped": True}
    said = result.get("user_answered", result.get("answer"))
    return {"answer": said} if isinstance(said, str) else {}


@router.post("/chats/{chat_id}/messages")
def send_message(chat_id: str, body: TextIn, request: Request) -> StreamingResponse:
    _chat(request, chat_id)
    app = request.app
    return _stream(main_slot.send(_store(request), chat_id, body.text, app.state.assistant_main,
                                  app.state.assistant_worker, body.files))


@router.post("/chats/{chat_id}/answer")
def answer_question(chat_id: str, body: AnswerIn, request: Request) -> StreamingResponse:
    _chat(request, chat_id)
    app = request.app
    return _stream(main_slot.reply(_store(request), chat_id, body.answer, app.state.assistant_main,
                                   app.state.assistant_worker))


@router.get("/chats/{chat_id}/stream")
def follow_turn(chat_id: str, request: Request) -> StreamingResponse:
    _chat(request, chat_id)
    return _stream(main_slot.follow(chat_id))


@router.post("/chats/{chat_id}/stop")
def stop_turn(chat_id: str, request: Request) -> dict:
    _chat(request, chat_id)
    return {"stopped": main_slot.stop(chat_id)}


def _stream(events) -> StreamingResponse:
    def body():
        try:
            for event in events:
                yield _sse(event)
        except Exception:  # a bug, not the user's fault; details go to the log
            log.exception("Assistant turn failed")
            yield _sse({"type": "error", "message": "Something went wrong while answering. Please try again."})

    return StreamingResponse(body(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _sse(event: dict) -> str:
    data = {k: v for k, v in event.items() if k != "type"}
    return f"event: {event['type']}\ndata: {json.dumps(jsonable(data), ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------- items and uploads

@router.get("/workspaces/{ws_id}/items")
def list_items(ws_id: str, request: Request) -> list[dict]:
    _workspace(request, ws_id)
    ws, _ = _store(request).open(ws_id)
    keep = ("pages", "words", "passages", "title", "width", "height")
    shown = {n["id"]: n["saved"] for n in notes_tool.pinned(ws)}  # notes: only the 5 the AI keeps in view
    out = []
    for i in ws.ids():
        e = ws.entry(i)
        if e["kind"] == "note":
            if i not in shown:
                continue
            e = {**e, "meta": {**e.get("meta", {}), "pinned": True, "saved": shown[i]}}
            meta_keys = keep + ("pinned", "saved")
        else:
            meta_keys = keep
        meta = {k: v for k, v in e.get("meta", {}).items() if k in meta_keys and not isinstance(v, (dict, list))}
        size = {"rows": e["rows"], "columns": len(e.get("columns", []))} if "rows" in e else {"bytes": e.get("bytes")}
        report = {"report": e["report"]["line"]} if e.get("report") else {}  # what a worker did with it
        out.append({"id": i, "kind": e["kind"], "name": e.get("name"), "parent": e.get("parent"), **size, **meta,
                    **report})
    return out


@router.post("/workspaces/{ws_id}/files", status_code=status.HTTP_201_CREATED)
def upload(ws_id: str, file: UploadFile, request: Request) -> dict:
    """A table (CSV, Excel) for the data tools, a document (PDF, Word, PowerPoint, text, Markdown) indexed for
    search, or any other file a script can read."""
    _workspace(request, ws_id)
    name = Path(file.filename or "upload").name.strip() or "upload"
    limit = request.app.state.assistant_upload_limit
    ws, index = _store(request).open(ws_id)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / name
        with path.open("wb") as out:
            copied = 0
            while chunk := file.file.read(1 << 20):
                copied += len(chunk)
                if copied > limit:
                    raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE,
                                        f"The file is larger than {limit // (1 << 20)} MB.")
                out.write(chunk)
        try:
            kept = files_tool.store_file(ws, index, path)
        except ToolError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.message) from exc
    if kept.get("kind") in ("image", "table"):
        _store(request).sync_items(ws_id)  # searchable by what it shows or holds
    return kept


def _item(request: Request, ws_id: str, item_id: str):
    _workspace(request, ws_id)
    ws, _ = _store(request).open(ws_id)
    if not ITEM.match(item_id) or item_id not in ws.ids():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such item.")
    return ws


@router.get("/workspaces/{ws_id}/items/{item_id}/file")
def item_file(ws_id: str, item_id: str, request: Request) -> FileResponse:
    ws = _item(request, ws_id, item_id)
    entry = ws.entry(item_id)
    if "file" not in entry:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This item has no file (a table: use /rows).")
    path = ws.path(item_id)
    mime = filetypes.IMAGE_MIME.get(path.suffix.lower())
    if mime:
        return FileResponse(path, media_type=mime, headers=SAFE)
    return FileResponse(path, filename=entry.get("name") or path.name, media_type="application/octet-stream",
                        headers=SAFE)


@router.get("/workspaces/{ws_id}/items/{item_id}/rows")
def item_rows(ws_id: str, item_id: str, request: Request, limit: int = 50) -> dict:
    ws = _item(request, ws_id, item_id)
    if ws.kind(item_id) != "table":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not a table.")
    df = ws.table(item_id)
    head = df.head(max(1, min(limit, 500)))
    return jsonable({"columns": list(map(str, df.columns)), "rows": head.astype(object).where(head.notna(), None)
                     .values.tolist(), "total_rows": len(df)})


class TextBody(BaseModel):
    text: str = Field(max_length=file_ops.MAX_WRITE)


class RunBody(BaseModel):
    input: str | None = Field(None, max_length=10_000, description="what a person would type, one line per input()")
    timeout: int = Field(30, ge=1, le=120)


@router.put("/workspaces/{ws_id}/items/{item_id}/text")
def save_text(ws_id: str, item_id: str, body: TextBody, request: Request) -> dict:
    """The user's own edit of a text file, from the file panel: saved as it is - the user is the one changing
    it. A report a helper kept on the file describes the version before, and now says so."""
    ws = _item(request, ws_id, item_id)
    _, index = _store(request).open(ws_id)
    e = ws.entry(item_id)
    if e["kind"] != "file" or not filetypes.editable_as_text(e["name"]):  # the same rule as files(update)
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Only text files can be edited here.")
    try:
        done = file_ops.replace_file(ws, index, item_id, body.text)
    except ToolError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.message) from exc
    report = ws.report(item_id)
    if report and "edited by the user" not in report.splitlines()[-1]:
        ws.set_report(item_id, report.rstrip() + f"\n- Then edited by the user ({datetime.now():%Y-%m-%d %H:%M}): "
                                                  "this report describes the version before.\n")
        files_tool.index_report(ws, index, item_id)
    return {"file": item_id, "bytes": done.result["bytes"]}


@router.post("/workspaces/{ws_id}/items/{item_id}/run")
def run_file(ws_id: str, item_id: str, body: RunBody, request: Request) -> dict:
    """Run a stored Python file as the user's own run, from the file panel: the same engine, limits and rules as
    a helper's (sandbox.ENGINE). Files it writes become items, made from this file; run again, it updates them."""
    ws = _item(request, ws_id, item_id)
    e = ws.entry(item_id)
    if e["kind"] != "file" or not e["name"].lower().endswith(".py"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Only Python files run here.")
    code = ws.path(item_id).read_text("utf-8", errors="replace")
    earlier = e.get("meta", {}).get("run_outputs") or {}
    result = sandbox.ENGINE.run(ws, code, body.timeout, reuse=earlier, stdin=body.input)
    made = {f["path"]: f["id"] for f in result.result.get("files", []) if f.get("id")}
    for item in made.values():
        ws.set_parent(item, item_id)
    if made:
        ws.set_meta(item_id, "run_outputs", {**earlier, **made})
        _store(request).sync_items(ws_id)  # pictures it drew, tables it wrote: searchable now
    return jsonable({"status": result.status, **result.result})


# ---------------------------------------------------------------- web cards

@router.get("/web/icon")
def web_icon(page: str, favicon: str | None = None) -> Response:
    found = web.fetch_icon(page, favicon)
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No icon.")
    data, mime = found
    return Response(data, media_type=mime, headers={**SAFE, "Cache-Control": "max-age=86400"})


@router.get("/web/image")
def web_image(url: str) -> Response:
    found = web.fetch_image(url)
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No image.")
    data, mime = found
    return Response(data, media_type=mime, headers={**SAFE, "Cache-Control": "max-age=86400"})
