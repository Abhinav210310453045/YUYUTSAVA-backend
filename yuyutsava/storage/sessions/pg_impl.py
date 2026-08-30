"""Postgres-backed ``SessionStore`` — the durable twin of SqliteSessionStore.

Selected when ``YUYUTSAVA_STORAGE_BACKEND=postgres`` (see
:func:`yuyutsava.storage.sessions.sqlite_impl.get_default_session_store`).
Moving the session index into Postgres lets it JOIN the rest of the relational
model: ``sessions.thread_id`` FKs to ``threads`` (migration v6), which in turn
ties sessions to ``tasks`` / ``llm_usage`` / ``artifacts`` for the first time.

Connection strategy
-------------------
* The **daemon** injects its shared :class:`PgPool` (``pool=...``); the store
  reuses it and trusts the owner to have run migrations at boot.
* The **CLI** has no pool lifecycle owner, so it constructs the store with
  ``pool=None``: each operation opens a short-lived autocommit connection to
  ``storage.pg_dsn`` (sessions are low-frequency), and the schema is ensured
  once per process via :func:`yuyutsava.storage.pg.migrations.apply`.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import psycopg

from yuyutsava.storage.backend import StorageSettings
from yuyutsava.storage.ids import mint_thread_id
from yuyutsava.storage.models import SESSION_STATUSES, Session
from yuyutsava.storage.pg import migrations as pg_migrations
from yuyutsava.storage.pg.pool import PgPool
from yuyutsava.storage.pg.threads import ensure_thread
from yuyutsava.storage.sessions.store import SessionNotFound

_TASK_PREVIEW_MAX = 200
_TITLE_MAX = 80
_SCHEMA_VERSION = 1

# Column NAMES, for INSERT. Keep the VALUES placeholder count in sync when
# adding a column. Split from _SELECT_COLS below because the read list carries
# expressions, not bare names, and an INSERT cannot take those.
_INSERT_COLS = (
    "id, thread_id, workspace, status, created_at, updated_at, "
    "message_count, memory_files_count, db_row_bytes, task_preview, "
    "schema_version, origin, title"
)

# Read list. created_at/updated_at are TIMESTAMPTZ since migration v20 and are
# projected back to epoch floats, so the Session dataclass and the web API's
# `cursor: float` are unchanged by that migration. ::float8 matters —
# extract(epoch ...) alone yields numeric, which psycopg returns as Decimal.
_SELECT_COLS = (
    "id, thread_id, workspace, status, "
    "extract(epoch FROM created_at)::float8 AS created_at, "
    "extract(epoch FROM updated_at)::float8 AS updated_at, "
    "message_count, memory_files_count, db_row_bytes, task_preview, "
    "schema_version, origin, title"
)


class PgSessionStore:
    """``SessionStore`` impl over the Postgres ``sessions`` table (migration v6)."""

    def __init__(
        self, storage: StorageSettings, *, pool: PgPool | None = None
    ) -> None:
        self._storage = storage
        self._pool = pool
        # An injected pool means the owner (the daemon) already ran migrations.
        self._ready = pool is not None
        self._ready_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Connection + schema plumbing
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def _conn(self):
        """Yield an autocommit connection — pooled (daemon) or short-lived (CLI)."""
        if self._pool is not None:
            async with self._pool.connection() as conn:
                yield conn
            return
        conn = await psycopg.AsyncConnection.connect(
            self._storage.pg_dsn, autocommit=True
        )
        try:
            yield conn
        finally:
            await conn.close()

    async def _ensure_ready(self) -> None:
        """Run migrations once per process when no pool owner did it for us."""
        if self._ready:
            return
        async with self._ready_lock:
            if self._ready:
                return
            transient = PgPool(self._storage)
            await transient.open()
            try:
                await pg_migrations.apply(transient)
            finally:
                await transient.close()
            self._ready = True

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def create(
        self,
        *,
        workspace: Path,
        task: str,
        thread_id: str | None = None,
        origin: str = "cli",
    ) -> Session:
        await self._ensure_ready()
        tid = thread_id or mint_thread_id(origin)
        now = time.time()
        preview = (task or "").strip().replace("\n", " ")[:_TASK_PREVIEW_MAX]
        ws = str(workspace.resolve())
        async with self._conn() as conn:
            # sessions.thread_id FKs to threads — upsert the parent first.
            await ensure_thread(
                conn, tid, origin=origin, workspace=ws, status="running"
            )
            await conn.execute(
                f"INSERT INTO sessions ({_INSERT_COLS}) VALUES "
                "(%s, %s, %s, %s, to_timestamp(%s), to_timestamp(%s), "
                "%s, %s, %s, %s, %s, %s, %s)",
                (tid, tid, ws, "running", now, now, 0, 0, 0, preview,
                 _SCHEMA_VERSION, origin, ""),
            )
        return Session(
            id=tid, thread_id=tid, workspace=Path(ws), status="running",
            created_at=now, updated_at=now, message_count=0,
            memory_files_count=0, db_row_bytes=0, task_preview=preview,
            schema_version=_SCHEMA_VERSION, origin=origin, title="",
        )

    async def set_title_if_empty(self, session_id: str, title: str) -> None:
        t = (title or "").strip()[:_TITLE_MAX]
        if not t:
            return
        await self._ensure_ready()
        async with self._conn() as conn:
            await conn.execute(
                "UPDATE sessions SET title = %s WHERE id = %s "
                "AND (title IS NULL OR title = '')",
                (t, session_id),
            )

    async def touch(
        self,
        session_id: str,
        *,
        message_delta: int = 0,
        memory_files_count: int | None = None,
        task_preview: str | None = None,
    ) -> None:
        await self._ensure_ready()
        now = time.time()
        preview = (
            None if task_preview is None else task_preview[:_TASK_PREVIEW_MAX]
        )
        async with self._conn() as conn:
            bytes_for_thread = await self._checkpoint_bytes(conn, session_id)
            sets = [
                "updated_at = to_timestamp(%s)",
                "message_count = message_count + %s",
                "db_row_bytes = %s",
            ]
            args: list[Any] = [now, int(message_delta), bytes_for_thread]
            if memory_files_count is not None:
                sets.append("memory_files_count = %s")
                args.append(int(memory_files_count))
            if preview is not None:
                sets.append("task_preview = %s")
                args.append(preview)
            args.append(session_id)
            await conn.execute(
                f"UPDATE sessions SET {', '.join(sets)} WHERE id = %s",
                tuple(args),
            )

    @staticmethod
    async def _checkpoint_bytes(conn: Any, thread_id: str) -> int:
        """Best-effort size of this thread's LangGraph checkpoints (now in the
        same DB). Returns 0 if the checkpointer hasn't created its tables yet."""
        try:
            cur = await conn.execute(
                "SELECT COALESCE(SUM(octet_length(checkpoint::text) "
                "+ octet_length(COALESCE(metadata::text, ''))), 0) "
                "FROM checkpoints WHERE thread_id = %s",
                (thread_id,),
            )
            row = await cur.fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        except Exception:
            return 0

    async def update_status(self, session_id: str, status: str) -> None:
        if status not in SESSION_STATUSES:
            raise ValueError(
                f"status must be one of {SESSION_STATUSES}, got {status!r}"
            )
        await self._ensure_ready()
        async with self._conn() as conn:
            await conn.execute(
                "UPDATE sessions SET status = %s, updated_at = to_timestamp(%s) WHERE id = %s",
                (status, time.time(), session_id),
            )

    async def delete(self, session_id: str) -> None:
        await self._ensure_ready()
        async with self._conn() as conn:
            await conn.execute("DELETE FROM sessions WHERE id = %s", (session_id,))

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get(self, session_id: str) -> Session:
        await self._ensure_ready()
        async with self._conn() as conn:
            cur = await conn.execute(
                f"SELECT {_SELECT_COLS} FROM sessions WHERE id = %s",
                (session_id,),
            )
            row = await cur.fetchone()
        if row is None:
            raise SessionNotFound(session_id)
        return _row_to_session(row)

    async def list(
        self,
        *,
        workspace: Path | None = None,
        limit: int = 100,
        order_by: str = "updated_at",
        cursor: float | None = None,
        origin: str | None = None,
    ) -> list[Session]:
        await self._ensure_ready()
        if order_by not in ("updated_at", "created_at"):
            raise ValueError(
                f"order_by must be updated_at or created_at, got {order_by!r}"
            )
        sql = f"SELECT {_SELECT_COLS} FROM sessions"
        clauses: list[str] = []
        params: list[Any] = []
        if workspace is not None:
            clauses.append("workspace = %s")
            params.append(str(workspace.resolve()))
        if origin is not None:
            clauses.append("origin = %s")
            params.append(origin)
        if cursor is not None:
            # Compares the raw TIMESTAMPTZ column, so the float cursor is cast.
            clauses.append(f"{order_by} < to_timestamp(%s)")
            params.append(float(cursor))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += f" ORDER BY {order_by} DESC LIMIT %s"
        params.append(int(limit))
        async with self._conn() as conn:
            cur = await conn.execute(sql, tuple(params))
            rows = await cur.fetchall()
        return [_row_to_session(r) for r in rows]

    async def list_thread_family(
        self, base: str, *, limit: int = 50
    ) -> list[Session]:
        """Sessions whose id is ``base`` or ``base:<suffix>``, newest first.

        left() comparison rather than LIKE — ids contain ``_`` (a LIKE
        wildcard), so a LIKE pattern would over-match sibling cards.
        """
        await self._ensure_ready()
        prefix = base + ":"
        async with self._conn() as conn:
            cur = await conn.execute(
                f"SELECT {_SELECT_COLS} FROM sessions "
                "WHERE id = %s OR left(id, %s) = %s "
                "ORDER BY updated_at DESC LIMIT %s",
                (base, len(prefix), prefix, int(limit)),
            )
            rows = await cur.fetchall()
        return [_row_to_session(r) for r in rows]


def _row_to_session(row: Any) -> Session:
    (
        sid, tid, ws, status, created, updated, msgs, mems, dbbytes, preview,
        ver, origin, title,
    ) = tuple(row)
    return Session(
        id=sid, thread_id=tid, workspace=Path(ws), status=status,
        created_at=float(created), updated_at=float(updated),
        message_count=int(msgs), memory_files_count=int(mems),
        db_row_bytes=int(dbbytes), task_preview=preview,
        schema_version=int(ver), origin=origin, title=title or "",
    )
