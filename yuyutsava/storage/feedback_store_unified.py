"""One ``message_feedback`` implementation, both backends.

Phase 2 step 2.5b (ADR-002), playbook order 11. Replaces
``SqliteFeedbackStore`` and ``PgFeedbackStore`` — 192 lines that agreed on
almost everything and disagreed on two things that mattered.

**Fixed: the returned record did not match the stored row.** ``upsert`` builds a
``MessageFeedback`` with ``created_ts=time.time()`` and hands it back to the
caller. The SQLite twin wrote that value. The Postgres twin **left the column
out** of its INSERT and let ``DEFAULT now()`` fire — the database server's clock.
So on Postgres the caller received a timestamp that was never persisted, and the
two disagreed forever with nothing raising. Same root cause as finding AE
(transcripts), but observable within a single call rather than only through the
sweeper. The column is now written explicitly on both.

**Fixed: re-rating was DELETE-then-INSERT.** Two statements, correct only
because both twins wrapped them in a transaction — and the Postgres one did not
until Phase 2 made ``PgPool.transaction`` available, which was one of the six
pre-existing bugs that work surfaced. Both backends carry a unique index on
``(thread_id, message_ref)``, so a single ``ON CONFLICT ... DO UPDATE`` does the
same job with no window in which the row is absent and no dependence on the
surrounding transaction.

Re-rating deliberately mints a **new** ``feedback_id`` and ``created_ts``: a
changed verdict is a new judgement, not an edit of the old one, and the upsert
overwrites both from ``EXCLUDED`` to preserve that.

**Rows are read by name.** The Postgres twin's ``_pg_row_to_rec`` indexed
positionally (``r[0]`` … ``r[9]``) — the third domain in a row where that
pattern blocked reuse under the dialect's ``dict_row`` connection (see findings
AF and AG). Named access is backend-neutral.

Parity verified on both live backends by
``test/storage/test_feedback_store_parity.py``.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, ClassVar

from ulid import ULID

from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.dialect import Dialect
from yuyutsava.storage.feedback_store import (
    DEFAULT_LIST_LIMIT,
    FeedbackStore,
    MessageFeedback,
    _validate,
)

logger = logging.getLogger("yuyutsava.storage.feedback_store_unified")

#: Fixed read set. Access below is by name, so this drives *which* columns are
#: fetched, not their positions.
_COLS: tuple[str, ...] = (
    "feedback_id", "thread_id", "session_id", "workspace", "message_ref",
    "rating", "note", "user_text", "assistant_text",
)


class FeedbackSchema(BaseSqliteStore):
    """SQLite DDL owner. Byte-identical to the retired twin's ``_SCHEMA_SQL``."""

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


def _to_rec(row: Any) -> MessageFeedback:
    """Map a row by name — see the module docstring on positional access."""
    return MessageFeedback(
        feedback_id=row["feedback_id"], thread_id=row["thread_id"],
        session_id=row["session_id"], workspace=row["workspace"],
        message_ref=row["message_ref"], rating=row["rating"], note=row["note"],
        user_text=row["user_text"] or "",
        assistant_text=row["assistant_text"] or "",
        created_ts=float(row["created_ts"]),
    )


class UnifiedFeedbackStore(FeedbackStore):
    """``message_feedback`` — one verdict per (thread, message)."""

    def __init__(self, dialect: Dialect) -> None:
        self._d = dialect

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
        d = self._d
        rec = MessageFeedback(
            feedback_id=f"fb_{ULID()}",
            thread_id=thread_id,
            session_id=session_id,
            workspace=str(workspace) if workspace is not None else None,
            message_ref=message_ref,
            rating=rating,
            note=note,
            user_text=user_text or "",
            assistant_text=assistant_text or "",
            created_ts=time.time(),
        )

        async def _do(conn):
            # One statement, not DELETE-then-INSERT: there is no instant in
            # which the message has no rating, and correctness no longer rests
            # on the caller's transaction.
            #
            # feedback_id and created_ts are overwritten from EXCLUDED because a
            # re-rating is a NEW judgement — the caller is handed `rec`, and the
            # stored row must be exactly it.
            await conn.execute(
                f"INSERT INTO message_feedback "
                f"({', '.join(_COLS)}, created_ts) "
                f"VALUES ({d.ph(len(_COLS))}, {d.ts_param()}) "
                f"ON CONFLICT (thread_id, message_ref) DO UPDATE SET "
                f"feedback_id = EXCLUDED.feedback_id, "
                f"session_id = EXCLUDED.session_id, "
                f"workspace = EXCLUDED.workspace, "
                f"rating = EXCLUDED.rating, "
                f"note = EXCLUDED.note, "
                f"user_text = EXCLUDED.user_text, "
                f"assistant_text = EXCLUDED.assistant_text, "
                f"created_ts = EXCLUDED.created_ts",
                (*(getattr(rec, c) for c in _COLS), rec.created_ts),
            )

        await d.write(_do)
        return rec

    async def list_for_thread(
        self, thread_id: str, *, limit: int = DEFAULT_LIST_LIMIT
    ) -> list[MessageFeedback]:
        d = self._d
        async with d.reading() as conn:
            cur = await conn.execute(
                f"SELECT {', '.join(_COLS)}, {d.epoch('created_ts')} "
                f"FROM message_feedback WHERE thread_id = {d.ph()} "
                f"ORDER BY created_ts DESC LIMIT {d.ph()}",
                (thread_id, limit),
            )
            rows = await cur.fetchall()
        return [_to_rec(r) for r in rows]

    async def list_all(self, *, limit: int = DEFAULT_LIST_LIMIT) -> list[MessageFeedback]:
        d = self._d
        async with d.reading() as conn:
            cur = await conn.execute(
                f"SELECT {', '.join(_COLS)}, {d.epoch('created_ts')} "
                f"FROM message_feedback ORDER BY created_ts DESC LIMIT {d.ph()}",
                (limit,),
            )
            rows = await cur.fetchall()
        return [_to_rec(r) for r in rows]

    async def delete_for_thread(self, thread_id: str) -> int:
        """Remove every rating for a thread. Returns rows deleted.

        Required by session deletion: these rows store ``user_text`` and
        ``assistant_text`` verbatim, so leaving them behind keeps the
        conversation content of a session the user asked to delete.
        """
        d = self._d

        async def _do(conn):
            cur = await conn.execute(
                f"DELETE FROM message_feedback WHERE thread_id = {d.ph()}",
                (thread_id,),
            )
            return cur.rowcount or 0

        return await d.write(_do)


def sqlite_feedback_store(db_path: Path | None = None) -> UnifiedFeedbackStore:
    from yuyutsava.storage.dialect import SqliteDialect
    from yuyutsava.storage.paths import state_db_path

    return UnifiedFeedbackStore(
        SqliteDialect(FeedbackSchema(db_path or state_db_path()))
    )


def pg_feedback_store(pool) -> UnifiedFeedbackStore:
    from yuyutsava.storage.dialect import PostgresDialect

    return UnifiedFeedbackStore(PostgresDialect(pool))


__all__ = [
    "FeedbackSchema",
    "UnifiedFeedbackStore",
    "pg_feedback_store",
    "sqlite_feedback_store",
]
