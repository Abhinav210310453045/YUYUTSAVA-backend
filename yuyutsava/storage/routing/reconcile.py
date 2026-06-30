"""Drain the SQLite buffer back into Postgres on recovery — then delete it.

The spillover contract is that a row never lives in both stores. After the
health probe clears ``degraded``, :class:`Reconciler.reconcile` copies each
buffered SQLite row into Postgres with ``INSERT ... ON CONFLICT DO NOTHING``
(idempotent via the primary key) and then **deletes** the drained rows from
SQLite. Work is bounded per batch so recovery never stalls the loop.

Extra drains/backfills (memory + skills embeddings) are registered as async
callbacks so this module stays decoupled from those subsystems.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Sequence

from yuyutsava.storage.events.sqlite_backend import SqliteEventsBackend
from yuyutsava.storage.pg.pool import PgPool

logger = logging.getLogger("yuyutsava.storage.routing.reconcile")

_BATCH = 500


@dataclass(frozen=True)
class TableSpec:
    """How to drain one buffered table from SQLite into Postgres."""

    table: str
    pk: tuple[str, ...]
    columns: tuple[str, ...]
    order: int                       # FK drain order (lower drains first)
    jsonb: frozenset[str] = frozenset()


# FK order: event_payloads → proposals → decisions; the rest are independent.
EVENT_TABLE_SPECS: tuple[TableSpec, ...] = (
    TableSpec("event_payloads", ("event_id",),
              ("event_id", "topic", "ts", "payload_json", "blob_path"),
              order=1, jsonb=frozenset({"payload_json"})),
    TableSpec("proposals", ("proposal_id",),
              ("proposal_id", "event_id", "topic", "summary", "proposed", "subagent",
               "urgency", "created_ts", "expires_ts", "status", "session_id", "agent_path"),
              order=2),
    TableSpec("decisions", ("decision_id",),
              ("decision_id", "proposal_id", "event_id", "outcome", "action_summary",
               "ts", "session_id", "agent_path"),
              order=3),
    TableSpec("consent_rules", ("rule_id",),
              ("rule_id", "topic_glob", "match_json", "decision", "created_ts", "expires_ts"),
              order=4, jsonb=frozenset({"match_json"})),
    TableSpec("tool_call_counters", ("tool_name", "day"),
              ("tool_name", "day", "count"), order=4),
    TableSpec("user_prefs", ("key",),
              ("key", "value_json", "updated_ts"), order=4, jsonb=frozenset({"value_json"})),
    TableSpec("consent_grants", ("grant_id",),
              ("grant_id", "domain", "subject_key", "decision", "scope", "scope_ref",
               "created_ts", "expires_ts"), order=4),
)


def _insert_sql(spec: TableSpec) -> str:
    placeholders = ", ".join(
        f"%s::jsonb" if c in spec.jsonb else "%s" for c in spec.columns
    )
    return (
        f"INSERT INTO {spec.table} ({', '.join(spec.columns)}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT ({', '.join(spec.pk)}) DO NOTHING"
    )


@dataclass
class Reconciler:
    """Drives buffer → Postgres reconciliation for the events tables, plus any
    registered extra drains and embedding backfills."""

    backend: SqliteEventsBackend
    pool: PgPool
    extra_drains: list[Callable[[], Awaitable[int]]] = field(default_factory=list)
    backfills: list[Callable[[], Awaitable[int]]] = field(default_factory=list)

    async def reconcile(self) -> int:
        """Drain everything; return the total rows moved. Never raises."""
        total = 0
        for spec in sorted(EVENT_TABLE_SPECS, key=lambda s: s.order):
            try:
                total += await self._drain_table(spec)
            except Exception:  # noqa: BLE001
                logger.exception("reconcile: draining %s failed", spec.table)
        for drain in self.extra_drains:
            try:
                total += await drain()
            except Exception:  # noqa: BLE001
                logger.exception("reconcile: extra drain failed")
        for backfill in self.backfills:
            try:
                await backfill()
            except Exception:  # noqa: BLE001
                logger.exception("reconcile: backfill failed")
        if total:
            logger.info("reconcile: drained %d buffered row(s) into postgres", total)
        return total

    async def _drain_table(self, spec: TableSpec) -> int:
        insert_sql = _insert_sql(spec)
        select_cols = ", ".join(spec.columns)
        moved = 0
        while True:
            rows = await self.backend.fetchall(
                f"SELECT {select_cols} FROM {spec.table} LIMIT {_BATCH}"
            )
            if not rows:
                break
            # Insert this batch into Postgres (idempotent).
            async with self.pool.connection() as conn:
                for row in rows:
                    await conn.execute(insert_sql, tuple(row[c] for c in spec.columns))
            # Delete exactly the drained rows from the SQLite buffer.
            pk_clause = " AND ".join(f"{c}=?" for c in spec.pk)
            for row in rows:
                await self.backend.execute(
                    f"DELETE FROM {spec.table} WHERE {pk_clause}",
                    tuple(row[c] for c in spec.pk),
                )
            moved += len(rows)
            if len(rows) < _BATCH:
                break
        return moved


__all__ = ["Reconciler", "TableSpec", "EVENT_TABLE_SPECS"]
