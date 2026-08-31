"""One ``ThreadSummaryStore`` over both backends — second domain on the adapter.

Phase 2 step 2.5b. Follows ``visuals/store_unified.py``; chosen next because it
is the smallest remaining pair (108 lines) and, unlike visuals, has **no on-disk
side effect** — so it tests whether the dialect seam generalises to a plain
row-only domain rather than only to the case it was designed against.

Version allocation, and a bug the parity suite exposed
------------------------------------------------------
The twins allocated the version number differently — SQLite did
``SELECT MAX(version)+1`` then a separate ``INSERT``; Postgres used a single
``INSERT ... SELECT ... RETURNING version``.

The Postgres form was *assumed* to be safe on the grounds that one statement
cannot interleave with a concurrent writer. **That assumption is wrong**, and
``test_concurrent_puts_get_distinct_versions`` proves it against a live server:
at READ COMMITTED the ``SELECT`` inside the ``INSERT`` still reads a *snapshot*,
so two concurrent transactions both see the same ``MAX(version)``, both insert
``max + 1``, and one dies on ``thread_summaries_pkey``.

This is a **pre-existing bug in ``PgThreadSummaryStore``**, not something the
migration introduced — the old twin fails the same test. SQLite never hit it
because ``BaseSqliteStore`` serialises writes through one lock.

So both backends now use the single-statement form (SQLite has supported
``RETURNING`` since 3.35; 3.50 here) **and** retry on duplicate-key, which is
what actually makes concurrent summary writes safe. Losing the race is normal
and cheap: re-read the max, insert again.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import ClassVar

from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.dialect import Dialect
from .summary_store import ThreadSummary, ThreadSummaryStore

logger = logging.getLogger("yuyutsava.context.summary_store_unified")

#: Bounded retries when concurrent writers race for the same version number.
#: 8 is generous: each loss means another writer succeeded, so contention this
#: deep implies a pathological number of simultaneous compactions on one thread.
_MAX_VERSION_RETRIES = 8


class SummarySchema(BaseSqliteStore):
    """SQLite DDL owner. Values match the original twin, so existing DBs load."""

    _SCHEMA_VERSION: ClassVar[int] = 1
    _META_TABLE: ClassVar[str] = "thread_summaries_meta"
    _SCHEMA_SQL: ClassVar[str] = """
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


class UnifiedThreadSummaryStore(ThreadSummaryStore):
    """``thread_summaries`` on whichever backend the dialect wraps."""

    def __init__(self, dialect: Dialect) -> None:
        self._d = dialect

    async def put(
        self,
        thread_id: str,
        summary: str,
        *,
        token_count: int = 0,
        task_id: str | None = None,
    ) -> int:
        d = self._d

        async def _do(conn):
            await d.ensure_parent(conn, thread_id)
            cur = await conn.execute(
                f"INSERT INTO thread_summaries "
                f"(thread_id, version, summary, token_count, task_id, created_ts) "
                f"SELECT {d.ph()}, COALESCE(MAX(version), 0) + 1, "
                f"       {d.ph()}, {d.ph()}, {d.ph()}, {d.ts_param()} "
                f"FROM thread_summaries WHERE thread_id = {d.ph()} "
                f"RETURNING version",
                (thread_id, summary, token_count, task_id, time.time(), thread_id),
            )
            row = await cur.fetchone()
            return int(row["version"])

        # Retry on duplicate-key. The MAX() runs against a transaction snapshot
        # on Postgres, so two concurrent writers can both pick the same next
        # version and one loses the primary key — being a single statement does
        # not prevent it. Losing is normal and cheap: re-read and insert again.
        # ``write()`` has already rolled the failed attempt back.
        last: BaseException | None = None
        for attempt in range(_MAX_VERSION_RETRIES):
            try:
                return await d.write(_do)
            except Exception as exc:  # noqa: BLE001 — re-raised below if not ours
                if not d.is_unique_violation(exc):
                    raise
                last = exc
                logger.debug(
                    "summary version race on thread=%s (attempt %d), retrying",
                    thread_id, attempt + 1,
                )
        raise RuntimeError(
            f"could not allocate a summary version for thread {thread_id!r} after "
            f"{_MAX_VERSION_RETRIES} attempts; last error: {last}"
        ) from last

    async def latest(self, thread_id: str) -> ThreadSummary | None:
        async with self._d.reading() as conn:
            cur = await conn.execute(
                f"SELECT thread_id, version, summary, token_count, task_id "
                f"FROM thread_summaries WHERE thread_id = {self._d.ph()} "
                f"ORDER BY version DESC LIMIT 1",
                (thread_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return ThreadSummary(
            thread_id=row["thread_id"],
            version=row["version"],
            summary=row["summary"],
            token_count=row["token_count"],
            task_id=row["task_id"],
        )


def sqlite_summary_store(db_path: Path | None = None) -> UnifiedThreadSummaryStore:
    from yuyutsava.storage.dialect import SqliteDialect
    from yuyutsava.storage.paths import state_db_path

    return UnifiedThreadSummaryStore(
        SqliteDialect(SummarySchema(db_path or state_db_path()))
    )


def pg_summary_store(pool) -> UnifiedThreadSummaryStore:
    from yuyutsava.storage.dialect import PostgresDialect

    return UnifiedThreadSummaryStore(PostgresDialect(pool))


__all__ = [
    "SummarySchema", "UnifiedThreadSummaryStore",
    "pg_summary_store", "sqlite_summary_store",
]
