# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
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
    POST   /assistant/chats/{chat}/messages   {text, files?}  send (files: ids uploaded with it or pointed at with @) - events
    POST   /assistant/chats/{chat}/answer     {answer|null}   answer the waiting question - events
    POST   /assistant/chats/{chat}/stop                       stop its running turn (the Stop button)
    PUT    /assistant/chats/{chat}/pins       {pins}          the files pinned to the chat (ids, in order)
    GET    /assistant/workspaces/{ws}/items                   files (agent.md first), tables, images
    GET    /assistant/workspaces/{ws}/find?q=                 the file panel's search: the model's own find
    POST   /assistant/workspaces/{ws}/files   (upload)        a table, a document (indexed) or a file
    GET    /assistant/workspaces/{ws}/items/{item}/file       the stored file (images inline)
    GET    /assistant/workspaces/{ws}/items/{item}/rows       a table's first rows
    GET    /assistant/workspaces/{ws}/items/{item}/png | /csv a chart file's picture, or its numbers as a table
    PUT    /assistant/workspaces/{ws}/items/{item}/text  {text}     the user's own edit of a text file
    POST   /assistant/workspaces/{ws}/items/{item}/console           run it in a console: the user types while it runs
    GET    /assistant/workspaces/{ws}/console/{session}?start=  its output, typed lines and end - events
    POST   /assistant/workspaces/{ws}/console/{session}/input {line}  a line the user typed, to its input()
    POST   /assistant/workspaces/{ws}/console/{session}/stop          stop it
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
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.assistant import main as main_slot
from app.assistant.workers import catalogue
from app import model_link
from app.assistant.store import Store
from app.config import settings
from app.tools import agent_md, approval, caption, console, file_ops, filetypes
from app.tools import files as files_tool
from app.tools import web
from app.tools.core import ToolError, jsonable

router = APIRouter(prefix="/assistant", tags=["assistant"])
log = logging.getLogger(__name__)

SAFE = {"Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'",
        "X-Content-Type-Options": "nosniff"}
ID = re.compile(r"^[0-9a-f]{12}$")
ITEM = re.compile(r"^(data|image|file)_\d+(?:_\w+)?$")


class NameIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class TextIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    files: list[Annotated[str, Field(pattern=ITEM.pattern)]] = Field(
        default_factory=list, max_length=20, description="ids of the files uploaded with this message, shown on it")


class PinsIn(BaseModel):
    pins: list[Annotated[str, Field(pattern=ITEM.pattern)]] = Field(max_length=20, description="the pinned files' ids")


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
    health = getattr(main, "health", None)  # llama-server: ready, loading or down; others: ready or down
    if health:
        state, message = health()
    else:
        ok_, message = main.ready()
        state = "ready" if ok_ else "down"
    ready = state == "ready"
    loaded = getattr(main, "loaded_model", None)
    where = getattr(main, "location", None)
    return {"ready": ready, "state": state, "message": message,
            "model": (loaded() if ready and loaded else None) or main.name,
            **({"where": where()} if ready and where else {}),
            "assistant": settings.assistant_name, "user": settings.assistant_user_name}


class ModelSettingsIn(BaseModel):
    use: Literal["kaggle", "local", "cloud"]
    # Each one empty keeps the one saved - so {"use": "cloud"} alone switches to the cloud set up before.
    url: str = Field("", max_length=600)       # that choice's address; for Kaggle also the notebook's Zen code
    password: str = Field("", max_length=300)  # Kaggle's
    key: str = Field("", max_length=500)       # a cloud's
    model: str = Field("", max_length=200)     # a cloud's


# Zen's Model settings (app/model_link.py): read for the Settings window - each choice as saved, never a password or a
# key, which stay on the server - and changed from it, or switched from the model's name in a chat: the choice is tried
# first, then saved and put in use; the app takes it on its next request (app/connection.py).
@router.get("/model-settings")
def model_settings_get() -> dict:
    values, m = model_link.read(Path(settings.model_settings)), model_link
    return {"use": m.in_use(values), "chosen": m.USE in values,  # chosen: set up from Settings at least once
            "kaggle": {"url": values.get(m.KAGGLE_URL, ""), "password_set": bool(values.get(m.KAGGLE_PASSWORD)),
                       "ready": m.ready("kaggle", values)},
            "local": {"url": m.local_url(values, settings.llamacpp_url), "ready": True},
            "cloud": {"url": values.get(m.CLOUD_URL, ""), "model": values.get(m.CLOUD_MODEL, ""),
                      "key_set": bool(values.get(m.CLOUD_KEY)), "ready": m.ready("cloud", values)}}


@router.post("/model-settings")
def model_settings_set(body: ModelSettingsIn, request: Request) -> dict:
    # only this app's own page may change where the model runs (another site open in the browser could otherwise post
    # here and send the chats to its own server)
    origin = request.headers.get("origin")
    if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only Zen's own page can change where its model runs.")
    path, m = Path(settings.model_settings), model_link
    saved = m.read(path)
    url = m.pasted(body.url, "address").rstrip("/")
    if body.use == "kaggle":
        url, password = m.code(body.url) or (url, m.pasted(body.password, "password"))
        changes = {m.KAGGLE_URL: url.rstrip("/") or saved.get(m.KAGGLE_URL, ""),
                   m.KAGGLE_PASSWORD: password or saved.get(m.KAGGLE_PASSWORD, "")}
        missing = ("" if changes[m.KAGGLE_URL] else "the notebook's address") or                   ("" if changes[m.KAGGLE_PASSWORD] else "its password")
        hint = "paste the Zen code from the notebook's last cell"
    elif body.use == "local":
        changes, missing, hint = {m.LOCAL_URL: url or m.local_url(saved, settings.llamacpp_url)}, "", ""
    else:
        changes = {m.CLOUD_URL: url or saved.get(m.CLOUD_URL, ""), m.CLOUD_KEY: body.key.strip() or saved.get(m.CLOUD_KEY, ""),
                   m.CLOUD_MODEL: body.model.strip() or saved.get(m.CLOUD_MODEL, "")}
        missing = ("" if changes[m.CLOUD_URL] else "the API's address") or                   ("" if changes[m.CLOUD_MODEL] else "the model's name")
        hint = "give it in Settings"
    if missing:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Zen needs {missing} first: {hint}.")
    address = next(v for k, v in changes.items() if k.endswith("_URL"))
    if not address.startswith(("https://", "http://")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The address starts with https:// (or http://) - paste it whole.")
    if any(c.isspace() for v in changes.values() for c in v):  # one line each: nothing else gets into the settings file
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Paste each one on its own - an address, a password or a key.")
    ok_, said = m.check(body.use, {**saved, **changes})
    if not ok_:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, said)
    m.write(path, {**changes, m.USE: body.use})
    return {"ok": True, "use": body.use, "model": said}


class SearchSetupIn(BaseModel):
    action: Literal["start", "stop"]


# Search by meaning on this computer (app/search_model.py): what is set up, and setting it up - files already here used,
# the rest downloaded in the background while the page shows how far it is.
@router.get("/search-model")
def search_model_get(request: Request) -> dict:
    return request.app.state.search_setup.state()


@router.post("/search-model")
def search_model_set(body: SearchSetupIn, request: Request) -> dict:
    origin = request.headers.get("origin")
    if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only Zen's own page can set up its search model.")
    setup = request.app.state.search_setup
    setup.start() if body.action == "start" else setup.stop()
    return setup.state()


class WindowThemeIn(BaseModel):
    theme: Literal["dark", "light"]


# The theme picked in the page, kept for Zen-harness.exe: Edge's app window draws its own title bar, dark or light only
# as it opens, so the launcher reads this file to open it matching the page.
@router.put("/window-theme", status_code=status.HTTP_204_NO_CONTENT)
def window_theme(body: WindowThemeIn) -> None:
    path = Path(settings.assistant_dir) / "window-theme"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body.theme, encoding="utf-8")


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
    stored = _store(request).messages(chat_id)
    for i, m in enumerate(stored):
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
            # sent_back: the app's hidden check answered this reply - the page shows it as a first draft (app.js)
            checked = i + 1 < len(stored) and stored[i + 1].get("hidden") and stored[i + 1].get("check")
            out.append({"role": "assistant", "text": m.get("text", ""), "at": m["at"],
                        "tools": [{"id": c["id"], "name": c["name"], "input": c["input"]} for c in m.get("tool_calls", [])],
                        **({"sent_back": True} if checked else {})})
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


@router.put("/chats/{chat_id}/pins")
def set_pins(chat_id: str, body: PinsIn, request: Request) -> dict:
    """The files pinned to the chat (from the file panel, the @ menu or the chat header): their snapshot goes with
    every turn of it until they are unpinned."""
    chat = _chat(request, chat_id)
    ws, _ = _store(request).open(chat["workspace_id"])
    missing = [i for i in body.pins if i not in ws.ids()]
    if missing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such file: {', '.join(missing)}.")
    pins = list(dict.fromkeys(body.pins))
    _store(request).update_chat(chat_id, pins=pins)
    return {"pins": [{"id": i, "name": ws.label(i)} for i in pins]}


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
    keep = ("pages", "words", "passages", "title", "width", "height", "caption")
    memory = agent_md.key(ws)
    out = []
    for i in sorted(ws.ids(), key=lambda k: k != memory):  # agent.md first: the project's memory heads the panel
        e = ws.entry(i)
        meta = {k: v for k, v in e.get("meta", {}).items() if k in keep and not isinstance(v, (dict, list))}
        size = {"rows": e["rows"], "columns": len(e.get("columns", []))} if "rows" in e else {"bytes": e.get("bytes")}
        report = {"report": e["report"]["line"]} if e.get("report") else {}  # what a worker did with it
        out.append({"id": i, "kind": e["kind"], "name": e.get("name"), "parent": e.get("parent"), "by": e.get("by"),
                    "bytes": e.get("bytes"), **size, **meta, **report, "versions": len(ws.versions(i)),
                    **({"memory": True} if i == memory else {})})
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
        try:  # a document's meaning-search vectors come after the answer, in the background (refresh, below):
            # measured 2026-10-17, a 1,923-page PDF kept the upload - and the message waiting on it - for minutes
            kept = files_tool.store_file(ws, index, path, by="user", embed=False)
        except ToolError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.message) from exc
    if kept.get("same"):  # the same bytes are here already: that file, not a copy
        kept["note"] = f"Already in this workspace as {kept['name']}."
    elif kept["kind"] == "image":  # a pasted image.png is named from what it shows (caption.py)
        worker = request.app.state.assistant_worker
        sees = getattr(worker, "sees_images", lambda: False)()
        named = caption.name_from_caption(ws, kept["id"], getattr(worker, "look", None) if sees else None)
        if named:
            kept.update(named)
    _store(request).sync_items(ws_id)  # searchable by what it shows, holds or says
    _store(request).refresh(ws_id)  # a document's vectors, in the background
    return kept


def _item(request: Request, ws_id: str, item_id: str):
    _workspace(request, ws_id)
    ws, _ = _store(request).open(ws_id)
    if not ITEM.match(item_id) or item_id not in ws.ids():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such item.")
    return ws


@router.delete("/workspaces/{ws_id}/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_item(ws_id: str, item_id: str, request: Request) -> Response:
    """The user deletes a file from the file panel (the page asks first) - before, only asking Zen could. agent.md
    stays: it is the project's memory (its text is cleared instead)."""
    ws = _item(request, ws_id, item_id)
    if agent_md.is_agent(ws.entry(item_id)["name"]):
        raise HTTPException(status.HTTP_409_CONFLICT, "agent.md is Zen's memory for this project: clear its text instead.")
    approval.delete(ws, _store(request).open(ws_id)[1], [item_id])
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/workspaces/{ws_id}/working")
def clear_working(ws_id: str, request: Request) -> dict:
    """Zen's working files - work/: the helpers' scripts and in-between tables - deleted at once (the page asks first);
    the user's own files and what the answers made stay."""
    _workspace(request, ws_id)
    ws, index = _store(request).open(ws_id)
    working = [k for k in ws.ids() if ws.entry(k)["name"].startswith("work/")]
    return {"deleted": len(approval.delete(ws, index, working).result["deleted"])}


@router.get("/workspaces/{ws_id}/items/{item_id}/file")
def item_file(ws_id: str, item_id: str, request: Request) -> FileResponse:
    """The file itself (a table: its CSV or Excel file)."""
    ws = _item(request, ws_id, item_id)
    entry = ws.entry(item_id)
    path = ws.path(item_id)
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This file is missing.")
    mime = filetypes.IMAGE_MIME.get(path.suffix.lower())
    if mime:
        return FileResponse(path, media_type=mime, headers=SAFE)
    return FileResponse(path, filename=Path(entry.get("name") or path.name).name,
                        media_type="application/octet-stream", headers=SAFE)


@router.get("/workspaces/{ws_id}/items/{item_id}/preview")
def page_preview(ws_id: str, item_id: str, request: Request) -> FileResponse:
    """A page Zen wrote, as it opened when it was tried (browser.try_page): its card in the chat shows it. None for a
    page changed since (its next try takes a new one)."""
    from app.tools.file_ops import preview_path
    ws = _item(request, ws_id, item_id)
    shot, source = preview_path(ws, item_id), ws.path(item_id)
    if not shot.is_file() or shot.stat().st_mtime < source.stat().st_mtime:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No picture of this page.")
    return FileResponse(shot, media_type="image/png", headers={**SAFE, "Cache-Control": "no-cache"})


@router.get("/workspaces/{ws_id}/items/{item_id}/png")
def chart_png(ws_id: str, item_id: str, request: Request) -> FileResponse:
    """A chart file drawn as a picture (to download; the page draws the chart itself)."""
    from app.tools.chart_image import is_chart, picture_of
    ws = _item(request, ws_id, item_id)
    if not is_chart(ws.label(item_id)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not a chart.")
    png = picture_of(ws, item_id)
    if png is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "This chart can't be drawn as a picture.")
    name = Path(ws.label(item_id)).name.removesuffix(".chart.json") + ".png"
    return FileResponse(png, media_type="image/png", filename=name, headers=SAFE)


@router.get("/workspaces/{ws_id}/find")
def find_items(ws_id: str, q: str, request: Request) -> dict:
    """The file panel's search: files(mode='find') itself - by name, by what is in them and by meaning - each file
    with the part that matched (and its id, for the page to open it)."""
    _workspace(request, ws_id)
    ws, index = _store(request).open(ws_id)
    try:
        found = files_tool.find_files(ws, index, q).result
    except ToolError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.message) from exc
    return jsonable({"found": found["found"], "more": found.get("more"),
                     "files": [{**f, "id": ws.find_path(f["path"])} for f in found["files"]]})


@router.get("/workspaces/{ws_id}/items/{item_id}/csv")
def chart_csv(ws_id: str, item_id: str, request: Request) -> Response:
    """A chart file's numbers as a table, to download (chart_image.table_of: one row a point)."""
    import csv
    import io
    from urllib.parse import quote

    from app.tools.chart_image import is_chart, spec_of, table_of
    ws = _item(request, ws_id, item_id)
    if not is_chart(ws.label(item_id)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not a chart.")
    columns, rows = table_of(spec_of(ws, item_id))
    out = io.StringIO()
    csv.writer(out).writerows([columns, *rows])
    name = Path(ws.label(item_id)).name.removesuffix(".chart.json") + ".csv"
    return Response("\ufeff" + out.getvalue(), media_type="text/csv; charset=utf-8",  # the mark: Excel reads it as UTF-8
                    headers={**SAFE, "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


@router.get("/workspaces/{ws_id}/items/{item_id}/versions")
def item_versions(ws_id: str, item_id: str, request: Request) -> list[dict]:
    """The file's earlier versions, newest first (kept before each change, the last 10)."""
    return _item(request, ws_id, item_id).versions(item_id)


class RestoreBody(BaseModel):
    version: str = Field(max_length=80, description="a version from /versions")


@router.post("/workspaces/{ws_id}/items/{item_id}/restore")
def restore_version(ws_id: str, item_id: str, body: RestoreBody, request: Request) -> dict:
    """An earlier version made the current one; the current one is kept as a version (so a restore can be undone)."""
    ws = _item(request, ws_id, item_id)
    try:
        put = ws.restore(item_id, body.version)
    except ToolError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, exc.message) from exc
    _store(request).sync_items(ws_id)  # its search passages follow its content
    return {"id": put.key, "name": put.path, "versions": len(ws.versions(item_id))}


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


class LineBody(BaseModel):
    line: str = Field(max_length=console.MAX_LINE)


@router.put("/workspaces/{ws_id}/items/{item_id}/text")
def save_text(ws_id: str, item_id: str, body: TextBody, request: Request) -> dict:
    """The user's own edit of a text file, from the file panel: saved as it is - the user is the one changing
    it. A report a helper kept on the file describes the version before, and now says so."""
    ws = _item(request, ws_id, item_id)
    _, index = _store(request).open(ws_id)
    e = ws.entry(item_id)
    if e["kind"] != "file" or not filetypes.editable_as_text(e["name"]):  # the same rule as files write and edit
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


@router.post("/workspaces/{ws_id}/items/{item_id}/console")
def console_start(ws_id: str, item_id: str, request: Request) -> dict:
    """Run a Python file in a console (2.12): its output streams, the user types its input; the session is kept in
    the file's report when it ends."""
    ws = _item(request, ws_id, item_id)
    _, index = _store(request).open(ws_id)
    try:
        session = console.start(ws, index, item_id)
    except ToolError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.message) from exc
    return {"session": session.id, "name": session.name}


def _session(request: Request, ws_id: str, session_id: str) -> console.Session:
    _workspace(request, ws_id)
    ws, _ = _store(request).open(ws_id)
    try:
        return console.get(session_id, ws)
    except ToolError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, exc.message) from exc


@router.get("/workspaces/{ws_id}/console/{session_id}")
def console_follow(ws_id: str, session_id: str, request: Request, start: int = 0) -> StreamingResponse:
    """The session's events from `start` (a page that reconnects gets the rest): out, err, in, end."""
    session = _session(request, ws_id, session_id)

    def body():
        for event in session.follow(max(0, start)):
            yield _sse(event) if event else ": still running\n\n"  # a comment line keeps the connection open
        store = _store(request)
        store.sync_items(ws_id)  # files it wrote: in the panel and searchable

    return StreamingResponse(body(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/workspaces/{ws_id}/console/{session_id}/input")
def console_input(ws_id: str, session_id: str, body: LineBody, request: Request) -> dict:
    try:
        _session(request, ws_id, session_id).type(body.line)
    except ToolError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.message) from exc
    return {"ok": True}


@router.post("/workspaces/{ws_id}/console/{session_id}/stop")
def console_stop(ws_id: str, session_id: str, request: Request) -> dict:
    _session(request, ws_id, session_id).stop()
    return {"ok": True}


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
