"""The assistant's web app: its API (app/api/assistant.py) and its chat page (web/index.html).

    uvicorn app.main:app --reload
    open http://localhost:8000        (chat)
    open http://localhost:8000/docs   (API)

Both model slots are served by one llama-server (settings.llamacpp_url): slot 0 is main, slot 1 the
helpers. The legacy product has its own app (legacy/main.py).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.assistant import main as main_slot
from app.assistant.store import Store
from app.config import settings
from app.providers import LlamaCppProvider, Provider
from app.rag.embed import make_embedder

from .api import assistant

WEB_PAGE = Path(__file__).resolve().parent.parent / "web" / "index.html"


def create_app(assistant_parts: tuple[Store, Provider, Provider] | None = None) -> FastAPI:
    """Build the app. Tests pass their own store and the models of the two slots (main, helpers)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        own_store = assistant_parts is None
        if own_store:
            def slot(n: int) -> LlamaCppProvider:
                return LlamaCppProvider(settings.llamacpp_model, settings.llamacpp_url, settings.assistant_thinking,
                                        slot=n, max_tokens=settings.assistant_max_tokens, cancel=main_slot.STOP,
                                        thinking_budget=settings.assistant_thinking_budget or None)
            store, main, worker = Store(settings.assistant_dir, make_embedder(settings)), slot(0), slot(1)
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
    app.include_router(assistant.router)

    @app.get("/", include_in_schema=False)
    def chat_page() -> FileResponse:
        """The single-page chat UI (web/index.html)."""
        return FileResponse(WEB_PAGE, media_type="text/html", headers={"Cache-Control": "no-cache"})

    @app.get("/health", tags=["system"])
    def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
