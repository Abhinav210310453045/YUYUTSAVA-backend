"""Transcript store: the full, durable conversation history per thread.

Where :mod:`yuyutsava.context.summary_store` keeps only the *condensed*
summary of a thread and LangGraph checkpoints keep the live state (swept after
~1h), this store keeps **every message verbatim** — one row per message, like
Cursor's per-bubble SQLite rows or Claude Code's per-session JSONL records.
:class:`~yuyutsava.context.transcript_middleware.TranscriptRecorderMiddleware`
appends messages here as the agent runs, so the transcript survives checkpoint
sweeps, daemon restarts, and compaction (which removes messages from live
state but never from this table).

Two interchangeable backends behind :class:`TranscriptStore`, same shape as
:mod:`yuyutsava.context.artifacts`:

- :class:`SqliteTranscriptStore` — a ``transcript_messages`` table in
  ``state.db`` (own meta table; coexists with the other stores via WAL).
- :class:`PgTranscriptStore` — the ``transcript_messages`` table created by
  :mod:`yuyutsava.storage.pg.migrations` (v7).

Each message is serialized with ``langchain_core.messages.message_to_dict``
so the typed record (role, content blocks, tool calls, tool_call_id) is
preserved with full fidelity. Dedup is by ``message_id`` (``INSERT OR IGNORE``
/ ``ON CONFLICT DO NOTHING``), so re-recording a resumed thread is idempotent.

Retention: transcripts are durable history, but ``delete_older_than`` exists
for parity with the other stores and lets operators bound growth via
:class:`yuyutsava.storage.sweeper.UnifiedSweeper` if desired.
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from langchain_core.messages import BaseMessage, message_to_dict

from yuyutsava.core.text_utils import sanitize_message_metadata

from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.pg.pool import PgPool
from yuyutsava.storage.pg.threads import ensure_thread

logger = logging.getLogger("yuyutsava.context.transcript_store")

DEFAULT_LIST_LIMIT = 1_000


@dataclass(frozen=True)
class TranscriptMessage:
    """One persisted conversation message."""

    thread_id: str
    message_id: str
    seq: int
    type: str
    content: dict
    created_ts: float


def _encode(message: BaseMessage) -> tuple[str, str, str] | None:
    """Return ``(message_id, type, content_json)`` or ``None`` if unrecordable.

    Messages without a stable id are skipped — they get an id once the
    ``add_messages`` reducer folds them into state, so a later turn records
    them. Serializing via ``message_to_dict`` keeps the typed record intact.
    """
    message_id = getattr(message, "id", None)
    if not message_id:
        return None
    # Backstop for the streamed-metadata accretion bug (OpenRouter echoes
    # finish_reason/model_name per chunk; LangChain's chunk merge concatenates
    # them). Streaming already sanitizes, but persist a clean record even for
    # messages that reach the store via other paths. No-op on clean values.
    sanitize_message_metadata(message)
    return str(message_id), message.type, json.dumps(message_to_dict(message))


class TranscriptStore(ABC):
    """Interface both backends implement."""

    @abstractmethod
    async def put_messages(
        self,
        thread_id: str,
        messages: Sequence[BaseMessage],
        *,
        task_id: str | None = None,
    ) -> int:
        """Append messages not already stored (dedup on ``message_id``).

        Returns the number of rows actually inserted.
        """

    @abstractmethod
    async def list_messages(
        self,
        thread_id: str,
        *,
        after_seq: int = 0,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> list[TranscriptMessage]:
        """Messages for ``thread_id`` ordered by ``seq`` ascending."""

    @abstractmethod
    async def delete_older_than(self, cutoff_ts: float) -> int:
        """TTL sweep hook (parity with the other stores). Returns rows deleted."""


class SqliteTranscriptStore(BaseSqliteStore, TranscriptStore):
    """``transcript_messages`` table inside ``state.db`` (zero-config fallback)."""

    _SCHEMA_VERSION = 1
    _META_TABLE = "transcript_messages_meta"
    _SCHEMA_SQL = """
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
        now = time.time()

        async def _do(conn):
            inserted = 0
            for message_id, mtype, content in rows:
                cur = await conn.execute(
                    "INSERT OR IGNORE INTO transcript_messages "
                    "(message_id, thread_id, type, content, task_id, created_ts) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (message_id, thread_id, mtype, content, task_id, now),
                )
                inserted += cur.rowcount or 0
            return inserted

        return await self._run_write(_do)

    async def list_messages(
        self,
        thread_id: str,
        *,
        after_seq: int = 0,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> list[TranscriptMessage]:
        await self._ensure_schema()
        async with self._conn() as conn:
            cur = await conn.execute(
                "SELECT seq, message_id, thread_id, type, content, created_ts "
                "FROM transcript_messages "
                "WHERE thread_id = ? AND seq > ? ORDER BY seq ASC LIMIT ?",
                (thread_id, after_seq, limit),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [
            TranscriptMessage(
                thread_id=r["thread_id"],
                message_id=r["message_id"],
                seq=r["seq"],
                type=r["type"],
                content=json.loads(r["content"]),
                created_ts=r["created_ts"],
            )
            for r in rows
        ]

    async def delete_older_than(self, cutoff_ts: float) -> int:
        async def _do(conn):
            cur = await conn.execute(
                "DELETE FROM transcript_messages WHERE created_ts < ?", (cutoff_ts,)
            )
            return cur.rowcount or 0

        return await self._run_write(_do)


class PgTranscriptStore(TranscriptStore):
    """``transcript_messages`` table in Postgres (schema owned by pg/migrations.py)."""

    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

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
        inserted = 0
        async with self._pool.connection() as conn:
            await ensure_thread(conn, thread_id)  # satisfy transcript_messages_thread_fk
            for message_id, mtype, content in rows:
                cur = await conn.execute(
                    "INSERT INTO transcript_messages "
                    "(message_id, thread_id, type, content, task_id) "
                    "VALUES (%s, %s, %s, %s::jsonb, %s) "
                    "ON CONFLICT (message_id) DO NOTHING",
                    (message_id, thread_id, mtype, content, task_id),
                )
                inserted += cur.rowcount or 0
        return inserted

    async def list_messages(
        self,
        thread_id: str,
        *,
        after_seq: int = 0,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> list[TranscriptMessage]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT seq, message_id, thread_id, type, content, "
                "extract(epoch FROM created_ts) "
                "FROM transcript_messages "
                "WHERE thread_id = %s AND seq > %s ORDER BY seq ASC LIMIT %s",
                (thread_id, after_seq, limit),
            )
            rows = await cur.fetchall()
        out: list[TranscriptMessage] = []
        for r in rows:
            content = r[4]
            if isinstance(content, str):
                content = json.loads(content)
            out.append(
                TranscriptMessage(
                    seq=r[0],
                    message_id=r[1],
                    thread_id=r[2],
                    type=r[3],
                    content=content,
                    created_ts=float(r[5]),
                )
            )
        return out

    async def delete_older_than(self, cutoff_ts: float) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM transcript_messages WHERE created_ts < to_timestamp(%s)",
                (cutoff_ts,),
            )
            return cur.rowcount or 0
