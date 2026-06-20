"""Rolling per-thread compaction summaries, persisted across restarts.

Every time :class:`YuyutsavaCompactionMiddleware` condenses a thread, the
produced summary lands here as a new version. The latest version is what
makes "cycle 3 still knows the plan" survive checkpoint sweeps and daemon
crashes: on resume with an empty history the middleware re-injects it.

Same two-backend shape as :mod:`yuyutsava.context.artifacts`.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.pg.pool import PgPool
from yuyutsava.storage.pg.threads import ensure_thread

logger = logging.getLogger("yuyutsava.context.summary_store")


@dataclass(frozen=True)
class ThreadSummary:
    thread_id: str
    version: int
    summary: str
    token_count: int
    task_id: str | None


class ThreadSummaryStore(ABC):
    @abstractmethod
    async def put(
        self,
        thread_id: str,
        summary: str,
        *,
        token_count: int = 0,
        task_id: str | None = None,
    ) -> int:
        """Append a new summary version for the thread; returns the version."""

    @abstractmethod
    async def latest(self, thread_id: str) -> ThreadSummary | None:
        """Most recent summary for the thread, or ``None``."""


class SqliteThreadSummaryStore(BaseSqliteStore, ThreadSummaryStore):
    _SCHEMA_VERSION = 1
    _META_TABLE = "thread_summaries_meta"
    _SCHEMA_SQL = """
        CREATE TABLE IF NOT EXISTS thread_summaries_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS thread_summaries (
            thread_id   TEXT NOT NULL,
            version     INTEGER NOT NULL,
            summary     TEXT NOT NULL,
            token_count INTEGER NOT NULL DEFAULT 0,
            task_id     TEXT,
            created_ts  REAL NOT NULL,
            PRIMARY KEY (thread_id, version)
        );
    """

    async def put(
        self,
        thread_id: str,
        summary: str,
        *,
        token_count: int = 0,
        task_id: str | None = None,
    ) -> int:
        async def _do(conn):
            cur = await conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM thread_summaries "
                "WHERE thread_id = ?",
                (thread_id,),
            )
            (version,) = await cur.fetchone()
            await conn.execute(
                "INSERT INTO thread_summaries "
                "(thread_id, version, summary, token_count, task_id, created_ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (thread_id, version, summary, token_count, task_id, time.time()),
            )
            return version

        return await self._run_write(_do)

    async def latest(self, thread_id: str) -> ThreadSummary | None:
        await self._ensure_schema()
        async with self._conn() as conn:
            cur = await conn.execute(
                "SELECT thread_id, version, summary, token_count, task_id "
                "FROM thread_summaries WHERE thread_id = ? "
                "ORDER BY version DESC LIMIT 1",
                (thread_id,),
            )
            row = await cur.fetchone()
            await cur.close()
        if row is None:
            return None
        return ThreadSummary(
            thread_id=row["thread_id"],
            version=row["version"],
            summary=row["summary"],
            token_count=row["token_count"],
            task_id=row["task_id"],
        )


class PgThreadSummaryStore(ThreadSummaryStore):
    """``thread_summaries`` table in Postgres (schema in pg/migrations.py)."""

    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    async def put(
        self,
        thread_id: str,
        summary: str,
        *,
        token_count: int = 0,
        task_id: str | None = None,
    ) -> int:
        async with self._pool.connection() as conn:
            await ensure_thread(conn, thread_id)  # satisfy thread_summaries_thread_fk
            cur = await conn.execute(
                """
                INSERT INTO thread_summaries
                    (thread_id, version, summary, token_count, task_id)
                SELECT %s, COALESCE(MAX(version), 0) + 1, %s, %s, %s
                FROM thread_summaries WHERE thread_id = %s
                RETURNING version
                """,
                (thread_id, summary, token_count, task_id, thread_id),
            )
            row = await cur.fetchone()
        return int(row[0])

    async def latest(self, thread_id: str) -> ThreadSummary | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT thread_id, version, summary, token_count, task_id "
                "FROM thread_summaries WHERE thread_id = %s "
                "ORDER BY version DESC LIMIT 1",
                (thread_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return ThreadSummary(
            thread_id=row[0], version=row[1], summary=row[2],
            token_count=row[3], task_id=row[4],
        )
