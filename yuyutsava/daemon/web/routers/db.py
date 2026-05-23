"""HTTP endpoints for the read-only SQLite introspection API.

Mounted under ``/db`` from :mod:`yuyutsava.daemon.web.app`. Loopback-only by
deployment (the daemon binds to 127.0.0.1); no auth header is required in v1.
Toggle the whole surface off by setting ``YUYUTSAVA_DB_API_ENABLED=false``.

The hard safety guarantees live in :mod:`yuyutsava.storage.introspect` —
``mode=ro`` connections + sqlglot statement parsing. This router is the thin
FastAPI shim.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException

from yuyutsava.storage.introspect import (
    DbApiError,
    execute_read_query,
    list_databases,
    list_tables,
    table_schema,
)
from yuyutsava.daemon.web.schemas.db import (
    ColumnInfo,
    DatabaseInfo,
    QueryIn,
    QueryOut,
    TableInfo,
)

router = APIRouter(tags=["db"])


@router.get("/db/databases", response_model=list[DatabaseInfo], summary="List exposed databases")
async def get_databases() -> list[DatabaseInfo]:
    return [DatabaseInfo(**asdict(d)) for d in await list_databases()]


@router.get("/db/{db}/tables", response_model=list[TableInfo], summary="List tables and views")
async def get_tables(db: str) -> list[TableInfo]:
    try:
        return [TableInfo(**t) for t in await list_tables(db)]
    except DbApiError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get(
    "/db/{db}/tables/{table}/schema",
    response_model=list[ColumnInfo],
    summary="Column metadata for one table",
)
async def get_schema(db: str, table: str) -> list[ColumnInfo]:
    try:
        return [ColumnInfo(**c) for c in await table_schema(db, table)]
    except DbApiError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/db/{db}/query", response_model=QueryOut, summary="Run a read-only SELECT/PRAGMA")
async def post_query(db: str, body: QueryIn) -> QueryOut:
    try:
        result = await execute_read_query(
            db, body.sql, params=body.params, limit=body.limit,
        )
    except DbApiError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return QueryOut(**result)
