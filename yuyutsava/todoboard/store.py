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


class SqliteTodoStore(BaseSqliteStore, TodoStore):
    """TODO tables inside ``state.db`` — zero-config primary on the SQLite
    backend, spillover buffer on the Postgres backend."""

    _SCHEMA_VERSION: ClassVar[int] = 2
    _META_TABLE: ClassVar[str] = "todo_board_meta"
    _SCHEMA_SQL: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS todo_board_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS todo_cards (
            card_id        TEXT PRIMARY KEY,
            title          TEXT NOT NULL,
            status         TEXT NOT NULL DEFAULT 'inbox',
            pinned         INTEGER NOT NULL DEFAULT 0,
            tags           TEXT NOT NULL DEFAULT '[]',
            workspace_path TEXT,
            created_ts     REAL NOT NULL,
            updated_ts     REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS todo_cards_status_idx
            ON todo_cards (status, updated_ts DESC);
        CREATE TABLE IF NOT EXISTS todo_notes (
            note_id      TEXT PRIMARY KEY,
            card_id      TEXT NOT NULL,
            body         TEXT NOT NULL,
            author       TEXT NOT NULL DEFAULT 'user',
            objective_id TEXT,
            phase        TEXT,
            created_ts   REAL NOT NULL,
            updated_ts   REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS todo_notes_card_idx
            ON todo_notes (card_id, created_ts);
        CREATE TABLE IF NOT EXISTS todo_attachments (
            attachment_id TEXT PRIMARY KEY,
            card_id       TEXT NOT NULL,
            kind          TEXT NOT NULL,
            path          TEXT,
            url           TEXT,
            mime          TEXT,
            title         TEXT,
            meta          TEXT NOT NULL DEFAULT '{}',
            created_ts    REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS todo_attachments_card_idx
            ON todo_attachments (card_id, created_ts);
        CREATE TABLE IF NOT EXISTS todo_objectives (
            objective_id TEXT PRIMARY KEY,
            card_id      TEXT NOT NULL,
            title        TEXT NOT NULL,
            phase        TEXT NOT NULL DEFAULT 'thinking',
            order_idx    INTEGER NOT NULL DEFAULT 0,
            reason       TEXT,
            outcome      TEXT,
            created_ts   REAL NOT NULL,
            updated_ts   REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS todo_objectives_card_idx
            ON todo_objectives (card_id, order_idx, created_ts);
        CREATE TABLE IF NOT EXISTS todo_events (
            event_id     TEXT PRIMARY KEY,
            card_id      TEXT NOT NULL,
            objective_id TEXT,
            kind         TEXT NOT NULL,
            payload      TEXT NOT NULL DEFAULT '{}',
            actor        TEXT NOT NULL DEFAULT 'user',
            created_ts   REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS todo_events_card_idx
            ON todo_events (card_id, created_ts);
    """

    async def _migrate(self, conn) -> None:
        # v1 → v2: think-flow columns on todo_notes (fresh installs get them
        # via the CREATE above; the new tables are covered by IF NOT EXISTS).
        # ALTER ADD COLUMN isn't idempotent in SQLite, so guard with
        # PRAGMA table_info — a crash between ALTER and the version bump must
        # stay re-runnable.
        cur = await conn.execute(
            f"SELECT value FROM {self._META_TABLE} WHERE key=?",
            (self._META_VERSION_KEY,),
        )
        row = await cur.fetchone()
        await cur.close()
        current = int(row[0]) if row else 0
        if current < 2:
            cur = await conn.execute("PRAGMA table_info(todo_notes)")
            cols = {r[1] for r in await cur.fetchall()}
            await cur.close()
            if "objective_id" not in cols:
                await conn.execute("ALTER TABLE todo_notes ADD COLUMN objective_id TEXT")
            if "phase" not in cols:
                await conn.execute("ALTER TABLE todo_notes ADD COLUMN phase TEXT")
        await super()._migrate(conn)

    async def add_card(self, card: TodoCardV1) -> None:
        async def _do(conn):
            await conn.execute(
                "INSERT INTO todo_cards (card_id, title, status, pinned, tags, "
                "workspace_path, created_ts, updated_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (card.card_id, card.title, card.status, int(card.pinned),
                 _tags_json(card.tags), card.workspace_path,
                 card.created_ts, card.updated_ts),
            )

        await self._run_write(_do)

    async def get_card(self, card_id: str) -> TodoCardV1 | None:
        await self._ensure_schema()
        async with self._conn() as conn:
            cur = await conn.execute(
                "SELECT * FROM todo_cards WHERE card_id = ?", (card_id,)
            )
            row = await cur.fetchone()
            await cur.close()
            if row is None:
                return None
            cur = await conn.execute(
                "SELECT * FROM todo_objectives WHERE card_id = ? "
                "ORDER BY order_idx, created_ts",
                (card_id,),
            )
            objs = await cur.fetchall()
            await cur.close()
            cur = await conn.execute(
                "SELECT * FROM todo_notes WHERE card_id = ? ORDER BY created_ts",
                (card_id,),
            )
            notes = await cur.fetchall()
            await cur.close()
            cur = await conn.execute(
                "SELECT * FROM todo_attachments WHERE card_id = ? ORDER BY created_ts",
                (card_id,),
            )
            atts = await cur.fetchall()
            await cur.close()
        return _sqlite_card(row, objs, notes, atts)

    async def update_card(self, card_id: str, fields: dict[str, Any]) -> bool:
        cols = {k: v for k, v in fields.items() if k in _CARD_UPDATE_FIELDS}
        if not cols:
            return True
        if "pinned" in cols:
            cols["pinned"] = int(bool(cols["pinned"]))
        if "tags" in cols:
            cols["tags"] = _tags_json(cols["tags"])

        async def _do(conn):
            sets = ", ".join(f"{k} = ?" for k in cols)
            cur = await conn.execute(
                f"UPDATE todo_cards SET {sets} WHERE card_id = ?",
                (*cols.values(), card_id),
            )
            return (cur.rowcount or 0) > 0

        return await self._run_write(_do)

    async def delete_card(self, card_id: str) -> bool:
        # No PRAGMA foreign_keys in the shared connection setup, so cascade by
        # hand — all child deletes in one write transaction.
        async def _do(conn):
            await conn.execute("DELETE FROM todo_notes WHERE card_id = ?", (card_id,))
            await conn.execute("DELETE FROM todo_attachments WHERE card_id = ?", (card_id,))
            await conn.execute("DELETE FROM todo_objectives WHERE card_id = ?", (card_id,))
            await conn.execute("DELETE FROM todo_events WHERE card_id = ?", (card_id,))
            cur = await conn.execute("DELETE FROM todo_cards WHERE card_id = ?", (card_id,))
            return (cur.rowcount or 0) > 0

        return await self._run_write(_do)

    async def list_cards(
        self, *, status: str | None = None, limit: int = DEFAULT_LIST_LIMIT
    ) -> list[TodoCardSummaryV1]:
        await self._ensure_schema()
        where = "WHERE c.status = ?" if status else ""
        params: tuple = (status, limit) if status else (limit,)
        async with self._conn() as conn:
            cur = await conn.execute(
                "SELECT c.*, "
                "(SELECT COUNT(*) FROM todo_notes n WHERE n.card_id = c.card_id) AS note_count, "
                "(SELECT COUNT(*) FROM todo_attachments a WHERE a.card_id = c.card_id) AS attachment_count, "
                "(SELECT COUNT(*) FROM todo_objectives o WHERE o.card_id = c.card_id) AS objective_count, "
                "(SELECT COUNT(*) FROM todo_objectives o WHERE o.card_id = c.card_id "
                " AND o.phase = 'completed') AS objective_done_count "
                f"FROM todo_cards c {where} "
                "ORDER BY c.pinned DESC, c.updated_ts DESC LIMIT ?",
                params,
            )
            rows = await cur.fetchall()
            await cur.close()
        return [_sqlite_summary(r) for r in rows]

    async def list_card_ids(self) -> list[str]:
        await self._ensure_schema()
        async with self._conn() as conn:
            cur = await conn.execute("SELECT card_id FROM todo_cards")
            rows = await cur.fetchall()
            await cur.close()
        return [r["card_id"] for r in rows]

    async def add_note(self, note: TodoNoteV1) -> bool:
        async def _do(conn):
            cur = await conn.execute(
                "SELECT 1 FROM todo_cards WHERE card_id = ?", (note.card_id,)
            )
            if await cur.fetchone() is None:
                return False
            await conn.execute(
                "INSERT INTO todo_notes (note_id, card_id, body, author, "
                "objective_id, phase, created_ts, updated_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (note.note_id, note.card_id, note.body, note.author,
                 note.objective_id, note.phase,
                 note.created_ts, note.updated_ts),
            )
            await conn.execute(
                "UPDATE todo_cards SET updated_ts = ? WHERE card_id = ?",
                (note.updated_ts, note.card_id),
            )
            return True

        return await self._run_write(_do)

    async def add_objective(self, obj: TodoObjectiveV1) -> bool:
        async def _do(conn):
            cur = await conn.execute(
                "SELECT 1 FROM todo_cards WHERE card_id = ?", (obj.card_id,)
            )
            if await cur.fetchone() is None:
                return False
            await conn.execute(
                "INSERT INTO todo_objectives (objective_id, card_id, title, phase, "
                "order_idx, reason, outcome, created_ts, updated_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (obj.objective_id, obj.card_id, obj.title, obj.phase,
                 obj.order_idx, obj.reason, obj.outcome,
                 obj.created_ts, obj.updated_ts),
            )
            await conn.execute(
                "UPDATE todo_cards SET updated_ts = ? WHERE card_id = ?",
                (obj.updated_ts, obj.card_id),
            )
            return True

        return await self._run_write(_do)

    async def get_objective(self, objective_id: str) -> TodoObjectiveV1 | None:
        await self._ensure_schema()
        async with self._conn() as conn:
            cur = await conn.execute(
                "SELECT * FROM todo_objectives WHERE objective_id = ?",
                (objective_id,),
            )
            row = await cur.fetchone()
            await cur.close()
        return _sqlite_objective(row) if row else None

    async def update_objective(
        self, objective_id: str, fields: dict[str, Any]
    ) -> TodoObjectiveV1 | None:
        cols = {k: v for k, v in fields.items() if k in _OBJECTIVE_UPDATE_FIELDS}
        if not cols:
            return await self.get_objective(objective_id)

        async def _do(conn):
            sets = ", ".join(f"{k} = ?" for k in cols)
            cur = await conn.execute(
                f"UPDATE todo_objectives SET {sets} WHERE objective_id = ?",
                (*cols.values(), objective_id),
            )
            if (cur.rowcount or 0) == 0:
                return None
            cur = await conn.execute(
                "SELECT * FROM todo_objectives WHERE objective_id = ?",
                (objective_id,),
            )
            row = await cur.fetchone()
            if row is not None and "updated_ts" in cols:
                await conn.execute(
                    "UPDATE todo_cards SET updated_ts = ? WHERE card_id = ?",
                    (cols["updated_ts"], row["card_id"]),
                )
            return _sqlite_objective(row) if row else None

        return await self._run_write(_do)

    async def delete_objective(self, objective_id: str) -> TodoObjectiveV1 | None:
        # Manual SET NULL on notes (no PRAGMA foreign_keys — see delete_card).
        async def _do(conn):
            cur = await conn.execute(
                "SELECT * FROM todo_objectives WHERE objective_id = ?",
                (objective_id,),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            await conn.execute(
                "UPDATE todo_notes SET objective_id = NULL WHERE objective_id = ?",
                (objective_id,),
            )
            await conn.execute(
                "DELETE FROM todo_objectives WHERE objective_id = ?",
                (objective_id,),
            )
            return _sqlite_objective(row)

        return await self._run_write(_do)

    async def assign_note(
        self,
        note_id: str,
        objective_id: str | None,
        phase: str | None,
        updated_ts: float,
    ) -> TodoNoteV1 | None:
        async def _do(conn):
            cur = await conn.execute(
                "UPDATE todo_notes SET objective_id = ?, phase = ?, updated_ts = ? "
                "WHERE note_id = ?",
                (objective_id, phase, updated_ts, note_id),
            )
            if (cur.rowcount or 0) == 0:
                return None
            cur = await conn.execute(
                "SELECT * FROM todo_notes WHERE note_id = ?", (note_id,)
            )
            row = await cur.fetchone()
            if row is not None:
                await conn.execute(
                    "UPDATE todo_cards SET updated_ts = ? WHERE card_id = ?",
                    (updated_ts, row["card_id"]),
                )
            return _sqlite_note(row) if row else None

        return await self._run_write(_do)

    async def add_event(self, ev: TodoEventV1) -> None:
        async def _do(conn):
            await conn.execute(
                "INSERT INTO todo_events (event_id, card_id, objective_id, kind, "
                "payload, actor, created_ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ev.event_id, ev.card_id, ev.objective_id, ev.kind,
                 json.dumps(ev.payload or {}), ev.actor, ev.created_ts),
            )

        await self._run_write(_do)

    async def list_events(self, card_id: str, *, limit: int = 500) -> list[TodoEventV1]:
        await self._ensure_schema()
        async with self._conn() as conn:
            cur = await conn.execute(
                "SELECT * FROM todo_events WHERE card_id = ? "
                "ORDER BY created_ts LIMIT ?",
                (card_id, limit),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [_sqlite_event(r) for r in rows]

    async def update_note(self, note_id: str, body: str, updated_ts: float) -> TodoNoteV1 | None:
        async def _do(conn):
            cur = await conn.execute(
                "UPDATE todo_notes SET body = ?, updated_ts = ? WHERE note_id = ?",
                (body, updated_ts, note_id),
            )
            if (cur.rowcount or 0) == 0:
                return None
            cur = await conn.execute(
                "SELECT * FROM todo_notes WHERE note_id = ?", (note_id,)
            )
            row = await cur.fetchone()
            if row is not None:
                await conn.execute(
                    "UPDATE todo_cards SET updated_ts = ? WHERE card_id = ?",
                    (updated_ts, row["card_id"]),
                )
            return _sqlite_note(row) if row else None

        return await self._run_write(_do)

    async def delete_note(self, note_id: str) -> bool:
        async def _do(conn):
            cur = await conn.execute(
                "DELETE FROM todo_notes WHERE note_id = ?", (note_id,)
            )
            return (cur.rowcount or 0) > 0

        return await self._run_write(_do)

    async def add_attachment(self, att: TodoAttachmentV1) -> bool:
        async def _do(conn):
            cur = await conn.execute(
                "SELECT 1 FROM todo_cards WHERE card_id = ?", (att.card_id,)
            )
            if await cur.fetchone() is None:
                return False
            await conn.execute(
                "INSERT INTO todo_attachments (attachment_id, card_id, kind, path, url, "
                "mime, title, meta, created_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (att.attachment_id, att.card_id, att.kind, att.path, att.url,
                 att.mime, att.title, json.dumps(att.meta or {}), att.created_ts),
            )
            await conn.execute(
                "UPDATE todo_cards SET updated_ts = ? WHERE card_id = ?",
                (att.created_ts, att.card_id),
            )
            return True

        return await self._run_write(_do)

    async def update_attachment(self, att: TodoAttachmentV1) -> bool:
        async def _do(conn):
            cur = await conn.execute(
                "UPDATE todo_attachments SET kind = ?, path = ?, url = ?, "
                "mime = ?, title = ?, meta = ?, created_ts = ? "
                "WHERE attachment_id = ?",
                (att.kind, att.path, att.url, att.mime, att.title,
                 json.dumps(att.meta or {}), att.created_ts, att.attachment_id),
            )
            if cur.rowcount == 0:
                return False
            await conn.execute(
                "UPDATE todo_cards SET updated_ts = ? WHERE card_id = ?",
                (att.created_ts, att.card_id),
            )
            return True

        return await self._run_write(_do)

    async def delete_attachment(self, attachment_id: str) -> TodoAttachmentV1 | None:
        async def _do(conn):
            cur = await conn.execute(
                "SELECT * FROM todo_attachments WHERE attachment_id = ?",
                (attachment_id,),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            await conn.execute(
                "DELETE FROM todo_attachments WHERE attachment_id = ?",
                (attachment_id,),
            )
            return _sqlite_attachment(row)

        return await self._run_write(_do)

    async def list_all_attachments(
        self, *, limit: int = DEFAULT_LIST_LIMIT
    ) -> list[TodoAttachmentV1]:
        await self._ensure_schema()
        async with self._conn() as conn:
            cur = await conn.execute(
                "SELECT * FROM todo_attachments ORDER BY created_ts DESC LIMIT ?",
                (limit,),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [_sqlite_attachment(r) for r in rows]


def _sqlite_note(r) -> TodoNoteV1:
    return TodoNoteV1(
        note_id=r["note_id"], card_id=r["card_id"], body=r["body"],
        author=r["author"], objective_id=r["objective_id"], phase=r["phase"],
        created_ts=r["created_ts"], updated_ts=r["updated_ts"],
    )


def _sqlite_objective(r) -> TodoObjectiveV1:
    return TodoObjectiveV1(
        objective_id=r["objective_id"], card_id=r["card_id"], title=r["title"],
        phase=r["phase"], order_idx=r["order_idx"], reason=r["reason"],
        outcome=r["outcome"], created_ts=r["created_ts"], updated_ts=r["updated_ts"],
    )


def _sqlite_event(r) -> TodoEventV1:
    return TodoEventV1(
        event_id=r["event_id"], card_id=r["card_id"],
        objective_id=r["objective_id"], kind=r["kind"],
        payload=_load_json(r["payload"], {}), actor=r["actor"],
        created_ts=r["created_ts"],
    )


def _sqlite_attachment(r) -> TodoAttachmentV1:
    return TodoAttachmentV1(
        attachment_id=r["attachment_id"], card_id=r["card_id"], kind=r["kind"],
        path=r["path"], url=r["url"], mime=r["mime"], title=r["title"],
        meta=_load_json(r["meta"], {}), created_ts=r["created_ts"],
    )


def _sqlite_card(row, objs, notes, atts) -> TodoCardV1:
    return TodoCardV1(
        card_id=row["card_id"], title=row["title"], status=row["status"],
        pinned=bool(row["pinned"]), tags=_load_json(row["tags"], []),
        workspace_path=row["workspace_path"],
        created_ts=row["created_ts"], updated_ts=row["updated_ts"],
        objectives=[_sqlite_objective(o) for o in objs],
        notes=[_sqlite_note(n) for n in notes],
        attachments=[_sqlite_attachment(a) for a in atts],
    )


def _sqlite_summary(r) -> TodoCardSummaryV1:
    return TodoCardSummaryV1(
        card_id=r["card_id"], title=r["title"], status=r["status"],
        pinned=bool(r["pinned"]), tags=_load_json(r["tags"], []),
        note_count=r["note_count"], attachment_count=r["attachment_count"],
        objective_count=r["objective_count"],
        objective_done_count=r["objective_done_count"],
        created_ts=r["created_ts"], updated_ts=r["updated_ts"],
    )


_PG_CARD_COLS = (
    "card_id, title, status, pinned, tags, workspace_path, "
    "extract(epoch FROM created_ts), extract(epoch FROM updated_ts)"
)
_PG_NOTE_COLS = (
    "note_id, card_id, body, author, objective_id, phase, "
    "extract(epoch FROM created_ts), extract(epoch FROM updated_ts)"
)
_PG_ATT_COLS = (
    "attachment_id, card_id, kind, path, url, mime, title, meta, "
    "extract(epoch FROM created_ts)"
)
_PG_OBJ_COLS = (
    "objective_id, card_id, title, phase, order_idx, reason, outcome, "
    "extract(epoch FROM created_ts), extract(epoch FROM updated_ts)"
)
_PG_EVENT_COLS = (
    "event_id, card_id, objective_id, kind, payload, actor, "
    "extract(epoch FROM created_ts)"
)


class PgTodoStore(TodoStore):
    """TODO tables in Postgres (schema owned by pg/migrations v16). Primary on
    the ``postgres`` backend; wrapped in ``RoutedStore`` with the SQLite twin
    as spillover buffer by the daemon."""

    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    async def add_card(self, card: TodoCardV1) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO todo_cards (card_id, title, status, pinned, tags, "
                "workspace_path, created_ts, updated_ts) "
                "VALUES (%s, %s, %s, %s, %s::jsonb, %s, to_timestamp(%s), to_timestamp(%s))",
                (card.card_id, card.title, card.status, int(card.pinned),
                 _tags_json(card.tags), card.workspace_path,
                 card.created_ts, card.updated_ts),
            )

    async def get_card(self, card_id: str) -> TodoCardV1 | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {_PG_CARD_COLS} FROM todo_cards WHERE card_id = %s",
                (card_id,),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            cur = await conn.execute(
                f"SELECT {_PG_OBJ_COLS} FROM todo_objectives "
                "WHERE card_id = %s ORDER BY order_idx, created_ts",
                (card_id,),
            )
            objs = await cur.fetchall()
            cur = await conn.execute(
                f"SELECT {_PG_NOTE_COLS} FROM todo_notes "
                "WHERE card_id = %s ORDER BY created_ts",
                (card_id,),
            )
            notes = await cur.fetchall()
            cur = await conn.execute(
                f"SELECT {_PG_ATT_COLS} FROM todo_attachments "
                "WHERE card_id = %s ORDER BY created_ts",
                (card_id,),
            )
            atts = await cur.fetchall()
        return _pg_card(row, objs, notes, atts)

    async def update_card(self, card_id: str, fields: dict[str, Any]) -> bool:
        cols = {k: v for k, v in fields.items() if k in _CARD_UPDATE_FIELDS}
        if not cols:
            return True
        sets, params = [], []
        for k, v in cols.items():
            if k == "pinned":
                sets.append("pinned = %s"); params.append(int(bool(v)))
            elif k == "tags":
                sets.append("tags = %s::jsonb"); params.append(_tags_json(v))
            elif k == "updated_ts":
                sets.append("updated_ts = to_timestamp(%s)"); params.append(v)
            else:
                sets.append(f"{k} = %s"); params.append(v)
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"UPDATE todo_cards SET {', '.join(sets)} WHERE card_id = %s",
                (*params, card_id),
            )
            return (cur.rowcount or 0) > 0

    async def delete_card(self, card_id: str) -> bool:
        async with self._pool.connection() as conn:
            # Children go via ON DELETE CASCADE (todo_notes/attachments/chunks).
            cur = await conn.execute(
                "DELETE FROM todo_cards WHERE card_id = %s", (card_id,)
            )
            return (cur.rowcount or 0) > 0

    async def list_cards(
        self, *, status: str | None = None, limit: int = DEFAULT_LIST_LIMIT
    ) -> list[TodoCardSummaryV1]:
        where = "WHERE c.status = %s" if status else ""
        params: tuple = (status, limit) if status else (limit,)
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT c.card_id, c.title, c.status, c.pinned, c.tags, "
                "extract(epoch FROM c.created_ts), extract(epoch FROM c.updated_ts), "
                "(SELECT COUNT(*) FROM todo_notes n WHERE n.card_id = c.card_id), "
                "(SELECT COUNT(*) FROM todo_attachments a WHERE a.card_id = c.card_id), "
                "(SELECT COUNT(*) FROM todo_objectives o WHERE o.card_id = c.card_id), "
                "(SELECT COUNT(*) FROM todo_objectives o WHERE o.card_id = c.card_id "
                " AND o.phase = 'completed') "
                f"FROM todo_cards c {where} "
                "ORDER BY c.pinned DESC, c.updated_ts DESC LIMIT %s",
                params,
            )
            rows = await cur.fetchall()
        return [_pg_summary(r) for r in rows]

    async def list_card_ids(self) -> list[str]:
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT card_id FROM todo_cards")
            rows = await cur.fetchall()
        return [r[0] for r in rows]

    async def add_note(self, note: TodoNoteV1) -> bool:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT 1 FROM todo_cards WHERE card_id = %s", (note.card_id,)
            )
            if await cur.fetchone() is None:
                return False
            await conn.execute(
                "INSERT INTO todo_notes (note_id, card_id, body, author, "
                "objective_id, phase, created_ts, updated_ts) "
                "VALUES (%s, %s, %s, %s, %s, %s, to_timestamp(%s), to_timestamp(%s))",
                (note.note_id, note.card_id, note.body, note.author,
                 note.objective_id, note.phase,
                 note.created_ts, note.updated_ts),
            )
            await conn.execute(
                "UPDATE todo_cards SET updated_ts = to_timestamp(%s) WHERE card_id = %s",
                (note.updated_ts, note.card_id),
            )
            return True

    async def add_objective(self, obj: TodoObjectiveV1) -> bool:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT 1 FROM todo_cards WHERE card_id = %s", (obj.card_id,)
            )
            if await cur.fetchone() is None:
                return False
            await conn.execute(
                "INSERT INTO todo_objectives (objective_id, card_id, title, phase, "
                "order_idx, reason, outcome, created_ts, updated_ts) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, to_timestamp(%s), to_timestamp(%s))",
                (obj.objective_id, obj.card_id, obj.title, obj.phase,
                 obj.order_idx, obj.reason, obj.outcome,
                 obj.created_ts, obj.updated_ts),
            )
            await conn.execute(
                "UPDATE todo_cards SET updated_ts = to_timestamp(%s) WHERE card_id = %s",
                (obj.updated_ts, obj.card_id),
            )
            return True

    async def get_objective(self, objective_id: str) -> TodoObjectiveV1 | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {_PG_OBJ_COLS} FROM todo_objectives WHERE objective_id = %s",
                (objective_id,),
            )
            row = await cur.fetchone()
        return _pg_objective(row) if row else None

    async def update_objective(
        self, objective_id: str, fields: dict[str, Any]
    ) -> TodoObjectiveV1 | None:
        cols = {k: v for k, v in fields.items() if k in _OBJECTIVE_UPDATE_FIELDS}
        if not cols:
            return await self.get_objective(objective_id)
        sets, params = [], []
        for k, v in cols.items():
            if k == "updated_ts":
                sets.append("updated_ts = to_timestamp(%s)"); params.append(v)
            else:
                sets.append(f"{k} = %s"); params.append(v)
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"UPDATE todo_objectives SET {', '.join(sets)} "
                "WHERE objective_id = %s RETURNING card_id",
                (*params, objective_id),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            if "updated_ts" in cols:
                await conn.execute(
                    "UPDATE todo_cards SET updated_ts = to_timestamp(%s) WHERE card_id = %s",
                    (cols["updated_ts"], row[0]),
                )
            cur = await conn.execute(
                f"SELECT {_PG_OBJ_COLS} FROM todo_objectives WHERE objective_id = %s",
                (objective_id,),
            )
            obj_row = await cur.fetchone()
        return _pg_objective(obj_row) if obj_row else None

    async def delete_objective(self, objective_id: str) -> TodoObjectiveV1 | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {_PG_OBJ_COLS} FROM todo_objectives WHERE objective_id = %s",
                (objective_id,),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            # Notes demote to card-level via the FK's ON DELETE SET NULL.
            await conn.execute(
                "DELETE FROM todo_objectives WHERE objective_id = %s",
                (objective_id,),
            )
        return _pg_objective(row)

    async def assign_note(
        self,
        note_id: str,
        objective_id: str | None,
        phase: str | None,
        updated_ts: float,
    ) -> TodoNoteV1 | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE todo_notes SET objective_id = %s, phase = %s, "
                "updated_ts = to_timestamp(%s) WHERE note_id = %s RETURNING card_id",
                (objective_id, phase, updated_ts, note_id),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            await conn.execute(
                "UPDATE todo_cards SET updated_ts = to_timestamp(%s) WHERE card_id = %s",
                (updated_ts, row[0]),
            )
            cur = await conn.execute(
                f"SELECT {_PG_NOTE_COLS} FROM todo_notes WHERE note_id = %s",
                (note_id,),
            )
            note_row = await cur.fetchone()
        return _pg_note(note_row) if note_row else None

    async def add_event(self, ev: TodoEventV1) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO todo_events (event_id, card_id, objective_id, kind, "
                "payload, actor, created_ts) "
                "VALUES (%s, %s, %s, %s, %s::jsonb, %s, to_timestamp(%s))",
                (ev.event_id, ev.card_id, ev.objective_id, ev.kind,
                 json.dumps(ev.payload or {}), ev.actor, ev.created_ts),
            )

    async def list_events(self, card_id: str, *, limit: int = 500) -> list[TodoEventV1]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {_PG_EVENT_COLS} FROM todo_events "
                "WHERE card_id = %s ORDER BY created_ts LIMIT %s",
                (card_id, limit),
            )
            rows = await cur.fetchall()
        return [_pg_event(r) for r in rows]

    async def update_note(self, note_id: str, body: str, updated_ts: float) -> TodoNoteV1 | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE todo_notes SET body = %s, updated_ts = to_timestamp(%s) "
                "WHERE note_id = %s RETURNING card_id",
                (body, updated_ts, note_id),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            await conn.execute(
                "UPDATE todo_cards SET updated_ts = to_timestamp(%s) WHERE card_id = %s",
                (updated_ts, row[0]),
            )
            cur = await conn.execute(
                f"SELECT {_PG_NOTE_COLS} FROM todo_notes WHERE note_id = %s",
                (note_id,),
            )
            note_row = await cur.fetchone()
        return _pg_note(note_row) if note_row else None

    async def delete_note(self, note_id: str) -> bool:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM todo_notes WHERE note_id = %s", (note_id,)
            )
            return (cur.rowcount or 0) > 0

    async def add_attachment(self, att: TodoAttachmentV1) -> bool:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT 1 FROM todo_cards WHERE card_id = %s", (att.card_id,)
            )
            if await cur.fetchone() is None:
                return False
            await conn.execute(
                "INSERT INTO todo_attachments (attachment_id, card_id, kind, path, url, "
                "mime, title, meta, created_ts) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, to_timestamp(%s))",
                (att.attachment_id, att.card_id, att.kind, att.path, att.url,
                 att.mime, att.title, json.dumps(att.meta or {}), att.created_ts),
            )
            await conn.execute(
                "UPDATE todo_cards SET updated_ts = to_timestamp(%s) WHERE card_id = %s",
                (att.created_ts, att.card_id),
            )
            return True

    async def update_attachment(self, att: TodoAttachmentV1) -> bool:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE todo_attachments SET kind = %s, path = %s, url = %s, "
                "mime = %s, title = %s, meta = %s::jsonb, "
                "created_ts = to_timestamp(%s) WHERE attachment_id = %s",
                (att.kind, att.path, att.url, att.mime, att.title,
                 json.dumps(att.meta or {}), att.created_ts, att.attachment_id),
            )
            if cur.rowcount == 0:
                return False
            await conn.execute(
                "UPDATE todo_cards SET updated_ts = to_timestamp(%s) WHERE card_id = %s",
                (att.created_ts, att.card_id),
            )
            return True

    async def delete_attachment(self, attachment_id: str) -> TodoAttachmentV1 | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {_PG_ATT_COLS} FROM todo_attachments WHERE attachment_id = %s",
                (attachment_id,),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            await conn.execute(
                "DELETE FROM todo_attachments WHERE attachment_id = %s",
                (attachment_id,),
            )
        return _pg_attachment(row)

    async def list_all_attachments(
        self, *, limit: int = DEFAULT_LIST_LIMIT
    ) -> list[TodoAttachmentV1]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {_PG_ATT_COLS} FROM todo_attachments "
                "ORDER BY created_ts DESC LIMIT %s",
                (limit,),
            )
            rows = await cur.fetchall()
        return [_pg_attachment(r) for r in rows]


def _pg_note(r) -> TodoNoteV1:
    return TodoNoteV1(
        note_id=r[0], card_id=r[1], body=r[2], author=r[3],
        objective_id=r[4], phase=r[5],
        created_ts=float(r[6]), updated_ts=float(r[7]),
    )


def _pg_objective(r) -> TodoObjectiveV1:
    return TodoObjectiveV1(
        objective_id=r[0], card_id=r[1], title=r[2], phase=r[3],
        order_idx=int(r[4]), reason=r[5], outcome=r[6],
        created_ts=float(r[7]), updated_ts=float(r[8]),
    )


def _pg_event(r) -> TodoEventV1:
    return TodoEventV1(
        event_id=r[0], card_id=r[1], objective_id=r[2], kind=r[3],
        payload=_load_json(r[4], {}), actor=r[5], created_ts=float(r[6]),
    )


def _pg_attachment(r) -> TodoAttachmentV1:
    return TodoAttachmentV1(
        attachment_id=r[0], card_id=r[1], kind=r[2], path=r[3], url=r[4],
        mime=r[5], title=r[6], meta=_load_json(r[7], {}), created_ts=float(r[8]),
    )


def _pg_card(row, objs, notes, atts) -> TodoCardV1:
    return TodoCardV1(
        card_id=row[0], title=row[1], status=row[2], pinned=bool(row[3]),
        tags=_load_json(row[4], []), workspace_path=row[5],
        created_ts=float(row[6]), updated_ts=float(row[7]),
        objectives=[_pg_objective(o) for o in objs],
        notes=[_pg_note(n) for n in notes],
        attachments=[_pg_attachment(a) for a in atts],
    )


def _pg_summary(r) -> TodoCardSummaryV1:
    return TodoCardSummaryV1(
        card_id=r[0], title=r[1], status=r[2], pinned=bool(r[3]),
        tags=_load_json(r[4], []), created_ts=float(r[5]), updated_ts=float(r[6]),
        note_count=int(r[7]), attachment_count=int(r[8]),
        objective_count=int(r[9]), objective_done_count=int(r[10]),
    )


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

        _default_store = SqliteTodoStore(state_db_path())
    return _default_store


__all__ = [
    "TodoStore",
    "SqliteTodoStore",
    "PgTodoStore",
    "get_default_todo_store",
    "set_default_todo_store",
    "DEFAULT_LIST_LIMIT",
]
