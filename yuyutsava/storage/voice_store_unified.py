"""One ``VoiceMessageStore`` over both backends — third domain on the adapter.

Phase 2 step 2.5b. Chosen third because, like visuals, it has an on-disk side
effect (``audio_blob_path``) — but unlike visuals the blobs are **not** owned by
this store: they are written and removed by :mod:`yuyutsava.audio_io.blobs`, and
``purge_session`` deletes them in its own step. The store holds only the path.

That difference is worth stating, because "row references a file" looked like it
implied "store must unlink the file" after the visuals migration. It does not,
and copying the visuals pattern blindly here would have double-deleted blobs.

Sequence numbers are auto-increment on both backends (SQLite ``AUTOINCREMENT``,
Postgres identity/serial), so unlike ``ThreadSummaryStore`` there is **no
version-allocation race** to defend against — the database assigns the number,
not a ``MAX()+1`` read. ``test_concurrent_puts_get_distinct_seqs`` pins that
rather than assuming it.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import ClassVar

from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.dialect import Dialect
from .voice_store import (
    DEFAULT_LIST_LIMIT,
    VoiceMessage,
    VoiceMessageStore,
    _validate,
)

logger = logging.getLogger("yuyutsava.storage.voice_store_unified")

_COLS = ("seq", "thread_id", "role", "modality", "text", "audio_blob_path", "sample_rate")


def _row_to_message(row) -> VoiceMessage:
    """One mapper for both backends (Postgres rows are ``dict_row`` mappings)."""
    return VoiceMessage(
        seq=int(row["seq"]),
        thread_id=row["thread_id"],
        role=row["role"],
        modality=row["modality"],
        text=row["text"] or "",
        audio_blob_path=row["audio_blob_path"],
        sample_rate=row["sample_rate"],
        created_ts=float(row["created_ts"]),
    )


class VoiceSchema(BaseSqliteStore):
    """SQLite DDL owner. Matches the original twin so existing DBs load as-is."""

    _SCHEMA_VERSION: ClassVar[int] = 1
    _META_TABLE: ClassVar[str] = "voice_messages_meta"
    _SCHEMA_SQL: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS voice_messages_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS voice_messages (
            seq             INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id       TEXT NOT NULL,
            role            TEXT NOT NULL,
            modality        TEXT NOT NULL,
            text            TEXT NOT NULL DEFAULT '',
            audio_blob_path TEXT,
            sample_rate     INTEGER,
            created_ts      REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS voice_messages_thread_idx
            ON voice_messages (thread_id, seq);
    """


class UnifiedVoiceMessageStore(VoiceMessageStore):
    """``voice_messages`` on whichever backend the dialect wraps."""

    def __init__(self, dialect: Dialect) -> None:
        self._d = dialect

    def _select(self) -> str:
        cols = ", ".join(_COLS)
        return f"SELECT {cols}, {self._d.epoch('created_ts')} FROM voice_messages"

    async def put_message(
        self,
        thread_id: str,
        *,
        role: str,
        text: str,
        modality: str = "text",
        audio_blob_path: str | None = None,
        sample_rate: int | None = None,
    ) -> int:
        _validate(role, modality)
        d = self._d
        now = time.time()

        async def _do(conn):
            await d.ensure_parent(conn, thread_id)
            # RETURNING on both: the database assigns seq, so the value is read
            # back rather than computed — no read-then-write race.
            cur = await conn.execute(
                f"INSERT INTO voice_messages "
                f"(thread_id, role, modality, text, audio_blob_path, sample_rate, created_ts) "
                f"VALUES ({d.ph(6)}, {d.ts_param()}) RETURNING seq",
                (thread_id, role, modality, text or "", audio_blob_path, sample_rate, now),
            )
            row = await cur.fetchone()
            return int(row["seq"])

        return await d.write(_do)

    async def list_messages(
        self,
        thread_id: str,
        *,
        after_seq: int = 0,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> list[VoiceMessage]:
        d = self._d
        async with d.reading() as conn:
            cur = await conn.execute(
                f"{self._select()} WHERE thread_id = {d.ph()} AND seq > {d.ph()} "
                f"ORDER BY seq ASC LIMIT {d.ph()}",
                (thread_id, after_seq, limit),
            )
            rows = await cur.fetchall()
        return [_row_to_message(r) for r in rows]

    async def get_message(self, thread_id: str, seq: int) -> VoiceMessage | None:
        d = self._d
        async with d.reading() as conn:
            cur = await conn.execute(
                f"{self._select()} WHERE thread_id = {d.ph()} AND seq = {d.ph()}",
                (thread_id, seq),
            )
            row = await cur.fetchone()
        return _row_to_message(row) if row else None

    async def delete_for_thread(self, thread_id: str) -> int:
        d = self._d

        async def _do(conn):
            cur = await conn.execute(
                f"DELETE FROM voice_messages WHERE thread_id = {d.ph()}", (thread_id,)
            )
            return cur.rowcount or 0

        # Audio blobs are NOT unlinked here — audio_io.blobs owns them and
        # purge_session removes them in its own step. Unlinking here too would
        # be a double delete.
        return await d.write(_do)


def sqlite_voice_store(db_path: Path | None = None) -> UnifiedVoiceMessageStore:
    from yuyutsava.storage.dialect import SqliteDialect
    from yuyutsava.storage.paths import state_db_path

    return UnifiedVoiceMessageStore(
        SqliteDialect(VoiceSchema(db_path or state_db_path()))
    )


def pg_voice_store(pool) -> UnifiedVoiceMessageStore:
    from yuyutsava.storage.dialect import PostgresDialect

    return UnifiedVoiceMessageStore(PostgresDialect(pool))


__all__ = [
    "UnifiedVoiceMessageStore", "VoiceSchema",
    "pg_voice_store", "sqlite_voice_store",
]
