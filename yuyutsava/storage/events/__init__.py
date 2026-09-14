"""Events storage: event payloads, proposals, decisions, consent rules and quotas.

Each domain has an abstract store (:mod:`~yuyutsava.storage.events.abc`) with SQLite
and Postgres implementations. :class:`~yuyutsava.storage.events.store.Store` is the
composition facade that routes each call to the right domain store — through a
:class:`~yuyutsava.storage.routing.facade.RoutedStore` on the Postgres backend, with
SQLite as the spillover buffer.

``Store``, ``Proposal`` and ``ConsentRule`` are re-exported because other modules
import them from here; import anything else from the module that owns it.
"""

from yuyutsava.storage.events.store import Store
from yuyutsava.storage.models import ConsentRule, Proposal

__all__ = ["ConsentRule", "Proposal", "Store"]
