"""The registry of persisted domains — one declaration per table.

Phase 2 step 2.4 (ADR-002). Session deletion and TTL sweeping used to be driven
by hand-maintained table lists inside :mod:`yuyutsava.storage.purge`. Adding a
persisted domain meant remembering to edit a list in an unrelated module, and
**forgetting was silent** — the rows simply survived a deletion the user asked
for.

That is not hypothetical. Two domains were found missing on 2026-08-08:

* ``message_feedback`` — stores ``user_text`` / ``assistant_text`` verbatim;
* ``pending_asks`` — stores the agent's question (``title``, ``body``) and the
  user's ``response``.

Both survived "delete this session". Both are now declared here.

How a table gets cleaned up
---------------------------
Not every scoped table is purged the same way, and the difference matters:

``PurgeMode.ROW_DELETE``
    Purge issues ``DELETE FROM <table> WHERE <key> = ...`` directly. The default
    for plain domain rows.

``PurgeMode.STORE_METHOD``
    The row has a side effect beyond the database — an image on disk, a blob —
    so deletion must go through the store's own ``delete_for_thread``. A raw
    table delete would orphan the file.

``PurgeMode.EXTERNAL``
    Something else already owns the deletion: the LangGraph checkpointer, the
    sessions store, the thread-hub drop. Declared so the completeness check can
    see the table is accounted for rather than forgotten.

``PurgeMode.KEEP``
    Deliberately survives session deletion. ``memories`` is the case: facts and
    preferences the user taught the system are not session data.

The point of ``EXTERNAL`` and ``KEEP`` is that "this table is not in the purge
list" becomes a *statement someone made*, not an absence nobody noticed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PurgeMode(str, Enum):
    ROW_DELETE = "row_delete"
    STORE_METHOD = "store_method"
    EXTERNAL = "external"
    KEEP = "keep"


class Backend(str, Enum):
    SQLITE = "sqlite"
    POSTGRES = "postgres"


class Failover(str, Enum):
    """What happens to a write when Postgres is unreachable.

    Recorded per domain because the answer was previously an *accident of
    wiring*: three stores were wrapped in ``RoutedStore`` and the other fourteen
    were not, with nothing stating why (finding ``F-S07``). During an outage a
    todo write silently buffered to SQLite and reconciled later, while a memory
    write from the same agent turn raised — one system, two behaviours, both
    undocumented.
    """

    #: Buffer to the SQLite twin during an outage; the Reconciler drains it back.
    #: For writes that happen OUTSIDE a checkpointed agent turn — REST endpoints,
    #: tool calls — where a raised error loses the write outright.
    SPILLOVER = "spillover"
    #: Let the error propagate. Correct where the caller is inside a checkpointed
    #: turn (LangGraph replays it) or where a stale local copy would be worse
    #: than a visible failure.
    RAISE = "raise"


BOTH = frozenset({Backend.SQLITE, Backend.POSTGRES})
PG_ONLY = frozenset({Backend.POSTGRES})


@dataclass(frozen=True)
class PersistedDomain:
    """One table's lifecycle contract."""

    table: str
    #: Column tying a row to a session's thread. ``None`` = not session-scoped.
    scope_key: str | None
    backends: frozenset[Backend]
    purge: PurgeMode
    #: TTL sweep age in days; ``None`` = never aged out.
    retention_days: int | None = None
    #: What happens to a write when Postgres is unreachable. Only meaningful on
    #: the Postgres backend; SQLite has nothing to fail over from.
    failover: Failover = Failover.RAISE
    note: str = ""

    @property
    def session_scoped(self) -> bool:
        return self.scope_key is not None


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

DOMAINS: tuple[PersistedDomain, ...] = (
    # -- Conversation content ------------------------------------------------
    PersistedDomain("transcript_messages", "thread_id", BOTH, PurgeMode.ROW_DELETE),
    PersistedDomain("transcript_chunks", "thread_id", PG_ONLY, PurgeMode.ROW_DELETE,
                    note="pgvector RAG index; no SQLite equivalent."),
    PersistedDomain("thread_summaries", "thread_id", BOTH, PurgeMode.ROW_DELETE),
    PersistedDomain("voice_messages", "thread_id", BOTH, PurgeMode.ROW_DELETE,
                    note="On-disk audio clips are removed separately by "
                         "delete_thread_voice_blobs()."),
    PersistedDomain("artifacts", "thread_id", BOTH, PurgeMode.ROW_DELETE),
    PersistedDomain("artifact_chunks", "thread_id", PG_ONLY, PurgeMode.ROW_DELETE,
                    note="pgvector index over offloaded tool results."),

    # -- Run bookkeeping -----------------------------------------------------
    PersistedDomain("tasks", "thread_id", BOTH, PurgeMode.ROW_DELETE),
    PersistedDomain("llm_usage", "thread_id", BOTH, PurgeMode.ROW_DELETE),
    PersistedDomain("proposals", "session_id", BOTH, PurgeMode.ROW_DELETE),
    PersistedDomain("decisions", "session_id", BOTH, PurgeMode.ROW_DELETE),
    PersistedDomain("interrupts", "thread_id", PG_ONLY, PurgeMode.ROW_DELETE,
                    note="SQLite keeps interrupts in a dedicated DB file, "
                         "purged by its own step rather than the bulk delete."),

    # -- Found missing 2026-08-08; both held user-visible text ---------------
    PersistedDomain("message_feedback", "thread_id", BOTH, PurgeMode.STORE_METHOD,
                    failover=Failover.SPILLOVER,
                    note="Holds user_text/assistant_text verbatim. Outside the "
                         "thread-hub FK graph, so neither the bulk delete nor "
                         "the PG cascade reached it. Spillover: written by the "
                         "REST 👍/👎 endpoint, outside any checkpointed turn, so "
                         "a raised error loses the rating outright."),
    PersistedDomain("pending_asks", "thread_id", BOTH, PurgeMode.STORE_METHOD,
                    note="Holds the agent's question (title/body) and the user's "
                         "response. Never purged and never swept before this."),

    # -- Rows with on-disk side effects --------------------------------------
    PersistedDomain("visual_artifacts", "thread_id", BOTH, PurgeMode.STORE_METHOD,
                    retention_days=30, failover=Failover.SPILLOVER,
                    note="Row stores an absolute path to a PNG; a raw table "
                         "delete would orphan the file. Spillover: the /visuals "
                         "API writes outside a checkpointed turn."),

    # -- Owned by another deletion path --------------------------------------
    PersistedDomain("checkpoints", "thread_id", BOTH, PurgeMode.EXTERNAL,
                    note="LangGraph checkpointer: saver.adelete_thread()."),
    PersistedDomain("checkpoint_blobs", "thread_id", BOTH, PurgeMode.EXTERNAL,
                    note="LangGraph checkpointer."),
    PersistedDomain("checkpoint_writes", "thread_id", BOTH, PurgeMode.EXTERNAL,
                    note="LangGraph checkpointer."),
    PersistedDomain("sessions", "thread_id", BOTH, PurgeMode.EXTERNAL,
                    note="Deleted last, by the session store, so a failure "
                         "above leaves the session listed and retryable."),
    PersistedDomain("threads", "thread_id", PG_ONLY, PurgeMode.EXTERNAL,
                    note="The PG thread hub; dropped after its children."),

    # -- Deliberately survives -----------------------------------------------
    PersistedDomain("memories", None, BOTH, PurgeMode.KEEP,
                    note="Facts and preferences the user taught the system are "
                         "not session data. The source_thread_id FK is SET NULL."),

    # -- Not session-scoped --------------------------------------------------
    PersistedDomain("todo_cards", None, BOTH, PurgeMode.KEEP,
                    failover=Failover.SPILLOVER,
                    note="The board outlives any chat that touched it. "
                         "Spillover: the /todos router and todo_* tools write "
                         "outside a checkpointed turn; drains via "
                         "CONTENT_TABLE_SPECS."),
    PersistedDomain("todo_notes", None, BOTH, PurgeMode.KEEP),
    PersistedDomain("todo_objectives", None, BOTH, PurgeMode.KEEP),
    PersistedDomain("todo_events", None, BOTH, PurgeMode.KEEP),
    PersistedDomain("todo_attachments", None, BOTH, PurgeMode.KEEP),
    PersistedDomain("todo_note_chunks", None, PG_ONLY, PurgeMode.KEEP),
    PersistedDomain("skills", None, BOTH, PurgeMode.KEEP),
    PersistedDomain("consent_rules", None, BOTH, PurgeMode.KEEP),
    PersistedDomain("consent_grants", None, BOTH, PurgeMode.KEEP),
    PersistedDomain("user_prefs", None, BOTH, PurgeMode.KEEP),
    PersistedDomain("tool_call_counters", None, BOTH, PurgeMode.KEEP),
    PersistedDomain("event_payloads", None, BOTH, PurgeMode.KEEP,
                    retention_days=7,
                    note="Aged out by the TTL sweeper, not by session delete."),
)

BY_TABLE: dict[str, PersistedDomain] = {d.table: d for d in DOMAINS}

#: Infrastructure tables no domain owns — excluded from completeness checks.
INFRASTRUCTURE_TABLES: frozenset[str] = frozenset({
    "schema_meta", "checkpoint_migrations", "embeddings",
})


def purge_tables(backend: Backend) -> tuple[tuple[str, str], ...]:
    """``(table, scope_key)`` pairs purge should DELETE from, for *backend*.

    Replaces the hand-maintained ``_STATE_TABLES`` / ``_PG_CHILD_TABLES``
    literals. Only ``ROW_DELETE`` domains appear: ``STORE_METHOD`` ones go
    through their store (so on-disk side effects are handled), and
    ``EXTERNAL`` / ``KEEP`` are someone else's business.
    """
    return tuple(
        (d.table, d.scope_key)
        for d in DOMAINS
        if d.purge is PurgeMode.ROW_DELETE
        and d.scope_key is not None
        and backend in d.backends
    )


def session_scoped_tables(backend: Backend) -> frozenset[str]:
    """Every table carrying session data on *backend*, however it is cleaned up."""
    return frozenset(
        d.table for d in DOMAINS if d.session_scoped and backend in d.backends
    )


def unaccounted(tables: frozenset[str]) -> frozenset[str]:
    """Live tables the registry does not describe.

    Feed this real schema introspection: anything it returns is a table nobody
    declared a lifecycle for, which is exactly how ``message_feedback`` and
    ``pending_asks`` came to survive session deletion.
    """
    return frozenset(tables) - set(BY_TABLE) - INFRASTRUCTURE_TABLES


__all__ = [
    "BY_TABLE", "BOTH", "PG_ONLY", "DOMAINS", "INFRASTRUCTURE_TABLES",
    "Backend", "PersistedDomain", "PurgeMode",
    "purge_tables", "session_scoped_tables", "unaccounted",
]
