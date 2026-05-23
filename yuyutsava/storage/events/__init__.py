"""Events storage: event payloads, proposals, decisions, consent rules, quotas.

Backed by a single ``state.db`` with multiple tables. Today everything is
owned by one :class:`Store` class for historical reasons (the daemon
constructs one and threads it through every consumer). The plan calls for
splitting this into per-domain stores (``EventStore``, ``ProposalStore``,
``ConsentRuleStore``, ``QuotaStore``) sharing a common DB owner —
**deferred** until a caller actually needs only one table.

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
