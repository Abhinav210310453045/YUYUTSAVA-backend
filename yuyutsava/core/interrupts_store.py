"""SQLite-backed audit log for HITL interrupts.

Records every permission prompt / user-question interrupt across both
invocation modes (``cli`` and ``daemon``) so we can later query "for session
X, what interrupts happened and which agent asked?" without scraping logs.

The DB is dedicated (``~/.yuyutsava/interrupts.db``) and uses proper
relational columns so a future migration into a unified events DB is a
copy-table away. See ``yuyutsava.core.config.interrupts_db_path``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger("yuyutsava.interrupts_store")

SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS interrupts_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS interrupts (
    id                TEXT PRIMARY KEY,
    session_id        TEXT NOT NULL,
    thread_id         TEXT NOT NULL,
    agent_path        TEXT NOT NULL,
    requesting_agent  TEXT,
    parent_agent      TEXT,
    invocation_mode   TEXT NOT NULL,
    kind              TEXT NOT NULL,
    operation         TEXT,
    paths_json        TEXT,
    zone              TEXT,
    risk_level        TEXT,
    reason            TEXT,
    question          TEXT,
    payload_json      TEXT NOT NULL,
    outcome           TEXT,
    user_response     TEXT,
    created_at        REAL NOT NULL,
    resolved_at       REAL
);

CREATE INDEX IF NOT EXISTS idx_interrupts_session
    ON interrupts(session_id);
CREATE INDEX IF NOT EXISTS idx_interrupts_agent_path
    ON interrupts(agent_path);
CREATE INDEX IF NOT EXISTS idx_interrupts_created_at
    ON interrupts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_interrupts_unresolved
    ON interrupts(session_id, resolved_at);
"""


class InterruptsStore:
    """Async SQLite store. Same connection-handling shape as ``SqliteSessionStore``."""

    def __init__(self, db_path: Path, *, busy_timeout_ms: int = 5000) -> None:
        self._db_path = db_path
        self._busy_timeout_ms = busy_timeout_ms
        self._write_lock = asyncio.Lock()
        self._initialized = False

    @asynccontextmanager
    async def _conn(self):
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(self._db_path))
        try:
            await conn.execute(f"PRAGMA busy_timeout={int(self._busy_timeout_ms)}")
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            conn.row_factory = aiosqlite.Row
            yield conn
        finally:
            await conn.close()

    async def _ensure_schema(self) -> None:
        if self._initialized:
            return
        async with self._conn() as conn:
            await conn.executescript(_SCHEMA_SQL)
            await conn.execute(
                "INSERT OR IGNORE INTO interrupts_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            await conn.commit()
        self._initialized = True

    async def _run_write(self, fn) -> Any:
        await self._ensure_schema()
        async with self._write_lock:
            attempt = 0
            while True:
                try:
                    async with self._conn() as conn:
                        result = await fn(conn)
                        await conn.commit()
                        return result
                except sqlite3.OperationalError as exc:
                    if "locked" in str(exc).lower() and attempt < 3:
                        attempt += 1
                        await asyncio.sleep(0.05 * (2 ** attempt))
                        continue
                    raise

    async def record(
        self,
        *,
        payload: dict,
        session_id: str,
        thread_id: str,
        invocation_mode: str,
    ) -> str:
        """Persist a new interrupt. Returns the generated row id.

        Best-effort: any exception is swallowed and ``""`` is returned so a
        store failure never blocks the user prompt on the critical path.
        """
        if not isinstance(payload, dict):
            return ""
        row_id = str(uuid.uuid4())
        kind = str(payload.get("type") or "other")
        agent_path = str(payload.get("agent_path") or invocation_mode or "unknown")
        requesting_agent = payload.get("requesting_agent")
        parent_agent = payload.get("parent_agent")
        operation = payload.get("operation")
        paths = payload.get("paths")
        zone = payload.get("zone")
        risk_level = payload.get("risk_level")
        reason = payload.get("reason")
        question = payload.get("question")
        try:
            paths_json = json.dumps(paths) if paths is not None else None
        except (TypeError, ValueError):
            paths_json = None
        try:
            payload_json = json.dumps(payload, default=str)
        except (TypeError, ValueError):
            payload_json = "{}"
        now = time.time()

        async def _do(conn: aiosqlite.Connection) -> str:
            await conn.execute(
                """
                INSERT INTO interrupts (
                    id, session_id, thread_id, agent_path,
                    requesting_agent, parent_agent, invocation_mode, kind,
                    operation, paths_json, zone, risk_level, reason, question,
                    payload_json, outcome, user_response,
                    created_at, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, NULL)
                """,
                (
                    row_id, session_id, thread_id, agent_path,
                    requesting_agent, parent_agent, invocation_mode, kind,
                    operation, paths_json, zone, risk_level, reason, question,
                    payload_json, now,
                ),
            )
            return row_id

        try:
            return await self._run_write(_do)
        except Exception as exc:  # noqa: BLE001
            logger.warning("interrupts_store.record failed: %s", exc)
            return ""

    async def resolve(
        self,
        row_id: str,
        *,
        outcome: str,
        user_response: str | None = None,
    ) -> None:
        """Mark an interrupt row resolved. Best-effort; failures are logged only."""
        if not row_id:
            return
        now = time.time()

        async def _do(conn: aiosqlite.Connection) -> None:
            await conn.execute(
                """
                UPDATE interrupts
                   SET outcome = ?, user_response = ?, resolved_at = ?
                 WHERE id = ?
                """,
                (outcome, user_response, now, row_id),
            )

        try:
            await self._run_write(_do)
        except Exception as exc:  # noqa: BLE001
            logger.warning("interrupts_store.resolve failed: %s", exc)

    async def mark_orphaned_for_session(self, session_id: str) -> int:
        """Flip any unresolved interrupt rows for this session to ``orphaned``.

        Called from the resume path so a killed permission prompt leaves a
        clean audit row instead of a perpetually-open one.
        """
        if not session_id:
            return 0
        now = time.time()

        async def _do(conn: aiosqlite.Connection) -> int:
            cur = await conn.execute(
                """
                UPDATE interrupts
                   SET outcome = 'orphaned', resolved_at = ?
                 WHERE session_id = ? AND resolved_at IS NULL
                """,
                (now, session_id),
            )
            return cur.rowcount or 0

        try:
            return await self._run_write(_do)
        except Exception as exc:  # noqa: BLE001
            logger.warning("interrupts_store.mark_orphaned_for_session failed: %s", exc)
            return 0

    async def list_for_session(self, session_id: str, *, limit: int = 100) -> list[dict]:
        await self._ensure_schema()
        async with self._conn() as conn:
            cur = await conn.execute(
                """
                SELECT * FROM interrupts
                 WHERE session_id = ?
              ORDER BY created_at DESC
                 LIMIT ?
                """,
                (session_id, int(limit)),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [dict(r) for r in rows]

    async def list_recent(
        self,
        *,
        agent_path_prefix: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        await self._ensure_schema()
        async with self._conn() as conn:
            if agent_path_prefix:
                cur = await conn.execute(
                    """
                    SELECT * FROM interrupts
                     WHERE agent_path LIKE ?
                  ORDER BY created_at DESC
                     LIMIT ?
                    """,
                    (f"{agent_path_prefix}%", int(limit)),
                )
            else:
                cur = await conn.execute(
                    "SELECT * FROM interrupts ORDER BY created_at DESC LIMIT ?",
                    (int(limit),),
                )
            rows = await cur.fetchall()
            await cur.close()
        return [dict(r) for r in rows]
