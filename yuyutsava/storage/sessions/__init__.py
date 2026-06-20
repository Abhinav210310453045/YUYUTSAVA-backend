"""Sessions storage layer.

Public exports:

- :class:`SessionStore`            — Protocol; swap backends by pointing the
                                     factory at a different implementation.
- :class:`SqliteSessionStore`      — SQLite/aiosqlite implementation.
- :class:`PgSessionStore`          — Postgres implementation (durable, JOINable).
- :class:`SessionNotFound`         — raised by ``get()`` when the id is unknown.
- :class:`SessionsSettings`        — backend + DB path + busy-timeout tuning.
- :func:`build_checkpointer`       — async ctxmgr yielding the LangGraph saver.
- :func:`get_default_session_store` — process-wide default singleton (backend-aware).
- :func:`set_default_session_store` — override the singleton (daemon pool injection).

``Session`` itself lives in :mod:`yuyutsava.storage.models` because it is a
cross-cutting record returned by reads across several store interfaces.
"""

from yuyutsava.storage.sessions.checkpointer import build_checkpointer
from yuyutsava.storage.sessions.config import SessionsSettings
from yuyutsava.storage.sessions.pg_impl import PgSessionStore
from yuyutsava.storage.sessions.sqlite_impl import (
    SqliteSessionStore,
    get_default_session_store,
    set_default_session_store,
)
from yuyutsava.storage.sessions.store import SessionNotFound, SessionStore

__all__ = [
    "PgSessionStore",
    "SessionNotFound",
    "SessionStore",
    "SessionsSettings",
    "SqliteSessionStore",
    "build_checkpointer",
    "get_default_session_store",
    "set_default_session_store",
]
