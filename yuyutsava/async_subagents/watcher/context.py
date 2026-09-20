"""What every part of the watcher needs: the host, the mirror, and who to tell.

The units below never call each other. That works because the things they would
call each other for are here — the client, the mirror, the event sink, the
completion sink, and the four per-task sets that must be shared or an
"exactly once" stops being once:

* ``known_threads`` — a thread is ingested once, not on every discovery pass;
* ``handled_interrupts`` — an interrupt is asked once, not again while a slow
  resume is still in flight;
* ``progress_seen`` — only steps newer than the last cycle are reported;
* ``healed_threads`` — a wedged thread is patched once, not on every poll.

Routing between the units — what a poll verdict means — is the watcher's job,
because it is a decision about which unit acts and not work any one of them owns.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from yuyutsava.async_subagents.launch_index import LaunchIndex
from yuyutsava.async_subagents.mirror import AsyncTaskMirror, MirroredTask
from yuyutsava.ports.channels import AskPrompt, ChannelEvent

logger = logging.getLogger("yuyutsava.async_subagents.watcher")

AskHandler = Callable[[AskPrompt], Awaitable[str]]
EventSink = Callable[[ChannelEvent], Awaitable[None]]
#: Called once per task when it reaches a terminal status, so the daemon can
#: wake the master agent on the originating thread.
CompletionSink = Callable[[MirroredTask, bool, str], Awaitable[None]]


@dataclass
class WatcherContext:
    """One background-subagent host, as the watcher's parts see it."""

    mirror: AsyncTaskMirror
    ask: AskHandler
    emit: EventSink
    agent_path_root: str = "orchestrator"
    task_timeout: float = 3600.0
    #: ``None`` (CLI Mode-1) means the watcher only emits the UI completion
    #: event and wakes nobody.
    completion_sink: CompletionSink | None = None
    #: Links a discovered sub-thread back to the launching turn and channel.
    launch_index: LaunchIndex | None = None
    #: The langgraph-sdk client. Absent until the watcher starts.
    client: Any | None = None

    known_threads: set[str] = field(default_factory=set)
    handled_interrupts: dict[str, set[str]] = field(default_factory=dict)
    progress_seen: dict[str, int] = field(default_factory=dict)
    healed_threads: set[str] = field(default_factory=set)

    async def notify_complete(self, task: MirroredTask, ok: bool, summary: str) -> None:
        """Hand a finished task to the completion sink, exactly once.

        A task reaches a terminal status only once — it then drops out of
        ``list_non_terminal`` — so this is naturally called once per task; the
        ``notified`` re-check is defensive. The sink decides whether a master
        wake-up is possible and flips ``notified`` when it enqueues one.
        """
        if self.completion_sink is None:
            return
        cur = self.mirror.get(task.task_id) or task
        if cur.notified:
            return
        try:
            await self.completion_sink(cur, ok, summary)
        except Exception:
            logger.exception("completion_sink failed for %s", task.task_id)

    def forget(self, task_id: str) -> None:
        """Drop the per-task bookkeeping for a task that has finished."""
        self.progress_seen.pop(task_id, None)


__all__ = ["AskHandler", "CompletionSink", "EventSink", "WatcherContext"]
