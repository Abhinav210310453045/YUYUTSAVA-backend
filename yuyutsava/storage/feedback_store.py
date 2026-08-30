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

    @abstractmethod
    async def delete_for_thread(self, thread_id: str) -> int:
        """Drop every feedback row for a thread. Returns rows deleted.

        Required by session deletion: a feedback row stores ``user_text`` and
        ``assistant_text`` verbatim, so leaving it behind would keep the
        conversation content of a session the user asked to delete.
        """


def _validate(rating: str) -> None:
    if rating not in RATINGS:
        raise ValueError(f"feedback rating must be one of {RATINGS}, got {rating!r}")



# NOTE: SqliteFeedbackStore was replaced on 2026-08-09 by UnifiedFeedbackStore in
# storage/feedback_store_unified.py (ADR-002 step 2.5b). Parity verified on both
# live backends in test/storage/test_feedback_store_parity.py.



# NOTE: PgFeedbackStore was replaced on 2026-08-09 by UnifiedFeedbackStore in
# storage/feedback_store_unified.py (ADR-002 step 2.5b). Parity verified on both
# live backends in test/storage/test_feedback_store_parity.py.



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
        from yuyutsava.storage.feedback_store_unified import sqlite_feedback_store

        _default_store = sqlite_feedback_store()
    return _default_store
