"""query_data: exact numbers from the workspace tables with one read-only DuckDB query.

Tables are queried by id (SELECT region, sum(amount) FROM data_2 GROUP BY region). The query is
checked by app.agent.sql_guard (one SELECT, only workspace tables, no file or system functions)
and runs on a connection with external access switched off, with a time limit. With save=true
the full result becomes a new table, so it can be charted, tested or modelled like any other.
"""

from __future__ import annotations

import duckdb
import sqlglot
from sqlglot import exp

from app.agent import sql_guard

from .core import ToolError, ToolResult, ok, tool
from .workspace import Workspace

MODEL_ROWS = 50  # rows the model sees; the UI table gets up to sql_guard.MAX_ROWS
SAVED_ROWS = 5  # with save=true the model works with the new table's id; a few rows show its shape


@tool
def query_data(ws: Workspace, sql: str, save: bool = False) -> ToolResult:
    """Run one SELECT over the workspace tables (named by id: data_1, data_2 ...)."""
    tables = set(ws.ids("table"))
    if not tables:
        raise ToolError("There are no tables yet; attach a file first.", invalid=False)
    con = duckdb.connect()
    try:
        used = sorted(_referenced(sql) & tables)
        for t in used:
            con.register(t, ws.table(t))
        con.execute("SET enable_external_access = false")
        con.execute("SET lock_configuration = true")
        try:
            res = sql_guard.run(con, sql, allowed=tables)
        except sql_guard.SqlRejected as exc:
            raise ToolError(f"{exc} Tables: {', '.join(sorted(tables))}.", field="sql") from exc
        shown = SAVED_ROWS if save else MODEL_ROWS
        result = {"columns": res.columns, "rows": [_tidy(r) for r in res.rows[:shown]], "total_rows": res.total_rows}
        if res.total_rows > shown:
            result["note"] = (f"Showing {shown} of {res.total_rows:,} rows"
                              + ("." if save else "; aggregate or add a LIMIT for fewer."))
        elif res.total_rows == 0 or (res.total_rows == 1 and all(v is None for v in res.rows[0])):
            # SUM / AVG over no rows give one row of NULLs: the same 'nothing matched'.
            result["note"] = ("No rows matched. Before concluding there are none, check the values you filtered on "
                              "(SELECT DISTINCT column ...): text must match exactly, including case and spelling.")
        warnings = [w for w in (ws.issues_warning(t) for t in used) if w]
        if warnings:
            result["data_warning"] = " ".join(warnings)
        if save:
            full = con.execute(res.sql).df()
            new_id = ws.add_table(full, name="query result", parent=used[0] if used else None,
                                  meta={"sql": res.sql})
            result["saved_as"] = new_id
    finally:
        con.close()
    artifact = {"type": "table", "title": "Query result", "columns": res.columns, "rows": [_tidy(r) for r in res.rows],
                "sql": res.sql, **({"note": f"First {len(res.rows)} of {res.total_rows:,} rows"} if res.truncated else {})}
    return ok(result, artifact)


def _tidy(row) -> list:
    """Floats without binary noise (2713.8500000000004 -> 2713.85); 9 decimals keep every exact figure."""
    return [round(v, 9) if isinstance(v, float) else v for v in row]


def _referenced(sql: str) -> set[str]:
    try:
        return {t.name.lower() for s in sqlglot.parse(sql, read="duckdb") if s for t in s.find_all(exp.Table)}
    except sqlglot.errors.ParseError:
        return set()  # sql_guard reports the parse error
