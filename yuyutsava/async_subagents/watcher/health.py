"""The watcher: a loop, and who handles what it finds.

deepagents' ``AsyncSubAgentMiddleware`` starts background runs and lets the
master poll their status. It does not watch them for ``interrupt()`` events or
surface them to anyone, so a background subagent that needs a decision waits
forever. This closes that gap, and is the only thing in the system that does.

It owns two things and delegates the rest: the poll loop's lifetime, and the
routing of what a poll found. Routing is genuinely its own — deciding that an
"interrupted" verdict is the interrupt handler's and a "timeout" is the
reporter's is a statement about the parts, not work any one part does.

  :mod:`polling`     what changed on the host
  :mod:`interrupts`  asking a person, and resuming
  :mod:`terminal`    what a finished (or timed-out) run says, once
  :mod:`context`     the host, the mirror, and the state they share
  :mod:`parsing`     reading the shapes the Agent Protocol returns
"""

from __future__ import annotations

import asyncio
import logging

from langgraph_sdk import get_client

from yuyutsava.aio.tasks import cancel_and_reap
from yuyutsava.async_subagents.launch_index import LaunchIndex
from yuyutsava.async_subagents.mirror import AsyncTaskMirror
from yuyutsava.async_subagents.watcher.context import (
    AskHandler,
    CompletionSink,
    EventSink,
    WatcherContext,
)
from yuyutsava.async_subagents.watcher.interrupts import InterruptHandler
from yuyutsava.async_subagents.watcher.polling import TaskPoller
from yuyutsava.async_subagents.watcher.terminal import TerminalReporter

logger = logging.getLogger("yuyutsava.async_subagents.watcher")


class AsyncTaskHealthWatcher:
    """Polls a background-subagent host and acts on what it reports."""

    def __init__(
        self,
        *,
        mirror: AsyncTaskMirror,
        host_url: str,
        ask_handler: AskHandler,
        event_sink: EventSink,
        agent_path_root: str = "orchestrator",
        poll_interval_sec: float = 1.5,
        per_task_timeout_sec: float = 3600.0,
        headers: dict[str, str] | None = None,
        completion_sink: CompletionSink | None = None,
        launch_index: LaunchIndex | None = None,
    ) -> None:
        self._ctx = WatcherContext(
            mirror=mirror,
            ask=ask_handler,
            emit=event_sink,
            agent_path_root=agent_path_root,
            task_timeout=per_task_timeout_sec,
            completion_sink=completion_sink,
            launch_index=launch_index,
        )
        self._host_url = host_url
        self._headers = headers
        self._poll = poll_interval_sec
        self._poller = TaskPoller(self._ctx)
        self._interrupts = InterruptHandler(self._ctx)
        self._terminal = TerminalReporter(self._ctx)
        self._stop = asyncio.Event()
        self._loop_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._loop_task is not None:
            return
        self._ctx.client = get_client(url=self._host_url, headers=self._headers)
        self._stop.clear()
        self._loop_task = asyncio.create_task(self._run_loop(), name="async-task-watcher")

    async def shutdown(self) -> None:
        self._stop.set()
        if self._loop_task is not None:
            try:
                await asyncio.wait_for(self._loop_task, timeout=5.0)
            except asyncio.TimeoutError:
                await cancel_and_reap(self._loop_task, what="async task watcher loop")
            self._loop_task = None
        # Best-effort: cancel every non-terminal run on the server.
        if self._ctx.client is not None:
            for t in self._ctx.mirror.list_non_terminal():
                if not t.sub_thread_id:
                    continue
                try:
                    runs = await self._ctx.client.runs.list(thread_id=t.sub_thread_id, limit=5)
                    for r in runs or []:
                        rid = r.get("run_id") if isinstance(r, dict) else getattr(r, "run_id", None)
                        if not rid:
                            continue
                        try:
                            await self._ctx.client.runs.cancel(
                                thread_id=t.sub_thread_id, run_id=rid
                            )
                        except Exception:  # noqa: BLE001 — shutting down; the mirror is marked below
                            logger.debug("shutdown: cancelling run %s failed", rid, exc_info=True)
                except Exception:  # noqa: BLE001 — shutting down; the mirror is marked below
                    logger.debug(
                        "shutdown: listing runs on %s failed", t.sub_thread_id, exc_info=True
                    )
        await self._ctx.mirror.mark_all_cancelled(reason="daemon_shutdown")

    async def _run_loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await self._poller.discover_new_threads()
                    await self._poll_known_tasks()
                except Exception:
                    logger.exception("watcher cycle failed")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll)
                except asyncio.TimeoutError:
                    pass
        finally:
            logger.debug("watcher loop exiting")

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    async def _poll_known_tasks(self) -> None:
        """Ask the poller about every live task, and act on what it says."""
        for task in self._ctx.mirror.list_non_terminal():
            verdict = await self._poller.classify(task)
            if verdict is None:
                continue
            if verdict.kind == "timeout":
                await self._terminal.enforce_timeout(task)
            elif verdict.kind == "interrupted":
                await self._interrupts.handle(task, verdict.run_id, verdict.thread)
            elif verdict.kind == "terminal":
                if verdict.status == "error":
                    # A run that errored on an orphaned tool_call leaves the
                    # thread wedged: every later read fails validation. Patch
                    # it with the thread already in hand, then report the
                    # error normally.
                    await self._terminal.heal_orphaned_tool_calls(task, verdict.thread)
                await self._terminal.report(task, verdict.run, verdict.status)

    # ------------------------------------------------------------------
    # Asked of the watcher from outside
    # ------------------------------------------------------------------

    async def resume_interrupt(self, task_id: str, interrupt_id: str | None, reply: str) -> bool:
        """Push a reply into a task parked on an interrupt. Returns success."""
        return await self._interrupts.resume(task_id, interrupt_id, reply)

    async def get_task_transcript(self, task_id: str) -> list[dict] | None:
        """The sub-thread's messages, for the UI's per-task log view."""
        return await self._poller.get_task_transcript(task_id)


__all__ = ["AsyncTaskHealthWatcher"]
