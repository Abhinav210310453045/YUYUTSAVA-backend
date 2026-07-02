"""Message-feedback store: user 👍/👎 reactions on assistant turns.

When the user reacts to a message in the Chat/Voice UI, we persist the
(user prompt, assistant reply) pair plus the rating. This is **durable insight
data**, not conversation state: a future feedback agent mines it to derive what
works and to tune prompts. Two consequences shape the schema:

  * the user + assistant text are **snapshotted** into the row (not referenced),
    so a feedback record is self-contained and stays meaningful even after the
    session — and its transcript — are deleted;
  * feedback is therefore **retained across session deletion** (it is NOT listed
    in ``storage/purge.py``'s ``_STATE_TABLES``), the same way durable
    fact/preference memories survive a purge.

Keyed by ``(thread_id, message_ref)`` with an upsert so re-rating a message
replaces the prior rating rather than piling up rows.

SQLite-only for now (the CLI/daemon share ``state.db`` via WAL); a Postgres
twin can follow the ``voice_store.py`` pattern if/when needed.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from ulid import ULID

from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.pg.pool import PgPool

logger = logging.getLogger("yuyutsava.storage.feedback_store")

DEFAULT_LIST_LIMIT = 1_000
RATINGS = ("up", "down")


@dataclass(frozen=True)
class MessageFeedback:
    """One persisted feedback record (self-contained snapshot)."""

    feedback_id: str
    thread_id: str
    session_id: str
    workspace: str | None
    message_ref: str          # client message id / turn ref this rating targets
    rating: str               # "up" | "down"
    note: str | None
    user_text: str            # snapshot of the prompting user turn
    assistant_text: str       # snapshot of the rated assistant turn
    created_ts: float


class FeedbackStore(ABC):
    """Interface the REST layer + future feedback agent depend on."""

    @abstractmethod
    async def upsert(
        self,
        *,
        thread_id: str,
        session_id: str,
        message_ref: str,
        rating: str,
        user_text: str,
        assistant_text: str,
        workspace: str | None = None,
        note: str | None = None,
    ) -> MessageFeedback:
        """Record (or replace) the rating for one message. Returns the row."""

    @abstractmethod
    async def list_for_thread(
        self, thread_id: str, *, limit: int = DEFAULT_LIST_LIMIT
    ) -> list[MessageFeedback]:
        """Feedback for one thread, newest first."""

    @abstractmethod
    async def list_all(self, *, limit: int = DEFAULT_LIST_LIMIT) -> list[MessageFeedback]:
        """All feedback newest first — the corpus a feedback agent mines."""


def _validate(rating: str) -> None:
    if rating not in RATINGS:
        raise ValueError(f"feedback rating must be one of {RATINGS}, got {rating!r}")


class SqliteFeedbackStore(BaseSqliteStore, FeedbackStore):
    """``message_feedback`` table inside ``state.db`` (zero-config)."""

    _SCHEMA_VERSION: ClassVar[int] = 1
    _META_TABLE: ClassVar[str] = "message_feedback_meta"
    _SCHEMA_SQL: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS message_feedback_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS message_feedback (
            feedback_id    TEXT PRIMARY KEY,
            thread_id      TEXT NOT NULL,
            session_id     TEXT NOT NULL,
            workspace      TEXT,
            message_ref    TEXT NOT NULL,
            rating         TEXT NOT NULL,
            note           TEXT,
            user_text      TEXT NOT NULL DEFAULT '',
            assistant_text TEXT NOT NULL DEFAULT '',
            created_ts     REAL NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS message_feedback_target_idx
            ON message_feedback (thread_id, message_ref);
        CREATE INDEX IF NOT EXISTS message_feedback_recent_idx
            ON message_feedback (created_ts);
    """

    async def upsert(
        self,
        *,
        thread_id: str,
        session_id: str,
        message_ref: str,
        rating: str,
        user_text: str,
        assistant_text: str,
        workspace: str | None = None,
        note: str | None = None,
    ) -> MessageFeedback:
        _validate(rating)
        workspace = str(workspace) if workspace is not None else None
        rec = MessageFeedback(
            feedback_id=f"fb_{ULID()}",
            thread_id=thread_id,
            session_id=session_id,
            workspace=workspace,
            message_ref=message_ref,
            rating=rating,
            note=note,
            user_text=user_text or "",
            assistant_text=assistant_text or "",
            created_ts=time.time(),
        )

        async def _do(conn):
            # Re-rating a message replaces the prior row (unique target index).
            # ON CONFLICT keeps the original feedback_id/created_ts stable would
            # require RETURNING; simplest correct behavior is delete-then-insert.
            await conn.execute(
                "DELETE FROM message_feedback WHERE thread_id = ? AND message_ref = ?",
                (thread_id, message_ref),
            )
            await conn.execute(
                "INSERT INTO message_feedback (feedback_id, thread_id, session_id, "
                "workspace, message_ref, rating, note, user_text, assistant_text, created_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rec.feedback_id, rec.thread_id, rec.session_id, rec.workspace,
                 rec.message_ref, rec.rating, rec.note, rec.user_text,
                 rec.assistant_text, rec.created_ts),
            )

        await self._run_write(_do)
        return rec

    async def list_for_thread(
        self, thread_id: str, *, limit: int = DEFAULT_LIST_LIMIT
    ) -> list[MessageFeedback]:
        await self._ensure_schema()
        async with self._conn() as conn:
            cur = await conn.execute(
                "SELECT * FROM message_feedback WHERE thread_id = ? "
                "ORDER BY created_ts DESC LIMIT ?",
                (thread_id, limit),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [_row_to_rec(r) for r in rows]

    async def list_all(self, *, limit: int = DEFAULT_LIST_LIMIT) -> list[MessageFeedback]:
        await self._ensure_schema()
        async with self._conn() as conn:
            cur = await conn.execute(
                "SELECT * FROM message_feedback ORDER BY created_ts DESC LIMIT ?",
                (limit,),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [_row_to_rec(r) for r in rows]


def _row_to_rec(r) -> MessageFeedback:
    return MessageFeedback(
        feedback_id=r["feedback_id"],
        thread_id=r["thread_id"],
        session_id=r["session_id"],
        workspace=r["workspace"],
        message_ref=r["message_ref"],
        rating=r["rating"],
        note=r["note"],
        user_text=r["user_text"] or "",
        assistant_text=r["assistant_text"] or "",
        created_ts=r["created_ts"],
    )


class PgFeedbackStore(FeedbackStore):
    """``message_feedback`` table in Postgres (schema owned by pg/migrations v15).

    Postgres is primary on the ``postgres`` backend. No thread FK — feedback
    survives session deletion by design (durable insight data). Mirrors the
    dual-backend shape of the voice store.
    """

    def __init__(self, pool: "PgPool") -> None:
        self._pool = pool

    async def upsert(
        self,
        *,
        thread_id: str,
        session_id: str,
        message_ref: str,
        rating: str,
        user_text: str,
        assistant_text: str,
        workspace: str | None = None,
        note: str | None = None,
    ) -> MessageFeedback:
        _validate(rating)
        workspace = str(workspace) if workspace is not None else None
        rec = MessageFeedback(
            feedback_id=f"fb_{ULID()}",
            thread_id=thread_id,
            session_id=session_id,
            workspace=workspace,
            message_ref=message_ref,
            rating=rating,
            note=note,
            user_text=user_text or "",
            assistant_text=assistant_text or "",
            created_ts=time.time(),
        )
        async with self._pool.connection() as conn:
            # Re-rating replaces the prior row (unique on thread_id, message_ref).
            await conn.execute(
                "DELETE FROM message_feedback WHERE thread_id = %s AND message_ref = %s",
                (thread_id, message_ref),
            )
            await conn.execute(
                "INSERT INTO message_feedback (feedback_id, thread_id, session_id, "
                "workspace, message_ref, rating, note, user_text, assistant_text) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (rec.feedback_id, rec.thread_id, rec.session_id, rec.workspace,
                 rec.message_ref, rec.rating, rec.note, rec.user_text, rec.assistant_text),
            )
        return rec

    async def list_for_thread(
        self, thread_id: str, *, limit: int = DEFAULT_LIST_LIMIT
    ) -> list[MessageFeedback]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT feedback_id, thread_id, session_id, workspace, message_ref, "
                "rating, note, user_text, assistant_text, extract(epoch FROM created_ts) "
                "FROM message_feedback WHERE thread_id = %s ORDER BY created_ts DESC LIMIT %s",
                (thread_id, limit),
            )
            rows = await cur.fetchall()
        return [_pg_row_to_rec(r) for r in rows]

    async def list_all(self, *, limit: int = DEFAULT_LIST_LIMIT) -> list[MessageFeedback]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT feedback_id, thread_id, session_id, workspace, message_ref, "
                "rating, note, user_text, assistant_text, extract(epoch FROM created_ts) "
                "FROM message_feedback ORDER BY created_ts DESC LIMIT %s",
                (limit,),
            )
            rows = await cur.fetchall()
        return [_pg_row_to_rec(r) for r in rows]


def _pg_row_to_rec(r) -> MessageFeedback:
    return MessageFeedback(
        feedback_id=r[0], thread_id=r[1], session_id=r[2], workspace=r[3],
        message_ref=r[4], rating=r[5], note=r[6], user_text=r[7] or "",
        assistant_text=r[8] or "", created_ts=float(r[9]),
    )


# Process-singleton, mirroring get/set_default_session_store(). Postgres is
# primary: the daemon injects a PgFeedbackStore (sharing its pool) at boot;
# otherwise this lazily builds the SQLite fallback.
_default_store: FeedbackStore | None = None


def set_default_feedback_store(store: FeedbackStore) -> None:
    global _default_store
    _default_store = store


def get_default_feedback_store() -> FeedbackStore:
    global _default_store
    if _default_store is None:
        from yuyutsava.storage.paths import state_db_path

        _default_store = SqliteFeedbackStore(state_db_path())
    return _default_store
