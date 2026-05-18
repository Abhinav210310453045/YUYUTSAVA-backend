"""SQLite-backed ``SessionStore``.

Lives in the same DB file as the LangGraph ``AsyncSqliteSaver`` checkpointer
(see ``yuyutsava/sessions/checkpointer.py``) but in a disjoint ``sessions``
table — one WAL file, one fsync per write batch.

Concurrency model
-----------------
* WAL + ``busy_timeout`` allow concurrent readers (daemon polling) while the
  CLI writes. Writes serialize via SQLite's internal lock.
* Each mutation runs in ``BEGIN IMMEDIATE`` with up to 3 retries on
  ``SQLITE_BUSY``.
* A per-process ``asyncio.Lock`` serializes within-process writers so the
  retry loop doesn't fight itself.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from yuyutsava.sessions.models import SESSION_STATUSES, Session
from yuyutsava.sessions.store import SessionNotFound

SCHEMA_VERSION = 1

_SCHEMA_SQL = """
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
    schema_version     INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_sessions_workspace_updated
    ON sessions(workspace, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_updated
    ON sessions(updated_at DESC);
"""

_TASK_PREVIEW_MAX = 200


def mint_thread_id(role: str = "cli") -> str:
    """Sweeper-compatible thread id: ``<role>-<unix_ts>-<uuid4>``.

    Matches ``yuyutsava/daemon/checkpointing.py:thread_id`` so the existing TTL
    sweeper can age these rows out too if it later runs against this DB.
    """
    return f"{role}-{int(time.time())}-{uuid.uuid4()}"


class SqliteSessionStore:
    """``SessionStore`` impl backed by a single sqlite file via aiosqlite."""

    def __init__(self, db_path: Path, *, busy_timeout_ms: int = 5000) -> None:
        self._db_path = db_path
        self._busy_timeout_ms = busy_timeout_ms
        self._write_lock = asyncio.Lock()
        self._initialized = False

    @asynccontextmanager
    async def _conn(self):
        """Open a short-lived connection with WAL + busy_timeout configured."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(self._db_path))
        try:
            await conn.execute(f"PRAGMA busy_timeout={int(self._busy_timeout_ms)}")
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            yield conn
        finally:
            await conn.close()

    async def _ensure_schema(self) -> None:
        if self._initialized:
            return
        async with self._conn() as conn:
            await conn.executescript(_SCHEMA_SQL)
            await conn.execute(
                "INSERT OR IGNORE INTO sessions_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            await conn.commit()
            await self._migrate(conn)
        self._initialized = True

    async def _migrate(self, conn: aiosqlite.Connection) -> None:
        """Forward-only migration hook keyed off ``sessions_meta.schema_version``.

        Today only v1 exists; future bumps add ``if current < N: ALTER ...`` here.
        """
        cur = await conn.execute(
            "SELECT value FROM sessions_meta WHERE key='schema_version'"
        )
        row = await cur.fetchone()
        await cur.close()
        current = int(row[0]) if row else 0
        if current < SCHEMA_VERSION:
            await conn.execute(
                "UPDATE sessions_meta SET value=? WHERE key='schema_version'",
                (str(SCHEMA_VERSION),),
            )
            await conn.commit()

    async def _run_write(self, fn) -> Any:
        """Serialize per-process writes; retry on SQLITE_BUSY up to 3x."""
        await self._ensure_schema()
        async with self._write_lock:
            attempt = 0
            while True:
                try:
                    async with self._conn() as conn:
                        await conn.execute("BEGIN IMMEDIATE")
                        result = await fn(conn)
                        await conn.commit()
                        return result
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                        raise
                    attempt += 1
                    if attempt >= 3:
                        raise
                    await asyncio.sleep(0.05 * attempt)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create(
        self,
        *,
        workspace: Path,
        task: str,
        thread_id: str | None = None,
    ) -> Session:
        tid = thread_id or mint_thread_id("cli")
        now = time.time()
        preview = (task or "").strip().replace("\n", " ")[:_TASK_PREVIEW_MAX]
        ws = str(workspace.resolve())
        row = (tid, tid, ws, "running", now, now, 0, 0, 0, preview, SCHEMA_VERSION)

        async def _do(conn):
            await conn.execute(
                """INSERT INTO sessions
                   (id, thread_id, workspace, status, created_at, updated_at,
                    message_count, memory_files_count, db_row_bytes,
                    task_preview, schema_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                row,
            )

        await self._run_write(_do)
        return Session(
            id=tid, thread_id=tid, workspace=Path(ws), status="running",
            created_at=now, updated_at=now, message_count=0,
            memory_files_count=0, db_row_bytes=0, task_preview=preview,
            schema_version=SCHEMA_VERSION,
        )

    async def get(self, session_id: str) -> Session:
        await self._ensure_schema()
        async with self._conn() as conn:
            cur = await conn.execute(
                "SELECT id, thread_id, workspace, status, created_at, updated_at, "
                "message_count, memory_files_count, db_row_bytes, task_preview, "
                "schema_version FROM sessions WHERE id=?",
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
    ) -> list[Session]:
        await self._ensure_schema()
        if order_by not in ("updated_at", "created_at"):
            raise ValueError(f"order_by must be updated_at or created_at, got {order_by!r}")
        sql = (
            "SELECT id, thread_id, workspace, status, created_at, updated_at, "
            "message_count, memory_files_count, db_row_bytes, task_preview, "
            "schema_version FROM sessions"
        )
        clauses: list[str] = []
        params: tuple[Any, ...] = ()
        if workspace is not None:
            clauses.append("workspace=?")
            params = (*params, str(workspace.resolve()))
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

        async def _do(conn):
            # Recompute checkpoint bytes for this thread. Both tables live in
            # the same file, so this is one local query — no cross-file join.
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
            raise ValueError(f"status must be one of {SESSION_STATUSES}, got {status!r}")
        now = time.time()

        async def _do(conn):
            await conn.execute(
                "UPDATE sessions SET status=?, updated_at=? WHERE id=?",
                (status, now, session_id),
            )

        await self._run_write(_do)

    async def delete(self, session_id: str) -> None:
        async def _do(conn):
            await conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))

        await self._run_write(_do)


def _row_to_session(row: tuple) -> Session:
    (
        sid, tid, ws, status, created, updated, msgs, mems, dbbytes, preview, ver,
    ) = row
    return Session(
        id=sid, thread_id=tid, workspace=Path(ws), status=status,
        created_at=created, updated_at=updated, message_count=msgs,
        memory_files_count=mems, db_row_bytes=dbbytes, task_preview=preview,
        schema_version=ver,
    )


# ---------------------------------------------------------------------------
# Process-level default store factory
# ---------------------------------------------------------------------------

_DEFAULT_STORE: SqliteSessionStore | None = None


def get_default_session_store() -> SqliteSessionStore:
    """Lazy singleton — one store per process, ~/.yuyutsava/sessions.db by default.

    Both the CLI and the daemon resolve to the same instance for their own
    process; cross-process coordination falls back to SQLite's WAL lock.
    """
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        from yuyutsava.sessions.config import SessionsSettings
        s = SessionsSettings.from_env()
        _DEFAULT_STORE = SqliteSessionStore(s.db_path, busy_timeout_ms=s.busy_timeout_ms)
    return _DEFAULT_STORE
