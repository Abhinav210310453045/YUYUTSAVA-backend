"""Voice-message store: per-turn conversation rows for voice/chat threads.

Where :mod:`yuyutsava.context.transcript_store` keeps the *verbatim LangChain
message record* (every human/ai/tool message, for context fidelity), this store
keeps the **human-facing conversation surface of a voice session** — one row per
spoken turn, with the text *and* a reference to the synthesized TTS audio so the
UI can re-render and **replay** a resumed voice conversation (Phase 6b).

It is deliberately separate from ``transcript_messages``:

  * transcript rows carry tool calls, system messages, content blocks — noise
    for a chat bubble list and impossible to attach audio to cleanly;
  * voice rows are a thin, ordered ``(role, text, audio?)`` list keyed by
    ``(thread_id, seq)`` — exactly what the Voice panel renders.

Agent TTS audio is always persisted (it's what "▶ replay" plays back); the user
side stores the STT *transcript* (text) by default, with the raw user audio
optional/off (privacy + size). Audio bytes live on disk under
``blobs/voice/`` (see :func:`yuyutsava.storage.paths.blobs_dir`), mirroring the
``event_payloads`` blob convention; the DB row holds the path. Unlike scratch
blobs, these are **session-scoped user history** — deleted when the session is
deleted, not aged out by the TTL sweeper.

Two interchangeable backends behind :class:`VoiceMessageStore`, same shape as
:mod:`yuyutsava.context.transcript_store`:

  * :class:`SqliteVoiceMessageStore` — a ``voice_messages`` table in ``state.db``.
  * :class:`PgVoiceMessageStore` — the ``voice_messages`` table created by
    :mod:`yuyutsava.storage.pg.migrations` (v11).
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.pg.pool import PgPool
from yuyutsava.storage.pg.threads import ensure_thread

logger = logging.getLogger("yuyutsava.storage.voice_store")

DEFAULT_LIST_LIMIT = 1_000

# Valid column values — kept loose (plain TEXT in the DB) but validated here so a
# typo surfaces at the call site rather than as a silently un-renderable row.
ROLES = ("user", "assistant")
MODALITIES = ("text", "audio")


@dataclass(frozen=True)
class VoiceMessage:
    """One persisted voice-conversation turn."""

    thread_id: str
    seq: int
    role: str          # "user" | "assistant"
    modality: str      # "text" | "audio"
    text: str
    audio_blob_path: str | None
    sample_rate: int | None
    created_ts: float

    @property
    def has_audio(self) -> bool:
        return bool(self.audio_blob_path)


class VoiceMessageStore(ABC):
    """Interface both backends implement."""

    @abstractmethod
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
        """Append one turn; returns its monotonically increasing ``seq``."""

    @abstractmethod
    async def list_messages(
        self,
        thread_id: str,
        *,
        after_seq: int = 0,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> list[VoiceMessage]:
        """Turns for ``thread_id`` ordered by ``seq`` ascending."""

    @abstractmethod
    async def get_message(self, thread_id: str, seq: int) -> VoiceMessage | None:
        """One turn by ``(thread_id, seq)`` — used to serve its audio blob."""

    @abstractmethod
    async def delete_for_thread(self, thread_id: str) -> int:
        """Drop every row for a thread (on session delete). Returns rows deleted."""


def _validate(role: str, modality: str) -> None:
    if role not in ROLES:
        raise ValueError(f"voice message role must be one of {ROLES}, got {role!r}")
    if modality not in MODALITIES:
        raise ValueError(f"voice message modality must be one of {MODALITIES}, got {modality!r}")


class SqliteVoiceMessageStore(BaseSqliteStore, VoiceMessageStore):
    """``voice_messages`` table inside ``state.db`` (zero-config fallback)."""

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
        now = time.time()

        async def _do(conn):
            cur = await conn.execute(
                "INSERT INTO voice_messages "
                "(thread_id, role, modality, text, audio_blob_path, sample_rate, created_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (thread_id, role, modality, text or "", audio_blob_path, sample_rate, now),
            )
            return int(cur.lastrowid or 0)

        return await self._run_write(_do)

    async def list_messages(
        self,
        thread_id: str,
        *,
        after_seq: int = 0,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> list[VoiceMessage]:
        await self._ensure_schema()
        async with self._conn() as conn:
            cur = await conn.execute(
                "SELECT seq, thread_id, role, modality, text, audio_blob_path, "
                "sample_rate, created_ts FROM voice_messages "
                "WHERE thread_id = ? AND seq > ? ORDER BY seq ASC LIMIT ?",
                (thread_id, after_seq, limit),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [_row_to_msg(r) for r in rows]

    async def get_message(self, thread_id: str, seq: int) -> VoiceMessage | None:
        await self._ensure_schema()
        async with self._conn() as conn:
            cur = await conn.execute(
                "SELECT seq, thread_id, role, modality, text, audio_blob_path, "
                "sample_rate, created_ts FROM voice_messages "
                "WHERE thread_id = ? AND seq = ?",
                (thread_id, seq),
            )
            row = await cur.fetchone()
            await cur.close()
        return _row_to_msg(row) if row else None

    async def delete_for_thread(self, thread_id: str) -> int:
        async def _do(conn):
            cur = await conn.execute(
                "DELETE FROM voice_messages WHERE thread_id = ?", (thread_id,)
            )
            return cur.rowcount or 0

        return await self._run_write(_do)


def _row_to_msg(r) -> VoiceMessage:
    return VoiceMessage(
        seq=r["seq"],
        thread_id=r["thread_id"],
        role=r["role"],
        modality=r["modality"],
        text=r["text"] or "",
        audio_blob_path=r["audio_blob_path"],
        sample_rate=r["sample_rate"],
        created_ts=r["created_ts"],
    )


class PgVoiceMessageStore(VoiceMessageStore):
    """``voice_messages`` table in Postgres (schema owned by pg/migrations.py v11)."""

    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

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
        async with self._pool.connection() as conn:
            await ensure_thread(conn, thread_id)  # satisfy voice_messages_thread_fk
            cur = await conn.execute(
                "INSERT INTO voice_messages "
                "(thread_id, role, modality, text, audio_blob_path, sample_rate) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING seq",
                (thread_id, role, modality, text or "", audio_blob_path, sample_rate),
            )
            row = await cur.fetchone()
        return int(row[0])

    async def list_messages(
        self,
        thread_id: str,
        *,
        after_seq: int = 0,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> list[VoiceMessage]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT seq, thread_id, role, modality, text, audio_blob_path, "
                "sample_rate, extract(epoch FROM created_ts) FROM voice_messages "
                "WHERE thread_id = %s AND seq > %s ORDER BY seq ASC LIMIT %s",
                (thread_id, after_seq, limit),
            )
            rows = await cur.fetchall()
        return [_pg_row_to_msg(r) for r in rows]

    async def get_message(self, thread_id: str, seq: int) -> VoiceMessage | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT seq, thread_id, role, modality, text, audio_blob_path, "
                "sample_rate, extract(epoch FROM created_ts) FROM voice_messages "
                "WHERE thread_id = %s AND seq = %s",
                (thread_id, seq),
            )
            row = await cur.fetchone()
        return _pg_row_to_msg(row) if row else None

    async def delete_for_thread(self, thread_id: str) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM voice_messages WHERE thread_id = %s", (thread_id,)
            )
            return cur.rowcount or 0


def _pg_row_to_msg(r) -> VoiceMessage:
    return VoiceMessage(
        seq=int(r[0]),
        thread_id=r[1],
        role=r[2],
        modality=r[3],
        text=r[4] or "",
        audio_blob_path=r[5],
        sample_rate=r[6],
        created_ts=float(r[7]),
    )
