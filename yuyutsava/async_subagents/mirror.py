"""Daemon-scoped mirror of in-flight async subagent tasks.

deepagents' ``AsyncSubAgentMiddleware`` keeps task state in the master's
``async_tasks`` channel — but that channel lives on the master's per-thread
state, and the orchestrator builds a fresh master with a new ``thread_id``
every turn (orchestrator_loop.py:_run_task). So ``async_tasks`` resets between
turns.

The mirror is a parallel daemon-owned record of the same tasks. It's updated
by :class:`AsyncTaskHealthWatcher` (single writer) and read by the orchestrator
loop's turn-start status injector. ``render_block()`` produces the text block
prepended to each master turn so the LLM stays aware of in-flight bg work
across compactions and across fresh threads.

Single-process, in-memory only (v1 scope). Not persisted to disk.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, replace
from typing import Iterable, Literal


MirrorStatus = Literal[
    "queued",
    "running",
    "awaiting_user",
    "success",
    "error",
    "cancelled",
    "interrupted",
    "timeout",
]

# Statuses that won't change again.
TERMINAL_STATUSES: frozenset[str] = frozenset({"success", "error", "cancelled", "timeout"})


@dataclass(frozen=True)
class MirroredTask:
    task_id: str
    agent_name: str           # e.g. "file-organizer-bg"
    graph_id: str             # e.g. "file-organizer"
    instruction: str
    status: str               # MirrorStatus value as plain str (SDK gives str)
    started_at: float
    last_update_at: float
    completed_at: float | None = None
    parent_thread_id: str | None = None    # master turn that started it
    sub_thread_id: str | None = None       # bg graph's thread on lg host
    pending_ask_id: str | None = None
    summary: str | None = None
    error: str | None = None
    origin: str | None = None              # originating channel (e.g. "cli","web")
    # True once the master has been told about this task's completion (via the
    # completion wake-up). Keeps render_block() from re-reporting a finished task
    # that the master already acknowledged.
    notified: bool = False

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


def _fmt_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"
    return f"{int(seconds // 3600)}h{int((seconds % 3600) // 60):02d}m"


class AsyncTaskMirror:
    """Thread-safe mirror of all async tasks the daemon has launched.

    Owned by ``DaemonSubsystems`` / passed through ``OrchestratorDeps``. Single
    writer (the watcher) and multiple readers (orchestrator turn-start injector,
    Electron event feed). Internal asyncio.Lock guards mutations.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, MirroredTask] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Writers
    # ------------------------------------------------------------------

    async def upsert(self, task: MirroredTask) -> None:
        async with self._lock:
            self._tasks[task.task_id] = task

    async def set_status(
        self,
        task_id: str,
        status: str,
        *,
        summary: str | None = None,
        error: str | None = None,
        pending_ask_id: str | None = None,
    ) -> MirroredTask | None:
        async with self._lock:
            cur = self._tasks.get(task_id)
            if cur is None:
                return None
            updated = replace(
                cur,
                status=status,
                last_update_at=time.time(),
                summary=summary if summary is not None else cur.summary,
                error=error if error is not None else cur.error,
                pending_ask_id=pending_ask_id,
                completed_at=time.time() if status in TERMINAL_STATUSES else cur.completed_at,
            )
            self._tasks[task_id] = updated
            return updated

    async def mark_notified(self, task_id: str) -> MirroredTask | None:
        """Flag that the master has been told about this task's completion."""
        async with self._lock:
            cur = self._tasks.get(task_id)
            if cur is None:
                return None
            updated = replace(cur, notified=True)
            self._tasks[task_id] = updated
            return updated

    async def remove(self, task_id: str) -> None:
        async with self._lock:
            self._tasks.pop(task_id, None)

    # ------------------------------------------------------------------
    # Readers
    # ------------------------------------------------------------------

    def list_all(self) -> list[MirroredTask]:
        # Snapshot reads don't need the lock — dict.values() is atomic and we
        # hand back a new list.
        return list(self._tasks.values())

    def list_non_terminal(self) -> list[MirroredTask]:
        return [t for t in self._tasks.values() if not t.is_terminal()]

    def list_recent_completed(self, *, unnotified_only: bool = True) -> list[MirroredTask]:
        """Terminal tasks the master may not have acknowledged yet.

        Used by ``render_block`` so a completion the wake-up path couldn't deliver
        (e.g. no known parent thread) still reaches the master on its next turn.
        """
        out = [
            t for t in self._tasks.values()
            if t.is_terminal() and (not unnotified_only or not t.notified)
        ]
        out.sort(key=lambda t: t.completed_at or t.last_update_at)
        return out

    def get(self, task_id: str) -> MirroredTask | None:
        return self._tasks.get(task_id)

    def count_running(self) -> int:
        return sum(1 for t in self._tasks.values() if not t.is_terminal())

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render_block(self, *, max_lines: int = 10) -> str:
        """Format the in-flight tasks block injected at each master turn.

        Returns an empty string when no tasks exist so we don't pollute the
        master's prompt with noise.
        """
        non_terminal = self.list_non_terminal()
        completed = self.list_recent_completed(unnotified_only=True)
        if not non_terminal and not completed:
            return ""
        now = time.time()
        lines: list[str] = []
        if non_terminal:
            non_terminal.sort(key=lambda t: t.started_at)
            lines.append("Background tasks in flight:")
            for t in non_terminal[:max_lines]:
                elapsed = _fmt_elapsed(now - t.started_at)
                tail = ""
                if t.status == "awaiting_user" and t.pending_ask_id:
                    tail = f"   (Ask {t.pending_ask_id[:8]} pending)"
                lines.append(
                    f"  - task={t.task_id[:8]}  agent={t.agent_name}  "
                    f"status={t.status}  elapsed={elapsed}{tail}"
                )
            if len(non_terminal) > max_lines:
                lines.append(f"  - …and {len(non_terminal) - max_lines} more.")
        if completed:
            lines.append("Background tasks that finished (tell the user):")
            for t in completed[:max_lines]:
                verb = "ok" if t.status == "success" else t.status
                summ = (t.summary or "").replace("\n", " ")[:120]
                lines.append(
                    f"  - task={t.task_id[:8]}  agent={t.agent_name}  "
                    f"result={verb}  {summ}"
                )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Bulk operations (lifecycle)
    # ------------------------------------------------------------------

    async def mark_all_cancelled(self, reason: str = "daemon_shutdown") -> Iterable[MirroredTask]:
        """Used at SIGTERM: cancel every non-terminal record."""
        out = []
        async with self._lock:
            for tid, t in list(self._tasks.items()):
                if t.is_terminal():
                    continue
                updated = replace(
                    t,
                    status="cancelled",
                    error=reason,
                    completed_at=time.time(),
                    last_update_at=time.time(),
                )
                self._tasks[tid] = updated
                out.append(updated)
        return out
