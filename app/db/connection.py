"""DuckDB access: one connection per process, a cursor per request, serialized writes.

The schema is built by numbered SQL files in ``migrations/``; each runs once, in
order, and is recorded in ``schema_migrations``, so an existing database file is
upgraded in place when the app changes.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb

from app.ingestion.profiles import seed_builtin_profiles

from . import registry

MIGRATIONS_DIR = Path(__file__).with_name("migrations")


class Database:
    def __init__(self, path: str | Path = ":memory:"):
        path = str(path)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = duckdb.connect(path)
        self._write_lock = threading.RLock()
        self.migrate()
        with self.transaction() as cur:
            registry.seed(cur)
            seed_builtin_profiles(cur)

    def migrate(self) -> list[str]:
        """Apply pending migrations; returns the names applied."""
        with self.transaction() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, name VARCHAR NOT NULL, "
                "applied_at TIMESTAMP NOT NULL DEFAULT current_timestamp)"
            )
            done = {v for (v,) in cur.execute("SELECT version FROM schema_migrations").fetchall()}
        applied = []
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = int(path.name.split("_", 1)[0])
            if version in done:
                continue
            with self.transaction() as cur:
                cur.execute(path.read_text())
                cur.execute("INSERT INTO schema_migrations (version, name) VALUES (?, ?)", [version, path.stem])
            applied.append(path.stem)
        if applied:
            # Write the new schema into the database file now rather than leaving it
            # in the write-ahead log: DuckDB can't replay some schema changes from the
            # log after a crash (see 004_explicit_ids.sql).
            self._conn.execute("CHECKPOINT")
        return applied

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
