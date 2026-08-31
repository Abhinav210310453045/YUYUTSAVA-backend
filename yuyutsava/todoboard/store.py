"""Persistence twins for the TODO board (``todo_cards`` / ``todo_notes`` /
``todo_attachments``).

Mirrors the ``feedback_store.py`` / ``visuals/store.py`` shape: a ``TodoStore``
ABC with a Postgres primary (schema owned by pg/migrations v16) and a SQLite
twin inside ``state.db`` (zero-config fallback AND the spillover buffer). The
SQLite twin keeps the exact PG table/column names so the Reconciler's
``TableSpec`` drain can replay buffered rows verbatim — which is also why
``pinned`` is INTEGER 0/1 on both sides (no bool cast on drain) and timestamps
are epoch REAL in SQLite / TIMESTAMPTZ in PG (``ts_cols`` wraps them in
``to_timestamp`` on drain).

Board data is durable user data: NO thread FK, never listed in
``purge_session``, no TTL sweep. Rows in/out are the exchange models
(:mod:`yuyutsava.todoboard.models`) directly — validation and blob handling
live one layer up in :mod:`yuyutsava.todoboard.exchange`, never here.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any, ClassVar

from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.pg.pool import PgPool
from yuyutsava.todoboard.models import (
    TodoAttachmentV1,
    TodoCardSummaryV1,
    TodoCardV1,
    TodoEventV1,
    TodoNoteV1,
    TodoObjectiveV1,
)

logger = logging.getLogger("yuyutsava.todoboard.store")

DEFAULT_LIST_LIMIT = 500

# Card fields update_card() may touch; the exchange validates values before
# the store ever sees them.
_CARD_UPDATE_FIELDS = ("title", "status", "pinned", "tags", "updated_ts")
_OBJECTIVE_UPDATE_FIELDS = ("title", "phase", "order_idx", "reason", "outcome", "updated_ts")


class TodoStore(ABC):
    """Interface the exchange layer depends on. All child mutations bump the
    parent card's ``updated_ts`` so board listings sort by real activity."""

    @abstractmethod
    async def add_card(self, card: TodoCardV1) -> None: ...

    @abstractmethod
    async def get_card(self, card_id: str) -> TodoCardV1 | None:
        """One card hydrated with its notes + attachments (oldest first)."""

    @abstractmethod
    async def update_card(self, card_id: str, fields: dict[str, Any]) -> bool:
        """Patch card columns; returns False when the id is unknown."""

    @abstractmethod
    async def delete_card(self, card_id: str) -> bool:
        """Drop the card and its children. Returns False when unknown.
        Blob-dir cleanup is the exchange's job (it knows workspace_path)."""

    @abstractmethod
    async def list_cards(
        self, *, status: str | None = None, limit: int = DEFAULT_LIST_LIMIT
    ) -> list[TodoCardSummaryV1]:
        """Summaries, pinned first then most recently updated."""

    @abstractmethod
    async def list_card_ids(self) -> list[str]:
        """All card ids, no limit — the orphan-dir sweep's ground truth."""

    @abstractmethod
    async def add_objective(self, obj: TodoObjectiveV1) -> bool:
        """Insert an objective; returns False when its card doesn't exist."""

    @abstractmethod
    async def get_objective(self, objective_id: str) -> TodoObjectiveV1 | None: ...

    @abstractmethod
    async def update_objective(
        self, objective_id: str, fields: dict[str, Any]
    ) -> TodoObjectiveV1 | None:
        """Patch objective columns; returns the updated row (None = unknown)."""

    @abstractmethod
    async def delete_objective(self, objective_id: str) -> TodoObjectiveV1 | None:
        """Drop one objective, demoting its notes to card-level (objective_id
        NULLed). Returns the deleted row so the exchange can log the event."""

    @abstractmethod
    async def assign_note(
        self,
        note_id: str,
        objective_id: str | None,
        phase: str | None,
        updated_ts: float,
    ) -> TodoNoteV1 | None:
        """Set (or clear) a note's objective/phase assignment."""

    @abstractmethod
    async def add_event(self, ev: TodoEventV1) -> None:
        """Append one timeline event. Does NOT bump the card's updated_ts —
        the mutation being described already did."""

    @abstractmethod
    async def list_events(self, card_id: str, *, limit: int = 500) -> list[TodoEventV1]:
        """A card's activity timeline, oldest first."""

    @abstractmethod
    async def add_note(self, note: TodoNoteV1) -> bool:
        """Insert a note; returns False when its card doesn't exist."""

    @abstractmethod
    async def update_note(self, note_id: str, body: str, updated_ts: float) -> TodoNoteV1 | None: ...

    @abstractmethod
    async def delete_note(self, note_id: str) -> bool: ...

    @abstractmethod
    async def add_attachment(self, att: TodoAttachmentV1) -> bool:
        """Insert an attachment; returns False when its card doesn't exist."""

    @abstractmethod
    async def update_attachment(self, att: TodoAttachmentV1) -> bool:
        """Overwrite an existing attachment row (singleton-block regeneration);
        returns False when no row has this attachment_id. Bumps the card's
        updated_ts like add_attachment."""

    @abstractmethod
    async def delete_attachment(self, attachment_id: str) -> TodoAttachmentV1 | None:
        """Drop one attachment row; returns it (the exchange unlinks the file)."""

    @abstractmethod
    async def list_all_attachments(
        self, *, limit: int = DEFAULT_LIST_LIMIT
    ) -> list[TodoAttachmentV1]:
        """Every card's attachments, newest first — the global gallery feed."""


def _tags_json(tags: list[str]) -> str:
    return json.dumps(list(tags or []))


def _load_json(raw: Any, fallback: Any) -> Any:
    if raw is None:
        return fallback
    if isinstance(raw, (dict, list)):
        return raw  # psycopg already decoded jsonb
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback



# NOTE: SqliteTodoStore was replaced on 2026-08-09 by UnifiedTodoStore in
# todoboard/store_unified.py (ADR-002 step 2.5b) — the last twin pair. Child
# deletion is now explicit on both backends rather than cascade-on-PG /
# by-hand-on-SQLite. Parity verified against BOTH retired twins and then the
# unified store, on both live backends, in test/storage/test_todo_store_parity.py.
















# NOTE: PgTodoStore was replaced on 2026-08-09 by UnifiedTodoStore in
# todoboard/store_unified.py (ADR-002 step 2.5b) — the last twin pair. Child
# deletion is now explicit on both backends rather than cascade-on-PG /
# by-hand-on-SQLite. Parity verified against BOTH retired twins and then the
# unified store, on both live backends, in test/storage/test_todo_store_parity.py.













# Process-singleton, mirroring get/set_default_feedback_store(). Postgres is
# primary: the daemon injects a RoutedStore(Pg, Sqlite) at boot and the CLI a
# plain PgTodoStore when it owns a pool; otherwise this lazily builds the
# SQLite fallback.
_default_store: TodoStore | None = None


def set_default_todo_store(store: TodoStore) -> None:
    global _default_store
    _default_store = store


def get_default_todo_store() -> TodoStore:
    global _default_store
    if _default_store is None:
        from yuyutsava.storage.paths import state_db_path

        from yuyutsava.todoboard.store_unified import sqlite_todo_store

        _default_store = sqlite_todo_store()
    return _default_store


__all__ = [
    "TodoStore",
    "SqliteTodoStore",
    "PgTodoStore",
    "get_default_todo_store",
    "set_default_todo_store",
    "DEFAULT_LIST_LIMIT",
]
