"""The Postgres runtime errors that trigger spillover to the SQLite buffer.

These are the *transient connectivity* failures — the server went away, the
socket broke, the pool couldn't hand out a connection in time. They mean
"Postgres is unreachable right now", not "this statement is wrong". Programming
errors (bad SQL, constraint violations) are deliberately NOT in this set: those
must surface, not get silently buffered.
"""

from __future__ import annotations

import psycopg
import psycopg_pool

# Caught by RoutedStore to flip to the SQLite buffer + mark the process degraded.
PG_RUNTIME_ERRORS: tuple[type[BaseException], ...] = (
    psycopg.OperationalError,
    psycopg.InterfaceError,
    psycopg_pool.PoolTimeout,
)

__all__ = ["PG_RUNTIME_ERRORS"]
