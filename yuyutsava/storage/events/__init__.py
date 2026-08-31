"""Events storage: event payloads, proposals, decisions, consent rules, quotas.

Phase 2 split the old monolith into per-domain ABCs (:mod:`abc`) each with a
SQLite twin (:mod:`sqlite_backend`) and a Postgres twin (:mod:`pg_stores`).
:class:`Store` is now a thin composition facade (:mod:`store`) re-exposing the
historical method names and routing them — through
:class:`~yuyutsava.storage.routing.RoutedStore` on the Postgres backend (with a
SQLite spillover buffer) — so call sites barely changed.

Public re-exports keep the import path stable:
``from yuyutsava.storage.events import Store, Proposal, ConsentRule``.
"""

from yuyutsava.storage.events.store import Store
from yuyutsava.storage.models import (
    ConsentRule,
    Decision,
    EventRecord,
    Proposal,
)

__all__ = [
    "ConsentRule",
    "Decision",
    "EventRecord",
    "Proposal",
    "Store",
]
