"""SQLite-backed audit log for HITL interrupts.

Records every permission prompt / user-question interrupt across both
invocation modes (``cli`` and ``daemon``) so we can later query "for session
X, what interrupts happened and which agent asked?" without scraping logs.

The DB is dedicated (see :func:`yuyutsava.storage.paths.interrupts_db_path`)
and uses proper relational columns so a future migration into a unified
events DB is a copy-table away.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import ClassVar

import aiosqlite

from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.models import InterruptRecord

logger = logging.getLogger("yuyutsava.storage.interrupts")


class InterruptsStore(BaseSqliteStore):
    """Async SQLite store for the HITL audit log."""

    _SCHEMA_VERSION: ClassVar[int] = 1
    _META_TABLE: ClassVar[str] = "interrupts_meta"
    _SCHEMA_SQL: ClassVar[str] = """
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

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def record(self, record: InterruptRecord) -> str:
        """Persist a new interrupt. Returns the generated row id.

        Best-effort: any exception is logged and an empty string is returned
        so a store failure never blocks the user prompt on the critical path.
        """
        row_id = str(uuid.uuid4())
        try:
            paths_json = json.dumps(record.paths) if record.paths is not None else None
        except (TypeError, ValueError):
            paths_json = None
        try:
            payload_json = json.dumps(record.payload, default=str)
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
                    row_id, record.session_id, record.thread_id, record.agent_path,
                    record.requesting_agent, record.parent_agent,
                    record.invocation_mode, record.kind,
                    record.operation, paths_json, record.zone, record.risk_level,
                    record.reason, record.question,
                    payload_json, now,
                ),
            )
            return row_id

        try:
            return await self._run_write(_do)
        except Exception as exc:  # noqa: BLE001
            logger.warning("InterruptsStore.record failed: %s", exc)
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
            logger.warning("InterruptsStore.resolve failed: %s", exc)

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
            logger.warning("InterruptsStore.mark_orphaned_for_session failed: %s", exc)
            return 0

    # ------------------------------------------------------------------
    # Reads — return typed InterruptRecord, never dict
    # ------------------------------------------------------------------

    async def list_for_session(
        self,
        session_id: str,
        *,
        limit: int = 100,
    ) -> list[InterruptRecord]:
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
        return [_row_to_record(r) for r in rows]

    async def list_recent(
        self,
        *,
        agent_path_prefix: str | None = None,
        limit: int = 50,
    ) -> list[InterruptRecord]:
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
        return [_row_to_record(r) for r in rows]


def _row_to_record(row: aiosqlite.Row) -> InterruptRecord:
    """Rehydrate an :class:`InterruptRecord` from a stored row."""
    try:
        payload = json.loads(row["payload_json"])
    except (json.JSONDecodeError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    paths_json = row["paths_json"]
    paths: list[str] | None
    if paths_json:
        try:
            decoded = json.loads(paths_json)
            paths = list(decoded) if isinstance(decoded, (list, tuple)) else None
        except (json.JSONDecodeError, TypeError):
            paths = None
    else:
        paths = None
    return InterruptRecord(
        session_id=row["session_id"],
        thread_id=row["thread_id"],
        invocation_mode=row["invocation_mode"],
        payload=payload,
        kind=row["kind"],
        agent_path=row["agent_path"],
        requesting_agent=row["requesting_agent"],
        parent_agent=row["parent_agent"],
        operation=row["operation"],
        paths=paths,
        zone=row["zone"],
        risk_level=row["risk_level"],
        reason=row["reason"],
        question=row["question"],
        id=row["id"],
        outcome=row["outcome"],
        user_response=row["user_response"],
        created_at=row["created_at"],
        resolved_at=row["resolved_at"],
    )
