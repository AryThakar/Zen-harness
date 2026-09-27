"""FastAPI application.

    uvicorn app.main:app --reload
    open http://localhost:8000/docs
"""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from langgraph.checkpoint.sqlite import SqliteSaver

from app.agent.graph import build_graph
from app.agent.providers import Provider, make_provider
from app.config import settings
from app.db.connection import Database
from app.ml import runner
from app.rag.embed import Embedder, make_embedder
from app.rag.library import Library

from .api import chat, dashboard, data, documents, ml, uploads
from .api.deps import get_cursor
from .api.schemas import HealthOut
from .api.staging import Staging


def create_app(
    db: Database | None = None,
    staging_dir: str | Path | None = None,
    provider: Provider | None = None,
    checkpointer=None,
    embedder: Embedder | None = None,
) -> FastAPI:
    """Build the app. Tests pass their own database, staging folder, model, memory and embedder."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.db = db or Database(settings.db_path)
        runner.mark_interrupted(app.state.db)
        app.state.staging = Staging(staging_dir or settings.staging_dir, settings.max_upload_mb * 1024 * 1024)
        app.state.provider = provider or make_provider(settings)
        memory, sqlite_conn = checkpointer, None
        if memory is None:
            Path(settings.chat_db_path).parent.mkdir(parents=True, exist_ok=True)
            sqlite_conn = sqlite3.connect(settings.chat_db_path, check_same_thread=False)
            memory = SqliteSaver(sqlite_conn)
        app.state.library = Library(app.state.db, embedder or make_embedder(settings))
        app.state.chat_graph = build_graph(app.state.db, app.state.provider, memory, library=app.state.library)
        yield
        if sqlite_conn is not None:
            sqlite_conn.close()
        if db is None:
            app.state.db.close()

    app = FastAPI(
        title="AI Business Analyst",
        description="Upload sales data, explore it, forecast it, and ask an AI assistant about it.",
        version="0.5.0",
        lifespan=lifespan,
    )
    app.include_router(uploads.router)
    app.include_router(data.router)
    app.include_router(dashboard.router)
    app.include_router(ml.router)
    app.include_router(documents.router)
    app.include_router(chat.router)

    @app.get("/health", response_model=HealthOut, tags=["system"])
    def health(cur=Depends(get_cursor)) -> HealthOut:
        sales, uploads_count = cur.execute(
            "SELECT (SELECT count(*) FROM sales), (SELECT count(*) FROM uploads)"
        ).fetchone()
        return HealthOut(status="ok", sales_rows=sales, uploads=uploads_count)

    return app


app = create_app()
