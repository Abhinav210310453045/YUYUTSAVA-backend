"""Crash-safe CLI session runtime.

This package owns the CLI-side session *runtime* — the
:func:`run_session` lifecycle that wires the agent, checkpointer, and store
together with crash recovery semantics. The session *storage* (DB schema,
SQLite impl, settings) lives in :mod:`yuyutsava.storage.sessions`.

In Step 5 of the restructure, ``runner.py`` will move into
``cli/commands/chat.py`` and this package will be removed entirely. For now
it stays as the CLI's session runtime entry point.

Public exports:

- :func:`run_session` — crash-safe runner invoked by the CLI.
- :class:`ResumeFailed` — raised when ``--resume`` cannot recover state.
"""

from yuyutsava.sessions.runner import ResumeFailed, run_session

__all__ = ["ResumeFailed", "run_session"]
