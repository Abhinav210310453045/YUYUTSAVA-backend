"""Spillover routing: Postgres is the sole writer when healthy; SQLite is a
transparent write *buffer* when Postgres is down.

On a Postgres runtime error a write is redirected to the SQLite twin and the
process is marked *degraded* (:mod:`health`). A background ``SELECT 1`` probe
clears the flag on recovery and :mod:`reconcile` drains the buffered SQLite
rows back into Postgres (idempotent ``INSERT ... ON CONFLICT DO NOTHING``) and
then **deletes** them from SQLite, so a row never lives in both places.

Pure-SQLite mode (no Postgres configured) never routes through here — SQLite is
the permanent primary.
"""

from yuyutsava.storage.routing.errors import PG_RUNTIME_ERRORS
from yuyutsava.storage.routing.facade import RoutedStore
from yuyutsava.storage.routing.health import StorageHealth

__all__ = ["PG_RUNTIME_ERRORS", "RoutedStore", "StorageHealth"]
