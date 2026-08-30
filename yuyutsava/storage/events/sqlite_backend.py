"""SQLite backend + per-domain twins for the events database.

Per the Phase 2 decision, these twins do NOT use ``BaseSqliteStore``'s
lazy-per-call connection model. Instead a single :class:`SqliteEventsBackend`
holds **one** persistent ``aiosqlite`` connection, opened once with the schema
created **eagerly** at ``Store.start()`` (no first-call latency) and closed at
``Store.stop()``. Writes serialise through an ``asyncio.Lock``; reads share the
connection (aiosqlite already serialises operations on its worker thread).

In Postgres mode this same backend backs the SQLite **buffer** that catches
writes while Postgres is down (see :mod:`yuyutsava.storage.routing`).
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

import aiosqlite
from ulid import ULID

from yuyutsava.storage.base import amigration_lock
# The pending_asks wire helpers moved to events/ask_wire.py (ADR-002 step
# 2.5b) — they are backend-independent and the unified store needed them.
from yuyutsava.storage.events.ask_wire import (  # noqa: F401
    _ASK_COLS, ask_record_to_params, ask_row_to_record,
)
from yuyutsava.storage.events.abc import (
    ConsentGrantStore,
    ConsentRuleStore,
    DecisionStore,
    EventStore,
    PendingAskStore,
    PrefsBackend,
    ProposalStore,
    ToolCounterStore,
)
from yuyutsava.storage.events.schema import SCHEMA_SQL, migrate
from yuyutsava.storage.models import ConsentRule, Decision, EventRecord, Proposal
from yuyutsava.storage.paths import state_db_path

logger = logging.getLogger("yuyutsava.storage.events.sqlite")


class SqliteEventsBackend:
    """Singleton aiosqlite connection shared by every events-domain twin."""

    def __init__(self, db_path: Path | None = None, *, busy_timeout_ms: int = 5000) -> None:
        self._db_path = db_path or state_db_path()
        self._busy_timeout_ms = busy_timeout_ms
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def open(self) -> None:
        """Connect and create all schema eagerly. Idempotent."""
        if self._conn is not None:
            return
        await asyncio.to_thread(
            self._db_path.parent.mkdir, parents=True, exist_ok=True
        )
        conn = await aiosqlite.connect(str(self._db_path))
        conn.row_factory = aiosqlite.Row
        await conn.execute(f"PRAGMA busy_timeout={int(self._busy_timeout_ms)}")
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        # Cross-process migration lock: daemon + chat can boot simultaneously.
        # Foreign keys stay OFF for the whole migration: schema v5 rebuilds
        # `proposals` and `decisions` to add their constraints (SQLite has no
        # ALTER TABLE ADD CONSTRAINT), and a rebuild trips over its own
        # intermediate states with enforcement on.
        async with amigration_lock():
            await conn.executescript(SCHEMA_SQL)
            await migrate(conn)
            await conn.commit()
        # ON only after migrating, and outside any transaction — the pragma is a
        # silent no-op inside one. Without this the REFERENCES clauses in
        # schema.py are decorative: SQLite defaults foreign_keys to OFF, which
        # is why the tables could diverge from Postgres unnoticed (finding AC).
        await conn.execute("PRAGMA foreign_keys=ON")
        self._conn = conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _c(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SqliteEventsBackend.open() must be called first")
        return self._conn

    @asynccontextmanager
    async def foreign_keys_off(self) -> "AsyncIterator[None]":
        """Suspend FK enforcement (and therefore ON DELETE CASCADE) on this connection.

        For the spillover **buffer**, where cascade semantics are actively wrong.
        In Postgres mode these tables are a write buffer holding rows until the
        reconciler copies them out — not a model of referential integrity. The
        reconciler drains parents first (Postgres needs the parent row before
        the child) and deletes each drained batch from the buffer as it goes, so
        with cascade live, deleting a drained ``event_payloads`` row would take
        that event's still-undrained ``proposals`` with it and they would never
        reach Postgres.

        The constraints are still exactly right for pure-SQLite mode, where
        these tables ARE the system of record. Only the drain needs them off.

        ``PRAGMA foreign_keys`` is a no-op inside a transaction, so this must
        wrap statements that are not already in one.
        """
        await self._c.execute("PRAGMA foreign_keys=OFF")
        try:
            yield
        finally:
            await self._c.execute("PRAGMA foreign_keys=ON")

    @asynccontextmanager
    async def transaction(self) -> "AsyncIterator[aiosqlite.Connection]":
        """Run several statements atomically: all commit, or none do.

        ``execute`` / ``execute_rowcount`` commit per statement, so a method
        issuing two of them is **not** atomic — a failure between them leaves
        the first applied. That is the same defect found on the Postgres side
        (``PgPool.connection()`` is autocommit); this is its SQLite twin.

        Mirrors :meth:`yuyutsava.storage.base.BaseSqliteStore._run_write`:
        ``BEGIN IMMEDIATE``, commit on success, explicit rollback on any
        exception — including ``CancelledError``, so a cancelled task cannot
        leave a partial write.

        Holds the same write lock as ``execute``, so a transaction and a
        single-statement write can never interleave.

            async with backend.transaction() as conn:
                await conn.execute("DELETE FROM ...", (...))
                await conn.execute("INSERT INTO ...", (...))
        """
        async with self._write_lock:
            conn = self._c
            await conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                await conn.rollback()
                raise
            await conn.commit()

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        async with self._write_lock:
            await self._c.execute(sql, params)
            await self._c.commit()

    async def execute_rowcount(self, sql: str, params: Sequence[Any] = ()) -> int:
        async with self._write_lock:
            cur = await self._c.execute(sql, params)
            await self._c.commit()
            rc = cur.rowcount
            await cur.close()
            return rc

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
        cur = await self._c.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        return row

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        cur = await self._c.execute(sql, params)
        rows = await cur.fetchall()
        await cur.close()
        return list(rows)


# ---------------------------------------------------------------------------
# Per-domain SQLite twins
# ---------------------------------------------------------------------------



# NOTE: SqliteEventStore was replaced on 2026-08-08 by the Unified* store in
# events/unified.py (ADR-002 step 2.5b). Parity verified against both twins on
# both live backends in test/storage/test_events_unified_parity.py.


# NOTE: SqliteProposalStore was replaced on 2026-08-08 by the Unified* stores in events/unified.py
# (ADR-002 step 2.5b) — one implementation over the dialect adapter. Parity
# verified against both twins on both live backends in
# test/storage/test_events_unified_parity.py.



# NOTE: SqliteDecisionStore was replaced on 2026-08-08 by the Unified* stores in events/unified.py
# (ADR-002 step 2.5b) — one implementation over the dialect adapter. Parity
# verified against both twins on both live backends in
# test/storage/test_events_unified_parity.py.


# NOTE: SqliteConsentRuleStore was replaced on 2026-08-08 by UnifiedConsentRuleStore in
# events/unified.py (ADR-002 step 2.5b) — one implementation over the dialect
# adapter. Parity was verified against both twins on both live backends
# (test/storage/test_events_unified_parity.py, 44 assertions) first.


class SqlitePrefsBackend(PrefsBackend):
    def __init__(self, backend: SqliteEventsBackend) -> None:
        self._b = backend

    async def put(self, key: str, value: Any) -> None:
        await self._b.execute(
            "INSERT INTO user_prefs(key, value_json, updated_ts) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
            "updated_ts=excluded.updated_ts",
            (key, json.dumps(value, ensure_ascii=False), time.time()),
        )

    async def delete(self, key: str) -> None:
        await self._b.execute("DELETE FROM user_prefs WHERE key=?", (key,))

    async def get(self, key: str, default: Any = None) -> Any:
        row = await self._b.fetchone("SELECT value_json FROM user_prefs WHERE key=?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value_json"])
        except Exception:
            return default

    async def list(self) -> dict[str, Any]:
        rows = await self._b.fetchall("SELECT key, value_json FROM user_prefs ORDER BY key")
        out: dict[str, Any] = {}
        for r in rows:
            try:
                out[r["key"]] = json.loads(r["value_json"])
            except Exception:
                pass
        return out



# NOTE: SqliteConsentGrantStore was replaced on 2026-08-08 by the Unified* stores in events/unified.py
# (ADR-002 step 2.5b) — one implementation over the dialect adapter. Parity
# verified against both twins on both live backends in
# test/storage/test_events_unified_parity.py.









# NOTE: SqlitePendingAskStore was replaced on 2026-08-08 by the Unified* store in
# events/unified.py (ADR-002 step 2.5b). Parity verified against both twins on
# both live backends in test/storage/test_events_unified_parity.py.


# ---------------------------------------------------------------------------
# Row → model helpers
# ---------------------------------------------------------------------------


def _row_to_proposal(row: aiosqlite.Row) -> Proposal:
    return Proposal(
        proposal_id=row["proposal_id"], event_id=row["event_id"], topic=row["topic"],
        summary=row["summary"], proposed=row["proposed"], subagent=row["subagent"],
        urgency=row["urgency"], created_ts=row["created_ts"], expires_ts=row["expires_ts"],
        status=row["status"], session_id=row["session_id"], agent_path=row["agent_path"],
    )


def _row_to_consent_rule(row: aiosqlite.Row) -> ConsentRule:
    return ConsentRule(
        rule_id=row["rule_id"], topic_glob=row["topic_glob"], match_json=row["match_json"],
        decision=row["decision"], created_ts=row["created_ts"], expires_ts=row["expires_ts"],
    )


def _row_to_decision(row: aiosqlite.Row) -> Decision:
    return Decision(
        decision_id=row["decision_id"], proposal_id=row["proposal_id"], event_id=row["event_id"],
        outcome=row["outcome"], action_summary=row["action_summary"], ts=row["ts"],
        session_id=row["session_id"], agent_path=row["agent_path"],
    )
