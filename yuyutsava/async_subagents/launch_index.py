"""Maps a background subagent task back to the conversation that launched it.

deepagents' ``start_async_task`` creates the sub-thread with no parent metadata,
and the :class:`~yuyutsava.async_subagents.watcher.AsyncTaskHealthWatcher`
discovers sub-threads by *polling* — so neither knows which master turn (or which
user-facing channel) spawned a given task.

The orchestrator loop *does* know: while running on a known ``thread_id`` with a
known origin channel it streams the ``start_async_task`` tool result, which
carries the new ``task_id``. It records that link here. The completion bridge
(:mod:`yuyutsava.daemon.bootstrap`) reads it to wake the master on the *original*
thread and route the reply back to the surface that started the work.

In-memory only (single process). Bounded LRU so a long-lived daemon can't grow
it without limit. Thread-safe (mirrors :class:`SessionOriginMap`'s locking) so
the async orchestrator loop and the async watcher can share one instance.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from dataclasses import dataclass

# deepagents' start_async_task returns "Launched async subagent. task_id: <id>".
# We sniff the id out of the tool-result preview the orchestrator already streams.
_TASK_ID_RE = re.compile(r"task_id:\s*(\S+)")


def parse_async_task_id(text: str | None) -> str | None:
    """Extract the ``task_id`` from a ``start_async_task`` tool result, or None."""
    if not text:
        return None
    m = _TASK_ID_RE.search(text)
    if not m:
        return None
    return m.group(1).strip().rstrip(".,)")


@dataclass(frozen=True)
class LaunchRecord:
    """One launch → conversation link."""

    task_id: str
    parent_thread_id: str
    origin: str | None = None  # originating channel name (e.g. "cli", "web")


class LaunchIndex:
    """Thread-safe, bounded ``task_id -> LaunchRecord`` map."""

    def __init__(self, *, max_entries: int = 1024) -> None:
        self._lock = threading.Lock()
        self._map: "OrderedDict[str, LaunchRecord]" = OrderedDict()
        self._max = max_entries

    def record(
        self, task_id: str, parent_thread_id: str, origin: str | None = None
    ) -> None:
        if not task_id or not parent_thread_id:
            return
        with self._lock:
            self._map[task_id] = LaunchRecord(task_id, parent_thread_id, origin)
            self._map.move_to_end(task_id)
            while len(self._map) > self._max:
                self._map.popitem(last=False)

    def get(self, task_id: str) -> LaunchRecord | None:
        if not task_id:
            return None
        with self._lock:
            return self._map.get(task_id)
