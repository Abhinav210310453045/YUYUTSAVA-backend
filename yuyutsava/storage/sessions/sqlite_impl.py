"""SQLite-backed ``SessionStore`` — concrete implementation.

Lives in the same DB file as the LangGraph ``AsyncSqliteSaver`` checkpointer
(see :mod:`yuyutsava.storage.sessions.checkpointer`) but in a disjoint
``sessions`` table — one WAL file, one fsync per write batch.

Concurrency
-----------
* WAL + ``busy_timeout`` allow concurrent readers (daemon polling) while the
  CLI writes. Writes serialize via SQLite's internal lock.
* Each mutation runs in ``BEGIN IMMEDIATE`` with retry on ``SQLITE_BUSY``.
* A per-process ``asyncio.Lock`` (provided by ``BaseSqliteStore``) serializes
  within-process writers so the retry loop doesn't fight itself.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, ClassVar

import aiosqlite

from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.ids import mint_thread_id
from yuyutsava.storage.models import SESSION_STATUSES, Session
from yuyutsava.storage.sessions.config import SessionsSettings
from yuyutsava.storage.sessions.store import SessionNotFound


_TASK_PREVIEW_MAX = 200
_TITLE_MAX = 80


class SqliteSessionStore(BaseSqliteStore):
    """``SessionStore`` impl backed by a single sqlite file via aiosqlite."""

    # v2: added the `origin` column (cli|voice) so the Sessions UI can split
    # voice vs CLI conversations off a DB column rather than a UI heuristic.
    # v3: added the `title` column — set once from the session's first user
    # message so conversation lists can show a human name instead of the id.
    _SCHEMA_VERSION: ClassVar[int] = 3
    _META_TABLE: ClassVar[str] = "sessions_meta"
    _SCHEMA_SQL: ClassVar[str] = """
    CREATE TABLE IF NOT EXISTS sessions_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS sessions (
        id                 TEXT PRIMARY KEY,
        thread_id          TEXT NOT NULL,
        workspace          TEXT NOT NULL,
        status             TEXT NOT NULL,
        created_at         REAL NOT NULL,
        updated_at         REAL NOT NULL,
        message_count      INTEGER NOT NULL DEFAULT 0,
        memory_files_count INTEGER NOT NULL DEFAULT 0,
        db_row_bytes       INTEGER NOT NULL DEFAULT 0,
        task_preview       TEXT NOT NULL DEFAULT '',
        schema_version     INTEGER NOT NULL DEFAULT 1,
        origin             TEXT NOT NULL DEFAULT 'cli',
        title              TEXT NOT NULL DEFAULT ''
    );

    CREATE INDEX IF NOT EXISTS idx_sessions_workspace_updated
        ON sessions(workspace, updated_at DESC);
    CREATE INDEX IF NOT EXISTS idx_sessions_updated
        ON sessions(updated_at DESC);
    """
    # NOTE: the origin index is created in _migrate (not here): _SCHEMA_SQL runs
    # before _migrate, and on a legacy v1 DB the `origin` column doesn't exist
    # yet, so an index on it here would fail before the ALTER had a chance to run.

    async def _migrate(self, conn: aiosqlite.Connection) -> None:
        """Forward-only migrations for existing session DBs.

        v1 -> v2 adds the `origin` column. ``CREATE TABLE IF NOT EXISTS`` in
        ``_SCHEMA_SQL`` covers fresh DBs; this ALTER covers DBs created before
        the column existed. We tolerate a duplicate-column error defensively.
        The origin index is (re)created here, after the column is guaranteed to
        exist, for both fresh and migrated DBs.
        """
        cur = await conn.execute(
            f"SELECT value FROM {self._META_TABLE} WHERE key=?",
            (self._META_VERSION_KEY,),
        )
        row = await cur.fetchone()
        await cur.close()
        current = int(row[0]) if row else 0
        if current < 2:
            try:
                await conn.execute(
                    "ALTER TABLE sessions ADD COLUMN origin TEXT NOT NULL DEFAULT 'cli'"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
        if current < 3:
            try:
                await conn.execute(
                    "ALTER TABLE sessions ADD COLUMN title TEXT NOT NULL DEFAULT ''"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_origin_updated "
            "ON sessions(origin, updated_at DESC)"
        )
        if current < self._SCHEMA_VERSION:
            await conn.execute(
                f"UPDATE {self._META_TABLE} SET value=? WHERE key=?",
                (str(self._SCHEMA_VERSION), self._META_VERSION_KEY),
            )
        await conn.commit()

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
        tid = thread_id or mint_thread_id(origin)
        now = time.time()
        preview = (task or "").strip().replace("\n", " ")[:_TASK_PREVIEW_MAX]
        ws = str(workspace.resolve())
        row = (
            tid, tid, ws, "running", now, now, 0, 0, 0, preview,
            self._SCHEMA_VERSION, origin,
        )

        async def _do(conn: aiosqlite.Connection) -> None:
            await conn.execute(
                """INSERT INTO sessions
                   (id, thread_id, workspace, status, created_at, updated_at,
                    message_count, memory_files_count, db_row_bytes,
                    task_preview, schema_version, origin)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                row,
            )

        await self._run_write(_do)
        return Session(
            id=tid, thread_id=tid, workspace=Path(ws), status="running",
            created_at=now, updated_at=now, message_count=0,
            memory_files_count=0, db_row_bytes=0, task_preview=preview,
            schema_version=self._SCHEMA_VERSION, origin=origin, title="",
        )

    async def set_title_if_empty(self, session_id: str, title: str) -> None:
        t = (title or "").strip()[:_TITLE_MAX]
        if not t:
            return

        async def _do(conn: aiosqlite.Connection) -> None:
            await conn.execute(
                "UPDATE sessions SET title=? WHERE id=? "
                "AND (title IS NULL OR title='')",
                (t, session_id),
            )

        await self._run_write(_do)

    async def touch(
        self,
        session_id: str,
        *,
        message_delta: int = 0,
        memory_files_count: int | None = None,
        task_preview: str | None = None,
    ) -> None:
        now = time.time()
        preview = (
            None if task_preview is None
            else task_preview[:_TASK_PREVIEW_MAX]
        )

        async def _do(conn: aiosqlite.Connection) -> None:
            # Recompute checkpoint bytes for this thread. Both tables live in
            # the same file (checkpoints lives in checkpoints.db today, but
            # if a future migration co-locates them this stays correct).
            bytes_for_thread = 0
            try:
                cur = await conn.execute(
                    "SELECT COALESCE(SUM(LENGTH(checkpoint)+LENGTH(metadata)), 0) "
                    "FROM checkpoints WHERE thread_id=?",
                    (session_id,),
                )
                row = await cur.fetchone()
                await cur.close()
                bytes_for_thread = int(row[0]) if row else 0
            except sqlite3.OperationalError:
                # Checkpoints table not yet created — fine, treat as 0.
                pass

            sets = ["updated_at=?", "message_count=message_count+?", "db_row_bytes=?"]
            args: list[Any] = [now, int(message_delta), bytes_for_thread]
            if memory_files_count is not None:
                sets.append("memory_files_count=?")
                args.append(int(memory_files_count))
            if preview is not None:
                sets.append("task_preview=?")
                args.append(preview)
            args.append(session_id)
            await conn.execute(
                f"UPDATE sessions SET {', '.join(sets)} WHERE id=?",
                tuple(args),
            )

        await self._run_write(_do)

    async def update_status(self, session_id: str, status: str) -> None:
        if status not in SESSION_STATUSES:
            raise ValueError(
                f"status must be one of {SESSION_STATUSES}, got {status!r}"
            )
        now = time.time()

        async def _do(conn: aiosqlite.Connection) -> None:
            await conn.execute(
                "UPDATE sessions SET status=?, updated_at=? WHERE id=?",
                (status, now, session_id),
            )

        await self._run_write(_do)

    async def delete(self, session_id: str) -> None:
        async def _do(conn: aiosqlite.Connection) -> None:
            await conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))

        await self._run_write(_do)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get(self, session_id: str) -> Session:
        await self._ensure_schema()
        async with self._conn() as conn:
            cur = await conn.execute(
                "SELECT id, thread_id, workspace, status, created_at, updated_at, "
                "message_count, memory_files_count, db_row_bytes, task_preview, "
                "schema_version, origin, title FROM sessions WHERE id=?",
                (session_id,),
            )
            row = await cur.fetchone()
            await cur.close()
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
        await self._ensure_schema()
        if order_by not in ("updated_at", "created_at"):
            raise ValueError(
                f"order_by must be updated_at or created_at, got {order_by!r}"
            )
        sql = (
            "SELECT id, thread_id, workspace, status, created_at, updated_at, "
            "message_count, memory_files_count, db_row_bytes, task_preview, "
            "schema_version, origin, title FROM sessions"
        )
        clauses: list[str] = []
        params: tuple[Any, ...] = ()
        if workspace is not None:
            clauses.append("workspace=?")
            params = (*params, str(workspace.resolve()))
        if origin is not None:
            clauses.append("origin=?")
            params = (*params, origin)
        if cursor is not None:
            clauses.append(f"{order_by} < ?")
            params = (*params, float(cursor))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += f" ORDER BY {order_by} DESC LIMIT ?"
        params = (*params, int(limit))
        async with self._conn() as conn:
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
            await cur.close()
        return [_row_to_session(r) for r in rows]

    async def list_thread_family(
        self, base: str, *, limit: int = 50
    ) -> list[Session]:
        """Sessions whose id is ``base`` or ``base:<suffix>``, newest first.

        substr() comparison rather than LIKE — ids contain ``_`` (a LIKE
        wildcard), so a LIKE pattern would over-match sibling cards.
        """
        await self._ensure_schema()
        prefix = base + ":"
        async with self._conn() as conn:
            cur = await conn.execute(
                "SELECT id, thread_id, workspace, status, created_at, updated_at, "
                "message_count, memory_files_count, db_row_bytes, task_preview, "
                "schema_version, origin, title FROM sessions "
                "WHERE id=? OR substr(id, 1, ?)=? "
                "ORDER BY updated_at DESC LIMIT ?",
                (base, len(prefix), prefix, int(limit)),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [_row_to_session(r) for r in rows]


def _row_to_session(row: Any) -> Session:
    """Build a ``Session`` from either a tuple row or an ``aiosqlite.Row``.

    ``BaseSqliteStore`` configures ``conn.row_factory = aiosqlite.Row`` so
    reads return rows that support both tuple-unpacking and key lookup.
    """
    (
        sid, tid, ws, status, created, updated, msgs, mems, dbbytes, preview,
        ver, origin, title,
    ) = row
    return Session(
        id=sid, thread_id=tid, workspace=Path(ws), status=status,
        created_at=created, updated_at=updated, message_count=msgs,
        memory_files_count=mems, db_row_bytes=dbbytes, task_preview=preview,
        schema_version=ver, origin=origin, title=title,
    )


# ---------------------------------------------------------------------------
# Process-level default store factory
# ---------------------------------------------------------------------------

from yuyutsava.storage.backend import StorageSettings  # noqa: E402 — avoid cycle at import time
from yuyutsava.storage.sessions.store import SessionStore  # noqa: E402

_DEFAULT_STORE: SessionStore | None = None


def set_default_session_store(store: SessionStore) -> None:
    """Override the process-wide default (the daemon injects a pool-backed
    :class:`PgSessionStore` at boot so the web router reuses its pool)."""
    global _DEFAULT_STORE
    _DEFAULT_STORE = store


def get_default_session_store() -> SessionStore:
    """Lazy singleton — one store per process, backend chosen from env.

    Postgres mode (``YUYUTSAVA_STORAGE_BACKEND=postgres`` or
    ``YUYUTSAVA_SESSIONS_BACKEND=postgres``) yields a :class:`PgSessionStore`
    so the CLI and daemon share one durable, JOINable ``sessions`` table;
    otherwise the SQLite twin in ``~/.yuyutsava/sessions.db`` (cross-process
    coordination via SQLite's WAL lock). Override via
    :func:`set_default_session_store`.
    """
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        s = SessionsSettings.from_env()
        storage = StorageSettings.from_env()
        if storage.is_postgres() or s.backend == "postgres":
            from yuyutsava.storage.sessions.pg_impl import PgSessionStore
            _DEFAULT_STORE = PgSessionStore(storage)
        else:
            _DEFAULT_STORE = SqliteSessionStore(
                s.db_path, busy_timeout_ms=s.busy_timeout_ms
            )
    return _DEFAULT_STORE
