"""Which session store a process uses — chosen, not cached.

Postgres mode (``YUYUTSAVA_STORAGE_BACKEND=postgres`` or
``YUYUTSAVA_SESSIONS_BACKEND=postgres``) gives a :class:`PgSessionStore`, so the
CLI and the daemon share one durable, JOINable ``sessions`` table; otherwise the
SQLite twin in ``~/.yuyutsava/sessions.db`` (cross-process coordination via
SQLite's WAL lock).

A function, not a process singleton: the daemon builds one at boot (on its own
pool) and hands it to everything that needs it; a CLI command builds its own.
"""

from __future__ import annotations

from typing import Any

from yuyutsava.storage.backend import StorageSettings
from yuyutsava.storage.sessions.config import SessionsSettings
from yuyutsava.storage.sessions.pg_impl import PgSessionStore
from yuyutsava.storage.sessions.sqlite_impl import SqliteSessionStore
from yuyutsava.storage.sessions.store import SessionStore


def session_store_for(
    storage: StorageSettings | None = None, *, pool: Any | None = None
) -> SessionStore:
    """The session store for *storage* (the environment's when omitted).

    ``pool`` is a live ``PgPool`` to share; without one a Postgres store opens
    its own connections.
    """
    storage = storage or StorageSettings.from_env()
    sessions = SessionsSettings.from_env()
    if storage.is_postgres() or sessions.backend == "postgres":
        return PgSessionStore(storage, pool=pool)
    return SqliteSessionStore(sessions.db_path, busy_timeout_ms=sessions.busy_timeout_ms)
