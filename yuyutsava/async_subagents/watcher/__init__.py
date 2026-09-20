"""Watching background subagent runs, and bridging their questions to a person.

:class:`~yuyutsava.async_subagents.watcher.health.AsyncTaskHealthWatcher`
is the whole public surface; :func:`compact_error` is here because the guard
policy reports failures in the same shape.
"""

from __future__ import annotations

from yuyutsava.async_subagents.watcher.health import AsyncTaskHealthWatcher
from yuyutsava.async_subagents.watcher.parsing import compact_error

__all__ = ["AsyncTaskHealthWatcher", "compact_error"]
