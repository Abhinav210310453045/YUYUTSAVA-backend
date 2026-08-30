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
from yuyutsava.storage.pg.threads import ensure_thread

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
    # Content-table extensions (empty/False for the events tables → no change):
    ts_cols: frozenset[str] = frozenset()   # epoch REAL → to_timestamp() for TIMESTAMPTZ
    thread_fk: bool = False                  # ensure_thread(thread_id) before insert
    any_conflict: bool = False               # bare ON CONFLICT DO NOTHING (any unique)


# FK order: event_payloads → proposals → decisions; the rest are independent.
#
# ``ts_cols`` marks epoch-REAL columns in the SQLite buffer whose Postgres
# counterpart is TIMESTAMPTZ, so the drain wraps them in to_timestamp(). Every
# timestamp column is TIMESTAMPTZ on Postgres since migration v20 — before that
# the events tables were DOUBLE PRECISION and needed no wrapping.
EVENT_TABLE_SPECS: tuple[TableSpec, ...] = (
    TableSpec("event_payloads", ("event_id",),
              ("event_id", "topic", "ts", "payload_json", "blob_path"),
              order=1, jsonb=frozenset({"payload_json"}),
              ts_cols=frozenset({"ts"})),
    TableSpec("proposals", ("proposal_id",),
              ("proposal_id", "event_id", "topic", "summary", "proposed", "subagent",
               "urgency", "created_ts", "expires_ts", "status", "session_id", "agent_path"),
              order=2, ts_cols=frozenset({"created_ts", "expires_ts"})),
    TableSpec("decisions", ("decision_id",),
              ("decision_id", "proposal_id", "event_id", "outcome", "action_summary",
               "ts", "session_id", "agent_path"),
              order=3, ts_cols=frozenset({"ts"})),
    TableSpec("consent_rules", ("rule_id",),
              ("rule_id", "topic_glob", "match_json", "decision", "created_ts", "expires_ts"),
              order=4, jsonb=frozenset({"match_json"}),
              ts_cols=frozenset({"created_ts", "expires_ts"})),
    TableSpec("tool_call_counters", ("tool_name", "day"),
              ("tool_name", "day", "count"), order=4),
    TableSpec("user_prefs", ("key",),
              ("key", "value_json", "updated_ts"), order=4, jsonb=frozenset({"value_json"}),
              ts_cols=frozenset({"updated_ts"})),
    TableSpec("consent_grants", ("grant_id",),
              ("grant_id", "domain", "subject_key", "decision", "scope", "scope_ref",
               "created_ts", "expires_ts"), order=4,
              ts_cols=frozenset({"created_ts", "expires_ts"})),
)


# Content/REST-path tables written OUTSIDE a checkpointed turn (feedback, visuals)
# — the ones that genuinely benefit from write-failover. Unlike the events tables
# (DOUBLE PRECISION timestamps), these use TIMESTAMPTZ, so created_ts (epoch REAL
# in the SQLite buffer) is wrapped in to_timestamp() on drain. visual_artifacts
# has a threads FK; message_feedback deliberately has none (survives deletion) and
# uses a bare ON CONFLICT (its natural key is a secondary unique index).
CONTENT_TABLE_SPECS: tuple[TableSpec, ...] = (
    TableSpec("visual_artifacts", ("visual_id",),
              ("visual_id", "thread_id", "kind", "title", "mime", "path", "source", "created_ts"),
              order=5, ts_cols=frozenset({"created_ts"}), thread_fk=True, any_conflict=True),
    TableSpec("message_feedback", ("feedback_id",),
              ("feedback_id", "thread_id", "session_id", "workspace", "message_ref",
               "rating", "note", "user_text", "assistant_text", "created_ts"),
              order=5, ts_cols=frozenset({"created_ts"}), any_conflict=True),
    # TODO board (pg/migrations v16 + v17): durable user data, no thread FK.
    # Cards drain before their FK children; objectives before notes (notes'
    # objective_id FK). pinned/order_idx are INTEGER on both sides so the
    # replay needs no bool cast. A buffered note referencing an objective
    # deleted in PG mid-outage violates todo_notes_objective_fk on replay
    # (any_conflict doesn't cover FK errors) — same accepted risk class as
    # the card FK. todo_events is history: append-only, no objective FK.
    TableSpec("todo_cards", ("card_id",),
              ("card_id", "title", "status", "pinned", "tags", "workspace_path",
               "created_ts", "updated_ts"),
              order=6, jsonb=frozenset({"tags"}),
              ts_cols=frozenset({"created_ts", "updated_ts"}), any_conflict=True),
    TableSpec("todo_objectives", ("objective_id",),
              ("objective_id", "card_id", "title", "phase", "order_idx",
               "reason", "outcome", "created_ts", "updated_ts"),
              order=7, ts_cols=frozenset({"created_ts", "updated_ts"}), any_conflict=True),
    TableSpec("todo_notes", ("note_id",),
              ("note_id", "card_id", "body", "author", "objective_id", "phase",
               "created_ts", "updated_ts"),
              order=8, ts_cols=frozenset({"created_ts", "updated_ts"}), any_conflict=True),
    TableSpec("todo_attachments", ("attachment_id",),
              ("attachment_id", "card_id", "kind", "path", "url", "mime", "title",
               "meta", "created_ts"),
              order=8, jsonb=frozenset({"meta"}),
              ts_cols=frozenset({"created_ts"}), any_conflict=True),
    TableSpec("todo_events", ("event_id",),
              ("event_id", "card_id", "objective_id", "kind", "payload", "actor",
               "created_ts"),
              order=9, jsonb=frozenset({"payload"}),
              ts_cols=frozenset({"created_ts"}), any_conflict=True),
)


def _insert_sql(spec: TableSpec) -> str:
    def _ph(c: str) -> str:
        if c in spec.ts_cols:
            return "to_timestamp(%s)"
        return "%s::jsonb" if c in spec.jsonb else "%s"

    placeholders = ", ".join(_ph(c) for c in spec.columns)
    conflict = "ON CONFLICT DO NOTHING" if spec.any_conflict \
        else f"ON CONFLICT ({', '.join(spec.pk)}) DO NOTHING"
    return (
        f"INSERT INTO {spec.table} ({', '.join(spec.columns)}) "
        f"VALUES ({placeholders}) {conflict}"
    )


@dataclass
class Reconciler:
    """Drives buffer → Postgres reconciliation for the events tables, plus any
    registered extra drains and embedding backfills."""

    backend: SqliteEventsBackend
    pool: PgPool
    extra_drains: list[Callable[[], Awaitable[int]]] = field(default_factory=list)
    backfills: list[Callable[[], Awaitable[int]]] = field(default_factory=list)
    content_specs: tuple[TableSpec, ...] = ()

    async def reconcile(self) -> int:
        """Drain everything; return the total rows moved. Never raises.

        Runs with foreign keys suspended on the buffer connection. Tables drain
        parents-first because Postgres requires the parent row before the child,
        and each batch is deleted from the buffer as it lands — so with SQLite's
        ``ON DELETE CASCADE`` live (schema v5), removing a drained
        ``event_payloads`` row would delete that event's not-yet-drained
        ``proposals`` before they were ever copied. Silent data loss, only
        during an outage recovery. See ``SqliteEventsBackend.foreign_keys_off``.
        """
        total = 0
        async with self.backend.foreign_keys_off():
            total += await self._drain_all()
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

    async def _drain_all(self) -> int:
        total = 0
        for spec in sorted((*EVENT_TABLE_SPECS, *self.content_specs), key=lambda s: s.order):
            try:
                total += await self._drain_table(spec)
            except Exception:  # noqa: BLE001
                logger.exception("reconcile: draining %s failed", spec.table)
        return total

    async def _drain_table(self, spec: TableSpec) -> int:
        # A content twin table only exists once an outage actually buffered to it;
        # skip quietly otherwise (avoids noisy errors every recovery).
        exists = await self.backend.fetchall(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (spec.table,)
        )
        if not exists:
            return 0
        insert_sql = _insert_sql(spec)
        select_cols = ", ".join(spec.columns)
        moved = 0
        while True:
            rows = await self.backend.fetchall(
                f"SELECT {select_cols} FROM {spec.table} LIMIT {_BATCH}"
            )
            if not rows:
                break
            # Insert this batch into Postgres (idempotent). Tables with a threads
            # FK need the parent row to exist first (buffered turns may predate it).
            async with self.pool.connection() as conn:
                for row in rows:
                    if spec.thread_fk and row["thread_id"]:
                        await ensure_thread(conn, row["thread_id"])
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


__all__ = ["Reconciler", "TableSpec", "EVENT_TABLE_SPECS", "CONTENT_TABLE_SPECS"]
