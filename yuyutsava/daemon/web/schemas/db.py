"""Pydantic schemas for the /db introspection routes."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class DatabaseInfo(BaseModel):
    name: str
    path: str
    exists: bool
    size_bytes: int | None = None


class TableInfo(BaseModel):
    name: str
    type: str  # "table" | "view"


class ColumnInfo(BaseModel):
    cid: int
    name: str
    type: str
    notnull: bool
    default_value: Any | None = None
    pk: int


class QueryIn(BaseModel):
    sql: str = Field(..., description="A single SELECT/WITH/PRAGMA table_info statement.")
    params: list[Any] | None = Field(
        default=None,
        description="Positional parameters bound to ? placeholders in the SQL.",
    )
    limit: int | None = Field(
        default=None,
        ge=1,
        description="Row cap (default 1000, max 1000).",
    )


class QueryOut(BaseModel):
    rows: list[dict[str, Any]]
    columns: list[str]
    truncated: bool
    elapsed_ms: int
