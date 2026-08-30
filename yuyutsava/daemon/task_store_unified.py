"""One ``tasks`` implementation, both backends.

Phase 2 step 2.5b (ADR-002), playbook order 10. Replaces ``SqliteTaskStore`` and
``PgTaskStore`` — 181 lines whose only real differences were the placeholder
style and Postgres's thread-hub foreign key.

Two things worth knowing before editing this file.

**``task_id`` is the pagination cursor.** It is ``tsk_`` + ULID, and ULIDs sort
by creation time, so ``ORDER BY task_id DESC`` is reverse-chronological and
``task_id < cursor`` is a keyset page. There is no separate ordering column to
fall back on; if that TEXT comparison ever ordered differently between the
backends, the task list would silently skip or repeat rows while paging rather
than fail. ``test_cursor_pages_strictly_older`` runs on both for that reason.

**``update`` interpolates its column names.** ``fields`` keys are formatted into
the SQL because the set of updatable columns is dynamic; ``_check_fields``
against ``_MUTABLE_COLUMNS`` is the only thing standing between a caller and SQL
injection. It runs before any string building, and the parity suite asserts it on
both backends rather than trusting the comment.

**Rows are read as mappings, not tuples.** The retired twins shared a
``_row_to_record`` built on ``tuple(row)``, which worked because
``pool.connection()`` yields tuples. The dialect's read connection uses
``dict_row``, and ``tuple()`` over a mapping yields its *keys* — so the shared
helper cannot be reused here. This module maps by name, which is backend-neutral
and does not depend on ``_SELECT_COLS`` ordering staying in sync with a
positional unpack.

Parity verified on both live backends by
``test/storage/test_task_store_parity.py``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, ClassVar

from yuyutsava.daemon.task_registry import (
    TaskRecord,
    TaskStore,
    _check_fields,
)
from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.dialect import Dialect

logger = logging.getLogger("yuyutsava.daemon.task_store_unified")

#: Fixed read order. Named access below means this is the source of truth for
#: *which* columns are fetched, not for their positions.
_COLS: tuple[str, ...] = (
    "task_id", "origin", "instruction", "status", "created_ts", "thread_id",
    "complexity", "started_ts", "finished_ts", "deferred_ms", "result_summary",
    "error", "model",
)

#: Columns that are ``TIMESTAMPTZ`` on Postgres (migration v20) and REAL epoch
#: on SQLite: they bind through ``ts_param`` and read back through ``epoch``.
_TS_COLS: frozenset[str] = frozenset({"created_ts", "started_ts", "finished_ts"})


def _insert_placeholders(d) -> str:
    return ", ".join(
        d.ts_param() if c in _TS_COLS else d.ph() for c in _COLS
    )


def _select_list(d) -> str:
    return ", ".join(d.epoch(c) if c in _TS_COLS else c for c in _COLS)


class TaskSchema(BaseSqliteStore):
    """SQLite DDL owner. Byte-identical to the retired twin, v2 ALTER included."""

    _SCHEMA_VERSION: ClassVar[int] = 2
    _META_TABLE: ClassVar[str] = "tasks_meta"
    _SCHEMA_SQL: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS tasks_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasks (
            task_id        TEXT PRIMARY KEY,
            origin         TEXT NOT NULL,
            instruction    TEXT NOT NULL,
            status         TEXT NOT NULL CHECK (status IN
                           ('queued','running','done','failed','cancelled')),
            thread_id      TEXT,
            complexity     INTEGER,
            model          TEXT,
            created_ts     REAL NOT NULL,
            started_ts     REAL,
            finished_ts    REAL,
            deferred_ms    INTEGER NOT NULL DEFAULT 0,
            result_summary TEXT,
            error          TEXT
        );
        CREATE INDEX IF NOT EXISTS tasks_status_idx ON tasks (status, created_ts);
    """

    async def _migrate(self, conn) -> None:
        # v1 -> v2 added `model`. Fresh DBs get it from _SCHEMA_SQL and anchor
        # at 2, so only a real v1 file takes the ALTER.
        cur = await conn.execute(
            f"SELECT value FROM {self._META_TABLE} WHERE key=?",
            (self._META_VERSION_KEY,),
        )
        row = await cur.fetchone()
        await cur.close()
        if (int(row[0]) if row else 0) < 2:
            await conn.execute("ALTER TABLE tasks ADD COLUMN model TEXT")
        await super()._migrate(conn)


def _to_record(row: Any) -> TaskRecord:
    """Map a row to a :class:`TaskRecord` **by name**.

    Not positionally: the dialect's Postgres read connection uses ``dict_row``,
    and ``tuple(mapping)`` yields keys rather than values. Named access also
    means adding a column to ``_COLS`` cannot silently shift every later field.
    """
    return TaskRecord(
        task_id=row["task_id"], origin=row["origin"], instruction=row["instruction"],
        status=row["status"], created_ts=row["created_ts"], thread_id=row["thread_id"],
        complexity=row["complexity"], started_ts=row["started_ts"],
        finished_ts=row["finished_ts"], deferred_ms=row["deferred_ms"],
        result_summary=row["result_summary"], error=row["error"], model=row["model"],
    )


class UnifiedTaskStore(TaskStore):
    """``tasks`` — the daemon's work ledger."""

    def __init__(self, dialect: Dialect) -> None:
        self._d = dialect

    async def insert(self, rec: TaskRecord) -> None:
        d = self._d

        async def _do(conn):
            # tasks.thread_id FKs to threads on Postgres; a queued task usually
            # has no thread yet, and ensure_parent no-ops on a falsy id. `origin`
            # is forwarded because this insert is often what CREATES the hub row
            # and is the only caller that knows where the task came from.
            await d.ensure_parent(conn, rec.thread_id, origin=rec.origin)
            await conn.execute(
                f"INSERT INTO tasks ({', '.join(_COLS)}) VALUES ({_insert_placeholders(d)})",
                tuple(getattr(rec, c) for c in _COLS),
            )

        await d.write(_do)

    async def update(self, task_id: str, fields: dict[str, Any]) -> bool:
        # BEFORE any string building: `fields` keys go into the SQL text, so
        # this whitelist check is the injection boundary for this module.
        _check_fields(fields)
        d = self._d
        cols = ", ".join(
            f"{k} = {d.ts_param() if k in _TS_COLS else d.ph()}" for k in fields
        )

        async def _do(conn):
            # mark_running patches thread_id onto an existing row — upsert the
            # parent first so the FK holds.
            await d.ensure_parent(conn, fields.get("thread_id"))
            cur = await conn.execute(
                f"UPDATE tasks SET {cols} WHERE task_id = {d.ph()}",  # noqa: S608 — cols from _MUTABLE_COLUMNS
                (*fields.values(), task_id),
            )
            return (cur.rowcount or 0) == 1

        return await d.write(_do)

    async def get(self, task_id: str) -> TaskRecord | None:
        d = self._d
        async with d.reading() as conn:
            cur = await conn.execute(
                f"SELECT {_select_list(d)} FROM tasks WHERE task_id = {d.ph()}",
                (task_id,),
            )
            row = await cur.fetchone()
        return _to_record(row) if row is not None else None

    async def list(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> list[TaskRecord]:
        d = self._d
        where: list[str] = []
        args: list[Any] = []
        if status:
            where.append(f"status = {d.ph()}")
            args.append(status)
        if cursor:
            # Strictly less-than: ULID task_ids sort chronologically, so this is
            # a keyset page and the boundary row is never served twice.
            where.append(f"task_id < {d.ph()}")
            args.append(cursor)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        async with d.reading() as conn:
            cur = await conn.execute(
                f"SELECT {_select_list(d)} FROM tasks {clause} "  # noqa: S608 — clause is built from literals above
                f"ORDER BY task_id DESC LIMIT {d.ph()}",
                (*args, limit),
            )
            rows = await cur.fetchall()
        return [_to_record(r) for r in rows]


def sqlite_task_store(db_path: Path | None = None) -> UnifiedTaskStore:
    from yuyutsava.storage.dialect import SqliteDialect
    from yuyutsava.storage.paths import state_db_path

    return UnifiedTaskStore(SqliteDialect(TaskSchema(db_path or state_db_path())))


def pg_task_store(pool) -> UnifiedTaskStore:
    from yuyutsava.storage.dialect import PostgresDialect

    return UnifiedTaskStore(PostgresDialect(pool))


__all__ = ["TaskSchema", "UnifiedTaskStore", "pg_task_store", "sqlite_task_store"]
