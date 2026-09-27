"""The column registry: every column of the sales table with its type and meaning.

The AI agent reads it to know what it can query, so custom columns carry the
description the user gave them.
"""

from __future__ import annotations

import duckdb

from app.ingestion.fields import DERIVED_COLUMNS, FIELDS, SALES_COLUMNS, SQL_TYPES

KIND_ORDER = "CASE kind WHEN 'required' THEN 0 WHEN 'optional' THEN 1 WHEN 'derived' THEN 2 ELSE 3 END"
SAMPLE_ROWS = 20_000  # example values come from a fixed random sample, so reads stay fast
SAMPLE_VALUES = 5


def builtin_columns() -> dict[str, tuple[str, str, str]]:
    """Canonical and derived columns: name -> (type, kind, default description)."""
    columns = {f.name: (SQL_TYPES[f.dtype], f.kind, f.description) for f in FIELDS if f.name in SALES_COLUMNS}
    columns.update({name: (sql_type, "derived", desc) for name, (sql_type, desc) in DERIVED_COLUMNS.items()})
    return columns


def seed(cur: duckdb.DuckDBPyConnection) -> None:
    """Register the canonical and derived columns (idempotent). Default descriptions
    follow the code, except where the user has written their own."""
    cur.executemany(
        """
        INSERT INTO column_registry (column_name, data_type, kind, description) VALUES (?, ?, ?, ?)
        ON CONFLICT (column_name) DO UPDATE SET
            data_type = excluded.data_type, kind = excluded.kind,
            description = CASE WHEN column_registry.edited THEN column_registry.description
                               ELSE excluded.description END
        """,
        [(name, *info) for name, info in builtin_columns().items()],
    )


def custom_types(cur: duckdb.DuckDBPyConnection) -> dict[str, str]:
    rows = cur.execute("SELECT column_name, data_type FROM column_registry WHERE kind = 'custom'").fetchall()
    return dict(rows)


def add_custom_columns(
    cur: duckdb.DuckDBPyConnection, types: dict[str, str], descriptions: dict[str, str]
) -> None:
    """Add custom columns to the sales table and register them (inside a transaction)."""
    for name, sql_type in types.items():
        cur.execute(f'ALTER TABLE sales ADD COLUMN IF NOT EXISTS "{name}" {sql_type}')
        cur.execute(
            """
            INSERT INTO column_registry (column_name, data_type, kind, description)
            VALUES (?, ?, 'custom', ?)
            ON CONFLICT (column_name) DO UPDATE SET
                description = coalesce(excluded.description, column_registry.description)
            """,
            [name, sql_type, descriptions.get(name)],
        )


def columns(cur: duckdb.DuckDBPyConnection, with_samples: bool = True) -> list[dict]:
    rows = cur.execute(
        f"SELECT column_name, data_type, kind, description FROM column_registry ORDER BY {KIND_ORDER}, column_name"
    ).fetchall()
    result = []
    for name, data_type, kind, description in rows:
        entry = {"column_name": name, "data_type": data_type, "kind": kind, "description": description}
        if with_samples:
            entry["sample_values"] = samples(cur, name) if data_type in ("VARCHAR", "BOOLEAN") else []
        result.append(entry)
    return result


def samples(cur: duckdb.DuckDBPyConnection, column: str) -> list:
    """The most common values of ``column`` in a repeatable sample of rows."""
    rows = cur.execute(
        f"""
        SELECT "{column}", count(*) AS n
        FROM (SELECT "{column}" FROM sales USING SAMPLE reservoir({SAMPLE_ROWS} ROWS) REPEATABLE (42))
        WHERE "{column}" IS NOT NULL
        GROUP BY 1 ORDER BY n DESC, 1 LIMIT {SAMPLE_VALUES}
        """
    ).fetchall()
    return [value for value, _ in rows]


def set_description(cur: duckdb.DuckDBPyConnection, column: str, description: str | None) -> dict | None:
    """The user's description of a column; clearing a built-in column's restores its default."""
    default = builtin_columns().get(column)
    edited = description is not None or default is None
    if description is None and default is not None:
        description = default[2]
    row = cur.execute(
        "UPDATE column_registry SET description = ?, edited = ? WHERE column_name = ? "
        "RETURNING column_name, data_type, kind, description",
        [description, edited, column],
    ).fetchone()
    if row is None:
        return None
    name, data_type, kind, desc = row
    return {"column_name": name, "data_type": data_type, "kind": kind, "description": desc,
            "sample_values": samples(cur, name) if data_type in ("VARCHAR", "BOOLEAN") else []}
