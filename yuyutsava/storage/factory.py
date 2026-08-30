"""``StoreFactory`` — resolve the backend once, build every store uniformly.

Phase 2 step 2.6 (ADR-002), closing findings ``F-S04`` and ``F-S07``.

Before this, ``build_daemon`` re-decided "Postgres or SQLite" independently for
every store, inline, thirteen times::

    artifact_store = pg_artifact_store(...) if pg else sqlite_artifact_store(...)
    summary_store  = pg_summary_store(pg)  if pg else sqlite_summary_store(...)
    task_store     = pg_task_store(pg)     if pg else sqlite_task_store(...)
    ...

That is the composition root being closed to extension: an eighteenth domain
means editing ``build_daemon``, and the branch is easy to get subtly wrong
because there is nothing to compare it against.

Worse, **two competing policies coexisted 80 lines apart**. Most stores used the
bare conditional above and therefore had no failover at all; three
(visuals, feedback, todos) were additionally wrapped in ``RoutedStore`` so a
Postgres outage buffered to SQLite. Nothing recorded which was which, so
"does this write survive a Postgres blip?" had a per-store answer that existed
only as the difference between two code shapes.

Now the backend is resolved once, in ``__init__``, and failover is a **declared
policy** read from :mod:`yuyutsava.storage.domains` rather than a wiring
accident. Adding a domain is a method here plus a registry entry — no edit to
the composition root.

Deliberately NOT a DI container. It is a plain object with one obvious method
per store, so the wiring stays greppable and reads top to bottom. ADR-003
rejects an implicit container for the same reason.
"""

from __future__ import annotations

from dataclasses import dataclass

import logging
from typing import Any

from yuyutsava.storage.backend import StorageSettings
from yuyutsava.storage.domains import BY_TABLE, Failover
from yuyutsava.storage.paths import state_db_path

logger = logging.getLogger("yuyutsava.storage.factory")


@dataclass(frozen=True)
class ContextStores:
    """The context-controller stores for one agent stack.

    ``transcript_index`` is ``None`` without pgvector; callers check it.
    """

    artifacts: Any
    summaries: Any
    transcripts: Any
    transcript_index: Any | None


class StoreFactory:
    """Builds every domain store for the resolved backend.

    Args:
        storage: backend selection + Postgres connection settings.
        pg_pool: live pool in Postgres mode, ``None`` for SQLite. This single
            value is the ONLY backend decision in the system.
        health: shared :class:`~yuyutsava.storage.routing.health.StorageHealth`.
            Required for spillover; without it every domain falls back to
            ``Failover.RAISE`` regardless of what it declares, because there is
            nothing to mark degraded or to trigger reconciliation.
        embedder: pgvector embedder; ``None`` disables semantic features.
    """

    def __init__(
        self,
        storage: StorageSettings,
        *,
        pg_pool: Any | None = None,
        health: Any | None = None,
        embedder: Any | None = None,
    ) -> None:
        self._storage = storage
        self._pg = pg_pool
        self._health = health
        self._embedder = embedder

    # -- backend ----------------------------------------------------------

    @property
    def is_postgres(self) -> bool:
        """The one backend decision. Everything else reads this."""
        return self._pg is not None

    @property
    def pg_pool(self) -> Any | None:
        return self._pg

    @property
    def embedder(self) -> Any | None:
        return self._embedder

    def _db(self):
        return state_db_path()

    def _wrap(self, table: str, pg_store: Any, sqlite_store: Any, *, name: str) -> Any:
        """Apply the domain's declared failover policy.

        ``SPILLOVER`` -> ``RoutedStore(pg, sqlite, health)``: Postgres serves
        while healthy, a runtime error marks the process degraded and re-runs
        the same call against the SQLite buffer, and the Reconciler drains it
        back on recovery.

        ``RAISE`` (the default) -> the Postgres store bare, so the error reaches
        the caller.

        On the SQLite backend there is nothing to fail over *from*, so the
        SQLite store is returned either way.
        """
        if not self.is_postgres:
            return sqlite_store

        domain = BY_TABLE.get(table)
        policy = domain.failover if domain else Failover.RAISE
        if policy is not Failover.SPILLOVER:
            return pg_store
        if self._health is None:
            logger.warning(
                "%s declares spillover failover but no StorageHealth was supplied; "
                "falling back to raise-on-error", name,
            )
            return pg_store

        from yuyutsava.storage.routing.facade import RoutedStore

        return RoutedStore(pg_store, sqlite_store, self._health, name=name)

    # -- context controller ------------------------------------------------

    def artifacts(self, *, semantic_recall: bool = True) -> Any:
        from yuyutsava.context.artifacts_unified import (
            pg_artifact_store, sqlite_artifact_store,
        )

        if self.is_postgres:
            store = pg_artifact_store(
                self._pg, embedder=self._embedder, semantic_recall=semantic_recall
            )
            if store.supports_recall:
                logger.info("  ctx recall: pgvector artifact index enabled (ctx_recall)")
            return store
        return sqlite_artifact_store(self._db())

    def summaries(self) -> Any:
        from yuyutsava.context.summary_store_unified import (
            pg_summary_store, sqlite_summary_store,
        )

        return (
            pg_summary_store(self._pg) if self.is_postgres
            else sqlite_summary_store(self._db())
        )

    def transcripts(self) -> Any:
        from yuyutsava.context.transcript_store_unified import (
            pg_transcript_store, sqlite_transcript_store,
        )

        return (
            pg_transcript_store(self._pg) if self.is_postgres
            else sqlite_transcript_store(self._db())
        )

    def transcript_index(self) -> Any | None:
        """pgvector index over past turns, or ``None`` without one.

        ``None`` is a real answer, not a failure: the transcript RAG middleware
        checks for it and no-ops. Built here because all three stack assemblers
        (CLI, tinker, daemon) needed the same two-condition guard —
        ``pg_pool is not None and embedder is not None`` — and a fourth caller
        getting one of the two wrong would silently lose recall.
        """
        if self._pg is None or self._embedder is None:
            return None
        from yuyutsava.context.transcript_index import PgTranscriptIndex

        return PgTranscriptIndex(self._pg, embedder=self._embedder)

    def context_stores(self, *, semantic_recall: bool = True) -> "ContextStores":
        """The four stores every agent stack needs, selected once.

        Phase 3 step 3.5. The CLI, the tinker bundle and the daemon each wrote
        out the same ``if pg_pool is not None:`` branch over
        artifacts/summaries/transcripts, plus a separate two-condition guard for
        the transcript index. Three copies of one decision, and the daemon's copy
        already lived here — so the other two now call this instead.

        These stay PG-primary / SQLite-fallback-at-boot rather than
        ``RoutedStore``: they are written only INSIDE a checkpointed turn, and
        the checkpointer is also Postgres, so a turn that could not reach
        Postgres has already failed. Spillover applies to the REST-path stores
        (feedback, visuals) instead.
        """
        return ContextStores(
            artifacts=self.artifacts(semantic_recall=semantic_recall),
            summaries=self.summaries(),
            transcripts=self.transcripts(),
            transcript_index=self.transcript_index(),
        )

    def voice(self) -> Any:
        from yuyutsava.storage.voice_store_unified import pg_voice_store, sqlite_voice_store

        return (
            pg_voice_store(self._pg) if self.is_postgres
            else sqlite_voice_store(self._db())
        )

    # -- daemon bookkeeping -------------------------------------------------

    def tasks(self) -> Any:
        from yuyutsava.daemon.task_store_unified import pg_task_store, sqlite_task_store

        return pg_task_store(self._pg) if self.is_postgres else sqlite_task_store(self._db())

    def usage(self) -> Any:
        from yuyutsava.daemon.usage import PgUsageStore, SqliteUsageStore

        return PgUsageStore(self._pg) if self.is_postgres else SqliteUsageStore(self._db())

    # -- semantic ----------------------------------------------------------

    def memory(self, settings: Any) -> Any | None:
        """``None`` when memory is disabled; SQLite keyword fallback without pgvector."""
        from yuyutsava.memory.store_unified import pg_memory_store, sqlite_memory_store

        if not settings.enabled:
            return None
        if self.is_postgres and self._embedder is not None:
            logger.info("  memory    : pgvector (embed=%s)", settings.embed_model)
            return pg_memory_store(
                self._pg, self._embedder,
                min_score=settings.min_score,
                dedup_threshold=settings.dedup_threshold,
            )
        logger.info("  memory    : sqlite keyword fallback (no embeddings)")
        return sqlite_memory_store(self._db())

    def skills(self, settings: Any) -> Any:
        from yuyutsava.skills.store_unified import pg_skill_store, sqlite_skill_store

        if self.is_postgres and self._embedder is not None:
            return pg_skill_store(self._pg, self._embedder, min_score=settings.min_score)
        return sqlite_skill_store(self._db())

    # -- REST-path stores (spillover candidates) ---------------------------

    def visuals(self) -> Any:
        from yuyutsava.visuals.store_unified import pg_visual_store, sqlite_visual_store

        return self._wrap(
            "visual_artifacts",
            pg_visual_store(self._pg) if self.is_postgres else None,
            sqlite_visual_store(self._db()),
            name="visual",
        )

    def feedback(self) -> Any:
        from yuyutsava.storage.feedback_store_unified import (
            pg_feedback_store, sqlite_feedback_store,
        )

        return self._wrap(
            "message_feedback",
            pg_feedback_store(self._pg) if self.is_postgres else None,
            sqlite_feedback_store(self._db()),
            name="feedback",
        )

    def todos(self) -> Any:
        from yuyutsava.todoboard.store_unified import pg_todo_store, sqlite_todo_store

        return self._wrap(
            "todo_cards",
            pg_todo_store(self._pg) if self.is_postgres else None,
            sqlite_todo_store(self._db()),
            name="todo",
        )

    # -- events -------------------------------------------------------------

    def events(self) -> Any:
        """The events ``Store`` facade; owns its own SQLite spillover buffer."""
        from yuyutsava.storage.events import Store

        return Store.for_backend(self._storage, self._pg, self._health)


__all__ = ["StoreFactory"]
