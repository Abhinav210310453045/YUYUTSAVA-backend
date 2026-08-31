"""One ``interrupts`` implementation, both backends.

Phase 2 step 2.5b (ADR-002), playbook order 14. Replaces
``SqliteInterruptsStore`` and ``PgInterruptsStore`` — 307 lines, the largest
pair migrated so far outside the todo board.

``interrupts`` is the HITL audit log: every permission prompt and question put
to the user, and what they answered. Its defining property is that the write
path is **best-effort**.

``record`` and ``resolve`` run in front of a live user prompt. If they raise,
the user gets a crash instead of a question. So both twins caught everything,
logged, and carried on — ``record`` returning ``""`` to signal "no audit row",
which every caller then feeds back to ``resolve``, hence the empty-id guard
there. That contract is preserved exactly and covered by
``test/storage/test_interrupts_store_parity.py::BestEffortWrites``, which drives
a store whose every write explodes.

**This domain does NOT have the two-clock ``created_at`` bug** that transcripts
(AE), feedback (AH), memory (AI) and artifacts (AJ) all carried: the column is
``DOUBLE PRECISION`` on Postgres, not ``TIMESTAMPTZ``, and both twins already
bound ``time.time()`` explicitly. Worth stating because four consecutive domains
had it — the pattern is common, not universal.

Two things did diverge:

* **JSON columns.** ``paths_json`` and ``payload_json`` are ``jsonb`` on
  Postgres and TEXT on SQLite, so the twins decoded them differently — SQLite
  parsed and guarded against malformed text, Postgres type-checked what psycopg
  had already parsed. ``Dialect.json_param``/``json_value`` states it once, and
  the guards are kept: a corrupt payload degrades to ``{}`` rather than losing
  the audit row.
* **Reads.** SQLite used ``SELECT *`` and Postgres an explicit column list
  read **positionally** (fifth domain where that blocked reuse — see findings
  AF, AG, AH, AJ). Both are now an explicit list read by name.

Parity verified on both live backends by
``test/storage/test_interrupts_store_parity.py``.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, ClassVar

from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.dialect import Dialect
from yuyutsava.storage.interrupts import InterruptRecord, InterruptsStore

logger = logging.getLogger("yuyutsava.storage.interrupts_unified")

#: Columns that are ``TIMESTAMPTZ`` on Postgres (migration v20) and REAL epoch
#: on SQLite.
_TS_COLS: frozenset[str] = frozenset({"created_at", "resolved_at"})


def _select_list(d: "Dialect") -> str:
    return ", ".join(d.epoch(c) if c in _TS_COLS else c for c in _COLS)


#: Fixed read set; access is by name, so this drives *which* columns are read.
_COLS: tuple[str, ...] = (
    "id", "session_id", "thread_id", "agent_path", "requesting_agent",
    "parent_agent", "invocation_mode", "kind", "operation", "paths_json",
    "zone", "risk_level", "reason", "question", "payload_json", "outcome",
    "user_response", "created_at", "resolved_at",
)


class InterruptsSchema(BaseSqliteStore):
    """SQLite DDL owner. Byte-identical to the retired twin's ``_SCHEMA_SQL``."""

    _SCHEMA_VERSION: ClassVar[int] = 1
    _META_TABLE: ClassVar[str] = "interrupts_meta"
    _SCHEMA_SQL: ClassVar[str] = """
    CREATE TABLE IF NOT EXISTS interrupts_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS interrupts (
        id                TEXT PRIMARY KEY,
        session_id        TEXT NOT NULL,
        thread_id         TEXT NOT NULL,
        agent_path        TEXT NOT NULL,
        requesting_agent  TEXT,
        parent_agent      TEXT,
        invocation_mode   TEXT NOT NULL,
        kind              TEXT NOT NULL,
        operation         TEXT,
        paths_json        TEXT,
        zone              TEXT,
        risk_level        TEXT,
        reason            TEXT,
        question          TEXT,
        payload_json      TEXT NOT NULL,
        outcome           TEXT,
        user_response     TEXT,
        created_at        REAL NOT NULL,
        resolved_at       REAL
    );

    CREATE INDEX IF NOT EXISTS idx_interrupts_session
        ON interrupts(session_id);
    CREATE INDEX IF NOT EXISTS idx_interrupts_agent_path
        ON interrupts(agent_path);
    CREATE INDEX IF NOT EXISTS idx_interrupts_created_at
        ON interrupts(created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_interrupts_unresolved
        ON interrupts(session_id, resolved_at);
    """


def _to_record(row: Any, d: Dialect) -> InterruptRecord:
    """Rehydrate an :class:`InterruptRecord`, by column name.

    Both JSON columns degrade rather than raise: a row whose payload cannot be
    decoded is still a real audit record of a prompt the user saw, and losing it
    would be worse than showing an empty payload.
    """
    payload = d.json_value(row["payload_json"])
    if not isinstance(payload, dict):
        payload = {}
    decoded = d.json_value(row["paths_json"])
    paths = list(decoded) if isinstance(decoded, (list, tuple)) else None
    return InterruptRecord(
        session_id=row["session_id"], thread_id=row["thread_id"],
        invocation_mode=row["invocation_mode"], payload=payload, kind=row["kind"],
        agent_path=row["agent_path"], requesting_agent=row["requesting_agent"],
        parent_agent=row["parent_agent"], operation=row["operation"], paths=paths,
        zone=row["zone"], risk_level=row["risk_level"], reason=row["reason"],
        question=row["question"], id=row["id"], outcome=row["outcome"],
        user_response=row["user_response"],
        created_at=float(row["created_at"]),
        resolved_at=(
            float(row["resolved_at"]) if row["resolved_at"] is not None else None
        ),
    )


class UnifiedInterruptsStore(InterruptsStore):
    """``interrupts`` — the HITL audit log."""

    def __init__(self, dialect: Dialect) -> None:
        self._d = dialect

    # -- writes: best-effort, never raise into a live prompt ----------------

    async def record(self, record: InterruptRecord) -> str:
        """Persist a new interrupt. Returns the row id, or ``""`` on failure.

        The empty string is the signal every caller passes back to
        :meth:`resolve`, which is why that method guards on it.
        """
        d = self._d
        row_id = str(uuid.uuid4())
        try:
            paths_json = json.dumps(record.paths) if record.paths is not None else None
        except (TypeError, ValueError):
            paths_json = None
        try:
            # default=str because payloads carry arbitrary objects (Paths,
            # datetimes) straight off the tool call.
            payload_json = json.dumps(record.payload, default=str)
        except (TypeError, ValueError):
            payload_json = "{}"
        now = time.time()

        async def _do(conn):
            # Postgres FKs thread_id to threads; no-op on SQLite.
            await d.ensure_parent(conn, record.thread_id)
            await conn.execute(
                f"INSERT INTO interrupts ({', '.join(_COLS)}) VALUES ("
                f"{d.ph(9)}, {d.json_param()}, {d.ph(4)}, {d.json_param()}, "
                f"NULL, NULL, {d.ts_param()}, NULL)",
                (
                    row_id, record.session_id, record.thread_id, record.agent_path,
                    record.requesting_agent, record.parent_agent,
                    record.invocation_mode, record.kind, record.operation,
                    paths_json, record.zone, record.risk_level, record.reason,
                    record.question, payload_json, now,
                ),
            )
            return row_id

        try:
            return await d.write(_do)
        except Exception as exc:  # noqa: BLE001
            # Best-effort: the prompt matters more than the audit row.
            logger.warning("InterruptsStore.record failed: %s", exc)
            return ""

    async def resolve(
        self, row_id: str, *, outcome: str, user_response: str | None = None
    ) -> None:
        """Mark a row resolved. Best-effort; failures are logged only."""
        if not row_id:
            return  # record() failed upstream — nothing to resolve
        d = self._d
        now = time.time()

        async def _do(conn):
            await conn.execute(
                f"UPDATE interrupts SET outcome = {d.ph()}, user_response = {d.ph()}, "
                f"resolved_at = {d.ts_param()} WHERE id = {d.ph()}",
                (outcome, user_response, now, row_id),
            )

        try:
            await d.write(_do)
        except Exception as exc:  # noqa: BLE001
            logger.warning("InterruptsStore.resolve failed: %s", exc)

    async def mark_orphaned_for_session(self, session_id: str) -> int:
        """Flip this session's unresolved rows to ``orphaned``. Returns the count.

        Called from the resume path: a permission prompt killed with its process
        should leave a closed audit row, not a perpetually-open one.
        """
        if not session_id:
            return 0
        d = self._d
        now = time.time()

        async def _do(conn):
            cur = await conn.execute(
                f"UPDATE interrupts SET outcome = 'orphaned', "
                f"resolved_at = {d.ts_param()} "
                f"WHERE session_id = {d.ph()} AND resolved_at IS NULL",
                (now, session_id),
            )
            return cur.rowcount or 0

        try:
            return await d.write(_do)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "InterruptsStore.mark_orphaned_for_session failed: %s", exc
            )
            return 0

    # -- reads: typed records, never dicts ----------------------------------

    async def list_for_session(
        self, session_id: str, *, limit: int = 100
    ) -> list[InterruptRecord]:
        d = self._d
        async with d.reading() as conn:
            cur = await conn.execute(
                f"SELECT {_select_list(d)} FROM interrupts "
                f"WHERE session_id = {d.ph()} ORDER BY created_at DESC LIMIT {d.ph()}",
                (session_id, int(limit)),
            )
            rows = await cur.fetchall()
        return [_to_record(r, d) for r in rows]

    async def list_recent(
        self, *, agent_path_prefix: str | None = None, limit: int = 50
    ) -> list[InterruptRecord]:
        d = self._d
        async with d.reading() as conn:
            if agent_path_prefix:
                # Prefix match: how the UI scopes the log to one agent's subtree.
                cur = await conn.execute(
                    f"SELECT {_select_list(d)} FROM interrupts "
                    f"WHERE agent_path LIKE {d.ph()} "
                    f"ORDER BY created_at DESC LIMIT {d.ph()}",
                    (f"{agent_path_prefix}%", int(limit)),
                )
            else:
                cur = await conn.execute(
                    f"SELECT {_select_list(d)} FROM interrupts "
                    f"ORDER BY created_at DESC LIMIT {d.ph()}",
                    (int(limit),),
                )
            rows = await cur.fetchall()
        return [_to_record(r, d) for r in rows]


def sqlite_interrupts_store(
    db_path: Path | None = None, *, busy_timeout_ms: int = 5000
) -> UnifiedInterruptsStore:
    """``busy_timeout_ms`` is carried through: the sessions runner tunes it,
    because a permission prompt blocking on SQLITE_BUSY stalls the user."""
    from yuyutsava.storage.dialect import SqliteDialect
    from yuyutsava.storage.paths import interrupts_db_path

    return UnifiedInterruptsStore(
        SqliteDialect(
            InterruptsSchema(
                db_path or interrupts_db_path(), busy_timeout_ms=busy_timeout_ms
            )
        )
    )


def pg_interrupts_store(pool) -> UnifiedInterruptsStore:
    from yuyutsava.storage.dialect import PostgresDialect

    return UnifiedInterruptsStore(PostgresDialect(pool))


__all__ = [
    "InterruptsSchema",
    "UnifiedInterruptsStore",
    "pg_interrupts_store",
    "sqlite_interrupts_store",
]
