"""Composition facade over the per-domain events stores.

Historically one monolithic ``Store`` owned every table on a single SQLite
connection. Phase 2 split each table into an ABC (:mod:`abc`) with a SQLite
twin (:mod:`sqlite_backend`) and a Postgres twin (:mod:`pg_stores`). This
:class:`Store` keeps the *old method names* and threads them to the right
domain store so call sites barely change — but every DB method is now ``async``
(a Postgres call must never block the loop) with one exception:
:meth:`list_consent_grants`, pinned synchronous by the ``ConsentStore``
Protocol and served from a cache filled at :meth:`start`.

Construction:

- ``Store()`` / ``Store(db_path)`` — SQLite-only (CLI ``prefs`` subcommand,
  tests). SQLite is the permanent primary.
- ``Store.for_backend(storage, pg_pool, health)`` — on the Postgres backend
  each domain is a :class:`~yuyutsava.storage.routing.RoutedStore` wrapping the
  Pg twin (primary) over the SQLite twin (failover buffer); on the SQLite
  backend it is the plain SQLite twin.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from yuyutsava.storage.events.schema import SCHEMA_SQL, SCHEMA_VERSION, migrate  # noqa: F401
from yuyutsava.storage.events.sqlite_backend import (
    SqliteConsentGrantStore,
    SqliteConsentRuleStore,
    SqliteDecisionStore,
    SqliteEventsBackend,
    SqliteEventStore,
    SqlitePendingAskStore,
    SqlitePrefsBackend,
    SqliteProposalStore,
    SqliteToolCounterStore,
)
from yuyutsava.storage.models import ConsentRule, Decision, EventRecord, Proposal

if TYPE_CHECKING:
    from yuyutsava.consent.models import Grant
    from yuyutsava.storage.backend import StorageSettings
    from yuyutsava.storage.pg.pool import PgPool
    from yuyutsava.storage.routing.health import StorageHealth

logger = logging.getLogger("yuyutsava.storage.events.store")


class Store:
    """Backend-agnostic facade re-exposing the historical ``Store`` surface."""

    def __init__(self, db_path: Path | None = None, *, write_queue_size: int = 1024) -> None:
        # ``write_queue_size`` retained for call-site compatibility (the writer
        # queue is gone; writes serialise inside the SQLite backend now).
        self._backend = SqliteEventsBackend(db_path)
        self.db_path = self._backend._db_path
        self._events = SqliteEventStore(self._backend)
        self._proposals = SqliteProposalStore(self._backend)
        self._decisions = SqliteDecisionStore(self._backend)
        self._consent_rules = SqliteConsentRuleStore(self._backend)
        self._counters = SqliteToolCounterStore(self._backend)
        self._prefs = SqlitePrefsBackend(self._backend)
        self._grants = SqliteConsentGrantStore(self._backend)
        self._asks = SqlitePendingAskStore(self._backend)
        self._grants_cache: list[Grant] = []

    @classmethod
    def for_backend(
        cls,
        storage: "StorageSettings",
        pg_pool: "PgPool | None" = None,
        health: "StorageHealth | None" = None,
    ) -> "Store":
        """Build the facade for the active backend.

        Postgres backend → each domain is a ``RoutedStore`` (Pg primary, SQLite
        buffer). SQLite backend (or no pool) → the SQLite twins built in
        ``__init__`` are kept as the permanent primary.
        """
        self = cls()
        if pg_pool is not None and storage.is_postgres() and health is not None:
            from yuyutsava.storage.events.pg_stores import (
                PgConsentGrantStore,
                PgConsentRuleStore,
                PgDecisionStore,
                PgEventStore,
                PgPendingAskStore,
                PgPrefsBackend,
                PgProposalStore,
                PgToolCounterStore,
            )
            from yuyutsava.storage.routing.facade import RoutedStore

            b = self._backend
            self._events = RoutedStore(PgEventStore(pg_pool), SqliteEventStore(b), health, name="event_payloads")
            self._proposals = RoutedStore(PgProposalStore(pg_pool), SqliteProposalStore(b), health, name="proposals")
            self._decisions = RoutedStore(PgDecisionStore(pg_pool), SqliteDecisionStore(b), health, name="decisions")
            self._consent_rules = RoutedStore(PgConsentRuleStore(pg_pool), SqliteConsentRuleStore(b), health, name="consent_rules")
            self._counters = RoutedStore(PgToolCounterStore(pg_pool), SqliteToolCounterStore(b), health, name="tool_call_counters")
            self._prefs = RoutedStore(PgPrefsBackend(pg_pool), SqlitePrefsBackend(b), health, name="user_prefs")
            self._grants = RoutedStore(PgConsentGrantStore(pg_pool), SqliteConsentGrantStore(b), health, name="consent_grants")
            self._asks = RoutedStore(PgPendingAskStore(pg_pool), SqlitePendingAskStore(b), health, name="pending_asks")
        return self

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Open the SQLite backend (always — it is the failover buffer on PG)
        and pre-load the consent-grant cache so ``list_consent_grants`` stays
        synchronous for the ``ConsentRegistry`` boot read."""
        await self._backend.open()
        try:
            self._grants_cache = await self._grants.load()
        except Exception:  # noqa: BLE001
            logger.exception("events: consent-grant cache preload failed")
            self._grants_cache = []

    async def stop(self) -> None:
        await self._backend.close()

    @property
    def sqlite_backend(self) -> SqliteEventsBackend:
        """The SQLite buffer backend — used by the reconciler to drain it."""
        return self._backend

    # ------------------------------------------------------------------ #
    # event_payloads                                                      #
    # ------------------------------------------------------------------ #

    async def put_event_payload(
        self, *, event_id: str, topic: str, ts: float,
        payload: dict[str, Any], blob_path: str | None = None,
    ) -> None:
        await self._events.put_event_payload(
            event_id=event_id, topic=topic, ts=ts, payload=payload, blob_path=blob_path,
        )

    async def get_event_payload(self, event_id: str) -> EventRecord | None:
        return await self._events.get_event_payload(event_id)

    async def delete_event_payloads_with_blob_prefix(self, prefix: str, older_than_ts: float) -> int:
        return await self._events.delete_event_payloads_with_blob_prefix(prefix, older_than_ts)

    async def delete_event_payloads_older_than(self, older_than_ts: float) -> int:
        return await self._events.delete_event_payloads_older_than(older_than_ts)

    # ------------------------------------------------------------------ #
    # proposals                                                           #
    # ------------------------------------------------------------------ #

    async def put_proposal(self, p: Proposal) -> None:
        await self._proposals.put(p)

    async def get_proposal(self, proposal_id: str) -> Proposal | None:
        return await self._proposals.get(proposal_id)

    async def try_set_proposal_status(
        self, proposal_id: str, *, from_status: str, to_status: str
    ) -> bool:
        return await self._proposals.try_set_status(
            proposal_id, from_status=from_status, to_status=to_status
        )

    # ------------------------------------------------------------------ #
    # pending asks (Tier-2 HITL, durable across restarts)                 #
    # ------------------------------------------------------------------ #

    async def put_pending_ask(self, record: dict[str, Any]) -> None:
        """Record an ask BEFORE it is broadcast, so it is never unrecoverable."""
        await self._asks.put(record)

    async def resolve_pending_ask(
        self, ask_id: str, response: str, *, status: str = "answered"
    ) -> bool:
        """Mark an ask answered; False when another surface got there first."""
        return await self._asks.resolve(ask_id, response, status=status)

    async def list_pending_asks(self, limit: int = 200) -> list[dict[str, Any]]:
        return await self._asks.list_pending(limit)

    async def get_pending_ask(self, ask_id: str) -> dict[str, Any] | None:
        return await self._asks.get(ask_id)

    # ------------------------------------------------------------------ #
    # decisions                                                           #
    # ------------------------------------------------------------------ #

    async def put_decision(
        self, *, proposal_id: str | None, event_id: str, outcome: str,
        action_summary: str | None = None, ts: float | None = None,
        session_id: str | None = None, agent_path: str | None = None,
    ) -> None:
        await self._decisions.put(
            proposal_id=proposal_id, event_id=event_id, outcome=outcome,
            action_summary=action_summary, ts=ts, session_id=session_id, agent_path=agent_path,
        )

    async def list_decisions(self, limit: int = 50, cursor: float | None = None) -> list[Decision]:
        return await self._decisions.list(limit, cursor)

    async def recall(self, topic_glob: str, since_sec: float, limit: int = 20) -> list[dict[str, Any]]:
        return await self._decisions.recall(topic_glob, since_sec, limit)

    # ------------------------------------------------------------------ #
    # consent_rules                                                       #
    # ------------------------------------------------------------------ #

    async def put_consent_rule(self, rule: ConsentRule) -> None:
        await self._consent_rules.put(rule)

    async def list_consent_rules(self) -> list[ConsentRule]:
        return await self._consent_rules.list()

    # ------------------------------------------------------------------ #
    # tool_call_counters                                                  #
    # ------------------------------------------------------------------ #

    async def incr_tool_call(self, tool_name: str, day: str) -> int:
        return await self._counters.incr(tool_name, day)

    async def get_tool_call_count(self, tool_name: str, day: str) -> int:
        return await self._counters.get(tool_name, day)

    # ------------------------------------------------------------------ #
    # user_prefs                                                          #
    # ------------------------------------------------------------------ #

    async def put_pref(self, key: str, value: Any) -> None:
        await self._prefs.put(key, value)

    async def delete_pref(self, key: str) -> None:
        await self._prefs.delete(key)

    async def get_pref(self, key: str, default: Any = None) -> Any:
        return await self._prefs.get(key, default)

    async def list_prefs(self) -> dict[str, Any]:
        return await self._prefs.list()

    # ------------------------------------------------------------------ #
    # consent_grants (yuyutsava.consent.ConsentStore Protocol)            #
    # ------------------------------------------------------------------ #

    async def put_consent_grant(self, grant: "Grant") -> None:
        await self._grants.put(grant)
        self._grants_cache.append(grant)

    async def delete_consent_grant(self, grant_id: str) -> None:
        await self._grants.delete(grant_id)
        self._grants_cache = [g for g in self._grants_cache if g.grant_id != grant_id]

    def list_consent_grants(self) -> list["Grant"]:
        # Persisted (PROJECT/PERSISTENT) grants only, cached at start(). The
        # ConsentRegistry reads this once at boot into its own cache.
        return list(self._grants_cache)
