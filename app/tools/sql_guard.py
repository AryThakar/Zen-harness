"""Check and run SQL written by the AI (query_data; the legacy product uses it through
legacy/agent/sql_guard.py, with its own tables).

Defence in depth:
1. This module parses the query (sqlglot, DuckDB dialect) and accepts only a
   single read-only query over the caller's allowlist of tables - no DDL/DML, no
   commands, no file-reading or table functions, no schema-qualified names -
   then runs the re-generated SQL, so what executes is exactly what was checked.
   Table references are resolved the way SQL does (a CTE may refer to a later
   CTE's name, which then means the real table), and a CTE may not take the name
   of a table outside the allowlist, so naming tricks can't reach other tables.
2. The caller's connection has external access switched off, so even a query
   that slipped through could not read files, attach databases or load
   extensions.
3. Results are capped and queries time out.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import duckdb
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

ALLOWED_TABLE_FUNCTIONS = {"unnest", "generate_series", "range"}
BLOCKED_FUNCTION_PREFIXES = ("read_", "sniff_", "glob", "getenv", "query", "load", "install", "parquet_")
FORBIDDEN_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter,
    exp.Command, exp.Copy, exp.Set, exp.Pragma, exp.Attach, exp.Detach, exp.Use,
)
MAX_ROWS = 200
TIMEOUT_SECONDS = 10


class SqlRejected(ValueError):
    """The query isn't allowed; the message says why, for the AI to fix it."""


@dataclass
class QueryResult:
    sql: str
    columns: list[str]
    rows: list[tuple]
    total_rows: int
    truncated: bool


def check(sql: str, existing: frozenset[str] = frozenset(), *, allowed: frozenset[str] | set[str]) -> str:
    """Validate ``sql``; return the normalized query to run. ``existing`` names the
    database's tables and views, so a CTE can't be named after a hidden one; ``allowed``
    are the tables the query may read."""
    try:
        statements = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except sqlglot.errors.ParseError as exc:
        raise SqlRejected(f"Could not parse the SQL: {str(exc).splitlines()[0]}") from exc
    if len(statements) != 1:
        raise SqlRejected("Send exactly one SELECT statement.")
    statement = statements[0]
    if not isinstance(statement, exp.Query):
        raise SqlRejected("Only read-only SELECT queries are allowed.")
    forbidden = statement.find(*FORBIDDEN_NODES)
    if forbidden is not None:
        raise SqlRejected(f"'{forbidden.key.upper()}' is not allowed; only SELECT queries.")

    cte_names = {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}
    hiding = sorted((cte_names & {name.lower() for name in existing}) - allowed)
    if hiding:
        raise SqlRejected(f"Rename the CTE '{hiding[0]}': it has the name of a table that isn't available.")
    for table in statement.find_all(exp.Table):
        if isinstance(table.this, exp.Func):
            name = _function_name(table.this)
            if name not in ALLOWED_TABLE_FUNCTIONS:
                raise SqlRejected(f"Table function '{name}' is not allowed.")
            continue
        if table.args.get("db") or table.args.get("catalog"):
            raise SqlRejected(f"Use table names without a schema: '{table.sql(dialect='duckdb')}'.")
        name = table.name.lower()
        if name not in allowed and name not in cte_names:
            raise SqlRejected(
                f"Table '{table.name}' is not available. Tables: {', '.join(sorted(allowed))}."
            )
    # Resolve references by scope: 'schema_migrations' inside a CTE defined before
    # a CTE of that name means the real table, whatever the names say.
    try:
        scopes = traverse_scope(statement)
    except Exception as exc:  # fail closed on anything the resolver can't follow
        raise SqlRejected(f"Could not analyse the query ({exc}); write it more simply.") from exc
    for scope in scopes:
        for source in scope.sources.values():
            if isinstance(source, exp.Table) and not isinstance(source.this, exp.Func):
                if source.name.lower() not in allowed:
                    raise SqlRejected(f"Table '{source.name}' is not available.")
    for func in statement.find_all(exp.Func):
        name = _function_name(func)
        if name.startswith(BLOCKED_FUNCTION_PREFIXES):
            raise SqlRejected(f"Function '{name}' is not allowed.")
    return statement.sql(dialect="duckdb")


def run(cur: duckdb.DuckDBPyConnection, sql: str, max_rows: int = MAX_ROWS,
        timeout: float = TIMEOUT_SECONDS, *, allowed: frozenset[str] | set[str]) -> QueryResult:
    """Check and run a query, returning at most ``max_rows`` rows."""
    existing = frozenset(name for (name,) in cur.execute(
        "SELECT table_name FROM duckdb_tables() UNION SELECT view_name FROM duckdb_views()").fetchall())
    safe_sql = check(sql, existing, allowed=allowed)
    timer = threading.Timer(timeout, cur.interrupt)
    timer.start()
    try:
        result = cur.execute(safe_sql)
        columns = [d[0] for d in result.description]
        rows = result.fetchmany(max_rows + 1)
        truncated = len(rows) > max_rows
        total = len(rows)
        if truncated:
            rows = rows[:max_rows]
            total = cur.execute(f"SELECT count(*) FROM ({safe_sql}) AS q").fetchone()[0]
    except duckdb.InterruptException as exc:
        raise SqlRejected(f"The query took longer than {timeout:g} seconds; simplify it.") from exc
    except duckdb.Error as exc:
        raise SqlRejected(f"DuckDB error: {str(exc).splitlines()[0]}") from exc
    finally:
        timer.cancel()
    return QueryResult(sql=safe_sql, columns=columns, rows=rows, total_rows=total, truncated=truncated)


def _function_name(func: exp.Func) -> str:
    return (func.name if isinstance(func, exp.Anonymous) else func.sql_name()).lower()
