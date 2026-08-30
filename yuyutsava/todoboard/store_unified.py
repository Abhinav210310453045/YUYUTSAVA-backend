"""One TODO-board implementation, both backends.

Phase 2 step 2.5b (ADR-002), playbook order 15 — the **last** twin pair, and the
largest: 804 lines, 20 methods, 5 tables.

The contract was validated against the *existing* twins before a line of this
file was written: all 43 behaviours in
``test/storage/test_todo_store_parity.py`` pass against ``SqliteTodoStore`` and
``PgTodoStore`` unchanged. So the tests describe what the board already does,
not what this rewrite happens to do — and four wrong assumptions of mine
(``"doing"`` is not a card status, ``assign_note`` takes ``updated_ts``,
``list_all_attachments`` returns bare attachments, ``"building"`` is not an
objective phase) were caught by that step rather than by shipping.

**Child deletion is now explicit on both backends.** Postgres had four
``ON DELETE CASCADE`` foreign keys; SQLite deleted the same four tables by hand,
because ``PRAGMA foreign_keys`` is off on that connection. Two mechanisms for
one behaviour. The unified store deletes children explicitly and lets the
Postgres foreign keys stand as a **safety net rather than the mechanism**, so
what happens no longer depends on which backend is underneath — or on a
constraint quietly losing its ``ON DELETE``. Same for the ``SET NULL`` on a
note's ``objective_id`` when its objective is deleted.

Everything else the dialect already absorbs: ``ts_param``/``epoch`` for the
``TIMESTAMPTZ``-vs-REAL timestamps (migration v20), ``json_param``/``json_value``
for ``tags``/``meta``/``payload``, and ``ph`` for placeholders. Rows are read
**by name** — the Postgres twin's five row mappers unpacked positionally, the
sixth domain where that pattern blocked reuse (findings AF, AG, AH, AJ, and the
interrupts store).

Parity verified on both live backends by
``test/storage/test_todo_store_parity.py``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, ClassVar

from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.dialect import Dialect
from yuyutsava.todoboard.models import (
    TodoAttachmentV1,
    TodoCardSummaryV1,
    TodoCardV1,
    TodoEventV1,
    TodoNoteV1,
    TodoObjectiveV1,
)
from yuyutsava.todoboard.store import (
    DEFAULT_LIST_LIMIT,
    _CARD_UPDATE_FIELDS,
    _OBJECTIVE_UPDATE_FIELDS,
    TodoStore,
    _tags_json,
)

logger = logging.getLogger("yuyutsava.todoboard.store_unified")

# Column order per table, plus which of them are timestamps (TIMESTAMPTZ on
# Postgres, REAL epoch on SQLite) and which are JSON (jsonb vs TEXT). Reads and
# writes are both generated from these, so a new column is added in one place.
_CARD_COLS = ("card_id", "title", "status", "pinned", "tags",
              "workspace_path", "created_ts", "updated_ts")
_NOTE_COLS = ("note_id", "card_id", "body", "author", "objective_id",
              "phase", "created_ts", "updated_ts")
_OBJ_COLS = ("objective_id", "card_id", "title", "phase", "order_idx",
             "reason", "outcome", "created_ts", "updated_ts")
_ATT_COLS = ("attachment_id", "card_id", "kind", "path", "url", "mime",
             "title", "meta", "created_ts")
_EVENT_COLS = ("event_id", "card_id", "objective_id", "kind", "payload",
               "actor", "created_ts")

_TS = frozenset({"created_ts", "updated_ts"})
_JSON = frozenset({"tags", "meta", "payload"})


def _select(d: Dialect, cols: tuple[str, ...], prefix: str = "") -> str:
    """Read list: timestamps projected to epoch floats, everything else by name."""
    out = []
    for c in cols:
        if c in _TS:
            out.append(d.epoch(f"{prefix}{c}", c) if prefix else d.epoch(c))
        else:
            out.append(f"{prefix}{c}")
    return ", ".join(out)


def _values(d: Dialect, cols: tuple[str, ...]) -> str:
    """Placeholder list matching *cols*, with the right cast per column kind."""
    return ", ".join(
        d.ts_param() if c in _TS else d.json_param() if c in _JSON else d.ph()
        for c in cols
    )


class TodoSchema(BaseSqliteStore):
    """SQLite DDL owner. Byte-identical to the retired twin, v2 migration included."""

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
        # v1 -> v2: think-flow columns on todo_notes. ALTER ADD COLUMN is not
        # idempotent in SQLite, so guard with PRAGMA table_info — a crash
        # between the ALTER and the version bump must stay re-runnable.
        cur = await conn.execute(
            f"SELECT value FROM {self._META_TABLE} WHERE key=?",
            (self._META_VERSION_KEY,),
        )
        row = await cur.fetchone()
        await cur.close()
        if (int(row[0]) if row else 0) < 2:
            cur = await conn.execute("PRAGMA table_info(todo_notes)")
            cols = {r[1] for r in await cur.fetchall()}
            await cur.close()
            if "objective_id" not in cols:
                await conn.execute("ALTER TABLE todo_notes ADD COLUMN objective_id TEXT")
            if "phase" not in cols:
                await conn.execute("ALTER TABLE todo_notes ADD COLUMN phase TEXT")
        await super()._migrate(conn)


# -- row mappers, all by column name -----------------------------------------


def _note(r: Any, d: Dialect) -> TodoNoteV1:
    return TodoNoteV1(
        note_id=r["note_id"], card_id=r["card_id"], body=r["body"],
        author=r["author"], objective_id=r["objective_id"], phase=r["phase"],
        created_ts=float(r["created_ts"]), updated_ts=float(r["updated_ts"]),
    )


def _objective(r: Any, d: Dialect) -> TodoObjectiveV1:
    return TodoObjectiveV1(
        objective_id=r["objective_id"], card_id=r["card_id"], title=r["title"],
        phase=r["phase"], order_idx=r["order_idx"], reason=r["reason"],
        outcome=r["outcome"],
        created_ts=float(r["created_ts"]), updated_ts=float(r["updated_ts"]),
    )


def _event(r: Any, d: Dialect) -> TodoEventV1:
    return TodoEventV1(
        event_id=r["event_id"], card_id=r["card_id"],
        objective_id=r["objective_id"], kind=r["kind"],
        payload=_json_dict(d, r["payload"]), actor=r["actor"],
        created_ts=float(r["created_ts"]),
    )


def _attachment(r: Any, d: Dialect) -> TodoAttachmentV1:
    return TodoAttachmentV1(
        attachment_id=r["attachment_id"], card_id=r["card_id"], kind=r["kind"],
        path=r["path"], url=r["url"], mime=r["mime"], title=r["title"],
        meta=_json_dict(d, r["meta"]), created_ts=float(r["created_ts"]),
    )


def _json_dict(d: Dialect, raw: Any) -> dict:
    """jsonb parses to a dict; TEXT needs decoding. Malformed degrades to {}."""
    val = d.json_value(raw)
    return val if isinstance(val, dict) else {}


def _json_list(d: Dialect, raw: Any) -> list:
    val = d.json_value(raw)
    return list(val) if isinstance(val, (list, tuple)) else []


class UnifiedTodoStore(TodoStore):
    """The TODO board on whichever backend the dialect wraps."""

    def __init__(self, dialect: Dialect) -> None:
        self._d = dialect

    # -- cards --------------------------------------------------------------

    async def add_card(self, card: TodoCardV1) -> None:
        d = self._d

        async def _do(conn):
            await conn.execute(
                f"INSERT INTO todo_cards ({', '.join(_CARD_COLS)}) "
                f"VALUES ({_values(d, _CARD_COLS)})",
                (card.card_id, card.title, card.status, int(card.pinned),
                 _tags_json(card.tags), card.workspace_path,
                 card.created_ts, card.updated_ts),
            )

        await d.write(_do)

    async def get_card(self, card_id: str) -> TodoCardV1 | None:
        d = self._d
        async with d.reading() as conn:
            cur = await conn.execute(
                f"SELECT {_select(d, _CARD_COLS)} FROM todo_cards "
                f"WHERE card_id = {d.ph()}", (card_id,))
            row = await cur.fetchone()
            if row is None:
                return None
            cur = await conn.execute(
                f"SELECT {_select(d, _OBJ_COLS)} FROM todo_objectives "
                f"WHERE card_id = {d.ph()} ORDER BY order_idx, created_ts",
                (card_id,))
            objs = await cur.fetchall()
            cur = await conn.execute(
                f"SELECT {_select(d, _NOTE_COLS)} FROM todo_notes "
                f"WHERE card_id = {d.ph()} ORDER BY created_ts", (card_id,))
            notes = await cur.fetchall()
            cur = await conn.execute(
                f"SELECT {_select(d, _ATT_COLS)} FROM todo_attachments "
                f"WHERE card_id = {d.ph()} ORDER BY created_ts", (card_id,))
            atts = await cur.fetchall()
        return TodoCardV1(
            card_id=row["card_id"], title=row["title"], status=row["status"],
            # INTEGER on both backends; callers get a bool.
            pinned=bool(row["pinned"]), tags=_json_list(d, row["tags"]),
            workspace_path=row["workspace_path"],
            created_ts=float(row["created_ts"]), updated_ts=float(row["updated_ts"]),
            objectives=[_objective(o, d) for o in objs],
            notes=[_note(n, d) for n in notes],
            attachments=[_attachment(a, d) for a in atts],
        )

    async def update_card(self, card_id: str, fields: dict[str, Any]) -> bool:
        # Whitelist first: keys are interpolated into the SQL below, so this is
        # the injection boundary.
        cols = {k: v for k, v in fields.items() if k in _CARD_UPDATE_FIELDS}
        if not cols:
            return True
        if "pinned" in cols:
            # INTEGER on BOTH backends — a bool here is a type error on Postgres.
            cols["pinned"] = int(bool(cols["pinned"]))
        if "tags" in cols:
            cols["tags"] = _tags_json(cols["tags"])
        d = self._d
        sets = ", ".join(
            f"{k} = "
            f"{d.ts_param() if k in _TS else d.json_param() if k in _JSON else d.ph()}"
            for k in cols
        )

        async def _do(conn):
            cur = await conn.execute(
                f"UPDATE todo_cards SET {sets} WHERE card_id = {d.ph()}",  # noqa: S608
                (*cols.values(), card_id),
            )
            return (cur.rowcount or 0) > 0

        return await d.write(_do)

    async def delete_card(self, card_id: str) -> bool:
        """Drop the card and its four child tables.

        Children are deleted **explicitly**, not left to a cascade. Postgres has
        ``ON DELETE CASCADE`` and SQLite has no foreign keys at all on these
        tables, so relying on the constraint would mean two mechanisms for one
        behaviour — and a constraint that quietly lost its ``ON DELETE`` would
        change what happens on one backend only. The Postgres keys remain as a
        safety net.

        Blob-dir cleanup is the exchange's job; it knows ``workspace_path``.
        """
        d = self._d

        async def _do(conn):
            for table in ("todo_notes", "todo_attachments",
                          "todo_objectives", "todo_events"):
                await conn.execute(
                    f"DELETE FROM {table} WHERE card_id = {d.ph()}", (card_id,))  # noqa: S608
            cur = await conn.execute(
                f"DELETE FROM todo_cards WHERE card_id = {d.ph()}", (card_id,))
            return (cur.rowcount or 0) > 0

        return await d.write(_do)

    async def list_cards(
        self, *, status: str | None = None, limit: int = DEFAULT_LIST_LIMIT
    ) -> list[TodoCardSummaryV1]:
        d = self._d
        where = f"WHERE c.status = {d.ph()}" if status else ""
        params: tuple = (status, limit) if status else (limit,)
        async with d.reading() as conn:
            cur = await conn.execute(
                f"SELECT {_select(d, _CARD_COLS, prefix='c.')}, "
                f"(SELECT COUNT(*) FROM todo_notes n WHERE n.card_id = c.card_id) "
                f"  AS note_count, "
                f"(SELECT COUNT(*) FROM todo_attachments a WHERE a.card_id = c.card_id) "
                f"  AS attachment_count, "
                f"(SELECT COUNT(*) FROM todo_objectives o WHERE o.card_id = c.card_id) "
                f"  AS objective_count, "
                f"(SELECT COUNT(*) FROM todo_objectives o WHERE o.card_id = c.card_id "
                f" AND o.phase = 'completed') AS objective_done_count "
                f"FROM todo_cards c {where} "
                # Pinned first, then most-recently-touched: the board's order.
                f"ORDER BY c.pinned DESC, c.updated_ts DESC LIMIT {d.ph()}",
                params,
            )
            rows = await cur.fetchall()
        return [
            TodoCardSummaryV1(
                card_id=r["card_id"], title=r["title"], status=r["status"],
                pinned=bool(r["pinned"]), tags=_json_list(d, r["tags"]),
                note_count=r["note_count"], attachment_count=r["attachment_count"],
                objective_count=r["objective_count"],
                objective_done_count=r["objective_done_count"],
                created_ts=float(r["created_ts"]), updated_ts=float(r["updated_ts"]),
            )
            for r in rows
        ]

    async def list_card_ids(self) -> list[str]:
        async with self._d.reading() as conn:
            cur = await conn.execute("SELECT card_id FROM todo_cards")
            rows = await cur.fetchall()
        return [r["card_id"] for r in rows]

    # -- shared helpers -----------------------------------------------------

    async def _card_exists(self, conn, card_id: str) -> bool:
        """Explicit parent check, so both backends return False rather than raise.

        Postgres would reject the insert on its foreign key; SQLite would accept
        an orphan. Checking here is what makes the two agree.
        """
        cur = await conn.execute(
            f"SELECT 1 FROM todo_cards WHERE card_id = {self._d.ph()}", (card_id,))
        return await cur.fetchone() is not None

    async def _bump(self, conn, card_id: str, ts: float) -> None:
        """Touch the card's ``updated_ts`` — it is what orders the board."""
        await conn.execute(
            f"UPDATE todo_cards SET updated_ts = {self._d.ts_param()} "
            f"WHERE card_id = {self._d.ph()}", (ts, card_id))

    async def _fetch_one(self, conn, table: str, id_col: str, cols, ident: str):
        cur = await conn.execute(
            f"SELECT {_select(self._d, cols)} FROM {table} "  # noqa: S608
            f"WHERE {id_col} = {self._d.ph()}", (ident,))
        return await cur.fetchone()

    # -- notes --------------------------------------------------------------

    async def add_note(self, note: TodoNoteV1) -> bool:
        d = self._d

        async def _do(conn):
            if not await self._card_exists(conn, note.card_id):
                return False
            await conn.execute(
                f"INSERT INTO todo_notes ({', '.join(_NOTE_COLS)}) "
                f"VALUES ({_values(d, _NOTE_COLS)})",
                (note.note_id, note.card_id, note.body, note.author,
                 note.objective_id, note.phase, note.created_ts, note.updated_ts),
            )
            await self._bump(conn, note.card_id, note.updated_ts)
            return True

        return await d.write(_do)

    async def update_note(
        self, note_id: str, body: str, updated_ts: float
    ) -> TodoNoteV1 | None:
        d = self._d

        async def _do(conn):
            cur = await conn.execute(
                f"UPDATE todo_notes SET body = {d.ph()}, "
                f"updated_ts = {d.ts_param()} WHERE note_id = {d.ph()}",
                (body, updated_ts, note_id))
            if (cur.rowcount or 0) == 0:
                return None
            row = await self._fetch_one(conn, "todo_notes", "note_id",
                                        _NOTE_COLS, note_id)
            if row is None:
                return None
            await self._bump(conn, row["card_id"], updated_ts)
            return _note(row, d)

        return await d.write(_do)

    async def delete_note(self, note_id: str) -> bool:
        d = self._d

        async def _do(conn):
            cur = await conn.execute(
                f"DELETE FROM todo_notes WHERE note_id = {d.ph()}", (note_id,))
            return (cur.rowcount or 0) > 0

        return await d.write(_do)

    async def assign_note(
        self, note_id: str, objective_id: str | None, phase: str | None,
        updated_ts: float,
    ) -> TodoNoteV1 | None:
        d = self._d

        async def _do(conn):
            cur = await conn.execute(
                f"UPDATE todo_notes SET objective_id = {d.ph()}, phase = {d.ph()}, "
                f"updated_ts = {d.ts_param()} WHERE note_id = {d.ph()}",
                (objective_id, phase, updated_ts, note_id))
            if (cur.rowcount or 0) == 0:
                return None
            row = await self._fetch_one(conn, "todo_notes", "note_id",
                                        _NOTE_COLS, note_id)
            if row is None:
                return None
            await self._bump(conn, row["card_id"], updated_ts)
            return _note(row, d)

        return await d.write(_do)

    # -- objectives ---------------------------------------------------------

    async def add_objective(self, obj: TodoObjectiveV1) -> bool:
        d = self._d

        async def _do(conn):
            if not await self._card_exists(conn, obj.card_id):
                return False
            await conn.execute(
                f"INSERT INTO todo_objectives ({', '.join(_OBJ_COLS)}) "
                f"VALUES ({_values(d, _OBJ_COLS)})",
                (obj.objective_id, obj.card_id, obj.title, obj.phase,
                 obj.order_idx, obj.reason, obj.outcome,
                 obj.created_ts, obj.updated_ts),
            )
            await self._bump(conn, obj.card_id, obj.updated_ts)
            return True

        return await d.write(_do)

    async def get_objective(self, objective_id: str) -> TodoObjectiveV1 | None:
        d = self._d
        async with d.reading() as conn:
            row = await self._fetch_one(conn, "todo_objectives", "objective_id",
                                        _OBJ_COLS, objective_id)
        return _objective(row, d) if row else None

    async def update_objective(
        self, objective_id: str, fields: dict[str, Any]
    ) -> TodoObjectiveV1 | None:
        cols = {k: v for k, v in fields.items() if k in _OBJECTIVE_UPDATE_FIELDS}
        if not cols:
            return await self.get_objective(objective_id)
        d = self._d
        sets = ", ".join(
            f"{k} = {d.ts_param() if k in _TS else d.ph()}" for k in cols
        )

        async def _do(conn):
            cur = await conn.execute(
                f"UPDATE todo_objectives SET {sets} "  # noqa: S608
                f"WHERE objective_id = {d.ph()}",
                (*cols.values(), objective_id))
            if (cur.rowcount or 0) == 0:
                return None
            row = await self._fetch_one(conn, "todo_objectives", "objective_id",
                                        _OBJ_COLS, objective_id)
            if row is None:
                return None
            if "updated_ts" in cols:
                await self._bump(conn, row["card_id"], cols["updated_ts"])
            return _objective(row, d)

        return await d.write(_do)

    async def delete_objective(self, objective_id: str) -> TodoObjectiveV1 | None:
        """Remove an objective; its notes and events survive.

        The ``objective_id`` on a note is nulled **explicitly** rather than left
        to Postgres's ``ON DELETE SET NULL`` — SQLite has no such constraint, so
        this is the same "one behaviour, one mechanism" choice as
        :meth:`delete_card`. ``phase`` is deliberately kept: it is historical
        context for when the note was written, not a live pointer.
        """
        d = self._d

        async def _do(conn):
            row = await self._fetch_one(conn, "todo_objectives", "objective_id",
                                        _OBJ_COLS, objective_id)
            if row is None:
                return None
            await conn.execute(
                f"UPDATE todo_notes SET objective_id = NULL "
                f"WHERE objective_id = {d.ph()}", (objective_id,))
            await conn.execute(
                f"DELETE FROM todo_objectives WHERE objective_id = {d.ph()}",
                (objective_id,))
            return _objective(row, d)

        return await d.write(_do)

    # -- attachments --------------------------------------------------------

    async def add_attachment(self, att: TodoAttachmentV1) -> bool:
        d = self._d

        async def _do(conn):
            if not await self._card_exists(conn, att.card_id):
                return False
            await conn.execute(
                f"INSERT INTO todo_attachments ({', '.join(_ATT_COLS)}) "
                f"VALUES ({_values(d, _ATT_COLS)})",
                (att.attachment_id, att.card_id, att.kind, att.path, att.url,
                 att.mime, att.title, json.dumps(att.meta or {}), att.created_ts),
            )
            await self._bump(conn, att.card_id, att.created_ts)
            return True

        return await d.write(_do)

    async def update_attachment(self, att: TodoAttachmentV1) -> bool:
        d = self._d

        async def _do(conn):
            cur = await conn.execute(
                f"UPDATE todo_attachments SET kind = {d.ph()}, path = {d.ph()}, "
                f"url = {d.ph()}, mime = {d.ph()}, title = {d.ph()}, "
                f"meta = {d.json_param()}, created_ts = {d.ts_param()} "
                f"WHERE attachment_id = {d.ph()}",
                (att.kind, att.path, att.url, att.mime, att.title,
                 json.dumps(att.meta or {}), att.created_ts, att.attachment_id),
            )
            if (cur.rowcount or 0) == 0:
                return False
            await self._bump(conn, att.card_id, att.created_ts)
            return True

        return await d.write(_do)

    async def delete_attachment(self, attachment_id: str) -> TodoAttachmentV1 | None:
        d = self._d

        async def _do(conn):
            row = await self._fetch_one(conn, "todo_attachments", "attachment_id",
                                        _ATT_COLS, attachment_id)
            if row is None:
                return None
            await conn.execute(
                f"DELETE FROM todo_attachments WHERE attachment_id = {d.ph()}",
                (attachment_id,))
            return _attachment(row, d)

        return await d.write(_do)

    async def list_all_attachments(
        self, *, limit: int = DEFAULT_LIST_LIMIT
    ) -> list[TodoAttachmentV1]:
        d = self._d
        async with d.reading() as conn:
            cur = await conn.execute(
                f"SELECT {_select(d, _ATT_COLS)} FROM todo_attachments "
                f"ORDER BY created_ts DESC LIMIT {d.ph()}", (limit,))
            rows = await cur.fetchall()
        return [_attachment(r, d) for r in rows]

    # -- events -------------------------------------------------------------

    async def add_event(self, ev: TodoEventV1) -> None:
        """Append one timeline row.

        Does **not** bump the card's ``updated_ts`` — the mutation this event
        describes already did, and bumping again would reorder the board for a
        mere audit write.
        """
        d = self._d

        async def _do(conn):
            await conn.execute(
                f"INSERT INTO todo_events ({', '.join(_EVENT_COLS)}) "
                f"VALUES ({_values(d, _EVENT_COLS)})",
                (ev.event_id, ev.card_id, ev.objective_id, ev.kind,
                 json.dumps(ev.payload or {}), ev.actor, ev.created_ts),
            )

        await d.write(_do)

    async def list_events(
        self, card_id: str, *, limit: int = 500
    ) -> list[TodoEventV1]:
        d = self._d
        async with d.reading() as conn:
            cur = await conn.execute(
                f"SELECT {_select(d, _EVENT_COLS)} FROM todo_events "
                f"WHERE card_id = {d.ph()} ORDER BY created_ts LIMIT {d.ph()}",
                (card_id, limit))
            rows = await cur.fetchall()
        return [_event(r, d) for r in rows]


def sqlite_todo_store(db_path: Path | None = None) -> UnifiedTodoStore:
    from yuyutsava.storage.dialect import SqliteDialect
    from yuyutsava.storage.paths import state_db_path

    return UnifiedTodoStore(SqliteDialect(TodoSchema(db_path or state_db_path())))


def pg_todo_store(pool) -> UnifiedTodoStore:
    from yuyutsava.storage.dialect import PostgresDialect

    return UnifiedTodoStore(PostgresDialect(pool))


__all__ = ["TodoSchema", "UnifiedTodoStore", "pg_todo_store", "sqlite_todo_store"]
