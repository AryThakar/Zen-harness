"""DuckDB access: one connection per process, a cursor per request, serialized writes."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb

from app.ingestion.fields import DERIVED_COLUMNS, FIELDS, SALES_COLUMNS, SQL_TYPES
from app.ingestion.profiles import seed_builtin_profiles

SCHEMA_FILE = Path(__file__).with_name("schema.sql")


class Database:
    def __init__(self, path: str | Path = ":memory:"):
        path = str(path)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = duckdb.connect(path)
        self._write_lock = threading.RLock()
        with self.transaction() as cur:
            cur.execute(SCHEMA_FILE.read_text())
            _seed_registry(cur)
            seed_builtin_profiles(cur)

    def cursor(self) -> duckdb.DuckDBPyConnection:
        """A cursor for reads. Close it when done."""
        return self._conn.cursor()

    @contextmanager
    def transaction(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """A cursor inside a transaction; only one writer at a time."""
        with self._write_lock:
            cur = self._conn.cursor()
            cur.begin()
            try:
                yield cur
            except BaseException:
                cur.rollback()
                raise
            else:
                cur.commit()
            finally:
                cur.close()

    def close(self) -> None:
        self._conn.close()


def _seed_registry(cur: duckdb.DuckDBPyConnection) -> None:
    rows = [
        (f.name, SQL_TYPES[f.dtype], f.kind, f.description)
        for f in FIELDS
        if f.name in SALES_COLUMNS
    ]
    rows += [(name, sql_type, "derived", desc) for name, (sql_type, desc) in DERIVED_COLUMNS.items()]
    cur.executemany(
        """
        INSERT INTO column_registry (column_name, data_type, kind, description, sample_values)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (column_name) DO UPDATE SET
            data_type = excluded.data_type, kind = excluded.kind, description = excluded.description
        """,
        [(*row, json.dumps([])) for row in rows],
    )
