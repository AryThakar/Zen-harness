"""Shared FastAPI dependencies."""

from __future__ import annotations

from collections.abc import Iterator

import duckdb
from fastapi import Request

from app.db.connection import Database

from .staging import Staging


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_staging(request: Request) -> Staging:
    return request.app.state.staging


def get_cursor(request: Request) -> Iterator[duckdb.DuckDBPyConnection]:
    """A read cursor for the duration of one request."""
    cur = get_db(request).cursor()
    try:
        yield cur
    finally:
        cur.close()
