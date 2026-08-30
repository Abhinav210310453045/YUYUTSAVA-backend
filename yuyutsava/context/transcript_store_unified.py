"""One ``transcript_messages`` implementation, both backends.

Phase 2 step 2.5b (ADR-002), playbook order 8. Replaces
``SqliteTranscriptStore`` and ``PgTranscriptStore`` — 159 lines of near-duplicate
SQL — with one store over :mod:`yuyutsava.storage.dialect`.

This is the first domain that needs *every* capability the dialect offers, which
is a useful proof that the abstraction is the right size:

============  ====================================================
``json_param``/``json_value``  ``content`` is ``jsonb`` on PG, TEXT on SQLite
``ts_param``/``epoch``         ``created_ts`` is ``TIMESTAMPTZ`` vs a REAL epoch
``ensure_parent``              PG has ``transcript_messages_thread_fk``
``write``                      the per-message insert loop is one transaction
============  ====================================================

Three divergences the twins carried, resolved rather than preserved:

**Who sets ``created_ts``.** SQLite passed ``time.time()``; Postgres let the
column ``DEFAULT now()`` fire. Two clocks for one field — and the TTL sweep
compares them against a single application-side cutoff, so a database whose
clock drifts from the app's would sweep the wrong rows. The unified store writes
it explicitly on both.

**The dedup keyword.** ``INSERT OR IGNORE`` vs ``ON CONFLICT DO NOTHING``. Both
backends support the standard form, so that is what is used.

**Decoding ``content``.** SQLite always parsed; Postgres parsed only
``if isinstance(content, str)``. Identical results today, but two different
assumptions about what the driver returns. ``Dialect.json_value`` states it once.

Not changed: ``seq`` allocation stays ``AUTOINCREMENT``/``BIGSERIAL``. Neither
guarantees a gap-free sequence, so ``after_seq`` paging is written as
"greater than the last seq I saw" — never "last + 1".

Parity verified on both live backends by
``test/storage/test_transcript_store_parity.py``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar

from langchain_core.messages import BaseMessage

from yuyutsava.context.transcript_store import (
    DEFAULT_LIST_LIMIT,
    TranscriptMessage,
    TranscriptStore,
    _encode,
)
from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.dialect import Dialect

logger = logging.getLogger("yuyutsava.context.transcript_store_unified")


class TranscriptSchema(BaseSqliteStore):
    """SQLite DDL owner. Byte-identical to the retired twin's ``_SCHEMA_SQL``.

    Kept separate from the store so the unified store owns *statements* and
    nothing owns DDL twice — and so existing ``state.db`` files load unchanged
    (same meta table, same schema version, so no migration fires).
    """

    _SCHEMA_VERSION: ClassVar[int] = 1
    _META_TABLE: ClassVar[str] = "transcript_messages_meta"
    _SCHEMA_SQL: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS transcript_messages_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS transcript_messages (
            seq        INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL UNIQUE,
            thread_id  TEXT NOT NULL,
            type       TEXT NOT NULL,
            content    TEXT NOT NULL,
            task_id    TEXT,
            created_ts REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS transcript_thread_idx
            ON transcript_messages (thread_id, seq);
        CREATE INDEX IF NOT EXISTS transcript_created_idx
            ON transcript_messages (created_ts);
    """


class UnifiedTranscriptStore(TranscriptStore):
    """``transcript_messages`` — every conversation message, verbatim."""

    def __init__(self, dialect: Dialect) -> None:
        self._d = dialect

    async def put_messages(
        self,
        thread_id: str,
        messages: Sequence[BaseMessage],
        *,
        task_id: str | None = None,
    ) -> int:
        rows = [enc for m in messages if (enc := _encode(m)) is not None]
        if not rows:
            return 0
        d = self._d
        now = time.time()

        async def _do(conn):
            # Postgres carries transcript_messages_thread_fk; SQLite has no FKs
            # here, so this is a no-op there rather than a branch at the call site.
            await d.ensure_parent(conn, thread_id)
            inserted = 0
            for message_id, mtype, content in rows:
                # ON CONFLICT DO NOTHING, not DO UPDATE: a message is immutable
                # once recorded, and re-recording a resumed thread must be free.
                cur = await conn.execute(
                    f"INSERT INTO transcript_messages "
                    f"(message_id, thread_id, type, content, task_id, created_ts) "
                    f"VALUES ({d.ph(3)}, {d.json_param()}, {d.ph()}, {d.ts_param()}) "
                    f"ON CONFLICT (message_id) DO NOTHING",
                    (message_id, thread_id, mtype, content, task_id, now),
                )
                inserted += cur.rowcount or 0
            return inserted

        # One transaction for the whole batch: a partially-recorded turn is
        # worse than an unrecorded one, because the retry then dedups against
        # its own half-written rows.
        return await d.write(_do)

    async def list_messages(
        self,
        thread_id: str,
        *,
        after_seq: int = 0,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> list[TranscriptMessage]:
        d = self._d
        async with d.reading() as conn:
            cur = await conn.execute(
                f"SELECT seq, message_id, thread_id, type, content, "
                f"{d.epoch('created_ts')} "
                f"FROM transcript_messages "
                f"WHERE thread_id = {d.ph()} AND seq > {d.ph()} "
                f"ORDER BY seq ASC LIMIT {d.ph()}",
                (thread_id, after_seq, limit),
            )
            rows = await cur.fetchall()
        return [
            TranscriptMessage(
                thread_id=r["thread_id"],
                message_id=r["message_id"],
                seq=r["seq"],
                type=r["type"],
                # jsonb hands back a dict, TEXT a str. Callers index into this
                # (the web router does content.get("data", {})), so a raw string
                # would render an empty conversation rather than raise.
                content=d.json_value(r["content"]),
                created_ts=float(r["created_ts"]),
            )
            for r in rows
        ]

    async def delete_older_than(self, cutoff_ts: float) -> int:
        d = self._d

        async def _do(conn):
            cur = await conn.execute(
                f"DELETE FROM transcript_messages WHERE created_ts < {d.ts_param()}",
                (cutoff_ts,),
            )
            return cur.rowcount or 0

        return await d.write(_do)


def sqlite_transcript_store(db_path: Path | None = None) -> UnifiedTranscriptStore:
    from yuyutsava.storage.dialect import SqliteDialect
    from yuyutsava.storage.paths import state_db_path

    return UnifiedTranscriptStore(
        SqliteDialect(TranscriptSchema(db_path or state_db_path()))
    )


def pg_transcript_store(pool) -> UnifiedTranscriptStore:
    from yuyutsava.storage.dialect import PostgresDialect

    return UnifiedTranscriptStore(PostgresDialect(pool))


__all__ = [
    "TranscriptSchema",
    "UnifiedTranscriptStore",
    "pg_transcript_store",
    "sqlite_transcript_store",
]
