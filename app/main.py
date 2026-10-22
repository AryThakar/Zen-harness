# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""The assistant's web app: its API (app/api/assistant.py) and its chat page (web/index.html, with its
styles, scripts, fonts and sound served from web/ under /static).

    uvicorn app.main:app --reload
    open http://localhost:8000        (chat)
    open http://localhost:8000/docs   (API)

The model is where Zen's Model settings say - the Kaggle notebook, a llama-server on this computer, or a cloud's
OpenAI-style API (app/model_link.py, app/connection.py); on llama-server, slot 0 is main and slot 1 the helpers.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.assistant import main as main_slot
from app.assistant.store import Store
from app.config import settings
from app.connection import Connection, Live, LiveEmbedder
from app.providers import Provider

from .api import assistant

WEB = Path(__file__).resolve().parent.parent / "web"
WEB_PAGE = WEB / "index.html"


class SameSiteOnly:
    """A change (any request but GET or HEAD) is taken only from this app's own page: another site open in the
    same browser can't upload, send or delete here (a file upload is a 'simple' request - the browser sends it
    to any site without asking first), nor can a sandboxed page (Origin 'null'). Plain ASGI, so the streamed
    answers pass through untouched."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] not in ("GET", "HEAD"):
            headers = dict(scope["headers"])
            origin = headers.get(b"origin", b"").decode("latin-1")
            if origin and urlsplit(origin).netloc != headers.get(b"host", b"").decode("latin-1"):
                await JSONResponse({"detail": "Requests from another site are refused."}, status_code=403)(
                    scope, receive, send)
                return
        await self.app(scope, receive, send)


class PageFiles(StaticFiles):
    """The page's styles, scripts, fonts and sound: checked again on every load (a 304 when unchanged), so
    an updated script is never run from the browser's cache alongside a newer page."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(assistant_parts: tuple[Store, Provider, Provider] | None = None) -> FastAPI:
    """Build the app. Tests pass their own store and the models of the two slots (main, helpers)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        own_store = assistant_parts is None
        if own_store:  # the model the Model settings say, made again whenever they change (app/connection.py)
            connection = Connection(settings.model_settings, settings, cancel=main_slot.STOP)
            store = Store(settings.assistant_dir, LiveEmbedder(connection))
            main, worker = Live(connection, 0), Live(connection, 1)
        else:
            store, main, worker = assistant_parts
        app.state.assistant_store = store
        app.state.assistant_main, app.state.assistant_worker = main, worker
        app.state.assistant_upload_limit = settings.max_upload_mb * 1024 * 1024
        yield
        if own_store:
            store.close()

    app = FastAPI(title=f"{settings.assistant_name} - a local assistant",
                  description="Chat, workspaces, helpers (web, knowledge, data, code) and their tools.",
                  version="0.6.0", lifespan=lifespan)
    app.add_middleware(SameSiteOnly)
    app.include_router(assistant.router)
    app.mount("/static", PageFiles(directory=WEB), name="static")

    @app.get("/", include_in_schema=False)
    def chat_page() -> FileResponse:
        """The single-page chat UI (web/index.html)."""
        return FileResponse(WEB_PAGE, media_type="text/html", headers={"Cache-Control": "no-cache"})

    @app.get("/health", tags=["system"])
    def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
