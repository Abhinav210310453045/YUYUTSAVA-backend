"""Canonical filesystem paths for every persisted yuyutsava artifact.

Single place to look up "where does X live on disk?". Every store, sweeper,
and introspector resolves its target through one of these functions so a
test fixture can override the location with one env var.

Path-returning helpers are **pure** — they compute and return paths without
touching the filesystem. Directory materialization is the caller's job
(typically once at sync startup via :func:`ensure_state_dirs`). Async
stores additionally mkdir-on-open via ``asyncio.to_thread`` as defence in
depth; doing the sync mkdir inside ``async def`` would trip
``blockbuster`` when ``langgraph dev`` is in the process.

Env overrides
-------------
- ``YUYUTSAVA_HOME``           override state dir (default: ``~/.yuyutsava``)
- ``YUYUTSAVA_SESSIONS_DB``    override sessions.db path
- ``YUYUTSAVA_STATE_DB``       override state.db path (events/proposals/rules/quotas/prefs)
- ``YUYUTSAVA_CHECKPOINTS_DB`` override checkpoints.db path (LangGraph saver)
- ``YUYUTSAVA_INTERRUPTS_DB``  override interrupts.db path (HITL audit)
- ``YUYUTSAVA_BLOBS_DIR``      override blobs/ root (webcam frames, audio clips)
"""

from __future__ import annotations

import os
from pathlib import Path


def state_dir() -> Path:
    """Per-user state directory. Pure path; create via :func:`ensure_state_dirs`.

    Holds every SQLite file and the ``blobs/`` subtree. Override with
    ``YUYUTSAVA_HOME``.
    """
    raw = os.environ.get("YUYUTSAVA_HOME", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".yuyutsava"


def sessions_db_path() -> Path:
    """SQLite file backing the CLI session index."""
    raw = os.environ.get("YUYUTSAVA_SESSIONS_DB", "").strip()
    return Path(raw).expanduser() if raw else state_dir() / "sessions.db"


def state_db_path() -> Path:
    """SQLite file backing events, proposals, decisions, rules, quotas, prefs.

    Currently owned by ``yuyutsava.events.store.Store``; in Step 2 the stores
    split into ``storage/events/`` and ``storage/prefs.py`` but the DB file
    stays the same so existing data is preserved.
    """
    raw = os.environ.get("YUYUTSAVA_STATE_DB", "").strip()
    return Path(raw).expanduser() if raw else state_dir() / "state.db"


def checkpoints_db_path() -> Path:
    """SQLite file backing the LangGraph ``AsyncSqliteSaver`` checkpointer."""
    raw = os.environ.get("YUYUTSAVA_CHECKPOINTS_DB", "").strip()
    return Path(raw).expanduser() if raw else state_dir() / "checkpoints.db"


def interrupts_db_path() -> Path:
    """SQLite file backing the cross-front HITL interrupt audit log."""
    raw = os.environ.get("YUYUTSAVA_INTERRUPTS_DB", "").strip()
    return Path(raw).expanduser() if raw else state_dir() / "interrupts.db"


def blobs_dir() -> Path:
    """Root directory for source-produced blobs (webcam JPEGs, audio clips)."""
    raw = os.environ.get("YUYUTSAVA_BLOBS_DIR", "").strip()
    return Path(raw).expanduser() if raw else state_dir() / "blobs"


def channels_config_path() -> Path:
    """User-state path for ``channels_config.json`` (channel plugins).

    Under ``state_dir()`` (unlike ``events_config_path``) because which
    channels a user enabled — and their params — is per-user runtime
    state, not a project artifact. Override with ``YUYUTSAVA_CHANNELS_CONFIG``.
    """
    raw = os.environ.get("YUYUTSAVA_CHANNELS_CONFIG", "").strip()
    return Path(raw).expanduser() if raw else state_dir() / "channels_config.json"


def events_config_path() -> Path:
    """Repo-local path for ``events_config.json``.

    Not under ``state_dir()`` because the source registry config is a
    project artifact, not user runtime state. Sits next to the events
    package so a fresh clone has working defaults.
    """
    return Path(__file__).resolve().parent.parent / "events" / "events_config.json"


def ensure_state_dirs() -> None:
    """Create every state directory the app writes to. Sync, idempotent.

    Call once from the sync entry point (CLI ``main``, daemon ``main``)
    before ``asyncio.run`` — the path helpers above are pure, so something
    has to create the dirs, and doing it inside the event loop trips
    ``blockbuster`` when the LangGraph dev host is in-process.
    """
    state_dir().mkdir(parents=True, exist_ok=True)
    blobs_dir().mkdir(parents=True, exist_ok=True)
    for p in (
        sessions_db_path(),
        state_db_path(),
        checkpoints_db_path(),
        interrupts_db_path(),
    ):
        p.parent.mkdir(parents=True, exist_ok=True)
