"""``db_*`` agent tools — read-only access to the daemon's SQLite stores.

These call directly into :mod:`yuyutsava.storage.introspect`; the same safety
guarantees (``mode=ro`` URI + sqlglot validator + LIMIT cap + timeout) apply
whether the caller is an agent or the HTTP API.

Tool names use the ``db_`` prefix so they are hidden by ``ToolFilterMiddleware``
upfront and discovered on demand via ``tool_search('db_*')``.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from langchain_core.tools import BaseTool, tool

from yuyutsava.storage.introspect import (
    DbApiError,
    execute_read_query,
    list_databases,
    list_tables,
    table_schema,
)


def _err(error: str, hint: str | None = None) -> str:
    out: dict[str, Any] = {"status": "error", "error": error}
    if hint:
        out["hint"] = hint
    return json.dumps(out)


def _ok(result: Any) -> str:
    return json.dumps({"status": "success", "result": result}, default=str)


def make_db_tools() -> list[BaseTool]:
    """Return the three db_* tools. Stateless — re-resolve DB paths on each call."""

    @tool
    async def db_list() -> str:
        """List the SQLite databases the daemon exposes for read-only inspection.

        Returns JSON ``{status, result: [{name, path, exists, size_bytes}, ...]}``.
        ``name`` is the logical handle (e.g. ``"state"``, ``"sessions"``) you pass
        to ``db_schema`` and ``db_query``.
        """
        try:
            return _ok([asdict(d) for d in await list_databases()])
        except DbApiError as e:
            return _err(str(e))

    @tool
    async def db_schema(db: str, table: str | None = None) -> str:
        """Introspect a database. With no ``table``, returns the list of tables/views.
        With ``table``, returns ``PRAGMA table_info`` rows for that table.

        Args:
            db:    Logical database name (see ``db_list``).
            table: Optional table or view name.

        Returns JSON ``{status, result: [...]}`` or ``{status: "error", error, hint?}``.
        """
        try:
            if table is None:
                return _ok(await list_tables(db))
            return _ok(await table_schema(db, table))
        except DbApiError as e:
            return _err(str(e))

    @tool
    async def db_query(
        db: str,
        sql: str,
        params: list[Any] | None = None,
        limit: int | None = None,
    ) -> str:
        """Run a single read-only ``SELECT`` / ``WITH`` / ``PRAGMA table_info``.

        Writes, multi-statement SQL, ATTACH, and ``load_extension`` are rejected.
        A row LIMIT of 1000 is enforced (overridable downward via ``limit``).
        Queries are aborted after 5s.

        Args:
            db:     Logical database name (see ``db_list``).
            sql:    The statement. Use ``?`` placeholders for values + ``params``.
            params: Optional positional parameters bound to ``?`` placeholders.
            limit:  Optional row cap (default 1000, max 1000).

        Returns JSON ``{status, result: {rows, columns, truncated, elapsed_ms}}``.
        """
        try:
            res = await execute_read_query(db, sql, params=params, limit=limit)
            return _ok(res)
        except DbApiError as e:
            return _err(str(e), hint=(
                "Only SELECT/WITH/PRAGMA table_info(...) are allowed. "
                "Use db_schema(db, table) to discover columns first."
            ))

    return [db_list, db_schema, db_query]
