"""Read-only SQLite introspection + query executor.

Two layers of safety, by design:

1. The DB is opened with the ``?mode=ro`` URI — SQLite refuses any write at the
   driver layer.
2. The SQL is parsed with **sqlglot** before execution; only single-statement
   ``SELECT`` / ``WITH`` / ``PRAGMA table_info`` is allowed. ``ATTACH``,
   ``load_extension``, multi-statements, and DDL/DML are rejected even though
   the connection would block them too — defense in depth.

Plus an enforced ``LIMIT`` cap and a wall-clock timeout via SQLite's
progress handler, so a pathological query (``SELECT randomblob(1e9)``,
gigantic cross joins) can't pin the daemon.

Two databases are exposed today:

  - ``state``    — the daemon's event/proposal/decision store.
  - ``sessions`` — the CLI session index + LangGraph checkpoints.

Add another by extending the ``_databases()`` map.

Lived under ``daemon/db_introspect.py`` until the storage restructure — it's a
storage concern (read-only SQL execution against the persistence layer), not
daemon-specific. The HTTP shim at ``daemon/web/routers/db.py`` and the
``db_*`` agent tools at ``agents/db_tools/tools.py`` are the two consumers.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite
import sqlglot
from sqlglot import expressions as exp

from yuyutsava.storage.paths import sessions_db_path, state_db_path

logger = logging.getLogger("yuyutsava.storage.introspect")


DEFAULT_LIMIT = 1000
MAX_LIMIT = 1000
QUERY_TIMEOUT_SEC = 5.0


@dataclass(frozen=True)
class DatabaseInfo:
    """Typed row returned from :func:`list_databases`.

    Replaces the previous ``dict[str, Any]`` shape — the HTTP layer's Pydantic
    ``DatabaseInfo`` schema mirrors these fields and is built via
    ``dataclasses.asdict`` at the router boundary.
    """

    name: str
    path: str
    exists: bool
    size_bytes: int | None = None


def _databases() -> dict[str, Path]:
    """Built fresh per call so env overrides (e.g. ``YUYUTSAVA_SESSIONS_DB``) take effect."""
    return {
        "state": state_db_path(),
        "sessions": sessions_db_path(),
    }


class DbApiError(ValueError):
    """Raised for any invalid db name, sql, or arguments. The HTTP layer maps
    this to HTTP 400 with the message; agent tools surface it as a structured
    error envelope."""


def _resolve(db: str) -> Path:
    dbs = _databases()
    if db not in dbs:
        raise DbApiError(f"unknown database {db!r}; known: {sorted(dbs)}")
    p = dbs[db]
    if not p.exists():
        raise DbApiError(f"database {db!r} not initialised yet (file does not exist)")
    return p


# ---------------------------------------------------------------------------
# SQL validator
# ---------------------------------------------------------------------------


_FORBIDDEN_NODES: tuple[type, ...] = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop,
    exp.Create, exp.Alter,
    exp.Command,
)
# Some sqlglot versions don't expose AttachDatabase as a top-level class;
# treat it via exp.Command-name fallback in the walk below.


def _validate_select(sql: str) -> exp.Expression:
    """Parse and validate that ``sql`` is exactly one read-only statement.

    Returns the parsed expression so callers can re-render normalised SQL.
    Raises ``DbApiError`` with a human-readable reason on any violation.
    """
    try:
        parsed = sqlglot.parse(sql, read="sqlite")
    except Exception as exc:  # noqa: BLE001 — re-wrap as our error
        raise DbApiError(f"could not parse sql: {exc}") from exc

    statements = [p for p in parsed if p is not None]
    if not statements:
        raise DbApiError("empty sql")
    if len(statements) > 1:
        raise DbApiError("multi-statement sql is not allowed")

    root = statements[0]

    # Top-level must be Select / With / Pragma.
    if not isinstance(root, (exp.Select, exp.Subquery, exp.Union)) and not _is_with(root) and not _is_safe_pragma(root):
        raise DbApiError(f"only SELECT / WITH / PRAGMA table_info are allowed (got {type(root).__name__})")

    for node in root.walk():
        # `walk` yields tuples in some versions; normalise.
        item = node[0] if isinstance(node, tuple) else node
        if isinstance(item, _FORBIDDEN_NODES):
            raise DbApiError(f"forbidden statement node: {type(item).__name__}")
        # Anonymous function calls — block load_extension and friends.
        if isinstance(item, exp.Anonymous):
            fname = (item.name or "").lower()
            if fname in {"load_extension", "attach", "detach"}:
                raise DbApiError(f"function {fname!r} is not allowed")
        # Catch ATTACH DATABASE via Command name (some sqlglot versions parse it that way).
        if isinstance(item, exp.Command):
            cname = (getattr(item, "name", "") or "").lower()
            if cname in {"attach", "detach"}:
                raise DbApiError(f"{cname.upper()} is not allowed")

    return root


def _is_with(node: exp.Expression) -> bool:
    # `WITH cte AS (SELECT...) SELECT ...` parses as a Select with a `with` arg.
    return isinstance(node, exp.Select) and node.args.get("with") is not None


_SAFE_PRAGMAS: frozenset[str] = frozenset({
    "table_info", "index_list", "index_info", "foreign_key_list", "schema_version",
})


def _is_safe_pragma(node: exp.Expression) -> bool:
    """Allow only introspection pragmas.

    sqlglot parses ``PRAGMA table_info(proposals)`` as
    ``Pragma(this=EQ(this=Var("table_info"), expression=Column("proposals")))``
    — so the pragma's name is buried under ``args["this"].this``. We walk down
    cautiously, falling back to False on any unexpected shape.
    """
    if not isinstance(node, exp.Pragma):
        return False
    this = node.args.get("this")
    # Plain `PRAGMA <name>` (no value) — this is the bare Var.
    if isinstance(this, exp.Var):
        return (this.name or "").lower() in _SAFE_PRAGMAS
    # `PRAGMA table_info(x)` shape — EQ(Var(name), Column(arg)).
    if isinstance(this, exp.EQ):
        lhs = this.args.get("this")
        if isinstance(lhs, exp.Var):
            return (lhs.name or "").lower() in _SAFE_PRAGMAS
    return False


def _ensure_limit(sql: str, parsed: exp.Expression, limit: int) -> str:
    """Wrap or rewrite ``sql`` to enforce ``LIMIT``. Returns possibly-rewritten SQL."""
    capped = min(limit, MAX_LIMIT)
    if isinstance(parsed, exp.Select):
        existing = parsed.args.get("limit")
        if existing is None:
            parsed.set("limit", exp.Limit(expression=exp.Literal.number(capped)))
            return parsed.sql(dialect="sqlite")
        # Honor user's LIMIT but never let it exceed MAX_LIMIT.
        try:
            user_limit = int(str(existing.expression))
            if user_limit > MAX_LIMIT:
                parsed.set("limit", exp.Limit(expression=exp.Literal.number(MAX_LIMIT)))
                return parsed.sql(dialect="sqlite")
        except Exception:  # noqa: BLE001 — non-numeric LIMIT, wrap defensively
            return f"SELECT * FROM ({sql}) LIMIT {capped}"
        return sql
    # WITH or PRAGMA — wrap as a subquery for simplicity.
    return f"SELECT * FROM ({sql}) LIMIT {capped}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def list_databases() -> list[DatabaseInfo]:
    """Return [DatabaseInfo(name, path, exists, size_bytes), ...]."""
    out: list[DatabaseInfo] = []
    for name, path in _databases().items():
        size_bytes: int | None = None
        if path.exists():
            try:
                size_bytes = path.stat().st_size
            except OSError:
                size_bytes = None
        out.append(DatabaseInfo(
            name=name, path=str(path), exists=path.exists(), size_bytes=size_bytes,
        ))
    return out


async def list_tables(db: str) -> list[dict[str, Any]]:
    """Return non-system tables and views: [{name, type}, ...]."""
    path = _resolve(db)
    async with aiosqlite.connect(f"file:{path}?mode=ro", uri=True) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT name, type FROM sqlite_master "
            "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
        rows = await cur.fetchall()
        return [{"name": r["name"], "type": r["type"]} for r in rows]


async def table_schema(db: str, table: str) -> list[dict[str, Any]]:
    """Return PRAGMA table_info() rows for *table*."""
    path = _resolve(db)
    if not table or not table.replace("_", "").isalnum():
        raise DbApiError(f"invalid table name {table!r}")
    async with aiosqlite.connect(f"file:{path}?mode=ro", uri=True) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(f"PRAGMA table_info({table})")
        rows = await cur.fetchall()
        return [
            {
                "cid": r["cid"], "name": r["name"], "type": r["type"],
                "notnull": bool(r["notnull"]), "default_value": r["dflt_value"],
                "pk": int(r["pk"]),
            }
            for r in rows
        ]


async def execute_read_query(
    db: str,
    sql: str,
    params: list[Any] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Run a read-only SELECT against ``db`` after validating ``sql``.

    Returns ``{"rows": [{col: val, ...}, ...], "columns": [...], "truncated": bool,
    "elapsed_ms": int}``. Raises ``DbApiError`` on validation failure.
    """
    path = _resolve(db)
    parsed = _validate_select(sql)
    effective_limit = min(limit or DEFAULT_LIMIT, MAX_LIMIT)
    final_sql = _ensure_limit(sql, parsed, effective_limit)

    started = time.monotonic()

    async def _run() -> tuple[list[Any], list[str]]:
        async with aiosqlite.connect(f"file:{path}?mode=ro", uri=True) as conn:
            conn.row_factory = aiosqlite.Row
            # PRAGMA query_only = belt-and-braces; mode=ro already blocks writes.
            await conn.execute("PRAGMA query_only=ON")
            cur = await conn.execute(final_sql, tuple(params or ()))
            fetched = await cur.fetchmany(effective_limit + 1)
            cols = [d[0] for d in cur.description] if cur.description else []
            return fetched, cols

    try:
        rows, columns = await asyncio.wait_for(_run(), timeout=QUERY_TIMEOUT_SEC)
    except asyncio.TimeoutError as exc:
        raise DbApiError(
            f"query exceeded {QUERY_TIMEOUT_SEC}s timeout — narrow it with WHERE / LIMIT"
        ) from exc
    except Exception as exc:  # noqa: BLE001 — wrap sqlite errors uniformly
        raise DbApiError(f"sqlite error: {exc}") from exc

    truncated = len(rows) > effective_limit
    rows = rows[:effective_limit]
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return {
        "rows": [dict(r) for r in rows],
        "columns": columns,
        "truncated": truncated,
        "elapsed_ms": elapsed_ms,
    }
