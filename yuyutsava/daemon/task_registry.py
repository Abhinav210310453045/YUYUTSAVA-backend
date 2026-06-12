"""First-class task tracking: ``tasks`` table + in-memory cancel flags.

Every unit of orchestrator work — submitted via ``POST /tasks``, approved
through triage, or auto-approved by a consent rule — gets a ``tsk_<ULID>``
id and a persisted row driven through the lifecycle::

    queued → running → done | failed | cancelled

The persisted row is what ``GET /tasks`` / ``GET /tasks/{id}`` serve to the
mobile app and other API clients; the in-memory part is the cancel-request
set, which must stay process-local because the orchestrator loop polls it
between stream events (coarse v1 cancellation — documented in the plan).

Same two-backend shape as :mod:`yuyutsava.context.artifacts`:

- :class:`SqliteTaskStore` — a ``tasks`` table in ``state.db`` (own meta
  table; coexists with the events store via WAL).
- :class:`PgTaskStore` — the ``tasks`` table created by
  :mod:`yuyutsava.storage.pg.migrations` (v2).
"""

from __future__ import annotations

import dataclasses
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Literal

from ulid import ULID

from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.pg.pool import PgPool

logger = logging.getLogger("yuyutsava.daemon.task_registry")

TASK_STATUSES = ("queued", "running", "done", "failed", "cancelled")
TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})

# Columns mark_* helpers are allowed to touch. Guards against a typo'd
# kwarg silently becoming SQL.
_MUTABLE_COLUMNS = frozenset({
    "status", "thread_id", "complexity", "model", "started_ts", "finished_ts",
    "deferred_ms", "result_summary", "error",
})


def mint_task_id() -> str:
    """``tsk_`` + ULID. ULIDs sort by creation time, which makes the id
    itself the pagination cursor (``ORDER BY task_id DESC``)."""
    return f"tsk_{ULID()}"


@dataclass(frozen=True)
class TaskRecord:
    """One row from ``tasks`` — the unit ``GET /tasks`` serves."""

    task_id: str
    origin: str             # "api" | "cli" | "telegram" | "event:<topic>" …
    instruction: str
    status: str             # one of TASK_STATUSES
    created_ts: float
    thread_id: str | None = None
    complexity: int | None = None      # filled by Phase 4 routing
    model: str | None = None           # chosen model name (Phase 4 routing)
    started_ts: float | None = None
    finished_ts: float | None = None
    deferred_ms: int = 0               # filled by Phase 5 admission control
    result_summary: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class TaskStore(ABC):
    """Persistence interface both backends implement."""

    @abstractmethod
    async def insert(self, rec: TaskRecord) -> None: ...

    @abstractmethod
    async def update(self, task_id: str, fields: dict[str, Any]) -> bool:
        """Patch columns on one row. Returns False when the row is missing.

        ``fields`` keys must be members of ``_MUTABLE_COLUMNS``.
        """

    @abstractmethod
    async def get(self, task_id: str) -> TaskRecord | None: ...

    @abstractmethod
    async def list(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> list[TaskRecord]:
        """Newest-first page. ``cursor`` is the ``task_id`` of the last item
        of the previous page (exclusive)."""


def _check_fields(fields: dict[str, Any]) -> None:
    unknown = set(fields) - _MUTABLE_COLUMNS
    if unknown:
        raise ValueError(f"non-updatable task columns: {sorted(unknown)}")
    if not fields:
        raise ValueError("update requires at least one field")


_SELECT_COLS = (
    "task_id, origin, instruction, status, created_ts, thread_id, complexity, "
    "started_ts, finished_ts, deferred_ms, result_summary, error, model"
)


def _row_to_record(row: Any) -> TaskRecord:
    # Works for both aiosqlite.Row (mapping) and psycopg tuples because the
    # SELECT column order above is fixed.
    vals = tuple(row)
    return TaskRecord(
        task_id=vals[0], origin=vals[1], instruction=vals[2], status=vals[3],
        created_ts=vals[4], thread_id=vals[5], complexity=vals[6],
        started_ts=vals[7], finished_ts=vals[8], deferred_ms=vals[9],
        result_summary=vals[10], error=vals[11], model=vals[12],
    )


class SqliteTaskStore(BaseSqliteStore, TaskStore):
    """``tasks`` table inside ``state.db`` (zero-config fallback)."""

    # v2 (Phase 4): + model column. Fresh DBs get it from _SCHEMA_SQL and
    # anchor at 2; v1 DBs get the ALTER in _migrate.
    _SCHEMA_VERSION = 2
    _META_TABLE = "tasks_meta"
    _SCHEMA_SQL = """
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
        cur = await conn.execute(
            f"SELECT value FROM {self._META_TABLE} WHERE key=?",
            (self._META_VERSION_KEY,),
        )
        row = await cur.fetchone()
        await cur.close()
        current = int(row[0]) if row else 0
        if current < 2:
            await conn.execute("ALTER TABLE tasks ADD COLUMN model TEXT")
        await super()._migrate(conn)

    async def insert(self, rec: TaskRecord) -> None:
        async def _do(conn):
            await conn.execute(
                "INSERT INTO tasks (task_id, origin, instruction, status, "
                "thread_id, complexity, model, created_ts, started_ts, "
                "finished_ts, deferred_ms, result_summary, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rec.task_id, rec.origin, rec.instruction, rec.status,
                 rec.thread_id, rec.complexity, rec.model, rec.created_ts,
                 rec.started_ts, rec.finished_ts, rec.deferred_ms,
                 rec.result_summary, rec.error),
            )

        await self._run_write(_do)

    async def update(self, task_id: str, fields: dict[str, Any]) -> bool:
        _check_fields(fields)
        cols = ", ".join(f"{k} = ?" for k in fields)

        async def _do(conn):
            cur = await conn.execute(
                f"UPDATE tasks SET {cols} WHERE task_id = ?",  # noqa: S608 — cols from _MUTABLE_COLUMNS
                (*fields.values(), task_id),
            )
            return (cur.rowcount or 0) == 1

        return await self._run_write(_do)

    async def get(self, task_id: str) -> TaskRecord | None:
        await self._ensure_schema()
        async with self._conn() as conn:
            cur = await conn.execute(
                f"SELECT {_SELECT_COLS} FROM tasks WHERE task_id = ?",
                (task_id,),
            )
            row = await cur.fetchone()
            await cur.close()
        return _row_to_record(row) if row is not None else None

    async def list(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> list[TaskRecord]:
        await self._ensure_schema()
        where: list[str] = []
        args: list[Any] = []
        if status:
            where.append("status = ?")
            args.append(status)
        if cursor:
            where.append("task_id < ?")
            args.append(cursor)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        async with self._conn() as conn:
            cur = await conn.execute(
                f"SELECT {_SELECT_COLS} FROM tasks {clause} "
                "ORDER BY task_id DESC LIMIT ?",
                (*args, limit),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [_row_to_record(r) for r in rows]


class PgTaskStore(TaskStore):
    """``tasks`` table in Postgres (schema owned by pg/migrations.py v2)."""

    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    async def insert(self, rec: TaskRecord) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO tasks (task_id, origin, instruction, status, "
                "thread_id, complexity, model, created_ts, started_ts, "
                "finished_ts, deferred_ms, result_summary, error) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (rec.task_id, rec.origin, rec.instruction, rec.status,
                 rec.thread_id, rec.complexity, rec.model, rec.created_ts,
                 rec.started_ts, rec.finished_ts, rec.deferred_ms,
                 rec.result_summary, rec.error),
            )

    async def update(self, task_id: str, fields: dict[str, Any]) -> bool:
        _check_fields(fields)
        cols = ", ".join(f"{k} = %s" for k in fields)
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"UPDATE tasks SET {cols} WHERE task_id = %s",  # noqa: S608 — cols from _MUTABLE_COLUMNS
                (*fields.values(), task_id),
            )
            return (cur.rowcount or 0) == 1

    async def get(self, task_id: str) -> TaskRecord | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {_SELECT_COLS} FROM tasks WHERE task_id = %s",
                (task_id,),
            )
            row = await cur.fetchone()
        return _row_to_record(row) if row is not None else None

    async def list(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> list[TaskRecord]:
        where: list[str] = []
        args: list[Any] = []
        if status:
            where.append("status = %s")
            args.append(status)
        if cursor:
            where.append("task_id < %s")
            args.append(cursor)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {_SELECT_COLS} FROM tasks {clause} "
                "ORDER BY task_id DESC LIMIT %s",
                (*args, limit),
            )
            rows = await cur.fetchall()
        return [_row_to_record(r) for r in rows]


CancelOutcome = Literal["ok", "not_found", "conflict"]


class TaskRegistry:
    """Lifecycle front for the ``tasks`` table + process-local cancel flags.

    All writes go straight through to the store (no queue), so reads via
    :meth:`get` / :meth:`list` are immediately consistent. The only state
    that lives purely in memory is the cancel-request set — the orchestrator
    loop polls :meth:`cancel_requested` between stream events, which must
    not cost a DB roundtrip per event.
    """

    def __init__(self, store: TaskStore) -> None:
        self._store = store
        self._cancel_requested: set[str] = set()

    @staticmethod
    def mint_task_id() -> str:
        return mint_task_id()

    # --- lifecycle writes -------------------------------------------------

    async def create(
        self,
        *,
        task_id: str,
        origin: str,
        instruction: str,
        session_hint: str | None = None,
        complexity: int | None = None,
    ) -> TaskRecord:
        """Insert a fresh ``queued`` row. ``session_hint`` is accepted for the
        Phase-3 channel-origin contract but not yet persisted. ``complexity``
        is the submit-time score when one exists (client override or scoring
        call); ``mark_running`` records the final value either way."""
        rec = TaskRecord(
            task_id=task_id,
            origin=origin,
            instruction=instruction,
            status="queued",
            created_ts=time.time(),
            complexity=complexity,
        )
        await self._store.insert(rec)
        return rec

    async def mark_running(
        self,
        task_id: str,
        *,
        thread_id: str,
        complexity: int | None = None,
        model: str | None = None,
    ) -> None:
        """``complexity`` and ``model`` (Phase 4 routing) are recorded when
        known — the audit join ``llm_usage × tasks`` depends on them."""
        fields: dict[str, Any] = {
            "status": "running", "thread_id": thread_id,
            "started_ts": time.time(),
        }
        if complexity is not None:
            fields["complexity"] = int(complexity)
        if model:
            fields["model"] = model
        await self._update(task_id, fields)

    async def mark_done(self, task_id: str, *, result_summary: str = "") -> None:
        await self._update(task_id, {
            "status": "done", "finished_ts": time.time(),
            "result_summary": result_summary[:300] or None,
        })
        self._cancel_requested.discard(task_id)

    async def mark_failed(self, task_id: str, *, error: str) -> None:
        await self._update(task_id, {
            "status": "failed", "finished_ts": time.time(),
            "error": error[:500],
        })
        self._cancel_requested.discard(task_id)

    async def mark_cancelled(self, task_id: str, *, note: str = "") -> None:
        await self._update(task_id, {
            "status": "cancelled", "finished_ts": time.time(),
            "error": note[:500] or None,
        })
        self._cancel_requested.discard(task_id)

    async def set_deferred_ms(self, task_id: str, deferred_ms: int) -> None:
        """Phase-5 hook: how long admission control held the task back."""
        await self._update(task_id, {"deferred_ms": int(deferred_ms)})

    async def _update(self, task_id: str, fields: dict[str, Any]) -> None:
        ok = await self._store.update(task_id, fields)
        if not ok:
            # Never swallow: a transition against a missing row means a
            # wiring bug (task ran without registration).
            logger.error("task registry: update of unknown task %s (%s)",
                         task_id, fields.get("status"))

    # --- cancellation (coarse v1) ------------------------------------------

    async def request_cancel(self, task_id: str) -> CancelOutcome:
        """Flag a task for cancellation.

        The orchestrator loop honors the flag between stream events (and
        before starting a queued task) — there is no hard kill of an
        in-flight LLM/tool call in v1.
        """
        rec = await self._store.get(task_id)
        if rec is None:
            return "not_found"
        if rec.status in TERMINAL_STATUSES:
            return "conflict"
        self._cancel_requested.add(task_id)
        return "ok"

    def cancel_requested(self, task_id: str) -> bool:
        return task_id in self._cancel_requested

    # --- reads --------------------------------------------------------------

    async def get(self, task_id: str) -> TaskRecord | None:
        return await self._store.get(task_id)

    async def list(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[TaskRecord], str | None]:
        """One page newest-first plus the cursor for the next page (or None)."""
        if status and status not in TASK_STATUSES:
            raise ValueError(f"unknown status {status!r}")
        limit = max(1, min(int(limit), 200))
        rows = await self._store.list(status=status, limit=limit, cursor=cursor)
        next_cursor = rows[-1].task_id if len(rows) == limit else None
        return rows, next_cursor
