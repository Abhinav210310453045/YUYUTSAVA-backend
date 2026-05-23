"""Canonical filesystem paths for every persisted yuyutsava artifact.

Single place to look up "where does X live on disk?". Every store, sweeper,
and introspector resolves its target through one of these functions so a
test fixture can override the location with one env var.

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
    """Per-user state directory. Created on first access.

    Holds every SQLite file and the ``blobs/`` subtree. Override with
    ``YUYUTSAVA_HOME``.
    """
    raw = os.environ.get("YUYUTSAVA_HOME", "").strip()
    p = Path(raw).expanduser() if raw else Path.home() / ".yuyutsava"
    p.mkdir(parents=True, exist_ok=True)
    return p


def sessions_db_path() -> Path:
    """SQLite file backing the CLI session index. Parent dir is created."""
    raw = os.environ.get("YUYUTSAVA_SESSIONS_DB", "").strip()
    p = Path(raw).expanduser() if raw else state_dir() / "sessions.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def state_db_path() -> Path:
    """SQLite file backing events, proposals, decisions, rules, quotas, prefs.

    Currently owned by ``yuyutsava.events.store.Store``; in Step 2 the stores
    split into ``storage/events/`` and ``storage/prefs.py`` but the DB file
    stays the same so existing data is preserved.
    """
    raw = os.environ.get("YUYUTSAVA_STATE_DB", "").strip()
    p = Path(raw).expanduser() if raw else state_dir() / "state.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def checkpoints_db_path() -> Path:
    """SQLite file backing the LangGraph ``AsyncSqliteSaver`` checkpointer."""
    raw = os.environ.get("YUYUTSAVA_CHECKPOINTS_DB", "").strip()
    p = Path(raw).expanduser() if raw else state_dir() / "checkpoints.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def interrupts_db_path() -> Path:
    """SQLite file backing the cross-front HITL interrupt audit log."""
    raw = os.environ.get("YUYUTSAVA_INTERRUPTS_DB", "").strip()
    p = Path(raw).expanduser() if raw else state_dir() / "interrupts.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def blobs_dir() -> Path:
    """Root directory for source-produced blobs (webcam JPEGs, audio clips)."""
    raw = os.environ.get("YUYUTSAVA_BLOBS_DIR", "").strip()
    p = Path(raw).expanduser() if raw else state_dir() / "blobs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def events_config_path() -> Path:
    """Repo-local path for ``events_config.json``.

    Not under ``state_dir()`` because the source registry config is a
    project artifact, not user runtime state. Sits next to the events
    package so a fresh clone has working defaults.
    """
    return Path(__file__).resolve().parent.parent / "events" / "events_config.json"
