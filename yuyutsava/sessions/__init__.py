"""Persistent CLI sessions — survive Ctrl+C, terminal close, crash, power loss.

Public surface (stable):

- :class:`Session` — frozen dataclass mirroring one row.
- :class:`SessionStore` — Protocol; swap impls by pointing at a different factory.
- :func:`get_default_session_store` — process-wide default (sqlite under ``~/.yuyutsava/sessions.db``).
- :func:`build_checkpointer` — async context-manager yielding a ``BaseCheckpointSaver``.
- :func:`run_session` — crash-safe runner used by the CLI.
- :class:`SessionsSettings` — tunables (db path, backend, busy-timeout).
"""

from yuyutsava.sessions.checkpointer import build_checkpointer
from yuyutsava.sessions.config import SessionsSettings
from yuyutsava.sessions.models import Session
from yuyutsava.sessions.runner import ResumeFailed, run_session
from yuyutsava.sessions.sqlite_store import (
    SqliteSessionStore,
    get_default_session_store,
    mint_thread_id,
)
from yuyutsava.sessions.store import SessionNotFound, SessionStore

__all__ = [
    "ResumeFailed",
    "Session",
    "SessionStore",
    "SessionNotFound",
    "SessionsSettings",
    "SqliteSessionStore",
    "build_checkpointer",
    "get_default_session_store",
    "mint_thread_id",
    "run_session",
]
