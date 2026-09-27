"""FastAPI application.

    uvicorn app.main:app --reload
    open http://localhost:8000/docs
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI

from app.config import settings
from app.db.connection import Database
from app.ml import runner

from .api import dashboard, data, ml, uploads
from .api.deps import get_cursor
from .api.schemas import HealthOut
from .api.staging import Staging


def create_app(db: Database | None = None, staging_dir: str | Path | None = None) -> FastAPI:
    """Build the app. Tests pass their own database and staging folder."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.db = db or Database(settings.db_path)
        runner.mark_interrupted(app.state.db)
        app.state.staging = Staging(staging_dir or settings.staging_dir, settings.max_upload_mb * 1024 * 1024)
        yield
        if db is None:
            app.state.db.close()

    app = FastAPI(
        title="AI Business Analyst",
        description="Upload sales data, explore it, forecast it, and (soon) ask questions about it.",
        version="0.3.0",
        lifespan=lifespan,
    )
    app.include_router(uploads.router)
    app.include_router(data.router)
    app.include_router(dashboard.router)
    app.include_router(ml.router)

    @app.get("/health", response_model=HealthOut, tags=["system"])
    def health(cur=Depends(get_cursor)) -> HealthOut:
        sales, uploads_count = cur.execute(
            "SELECT (SELECT count(*) FROM sales), (SELECT count(*) FROM uploads)"
        ).fetchone()
        return HealthOut(status="ok", sales_rows=sales, uploads=uploads_count)

    return app


app = create_app()
