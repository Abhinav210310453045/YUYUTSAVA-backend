"""Sessions storage layer.

Public exports:

- :class:`SessionStore`            — Protocol; swap backends by pointing the
                                     factory at a different implementation.
- :class:`SqliteSessionStore`      — SQLite/aiosqlite implementation.
- :class:`PgSessionStore`          — Postgres implementation (durable, JOINable).
- :class:`SessionNotFound`         — raised by ``get()`` when the id is unknown.
- :class:`SessionsSettings`        — backend + DB path + busy-timeout tuning.
- :func:`build_checkpointer`       — async ctxmgr yielding the LangGraph saver.
- :func:`session_store_for`         — the store a process should use (backend-aware).

``Session`` itself lives in :mod:`yuyutsava.storage.models` because it is a
cross-cutting record returned by reads across several store interfaces.
"""

from yuyutsava.storage.sessions.checkpointer import build_checkpointer
from yuyutsava.storage.sessions.config import SessionsSettings
from yuyutsava.storage.sessions.pg_impl import PgSessionStore
from yuyutsava.storage.sessions.select import session_store_for
from yuyutsava.storage.sessions.sqlite_impl import SqliteSessionStore
from yuyutsava.storage.sessions.store import SessionNotFound, SessionStore

__all__ = [
    "PgSessionStore",
    "SessionNotFound",
    "SessionStore",
    "SessionsSettings",
    "SqliteSessionStore",
    "build_checkpointer",
    "session_store_for",
]
