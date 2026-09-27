"""Browse stored data: sales rows, the column registry and format profiles."""

from __future__ import annotations

import json
from datetime import date, timedelta

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.db import registry
from app.db.connection import Database

from .deps import get_cursor, get_db
from .schemas import ColumnInfoOut, DescriptionIn, MappingModel, ProfileOut, SalesPage

router = APIRouter(tags=["data"])

SORTABLE = ("order_date", "revenue", "profit", "quantity", "unit_price", "product", "customer_id")
TEXT_FILTERS = ("product", "category", "location", "channel", "customer_id", "order_id")
HIDDEN_COLUMNS = {"row_hash"}


@router.get("/sales", response_model=SalesPage)
def list_sales(
    start: date | None = None,
    end: date | None = None,
    product: str | None = None,
    category: str | None = None,
    location: str | None = None,
    channel: str | None = None,
    customer_id: str | None = None,
    order_id: str | None = None,
    q: str | None = Query(None, description="search product, order and customer"),
    returns: bool | None = Query(None, description="true = only returns, false = no returns"),
    sort: str = Query("order_date", pattern="^(" + "|".join(SORTABLE) + ")$"),
    desc: bool = True,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    cur: duckdb.DuckDBPyConnection = Depends(get_cursor),
) -> SalesPage:
    where, params = [], []
    if start:
        where.append("order_date >= ?")
        params.append(start)
    if end:
        where.append("order_date < ?")
        params.append(end + timedelta(days=1))
    filters = {"product": product, "category": category, "location": location,
               "channel": channel, "customer_id": customer_id, "order_id": order_id}
    for column in TEXT_FILTERS:
        if filters[column] is not None:
            where.append(f"{column} = ?")
            params.append(filters[column])
    if q:
        where.append("(product ILIKE ? OR order_id ILIKE ? OR customer_id ILIKE ?)")
        params += [f"%{q}%"] * 3
    if returns is not None:
        where.append("is_return = ?")
        params.append(returns)
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    total = cur.execute(f"SELECT count(*) FROM sales {clause}", params).fetchone()[0]
    columns = [c for (c,) in cur.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'sales' ORDER BY ordinal_position"
    ).fetchall() if c not in HIDDEN_COLUMNS]
    select = ", ".join(f'"{c}"' for c in columns)
    rows = cur.execute(
        f"SELECT {select} FROM sales {clause} "
        f"ORDER BY {sort} {'DESC' if desc else 'ASC'} NULLS LAST, sale_id LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return SalesPage(total=total, columns=columns, rows=[dict(zip(columns, r)) for r in rows])


@router.get("/schema", response_model=list[ColumnInfoOut])
def list_columns(cur: duckdb.DuckDBPyConnection = Depends(get_cursor)) -> list[ColumnInfoOut]:
    """Every column of the sales table with its meaning - what the AI reads too."""
    return [ColumnInfoOut(**c) for c in registry.columns(cur)]


@router.patch("/schema/{column}", response_model=ColumnInfoOut)
def describe_column(column: str, body: DescriptionIn, db: Database = Depends(get_db)) -> ColumnInfoOut:
    """Change a column's description (helps the AI understand custom columns)."""
    with db.transaction() as cur:
        updated = registry.set_description(cur, column, body.description)
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Column '{column}' not found.")
    return ColumnInfoOut(**updated)


@router.get("/profiles", response_model=list[ProfileOut])
def list_profiles(cur: duckdb.DuckDBPyConnection = Depends(get_cursor)) -> list[ProfileOut]:
    rows = cur.execute(
        "SELECT name, builtin, mapping, created_at FROM format_profiles ORDER BY builtin, name"
    ).fetchall()
    return [ProfileOut(name=n, builtin=b, mapping=MappingModel(**json.loads(m)), created_at=c)
            for n, b, m, c in rows]


@router.delete("/profiles/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_profile(name: str, db: Database = Depends(get_db)) -> None:
    with db.transaction() as cur:
        row = cur.execute("SELECT builtin FROM format_profiles WHERE name = ?", [name]).fetchone()
        if not row:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Profile '{name}' not found.")
        if row[0]:
            raise HTTPException(status.HTTP_409_CONFLICT, "Built-in profiles can't be deleted.")
        cur.execute("DELETE FROM format_profiles WHERE name = ?", [name])
